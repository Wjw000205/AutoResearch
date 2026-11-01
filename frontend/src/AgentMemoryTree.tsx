import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { BrainCircuit, ChevronDown, GitBranch, Maximize2, Minus, Pause, Play, Plus, RotateCcw, X } from 'lucide-react';
import { date } from './api';
import { useRemoteResource } from './useRemoteResource';
import { MemoryTextContent } from './MemoryExplorer';
import type { MemoryTextPage } from './MemoryExplorerTypes';
import { activeMemoryEdges, layoutMemoryTree, memoryEdgePath, memoryNodePath, memoryTreePath, MEMORY_ROLES, MEMORY_STATES, TREE_CARD_HEIGHT, TREE_CARD_WIDTH, type MemoryTreeData, type MemoryTreeNode } from './AgentMemoryTreeModel';
import './agent-memory-tree.css';

function MemoryNodeDetail({ runId, tree, node, onClose }: { runId: string; tree: MemoryTreeData; node: MemoryTreeNode; onClose: () => void }) {
  const [offset, setOffset] = useState(0);
  const resource = useRemoteResource<MemoryTextPage>(tree.snapshot_id ? memoryNodePath(runId, tree.role, tree.snapshot_id, node.id, offset) : null);
  let readable = '';
  if (resource.data && !offset && !resource.data.has_more) {
    try {
      const record = JSON.parse(resource.data.text);
      readable = ['text', 'content', 'quote'].map(key => record?.[key]).find(value => typeof value === 'string') ?? '';
    } catch { /* Plain text and partial JSON are already readable in the original view. */ }
  }
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => { close.current?.focus({ preventScroll: true }); }, []);
  return <section className="agent-memory-detail" aria-label="记忆节点详情" onKeyDown={event => { if (event.key === 'Escape') onClose(); }}>
    <header><div><span>第 {node.level} 层 · {MEMORY_STATES[node.state]}</span><h4>{node.title}</h4></div><button ref={close} type="button" className="icon-btn" aria-label="关闭记忆详情" onClick={onClose}><X size={16} /></button></header>
    <p>{node.summary}</p>
    <dl>{node.source_id && <><dt>来源标识</dt><dd>{node.source_id}</dd></>}<dt>上下文快照</dt><dd>{tree.snapshot_id}</dd>{node.detail_path && <><dt>保存路径</dt><dd>{node.detail_path}</dd></>}</dl>
    {Object.keys(node.metadata).length > 0 && <details><summary>来源与检索参数</summary><pre>{JSON.stringify(node.metadata, null, 2)}</pre></details>}
    {resource.loading && <p role="status">正在读取已保存内容…</p>}
    {resource.error && <p className="memory-explorer-error" role="alert">节点读取失败：{resource.error}</p>}
    {readable && <pre className="agent-memory-readable">{readable}</pre>}
    {resource.data && (readable ? <details><summary>完整结构记录与来源校验</summary><MemoryTextContent page={resource.data} offset={offset} onOffset={setOffset} /></details> : <MemoryTextContent page={resource.data} offset={offset} onOffset={setOffset} />)}
  </section>;
}

export function MemoryTreeCanvas({ tree, onSelect, playing, replay = 0 }: { tree: MemoryTreeData; onSelect: (node: MemoryTreeNode) => void; playing: boolean; replay?: number }) {
  const [expanded, setExpanded] = useState(new Set<string>());
  const [zoom, setZoom] = useState<number | null>(null);
  const [width, setWidth] = useState(800);
  const viewport = useRef<HTMLDivElement>(null);
  const graph = useMemo(() => layoutMemoryTree(tree.nodes, expanded), [tree.nodes, expanded]);
  const edges = useMemo(() => activeMemoryEdges(tree), [tree]);
  const positions = new Map(graph.nodes.map(node => [node.id, node]));
  const fit = Math.min(1, Math.max(.4, (width - 30) / graph.width));
  const scale = zoom ?? fit;
  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const observer = new ResizeObserver(entries => { if (entries[0].contentRect.width) setWidth(entries[0].contentRect.width); });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return <><div className="agent-memory-canvas-tools"><span>逐层展开 · 点击节点读详情</span><div><button type="button" aria-label="缩小记忆树" onClick={() => setZoom(Math.max(.35, scale - .1))}><Minus size={13} /></button><span>{Math.round(scale * 100)}%</span><button type="button" aria-label="放大记忆树" onClick={() => setZoom(Math.min(1.6, scale + .1))}><Plus size={13} /></button><button type="button" aria-label="适应记忆树画布" onClick={() => { setZoom(null); viewport.current?.scrollTo({ left: 0, top: 0 }); }}><Maximize2 size={14} /></button></div></div>
    <div ref={viewport} className="agent-memory-viewport" tabIndex={0} aria-label="可滚动的四层记忆金字塔">
      <div className="agent-memory-scaled" style={{ width: Math.max(width - 8, graph.width * scale), height: graph.height * scale }}>
        <div className="agent-memory-graph" style={{ width: graph.width, height: graph.height, transform: `scale(${scale})`, left: Math.max(0, (width - graph.width * scale) / 2), '--node-text-scale': Math.max(1, Math.min(2, 1 / scale)) } as CSSProperties}>
          <svg width={graph.width} height={graph.height} aria-hidden="true" className="agent-memory-edges">
            {graph.nodes.map(node => { const parent = node.parent_id && positions.get(node.parent_id); if (!parent) return null;
              const active = edges.has(`${parent.id}:${node.id}`), path = memoryEdgePath(parent, node);
              return <g key={node.id}><path className={`memory-edge ${active ? 'is-retrieved' : ''}`} d={path} />{active && <path key={`${tree.snapshot_id}:${replay}`} className={`memory-edge-flow ${playing ? 'is-playing' : ''}`} style={{ '--flow-delay': `${node.level * .22}s` } as CSSProperties} d={path} />}</g>;
            })}
          </svg>
          {graph.nodes.map(node => <div key={node.id} className={`agent-memory-node-wrap level-${node.level}`} style={{ left: node.x - TREE_CARD_WIDTH / 2, top: node.y, width: TREE_CARD_WIDTH, height: TREE_CARD_HEIGHT }}>
            <button type="button" className={`agent-memory-node memory-state-${node.state}`} onClick={() => onSelect(node)} aria-label={`${node.title}，${MEMORY_STATES[node.state]}，查看详情`} title={node.summary}>
              <span className="agent-memory-node-label">{node.level === 0 ? <BrainCircuit size={13} /> : <GitBranch size={11} />}<span>{node.level === 0 ? 'AGENT' : `L${node.level}`}</span><i />{MEMORY_STATES[node.state]}</span>
              <strong>{node.title}</strong><small>{node.summary || (node.level === 0 ? '当前角色的独立记忆' : '点击查看来源与保存内容')}</small>
            </button>
            {(node.hiddenChildren > 0 || expanded.has(node.id)) && <button type="button" className="agent-memory-expand" onClick={() => setExpanded(previous => { const next = new Set(previous); if (next.has(node.id)) next.delete(node.id); else next.add(node.id); return next; })} aria-expanded={expanded.has(node.id)}>{expanded.has(node.id) ? '收起分支' : `还有 ${node.hiddenChildren} 条`}<ChevronDown size={10} /></button>}
          </div>)}
        </div>
      </div>
    </div></>;
}

type TreeProps = { runId: string; role: string; onRoleChange: (role: string) => void; onCollapse?: () => void };

function AgentMemoryTreeSession({ runId, role, onRoleChange, onCollapse }: TreeProps) {
  const [following, setFollowing] = useState(true);
  const [displayed, setDisplayed] = useState<MemoryTreeData | null>(null);
  const [selected, setSelected] = useState<MemoryTreeNode | null>(null);
  const [replay, setReplay] = useState(0);
  const [motion, setMotion] = useState(true);
  const resource = useRemoteResource<MemoryTreeData>(memoryTreePath(runId, role), 0, 5000);
  useEffect(() => {
    if (resource.data && following && !selected) setDisplayed(previous => previous?.snapshot_id === resource.data?.snapshot_id ? previous : resource.data);
  }, [resource.data, following, selected]);
  // Snapshot selection is separate from polling, so reading never jumps to new content.
  const tree = displayed ?? resource.data;
  const newer = resource.data?.snapshot_id && tree?.snapshot_id !== resource.data.snapshot_id;
  const selectedButton = useRef<HTMLElement | null>(null);
  function closeDetail() { setSelected(null); selectedButton.current?.focus({ preventScroll: true }); }
  return <section className="agent-memory-panel" aria-label="Agent 可视化记忆树">
    <header className="agent-memory-heading"><div><span className="agent-memory-eyebrow">MEMORY MAP</span><h3><BrainCircuit size={17} />四层记忆树</h3></div><span className={`agent-memory-live ${following && !selected ? 'is-live' : ''}`}><i />{following && !selected ? '自动跟随' : '当前快照'}</span>{onCollapse && <button type="button" className="icon-btn" aria-label="收起记忆树，完整宽度查看输出" onClick={onCollapse}><X size={15} /></button>}</header>
    <div className="agent-memory-role-tabs" aria-label="选择记忆树 Agent">{Object.entries(MEMORY_ROLES).map(([value, name]) => <button type="button" key={value} aria-pressed={value === role} onClick={() => onRoleChange(value)}>{name}</button>)}</div>
    <div className="agent-memory-toolbar"><span>{tree?.created_at ? date(tree.created_at, true) : '等待上下文记录'}</span><div><button type="button" aria-pressed={!following} onClick={() => setFollowing(value => !value)}>{following ? <Pause size={12} /> : <Play size={12} />}{following ? '暂停跟随' : '继续跟随'}</button><button type="button" onClick={() => { setReplay(value => value + 1); setMotion(true); }}><RotateCcw size={12} />路径回放</button><button type="button" aria-label={motion ? '暂停记忆路径光效' : '开启记忆路径光效'} aria-pressed={motion} onClick={() => setMotion(value => !value)}>{motion ? <Pause size={12} /> : <Play size={12} />}</button></div></div>
    {newer && <button type="button" className="agent-memory-new" onClick={() => { setSelected(null); setDisplayed(resource.data); setFollowing(true); }}>有新的上下文 · 跟随最新路径</button>}
    {resource.error && <p role="alert" className="memory-explorer-error">记忆树读取失败：{resource.error}{tree && '。下方保留上次成功读取的快照。'}</p>}
    {!tree && resource.loading && <p role="status" className="agent-memory-empty">正在读取该 Agent 的记忆路径…</p>}
    {tree && !tree.nodes.length && <div className="agent-memory-empty"><BrainCircuit size={28} /><p>该 Agent 尚无已保存的上下文。</p><small>首次准备模型请求后，记忆路径会显示在这里。</small></div>}
    {tree && tree.nodes.length > 0 && <><div className="agent-memory-body"><MemoryTreeCanvas tree={tree} replay={replay} playing={motion} onSelect={node => { selectedButton.current = document.activeElement as HTMLElement; setSelected(node); }} />{selected && <MemoryNodeDetail key={`${tree.snapshot_id}:${selected.id}`} runId={runId} tree={tree} node={selected} onClose={closeDetail} />}</div>
      <div className="agent-memory-legend">{(['retrieved', 'retained', 'reference', 'omitted'] as const).map(state => <span key={state}><i className={`legend-${state}`} />{MEMORY_STATES[state]}</span>)}</div>
      <p className="agent-memory-footnote">{tree.provenance === 'legacy_snapshot' ? '历史输入快照重建' : '来自已保存上下文'} · {tree.paths.length} 条记录路径{tree.omitted_count > 0 ? ` · 另有 ${tree.omitted_count} 条未在概览展示` : ''}。{tree.notice}</p></>}
  </section>;
}

export function AgentMemoryTree(props: TreeProps) {
  return <AgentMemoryTreeSession key={`${props.runId}:${props.role}`} {...props} />;
}
