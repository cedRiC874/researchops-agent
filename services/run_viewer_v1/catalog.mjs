import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { known as k, absent as a } from './view-model.mjs';

export const BASELINE = '551b9e252670be871ff75b0638b033b07d3c6f08';
export const SOURCE_PATH = 'docs/evidence/phase6-deepseek-depth60-v1/public_summary.json';
export const SOURCE_HASH = 'b0a549aac1f42b7a389fcffca91172299897979ed8d2b677b55d941c80a4bb9e';
const metric = (label, field) => ({ label, field });
const status = (label, value, note, tone = 'neutral') => ({ label, field: k(value, note), tone });

export function projectHistory(bytes) {
  if (createHash('sha256').update(bytes).digest('hex') !== SOURCE_HASH) {
    throw new Error('Public source integrity mismatch; no fallback permitted');
  }
  const s = JSON.parse(bytes);
  const field = (value, pointer, note = '') => k(value, note, `${SOURCE_PATH}#${pointer}`);
  return {
    id: 'depth60-public', title: 'Depth-60 开发集运行', kind: 'historical',
    sourceLabel: '公开历史记录', dataLabel: '固定合成任务 · development',
    description: '60 项执行已结束，20 项通过逐题检查。执行完成不代表全部检查通过。',
    scope: '批次聚合 · 60 项任务 · holdout 0 项',
    runId: a('not_published', '公开摘要没有单次运行 ID；下面的证据 ID 标识公开材料。'),
    publicId: k(s.public_evidence_id), version: k(`Runner ${s.plan.runner_version} · ${s.plan.model_alias}`,
      '模型别名可变；这里记录历史版本，不查询当前 Provider。'),
    executionCommit: k(s.repository_anchor.commit),
    recordedAt: field(s.documented_at_utc, 'documented_at_utc', '材料记录时间，不是运行开始时间'),
    startedAt: a(), endedAt: a(),
    source: { baseline: BASELINE, path: SOURCE_PATH, sha256: SOURCE_HASH,
      detail: '固定基线的公开脱敏摘要，按原字节收录。页面未重新校验原始审计链。' },
    statuses: [
      status('执行状态', '已完成 · 60/60', 'runtime_failures=0；harness_errors=0', 'good'),
      status('逐题检查', '通过 20/60', '40 项未通过；逐题明细未公开', 'warning'),
      status('归档回验', '历史记录称通过', '审计链、artifact hash、consume/terminal 绑定均为 true；本页未回验', 'neutral'),
      { label: '外部验收', field: a('not_provided', '没有对应的外部验收回执；不从执行结果推断。'), tone: 'neutral' },
    ],
    events: [], timelineNote: '未提供逐步事件。公开材料只保留聚合结果；不能由请求数推造请求、工具、响应或暂停时间线。',
    results: [{ id: 'depth60-summary', type: 'aggregate', label: '批次检查结果',
      detail: '这是一项聚合结论，不是逐题模型答案。',
      evidenceId: s.public_evidence_id, evidenceKind: '公开材料 ID（不是工具 evidence ID）',
      toolCallId: a('not_provided', '公开摘要没有对应工具调用记录'),
      source: `${SOURCE_PATH}#outcome`,
      values: [metric('计划项数', field(s.outcome.planned, 'outcome.planned')),
        metric('完成项数', field(s.outcome.completed, 'outcome.completed')),
        metric('检查通过', field(s.outcome.passed, 'outcome.passed')),
        metric('检查未通过', field(s.outcome.failed_attempted, 'outcome.failed_attempted')),
        metric('未开始', field(s.outcome.not_started, 'outcome.not_started'))] }],
    metrics: [
      metric('模型请求', field(s.usage.model_requests, 'usage.model_requests', '批次计数；不是账户侧网络请求总量')),
      metric('输入 tokens', field(s.usage.input_tokens, 'usage.input_tokens')),
      metric('输出 tokens', field(s.usage.output_tokens, 'usage.output_tokens')),
      metric('用量覆盖', field('100%', 'usage.coverage', '来源 usage.status=complete')),
      metric('整批墙钟耗时', a('not_provided')),
      metric('Agent 段 P50', field(`${(s.latency_ms.p50_nearest_rank / 1000).toFixed(3)} 秒`, 'latency_ms.p50_nearest_rank', '60 项；nearest-rank')),
      metric('Agent 段 P95', field(`${(s.latency_ms.p95_nearest_rank / 1000).toFixed(3)} 秒`, 'latency_ms.p95_nearest_rank', '不是纯 Provider 延迟或 SLA')),
      metric('估算费用', field(`CNY ${s.cost.estimated_cost}`, 'cost.estimated_cost', '冻结峰时价格；全部输入按缓存未命中计价；不是账单')),
      metric('实际账单', a('unknown', '来源 actual_provider_bill=null；本地费用阈值不是账单硬上限', `${SOURCE_PATH}#cost.actual_provider_bill`)),
    ],
    issues: Object.entries(s.failure_reason_counts).map(([code, count]) => ({ id: code, type: 'check', label: code,
      detail: `${count} 项命中；原因可重叠，不能相加当作失败题数。`, source: `${SOURCE_PATH}#failure_reason_counts.${code}` })),
    notExecuted: '未开始任务 0 项。具体未执行的内部步骤未提供。',
    nextStep: '逐题结果和事件链不在公开包中；进一步追溯需主线程批准的脱敏投影。',
    boundaries: ['归因于 DeepSeek 与冻结控制面的共同结果。', '不证明模型单独质量、private holdout、未知分布泛化或生产 SLA。', '20/60 保持原始口径；本页不改变历史结论。'],
  };
}

function fixtureBase(id, title) {
  return { id, title, kind: 'synthetic', sourceLabel: '合成展示 fixture', dataLabel: '虚构数据 · 离线展示',
    runId: k(id), publicId: a('not_applicable', '没有历史公开证据 ID'),
    version: k('展示 fixture v1', '不是执行器版本'), executionCommit: a('not_applicable'),
    recordedAt: a('not_applicable', '合成记录没有真实运行日期'), startedAt: a(), endedAt: a(),
    source: { baseline: BASELINE, path: 'services/run_viewer_v1/catalog.mjs',
      detail: '人工编写的展示数据。所有事件、结果及标识符均为模拟，未发生 Provider 调用。' },
    timelineNote: '以下全部为合成事件。只表示展示顺序；未提供真实时间。',
    boundaries: ['仅验证页面展示与交互，不构成历史执行、质量评估或在线验收证据。'],
  };
}
export function syntheticFailure() {
  return { ...fixtureBase('DEMO-FAILURE-001', '响应校验失败示例'),
    description: '模拟首个请求在本地校验处失败，展示“失败已知、实际用量未知”的边界。', scope: '合成场景 · 计划 3 项',
    statuses: [status('执行状态', '已失败（模拟）', '完成 0/3 项', 'danger'),
      status('逐题检查', '未执行（模拟）', '没有可检查的完整结果', 'warning'),
      { label: '归档回验', field: a('not_provided', '没有回验记录'), tone: 'neutral' },
      { label: '外部验收', field: a('not_applicable', '展示 fixture'), tone: 'neutral' }],
    events: [
      { id: 'DEMO-F-01', type: 'request', label: '请求开始（模拟）', detail: '场景 1 的模拟请求', time: a() },
      { id: 'DEMO-F-02', type: 'response', label: '本地响应校验失败（模拟）', detail: 'demo_response_validation_failed；不归因为 Provider 故障', time: a() },
      { id: 'DEMO-F-03', type: 'terminal', label: '失败终态（模拟）', detail: '未执行工具、后续场景与重试', time: a() },
    ], results: [],
    metrics: [metric('模拟请求数', k(1, '不是实际 Provider 请求')),
      metric('输入 tokens', a('unknown', '模拟缺少有效用量观测')),
      metric('输出 tokens', a('unknown', '模拟缺少有效用量观测')),
      metric('耗时', a('not_provided')),
      metric('估算费用', a('not_provided', '没有计价依据')),
      metric('实际账单', a('not_applicable', '没有实际在线调用'))],
    issues: [{ id: 'demo_response_validation_failed', type: 'runtime', label: '本地校验未通过（模拟）',
      detail: '具体响应形态与因果根因未知。', source: 'catalog.mjs / syntheticFailure' }],
    notExecuted: '模拟工具执行 0 次；后续 2 项未开始。', nextStep: '此页只展示失败位置；不提供重跑或恢复操作。',
  };
}
export function syntheticPartial() {
  return { ...fixtureBase('DEMO-PARTIAL-001', '聚合分析部分完成示例'),
    description: '模拟聚合检查已完成，受控发布暂停，后续步骤未开始。', scope: '合成场景 · 完成 1 / 暂停 1 / 未开始 1',
    statuses: [status('执行状态', '部分完成（模拟）', '1 项完成；1 项暂停；1 项未开始', 'warning'),
      status('逐题检查', '已检查 1/3（模拟）', '已检查项 1/1 通过；其余未检查', 'warning'),
      { label: '归档回验', field: a('not_provided', '没有回验记录'), tone: 'neutral' },
      { label: '外部验收', field: a('not_applicable', '展示 fixture'), tone: 'neutral' }],
    events: [
      { id: 'DEMO-P-01', type: 'request', label: '请求聚合检查（模拟）', detail: '仅使用合成聚合输入', time: a() },
      { id: 'DEMO-TOOL-001', type: 'tool', label: 'inspect_dataset（模拟）', detail: '返回 100 行、4 行存在缺失的聚合计数', time: a() },
      { id: 'DEMO-P-03', type: 'response', label: '聚合结论形成（模拟）', detail: '关联 DEMO-EV-001', time: a() },
      { id: 'DEMO-P-04', type: 'pause', label: '受控发布提案暂停（模拟）', detail: '未执行发布；没有批准或恢复操作', time: a() },
    ],
    results: [{ id: 'DEMO-CLAIM-001', type: 'aggregate', label: '100 行中 4 行存在缺失（模拟）',
      detail: '结论只使用聚合计数，不包含任何数据行。', evidenceId: 'DEMO-EV-001', evidenceKind: '合成 evidence ID',
      toolCallId: k('DEMO-TOOL-001'), source: 'catalog.mjs / syntheticPartial；模拟 inspect_dataset 聚合结构',
      values: [metric('row_count', k(100)), metric('rows_with_missing', k(4)), metric('missing_ratio', k('4%'))] }],
    metrics: [metric('模拟请求数', k(1, '不是实际 Provider 请求')),
      metric('输入 tokens', a('not_applicable', '离线展示未调用模型')),
      metric('输出 tokens', a('not_applicable', '离线展示未调用模型')),
      metric('耗时', a('not_provided')), metric('估算费用', a('not_applicable')),
      metric('实际账单', a('not_applicable', '没有实际在线调用'))],
    issues: [{ id: 'DEMO-PAUSED', type: 'pause', label: '受控操作暂停（模拟）',
      detail: '当前仅有提案，未产生发布副作用。', source: 'catalog.mjs / syntheticPartial' }],
    notExecuted: '发布未执行，最后 1 项未开始；终态记录未提供。',
    nextStep: '暂停信息仅供查看，不表示当前仍有在线任务等待处理。',
  };
}
export function loadCatalog() {
  const bytes = readFileSync(new URL('./data/depth60.public.json', import.meta.url));
  return new Map([projectHistory(bytes), syntheticFailure(), syntheticPartial()].map(run => [run.id, run]));
}
