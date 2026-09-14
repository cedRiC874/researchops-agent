// Uses an already-installed Playwright; no dependency installation or runtime calls.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { once } from 'node:events';
import { mkdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { createViewerServer } from '../server.mjs';
import { loadCatalog } from '../catalog.mjs';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

test('browser interactions, safe rendering and responsive layouts', async t => {
  const server = createViewerServer(); server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => new Promise(resolve => server.close(resolve)));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ headless: true,
    ...(process.env.RUN_VIEWER_BROWSER ? { executablePath: process.env.RUN_VIEWER_BROWSER } : {}) });
  t.after(() => browser.close());
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, permissions: ['clipboard-read', 'clipboard-write'] });
  const page = await context.newPage(); const errors = [], forbiddenRequests = [];
  page.on('pageerror', error => errors.push(error.message));
  await context.route('**/*', route => {
    if (!route.request().url().startsWith(`${base}/`) || !['GET', 'HEAD'].includes(route.request().method())) {
      forbiddenRequests.push(route.request().url()); return route.abort();
    }
    return route.continue();
  });
  const waitTitle = text => page.waitForFunction(text => document.getElementById('run-title').textContent === text && !document.getElementById('run-detail').hidden, text);
  const screenshot = async name => {
    if (process.env.RUN_VIEWER_SCREENSHOT_DIR) {
      mkdirSync(process.env.RUN_VIEWER_SCREENSHOT_DIR, { recursive: true });
      await page.screenshot({ path: join(process.env.RUN_VIEWER_SCREENSHOT_DIR, name), fullPage: true });
    }
  };
  await page.goto(base); await waitTitle('Depth-60 开发集运行');
  await t.test('historical summary, independent labels, evidence and pagination', async () => {
    assert.match(await page.locator('#statuses').innerText(), /已完成 · 60\/60/);
    assert.match(await page.locator('#statuses').innerText(), /通过 20\/60/);
    assert.equal(await page.locator('.status-card').filter({ hasText: '外部验收' }).locator('.status-value').innerText(), '未提供');
    assert.match(await page.locator('#timeline-empty').innerText(), /未提供/);
    assert.match(await page.locator('#metrics').innerText(), /235943/);
    await page.locator('#results summary').click();
    assert.match(await page.locator('#results').innerText(), /不是工具 evidence ID/);
    await page.locator('#issue-next').click(); assert.match(await page.locator('#issue-page').innerText(), /2\/3 页/);
    await page.locator('#issue-prev').click();
    await page.getByRole('button', { name: '复制 8805d914-9974-4eeb-8fb4-0b081a5215d1', exact: true }).first().click();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), '8805d914-9974-4eeb-8fb4-0b081a5215d1');
    await page.waitForFunction(() => document.getElementById('copy-status').textContent === '');
    await screenshot('history-desktop.png');
  });
  await t.test('synthetic failure and source filter', async () => {
    await page.locator('#source-filter').selectOption('synthetic'); await waitTitle('响应校验失败示例');
    assert.equal(await page.locator('.run-item').count(), 2);
    assert.match(await page.locator('#source-badges').innerText(), /合成展示 fixture/);
    assert.match(await page.locator('#metrics').innerText(), /输入 tokens\n未知/);
    assert.match(await page.locator('#metrics').innerText(), /实际账单\n不适用/);
    assert.match(await page.locator('#results').innerText(), /未提供/);
  });
  await t.test('rapid source switches cannot render a stale run from another source', async () => {
    await page.locator('#source-filter').selectOption('historical'); await waitTitle('Depth-60 开发集运行');
    const delayed = async route => {
      await new Promise(resolve => setTimeout(resolve, 150));
      await route.fulfill({ json: loadCatalog().get('DEMO-FAILURE-001') });
    };
    await page.route('**/api/runs/DEMO-FAILURE-001', delayed);
    await page.locator('#source-filter').selectOption('synthetic');
    await page.locator('#source-filter').selectOption('historical'); await waitTitle('Depth-60 开发集运行');
    await page.waitForTimeout(250);
    assert.equal(await page.locator('#run-title').innerText(), 'Depth-60 开发集运行');
    await page.unroute('**/api/runs/DEMO-FAILURE-001', delayed);
    await page.locator('#source-filter').selectOption('synthetic'); await waitTitle('响应校验失败示例');
  });
  await t.test('partial completion, evidence, event filter and no terminal fabrication', async () => {
    await page.locator('[data-id="DEMO-PARTIAL-001"]').click(); await waitTitle('聚合分析部分完成示例');
    await page.locator('#results summary').click();
    assert.match(await page.locator('#results').innerText(), /DEMO-EV-001/);
    assert.match(await page.locator('#results').innerText(), /DEMO-TOOL-001/);
    await page.locator('#event-type').selectOption('pause'); assert.equal(await page.locator('#events li').count(), 1);
    await page.locator('#event-type').selectOption('terminal'); assert.equal(await page.locator('#events li').count(), 0);
    assert.match(await page.locator('#timeline-empty').innerText(), /没有匹配/);
    await page.locator('#event-type').selectOption('all');
    await page.locator('#event-query').fill('DEMO-TOOL'); assert.equal(await page.locator('#events li').count(), 1);
    await page.locator('#event-query').fill(''); await screenshot('partial-desktop.png');
  });
  await t.test('mobile layout has no page overflow', async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await screenshot('partial-mobile.png');
    await page.setViewportSize({ width: 1440, height: 1100 });
  });
  await t.test('initial delayed catalog respects the actual filter via served app.mjs', async () => {
    const racePage = await context.newPage(); racePage.on('pageerror', error => errors.push(error.message));
    let releaseCatalog, markIntercepted, hits = 0;
    const gate = new Promise(resolve => { releaseCatalog = resolve; });
    const intercepted = new Promise(resolve => { markIntercepted = resolve; });
    const detailRequests = [];
    racePage.on('request', request => {
      if (request.url().startsWith(`${base}/api/runs/`)) detailRequests.push(request.url());
    });
    await racePage.route(`${base}/api/runs`, async route => {
      hits++; markIntercepted(); await gate; await route.continue();
    });
    try {
      const script = racePage.waitForResponse(`${base}/app.mjs`);
      await racePage.goto(base, { waitUntil: 'commit' }); await intercepted;
      assert.deepEqual(await (await script).body(), readFileSync(new URL('../app.mjs', import.meta.url)));
      await racePage.locator('#source-filter').selectOption('synthetic');
      assert.equal(await racePage.locator('.run-item').count(), 0);
      assert.equal(await racePage.locator('#run-detail').isVisible(), false);
      releaseCatalog();
      await racePage.waitForFunction(() => document.getElementById('run-title').textContent === '响应校验失败示例'
        && !document.getElementById('run-detail').hidden);
      assert.equal(hits, 1, 'delayed catalog interception must actually run');
      assert.equal(await racePage.locator('#source-filter').inputValue(), 'synthetic');
      assert.deepEqual(await racePage.locator('.run-item').evaluateAll(items => items.map(item => item.dataset.id)),
        ['DEMO-FAILURE-001', 'DEMO-PARTIAL-001']);
      assert.equal(await racePage.locator('.run-item[aria-current="true"]').getAttribute('data-id'), 'DEMO-FAILURE-001');
      assert.deepEqual(detailRequests, [`${base}/api/runs/DEMO-FAILURE-001`]);
      t.diagnostic('P2 catalog injection hit=1; served app.mjs bytes matched; no historical detail request');
    } finally { releaseCatalog(); await racePage.close(); }
  });
  for (const outcome of ['success', 'failure']) {
    for (const transition of ['other-record', 'round-trip', 'newer-copy']) {
      await t.test(`delayed clipboard ${outcome} is obsolete after ${transition}`, async () => {
        const racePage = await context.newPage(); racePage.on('pageerror', error => errors.push(error.message));
        await racePage.addInitScript(() => {
          const pending = [], timerIds = new Set();
          const probe = window.__copyProbe = { calls: [], settled: [], timersCreated: 0, timersCleared: 0 };
          // The actual app click handler must call this method. Merely scheduling a rejection is not evidence.
          Object.defineProperty(navigator.clipboard, 'writeText', { configurable: true, value(value) {
            probe.calls.push(value);
            return new Promise((resolve, reject) => pending.push({ resolve, reject }));
          } });
          probe.settle = (index, outcome) => {
            if (!pending[index] || probe.settled.includes(index)) throw new Error('Clipboard injection was not pending');
            probe.settled.push(index);
            if (outcome === 'success') pending[index].resolve();
            else pending[index].reject(new Error('Injected delayed clipboard failure'));
          };
          const originalSet = window.setTimeout.bind(window), originalClear = window.clearTimeout.bind(window);
          window.setTimeout = (callback, delay, ...args) => {
            const id = originalSet(callback, delay, ...args);
            if (delay === 3500) { probe.timersCreated++; timerIds.add(id); }
            return id;
          };
          window.clearTimeout = id => {
            if (timerIds.has(id)) probe.timersCleared++;
            return originalClear(id);
          };
        });
        try {
          const script = racePage.waitForResponse(`${base}/app.mjs`);
          await racePage.goto(base); await racePage.locator('#run-detail').waitFor({ state: 'visible' });
          assert.deepEqual(await (await script).body(), readFileSync(new URL('../app.mjs', import.meta.url)));
          const originId = '8805d914-9974-4eeb-8fb4-0b081a5215d1';
          const copy = id => racePage.locator('#summary-grid').getByRole('button', { name: `复制 ${id}`, exact: true }).click();
          const navigate = async (id, title) => {
            await racePage.locator(`[data-id="${id}"]`).click();
            await racePage.waitForFunction(title => document.getElementById('run-title').textContent === title
              && !document.getElementById('run-detail').hidden, title);
          };
          const settle = (index, result) => racePage.evaluate(async ({ index, result }) => {
            window.__copyProbe.settle(index, result);
            // Flush the actual async click handler before inspecting DOM and timer side effects.
            await new Promise(resolve => queueMicrotask(() => queueMicrotask(resolve)));
          }, { index, result });
          const snapshot = () => racePage.evaluate(() => ({
            text: document.getElementById('copy-status').textContent,
            created: window.__copyProbe.timersCreated, cleared: window.__copyProbe.timersCleared,
          }));
          await copy(originId);
          assert.deepEqual(await racePage.evaluate(() => window.__copyProbe.calls), [originId]);
          assert.deepEqual(await snapshot(), { text: '', created: 0, cleared: 0 });
          if (transition !== 'newer-copy') await navigate('DEMO-FAILURE-001', '响应校验失败示例');
          if (transition === 'round-trip') {
            await navigate('depth60-public', 'Depth-60 开发集运行');
          } else {
            await copy(transition === 'other-record' ? 'DEMO-FAILURE-001' : originId);
            await settle(1, 'success');
            assert.match((await snapshot()).text, /^已复制 /);
            assert.equal((await snapshot()).created, 1, 'new current copy must own a real toast timer');
          }
          const before = await snapshot();
          await settle(0, outcome);
          assert.deepEqual(await snapshot(), before, 'obsolete callback cannot change text or create/clear a timer');
          const probe = await racePage.evaluate(() => ({ calls: window.__copyProbe.calls, settled: window.__copyProbe.settled }));
          assert.equal(probe.calls.length, transition === 'round-trip' ? 1 : 2);
          assert.ok(probe.settled.includes(0), 'old injected Promise must actually settle');
          t.diagnostic(`P2 clipboard ${outcome}/${transition}: calls=${probe.calls.length}; settled=${probe.settled.join(',')}; obsolete text/timer mutations=0; served app.mjs bytes matched`);
        } finally { await racePage.close(); }
      });
    }
  }
  await t.test('hostile model text stays text, even without CSP; missing metrics and large events', async () => {
    const hostile = '<img src=x onerror="window.__executed=1"><script>window.__executed=1</script>[click](javascript:alert(1))';
    const run = structuredClone(loadCatalog().get('DEMO-FAILURE-001'));
    run.description = hostile; run.issues[0].detail = hostile;
    run.results = [{ label: hostile, detail: hostile, evidenceId: hostile, evidenceKind: hostile,
      toolCallId: { availability: 'known', value: hostile }, source: hostile, values: [] }];
    run.metrics = [{ label: '缺少字段' }, { label: '显式 null', field: { availability: 'known', value: null } },
      { label: '真实零', field: { availability: 'known', value: 0 } }];
    run.events = Array.from({ length: 101 }, (_, i) => ({ id: `DEMO-LARGE-${i}`, label: hostile, type: 'tool', detail: hostile }));
    // Browser-only interception never changes the served catalog or adds an API route.
    await page.route('**/api/runs/DEMO-FAILURE-001', route => route.fulfill({ json: run }));
    await page.route(`${base}/`, async route => {
      const response = await route.fetch(); const headers = response.headers(); delete headers['content-security-policy'];
      await route.fulfill({ response, headers });
    });
    await page.reload(); await waitTitle('Depth-60 开发集运行');
    await page.locator('[data-id="DEMO-FAILURE-001"]').click(); await waitTitle('响应校验失败示例');
    await page.locator('#results summary').click();
    assert.equal(await page.locator('#description').textContent(), hostile);
    assert.equal(await page.locator('article img, article script, article iframe').count(), 0);
    assert.equal(await page.locator('article [onerror], article a[href^="javascript:"]').count(), 0);
    assert.equal(await page.evaluate(() => window.__executed), undefined);
    assert.match(await page.locator('#metrics').innerText(), /缺少字段\n未提供/);
    assert.match(await page.locator('#metrics').innerText(), /显式 null\n未知/);
    assert.match(await page.locator('#metrics').innerText(), /真实零\n0/);
    assert.equal(await page.locator('#events li').count(), 8);
    await page.locator('#event-next').click(); assert.match(await page.locator('#event-page').innerText(), /2\/13 页/);
    await page.locator('#event-query').fill('DEMO-LARGE-100'); assert.equal(await page.locator('#events li').count(), 1);
    assert.match(await page.locator('#event-page').innerText(), /1\/1 页/);
  });
  await t.test('no write controls, off-origin traffic or browser errors', async () => {
    assert.equal(await page.locator('input[type=file],form').count(), 0);
    const buttons = await page.locator('button').allTextContents();
    assert.ok(buttons.every(text => !/^(上传|执行|重跑|批准|删除|恢复|Provider选择)$/.test(text)));
    assert.deepEqual(errors, []); assert.deepEqual(forbiddenRequests, []);
  });
});
