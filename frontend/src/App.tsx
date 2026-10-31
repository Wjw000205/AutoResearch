import { OptimizationProgress } from './OptimizationProgress';
import { ReferenceText, ReferenceNames } from './ReferenceNames';
import { validationResumeReason, validationStageLabel } from './ValidationHold';
import { SubscriptionProvider } from './SubscriptionProviders';
import { LiteratureVenue, LiteratureVenueControl } from './LiteratureVenue';
import { lazy, Suspense, useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import {
  Activity, ArrowDownToLine, ArrowLeft, ArrowRight, ArrowUpRight, Beaker, BookOpen,
  BrainCircuit, Check, CheckCheck, ChevronDown, ChevronRight, CircleAlert, CircleCheck,
  CircleHelp, Clock3, Code2, Coins, Compass, Cpu, Database, ExternalLink, FileText, FlaskConical,
  FolderOpen, Gauge, GitBranch, HardDrive, Layers3, LayoutDashboard, LibraryBig, LoaderCircle,
  Menu, MessageSquareText, Network, Pause, Play, Plus, RefreshCw, Search, Settings2,
  ShieldCheck, SkipForward, Square, Trash2, Upload, Workflow, Wrench, X, Zap,
  type LucideIcon,
} from 'lucide-react';
import { api, post, number, money, compact, date, textOf, safeUrl, type Detail, type Json, type Overview, type Provider, type Run, type RunListItem, type Settings, type RunReport, type ResearchMap } from './api';
import { ConsensusView, EvidencePanel, HandoffBoard, RecoveryNotice } from './ResearchViews';
import { ComputeControls } from './ComputeControls';
import { ArchitectureStageEntry, StageDetailBody, StageDetails, stageSummary } from './StageDetails';
import { definitionResumeReason } from './ArchitectureRecovery';
import { StageConnections } from './StageConnections';
import { InnovationView } from './InnovationView';
import { ViewBoundary } from './ViewBoundary';
import { CitationDiscardNotice } from './CitationDiscardNotice';
import { isHistoricalReadingGate, RequestRecoveryDiagnostic } from './RequestRecoveryDiagnostic';
import { ExperimentView } from './ExperimentView';
import { BudgetControl, CallBudgetControl, ContextBudgetControl, CONTEXT_TOKEN_MAX, DiscussionControls, DiscussionProgress, ProviderTimeoutControl, ProviderStreamingControl, ResearchCycleControls, ImplementationControls, budgetLabel } from './ExecutionControls';
import { OrchestrationView } from './OrchestrationView';
import { MemoryIndexPanel, memoryIndexPath, type MemoryIndex } from './MemoryIndexPanel';
import { MemoryRecord } from './MemoryRecord';
import { MemoryExplorer, MemoryStudyEntry } from './MemoryExplorer';
import { readResearchTab } from './MemoryExplorerHelpers';
import { billingPagePath, memoryPagePath, RecordPagination } from './RecordPagination';
import { PaperRagPage, PaperRagPanel } from './PaperRagPanel';
import { ResearchCommandCenter } from './ResearchCommandCenter';
import { ToolsPage } from './ToolsPage';
import { ToolCallsPanel } from './ToolCallsPanel';
import { TimelineToolSummary } from './ToolEventSummary';
import './tool-event-summary.css';
import { ResearchObjective } from './ResearchObjective';
import { LocalCodexConnection, LocalCodexPanel, type LocalCodexStatus } from './LocalCodexPanel';
import { CodexCostNotice, LocalCodexBilling, codexCount } from './LocalCodexUsage';
import { ModelPricingEditor, totalModelCost } from './ModelPricing';
import { ImplementationProgress, ImplementationReviewSummary, implementationReviewSummary } from './ImplementationProgress';
import { TaskCallBudget, TaskCallBudgetNote } from './TaskCallBudget';
import { ConsensusStatus } from './ConsensusStatus';
import { ReasoningRequests } from './ReasoningRequests';
import { useRemoteResource, useProjectionSnapshot } from './useRemoteResource';
import { mergeRunProjection, runViewPath, projectionRevision, workspaceRequestPaths, type RunProjection } from './RunProjections';
import { HumanMessage, humanizeMessage } from './localization';
import { RequestRetryPanel } from './RequestRetryPanel';
import { createRunActionController, initialRunActionState, type RunAction } from './RunAction';
import { createDurableRunRequest } from './DurableRunRequest';
import { mergeReference, referenceRagLabel, REFERENCE_LIMIT, uploadReferenceBatch, type ReferenceUploadProgress } from './ReferenceUploads';
import './reference-uploads.css';
import { RunActionNotice } from './RunActionNotice';
import { currentRunReport, ReportKindNotice, BillingRequestStatus, BillingRateDetails, isUnknownCharge, billingTokenLabel, billingRunLabel, billingRunOptions } from './DeliveryViews';
import { HistoricalReportPreview, historicalPreviewKey } from './HistoricalReportPreview';
import { DeleteResearchDialog, deletionNavigationTarget } from './DeleteResearchDialog';
import { useDialogKeyboard } from './useDialogKeyboard';
import { optimizationDisplayStage } from './OptimizationDisplayStage';
import { collectModerationReviews, hasPendingModerationReview, MODERATION_RECOVERY_REASON, ModerationReviewPanel, ResumeResearchButton } from './ModerationReviews';
const MarkdownReport = lazy(() => import('./MarkdownReport'));

const STAGES = [
  ['intake', '需求与资源', Cpu], ['literature', '文献发现', BookOpen], ['innovation', '创新点讨论', BrainCircuit], ['related_work', '相关论文分析', BookOpen], ['debate', '想法共识', MessageSquareText],
  ['design', '架构设计', GitBranch], ['implementation', '代码实现', Code2], ['experiments', '实验验证', FlaskConical],
  ['architecture_tuning', '架构评估与微调', GitBranch], ['tuning', '参数微调', Gauge], ['validation_review', '验证评审', MessageSquareText], ['revision', '方案修订', RefreshCw], ['final_tests', '独立测试', FlaskConical], ['report', '结果报告', FileText],
] as const;
const STATUS: Record<string, string> = { draft: '待启动', running: '运行中', paused: '已暂停', blocked: '等待处理', completed: '已完成', cancelled: '已取消', failed: '执行失败', pending: '待开始', interrupted: '已中断', succeeded: '成功', approved: '通过', rejected: '未通过' };

export function ResearchStageTrack({ run, steps, researchMap, data }: { run: Pick<Run, 'stage' | 'status'> & { config?: Json; state?: Json; validation_hold?: Json }; steps: Json[]; researchMap?: ResearchMap; data?: Detail }) {
  const [selected, setSelected] = useState<string | null>(null);
  const track = useRef<HTMLDivElement>(null);
  const [previewLayout, setPreviewLayout] = useState<{ key: string; above: boolean; height: number } | null>(null);
  const placePreview = (key: string, item: HTMLElement) => {
    const rect = item.getBoundingClientRect();
    const desired = Math.min(item.querySelector<HTMLElement>('.stage-tooltip')?.scrollHeight || 420, 420);
    const below = window.innerHeight - rect.bottom - 24, above = rect.top - 24;
    const useAbove = below < desired && above > below;
    setPreviewLayout({ key, above: useAbove, height: Math.max(100, Math.min(420, useAbove ? above : below)) });
  };
  const adapting = Boolean(run.state?.dataset_adaptation);
  const configuring = run.state?.dataset_configuration_protocol === 1;
  const displayStage = optimizationDisplayStage(run.stage, researchMap?.optimization_loop);
  const connectionRevision = `${run.config?.literature_protocol}:${displayStage}:${run.status}:${adapting}:${configuring}:${data?.architecture_review?.current?.status}:${steps.map(step => `${step.stage}:${step.status}`).join(',')}`;
  return <><div ref={track} className="stage-track" aria-label="研究执行阶段"><StageConnections track={track} revision={connectionRevision} />{STAGES.filter(([key]) => key !== 'related_work' || run.config?.literature_protocol === 2).map(([key, label, Icon]) => {
    const step = steps.find(item => item.stage === key);
    const node = researchMap?.mainline?.find(item => item.id === key);
    const skipped = step?.status === 'skipped' || node?.status === 'skipped';
    const architectureState = data?.architecture_review?.current?.status;
    const documentReview = key === 'design' && step?.status === 'completed' && data?.architecture_review?.required && architectureState !== 'approved';
    const holdLabel = validationStageLabel(run, key);
    // Dataset adaptation reuses completed upstream work without writing new
    // stage records. A configuration queue can also move new-data work into
    // tuning while retaining an interrupted experiments-stage checkpoint.
    const experimentsReused = adapting && configuring && key === 'experiments'
      && ['tuning', 'architecture_tuning', 'validation_review', 'final_tests', 'report'].includes(run.stage)
      && (!step || step.status === 'interrupted');
    const reuseLabel = adapting && key === 'debate' ? '复用原共识'
      : adapting && configuring && key === 'design' ? '复用原架构'
        : adapting && configuring && key === 'implementation' ? '复用模型实现'
          : experimentsReused ? '复用原实验阶段' : undefined;
    const reuseCompleted = Boolean(reuseLabel) && (!step || ['completed', 'interrupted'].includes(step.status));
    const diagnosticRepair = key === 'tuning' && (step?.activity ?? step?.output?.activity) === 'diagnostic_repair';
    const tuningPending = key === 'tuning' && (diagnosticRepair || step?.tuning_pending || step?.output?.tuning_pending || step?.semantic_completion === false || step?.semantic_completion === 0);
    const recoveredArchitecture = displayStage !== run.stage && ['architecture_tuning', 'tuning'].includes(key);
    const done = !recoveredArchitecture && !tuningPending && !holdLabel && !skipped && (step?.status === 'completed' || reuseCompleted) && !documentReview;
    const definitions = data?.architecture_review?.revision?.definition_review;
    const documentLabel = definitions && definitions.status !== 'approved'
      ? run.status === 'blocked' || run.status === 'paused' || ['blocked', 'rejected'].includes(definitions.status) ? '定义等待处理'
        : definitions.status === 'drafting' ? run.status === 'running' ? '编写具体定义' : '待补充具体定义'
          : definitions.status === 'reviewing' ? '具体定义评议中' : '待补充具体定义'
      : architectureState === 'rejected' ? '文档待修改' : run.status === 'blocked' || run.status === 'paused' ? '评议等待处理' : architectureState === 'reviewing' ? '文档评议中' : '待文档评议';
    const current = !skipped && displayStage === key && run.status !== 'completed';
    const architectureLabel = key === 'architecture_tuning' && done
      ? ['parameters', 'stable'].includes(researchMap?.optimization_loop?.status) ? '架构评审通过'
        : researchMap?.optimization_loop?.status === 'ready_for_review' ? '具备评审条件'
        : researchMap?.optimization_loop?.status === 'adjustment_rejected' ? '调整未通过 · 待重评' : '本轮调整完成 · 待验证'
      : undefined;
    const explanation = skipped ? node?.reason || step?.output?.reason || (key === 'final_tests' ? '未执行独立测试' : '此阶段未执行') : undefined;
    const summary = explanation ?? (experimentsReused ? '原实验阶段已完成并复用；本轮数据配置、接口检查和成对验证在参数微调中继续，尚不代表新数据验证通过。' : reuseCompleted ? `${reuseLabel}，本阶段已完成。原始记录保留在详情中。` : configuring && key === 'design' ? '本轮沿用原架构；数据配置、接口检查和成对验证在实验阶段完成。' : adapting && key === 'design' ? '本轮复用已有架构，适配真实数据、数据划分和实验协议；原架构文档与评议保留在详情中。' : stageSummary(key, step, data));
    const preview = previewLayout?.key === key ? previewLayout : null;
    return <div key={key} data-stage={key} data-preview-side={preview?.above ? 'above' : 'below'} className={`stage-item ${done ? 'done' : ''} ${current ? 'current' : ''} ${skipped ? 'skipped' : ''}`} title={explanation} onMouseEnter={event => placePreview(key, event.currentTarget)} onFocus={event => placePreview(key, event.currentTarget)}><button type="button" className="stage-trigger" aria-label={`查看${label}详情`} aria-describedby={`stage-summary-${key}`} aria-haspopup="dialog" onClick={() => setSelected(key)}><span className="stage-circle">{skipped ? <SkipForward size={16} /> : done ? <Check size={16} /> : <Icon size={16} />}</span><strong>{label}</strong>{adapting && !configuring && key === 'design' && <small>本轮：数据适配</small>}{configuring && key === 'experiments' && !experimentsReused && <small>本轮：数据配置与验证</small>}<small>{holdLabel ?? (diagnosticRepair ? run.status === 'running' ? '诊断补修中' : '诊断补修待继续' : skipped ? key === 'final_tests' ? '已跳过 · 未执行' : '已跳过' : step?.status === 'rejected' ? '未通过 · 重新思考' : documentReview ? documentLabel : done ? architectureLabel ?? '已完成' : current ? STATUS[run.status] ?? '当前阶段' : '待开始')}</small>{reuseCompleted && done && <small>{reuseLabel}</small>}</button><div id={`stage-summary-${key}`} className="stage-tooltip" role="tooltip" style={preview ? { maxHeight: preview.height } : undefined}><strong>{label}</strong><p><ReferenceText value={summary} /></p><small>点击查看论文、分析及阶段记录</small></div></div>;
  })}</div><section className="optimization-loop-note" aria-label="验证与优化循环"><OptimizationProgress value={researchMap?.optimization_loop} /><p>实验验证 → 架构评估与调整 ↺ 验证 → 参数微调 → 稳定性复核</p><p>{researchMap?.optimization_loop?.completion_rule ?? '当前数据集架构与参数均经三方核验稳定后才切换；首数据集模型源码冻结复用。'}</p></section>{selected && <StageDetails stage={selected} label={STAGES.find(([key]) => key === selected)?.[1] ?? selected} step={steps.find(item => item.stage === selected)} data={data} run={(data?.run ?? run) as Run} close={() => setSelected(null)} />}</>;
}
const ROLE: Record<string, { name: string; purpose: string; short: string; icon: LucideIcon; className: string }> = {
  director: { name: '研究总监', purpose: '方向收敛 · 架构审议 · 结果评价', short: '统筹', icon: BrainCircuit, className: 'director' },
  engineer: { name: '实验工程师', purpose: '代码实现 · 实验验证 · 参数优化', short: '实验', icon: Code2, className: 'engineer' },
  scout: { name: '文献研究员', purpose: '在线检索 · 证据核验 · 创新质询', short: '文献', icon: BookOpen, className: 'scout' },
};
type Page = 'overview' | 'runs' | 'literature' | 'rag' | 'memory' | 'tools' | 'billing' | 'hardware' | 'settings';
const NAV: { id: Page; title: string; icon: LucideIcon; label?: string }[] = [
  { id: 'overview', title: '工作空间', icon: LayoutDashboard }, { id: 'runs', title: '研究任务', icon: FlaskConical },
  { id: 'literature', title: '文献与证据', icon: BookOpen }, { id: 'rag', title: '论文 RAG', icon: LibraryBig }, { id: 'memory', title: '持久化记忆', icon: Database },
  { id: 'tools', title: '工具目录', icon: Wrench },
  { id: 'billing', title: '用量与计费', icon: Coins }, { id: 'hardware', title: '计算资源', icon: Cpu },
  { id: 'settings', title: '智能体与设置', icon: Settings2 },
];

function readRoute(): { page: Page; runId: string | null } {
  const [page, id] = window.location.hash.split('?')[0].replace(/^#\/?/, '').split('/');
  return { page: NAV.some(item => item.id === page) ? page as Page : 'overview', runId: page === 'runs' && /^[a-f0-9]{16,64}$/i.test(id ?? '') ? id : null };
}

const useData = useRemoteResource;

function Status({ value }: { value: string }) { return <span className={`status status-${value}`}><i />{STATUS[value] ?? value}</span>; }
function Mode({ mode }: { mode: string }) { return <span className={`mode mode-${mode}`}>{mode === 'demo' ? '演示 · 模拟' : '真实调用'}</span>; }
function IconBox({ icon: Icon, className = '' }: { icon: LucideIcon; className?: string }) { return <span className={`icon-box ${className}`}><Icon size={19} /></span>; }
function Empty({ icon: Icon = FlaskConical, title, description, action }: { icon?: LucideIcon; title: string; description: string; action?: ReactNode }) {
  return <div className="empty-state"><div className="empty-illustration"><span /><Icon size={30} strokeWidth={1.3} /><span /></div><h3><ReferenceText value={title} /></h3><p><ReferenceText value={description} /></p>{action}</div>;
}
function Loading() { return <div className="loading"><LoaderCircle size={20} className="spin" />正在读取工作空间</div>; }
function ErrorBox({ message }: { message: string }) { return message ? <div className="error-box" role="alert"><CircleAlert size={18} /><HumanMessage text={message} /></div> : null; }
export function WorkspaceReadStatus({ loading, error, retry }: { loading: boolean; error?: string; retry: () => void }) {
  return <div className="empty-state">{loading ? <><LoaderCircle size={24} className="spin" /><h3 role="status">正在读取工作空间</h3><p>正在获取最新状态，读取超时后会显示具体提示。</p></> : <ErrorBox message={error || '工作空间尚未读取完成，请重试。'} />}<button type="button" className="btn" onClick={retry}><RefreshCw size={15} />重新读取</button><p>重新读取页面不会重新启动研究。</p></div>;
}
function SectionTitle({ eyebrow, title, action }: { eyebrow?: string; title: string; action?: ReactNode }) { return <div className="section-title"><div>{eyebrow && <div className="eyebrow">{eyebrow}</div>}<h2><ReferenceText value={title} /></h2></div>{action}</div>; }
export function Progress({ value, className = '' }: { value: number; className?: string }) { const percent = Math.max(0, Math.min(100, number(value))); return <div className={`progress ${className}`}><span style={{ width: `${percent}%` }} /></div>; }
function KV({ label, children }: { label: string; children: ReactNode }) { return <div className="kv"><span>{label}</span><strong>{children}</strong></div>; }
function Field({ label, help, children }: { label: string; help?: string; children: ReactNode }) { return <label className="field"><span>{label}</span>{children}{help && <small>{help}</small>}</label>; }
function Pill({ icon: Icon, children }: { icon?: LucideIcon; children: ReactNode }) { return <span className="pill">{Icon && <Icon size={13} />}{children}</span>; }


export default function App() {
  const [page, setPage] = useState<Page>(() => readRoute().page);
  const [runId, setRunId] = useState<string | null>(() => readRoute().runId);
  const [revision, setRevision] = useState(0);
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Pick<Run, 'id' | 'title' | 'status'> | null>(null);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [notice, setNotice] = useState('');
  const pagePaths = workspaceRequestPaths(page, runId);
  const overview = useData<Overview>(pagePaths.overview, revision, 15000);
  const runList = useData<RunListItem[]>(pagePaths.runs, revision, page === 'runs' ? 15000 : 0);
  const hardware = useData<Json>(pagePaths.hardware, revision);
  const pageResource = pagePaths.overview ? overview : pagePaths.runs ? runList : pagePaths.hardware ? hardware : null;
  const notify = useCallback((message: string) => { setNotice(message); }, []);
  const refresh = useCallback(() => setRevision(value => value + 1), []);
  useEffect(() => {
    const read = () => { const route = readRoute(); setPage(route.page); setRunId(route.runId); };
    window.addEventListener('popstate', read); window.addEventListener('hashchange', read);
    return () => { window.removeEventListener('popstate', read); window.removeEventListener('hashchange', read); };
  }, []);
  useEffect(() => {
    function shortcut(event: KeyboardEvent) {
      const element = event.target as HTMLElement;
      if (event.key.toLowerCase() === 'n' && !event.ctrlKey && !event.metaKey && !event.altKey && !element.isContentEditable && !['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName) && !element.closest('[role="dialog"]')) { event.preventDefault(); setCreateOpen(true); }
    }
    window.addEventListener('keydown', shortcut);
    return () => window.removeEventListener('keydown', shortcut);
  }, []);
  useEffect(() => { if (!notice) return; const timer = window.setTimeout(() => setNotice(''), 5500); return () => window.clearTimeout(timer); }, [notice]);
  const navigate = (next: Page) => { window.history.pushState(null, '', `#/${next}`); setPage(next); setRunId(null); setMobileOpen(false); window.scrollTo(0, 0); };
  const openRun = (id: string) => { window.history.pushState(null, '', `#/runs/${id}`); setRunId(id); setPage('runs'); window.scrollTo(0, 0); };
  const running = overview.data?.run_counts?.running ?? (runList.data ?? overview.data?.runs)?.filter(run => run.status === 'running').length ?? 0;
  return <div className="app-shell">
    {mobileOpen && <button className="sidebar-scrim" aria-label="关闭导航" onClick={() => setMobileOpen(false)} />}
    <aside className={`sidebar ${mobileOpen ? 'is-open' : ''}`}>
      <button className="brand" onClick={() => navigate('overview')} aria-label="AutoResearch 首页"><span className="brand-symbol"><Network size={25} strokeWidth={1.65} /></span><span>AutoResearch<small>多智能体协同科研</small></span></button>
      <div className="workspace-switch"><span className="workspace-avatar">A</span><div>本地研究工作空间<small>本机研究空间</small></div><ChevronDown size={14} /></div>
      <div className="nav-label">工作空间</div>
      <nav aria-label="主导航">{NAV.map(item => <div key={item.id}>{item.id === 'billing' && <div className="nav-label system-label">系统与资源</div>}<button className={`nav-link ${page === item.id ? 'active' : ''}`} onClick={() => navigate(item.id)}><item.icon size={18} strokeWidth={1.7} /><span><ReferenceText value={item.title} /></span>{item.id === 'runs' && running > 0 && <b>{running}</b>}{page === item.id && <i />}</button></div>)}</nav>
      <div className="sidebar-bottom"><div className="harness-label"><span className={pageResource?.error ? 'connection-dot offline' : 'connection-dot'} /><span>{pageResource?.error ? '后端连接中断' : runId ? '研究状态见当前页面' : pageResource?.data ? '统一执行框架已连接' : pageResource?.loading ? '正在连接执行框架' : '本地研究工作空间'}</span><Activity size={14} /></div><p>每一步，都有证据可循。</p><div className="sidebar-footer"><span className="version">v1.0 <span>本地</span></span><button aria-label="查看系统设置" title="查看系统设置" onClick={() => navigate('settings')}><Settings2 size={15} /></button></div></div>
    </aside>
    <div className="main-shell">
      <header className="topbar"><div className="breadcrumbs"><button className="mobile-menu" aria-label="打开导航" onClick={() => setMobileOpen(true)}><Menu size={20} /></button><span>工作空间</span><ChevronRight size={13} /><strong><ReferenceText value={runId ? '研究详情' : NAV.find(item => item.id === page)?.title} /></strong></div><div className="topbar-right"><span className="local-label"><span />本地运行</span><span className="topbar-divider" /><span className="date-label">{new Date().toLocaleDateString('zh-CN', { month: 'long', day: 'numeric' })}</span><button className="avatar" aria-label="查看工作空间设置" onClick={() => navigate('settings')}>AR</button></div></header>
      <main>
        {pageResource?.error && <ErrorBox message={`无法连接后端：${pageResource.error}。请确认服务已在 127.0.0.1:8000 启动。`} />}
        {runId ? <RunDetail key={runId} id={runId} revision={revision} refresh={refresh} notify={notify} back={() => navigate('runs')} onDelete={setDeleteTarget} /> : <>
          {page === 'overview' && (overview.data ? <Dashboard data={overview.data} create={() => setCreateOpen(true)} openRun={openRun} navigate={navigate} onDelete={setDeleteTarget} /> : <WorkspaceReadStatus loading={overview.loading} error={overview.error} retry={refresh} />)}
          {page === 'runs' && (runList.data ? <RunsPage runs={runList.data} create={() => setCreateOpen(true)} openRun={openRun} onDelete={setDeleteTarget} /> : <WorkspaceReadStatus loading={runList.loading} error={runList.error} retry={refresh} />)}
          {page === 'literature' && <LiteraturePage notify={notify} />}
          {page === 'rag' && <PaperRagPage revision={revision} />}
          {page === 'memory' && <MemoryPage runs={runList.data ?? []} notify={notify} />}
          {page === 'tools' && <ToolsPage revision={revision} />}
          {page === 'billing' && <BillingPage revision={revision} runs={runList.data ?? []} runsLoaded={!!runList.data} />}
          {page === 'hardware' && (hardware.data ? <HardwarePage hardware={hardware.data} refresh={refresh} /> : <WorkspaceReadStatus loading={hardware.loading} error={hardware.error} retry={refresh} />)}
          {page === 'settings' && <SettingsPage revision={revision} refresh={refresh} notify={notify} />}
        </>}
        <footer className="content-footer"><span><GitBranch size={13} />追溯想法、决策与每一次实验</span><span>AutoResearch <span className="footer-dot">·</span> 本地科研协作平台</span></footer>
      </main>
    </div>
    {createOpen && <CreateRun close={() => setCreateOpen(false)} onCreated={run => { setCreateOpen(false); refresh(); openRun(run.id); notify('研究任务已创建，可检查配置后启动。'); }} />}
    {deleteTarget && <DeleteResearchDialog key={deleteTarget.id} run={deleteTarget} close={() => setDeleteTarget(null)} onDeleted={result => { setDeleteTarget(null); if (deletionNavigationTarget(runId, result.run_id)) navigate('runs'); refresh(); notify('研究已删除，已有计费记录已保留。'); }} />}
    {notice && <div className="toast" role="status"><CircleCheck size={18} /><span>{notice}</span><button aria-label="关闭通知" onClick={() => setNotice('')}><X size={15} /></button></div>}
  </div>;
}

function Dashboard({ data, create, openRun, navigate, onDelete }: { data: Overview; create: () => void; openRun: (id: string) => void; navigate: (page: Page) => void; onDelete: (run: RunListItem) => void }) {
  const running = data.runs.filter(run => run.status === 'running');
  const completed = data.runs.filter(run => run.status === 'completed');
  const hardware = data.hardware ?? {};
  const gpu = Array.isArray(hardware.gpus) ? hardware.gpus[0] : undefined;
  return <>
    <div className="page-heading dashboard-heading"><div><div className="eyebrow"><span className="tiny-line" />让研究持续推进</div><h1>让想法，走向可验证的发现<span>。</span></h1><p>三个智能体协作，一条完整研究路径。你定义方向，系统推进每一步。</p></div><button className="btn btn-primary" onClick={create}><Plus size={17} />新建研究<span className="shortcut">N</span></button></div>
    <div className="stats-ribbon">
      {[{ label: '进行中的研究', value: String(data.run_counts?.running ?? running.length).padStart(2, '0'), note: `${data.run_counts?.total ?? data.runs.length} 项研究任务`, icon: Workflow }, { label: '已完成研究', value: String(data.run_counts?.completed ?? completed.length).padStart(2, '0'), note: '实验与结论可追溯', icon: CheckCheck }, { label: '模型费用估算', value: totalModelCost(data.totals ?? {}), note: `${number(data.totals?.requests)} 次 API 请求 · 含 Codex 已计价估算`, icon: Coins }, { label: '持久化记忆', value: compact(data.memory_count), note: '跨阶段知识检索与复用', icon: Database }].map(item => <div className="stat" key={item.label}><div className="stat-label">{item.label}<item.icon size={16} /></div><div className="stat-value">{item.value}</div><div className="stat-note">{item.note}</div></div>)}
    </div>
    <CodexCostNotice />
    <div className="dashboard-columns"><div className="dashboard-primary">
      <section className="panel research-panel"><SectionTitle title="研究任务" eyebrow="研究流程" action={<button className="text-btn" onClick={() => navigate('runs')}>全部任务<ArrowUpRight size={15} /></button>} />
        {data.runs.length ? <div className="run-list">{[...data.runs].sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, 4).map(run => <RunRow key={run.id} run={run} onClick={() => openRun(run.id)} onDelete={() => onDelete(run)} />)}</div> : <Empty title="你的下一项发现，从这里开始" description="输入研究方向或上传参考文献。智能体将结合主机资源检索证据、讨论方案，并在达成共识后执行实验。" action={<button className="btn btn-secondary" onClick={create}><Plus size={16} />创建第一项研究</button>} />}
        <div className="pipeline-strip">{STAGES.map(([id, label], index) => <div key={id}><span>{String(index + 1).padStart(2, '0')}</span><small>{label}</small>{index < STAGES.length - 1 && <ChevronRight size={12} />}</div>)}</div>
      </section>
      <section className="team-section"><SectionTitle title="分工明确，协同研究" eyebrow="研究团队" action={<button className="text-btn" onClick={() => navigate('settings')}>配置智能体<ArrowUpRight size={15} /></button>} /><div className="agent-roster">{Object.entries(ROLE).map(([role, info]) => {
        const provider = data.providers.find(item => item.id === data.roles?.[role]);
        return <button className={`agent-cell ${info.className}`} key={role} onClick={() => navigate('settings')}><div className="agent-top"><span className={`agent-avatar ${info.className}`}><info.icon size={21} strokeWidth={1.5} /></span><span className={`agent-connected ${provider?.configured ? 'configured' : ''}`}>{provider?.configured ? '已配置' : '待配置'}</span></div><h3><ReferenceText value={info.name} /><ArrowUpRight size={14} /></h3><div className="agent-model">{provider?.model ?? '选择模型'}</div><p><ReferenceText value={info.purpose} /></p><div className="agent-provider"><ReferenceText value={provider?.name ?? '尚未绑定供应商'} /><span>{info.short}</span></div></button>;
      })}</div></section>
    </div><aside className="dashboard-secondary">
      <section className="protocol-panel"><div className="protocol-heading"><ShieldCheck size={20} /><span>研究执行协议</span><Pill>已启用</Pill></div><h3>可信的过程，<br />才有可信的结果。</h3><div className="protocol-rule"><span>01</span><div><strong>先共识，再推进</strong><p>多轮独立评议，意见一致后进入实验。</p></div></div><div className="protocol-rule"><span>02</span><div><strong>每条文献，都有出处</strong><p>在线发现、权威源核验，保留证据链。</p></div></div><div className="protocol-rule"><span>03</span><div><strong>为当前主机设计</strong><p>硬件门禁与每轮执行边界约束实验规模。</p></div></div><div className="protocol-bottom"><Layers3 size={14} />有界重试 · 状态持久化 · 中断恢复</div></section>
      <section className="resource-snapshot"><div className="small-section-title"><h3>本机计算资源</h3><button aria-label="查看计算资源" onClick={() => navigate('hardware')}><ArrowUpRight size={16} /></button></div><div className="hardware-name"><Cpu size={25} strokeWidth={1.3} /><div><strong><ReferenceText value={gpu?.name ?? '处理器实验环境'} /></strong><small>{number(hardware.cpu_cores)} 核心 · {number(hardware.ram_total_gb).toFixed(1)} GB 内存</small></div></div><div className="resource-meter"><span>可用内存</span><strong>{number(hardware.ram_available_gb).toFixed(1)} / {number(hardware.ram_total_gb).toFixed(1)} GB</strong></div><Progress value={number(hardware.ram_available_gb) / Math.max(1, number(hardware.ram_total_gb)) * 100} className="teal" /><p><CircleCheck size={13} />实验前自动评估资源需求</p></section>
      <div className="knowledge-link"><span className="knowledge-icon"><BookOpen size={20} /></span><div><h4>从可靠的证据出发</h4><p>搜索与核验你的研究文献</p></div><button aria-label="前往文献与证据" onClick={() => navigate('literature')}><ArrowRight size={18} /></button></div>
    </aside></div>
  </>;
}

export function RunRow({ run, onClick, onDelete }: { run: RunListItem; onClick: () => void; onDelete: () => void }) {
  return <div className="run-row-shell"><button type="button" className="run-row" onClick={onClick}><span className={`run-icon ${run.status === 'running' ? 'live' : ''}`}><FlaskConical size={21} strokeWidth={1.5} /></span><div className="run-row-main"><div className="run-row-title"><h3><ReferenceText value={run.title} /></h3><Mode mode={run.mode} /></div><p><ReferenceText value={run.objective_preview ?? run.objective} /></p><div className="run-row-meta"><span>{date(run.created_at)}</span><i /><span>{STAGES.find(stage => stage[0] === run.stage)?.[1] ?? run.stage}</span></div></div><div className="run-row-side"><Status value={run.status} /><Progress value={number(run.progress)} /></div><ChevronRight size={17} className="row-chevron" /></button><button type="button" className="run-delete-button" title="删除研究" aria-label={`删除研究“${run.title}”`} onClick={onDelete}><Trash2 size={16} /></button></div>;
}

function RunsPage({ runs, create, openRun, onDelete }: { runs: RunListItem[]; create: () => void; openRun: (id: string) => void; onDelete: (run: RunListItem) => void }) {
  const [filter, setFilter] = useState('all');
  const [query, setQuery] = useState('');
  const filtered = runs.filter(run => (filter === 'all' || run.status === filter) && `${run.title} ${run.objective_preview ?? run.objective ?? ''}`.toLowerCase().includes(query.toLowerCase()));
  return <><div className="page-heading"><div><div className="eyebrow">研究任务</div><h1>每一项研究，都有来路</h1><p>从开放的想法到可复现的实验，查看任务、状态与执行记录。</p></div><button className="btn btn-primary" onClick={create}><Plus size={17} />新建研究</button></div><section className="panel"><div className="list-toolbar"><div className="tabs">{[['all', '全部'], ['running', '运行中'], ['paused', '已暂停'], ['blocked', '待处理'], ['completed', '已完成']].map(([value, label]) => <button key={value} className={filter === value ? 'selected' : ''} onClick={() => setFilter(value)}>{label}<span>{runs.filter(run => value === 'all' || run.status === value).length}</span></button>)}</div><div className="search-input"><Search size={16} /><input aria-label="搜索研究任务" placeholder="搜索研究任务" value={query} onChange={event => setQuery(event.target.value)} /></div></div>{filtered.length ? <div className="run-list">{filtered.map(run => <RunRow key={run.id} run={run} onClick={() => openRun(run.id)} onDelete={() => onDelete(run)} />)}</div> : <Empty title={runs.length ? '没有匹配的研究' : '还没有研究任务'} description={runs.length ? '尝试其他关键词或任务状态。' : '自由输入研究方向，或从你正在阅读的一篇论文开始。'} action={!runs.length && <button className="btn btn-secondary" onClick={create}><Plus size={15} />创建研究任务</button>} />}</section></>;
}

function CreateRun({ close, onCreated }: { close: () => void; onCreated: (run: Run) => void }) {
  const { data: settings } = useData<Settings>('/settings', 0);
  const [form, setForm] = useState<Json>({ title: '', objective: '', publication_target: '', ccf_ab_only: false, mode: 'live', budget_usd: null, max_calls: 150, max_rounds: 3, discussion_mode: 'until_stalled', stagnation_rounds: 3, max_trials: 12, max_research_cycles: null, min_relative_improvement: 0.01, context_tokens: CONTEXT_TOKEN_MAX, experiment_timeout: 120, allow_local_execution: false, execution_mode: 'local', python_executable: '', gpu_index: null, vram_limit_gb: null, cpu_limit_cores: null, ram_limit_gb: null, disk_limit_gb: null, implementation_backend: 'local_codex', codex_timeout_seconds: 1800, codex_max_revisions: 3, implementation_review_strategy: 'acceptance_contract', implementation_indexed_review_protocol: 1, implementation_review_max_revisions: 2, implementation_review_max_seconds: 14400, implementation_review_time_basis: 'policy_active_time', implementation_review_max_calls: 240, codex_model: '', codex_reasoning_effort: '', codex_project_dir: 'F:\\Python program\\AutoResearch' });
  const [references, setReferences] = useState<Json[]>([]);
  const [referenceUrl, setReferenceUrl] = useState('');
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<ReferenceUploadProgress | null>(null);
  const [uploadMessage, setUploadMessage] = useState('');
  const [advanced, setAdvanced] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const referenceOperationRef = useRef(false);
  const objectiveRef = useRef<HTMLTextAreaElement>(null);
  useDialogKeyboard(close, busy || uploading);
  useEffect(() => { if (settings) setForm(value => ({ ...value, ...Object.fromEntries(Object.entries(settings.defaults).filter(([key]) => key in value)), implementation_review_strategy: 'acceptance_contract', implementation_indexed_review_protocol: 1, implementation_backend: 'local_codex' })); }, [settings]);
  useEffect(() => { objectiveRef.current?.focus(); }, []);
  function update(key: string, value: unknown) { setForm(previous => ({ ...previous, [key]: value })); }
  async function upload(files: File[]) {
    if (!files.length || busy || referenceOperationRef.current) return;
    if (references.length >= REFERENCE_LIMIT) { setError('每项研究最多上传或导入 10 份参考资料。'); if (inputRef.current) inputRef.current.value = ''; return; }
    referenceOperationRef.current = true;
    setUploading(true); setError(''); setUploadMessage('');
    try {
      const result = await uploadReferenceBatch({ files, references, progress: setUploadProgress,
        accepted: reference => setReferences(previous => mergeReference(previous, reference)),
        upload: async file => {
          const body = new FormData(); body.append('file', file);
          const response = await fetch('/api/references', { method: 'POST', body });
          const reference = await response.json();
          if (!response.ok) throw new Error(typeof reference.detail === 'string' ? reference.detail : '参考资料上传失败');
          return reference as Json;
        } });
      setUploadMessage(`已添加 ${result.added} 份资料${result.reused ? `，复用 ${result.reused} 份重复资料` : ''}${result.skipped ? `；超过 10 份上限，未上传其余 ${result.skipped} 份` : ''}。`);
      setError(result.errors.join('\n'));
    } finally { referenceOperationRef.current = false; setUploading(false); setUploadProgress(null); if (inputRef.current) inputRef.current.value = ''; }
  }
  async function importUrl() {
    if (busy || referenceOperationRef.current) return;
    if (references.length >= REFERENCE_LIMIT) { setError('每项研究最多上传或导入 10 份参考资料。'); return; }
    if (!safeUrl(referenceUrl)) { setError('请输入完整的 http 或 https 参考资料链接。'); return; }
    referenceOperationRef.current = true;
    setUploading(true); setError(''); setUploadMessage('');
    try { const result = await post<Json>('/references/url', { url: referenceUrl }); setReferences(previous => mergeReference(previous, result)); setReferenceUrl(''); }
    catch (err) { setError((err as Error).message); } finally { referenceOperationRef.current = false; setUploading(false); }
  }
  async function submit(event: FormEvent) {
    event.preventDefault(); if (busy || referenceOperationRef.current) return; setBusy(true); setError('');
    try { onCreated(await post<Run>('/runs', { ...form, title: String(form.title).trim() || String(form.objective).trim().slice(0, 50), reference_ids: references.map(item => item.id) })); }
    catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  }
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy && !uploading) close(); }}><section className="modal create-modal" role="dialog" aria-modal="true" aria-labelledby="create-title"><form onSubmit={submit}><div className="modal-header"><div><div className="eyebrow">开始一项新研究</div><h2 id="create-title">你想探索什么？</h2><p>定义研究方向，让三个智能体共同推进。</p></div><button type="button" className="icon-btn" aria-label="关闭" disabled={busy || uploading} onClick={close}><X size={20} /></button></div><div className="modal-body">
    <Field label="研究方向与目标" help="描述待解决的问题、想改进的方法和期望成果，智能体将先评估可行性。"><textarea ref={objectiveRef} required minLength={8} maxLength={16000} rows={4} placeholder="描述研究问题、现有方法的不足、评价目标与资源约束；也可上传相关论文作为研究起点。" value={form.objective} onChange={event => update('objective', event.target.value)} /></Field>
    <div className="form-grid"><Field label="研究名称（可选）"><input maxLength={160} placeholder="为这项研究起个名字" value={form.title} onChange={event => update('title', event.target.value)} /></Field><Field label="成果或投稿目标（可选）"><input placeholder="例如：CCF A / 实验报告 / 原型系统" maxLength={300} value={form.publication_target} onChange={event => update('publication_target', event.target.value)} /></Field></div>
    <div className="field"><span>已有文章与参考资料（可选） · {references.length} / {REFERENCE_LIMIT}</span><small>可一次选择多篇文章，每份最多 12 MB。真实研究启动后会精读新文章、补充笔记并保存到 RAG；已在 RAG 的文章复用已有分析与笔记。结合这些文章与在线检索的最新工作提出创新点。</small><div className="reference-input"><input ref={inputRef} className="sr-only" type="file" aria-label="选择多篇已有文章" accept=".pdf,.txt,.md" multiple disabled={busy || uploading || references.length >= REFERENCE_LIMIT} onChange={event => void upload(Array.from(event.target.files ?? []))} /><button type="button" className="btn btn-secondary" disabled={busy || uploading || references.length >= REFERENCE_LIMIT} onClick={() => inputRef.current?.click()}>{uploading ? <LoaderCircle size={15} className="spin" /> : <Upload size={15} />}上传多篇 PDF / 文本</button><input aria-label="参考资料链接" type="url" placeholder="或粘贴论文 / 资料链接" disabled={busy || uploading || references.length >= REFERENCE_LIMIT} value={referenceUrl} onChange={event => setReferenceUrl(event.target.value)} /><button type="button" className="btn" disabled={busy || uploading || !referenceUrl || references.length >= REFERENCE_LIMIT} onClick={() => void importUrl()}>导入</button></div><div className="reference-upload-status" role="status" aria-live="polite">{uploadProgress ? `正在上传 ${Math.min(uploadProgress.completed + 1, uploadProgress.total)} / ${uploadProgress.total}：${uploadProgress.fileName}` : uploading ? '正在导入参考资料…' : uploadMessage}</div>{references.length > 0 && <div className="reference-chips">{references.map(reference => <span key={reference.id}><FileText size={13} /><ReferenceText value={reference.title} />{referenceRagLabel(reference) && <small className="reference-rag-status">{referenceRagLabel(reference)}</small>}<button type="button" aria-label={`移除 ${reference.title}`} disabled={busy || uploading} onClick={() => { setReferences(previous => previous.filter(item => item.id !== reference.id)); setUploadMessage(''); }}><X size={12} /></button></span>)}</div>}</div>
    <div className="mode-options"><button type="button" className={form.mode === 'live' ? 'selected' : ''} onClick={() => update('mode', 'live')}><Network size={20} /><div><strong>真实智能体研究</strong><small>使用已配置模型接口，计入真实用量</small></div><span className="radio-dot" /></button><button type="button" className={form.mode === 'demo' ? 'selected' : ''} onClick={() => update('mode', 'demo')}><Beaker size={20} /><div><strong>流程演示 <b>模拟</b></strong><small>预设模拟响应，仅验证流程与界面</small></div><span className="radio-dot" /></button></div>
    <div className="form-grid"><BudgetControl value={form.budget_usd} onChange={value => update('budget_usd', value)} /><Field label="每轮最多实验条数"><input type="number" required min="6" max="100" value={form.max_trials} onChange={event => update('max_trials', number(event.target.value))} /></Field></div>
    <LiteratureVenueControl value={form.ccf_ab_only} onChange={value => update('ccf_ab_only', value)} /><DiscussionControls value={form} onChange={update} /><ResearchCycleControls value={form} onChange={update} /><ImplementationControls value={form} mode={form.mode} onChange={update} />
    <button type="button" className="advanced-toggle" onClick={() => setAdvanced(!advanced)}><Settings2 size={15} />更多执行限制<ChevronDown size={15} className={advanced ? 'rotated' : ''} /></button>{advanced && <div className="form-grid three advanced-fields"><CallBudgetControl value={form.max_calls} onChange={value => update('max_calls', value)} /><ContextBudgetControl value={form.context_tokens} onChange={value => update('context_tokens', value)} /><Field label="单次实验超时（秒）"><input type="number" required min="10" max="7200" value={form.experiment_timeout} onChange={event => update('experiment_timeout', number(event.target.value))} /></Field></div>}
    {advanced && <ComputeControls value={form} onChange={update} />}
    <label className="consent"><input type="checkbox" checked={form.allow_local_execution} onChange={event => update('allow_local_execution', event.target.checked)} /><span><strong>允许本地写入和运行实验代码</strong><small>本机 Codex 写入与测试、实验运行均在独立目录执行，带超时和资源限制；进程隔离不等同于安全沙箱。未授权时执行门禁会阻止实验。</small></span></label><ErrorBox message={error} />
    </div><div className="modal-footer"><span><ShieldCheck size={14} />金额可不设上限，硬件与共识门禁保持启用</span><button type="submit" className="btn btn-primary" disabled={busy || uploading}>{busy ? <LoaderCircle size={16} className="spin" /> : <Plus size={16} />}创建研究任务</button></div></form></section></div>;
}

export function RunTokenUsage({ billing }: { billing: Json }) {
  const native = billing.local_codex?.observed_totals;
  return <span className="run-token-usage" aria-label="模型 token 用量">
    <span><span>API token</span><span>输入 {codexCount(billing.input_tokens)}</span><span>输出 {codexCount(billing.output_tokens)}</span></span>
    <span title="本机 Codex 每线程最新已报告累计用量，每个线程只计一次；未报告的用量不按零估算。"><span>本机 Codex token（已报告）</span><span>输入 {codexCount(native?.inputTokens)}</span><span>输出 {codexCount(native?.outputTokens)}</span></span>
  </span>;
}

function RunDetail({ id, revision, refresh, notify, back, onDelete }: { id: string; revision: number; refresh: () => void; notify: (message: string) => void; back: () => void; onDelete: (run: Run) => void }) {
  const { data: summaryValue, error, loading } = useData<Detail>(`/runs/${encodeURIComponent(id)}/summary`, revision, 6000);
  const summary = summaryValue ? { ...summaryValue, client_refresh: revision } : null;
  const [tab, setTab] = useState(() => readResearchTab(window.location.hash));
  useEffect(() => {
    const selected = () => { if (readRoute().runId === id) setTab(readResearchTab(window.location.hash)); };
    window.addEventListener('hashchange', selected);
    return () => window.removeEventListener('hashchange', selected);
  }, [id]);
  useEffect(() => {
    if (readRoute().runId === id) window.history.replaceState(null, '', `#/runs/${id}${tab === 'memory' ? '?tab=memory' : ''}`);
  }, [id, tab]);
  const snapshot = useProjectionSnapshot(summary ? `${id}:${tab}` : null, projectionRevision(summary), revision);
  const version = snapshot.key;
  const viewPath = summary ? runViewPath(id, tab, summary.loaded_sections) : null;
  const view = useRemoteResource<RunProjection>(viewPath, version, tab === 'experiments' ? 5000 : 0, tab !== 'experiments');
  const data = summary ? mergeRunProjection(summary, view.data) : null;
  const viewReady = !viewPath || Boolean(view.data);
  const [otherAction, setAction] = useState('');
  const [runAction, setRunAction] = useState(initialRunActionState);
  const action = otherAction || runAction.action || '';
  const [actionError, setActionError] = useState('');
  const actionCallbacks = useRef({ refresh, notify }); actionCallbacks.current = { refresh, notify };
  const actionController = useRef<ReturnType<typeof createRunActionController> | null>(null);
  useEffect(() => {
    let storage: Storage | undefined;
    try { storage = window.sessionStorage; } catch { /* Browser storage can be disabled. */ }
    const durable = createDurableRunRequest({ runId: id, storage });
    const controller = createRunActionController({ runId: id,
      request: value => durable.request(value), changed: setRunAction, receiptRequired: true,
      refresh: () => actionCallbacks.current.refresh(),
      accepted: (value, status) => {
        actionCallbacks.current.refresh();
        actionCallbacks.current.notify(({ start: status === 'running' ? '研究已启动' : '启动请求已处理，请查看最新状态',
          resume: status === 'running' ? '研究已恢复' : '恢复请求已处理，请查看最新状态',
          pause: '暂停请求已提交，执行状态将持久化', cancel: '取消请求已处理，请查看最新状态' })[value]);
      } });
    actionController.current = controller;
    const pending = durable.pending();
    if (pending) void controller.send(pending.action);
    return () => { controller.stop(); durable.stop(); if (actionController.current === controller) actionController.current = null; };
  }, [id]);
  useEffect(() => { actionController.current?.observe(summaryValue?.run); }, [summaryValue]);
  const [reportRevision, setReportRevision] = useState(0);
  const reportRequest = useRemoteResource<RunReport>(summary && tab === 'report' ? `/runs/${encodeURIComponent(id)}/report` : null, `${version}:${reportRevision}`, 0, true);
  const report = reportRequest.data;
  const [limitsOpen, setLimitsOpen] = useState(false);
  const [retryOpen, setRetryOpen] = useState(false);
  const [selectedStage, setSelectedStage] = useState<string | null>(null);
  async function act(value: RunAction) {
    if (action || actionController.current?.get().action) return;
    if (value === 'resume' && summary && validationResumeReason(summary.run)) { setActionError(validationResumeReason(summary.run)!); return; }
    if (value === 'resume' && summary && definitionResumeReason(summary.run, summary.architecture_review)) { setActionError(definitionResumeReason(summary.run, summary.architecture_review)!); return; }
    if (value === 'resume' && summary && hasPendingModerationReview(collectModerationReviews(id, summary.moderation_reviews, summary.billing?.records))) { setActionError(MODERATION_RECOVERY_REASON); return; }
    setActionError('');
    await actionController.current?.send(value);
  }
  function loadReport() { setTab('report'); setReportRevision(value => value + 1); }
  async function exportIdea(_proposal: Json, proposalHash?: string) {
    if (!proposalHash || action) return;
    setAction('idea-materials'); setActionError('');
    try {
      const result = await api<{ markdown: string; filename: string; proposal_hash: string }>(`/runs/${encodeURIComponent(id)}/idea-materials?proposal_hash=${encodeURIComponent(proposalHash)}`);
      if (result.proposal_hash !== proposalHash) throw new Error('返回材料与当前提案版本不一致');
      const url = URL.createObjectURL(new Blob([result.markdown], { type: 'text/markdown;charset=utf-8' }));
      const link = document.createElement('a'); link.href = url; link.download = result.filename; link.click(); URL.revokeObjectURL(url);
      notify('已下载所选版本的想法出处、相似工作对比与论文论证材料');
    } catch (err) { setActionError((err as Error).message); } finally { setAction(''); }
  }
  if (!data || !summary) return <><button className="back-link" onClick={back}><ArrowLeft size={15} />研究任务</button>{runAction.action
    ? <RunActionNotice state={runAction} readError={error} reading={loading} reload={refresh} />
    : <><ErrorBox message={actionError || runAction.error} /><WorkspaceReadStatus loading={loading} error={error} retry={refresh} /></>}</>;
  const { run } = data;
  const visibleReport = currentRunReport(report, run);
  const steps = Array.isArray(data.steps) ? data.steps : [];
  const experiments = Array.isArray(data.experiments) ? data.experiments : [];
  const events = Array.isArray(data.events) ? data.events : [];
  const artifacts = Array.isArray(data.artifacts) ? data.artifacts : [];
  const billing = summary.billing ?? {};
  const implementationReview = run.stage === 'implementation' ? implementationReviewSummary(summary.implementation_progress, id) : null;
  const implementationReviewLabel = '实现文档验收';
  const moderationReviews = collectModerationReviews(id, summary.moderation_reviews, billing.records);
  return <ReferenceNames records={[data, report]}><><button className="back-link" onClick={back}><ArrowLeft size={15} />研究任务</button><div className="page-heading detail-heading"><div><div className="heading-meta"><Mode mode={run.mode} /><Status value={run.status} /><span>{date(run.created_at, true)} 创建</span></div><h1><ReferenceText value={run.title} /></h1></div><div className="action-group">{run.status !== 'running' && <button className="btn" disabled={!!action} onClick={() => setLimitsOpen(true)}><Settings2 size={15} />执行配置</button>}{run.status === 'draft' && <button className="btn btn-primary" disabled={!!action} onClick={() => void act('start')}><Play size={15} />启动研究</button>}{run.status === 'running' && <button className="btn btn-secondary" disabled={!!action} onClick={() => void act('pause')}><Pause size={15} />暂停</button>}<ResumeResearchButton status={run.status} busy={!!action} reviews={moderationReviews} blockReason={validationResumeReason(run) ?? definitionResumeReason(run, summary.architecture_review)} blockReasonId={validationResumeReason(run) ? 'validation-resume-reason' : undefined} resume={() => void act('resume')} />{!['completed', 'cancelled'].includes(run.status) && <button className="btn" disabled={!!action} onClick={() => void act('cancel')}><Square size={13} />取消</button>}<button className="btn" disabled={!!action} onClick={() => void loadReport()}>{action === 'report' ? <LoaderCircle size={15} className="spin" /> : <FileText size={15} />}报告</button><button type="button" className="btn btn-danger-ghost" disabled={!!action} onClick={() => onDelete(run)}><Trash2 size={15} />删除</button></div></div>
    <ResearchObjective key={`objective:${id}`} objective={run.objective} />
    <RunActionNotice state={runAction} readError={error} reading={loading} reload={refresh} />
    {run.mode === 'demo' && <div className="demo-banner"><Beaker size={17} /><strong>演示模式</strong><span>智能体决策为预设模拟响应，实验为本机处理器上的真实小型运行。结果用于验证编排流程，不构成真实智能体科研结论。</span></div>}
    {!hasPendingModerationReview(moderationReviews) && <RecoveryNotice innovationPending={run.config?.innovation_protocol === 1 && data.innovation?.status !== 'completed'} contextReferenceCheck={data.context_reference_check} run={run} handoffs={data.handoffs} literature={data.literature} openLimits={() => setLimitsOpen(true)} openConsensus={() => setTab('consensus')} openEvidence={() => setTab('evidence')} openArchitecture={() => setSelectedStage('design')} openImplementation={() => setSelectedStage('implementation')} openValidation={() => setSelectedStage('validation_review')} />}<ModerationReviewPanel runId={id} reviews={moderationReviews} />
    {!actionError && !runAction.error && !error && isHistoricalReadingGate(run.error ?? '', data.literature?.reading_gate?.approved)
      ? <RequestRecoveryDiagnostic message={run.error ?? ''} historical />
      : <ErrorBox message={actionError || (!runAction.action ? runAction.error : '') || (!['blocked', 'paused', 'failed'].includes(run.status) ? run.error || '' : '')} />}
    <details className="panel compact-panel" open={retryOpen} onToggle={event => setRetryOpen(event.currentTarget.open)}><summary>检查最终回答重试与已保存授权</summary>{retryOpen && <RequestRetryPanel run={run} moderationPending={hasPendingModerationReview(moderationReviews)} externalBusy={!!action} onBusyChange={busy => setAction(previous => busy ? 'request-retry' : previous === 'request-retry' ? '' : previous)} onAuthorized={() => notify('已授权一次重试，请使用顶部“恢复执行”开始；本次授权未启动研究。')} />}</details>
    <ResearchStageTrack run={run} steps={summary.steps ?? []} researchMap={summary.research_map} data={summary} />
    <ConsensusStatus runId={id} activity={summary.research_map?.consensus_activity} onDetails={() => setTab('consensus')} />
    <div className="run-meta-strip"><KV label="模型费用估算">{totalModelCost(billing)} <em>· API {money(billing.cost_usd)} / {budgetLabel(run.config?.budget_usd)}</em><RunTokenUsage billing={billing} /></KV><TaskCallBudget runId={id} summary={billing.call_budget} limit={run.config?.max_calls} /><KV label="实验总记录">{summary.counts?.experiments ?? '未报告'} <em>· {run.state?.experiment_attempt_policy ? "实验不限次数" : `每轮上限 ${number(run.config?.max_trials)}`}</em></KV><KV label={implementationReview ? implementationReviewLabel : data.current_debate?.phase === 'voting' ? '当前评议轮次' : '最近完整评议'}>{implementationReview ? <ImplementationReviewSummary value={implementationReview} /> : <DiscussionProgress round={number(data.current_debate?.phase === 'voting' ? data.current_debate.round : data.consensus?.round)} config={run.config ?? {}} progress={data.consensus?.discussion_progress} reassessment={data.consensus?.progress_reassessment} />}</KV></div>
    <TaskCallBudgetNote providerRequests={billing.requests} />
    <CodexCostNotice />
    <div className="detail-columns"><section className="panel detail-main"><div className="tabs detail-tabs">{[['command', '研究指挥台', Compass], ['orchestration', '编排与工具', Workflow], ['tools', '工具调用', Wrench], ['memory', '记忆与上下文', Database], ['timeline', '执行记录', Activity], ['handoffs', '智能体交接', Network], ['innovation', '创新点讨论', BrainCircuit], ['consensus', '共识讨论', MessageSquareText], ['evidence', '研究证据', BookOpen], ['design', '架构设计', GitBranch], ['implementation', '代码实现与验收', Code2], ['experiments', '实验对比', FlaskConical], ['artifacts', '研究产物', FolderOpen], ['report', '结果报告', FileText]].map(([value, label, Icon]) => { const TabIcon = Icon as LucideIcon; return <button key={String(value)} className={tab === value ? 'selected' : ''} onClick={() => { if (value === 'report' && !visibleReport?.markdown) void loadReport(); else setTab(String(value)); }}><TabIcon size={15} />{String(label)}</button>; })}</div>
      {tab === 'design' && <ArchitectureStageEntry key={`architecture:${id}`} progressView data={summary} run={run} openLimits={() => setLimitsOpen(true)} prepared={() => { refresh(); notify('恢复交接已准备，恢复执行后继续；原候选与评议记录保留。'); }} externalBusy={!!action} onBusyChange={busy => setAction(previous => busy ? 'definition-prepare' : previous === 'definition-prepare' ? '' : previous)} />}
      {tab === 'implementation' && <ImplementationProgress progress={summary.implementation_progress} runId={id} />}
      {viewPath && viewReady && <button className="btn small" onClick={snapshot.refresh}><RefreshCw size={14} />{snapshot.stale ? '有新记录，刷新当前详情' : '刷新当前详情'}</button>}
      {!viewReady && (view.error ? <div><ErrorBox message={view.error} /><button className="btn" onClick={snapshot.refresh}>重新读取此页</button></div> : <Loading />)}
      {viewReady && <>
      {tab === 'timeline' && <ReasoningRequests key={id} runId={id} tokenSummary={data.billing?.local_codex} />}
      {tab === 'timeline' && (events.length ? <div className="event-timeline">{[...events].reverse().map((event, index) => <div className="event-row" key={event.id ?? index}><div className={`event-marker ${String(event.kind).includes('error') || String(event.kind).includes('failed') ? 'event-error' : ''}`}>{index === 0 && run.status === 'running' ? <LoaderCircle size={14} className="spin" /> : <span />}</div><div className="event-content"><div className="event-heading"><span>{humanizeMessage(event.kind ?? 'event')}</span><time>{date(event.created_at, true)}</time></div><TimelineToolSummary event={event} /><HumanMessage text={event.message ?? ''} />{event.kind === 'review.citations_discarded' && <CitationDiscardNotice audit={event.data?.citation_discard_audit} />}{event.data && Object.keys(event.data).length > 0 && <details><summary>查看详情<ChevronDown size={12} /></summary><pre><ReferenceText value={event.data} /></pre></details>}</div></div>)}</div> : <Empty icon={Activity} title="准备好启动研究" description="启动后，智能体决策、工具调用、资源门禁和实验事件将按时间持续记录。" />)}
      {tab === 'command' && <ResearchCommandCenter key={id} datasetAdaptation={Boolean(run.state?.dataset_adaptation)} implementationProgress={summary.implementation_progress} codexModel={run.config?.codex_model} onCodexDetails={() => setSelectedStage(data.research_map?.current_execution?.source === 'persisted_dataset_codex' ? run.stage : 'implementation')} revision={projectionRevision(summary)} refreshRevision={revision} definitionReview={data.architecture_review?.revision?.definition_review} map={data.research_map ? { ...data.research_map, links: data.research_map.links ?? (data.handoffs ?? []).map(handoff => ({ id: handoff.id, from_role: handoff.from_role, to_role: handoff.to_role, purpose: handoff.purpose, status: handoff.status, at: handoff.updated_at ?? handoff.created_at, active: handoff.status === 'running' && run.status === 'running' })) } : undefined} runId={id} />}
      {tab === 'tools' && <ToolCallsPanel key={id} runId={id} revision={revision} running={run.status === 'running'} />}
      {tab === 'memory' && <MemoryExplorer key={id} runId={id} revision={revision} />}
      {tab === 'orchestration' && <OrchestrationView orchestration={data.orchestration} cycles={data.research_cycles} observedAgents={data.research_map?.agents} />}
      {tab === 'innovation' && <ViewBoundary label="创新点讨论"><InnovationView session={data.innovation} run={run} papers={data.literature?.papers ?? []} /></ViewBoundary>}
      {tab === 'consensus' && <ConsensusView consensus={data.consensus} currentDebate={data.current_debate} papers={data.literature?.papers ?? []} references={data.references} onExportProposal={(proposal, hash) => void exportIdea(proposal, hash)} />}
      {tab === 'handoffs' && <HandoffBoard handoffs={data.handoffs} />}
      {tab === 'evidence' && <EvidencePanel steps={steps} run={run} references={data.references} literature={data.literature} />}
      {tab === 'design' && <ViewBoundary key={`design:${id}`} label="架构设计"><StageDetailBody stage="design" step={steps.find(step => step.stage === 'design')} data={data} run={run} /></ViewBoundary>}
      {tab === 'implementation' && <LocalCodexPanel runId={id} workflow={data.local_codex} />}
      {tab === 'experiments' && <ExperimentView runId={id} experiments={experiments} evaluation={data.evaluation} summary={data.experiment_summary} />}
      {tab === 'artifacts' && (artifacts.length ? <div className="artifact-list">{artifacts.map(artifact => <div key={artifact.id} className="artifact-row"><IconBox icon={artifact.kind === 'code' ? Code2 : FileText} /><div><strong><ReferenceText value={artifact.name} /></strong><code><ReferenceText value={artifact.path} /></code></div><Pill>{artifact.kind}</Pill></div>)}<a className="btn btn-secondary" href={`/api/runs/${encodeURIComponent(id)}/export`} download><ArrowDownToLine size={15} />下载完整研究归档</a></div> : <Empty icon={FolderOpen} title="研究产物将在这里归档" description="代码、实验记录、引用证据与最终报告将关联至这项研究。" />)}
      {tab === 'report' && reportRequest.loading && <Loading />}
      {tab === 'report' && reportRequest.error && <ErrorBox message={reportRequest.error} />}
      {tab === 'report' && !reportRequest.loading && !reportRequest.error && (visibleReport?.markdown ? <div className="report-view"><div className="report-toolbar"><Pill icon={FileText}>{visibleReport.kind === 'stage' ? '未完成 · 阶段报告' : '最终研究报告'}</Pill><button className="btn small" disabled={!!action} onClick={() => void loadReport()}><RefreshCw size={14} />刷新报告</button><button className="btn small" onClick={() => { const url = URL.createObjectURL(new Blob([visibleReport.markdown], { type: 'text/markdown;charset=utf-8' })); const link = document.createElement('a'); link.href = url; link.download = `research-${id.slice(0, 8)}-${visibleReport.kind === 'stage' ? 'stage' : 'final'}.md`; link.click(); URL.revokeObjectURL(url); }}><ArrowDownToLine size={14} />下载报告</button><a className="btn small" href={`/api/runs/${encodeURIComponent(id)}/export`} download>完整归档<ArrowDownToLine size={14} /></a></div><ReportKindNotice report={visibleReport} /><Markdown text={visibleReport.markdown} /></div> : <Empty icon={FileText} title={['blocked', 'failed'].includes(run.status) ? '读取当前阶段报告' : run.status === 'running' ? '研究仍在推进' : '报告尚未生成'} description={['blocked', 'failed'].includes(run.status) ? '刷新可读取已保存的阻塞或失败阶段报告；未保存时仍可查看执行记录与研究产物。' : '最终报告在报告阶段提交后提供。已恢复运行时不会把旧阶段报告显示为完成结果。'} action={<button className="btn" disabled={!!action} onClick={() => void loadReport()}><RefreshCw size={14} />刷新报告</button>} />)}
      {tab === 'report' && <HistoricalReportPreview key={historicalPreviewKey(run)} run={run} experiments={experiments} renderMarkdown={text => <Markdown text={text} />} />}
      </>}
    </section><aside className="detail-aside" aria-label="研究任务侧栏">
      <LazyContextPanel run={run} revision={projectionRevision(summary)} refresh={revision} billing={billing} /><section className="panel compact-panel"><h3><ShieldCheck size={16} />执行边界</h3><KV label="共识门禁">{run.state?.dataset_adaptation ? '复用原方法共识；新数据协议单独复核' : summary.consensus?.unanimous ? <span className="good-text">一致通过</span> : '等待共识'}</KV><KV label="本地代码执行">{run.config?.allow_local_execution ? '已授权' : '未授权'}</KV><KV label="单次实验超时">{number(run.config?.experiment_timeout)} 秒</KV><KV label="执行来源">{run.mode === 'demo' ? '演示模拟' : '真实模型接口'}</KV>{run.config?.publication_target && <KV label="成果目标">{run.config.publication_target}</KV>}</section><section className="panel compact-panel"><h3><Layers3 size={16} />阶段产出</h3>{summary.steps?.length ? summary.steps.map((step, index) => <button className="btn small" key={step.stage ?? index} onClick={() => setSelectedStage(step.stage)}>{STAGES.find(item => item[0] === step.stage)?.[1] ?? step.stage}<ChevronRight size={13} /></button>) : <p className="muted small-text">各阶段的结构化决策会自动存档。</p>}</section></aside></div>
    {selectedStage && <StageDetails stage={selectedStage} label={STAGES.find(item => item[0] === selectedStage)?.[1] ?? selectedStage} data={summary} run={run} close={() => setSelectedStage(null)} />}
    {limitsOpen && <RunLimits run={run} codexLocked={data.local_codex?.configuration_locked === true} close={() => setLimitsOpen(false)} saved={() => { setLimitsOpen(false); refresh(); notify('执行边界已保存，可以恢复研究。'); }} />}
  </></ReferenceNames>;
}

function RunLimits({ run, codexLocked, close, saved }: { run: Run; codexLocked?: boolean; close: () => void; saved: () => void }) {
  const fields: [string, string, number, number][] = [['max_calls', '模型调用次数上限', 3, 1000], ['max_trials', '每研究轮实验条数上限', 6, 100], ['context_tokens', '上下文词元预算', 4000, CONTEXT_TOKEN_MAX], ['experiment_timeout', '实验超时（秒）', 10, 7200], ['architecture_stagnation_rounds', '架构评议连续无进展暂停轮数', 2, 10]];
  const [config, setConfig] = useState<Json>(Object.fromEntries([...fields.map(([key]) => [key, run.config[key] ?? (key === "architecture_stagnation_rounds" ? 3 : run.config[key])]), ['budget_usd', run.config.budget_usd ?? null], ['max_rounds', run.config.max_rounds ?? 3], ['discussion_mode', run.config.discussion_mode ?? 'bounded'], ['stagnation_rounds', run.config.stagnation_rounds ?? 3], ['max_research_cycles', run.research_iteration ? run.research_iteration.limit : (run.config.max_research_cycles === undefined ? 3 : run.config.max_research_cycles)], ['min_relative_improvement', run.config.min_relative_improvement ?? 0.01], ['allow_local_execution', !!run.config.allow_local_execution], ['execution_mode', run.config.execution_mode ?? 'local'], ['python_executable', run.config.python_executable ?? ''], ['gpu_index', run.config.gpu_index ?? null], ['vram_limit_gb', run.config.vram_limit_gb ?? null], ['cpu_limit_cores', run.config.cpu_limit_cores ?? null], ['ram_limit_gb', run.config.ram_limit_gb ?? null], ['disk_limit_gb', run.config.disk_limit_gb ?? null], ['implementation_backend', 'local_codex'], ['codex_timeout_seconds', run.config.codex_timeout_seconds ?? 1800], ['codex_max_revisions', run.config.codex_max_revisions === undefined ? 3 : run.config.codex_max_revisions], ['implementation_review_strategy', run.config.implementation_review_strategy ?? 'acceptance_contract'], ['implementation_indexed_review_protocol', run.config.implementation_indexed_review_protocol], ['implementation_review_max_revisions', run.config.implementation_review_max_revisions], ['implementation_review_max_seconds', run.config.implementation_review_max_seconds], ['implementation_review_time_basis', run.config.implementation_review_time_basis ?? 'policy_active_time'], ['implementation_review_max_calls', run.config.implementation_review_max_calls], ['codex_model', run.config.codex_model ?? ''], ['codex_reasoning_effort', run.config.codex_reasoning_effort ?? ''], ['codex_project_dir', run.config.codex_project_dir ?? 'F:\\Python program\\AutoResearch']]));
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  useDialogKeyboard(close, busy);
  async function submit(event: FormEvent) { event.preventDefault(); setBusy(true); setError(''); try { await api(`/runs/${encodeURIComponent(run.id)}/config`, { method: 'PUT', body: JSON.stringify(config) }); saved(); } catch (err) { setError((err as Error).message); } finally { setBusy(false); } }
  return <div className="modal-backdrop"><section className="modal limits-modal" role="dialog" aria-modal="true" aria-labelledby="limits-title"><form onSubmit={submit}><div className="modal-header"><div><div className="eyebrow">执行配置</div><h2 id="limits-title">调整执行配置</h2><p>调整讨论停止条件、预算与执行授权后，可恢复当前研究。</p></div><button className="icon-btn" type="button" aria-label="关闭配置" disabled={busy} onClick={close}><X size={20} /></button></div><div className="modal-body"><div className="form-grid three"><BudgetControl value={config.budget_usd} onChange={value => setConfig(previous => ({ ...previous, budget_usd: value }))} />{fields.map(([key, label, min, max]) => key === 'max_calls' ? <CallBudgetControl key={key} value={config.max_calls} onChange={value => setConfig(previous => ({ ...previous, max_calls: value }))} /> : key === 'context_tokens' ? <ContextBudgetControl key={key} value={config.context_tokens} onChange={value => setConfig(previous => ({ ...previous, context_tokens: value }))} /> : <Field key={key} label={label}><input required type="number" min={min} max={max} step={key === 'budget_usd' ? '0.01' : '1'} value={config[key]} onChange={event => setConfig(previous => ({ ...previous, [key]: number(event.target.value) }))} />{key === 'architecture_stagnation_rounds' && <small>不设总评议轮数上限；只有连续无实质推进或重复已否决内容时暂停，独立评审确认问题闭合后清零。</small>}</Field>)}</div><DiscussionControls value={config} onChange={(key, value) => setConfig(previous => ({ ...previous, [key]: value }))} /><ResearchCycleControls value={config} onChange={(key, value) => setConfig(previous => ({ ...previous, [key]: value }))} /><ImplementationControls value={config} mode={run.mode} projectLocked={codexLocked} onChange={(key, value) => setConfig(previous => ({ ...previous, [key]: value }))} /><ComputeControls value={config} locked={codexLocked} onChange={(key, value) => setConfig(previous => ({ ...previous, [key]: value }))} /><label className="consent"><input type="checkbox" checked={config.allow_local_execution} onChange={event => setConfig(previous => ({ ...previous, allow_local_execution: event.target.checked }))} /><span><strong>允许本地写入和运行实验代码</strong><small>本机 Codex 写入与测试沿用此授权；本地进程带超时和资源限制，但不等同于安全沙箱。修改配置不会自动启动任务。</small></span></label><ErrorBox message={error} /></div><div className="modal-footer"><span><ShieldCheck size={14} />已产生的实验记录与费用会保留</span><button className="btn btn-primary" disabled={busy}>{busy ? <LoaderCircle size={15} className="spin" /> : <Check size={15} />}保存配置</button></div></form></section></div>;
}

function Markdown({ text }: { text: string }) {
  return <Suspense fallback={<Loading />}><MarkdownReport text={text} /></Suspense>;
}

function LazyContextPanel({ run, revision, refresh, billing }: { run: Run; revision: string; refresh: number; billing: Json }) {
  const [open, setOpen] = useState(false);
  const snapshot = useProjectionSnapshot(open ? run.id : null, revision, refresh);
  const resource = useRemoteResource<RunProjection>(open ? `/runs/${encodeURIComponent(run.id)}/views/context` : null, snapshot.key, 0, true);
  return <section className="panel compact-panel"><h3><BrainCircuit size={16} />上下文与缓存</h3><p className="panel-note">按需查看各角色最近上下文、压缩记录与缓存统计。</p><button className="btn small" aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? '收起上下文详情' : '查看上下文详情'}</button>{open && snapshot.stale && <button className="btn small" onClick={snapshot.refresh}>有新记录，刷新上下文</button>}{open && (resource.error ? <><ErrorBox message={resource.error} /><button className="btn small" onClick={snapshot.refresh}>重新读取</button></> : resource.data ? <ContextPanel context={resource.data.context} billing={billing} configuredContextTokens={run.config?.context_tokens} /> : <Loading />)}</section>;
}

export function ContextPanel({ context, billing, configuredContextTokens }: { context?: Json; billing: Json; configuredContextTokens?: number }) {
  const value = context ?? {};
  const used = value.current_tokens ?? value.tokens_estimate ?? value.estimated_tokens ?? value.context_tokens ?? 0;
  const contexts: Json[] = Array.isArray(value.roles) ? value.roles : [];
  const compactions = value.compactions ?? value.compaction_count ?? value.snapshots ?? 0;
  const configuredBudget = typeof configuredContextTokens === 'number' && Number.isInteger(configuredContextTokens) && configuredContextTokens > 0 ? `${compact(configuredContextTokens)} 词元` : '未记录';
  const currentLimits: Json[] = Array.isArray(value.current_limits) ? value.current_limits : [];
  return <section className="panel compact-panel">
    <h3><BrainCircuit size={16} />上下文与缓存</h3>
    <div className="context-gauge"><strong>{compact(used)}<small> 词元</small></strong><span>各角色最近请求上下文合计（估算）</span></div>
    <KV label="当前配置上限">{configuredBudget}</KV>
    <p className="panel-note">上限不是每次发送量。每次重新组装并尽量压缩工作集，已提交的读取记录可归档并按需展开。普通 High 的 4 万词元目标和分槽额度均为参考，不设独立的 6 万硬上限；必要原文、精确数值、反证和约束继续保留。</p>
    <p className="panel-note">新预算在下次模型请求生效；实际可用输入仍受模型总窗口限制，并扣除本次输出额度和 256 个词元安全余量。下方各角色数值保留最近请求记录。</p>
    {contexts.map(item => {
      const compiler = item.compiler ?? {};
      const working = compiler.budget_policy === 'task_working_set_v3';
      const minimized = compiler.budget_policy === 'configured_capacity_working_set_v3';
      const legacy = !!item.compiler && !working && !minimized && compiler.budget_policy !== 'configured_capacity_v2';
      const current = currentLimits.find(limit => limit.role === item.role);
      return <div className="context-role" key={item.role}>
        <div><span><ReferenceText value={ROLE[item.role]?.name ?? item.role} /> · 最近请求</span></div>
        {current && <KV label="当前默认可用输入">{compact(current.input_limit)}</KV>}
        <KV label="最近输入（估算）">{compact(item.tokens_estimate)}</KV>
        {typeof compiler.target_tokens === 'number' && <KV label="该次组装软目标">{compact(compiler.target_tokens)}</KV>}
        <KV label={working ? '该次任务输入上限' : legacy ? '该次任务限制（旧规则）' : '该次可用输入上限'}>{compact(item.limit)}</KV>
        {working && typeof compiler.capacity_ceiling_tokens === 'number' && <KV label="该次模型与配置容量">{compact(compiler.capacity_ceiling_tokens)}</KV>}
        <Progress value={number(item.tokens_estimate) / Math.max(1, number(item.limit)) * 100} className="teal" />
        {legacy && <p className="panel-note">旧记录保留当时限制；新请求按当前配置上限执行。</p>}
      </div>;
    })}
    <KV label="压缩快照">{Array.isArray(compactions) ? compactions.length : number(compactions)}</KV>
    <KV label="本地响应缓存命中">{number(billing.response_cache_hits)}</KV>
    <KV label="供应商缓存词元">{compact(billing.cached_tokens)}</KV>
    <KV label="持久化记忆">{number(value.memory_count ?? value.memories)}</KV>
    <p className="panel-note">共享工具结果可复用；模型响应按角色与供应商隔离。远程推理缓存由上游管理。</p>
    <details className="context-details"><summary>查看原始统计<ChevronDown size={12} /></summary><pre><ReferenceText value={value} /></pre></details>
  </section>;
}

function LiteraturePage({ notify }: { notify: (message: string) => void }) {
  const [query, setQuery] = useState(''); const [papers, setPapers] = useState<Json[]>([]); const [searched, setSearched] = useState(false);
  const [busy, setBusy] = useState(false); const [verifying, setVerifying] = useState(''); const [error, setError] = useState('');
  async function search(event: FormEvent) { event.preventDefault(); if (!query.trim()) return; setBusy(true); setError(''); try { const result = await api<Json[]>(`/literature?q=${encodeURIComponent(query.trim())}`); setPapers(result); setSearched(true); } catch (err) { setError((err as Error).message); } finally { setBusy(false); } }
  async function verify(paper: Json, index: number) { setVerifying(String(index)); setError(''); try { const result = await post<Json>('/literature/verify', paper); setPapers(previous => previous.map((item, i) => i === index ? result : item)); notify(result.verified ? '文献已通过来源核验' : '核验已完成，请查看验证信息'); } catch (err) { setError((err as Error).message); } finally { setVerifying(''); } }
  return <ReferenceNames records={papers}><><div className="page-heading"><div><div className="eyebrow">以证据支撑判断</div><h1>让每一个引用，经得起核验</h1><p>在线发现文献，并通过权威记录检查标识与元数据。发现和验证分别留痕。</p></div><Pill icon={ShieldCheck}>文献研究员 · 资料检索</Pill></div><form className="large-search" onSubmit={search}><Search size={22} strokeWidth={1.5} /><input required aria-label="搜索文献" placeholder="输入研究问题、论文标题、关键词或文献标识（DOI）…" value={query} onChange={event => setQuery(event.target.value)} /><button className="btn btn-primary" disabled={busy}>{busy ? <LoaderCircle size={16} className="spin" /> : <ArrowRight size={16} />}搜索文献</button></form><div className="source-strip"><span>公共学术来源</span><Pill>Crossref</Pill><Pill>OpenAlex</Pill><Pill>arXiv</Pill><span className="source-note">核验元数据真实性，不等同于认可论文结论</span></div><ErrorBox message={error} /><section className="panel literature-panel"><SectionTitle title={searched ? `检索结果 · ${papers.length} 篇` : '为研究建立证据基础'} action={searched && <span className="muted small-text">{papers.filter(paper => paper.verified).length} 篇已核验</span>} />{papers.length ? <div className="paper-list">{papers.map((paper, index) => <article className="paper" key={paper.id ?? index}><span className="paper-number">{String(index + 1).padStart(2, '0')}</span><div className="paper-body"><div className="paper-tags"><Pill>{paper.source ?? '在线来源'}</Pill><span>{paper.year ?? '年份未知'}</span>{paper.verified ? <span className="verified"><ShieldCheck size={13} />已核验</span> : <span className="unverified">待核验</span>}</div><h3><ReferenceText value={paper.title} /></h3><LiteratureVenue paper={paper} /><p className="paper-authors"><ReferenceText value={Array.isArray(paper.authors) ? paper.authors.map((author: unknown) => typeof author === 'string' ? author : textOf(author)).join(' · ') : textOf(paper.authors)} /></p>{paper.abstract && <p className="paper-abstract"><ReferenceText value={paper.abstract} /></p>}<div className="paper-footer"><code><ReferenceText value={paper.doi ? `DOI: ${paper.doi}` : '未返回文献标识（DOI）'} /></code><div>{safeUrl(paper.url) && <a className="text-btn" href={safeUrl(paper.url)} target="_blank" rel="noreferrer">查看原文<ExternalLink size={13} /></a>}<button className="btn small" disabled={!!verifying} onClick={() => void verify(paper, index)}>{verifying === String(index) ? <LoaderCircle size={13} className="spin" /> : <ShieldCheck size={13} />}{paper.verified ? '重新核验' : '核验真实性'}</button></div></div>{paper.verification && <details className="verification-details"><summary>核验记录{paper.checked_at && ` · ${date(paper.checked_at, true)}`}<ChevronDown size={12} /></summary><pre><ReferenceText value={paper.verification} /></pre></details>}</div></article>)}</div> : <Empty icon={BookOpen} title={searched ? '没有找到匹配的文献' : '从一个明确的问题开始检索'} description={searched ? '尝试更简短的英文关键词、精确标题或文献标识（DOI）。' : '输入研究方向后，系统会在线获取真实文献记录。结果不会被预设示例填充。'} />}</section></></ReferenceNames>;
}

function MemoryPage({ runs, notify }: { runs: RunListItem[]; notify: (message: string) => void }) {
  const [query, setQuery] = useState(''); const [submittedQuery, setSubmittedQuery] = useState(''); const [runId, setRunId] = useState(''); const [revision, setRevision] = useState(0);
  const [offset, setOffset] = useState(0); const [indexOffset, setIndexOffset] = useState(0); const [indexOpen, setIndexOpen] = useState(false); const [ragOpen, setRagOpen] = useState(false);
  const [adding, setAdding] = useState(false); const [text, setText] = useState(''); const [role, setRole] = useState('director'); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const { data: memories, loading, error: loadError } = useData<Json[]>(memoryPagePath(submittedQuery, runId, offset), revision);
  const { data: memoryIndex, loading: indexLoading, error: indexError } = useData<MemoryIndex>(indexOpen ? memoryIndexPath(runId, indexOffset) : null, revision, 12000);
  async function add(event: FormEvent) { event.preventDefault(); setBusy(true); setError(''); try { await post('/memory', { text, role, ...(runId ? { run_id: runId } : {}), metadata: { source: 'user', provenance: 'user_authored' } }); setText(''); setAdding(false); setOffset(0); setRevision(value => value + 1); notify('记忆已保存，可在后续研究中检索'); } catch (err) { setError((err as Error).message); } finally { setBusy(false); } }
  return <ReferenceNames records={[runs, memoryIndex, memories]}><><div className="page-heading"><div><div className="eyebrow">持续积累研究知识</div><h1>让经验积累，而非重新开始</h1><p>保留决策、实验观察与证据。以检索增强上下文，在后续研究中复用知识。</p></div><button className="btn btn-primary" onClick={() => setAdding(!adding)}><Plus size={16} />添加记忆</button></div><div className="memory-explainer"><div><Database size={21} /><span><strong>持久化存储</strong><small>本地数据库保存与检索</small></span></div><ArrowRight size={18} /><div><Search size={21} /><span><strong>相关知识召回</strong><small>按问题检索历史片段</small></span></div><ArrowRight size={18} /><div><BrainCircuit size={21} /><span><strong>可追溯上下文</strong><small>保留来源与任务归属</small></span></div></div>
    <MemoryStudyEntry runs={runs} runId={runId} />
    <details className="on-demand-panel" onToggle={event => setRagOpen(event.currentTarget.open)}><summary>共享论文 RAG<ChevronDown size={14} /></summary>{ragOpen && <PaperRagPanel revision={revision} />}</details>
    {adding && <form className="panel add-memory" onSubmit={add}><SectionTitle title="添加一条研究记忆" action={<button type="button" className="icon-btn" aria-label="取消添加" onClick={() => setAdding(false)}><X size={16} /></button>} /><Field label="内容"><textarea required rows={4} maxLength={20000} placeholder="记录有价值的实验观察、方法约束或已验证的证据…" value={text} onChange={event => setText(event.target.value)} /></Field><div className="add-memory-footer"><Field label="归属角色"><select value={role} onChange={event => setRole(event.target.value)}>{Object.entries(ROLE).map(([id, item]) => <option value={id} key={id}><ReferenceText value={item.name} /></option>)}</select></Field><span className="muted small-text">保存到下方选择的任务；未选择时作为共享记忆。</span><button className="btn btn-primary" disabled={busy}>{busy ? <LoaderCircle size={15} className="spin" /> : <Check size={15} />}保存记忆</button></div><ErrorBox message={error} /></form>}
    <section className="panel"><form className="list-toolbar memory-toolbar" onSubmit={event => { event.preventDefault(); setSubmittedQuery(query); setOffset(0); setRevision(value => value + 1); }}><div className="search-input"><Search size={16} /><input placeholder="检索相关知识、决策或实验观察…" aria-label="搜索记忆" value={query} onChange={event => setQuery(event.target.value)} /></div><select aria-label="按研究任务筛选（含共享记忆）" value={runId} onChange={event => { setRunId(event.target.value); setOffset(0); setIndexOffset(0); }}><option value="">全部研究 / 共享记忆</option>{runs.map(run => <option key={run.id} value={run.id}><ReferenceText value={run.title} /></option>)}</select><button className="btn" type="submit">检索</button></form><details className="on-demand-panel" onToggle={event => setIndexOpen(event.currentTarget.open)}><summary>文献与 PDF 记忆索引<ChevronDown size={14} /></summary>{indexOpen && <MemoryIndexPanel key={runId || 'all'} data={memoryIndex} loading={indexLoading} error={indexError} onRefresh={() => setRevision(value => value + 1)} onOffset={setIndexOffset} />}</details><ErrorBox message={loadError} />{loading ? <Loading /> : memories?.length ? <div className="memory-list">{memories.map((memory, index) => <article key={memory.id ?? index} className="memory-item"><IconBox icon={Database} className="teal" /><div><div className="memory-meta"><Pill><ReferenceText value={ROLE[memory.role]?.name ?? memory.role ?? '共享记忆'} /></Pill>{memory.run_id && <span><ReferenceText value={runs.find(run => run.id === memory.run_id)?.title ?? memory.run_id.slice(0, 10)} /></span>}<time>{date(memory.created_at, true)}</time></div><MemoryRecord memory={memory} runId={runId} revision={revision} /><div className="memory-source"><span>来源：{memory.metadata?.source ?? memory.metadata?.provenance ?? memory.source ?? '研究过程'}</span>{memory.score !== undefined && <span>相关度 {number(memory.score).toFixed(3)}</span>}<code>{memory.id?.slice(0, 12)}</code></div></div></article>)}</div> : <Empty icon={Database} title={submittedQuery ? '没有匹配的记忆' : '研究记忆，将在这里沉淀'} description={submittedQuery ? '尝试其他关键词或切换任务筛选。' : '运行研究会自动积累知识，你也可以手动添加领域经验与约束。'} />}{memories && <RecordPagination label="记忆" offset={offset} count={memories.length} loading={loading} onOffset={setOffset} />}</section>
  </></ReferenceNames>;
}

function BillingPage({ revision, runs, runsLoaded }: { revision: number; runs: RunListItem[]; runsLoaded: boolean }) {
  const [runId, setRunId] = useState('');
  const [pricingRefresh, setPricingRefresh] = useState(0);
  const [offset, setOffset] = useState(0); const [nativeOffset, setNativeOffset] = useState(0); const [nativeOpen, setNativeOpen] = useState(false); const [nativeThreadsOpen, setNativeThreadsOpen] = useState(false);
  const { data, loading, error } = useData<Json>(billingPagePath(runId, offset, nativeOffset, nativeOpen || nativeThreadsOpen), revision + pricingRefresh, 7000);
  if (loading && !data) return <Loading />;
  const billing = data ?? {};
  const records = Array.isArray(billing.records) ? billing.records : [];
  const providers = Array.isArray(billing.by_provider) ? billing.by_provider : Object.entries(billing.by_provider ?? {}).map(([provider, value]) => ({ provider, ...(typeof value === 'object' ? value as Json : { cost_usd: value }) }));
  return <><div className="page-heading"><div><div className="eyebrow">调用与计费记录</div><h1>看清模型用量与已记录费用</h1><p>按模型单价与消耗 token 核算费用，输入、输出及缓存分别计价。实际收费以对应账单为准。</p></div><div className="billing-filter"><label htmlFor="billing-run">费用归属</label><select id="billing-run" value={runId} onChange={event => { setRunId(event.target.value); setOffset(0); setNativeOffset(0); }}><option value="">全部任务（含明确标记的演示记录）</option>{billingRunOptions(runs, [...records, ...(Array.isArray(billing.local_codex?.records) ? billing.local_codex.records : [])], runsLoaded).map(option => <option key={option.id} value={option.id}>{option.label}</option>)}</select></div></div><ErrorBox message={error} /><div className="stats-ribbon billing-stats">{[{ label: '模型费用估算', value: totalModelCost(billing), note: 'API 已记录费用 + Codex 已计价部分' }, { label: '供应商输入词元', value: compact(billing.input_tokens), note: `${compact(billing.cached_tokens)} 缓存词元` }, { label: '供应商输出词元', value: compact(billing.output_tokens), note: '含供应商返回的用量' }, { label: '供应商请求', value: compact(billing.requests), note: `${number(billing.response_cache_hits)} 条本地缓存命中记录` }].map(item => <div className="stat" key={item.label}><div className="stat-label">{item.label}</div><div className="stat-value">{item.value}</div><div className="stat-note">{item.note}</div></div>)}</div>
    <ModelPricingEditor onSaved={() => setPricingRefresh(v => v + 1)} /><CodexCostNotice /><LocalCodexBilling summary={billing.local_codex} runs={runs} runsLoaded={runsLoaded} recordsOpen={nativeOpen} onRecordsOpen={setNativeOpen} threadsOpen={nativeThreadsOpen} onThreadsOpen={setNativeThreadsOpen} onOffset={setNativeOffset} loading={loading} />
    <div className="billing-accounting-note"><Clock3 size={15} /><span>待结算预算预留 <strong>{money(billing.pending_usd)}</strong> · {number(billing.pending_requests)} 个在途请求</span><span>{number(billing.estimated_records)} 条费用记录采用估算费率或估算用量</span><span>本页 {records.filter(isUnknownCharge).length} 条扣费未知 · 保守记录 {money(records.filter(isUnknownCharge).reduce((sum: number, record: Json) => sum + number(record.cost_usd), 0))}</span></div>
    <div className="billing-grid"><section className="panel"><SectionTitle title="按供应商归集" eyebrow="供应商费用分布" />{providers.length ? <div className="provider-costs">{providers.map((provider: Json, index: number) => <div className="provider-cost" key={provider.provider ?? provider.id ?? index}><div><span className={`provider-cost-dot color-${index % 3}`} /><strong><ReferenceText value={provider.provider ?? provider.name ?? provider.id} /></strong><span>{number(provider.requests)} 次请求</span><b>{money(provider.cost_usd)}</b></div><Progress value={number(provider.cost_usd) / Math.max(number(billing.cost_usd), 0.00001) * 100} /></div>)}</div> : <Empty icon={Coins} title="还没有模型用量" description="真实模型调用产生后，将按供应商显示成本分布。" />}</section><section className="billing-principles"><Coins size={25} strokeWidth={1.4} /><h3>用量有记录，费用有依据</h3><p>接口返回的词元用量作为计量来源；未确认费率会明确标记为估算；实际收费以供应商账单为准。</p><ul><li><Check size={14} />设定金额上限时按上限准入</li><li><Check size={14} />输入、输出与缓存分别计量</li><li><Check size={14} />记录预估成本与供应商来源</li></ul><small>删除研究后，已产生的计费记录仍会保留。费用以供应商最终账单为准。</small></section></div>
    <section className="panel"><SectionTitle title="供应商调用明细" action={<Pill>{billing.records_total ?? records.length} 条记录</Pill>} />{records.length ? <div className="table-scroll"><table className="billing-table"><thead><tr><th>时间 / 来源</th><th>归属任务</th><th>模型与角色</th><th>请求状态</th><th>输入</th><th>输出</th><th>缓存</th><th>费用</th></tr></thead><tbody>{records.map((record: Json, index: number) => <tr key={record.id ?? index}><td>{date(record.created_at, true)}{record.provenance ? <HumanMessage text={textOf(record.provenance)} className="small-text" /> : <small>{record.provider ?? '—'}</small>}</td><td className="billing-run-cell"><strong><ReferenceText value={billingRunLabel(record.run_id, runs, runsLoaded)} /></strong><small><Mode mode={runs.find(run => run.id === record.run_id)?.mode ?? (String(record.provenance).includes('demo') ? 'demo' : 'live')} /></small></td><td><strong>{record.model ?? '—'}</strong><small><ReferenceText value={ROLE[record.role]?.name ?? record.role ?? '—'} /></small></td><td><BillingRequestStatus record={record} /></td><td className="mono">{billingTokenLabel(record, 'input_tokens')}</td><td className="mono">{billingTokenLabel(record, 'output_tokens')}</td><td>{record.cache_hit ? <span className="good-text">本地命中</span> : billingTokenLabel(record, 'cached_tokens')}</td><td className="mono">{record.cost_usd === null ? '未定价' : money(record.cost_usd)}<BillingRateDetails record={record} /></td></tr>)}</tbody></table></div> : <Empty icon={Activity} title="每一次调用，都将留痕" description="这里会显示真实请求或明确标记的模拟调用记录。" />}{data && <RecordPagination label="供应商调用" offset={number(billing.records_offset, offset)} count={records.length} total={billing.records_total} loading={loading} onOffset={setOffset} />}</section>
  </>;
}

function HardwarePage({ hardware, refresh }: { hardware?: Json; refresh: () => void }) {
  const data = hardware ?? {}; const gpus: Json[] = Array.isArray(data.gpus) ? data.gpus : [];
  return <><div className="page-heading"><div><div className="eyebrow">匹配本机计算能力</div><h1>让研究规模，匹配真实算力</h1><p>在方案设计与每次实验之前，检查内存、显存和时间需求。</p></div><button className="btn" onClick={refresh}><RefreshCw size={15} />重新读取配置</button></div><div className="hardware-overview"><div className="hardware-hero"><span className="cpu-illustration"><Cpu size={55} strokeWidth={1} /></span><div><Pill icon={Activity}>本地主机</Pill><h2>{data.cpu_name ?? '等待读取硬件信息'}</h2><p>{data.platform ?? '—'} <span>·</span> Python {data.python ?? '—'}</p></div></div><div className="hardware-stat-row"><div><Cpu size={18} /><strong>{number(data.cpu_cores)}</strong><span>逻辑核心</span></div><div><Layers3 size={18} /><strong>{number(data.ram_total_gb).toFixed(1)}<small> GB</small></strong><span>系统内存</span></div><div><HardDrive size={18} /><strong>{number(data.disk_free_gb).toFixed(1)}<small> GB</small></strong><span>可用磁盘</span></div><div><Zap size={18} /><strong>{gpus.length}</strong><span>检测到的显卡</span></div></div></div>
    <div className="hardware-columns"><section className="panel"><SectionTitle title="加速设备" eyebrow="计算设备" />{gpus.length ? <div className="gpu-list">{gpus.map((gpu, index) => <div className="gpu" key={index}><div><IconBox icon={Zap} className="teal" /><h3><ReferenceText value={gpu.name} /><small>显卡 {index}</small></h3><Pill>已检测</Pill></div><div className="resource-meter"><span>可用显存</span><strong>{number(gpu.free_gb).toFixed(1)} / {number(gpu.total_gb).toFixed(1)} GB</strong></div><Progress value={number(gpu.free_gb) / Math.max(1, number(gpu.total_gb)) * 100} className="teal" /></div>)}</div> : <Empty icon={Cpu} title="当前使用处理器实验路径" description="未检测到可用的显卡数据。系统将以当前处理器与内存配置评估实验规模。" />}<div className="ram-panel"><div className="resource-meter"><span>系统可用内存</span><strong>{number(data.ram_available_gb).toFixed(1)} / {number(data.ram_total_gb).toFixed(1)} GB</strong></div><Progress value={number(data.ram_available_gb) / Math.max(1, number(data.ram_total_gb)) * 100} className="teal" /></div></section><section className="panel resource-policy"><SectionTitle title="每次执行，都有资源门禁" eyebrow="硬件准入检查" />{[{ icon: BrainCircuit, title: '模型与训练规模', text: '结合参数量、位宽、训练方式评估需求；超过可用资源的计划会被阻止。' }, { icon: Gauge, title: '保留运行余量', text: '按可用内存和显存保守评估，让方案优先选择可执行的实验。' }, { icon: Clock3, title: '有界执行与恢复', text: '限制单次实验时间，监测资源，超限终止并保存状态。' }].map(item => <div className="resource-policy-row" key={item.title}><IconBox icon={item.icon} /><div><h3><ReferenceText value={item.title} /></h3><p><ReferenceText value={item.text} /></p></div></div>)}<div className="inline-info"><CircleHelp size={16} />主机检查不会下载模型，也不会自动启动大型训练。</div></section></div><details className="panel raw-hardware"><summary>完整硬件检测结果<ChevronDown size={15} /></summary><pre><ReferenceText value={data} /></pre></details>
  </>;
}

function ProviderModelListResult({ result }: { result: Json }) {
  const ok = result.ok === true;
  return <div className={`test-result provider-model-list-result ${ok ? 'success' : 'failure'}`}>{ok ? <CircleCheck size={16} /> : <CircleAlert size={16} />}<div><strong>{ok ? '模型列表可读取，生成能力未验证' : '模型列表检查未通过，生成能力未验证'}</strong><p>检查型号（已保存配置）：<ReferenceText value={textOf(result.tested_model) || '未记录'} /> · <ReferenceText value={typeof result.latency_ms === 'number' && Number.isFinite(result.latency_ms) ? `模型列表请求耗时：${result.latency_ms.toFixed(0)} 毫秒` : '请求耗时未记录'} /></p>{result.message != null && <details><summary>原始诊断</summary><pre tabIndex={0}><ReferenceText value={result.message} /></pre></details>}</div></div>;
}

function SettingsPage({ revision, refresh, notify }: { revision: number; refresh: () => void; notify: (message: string) => void }) {
  const { data, loading, error: loadError } = useData<Settings>('/settings', revision);
  const [probeRevision, setProbeRevision] = useState(0);
  const codexStatus = useData<LocalCodexStatus>('/local-codex/status', revision + probeRevision);
  const [savedCodexProjectDir, setSavedCodexProjectDir] = useState<string | undefined>();
  const [settings, setSettings] = useState<Settings | null>(null); const [keys, setKeys] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false); const [testing, setTesting] = useState(''); const [results, setResults] = useState<Record<string, Json>>({}); const [error, setError] = useState('');
  const providerEditVersions = useRef<Record<string, number>>({});
  useEffect(() => { if (data) { setSavedCodexProjectDir(data.defaults.codex_project_dir ?? ''); setSettings(structuredClone(data)); setResults({}); for (const provider of data.providers) providerEditVersions.current[provider.id] = (providerEditVersions.current[provider.id] ?? 0) + 1; } }, [data]);
  function invalidateProviderResult(id: string) { providerEditVersions.current[id] = (providerEditVersions.current[id] ?? 0) + 1; setResults(previous => Object.fromEntries(Object.entries(previous).filter(([providerId]) => providerId !== id))); }
  function changeProvider(index: number, key: string, value: unknown) { const provider = settings?.providers[index]; if (provider) invalidateProviderResult(provider.id); setSettings(previous => previous ? { ...previous, providers: previous.providers.map((item, i) => i === index ? { ...item, [key]: value } : item) } : previous); }
  function changeKey(id: string, value: string) { invalidateProviderResult(id); setKeys(previous => ({ ...previous, [id]: value })); }
  async function save(event: FormEvent) { event.preventDefault(); if (!settings || busy || testing) return; setBusy(true); setError(''); try { const saved = await api<Settings>('/settings', { method: 'PUT', body: JSON.stringify({ ...settings, providers: settings.providers.map(provider => ({ ...provider, ...(keys[provider.id]?.trim() ? { api_key: keys[provider.id].trim() } : {}) })) }) }); setSavedCodexProjectDir(saved.defaults.codex_project_dir ?? ''); setKeys({}); refresh(); notify('智能体与执行设置已保存'); } catch (err) { setError((err as Error).message); } finally { setBusy(false); } }
  async function test(provider: Provider) {
    invalidateProviderResult(provider.id);
    const version = providerEditVersions.current[provider.id], savedModel = data?.providers.find(item => item.id === provider.id)?.model;
    setTesting(provider.id); setError('');
    try { const result = await post<Json>(`/providers/${encodeURIComponent(provider.id)}/test`); if (providerEditVersions.current[provider.id] === version) setResults(previous => ({ ...previous, [provider.id]: { ...result, tested_model: typeof result.model === 'string' && result.model ? result.model : savedModel } })); }
    catch (err) { if (providerEditVersions.current[provider.id] === version) setResults(previous => ({ ...previous, [provider.id]: { ok: false, tested_model: savedModel, message: (err as Error).message } })); }
    finally { setTesting(''); }
  }
  if (loading || !settings) return loadError ? <ErrorBox message={loadError} /> : <Loading />;
  return <form onSubmit={save}><div className="page-heading"><div><div className="eyebrow">统一执行框架，明确角色分工</div><h1>各司其职，在同一套系统里协作</h1><p>绑定模型、配置实际费率与执行上限。密钥只写入本地配置，界面不回显。</p></div><button type="submit" className="btn btn-primary" disabled={busy || !!testing}>{busy ? <LoaderCircle size={16} className="spin" /> : <Check size={16} />}保存设置</button></div><ErrorBox message={error || loadError} /><LocalCodexConnection configuredProjectDir={settings.defaults.codex_project_dir ?? ''} savedProjectDir={savedCodexProjectDir} status={codexStatus.data} loading={codexStatus.loading} error={codexStatus.error} onRefresh={() => setProbeRevision(value => value + 1)} /><section className="panel role-settings"><SectionTitle title="角色分工" eyebrow="智能体职责" /><div className="role-mapping">{Object.entries(ROLE).map(([role, info]) => <div key={role}><span className={`agent-avatar ${info.className}`}><info.icon size={22} /></span><h3><ReferenceText value={info.name} /></h3><p><ReferenceText value={info.purpose} /></p><select aria-label={`${info.name}供应商`} value={settings.roles[role] ?? ''} onChange={event => setSettings(previous => previous ? { ...previous, roles: { ...previous.roles, [role]: event.target.value } } : previous)}><option value="" disabled>选择供应商</option>{settings.providers.map(provider => <option key={provider.id} value={provider.id}>{provider.name} · {provider.model}</option>)}</select></div>)}</div></section>
    <div className="provider-settings-heading"><h2>模型连接与费用</h2><span>费率单位：美元 / 百万词元 · 初始值为未确认估算</span></div>{settings.providers.map((provider, index) => provider.protocol === 'local_codex' ? <SubscriptionProvider key={provider.id} provider={provider} change={(key, value) => changeProvider(index, key, value)} check={() => void test(provider)} checking={!!testing || busy} result={results[provider.id]} /> : <section className="panel provider-settings" key={provider.id}><div className="provider-settings-top"><IconBox icon={Network} /><div><h3><ReferenceText value={provider.name} /></h3><span>{provider.id}</span></div><span className={`agent-connected ${provider.configured ? 'configured' : ''}`}>{provider.configured ? '密钥已配置' : '待配置密钥'}</span><button className="btn small" type="button" disabled={!!testing || busy} onClick={() => void test(provider)}>{testing === provider.id ? <LoaderCircle size={14} className="spin" /> : <Zap size={14} />}检查模型列表（不生成）</button></div><p className="provider-model-list-note">检查使用已保存配置，只读取模型列表，不会调用生成。页面中的未保存修改不会参与本次检查。</p><div className="form-grid three"><Field label="供应商名称"><input value={provider.name} onChange={event => changeProvider(index, 'name', event.target.value)} /></Field><Field label="协议"><select value={provider.protocol} onChange={event => changeProvider(index, 'protocol', event.target.value)}><option value="responses">OpenAI 响应接口</option><option value="anthropic">Anthropic 消息接口</option><option value="chat">OpenAI 对话补全接口</option>{!['responses', 'anthropic', 'chat'].includes(provider.protocol) && <option value={provider.protocol}>{provider.protocol}</option>}</select></Field><Field label="模型标识"><input required value={provider.model} onChange={event => changeProvider(index, 'model', event.target.value)} /></Field></div><div className="form-grid"><Field label="接口地址"><input type="url" required value={provider.base_url} onChange={event => changeProvider(index, 'base_url', event.target.value)} /></Field><Field label="接口密钥" help={provider.configured ? '已保存密钥不会返回到浏览器。留空保留现有密钥。' : '输入密钥后点击保存设置。'}><input type="password" autoComplete="new-password" placeholder={provider.configured ? '已配置 · 留空保留' : '输入接口密钥'} value={keys[provider.id] ?? ''} onChange={event => changeKey(provider.id, event.target.value)} /></Field></div><div className="form-grid four">{[['input_price', '输入费率'], ['output_price', '输出费率'], ['cached_input_price', '缓存读取费率'], ['cache_write_price', '缓存写入费率']].map(([key, label]) => <Field key={key} label={label}><input aria-label={`${provider.name}${label}`} type="number" min="0" step="0.000001" placeholder="未确认" value={provider[key] ?? ''} onChange={event => changeProvider(index, key, event.target.value === '' ? null : number(event.target.value))} /></Field>)}</div><ProviderTimeoutControl providerName={provider.name} value={provider.timeout_seconds} onChange={value => changeProvider(index, 'timeout_seconds', value)} /><ProviderStreamingControl providerName={provider.name} protocol={provider.protocol} value={provider.responses_streaming} onChange={value => changeProvider(index, 'responses_streaming', value)} /><div className="provider-options"><label><input type="checkbox" checked={Boolean(provider.price_verified)} onChange={event => changeProvider(index, 'price_verified', event.target.checked)} /><span>已向供应商确认以上费率</span></label><label><input type="checkbox" checked={Boolean(provider.prompt_cache_enabled)} onChange={event => changeProvider(index, 'prompt_cache_enabled', event.target.checked)} /><span>启用供应商提示词缓存</span></label></div><details className="provider-limits"><summary>模型上下文与输出限制<ChevronDown size={14} /></summary><div className="form-grid"><Field label="上下文容量（词元）"><input type="number" min="4000" max="2000000" value={provider.context_window ?? 128000} onChange={event => changeProvider(index, 'context_window', number(event.target.value))} /></Field><Field label="输出上限（词元）"><input type="number" min="64" max="32000" value={provider.max_output_tokens ?? 4096} onChange={event => changeProvider(index, 'max_output_tokens', number(event.target.value))} /></Field></div></details>{results[provider.id] && <ProviderModelListResult result={results[provider.id]} />}</section>)}
    <section className="panel default-settings"><SectionTitle title="默认执行边界" eyebrow="默认执行配置" /><div className="form-grid three"><BudgetControl value={settings.defaults.budget_usd} onChange={value => setSettings(previous => previous ? { ...previous, defaults: { ...previous.defaults, budget_usd: value } } : previous)} />{[['max_calls', '最多模型调用次数', 3, 1000], ['max_trials', '每研究轮实验条数上限', 6, 100], ['context_tokens', '上下文词元预算', 4000, CONTEXT_TOKEN_MAX], ['experiment_timeout', '单次实验超时（秒）', 10, 7200]].map(([key, label, min, max]) => key === 'max_calls' ? <CallBudgetControl key={key} value={settings.defaults.max_calls} onChange={value => setSettings(previous => previous ? { ...previous, defaults: { ...previous.defaults, max_calls: value } } : previous)} /> : key === 'context_tokens' ? <ContextBudgetControl key={key} value={settings.defaults.context_tokens} onChange={value => setSettings(previous => previous ? { ...previous, defaults: { ...previous.defaults, context_tokens: value } } : previous)} /> : <Field key={key} label={String(label)}><input type="number" required min={min} max={max} step={key === 'budget_usd' ? '0.01' : '1'} value={settings.defaults[String(key)] ?? ''} onChange={event => setSettings(previous => previous ? { ...previous, defaults: { ...previous.defaults, [String(key)]: number(event.target.value) } } : previous)} /></Field>)}</div><LiteratureVenueControl value={settings.defaults.ccf_ab_only} onChange={value => setSettings(previous => previous ? { ...previous, defaults: { ...previous.defaults, ccf_ab_only: value } } : previous)} /><DiscussionControls value={settings.defaults} onChange={(key, value) => setSettings(previous => previous ? { ...previous, defaults: { ...previous.defaults, [key]: value } } : previous)} /><ResearchCycleControls value={settings.defaults} onChange={(key, value) => setSettings(previous => previous ? { ...previous, defaults: { ...previous.defaults, [key]: value } } : previous)} /><ComputeControls value={settings.defaults} onChange={(key, value) => setSettings(previous => previous ? { ...previous, defaults: { ...previous.defaults, [key]: value } } : previous)} /><ImplementationControls value={settings.defaults} onChange={(key, value) => setSettings(previous => previous ? { ...previous, defaults: { ...previous.defaults, [key]: value } } : previous)} /><div className="inline-info"><Layers3 size={18} /><HumanMessage text={settings.cache_note ?? '工具缓存跨角色复用；模型响应缓存按角色、供应商、模型与上下文隔离。远程模型的推理缓存由供应商控制，不能在不同模型间共享。'} /></div></section><div className="settings-save"><span>设置将用于后续任务与模型请求。</span><button className="btn btn-primary" disabled={busy || !!testing}>{busy ? <LoaderCircle size={16} className="spin" /> : <Check size={16} />}保存全部设置</button></div>
  </form>;
}
