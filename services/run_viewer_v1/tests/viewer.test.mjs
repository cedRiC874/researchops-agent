import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { request } from 'node:http';
import { once } from 'node:events';
import { loadCatalog, projectHistory, BASELINE, SOURCE_PATH } from '../catalog.mjs';
import { display, known, absent, pageOf, safeIdentifier } from '../view-model.mjs';
import { createViewerServer } from '../server.mjs';

test('historical bytes equal the exact committed public source', () => {
  const committed = execFileSync('git', ['show', `${BASELINE}:${SOURCE_PATH}`], { cwd: new URL('../../../', import.meta.url) });
  assert.deepEqual(readFileSync(new URL('../data/depth60.public.json', import.meta.url)), committed);
});
test('history preserves execution, check denominator, provenance and unknown bill', () => {
  const run = loadCatalog().get('depth60-public');
  assert.equal(run.kind, 'historical'); assert.equal(run.events.length, 0);
  assert.equal(run.statuses.length, 4);
  assert.equal(run.statuses[0].field.value, '已完成 · 60/60');
  assert.equal(run.statuses[1].field.value, '通过 20/60');
  assert.match(run.statuses[2].field.note, /本页未回验/);
  assert.equal(run.statuses[3].field.availability, 'not_provided');
  assert.equal(run.runId.availability, 'not_published');
  assert.equal(run.publicId.value, '8805d914-9974-4eeb-8fb4-0b081a5215d1');
  const metrics = Object.fromEntries(run.metrics.map(m => [m.label, m.field]));
  assert.equal(metrics['模型请求'].value, 103);
  assert.equal(metrics['输入 tokens'].value, 235943);
  assert.equal(metrics['输出 tokens'].value, 54396);
  assert.equal(metrics['实际账单'].availability, 'unknown');
  assert.equal(metrics['估算费用'].value, 'CNY 1.197393');
  assert.equal(run.results[0].toolCallId.availability, 'not_provided');
});
test('modified historical input fails closed without another source', () => {
  assert.throws(() => projectHistory(Buffer.from('{}')), /integrity mismatch/);
});
test('failure and partial fixtures remain explicitly synthetic', () => {
  const catalog = loadCatalog();
  for (const id of ['DEMO-FAILURE-001', 'DEMO-PARTIAL-001']) {
    const run = catalog.get(id); assert.equal(run.kind, 'synthetic');
    assert.match(run.source.detail, /未发生 Provider 调用/);
    assert.equal(run.statuses[3].field.availability, 'not_applicable');
    for (const event of run.events) assert.match(event.label, /模拟/);
  }
  assert.equal(catalog.get('DEMO-FAILURE-001').results.length, 0);
  const partial = catalog.get('DEMO-PARTIAL-001');
  assert.equal(partial.events.at(-1).type, 'pause');
  assert.equal(partial.results[0].toolCallId.value, 'DEMO-TOOL-001');
  assert.ok(partial.events.some(e => e.id === partial.results[0].toolCallId.value));
  assert.match(partial.notExecuted, /终态记录未提供/);
});
test('missing, null, unknown, not applicable and true zero stay distinct', () => {
  assert.equal(display(undefined), '未提供'); assert.equal(display(null), '未提供');
  assert.equal(display(known(null)), '未知'); assert.equal(display(known(undefined)), '未知');
  assert.equal(display(absent('unknown')), '未知');
  assert.equal(display(absent('not_applicable')), '不适用');
  assert.equal(display(absent('not_published')), '未公开');
  assert.equal(display(known(0)), '0'); assert.equal(display(known(false)), '否');
  assert.throws(() => absent('invented'));
  assert.throws(() => absent('toString'));
  assert.equal(display({ availability: 'toString' }), '未知');
});
test('large records paginate before DOM rendering; filters clamp stale pages', () => {
  const rows = Array.from({ length: 10001 }, (_, i) => ({ id: `E-${i}`, type: i % 2 ? 'tool' : 'request', label: `event ${i}` }));
  assert.equal(pageOf(rows).items.length, 8);
  assert.equal(pageOf(rows, '', 'all', 1251).items.length, 1);
  assert.equal(pageOf(rows, 'E-9999', 'tool', 900).page, 1);
  assert.equal(pageOf(rows, 'absent').count, 0);
  assert.equal(pageOf(rows, '', 'tool').count, 5000);
  assert.equal(pageOf(rows, '', 'all', 1, 9999).items.length, 100);
});
test('copy accepts only bounded safe identifiers, not paths or markup', () => {
  for (const value of ['DEMO-EV-001', '8805d914-9974-4eeb-8fb4-0b081a5215d1']) assert.equal(safeIdentifier(value), true);
  for (const value of ['../secret', 'C:/key', 'https://host', '<script>', 'a'.repeat(129), null]) assert.equal(safeIdentifier(value), false);
});
test('server exposes only local GET/HEAD allowlisted assets and run IDs', async t => {
  const server = createViewerServer(); server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => new Promise(resolve => server.close(resolve)));
  const port = server.address().port;
  const get = (path, method = 'GET', headers = {}) => new Promise((resolve, reject) => {
    const req = request({ host: '127.0.0.1', port, path, method, headers }, res => {
      let body = ''; res.setEncoding('utf8'); res.on('data', data => { body += data; });
      res.on('end', () => resolve({ status: res.statusCode, body, headers: res.headers }));
    }); req.on('error', reject); req.end();
  });
  const history = await get('/api/runs/depth60-public');
  assert.equal(history.status, 200); assert.equal(JSON.parse(history.body).id, 'depth60-public');
  assert.equal(JSON.parse((await get('/api/runs')).body).length, 3);
  const root = await get('/'); assert.equal(root.status, 200);
  assert.match(root.headers['content-security-policy'], /script-src 'self'/);
  assert.match(root.headers['content-security-policy'], /form-action 'none'/);
  assert.equal(root.headers['access-control-allow-origin'], undefined);
  assert.equal(root.headers['x-content-type-options'], 'nosniff');
  assert.equal((await get('/', 'HEAD')).body, '');
  for (const path of ['/../README.md', '/%2e%2e/README.md', '/data/depth60.public.json', '/catalog.mjs', '/server.mjs', '/api/runs/unknown', '/api/runs/depth60-public?path=C:/key', '/?path=../../output', '/api/runs/%44EMO-FAILURE-001', '//api/runs', '/api/runs/DEMO-PARTIAL-001/']) {
    assert.equal((await get(path)).status, 404, path);
  }
  for (const method of ['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']) assert.equal((await get('/api/runs', method)).status, 405);
  assert.equal((await get('/', 'GET', { Host: 'attacker.invalid' })).status, 403);
  assert.equal((await get('/', 'GET', { Origin: 'https://attacker.invalid' })).status, 403);
});
