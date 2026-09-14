import { display, pageOf, safeIdentifier } from './view-model.mjs';

const $ = id => document.getElementById(id);
const node = (tag, text = '', className = '') => {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
};
let catalog = [], current = null, eventPage = 1, issuePage = 1, requestVersion = 0;
let copyTimer, copyVersion = 0;
function fieldNode(field) {
  const wrapper = node('span', display(field));
  if (field?.note) wrapper.append(node('small', field.note, 'field-note'));
  if (field?.source) wrapper.title = field.source;
  return wrapper;
}
function copyId(value) {
  const wrap = node('span');
  wrap.append(node('span', value, 'identifier'));
  if (!safeIdentifier(value)) return wrap;
  const button = node('button', '复制', 'copy');
  button.type = 'button'; button.setAttribute('aria-label', `复制 ${value}`);
  button.addEventListener('click', async () => {
    const selection = requestVersion, operation = ++copyVersion;
    const isCurrent = () => selection === requestVersion && operation === copyVersion;
    clearTimeout(copyTimer); copyTimer = undefined; $('copy-status').textContent = '';
    let message;
    try { await navigator.clipboard.writeText(value); message = `已复制 ${value}`; }
    catch { message = '剪贴板不可用，请选中标识符手动复制。'; }
    // Both success and failure may arrive after navigation or a newer copy, including A → B → A.
    if (!isCurrent()) return;
    $('copy-status').textContent = message;
    copyTimer = setTimeout(() => {
      if (!isCurrent()) return;
      $('copy-status').textContent = ''; copyTimer = undefined;
    }, 3500);
  });
  wrap.append(button); return wrap;
}
function definition(label, field, copy = false) {
  const wrapper = node('div'), dd = node('dd');
  wrapper.append(node('dt', label));
  if (copy && field?.availability === 'known' && safeIdentifier(field.value)) {
    dd.append(copyId(field.value));
    if (field.note) dd.append(node('small', field.note, 'field-note'));
  } else dd.append(fieldNode(field));
  wrapper.append(dd); return wrapper;
}
async function json(path) {
  const response = await fetch(path, { cache: 'no-store', credentials: 'omit' });
  if (!response.ok) throw new Error('read_failed');
  return response.json();
}
function renderList() {
  $('run-list').replaceChildren();
  for (const run of catalog.filter(run => $('source-filter').value === 'all' || run.kind === $('source-filter').value)) {
    const button = node('button', '', 'run-item'); button.type = 'button'; button.dataset.id = run.id;
    button.setAttribute('aria-current', String(current?.id === run.id));
    button.append(node('strong', run.title), node('small', run.sourceLabel));
    button.addEventListener('click', () => selectRun(run.id)); $('run-list').append(button);
  }
}
function selectVisibleRun() {
  renderList();
  const visible = catalog.filter(run => $('source-filter').value === 'all' || run.kind === $('source-filter').value);
  const selected = visible.find(run => run.id === current?.id) || visible[0];
  // Read the actual filter now, including changes made while the initial catalog was loading.
  if (selected) return selectRun(selected.id);
}
function renderEvents() {
  if (!current) return;
  const page = pageOf(current.events, $('event-query').value, $('event-type').value, eventPage);
  eventPage = page.page; $('events').replaceChildren();
  for (const event of page.items) {
    const li = node('li'); li.append(node('strong', event.label));
    const info = node('div', '', 'event-info'); info.append(copyId(event.id), node('span', ` · 时间：${display(event.time)}`));
    li.append(info, node('p', event.detail)); $('events').append(li);
  }
  $('timeline-empty').hidden = page.count > 0;
  $('timeline-empty').textContent = current.events.length ? '没有匹配的事件。' : '未提供事件明细；请求、工具调用、响应、暂停及终态的逐步记录均无法展开。';
  $('event-pager').hidden = current.events.length === 0;
  $('event-page').textContent = `${page.page}/${page.pages} 页 · 匹配 ${page.count}/${page.total} 条`;
  $('event-prev').disabled = page.page <= 1; $('event-next').disabled = page.page >= page.pages;
}
function renderIssues() {
  const page = pageOf(current.issues, '', 'all', issuePage, 4); issuePage = page.page;
  $('issues').replaceChildren();
  for (const issue of page.items) {
    const item = node('div', '', 'issue'); item.append(node('h3', issue.label), node('p', issue.detail));
    if (issue.source) item.append(node('p', issue.source, 'source-line'));
    $('issues').append(item);
  }
  if (!page.count) $('issues').append(node('p', '未提供问题明细。', 'empty'));
  $('issue-page').textContent = `${page.page}/${page.pages} 页 · ${page.count} 条`;
  $('issue-prev').disabled = page.page <= 1; $('issue-next').disabled = page.page >= page.pages;
}
function render(run) {
  current = run; eventPage = 1; issuePage = 1;
  $('event-query').value = ''; $('event-type').value = 'all'; $('provenance').open = false;
  $('run-title').textContent = run.title; $('description').textContent = run.description;
  document.title = `${run.title} · ResearchOps 只读详情`;
  $('source-badges').replaceChildren(node('span', run.sourceLabel, `badge ${run.kind === 'synthetic' ? 'synthetic' : ''}`), node('span', run.dataLabel, 'badge secondary'));
  $('summary-grid').replaceChildren(
    definition('运行 ID', run.runId, true), definition('公开证据 ID', run.publicId, true),
    definition('执行版本 / 展示版本', run.version), definition('运行范围', { value: run.scope, availability: 'known' }),
    definition('材料记录时间（UTC）', run.recordedAt), definition('运行起止时间', { value: `${display(run.startedAt)} → ${display(run.endedAt)}`, availability: 'known' }));
  $('source-detail').replaceChildren(node('p', run.source.detail), node('p', `公开来源：${run.source.path}`),
    node('p', `读取基线：${run.source.baseline}`), node('p', `历史执行源码锚点：${display(run.executionCommit)}`));
  if (run.source.sha256) $('source-detail').append(node('p', `公开文件 SHA-256：${run.source.sha256}`));
  for (const text of run.boundaries) $('source-detail').append(node('p', text));
  $('statuses').replaceChildren();
  for (const state of run.statuses) {
    const card = node('div', '', `status-card ${state.tone}`);
    card.append(node('p', state.label, 'status-label'), node('p', display(state.field), 'status-value'), node('p', state.field.note || '', 'field-note'));
    $('statuses').append(card);
  }
  $('timeline-note').textContent = run.timelineNote;
  $('metrics').replaceChildren(...run.metrics.map(({ label, field }) => definition(label, field)));
  $('results').replaceChildren();
  for (const result of run.results) {
    const card = node('div', '', 'result-card'); card.append(node('h3', result.label), node('p', result.detail));
    const details = node('details'); details.append(node('summary', '展开聚合结果与 evidence 来源'));
    const metrics = node('dl'); metrics.append(...result.values.map(({ label, field }) => definition(label, field)));
    details.append(metrics, node('p', result.evidenceKind));
    if (result.evidenceId) details.append(copyId(result.evidenceId));
    const call = node('p', '对应工具调用：');
    if (result.toolCallId?.availability === 'known' && safeIdentifier(result.toolCallId.value)) call.append(copyId(result.toolCallId.value));
    else call.append(fieldNode(result.toolCallId));
    details.append(call, node('p', `来源：${result.source}`, 'source-line')); card.append(details); $('results').append(card);
  }
  if (!run.results.length) $('results').append(node('p', '未提供可展开的结果或 evidence 关联。', 'empty'));
  $('not-executed').textContent = run.notExecuted; $('next-step').textContent = run.nextStep;
  $('claim-boundary').textContent = run.boundaries.join(' ');
  renderEvents(); renderIssues(); renderList(); $('run-detail').hidden = false;
}
async function selectRun(id) {
  const version = ++requestVersion;
  ++copyVersion;
  clearTimeout(copyTimer); copyTimer = undefined; $('copy-status').textContent = '';
  $('loading').hidden = false; $('run-detail').hidden = true; $('load-error').hidden = true;
  try {
    if (!catalog.some(run => run.id === id)) throw new Error('unknown_id');
    const run = await json(`/api/runs/${id}`);
    if (version !== requestVersion) return;
    render(run);
  } catch {
    if (version !== requestVersion) return;
    $('load-error').textContent = '无法读取白名单记录。请检查本任务预览服务及固定样例完整性。'; $('load-error').hidden = false;
  } finally { if (version === requestVersion) $('loading').hidden = true; }
}
$('source-filter').addEventListener('change', selectVisibleRun);
for (const id of ['event-type', 'event-query']) $(id).addEventListener(id === 'event-query' ? 'input' : 'change', () => { eventPage = 1; renderEvents(); });
$('event-prev').addEventListener('click', () => { eventPage--; renderEvents(); });
$('event-next').addEventListener('click', () => { eventPage++; renderEvents(); });
$('issue-prev').addEventListener('click', () => { issuePage--; renderIssues(); });
$('issue-next').addEventListener('click', () => { issuePage++; renderIssues(); });
try { catalog = await json('/api/runs'); await selectVisibleRun(); }
catch { $('loading').hidden = true; $('load-error').textContent = '无法读取公开样例目录。请按说明启动本地只读服务。'; $('load-error').hidden = false; }
