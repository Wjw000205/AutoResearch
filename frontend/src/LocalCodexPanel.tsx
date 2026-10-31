import { ReferenceText, ReferenceNames } from './ReferenceNames';
import { useEffect, useId, useRef, useState } from 'react';
import { Code2, RefreshCw } from 'lucide-react';
import { api, date, textOf, type Json } from './api';
import { HumanMessage } from './localization';
import { CodexReceiptUsage } from './LocalCodexUsage';
import { IndexedDocument } from './IndexedDocument';
import { CurrentImplementationDocumentPanel } from './ImplementationDocumentAccess';

export interface LocalCodexStatus {
  available: boolean; version?: string | null; authenticated: boolean | null; checked_at?: string;
  error?: string; project?: { id?: string; name?: string; root?: string }; integration?: string;
}

export interface LocalCodexModel { id: string; model?: string | null; display_name?: string; supported_reasoning_efforts?: string[]; default_reasoning_effort?: string | null; is_default?: boolean }
export interface LocalCodexModelList { models: LocalCodexModel[]; checked_at?: string; error?: string }
const EFFORTS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'];
const effortLabels: Record<string, string> = { none: '无额外推理', minimal: '最低', low: '低', medium: '中', high: '高', xhigh: '更高', max: '最高', ultra: '超高' };

export function localCodexEfforts(models: LocalCodexModel[] | null, model: string): string[] {
  const matched = models?.find(item => item.id === model || item.model === model);
  return matched && Array.isArray(matched.supported_reasoning_efforts) ? matched.supported_reasoning_efforts.filter(item => EFFORTS.includes(item)) : EFFORTS;
}

export async function readLocalCodexModels(fetcher: (path: string) => Promise<LocalCodexModelList> = api): Promise<LocalCodexModelList> {
  const result = await fetcher('/local-codex/models');
  if (!result || !Array.isArray(result.models) || result.models.some(item => !item || typeof item.id !== 'string' || !item.id.trim())) throw new Error('本机 Codex 模型列表格式不完整，请重新读取。');
  return result;
}

export function LocalCodexModelControls({ model = '', effort = '', locked = false, onChange }: {
  model?: string; effort?: string; locked?: boolean; onChange: (key: string, value: string) => void;
}) {
  const id = useId(), alive = useRef(true), pending = useRef(false);
  const [catalog, setCatalog] = useState<LocalCodexModelList | null>(null), [loading, setLoading] = useState(false), [error, setError] = useState('');
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  async function load() {
    if (pending.current) return;
    pending.current = true; setLoading(true); setError('');
    try { const value = await readLocalCodexModels(); if (alive.current) setCatalog(value); }
    catch (err) { if (alive.current) { setCatalog(null); setError((err as Error).message); } }
    finally { pending.current = false; if (alive.current) setLoading(false); }
  }
  const efforts = localCodexEfforts(catalog?.models ?? null, model);
  const known = catalog?.models.find(item => item.id === model || item.model === model);
  return <><div className="field"><label htmlFor={`${id}-model`}>本机 Codex 模型</label><input id={`${id}-model`} list={`${id}-models`} value={model} readOnly={locked} placeholder="留空沿用本机默认模型" onChange={event => { onChange('codex_model', event.target.value); onChange('codex_reasoning_effort', ''); }} /><datalist id={`${id}-models`}>{catalog?.models.map(item => <option key={item.id} value={item.id}>{item.display_name || item.id}{item.is_default === true ? '（本机默认）' : ''}</option>)}</datalist><button type="button" className="btn small" disabled={loading} onClick={() => void load()}><RefreshCw size={13} className={loading ? 'spin' : ''} />{loading ? '正在读取模型列表' : '读取本机模型列表（不生成）'}</button><small>可选择已读取型号或输入模型标识；切换模型会将推理等级恢复为默认。列表读取不会生成内容。</small>{catalog && <small>已读取 {catalog.models.length} 个型号{catalog.checked_at ? ` · ${date(catalog.checked_at, true)}` : ''}</small>}{(error || catalog?.error) && <HumanMessage text={error || catalog?.error || ''} />}</div><div className="field"><label htmlFor={`${id}-effort`}>本机 Codex 推理等级</label><select id={`${id}-effort`} value={effort} disabled={locked} onChange={event => onChange('codex_reasoning_effort', event.target.value)}><option value="">沿用本机默认</option>{effort && !efforts.includes(effort) && <option value={effort} disabled>当前配置未在支持列表中：{effort}</option>}{efforts.map(value => <option key={value} value={value}>{effortLabels[value]}（<ReferenceText value={value} />）</option>)}</select><small>{known ? `按该型号已读取的支持列表选择${known.default_reasoning_effort ? `，列表默认等级为 ${effortLabels[known.default_reasoning_effort] ?? known.default_reasoning_effort}` : ''}；实际请求仍由后台验证。` : '尚未取得该型号的支持列表；暂列可选等级，是否支持由后台验证。留空沿用本机默认。'}</small>{locked && <small>实现规范已经启动，模型、推理等级、项目目录与实现方式已锁定。</small>}</div></>;
}

export function codexProjectDisplay(configured?: string, saved?: string, probe?: string) {
  if (typeof configured === 'string') return { directory: configured || '未填写',
    status: typeof saved !== 'string' ? '保存状态未确认' : configured === saved ? '已保存' : '未保存，仅为当前编辑值' };
  return { directory: probe || '未记录', status: '探测时的默认配置；当前编辑值未提供' };
}

export function LocalCodexConnection({ status, loading = false, error, onRefresh, configuredProjectDir, savedProjectDir }: {
  status?: LocalCodexStatus | null; loading?: boolean; error?: string; onRefresh?: () => void;
  configuredProjectDir?: string; savedProjectDir?: string;
}) {
  const project = codexProjectDisplay(configuredProjectDir, savedProjectDir, status?.project?.root);
  return <section className="panel local-codex-connection" aria-label="本机 Codex 探测"><header><h3><Code2 size={17} />本机 Codex</h3>{onRefresh && <button className="btn small" type="button" disabled={loading} onClick={onRefresh}><RefreshCw size={14} className={loading ? 'spin' : ''} />{loading ? '正在探测' : '重新探测（不生成）'}</button>}</header><p>探测仅检查本机命令版本，不启动生成或研究；可用不代表登录和代码生成能力已经验证。</p><dl><div><dt>本页 Codex 项目工作目录</dt><dd><code><ReferenceText value={project.directory} /></code></dd></div><div><dt>目录配置状态</dt><dd>{project.status}</dd></div></dl><p>此处跟随下方默认执行配置。保存后用于新建研究；已有研究的固定目录以任务回执为准。</p>{error && <HumanMessage text={error} />}{status ? <><p><strong>{status.available === true ? '已找到本机 Codex 命令' : '本机 Codex 命令尚不可用'}</strong></p><dl><div><dt>版本</dt><dd>{status.version || '未记录'}</dd></div><div><dt>登录状态</dt><dd>{status.authenticated === true ? '已确认登录' : status.authenticated === false ? '未登录' : '未检查，不能据此判断已登录'}</dd></div><div><dt>接入方式</dt><dd>{status.integration === 'app-server-stdio' ? '本机 Codex 应用服务（标准输入输出）' : '未记录'}</dd></div><div><dt>探测时间</dt><dd>{status.checked_at ? date(status.checked_at, true) : '未记录'}</dd></div></dl>{status.error && <HumanMessage text={status.error} />}</> : <p><ReferenceText value={loading ? '正在读取本机 Codex 探测结果。' : '尚无探测记录。'} /></p>}</section>;
}

const rows = (value: unknown): Json[] => Array.isArray(value) ? value.filter((item): item is Json => !!item && typeof item === 'object' && !Array.isArray(item)) : [];
const strings = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && !!item.trim()) : [];
const written = (value: unknown, fallback = '未记录') => typeof value === 'string' && value.trim() ? value : fallback;
const roles: Record<string, string> = { director: '研究总监', engineer: '实验工程师', scout: '文献研究员' };
const phases: Record<string, string> = { specification: '制定实现规范', spec_review: '三方评审实现规范', implementing: '本机 Codex 正在实现', acceptance: '本地验收中', code_review: '三方评审实际代码', completed: '实现流程已完成', blocked: '等待处理', rejected: '未通过', approved: '已通过', prepared: '已准备，尚未派发', thread_start_dispatched: '已请求创建会话', thread_resume_dispatched: '已请求恢复会话', thread_ready: '会话已就绪', turn_start_dispatched: '已派发实现请求', running: '执行中', failed: '执行失败', interrupted: '已中断', unknown: '执行结果尚不确定' };
const approval = (value: unknown) => value === true ? '记录为通过' : value === false ? '记录为未通过' : '结果未记录';

function CodexState({ value, scope = 'workflow' }: { value: unknown; scope?: 'workflow' | 'request' | 'check' }) {
  const raw = written(value, '');
  if (raw === 'completed' && scope !== 'workflow') return <span>{scope === 'request' ? '请求已完成，交付仍须验收' : '本地检查进程已结束'}</span>;
  return raw && Object.hasOwn(phases, raw) ? <span>{phases[raw]}</span> : raw ? <HumanMessage text={raw} /> : <span>状态未记录</span>;
}
function RecordField({ label, value }: { label: string; value: unknown }) {
  return <div className="proposal-field"><span>{label}</span><p><ReferenceText value={textOf(value) || '未记录'} /></p></div>;
}
function Issues({ value, empty = '未记录缺项。' }: { value: unknown; empty?: string }) {
  const items = strings(value);
  return items.length ? <ul>{items.map((item, index) => <li key={index}><HumanMessage text={item} /></li>)}</ul> : <p><ReferenceText value={empty} /></p>;
}

export function codexReviewMatches(vote: Json, binding: Json): boolean {
  return Object.entries(binding).length > 0 && Object.entries(binding).every(([key, value]) => typeof value === 'string' && !!value && vote[key] === value);
}

export function codexRecoveryText(attempt: Json): string {
  const checkpoint = attempt.checkpoint ?? {};
  if (['failed', 'interrupted'].includes(checkpoint.status) && written(attempt.confirmed_terminal_at, '')) {
    const terminal = checkpoint.status === 'failed' ? '失败' : '中断';
    return written(attempt.resumed_by_generation, '')
      ? `原请求已确认${terminal}，已保存一次显式续接授权。后续实现是否派发、是否通过，以对应回执与验收为准。`
      : `原请求已确认${terminal}。可使用顶部“恢复”，在原修订上限内授权一次后续实现；原请求记录保留，新请求可能再次计费。`;
  }
  if (checkpoint.recovered_read_only === true) return '已只读核对原会话与原请求；未重新派发。';
  return ['unknown', 'interrupted', 'blocked'].includes(checkpoint.status)
    ? '当前请求结果尚待核对。恢复时只读取原请求记录，不能据此假定已重试或已验收。'
    : '中断后保留原请求记录；恢复状态以回执为准。';
}

function ReviewVotes({ votes, binding, title }: { votes: unknown; binding: Json; title: string }) {
  const reviews = rows(votes);
  return <section className="codex-reviews" aria-label={title}><h4><ReferenceText value={title} /></h4><p>每位角色独立评审同一版本；角色意见与本地运行证据分别展示。</p>{Object.entries(roles).map(([role, label]) => {
    const matches = reviews.filter(vote => vote.role === role);
    const vote = matches.length === 1 ? matches[0] : null;
    const bound = vote !== null && codexReviewMatches(vote, binding);
    return <article className="provenance-card" key={role}><h5>{label}</h5>{vote ? <><strong>{bound ? vote.approve === true ? '同意（角色评议）' : vote.approve === false ? '需要修订（角色评议）' : '判断未记录' : '版本标识不匹配，不能计入本次评审'}</strong><RecordField label="评议理由" value={vote.rationale} /><h5>缺项与异议</h5><Issues value={vote.issues} />{rows(vote.fixes).map((fix, index) => <div className="codex-review-fix" key={index}><p>对应异议：{Number.isInteger(fix.issue_index) && fix.issue_index >= 0 && Array.isArray(vote.issues) && typeof vote.issues[fix.issue_index] === 'string' && vote.issues[fix.issue_index].trim() ? vote.issues[fix.issue_index] : '未匹配到已记录异议'}</p><RecordField label="修改位置" value={fix.location} /><RecordField label="修改办法" value={fix.change} /><RecordField label="验证办法" value={fix.verification} /></div>)}<details><summary>评审绑定的版本标识</summary>{Object.keys(binding).map(key => <p key={key}><code><ReferenceText value={key} />: <ReferenceText value={written(vote[key])} /></code></p>)}</details></> : <p><ReferenceText value={matches.length > 1 ? '存在多条该角色评议，需核对版本；不合并为一票。' : '尚无该角色对应版本的评议。'} /></p>}</article>;
  })}</section>;
}

function Specification({ record, runId }: { record: Json; runId: string }) {
  if (record.document_protocol !== 2 || record.implementation_indexed_review_protocol !== 1 || !record.document) return null;
  return <details className="codex-specification" open={record.approved !== true}>
    <summary>实现文档版本 {Number.isInteger(record.index) ? record.index + 1 : '未记录'} · {approval(record.approved)}</summary>
    <p>当前版本通过目录和工具交给三方阅读全文审核；读取进度与独立意见见当前审核明细。</p>
    <RecordField label="实现文档" value={record.document.title || '代码实现文档'} />
    <IndexedDocument key={record.document.sha256} runId={runId} document={record.document} label="实现" />
  </details>;
}

function Manifest({ files, snapshot }: { files: unknown; snapshot?: boolean }) {
  const manifest = rows(files);
  return <section className="codex-manifest"><h4>实际产物清单<ReferenceText value={snapshot ? '（交付快照）' : ''} /></h4>{manifest.length ? <div className="table-scroll"><table><thead><tr><th>相对文件路径</th><th>字节数</th><th>核对范围</th></tr></thead><tbody>{manifest.map((file, index) => <tr key={index}><td><code><ReferenceText value={written(file.path)} /></code></td><td>{typeof file.bytes === 'number' && Number.isFinite(file.bytes) && file.bytes >= 0 ? file.bytes : '未记录'}</td><td>{file.verified === true ? '磁盘文件已核对' : '文件核对状态未标注'}</td></tr>)}</tbody></table></div> : <p>尚无磁盘产物清单，不能用完成回复替代文件。</p>}<small>文件清单能定位这份交付，不能单独证明算法正确或科研结论成立。</small></section>;
}

function LocalAcceptance({ acceptance }: { acceptance?: Json | null }) {
  const checks = rows(acceptance?.smoke);
  return <section className="codex-local-acceptance"><h4>真实本地验收输出 · <ReferenceText value={approval(acceptance?.approved)} /></h4><p>本地检查用于验证交付结构和分支可运行性，不能当作正式科研实验、完整测试覆盖或最终独立测试。</p><Issues value={acceptance?.issues} empty="尚未记录验收缺项。" />{checks.length ? checks.map((check, index) => <details key={check.id ?? index}><summary>本地运行检查 {index + 1} · {approval(check.passed)}</summary><RecordField label="执行命令" value={Array.isArray(check.command) ? check.command.join(' ') : check.command} /><RecordField label="运行标识" value={check.id} /><p>进程状态：<CodexState value={check.status} scope="check" /> · 退出码：<ReferenceText value={typeof check.exit_code === 'number' ? check.exit_code : '未记录'} /></p><RecordField label="运行参数" value={check.params} /><RecordField label="耗时（秒）" value={check.duration_seconds} /><h5>标准输出</h5><pre tabIndex={0}><ReferenceText value={typeof check.stdout === 'string' ? check.stdout || '（已记录，输出为空）' : '未记录，不能推断运行成功'} /></pre><h5>标准错误</h5><pre tabIndex={0}><ReferenceText value={typeof check.stderr === 'string' ? check.stderr || '（已记录，输出为空）' : '未记录'} /></pre>{check.error && <HumanMessage text={textOf(check.error)} />}<Issues value={check.issues} /><details><summary>本地检查报告值（非正式实验结果）</summary><pre tabIndex={0}><ReferenceText value={textOf(check.metrics) || '未记录'} /></pre></details>{check.scope && <HumanMessage text={textOf(check.scope)} />}</details>) : <p>尚无真实本地运行输出，未验证测试是否通过。</p>}</section>;
}

export function CodexReceiptActivity({ checkpoint }: { checkpoint: Json }) {
  const types: Record<string, string> = { commandExecution: '执行本地命令', fileChange: '文件变更', contextCompaction: '上下文压缩' };
  const statuses: Record<string, string> = { inProgress: '进行中', completed: '已结束', failed: '失败', declined: '未执行' };
  const errors: Record<string, string> = { contextWindowExceeded: '上下文容量不足', sessionBudgetExceeded: '会话预算限制', usageLimitExceeded: '用量限制', rateLimitExceeded: '请求频率限制', serverOverloaded: '服务繁忙', cyberPolicy: '服务策略限制', misalignmentPolicyViolation: '服务策略限制', internalServerError: '服务内部错误', unauthorized: '鉴权错误', badRequest: '请求参数错误', sandboxError: '执行环境错误', httpConnectionFailed: '网络连接失败', responseStreamConnectionFailed: '响应流连接失败', responseStreamDisconnected: '响应流中断', responseTooManyFailedAttempts: '响应多次失败' };
  const activities = rows(checkpoint.activities).filter(item => Object.hasOwn(types, item.type)).slice(-100);
  const failures = rows(checkpoint.server_errors).slice(-12), history = rows(checkpoint.recovery_history).slice(-10);
  const first = checkpoint.first_failure && typeof checkpoint.first_failure === 'object' ? checkpoint.first_failure : null;
  return <section className="codex-recovery" aria-label="Codex 已记录执行活动"><h5>已记录执行活动</h5><p>最近收到活动：<ReferenceText value={checkpoint.last_event_at ? date(checkpoint.last_event_at, true) : '尚未记录'} />。只列事件类型与状态，不展示隐藏推理；事件结束不等于交付验收通过。</p><details><summary>最近保留的命令／文件／压缩活动 · {activities.length} 条</summary>{activities.length ? activities.map((item, index) => <p key={item.id || index}><ReferenceText value={types[item.type]} /> · <ReferenceText value={statuses[item.status] || '状态未记录'} /> · <ReferenceText value={item.source === 'readback' ? '恢复时读取原记录' : item.source === 'stream' ? '执行流回执' : '来源未记录'} /> · <ReferenceText value={date(item.observed_at, true)} /><ReferenceText value={typeof item.exit_code === 'number' && Number.isInteger(item.exit_code) ? ` · 退出码 ${item.exit_code}` : ''} /></p>) : <p>尚无这些类型的活动回执，不能据此推断未执行。</p>}</details>{first && <details><summary>首次失败记录（恢复后保留）</summary><p><CodexState value={first.status} scope="request" /> · <ReferenceText value={date(first.observed_at, true)} /></p>{typeof first.error_code === 'string' && <HumanMessage text={first.error_code} />}<p>会话：<code><ReferenceText value={written(first.thread_id)} /></code> · 请求：<code><ReferenceText value={written(first.turn_id)} /></code></p></details>}{failures.length > 0 && <details><summary>最近保留的服务错误 · {failures.length} 条</summary>{failures.map((item, index) => <div key={index}><p><ReferenceText value={errors[item.category] || '服务返回错误，类别待核对'} /><ReferenceText value={Number.isInteger(item.http_status) && item.http_status >= 100 && item.http_status <= 599 ? `（HTTP ${item.http_status}）` : ''} /> · <ReferenceText value={date(item.observed_at, true)} /></p><p><ReferenceText value={item.will_retry === true ? '服务报告将重试；不代表已经重试成功。' : '未记录服务将自动重试。'} /></p><details><summary>原始错误类别</summary><code><ReferenceText value={written(item.category)} /></code></details></div>)}</details>}{history.length > 0 && <details><summary>恢复前状态摘要 · 最近 {history.length} 条</summary>{history.map((item, index) => <div key={index}><p><ReferenceText value={date(item.observed_at, true)} /> · <CodexState value={item.previous_status} scope="request" /></p>{typeof item.previous_error_code === 'string' && <HumanMessage text={item.previous_error_code} />}</div>)}</details>}</section>;
}

function CodexAttempt({ attempt, workflow }: { attempt: Json; workflow: Json }) {
  const checkpoint = attempt.checkpoint && typeof attempt.checkpoint === 'object' ? attempt.checkpoint : {};
  const snapshot = attempt.snapshot && typeof attempt.snapshot === 'object' ? attempt.snapshot : {};
  const messages = rows(checkpoint.public_messages).filter(item => (item.phase === null || item.phase === undefined || item.phase === 'commentary' || item.phase === 'final_answer') && typeof item.text === 'string');
  const acceptance = attempt.acceptance && typeof attempt.acceptance === 'object' ? attempt.acceptance : null;
  const files = rows(attempt.files).length ? attempt.files : snapshot.files;
  return <article className="provenance-card codex-attempt"><header><h4>实现尝试 {Number.isInteger(attempt.index) ? attempt.index + 1 : '未记录'}</h4><CodexState value={attempt.status} /></header><p>Codex 请求：<CodexState value={checkpoint.status} scope="request" /></p><div className="provenance-grid"><RecordField label="实际执行目录" value={checkpoint.workspace || workflow.workspace} /><RecordField label="交付快照目录" value={snapshot.workspace} /><RecordField label="会话标识" value={checkpoint.thread_id || workflow.thread_id} /><RecordField label="本次请求标识" value={checkpoint.turn_id} /><RecordField label="线程报告的模型" value={checkpoint.reported_model || checkpoint.model} /><RecordField label="线程报告的推理等级" value={checkpoint.reported_effort} />{checkpoint.project_id && <RecordField label="回执项目标识" value={checkpoint.project_id} />}</div><p>模型与推理等级由线程配置回执报告，不代表逐次调用的底层执行已独立核实。</p><CodexReceiptUsage usage={checkpoint.usage} observedAt={checkpoint.usage_observed_at} /><CodexReceiptActivity checkpoint={checkpoint} /><div className="codex-recovery"><h5>中断与恢复</h5><p><ReferenceText value={codexRecoveryText(attempt)} /></p>{checkpoint.error_code && <HumanMessage text={String(checkpoint.error_code)} />}</div><details className="codex-public-activity"><summary>Codex 公开活动 · {messages.length} 条</summary><p>这里仅展示公开活动和回复。回复中的完成说明不能替代磁盘产物与本地验收。</p>{messages.length ? messages.map((message, index) => <article key={message.id ?? index}><strong>{message.phase === 'final_answer' ? '最终公开回复' : message.phase === 'commentary' ? '公开进度' : '公开回复'}</strong><p><ReferenceText value={message.text} /></p></article>) : <p>尚无已保存的公开活动。</p>}{checkpoint.public_output_truncated === true && <p>公开输出已截断，当前展示不是完整记录。</p>}</details><Manifest files={files} snapshot={!rows(attempt.files).length} /><LocalAcceptance acceptance={acceptance} /><ReviewVotes title="三方实际代码评审" votes={attempt.review_votes} binding={{ proposal_hash: workflow.proposal_hash, spec_hash: workflow.spec_hash, code_sha256: attempt.code_sha256, delivery_hash: attempt.delivery_hash }} /><h5>本次实现尚存问题</h5><Issues value={attempt.issues} /><details><summary>交付文件</summary><p>实现文档：<ReferenceText value={workflow.document?.title || 'implementation.md'} /></p>{rows(snapshot.manifest ?? attempt.manifest).map((file, index) => <p key={index}><ReferenceText value={file.path || '未命名交付文件'} /></p>)}</details></article>;
}

export function LocalCodexPanel({ workflow, runId }: { workflow?: Json | null; runId: string }) {
  const current = workflow?.current && typeof workflow.current === 'object' ? workflow.current : null;
  const matching = current && current.run_id === runId;
  const handoff = workflow?.document_handoff;
  const prepared = handoff?.run_id === runId && handoff.status === 'prepared';
  return <ReferenceNames records={workflow}><section className="local-codex-panel" aria-label="代码实现与验收"><h3>代码实现与验收</h3><CurrentImplementationDocumentPanel runId={runId} localCodex={workflow} /><p>编写实现 Markdown → 三方索引全文审核 → 本机 Codex 实现 → 交付验收。</p><p>新实现交接直接提示“按照 implementation.md 实现代码”。完整 Codex 负责读文档、写文件、测试和修复；agent 只核查是否完成、是否符合要求。</p><p>实现规范评审、本机写码、本地验收和三方代码评审分别保留记录。生成方的完成声明不作为验收通过的依据。</p>{prepared && <p className="inline-info">已安排后续交接：架构文档评议通过后，先编写实现 Markdown，再交给本机 Codex；尚未派发写码。</p>}<p>{prepared ? '后续实现方式：本机 Codex（架构评议通过后启用）' : <>本任务实现方式：{'本机 Codex'}</>}</p>{workflow?.project_dir && <RecordField label="本任务配置的项目目录" value={workflow.project_dir} />}{current && !matching ? <p>返回的实现记录不属于当前研究，未展示其内容。请刷新核对。</p> : matching ? <><header><h4>当前实现流程</h4><CodexState value={current.status} /></header><div className="provenance-grid"><RecordField label="研究轮次" value={current.cycle} /><RecordField label="实现类型" value={current.kind === 'runtime_repair' ? '实验运行后修复' : current.kind === 'implementation' ? '首次实现' : '未记录'} /><RecordField label="固定项目目录" value={current.project_root} /><RecordField label="实际写码目录" value={current.workspace} /><RecordField label="指定模型" value={current.model || '沿用本机默认'} /><RecordField label="指定推理等级" value={current.reasoning_effort || '沿用本机默认'} /></div>{workflow?.configuration_locked === true && <p>实现规范已启动，项目目录、模型、推理等级与实现方式已锁定。</p>}{current.reason && <HumanMessage text={textOf(current.reason)} />}<section className="codex-specifications"><h4>明确实现规范与三方评审</h4>{rows(current.specifications).length ? rows(current.specifications).map((item, index) => <Specification key={index} record={item} runId={runId} />) : <p>尚无已保存的实现规范，不能推断三方已经同意。</p>}</section>{rows(current.attempts).length ? rows(current.attempts).map((attempt, index) => <CodexAttempt key={attempt.operation_id ?? index} attempt={attempt} workflow={current} />) : <p>尚未记录本机 Codex 实现请求、产物或本地验收。</p>}</> : <p><ReferenceText value={prepared ? '尚无已生成的实现文档或本机 Codex 产物；旧请求记录已保留。' : '尚无本机 Codex 实现记录。真实任务进入代码实现阶段后，这里展示实际工作目录、公开活动、产物清单和验收证据。'} /></p>}{workflow?.cost_note && <HumanMessage text={textOf(workflow.cost_note)} />}</section></ReferenceNames>;
}
