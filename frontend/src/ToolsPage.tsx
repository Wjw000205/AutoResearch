import { useEffect, useMemo, useState } from 'react';
import { RefreshCw, Search, Wrench } from 'lucide-react';
import { humanizeMessage } from './localization';
import { useRemoteResource } from './useRemoteResource';
import type { RegisteredTool, ToolCatalog } from './ToolTypes';
import { categoryName, toolName, toolDescription } from './ToolLabels';
import { RecordPagination } from './RecordPagination';
import './tools.css';

export function filterRegisteredTools(tools: RegisteredTool[], query: string, role = '', category = '') {
  const search = query.trim().toLocaleLowerCase();
  return tools.filter(tool => (!role || tool.roles.includes(role)) && (!category || tool.category === category)
    && (!search || [tool.name, toolName(tool.name), toolDescription(tool.name), tool.description, tool.arguments_contract, tool.category, categoryName(tool.category), ...tool.roles,
      ...tool.roles.map(humanizeMessage)].join('\n').toLocaleLowerCase().includes(search)));
}

export function ToolCatalogCards({ tools }: { tools: RegisteredTool[] }) {
  return <div className="tool-catalog-grid">{tools.map(tool => <article className="tool-catalog-card" key={tool.name}>
    <header><Wrench size={17} aria-hidden="true" /><h3>{toolName(tool.name)}<small><code>{tool.name}</code></small></h3><span className="tool-category" title={tool.category}>{categoryName(tool.category)}</span></header>
    <p className="tool-description">{toolDescription(tool.name)}</p>
    <div className="tool-role-list" aria-label={`${tool.name}可调用角色`}>{tool.roles.map(role => <span className={`tool-role role-${role}`} key={role}>{humanizeMessage(role)}<code>{role}</code></span>)}</div>
    <details className="tool-contract"><summary>调用条件与参数说明</summary>
      <h4>角色与调用条件</h4>
      {tool.availability.length ? <ul className="tool-conditions">{tool.availability.map((access, index) => <li key={`${access.role}:${index}`}><strong>{humanizeMessage(access.role)}</strong><p>{access.condition}</p>{access.protocol && <small>协议：<code>{access.protocol}</code></small>}</li>)}</ul> : <p>未记录具体调用条件。</p>}
      <h4>参数约定</h4><p className="tool-scope-note">参数名和类型表达式保留原文，便于核对实际调用。</p><pre className="tool-readable-text">{tool.arguments_contract || '未提供参数说明。'}</pre>
      <p className="tool-scope-note">实际调用仍需满足当前任务的分派约定和运行时检查。</p>
    </details>
  </article>)}</div>;
}

export function ToolCatalogView({ catalog, query = '', role = '', category = '' }: {
  catalog: ToolCatalog; query?: string; role?: string; category?: string;
}) {
  const tools = filterRegisteredTools(catalog.tools, query, role, category);
  const [page, setPage] = useState(0);
  const pageSize = 12;
  const lastPage = Math.max(0, Math.ceil(tools.length / pageSize) - 1);
  const offset = Math.min(page, lastPage) * pageSize;
  const visibleTools = tools.slice(offset, offset + pageSize);
  useEffect(() => setPage(0), [query, role, category]);
  useEffect(() => setPage(current => Math.min(current, lastPage)), [lastPage]);
  const pagination = <RecordPagination label="工具目录" offset={offset} count={visibleTools.length} total={tools.length}
    limit={pageSize} onOffset={value => setPage(value / pageSize)} />;
  return <>
    {catalog.notice && <p className="tool-catalog-notice">{catalog.notice}</p>}
    <div className="tool-agent-grid" aria-label="智能体工具分配">{catalog.agents.map(agent => <section className={`tool-agent-card role-${agent.role}`} key={agent.role}>
      <h3>{humanizeMessage(agent.role)}<code>{agent.role}</code></h3><p>{agent.tools.length} 个已注册工具</p>
      <dl><div><dt>供应商</dt><dd>{agent.provider || '未配置'}</dd></div><div><dt>模型</dt><dd>{agent.model || '未配置'}</dd></div></dl>
    </section>)}</div>
    <div className="tool-results-heading"><h2>已注册工具</h2><span role="status">显示 {tools.length} / {catalog.total} 个工具</span><small>来源：运行时工具注册表 · <code>{catalog.source}</code></small></div>
    {tools.length ? <>{pagination}<ToolCatalogCards tools={visibleTools} />{pagination}</> : <div className="tool-empty"><Wrench size={26} aria-hidden="true" /><h3>暂无符合条件的工具</h3><p>调整名称、角色或分组筛选后重试。</p></div>}
  </>;
}

export function ToolsPage({ revision = 0 }: { revision?: string | number }) {
  const [refresh, setRefresh] = useState(0);
  const [query, setQuery] = useState('');
  const [role, setRole] = useState('');
  const [category, setCategory] = useState('');
  const remote = useRemoteResource<ToolCatalog>('/tools', `${revision}:${refresh}`);
  const categories = useMemo(() => [...new Set(remote.data?.tools.map(tool => tool.category) ?? [])].sort(), [remote.data]);
  return <div className="tools-page">
    <header className="page-heading"><div><div className="eyebrow"><Wrench size={16} aria-hidden="true" />智能体工具</div><h1>工具与智能体</h1><p>查看已有工具、可调用角色、参数约定和任务限制。各研究的实际调用记录可在“工具调用”中查看。</p></div><button type="button" className="btn" disabled={remote.loading} onClick={() => setRefresh(value => value + 1)}><RefreshCw size={15} aria-hidden="true" />刷新工具</button></header>
    <section className="panel tool-catalog-panel" aria-label="工具目录">
      <div className="tool-filters">
        <label className="tool-search"><span>搜索工具</span><div><Search size={16} aria-hidden="true" /><input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="工具名称、说明或角色" /></div></label>
        <label><span>可调用角色</span><select value={role} onChange={event => setRole(event.target.value)}><option value="">全部角色</option>{(remote.data?.agents ?? []).map(agent => <option value={agent.role} key={agent.role}>{humanizeMessage(agent.role)} · {agent.role}</option>)}</select></label>
        <label><span>工具分组</span><select value={category} onChange={event => setCategory(event.target.value)}><option value="">全部分组</option>{categories.map(value => <option key={value} value={value}>{categoryName(value)} · {value}</option>)}</select></label>
      </div>
      {remote.loading && <p className="tool-loading" role="status">正在读取工具目录…</p>}
      {remote.error && <div className="tool-error" role="alert"><p>工具目录读取失败：{remote.error}</p><button type="button" className="btn small" onClick={() => setRefresh(value => value + 1)}>重新读取</button></div>}
      {remote.data && <ToolCatalogView catalog={remote.data} query={query} role={role} category={category} />}
    </section>
  </div>;
}
