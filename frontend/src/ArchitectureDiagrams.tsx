import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Download, Network, RefreshCw, Workflow, ZoomIn, ZoomOut, Maximize2, X, LocateFixed, ChevronRight, ListTree, BookOpen } from 'lucide-react';
import { api, type Json } from './api';
import { createResourceSession, RESOURCE_READ_TIMEOUT_MS } from './RemoteResource';
import { useRemoteResource } from './useRemoteResource';
import { ScientificDiagramText } from './ScientificDiagramText';
import { ScientificDiagramSvg, parseScientificDiagramSvg, type ScientificSvgElement } from './ScientificDiagramSvg';
import './architecture-diagrams.css';

export interface DiagramAnchor { document_id: string; section_id: string; char_start: number; char_end: number }
export interface DiagramNode { id: string; label: string; lines: string[]; detail: string; kind: string; layer: number; order: number; sources: DiagramAnchor[] }
export interface DiagramEdge { source: string; target: string; label: string }
export interface AgentGraph { title: string; nodes: DiagramNode[]; edges: DiagramEdge[]; paragraphs: {text: string; sources: DiagramAnchor[]}[] }
export interface DiagramGeometry { id: string; x: number; y: number; width: number; height: number; node_ids: string[]; role?: 'module' | 'glyph' }
export interface DiagramPresentation { document_id: string; document_sha256: string; bundle_hash: string; kind: 'network' | 'flowchart'; svg: string; width: number; height: number; nodes: DiagramGeometry[]; provenance: 'saved_agent_bundle_presentation' }

/** Selection always resolves to the original, saved agent nodes. */
export function diagramSelectionNodes(graph: AgentGraph, ids: readonly string[]) {
  const selected = new Set(ids);
  return graph.nodes.filter(node => selected.has(node.id));
}

/** Locate a source node inside its most precise displayed glyph. */
export function diagramNodeGeometry(id: string, geometries: readonly DiagramGeometry[]) {
  return geometries.filter(item => item && Array.isArray(item.node_ids) && item.node_ids.includes(id) && [item.x,item.y,item.width,item.height].every(Number.isFinite)
    && item.width > 0 && item.height > 0).sort((a,b) => Number(a.role !== 'glyph') - Number(b.role !== 'glyph')
      || a.width * a.height - b.width * b.height)[0] ?? null;
}

/** Use a region's displayed heading, rather than its long accessibility title. */
export function diagramModuleLabel(tree: ScientificSvgElement, elementKey: string, fallback: string): string {
  const find = (node: ScientificSvgElement): ScientificSvgElement | null => node.key === elementKey ? node
    : node.children.reduce<ScientificSvgElement | null>((result,child) => result ?? (typeof child === 'string' ? null : find(child)),null);
  const text = (node: ScientificSvgElement): string => node.children.map(child => typeof child === 'string' ? child : text(child)).join('');
  const node = find(tree);
  const labels = (node?.children ?? []).filter((child): child is ScientificSvgElement => typeof child !== 'string' && child.tag === 'text')
    .map(child => ({text:text(child).trim(),size:Number(child.attributes['font-size'] ?? child.style?.['font-size'] ?? 0),
      weight:child.attributes['font-weight'] ?? child.style?.['font-weight'] ?? ''}))
    .filter(label=>label.text && !/^\([A-Za-z0-9]+\)$/u.test(label.text));
  const heading = labels.filter(label=>label.weight==='bold'||Number(label.weight)>=600).sort((a,b)=>b.size-a.size)[0] ?? labels[0];
  return heading?.text ?? fallback.split(/[\r\n]/u).find(line=>line.trim())?.trim() ?? '功能区';
}

/** Layout is presentation only; every module and connection comes from the agent receipt. */
export function diagramLayout(graph: AgentGraph, vertical = false) {
  const layers = new Map<number, DiagramNode[]>();
  for (const node of graph.nodes) layers.set(node.layer, [...(layers.get(node.layer) ?? []), node]);
  const maxLayer = Math.max(0, ...layers.keys()), maxBranches = Math.max(1, ...[...layers.values()].map(nodes => nodes.length));
  const width = vertical ? maxBranches * 280 + 80 : (maxLayer + 1) * 340 + 80;
  const height = vertical ? (maxLayer + 1) * 195 + 60 : maxBranches * 195 + 120;
  const positions = new Map<string, {x: number; y: number}>();
  for (const [layer, nodes] of layers) {
    const sorted = [...nodes].sort((a, b) => a.order - b.order);
    sorted.forEach((node, index) => positions.set(node.id, vertical
      ? { x: width / 2 + (index - (sorted.length - 1) / 2) * 280, y: 100 + layer * 195 }
      : { x: 160 + layer * 340, y: height / 2 + (index - (sorted.length - 1) / 2) * 195 }));
  }
  return { width, height, positions };
}

/** Skip connections and feedback use outer lanes rather than passing through layers. */
export function diagramEdgePath(graph: AgentGraph, edge: DiagramEdge, index: number, vertical = false) {
  const {width,positions}=diagramLayout(graph,vertical);
  const from=positions.get(edge.source),to=positions.get(edge.target);
  if(!from||!to)return null;
  const source=graph.nodes.find(node=>node.id===edge.source)!,target=graph.nodes.find(node=>node.id===edge.target)!;
  if(vertical&&from.y===to.y){
    const direction=to.x>from.x?1:-1;
    const x1=from.x+direction*(source.kind==='decision'?132:120),x2=to.x-direction*((target.kind==='decision'?132:120)+7);
    return{d:`M${x1} ${from.y} L${x2} ${to.y}`,labelX:(x1+x2)/2,labelY:from.y-9};
  }
  const direction=(vertical?to.y>from.y:to.x>from.x)?1:-1;
  const x1=from.x+(vertical?0:direction*(source.kind==='decision'?132:120));
  const y1=from.y+(vertical?direction*(source.kind==='decision'?82:66):0);
  const x2=to.x-(vertical?0:direction*((target.kind==='decision'?132:120)+7));
  const y2=to.y-(vertical?direction*((target.kind==='decision'?82:66)+7):0);
  if(Math.abs(target.layer-source.layer)>1||direction<0){
    const gap=14*direction;
    if(vertical){
      const lane=width-15-(index%3)*8;
      return{d:`M${x1} ${y1} C${x1} ${y1+gap} ${lane} ${y1+gap} ${lane} ${y1+gap} L${lane} ${y2-gap} C${lane} ${y2-gap} ${x2} ${y2-gap} ${x2} ${y2}`,labelX:lane-6,labelY:(y1+y2)/2};
    }
    const lane=20+(index%3)*15;
    return{d:`M${x1} ${y1} C${x1+gap} ${y1} ${x1+gap} ${lane} ${x1+gap} ${lane} L${x2-gap} ${lane} C${x2-gap} ${lane} ${x2-gap} ${y2} ${x2} ${y2}`,labelX:(x1+x2)/2,labelY:lane-5};
  }
  return{d:vertical?`M${x1} ${y1} C${x1} ${(y1+y2)/2} ${x2} ${(y1+y2)/2} ${x2} ${y2}`:`M${x1} ${y1} C${(x1+x2)/2} ${y1} ${(x1+x2)/2} ${y2} ${x2} ${y2}`,labelX:(x1+x2)/2,labelY:(y1+y2)/2-8};
}

function sourceLabel(anchor: DiagramAnchor, documents: Json[]) {
  const doc = documents.find(doc => doc.document_id === anchor.document_id);
  return `${doc?.title?.split('/').pop() ?? '架构文档'} · ${anchor.section_id} · ${anchor.char_start}–${anchor.char_end}`;
}

function SourcePreview({ runId, anchor, documents, onPageChange }: { runId: string; anchor: DiagramAnchor | null; documents: Json[]; onPageChange?:()=>void }) {
  const [page,setPage] = useState(0);
  const doc = documents.find(doc => doc.document_id === anchor?.document_id);
  const section = doc?.sections?.find((section: Json) => section.id === anchor?.section_id);
  const start = anchor ? anchor.char_start+page*4000 : 0, end = anchor ? Math.min(anchor.char_end,start+4000) : 0;
  const pages = anchor ? Math.max(1,Math.ceil((anchor.char_end-anchor.char_start)/4000)) : 1;
  const params = anchor && doc && section ? new URLSearchParams({ expected_sha256: doc.sha256, section_id: anchor.section_id,
    offset: String(start - section.start), limit: String(end-start) }) : null;
  const resource = useRemoteResource<Json>(params ? `/runs/${encodeURIComponent(runId)}/documents/${encodeURIComponent(doc!.document_id)}?${params}` : null, doc?.sha256 ?? '', 0, true);
  const read=resource.data;
  const matchingRead=anchor && read?.document_id===anchor.document_id && read?.sha256===doc?.sha256
    && read?.section_id===anchor.section_id && read?.char_start===start && read?.char_end===end;
  if (!anchor) return null;
  return <div className="diagram-source-preview"><strong>{sourceLabel(anchor, documents)}</strong>
    <p className="diagram-source-range">当前引用 {start}–{end} · 第 {page+1}/{pages} 段</p>
    {pages>1&&<div className="diagram-source-pagination"><button type="button" className="btn small" disabled={page===0||resource.loading} onClick={()=>{setPage(value=>value-1);onPageChange?.();}}>上一段</button>
      <button type="button" className="btn small" disabled={page+1>=pages||resource.loading} onClick={()=>{setPage(value=>value+1);onPageChange?.();}}>下一段</button></div>}
    {resource.loading && <p role="status">正在读取图示来源…</p>}{resource.error && <p role="alert">{resource.error}</p>}
    {matchingRead && <><small className="diagram-source-identity" title={`文档 ${read.document_id} · SHA256 ${read.sha256}`}>已核验版本 {read.sha256.slice(0,12)} · {read.section_id} · {read.char_start}–{read.char_end}</small>
      <pre data-source-document-id={read.document_id} data-source-sha256={read.sha256} data-source-section-id={read.section_id} data-source-char-start={read.char_start} data-source-char-end={read.char_end}>{read.text}</pre></>}
  </div>;
}

interface DiagramSize { width: number; height: number }
interface DiagramView extends DiagramSize { left: number; top: number }
interface DiagramViewportRequest { point: {x:number;y:number}; zoom:number; region?:DiagramSize; keepVisible?:boolean }
const bounded = (value: number, max: number) => Math.max(0, Math.min(value, Math.max(0, max)));

export function diagramFocusPoint(point: {x: number; y: number}, size: DiagramSize, viewport: DiagramSize, zoom: number) {
  return {left: bounded(point.x * zoom - viewport.width / 2, size.width * zoom - viewport.width),
    top: bounded(point.y * zoom - viewport.height / 2, size.height * zoom - viewport.height)};
}
export function diagramZoomViewport(size: DiagramSize, viewport: DiagramView, previousZoom: number, nextZoom: number) {
  return diagramFocusPoint({x: (viewport.left + viewport.width / 2) / previousZoom,
    y: (viewport.top + viewport.height / 2) / previousZoom}, size, viewport, nextZoom);
}
export function diagramViewport(size: DiagramSize, viewport: DiagramView, zoom: number) {
  const width = Math.min(size.width, viewport.width / zoom), height = Math.min(size.height, viewport.height / zoom);
  return {x: bounded(viewport.left / zoom, size.width - width), y: bounded(viewport.top / zoom, size.height - height), width, height};
}
export function diagramFitZoom(size: DiagramSize, viewport: DiagramSize) {
  return Math.max(.05, Math.min(1, (viewport.width - 48) / size.width, (viewport.height - 48) / size.height));
}
interface AgentDiagramProps {
  graph: AgentGraph; vertical?: boolean; runId: string; documents?: Json[];
  onExpand?: () => void; expanded?: boolean; architectureDocumentId?: string;
  documentSha256?: string; bundleHash?: string; presentation?: DiagramPresentation; historical?: boolean;
}

export function AgentDiagram({ graph, vertical = false, runId, documents = [], onExpand, expanded = false,
  architectureDocumentId, documentSha256, bundleHash, presentation, historical = false }: AgentDiagramProps) {
  const canvas = useRef<HTMLDivElement>(null), svg = useRef<SVGSVGElement>(null);
  const inspector = useRef<HTMLElement>(null), inspectorBody = useRef<HTMLDivElement>(null), lastFocus = useRef<{x:number;y:number}|null>(null);
  const directoryId = `diagram-directory-${useId().replace(/[^a-zA-Z0-9_-]/g,'')}`;
  const inspectorId = directoryId.replace('directory','inspector');
  const drag = useRef<{x: number; y: number; left: number; top: number} | null>(null);
  const [zoom, setZoom] = useState(1), [selectedIds, setSelectedIds] = useState<string[]>(graph.nodes[0] ? [graph.nodes[0].id] : []);
  const [directoryOpen, setDirectoryOpen] = useState(false), [wholeGraph, setWholeGraph] = useState(true);
  const [inspectorOpen,setInspectorOpen] = useState(false), [inspectorTab,setInspectorTab] = useState<'module'|'source'>('module');
  const [detailNodeId,setDetailNodeId] = useState(graph.nodes[0]?.id ?? '');
  const currentWholeGraph = useRef(true);
  const [anchor, setAnchor] = useState<DiagramAnchor | null>(null), [renderRetry, setRenderRetry] = useState(0);
  const [view, setView] = useState<DiagramView>({width: 600, height: 400, left: 0, top: 0}), [dragging, setDragging] = useState(false);
  const [pendingViewport, setPendingViewport] = useState<DiagramViewportRequest | null>(null);
  const latestViewport = useRef<DiagramViewportRequest | null>(null);
  const kind = vertical ? 'flowchart' : 'network', name = vertical ? '流程图' : '网络图';
  const params = architectureDocumentId && documentSha256
    ? new URLSearchParams({document_id: architectureDocumentId, expected_sha256: documentSha256}) : null;
  if (historical) params?.set('historical', 'true');
  const renderPath = !presentation && params ? `/runs/${encodeURIComponent(runId)}/architecture-diagrams/render/${kind}?${params}` : null;
  const resource = useRemoteResource<DiagramPresentation>(renderPath, `${bundleHash ?? ''}:${renderRetry}`, 0, true);
  const response = presentation ?? resource.data;
  const rendered = useMemo(() => {
    if (!response) return { document: null, error: '' };
    try {
      if (response.kind !== kind || response.provenance !== 'saved_agent_bundle_presentation'
          || architectureDocumentId && response.document_id !== architectureDocumentId
          || documentSha256 && response.document_sha256 !== documentSha256
          || bundleHash && response.bundle_hash !== bundleHash) {
        throw new Error('科研绘图返回版本与当前保存的架构不一致，请重新读取。');
      }
      if (!Array.isArray(response.nodes) || !Number.isFinite(response.width) || !Number.isFinite(response.height)
          || response.width <= 0 || response.height <= 0) throw new Error('科研绘图缺少有效的尺寸与模块定位信息。');
      const document = parseScientificDiagramSvg(response.svg);
      if (Math.abs(document.width - response.width) > .01 || Math.abs(document.height - response.height) > .01) {
        throw new Error('科研绘图尺寸与定位信息不一致。');
      }
      return { document, error: '' };
    } catch (error) {
      return { document: null, error: error instanceof Error ? error.message : '科研绘图无法安全显示。' };
    }
  }, [response, kind, architectureDocumentId, documentSha256, bundleHash]);
  const size = {width: rendered.document?.width ?? 600, height: rendered.document?.height ?? 400};
  const {width,height} = size;
  const geometries = useMemo<DiagramGeometry[]>(() => {
    if (!rendered.document) return [];
    const displayed = [...rendered.document.modules,...rendered.document.localGlyphs].map(item => ({
      id: item.id, role: item.role, x: item.x, y: item.y, width: item.width, height: item.height, node_ids: item.nodeIds,
    }));
    const mapped = new Set(displayed.flatMap(item => item.node_ids));
    // Prefer geometry from the exact safe SVG tree being displayed. The server
    // mapping can locate a source node absent from a local illustrative glyph.
    const fallback = response!.nodes.filter(item => item && Array.isArray(item.node_ids)).map(item => ({
      ...item, node_ids: item.node_ids.filter(id => !mapped.has(id)),
    })).filter(item => item.node_ids.length > 0);
    return [...displayed,...fallback];
  }, [rendered.document,response]);
  const selectedNodes = diagramSelectionNodes(graph, selectedIds), selected = selectedNodes[0]?.id ?? '';
  const activeDetail = selectedNodes.find(node=>node.id===detailNodeId) ?? selectedNodes[0];
  const activeSourceIndex=activeDetail?.sources.findIndex(source=>source.document_id===anchor?.document_id
    &&source.section_id===anchor?.section_id&&source.char_start===anchor?.char_start&&source.char_end===anchor?.char_end)??-1;
  const sortedNodes = [...graph.nodes].sort((a,b) => a.layer - b.layer || a.order - b.order);
  const modules = useMemo(() => (rendered.document?.modules ?? []).map(unit => ({...unit,
    originalNodes: diagramSelectionNodes(graph,unit.nodeIds),
    shortLabel: diagramModuleLabel(rendered.document!.tree,unit.elementKey,unit.label),
  })).filter(unit=>unit.originalNodes.length>0),[rendered.document,graph]);
  const overview = diagramViewport(size, view, zoom), error = resource.error || rendered.error;
  useLayoutEffect(()=>{
    if(inspectorTab==='source'&&inspector.current?.contains(document.activeElement)
        && document.activeElement?.closest('.diagram-node-detail')) {
      inspector.current.querySelector<HTMLButtonElement>('.diagram-inspector-tabs [aria-selected="true"]')?.focus({preventScroll:true});
    }
  },[inspectorTab]);
  function syncViewport() {
    const target = canvas.current;
    if (target) setView({width: target.clientWidth, height: target.clientHeight, left: target.scrollLeft, top: target.scrollTop});
  }
  function queueViewport(request: DiagramViewportRequest | null) {
    latestViewport.current = request; setPendingViewport(request);
  }
  useEffect(() => {
    const target = canvas.current; if (!target) return;
    let active = true;
    const observer = new ResizeObserver(() => {
      if (!active) return;
      syncViewport();
      if (wholeGraph && currentWholeGraph.current && rendered.document) {
        const next = Math.max(.05,Math.min(1,(target.clientWidth-48)/width));
        if (Math.abs(next-zoom)>.001) zoomTo(next,true);
      }
    }); observer.observe(target);
    return () => { active = false; observer.disconnect(); };
  }, [width,height,vertical,wholeGraph,zoom,rendered.document]);
  useLayoutEffect(() => {
    const target = canvas.current; if (!target) return;
    if (!rendered.document) { syncViewport(); return; }
    if (wholeGraph) fitWholeGraph();
    else queueViewport({point:{x:vertical?width/2:0,y:0},zoom});
  }, [width,height,vertical,rendered.document]);
  useLayoutEffect(() => {
    const target = canvas.current;
    if (!target || !pendingViewport || zoom !== pendingViewport.zoom) return;
    let active = true, frame = 0;
    const apply = () => {
      if (!active || canvas.current !== target || latestViewport.current !== pendingViewport) return;
      const svgBounds = svg.current?.getBoundingClientRect();
      // Updating the SVG style in React's commit can precede its actual layout.
      // Keep the request until the displayed SVG has the new dimensions; a
      // scroll write against the previous fit-to-screen extent is clamped to 0.
      if (svgBounds && svgBounds.width > 0 && svgBounds.height > 0
          && (Math.abs(svgBounds.width - width * zoom) > 1 || Math.abs(svgBounds.height - height * zoom) > 1)) {
        frame = requestAnimationFrame(apply);
        return;
      }
      if (pendingViewport.region) {
        // The reading canvas has now replaced overview's auto-height. Fit this
        // actual viewport, rather than the taller overview measured on click.
        const regionZoom = Math.max(.85,Math.min(1.5,(target.clientWidth-48)/pendingViewport.region.width,
          (target.clientHeight-48)/pendingViewport.region.height));
        if (Math.abs(regionZoom-zoom)>.001) {
          queueViewport({...pendingViewport,zoom:regionZoom}); setZoom(regionZoom);
          return;
        }
      }
      let offset = diagramFocusPoint(pendingViewport.point,size,
        {width: target.clientWidth, height: target.clientHeight},zoom);
      if (pendingViewport.keepVisible) {
        // A nearby drawer may cover the newly selected glyph. Preserve the
        // reader's viewport unless this particular point is actually covered.
        offset={left:target.scrollLeft,top:target.scrollTop};
        const box=target.getBoundingClientRect(),pane=inspector.current?.getBoundingClientRect();
        const x=pendingViewport.point.x*zoom-target.scrollLeft,y=pendingViewport.point.y*zoom-target.scrollTop;
        if(pane && pane.width>0 && pane.height>0 && pane.left<box.right && pane.right>box.left
            && pane.top<box.bottom && pane.bottom>box.top && x+box.left>=pane.left-20 && x+box.left<=pane.right
            && y+box.top>=pane.top-20 && y+box.top<=pane.bottom) {
          const freeWidth=pane.left-box.left;
          if(freeWidth>80) offset.left=bounded(pendingViewport.point.x*zoom-freeWidth/2,width*zoom-target.clientWidth);
          else if(pane.top-box.top>80) offset.top=bounded(pendingViewport.point.y*zoom-(pane.top-box.top)/2,height*zoom-target.clientHeight);
        }
      }
      target.scrollLeft = offset.left; target.scrollTop = offset.top;
      syncViewport();
      latestViewport.current = null;
      setPendingViewport(current => current === pendingViewport ? null : current);
    };
    frame = requestAnimationFrame(apply);
    return () => { active = false; cancelAnimationFrame(frame); };
  }, [pendingViewport,zoom,width,height]);
  function zoomTo(value: number, overall = false) {
    const target = canvas.current; if (!target || !rendered.document) return;
    const next = Math.max(.05, Math.min(2.5,value));
    const point = {x: (target.scrollLeft + target.clientWidth / 2) / zoom,
      y: (target.scrollTop + target.clientHeight / 2) / zoom};
    queueViewport({point,zoom: next});
    currentWholeGraph.current = overall; setWholeGraph(overall); setZoom(next);
  }
  function fitWholeGraph() {
    const target = canvas.current; if (!target || !rendered.document) return;
    // Overall mode follows the figure's aspect ratio. Its auto height must not
    // feed back into fitting, which would shrink again on every resize.
    zoomTo(Math.max(.05,Math.min(1,(target.clientWidth-48)/width)),true);
  }
  function focusPoint(point: {x:number;y:number;width?:number;height?:number}, fitRegion = false) {
    const target=canvas.current; if (!target || !rendered.document) return;
    lastFocus.current = point;
    const next = fitRegion ? Math.max(.85,Math.min(1.5,zoom)) : Math.max(.85,zoom);
    const region = fitRegion && point.width && point.height ? {width:point.width,height:point.height} : undefined;
    currentWholeGraph.current = false; setWholeGraph(false); queueViewport({point,zoom:next,region}); setZoom(next);
  }
  function readSelection() {
    const point = lastFocus.current ?? diagramNodeGeometry(selected,geometries);
    if (point) focusPoint(point);
  }
  function selectNodes(ids: string[]) {
    const nodes = diagramSelectionNodes(graph, ids);
    if (!nodes.length) return;
    setSelectedIds(nodes.map(node => node.id)); setDetailNodeId(nodes[0].id); setAnchor(null); setInspectorTab('module'); setInspectorOpen(true);
    if (inspectorBody.current) inspectorBody.current.scrollTop=0;
  }
  function openSource(source:DiagramAnchor) {
    setAnchor(source);setInspectorTab('source');setInspectorOpen(true);
    if(inspectorBody.current)inspectorBody.current.scrollTop=0;
  }
  function closeInspector() {
    setInspectorOpen(false);setAnchor(null);setInspectorTab('module');
    const focusTarget=canvas.current?.querySelector<SVGElement>('[role="button"][aria-pressed="true"]')
      ?? canvas.current?.closest('figure')?.querySelector<HTMLElement>('[data-diagram-detail-trigger]');
    focusTarget?.focus({preventScroll:true});
  }
  function showDetailNode(id:string) {
    if(!selectedNodes.some(node=>node.id===id))return;
    setDetailNodeId(id);setAnchor(null);setInspectorTab('module');
    if(inspectorBody.current)inspectorBody.current.scrollTop=0;
  }
  function selectDisplayed(ids:string[],unit:{x:number;y:number;width:number;height:number;role:string}) {
    const opening=!inspectorOpen;
    selectNodes(ids);lastFocus.current=unit;
    if(wholeGraph || opening&&expanded) focusPoint(unit,unit.role==='module');
    else if(opening) queueViewport({point:unit,zoom,keepVisible:true});
    else queueViewport(null);
  }
  function selectNode(id: string, locate = false) {
    selectNodes([id]);
    const target = canvas.current, point = diagramNodeGeometry(id, geometries);
    if (!locate || !target || !point || !rendered.document) return;
    focusPoint(point);
  }
  function download() {
    if (!svg.current) return;
    const exported=svg.current.cloneNode(true) as SVGSVGElement;
    exported.removeAttribute('style');
    // Selection/focus is a reader affordance, not part of the scientific export.
    exported.querySelectorAll('[data-diagram-selection]').forEach(element => element.remove());
    exported.querySelectorAll('[role="button"]').forEach(element => {
      element.removeAttribute('role'); element.removeAttribute('tabindex'); element.removeAttribute('aria-pressed');
    });
    const url = URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(exported)], { type: 'image/svg+xml;charset=utf-8' }));
    const link = document.createElement('a'); link.href = url;
    link.download = vertical ? 'architecture-flowchart.svg' : 'architecture-neural-network.svg';
    document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  return <figure className={`agent-diagram ${wholeGraph?'is-whole-graph':'is-reading'}`} onKeyDown={event=>{if(event.key==='Escape'&&inspectorOpen){event.preventDefault();event.stopPropagation();closeInspector();}}}><div className="diagram-heading"><h4>{vertical ? <Workflow size={18} /> : <Network size={18} />}{graph.title}</h4><span className="diagram-count">{graph.nodes.length} 个模块 · {graph.edges.length} 条连接</span></div>
    <div className="diagram-toolbar"><div className="diagram-controls">
      <button type="button" className="btn small" aria-label={`缩小${name}`} disabled={!rendered.document || zoom <= .05} onClick={() => zoomTo(zoom-.25)}><ZoomOut size={16} /></button>
      <output aria-label={`${name}缩放比例`}>{Math.round(zoom*100)}%</output>
      <button type="button" className="btn small" aria-label={`放大${name}`} disabled={!rendered.document || zoom >= 2.5} onClick={() => zoomTo(zoom+.25)}><ZoomIn size={16} /></button>
      <button type="button" className="btn small" disabled={!rendered.document} aria-pressed={wholeGraph} title="整体全图：查看完整结构，点击功能区放大阅读" onClick={fitWholeGraph}>适应全图</button>
      <button type="button" className="btn small" disabled={!rendered.document} aria-pressed={!wholeGraph} aria-label={`${name}局部阅读`} onClick={readSelection}><BookOpen size={15}/>局部阅读</button>
      <button type="button" className="btn small" disabled={!rendered.document} onClick={() => zoomTo(1)}>100%</button>
      <button type="button" className="btn small" aria-controls={directoryId} aria-expanded={directoryOpen} aria-label={`${directoryOpen?'收起':'展开'}${name}模块目录`} onClick={() => setDirectoryOpen(value=>!value)}><ListTree size={15}/>模块目录</button>
      {onExpand && <button type="button" data-diagram-expand="true" className="btn small" aria-label={expanded?`收起${name}完整查看`:`全屏查看${name}`} onClick={onExpand}>{expanded?<X size={15}/>:<Maximize2 size={15} />}{expanded?'收起查看':'展开查看'}</button>}
      <button type="button" className="btn small" disabled={!rendered.document} onClick={download}><Download size={15} />SVG</button>
    </div><label className="diagram-module-picker"><LocateFixed size={15}/><select aria-label={`定位${name}模块`} value={selected} onChange={event => selectNode(event.target.value,true)}>{sortedNodes.map((node,index) => <option key={node.id} value={node.id}>{index+1}. {node.label}</option>)}</select></label></div>
    {modules.length>0&&<nav className="diagram-feature-navigation" aria-label={`${name}功能区快捷导航`}>{modules.map((unit,index)=><button type="button" key={unit.id} data-diagram-module={unit.id} title={unit.shortLabel}
      aria-current={unit.nodeIds.some(id=>selectedIds.includes(id))?'true':undefined} onClick={()=>{selectNodes(unit.nodeIds);focusPoint(unit,true);}}><span>{String(index+1).padStart(2,'0')}</span><strong>{unit.shortLabel}</strong><small>{unit.originalNodes.length} 个原始模块</small></button>)}</nav>}
    <div className="diagram-reading-guide"><span>{wholeGraph?'整体全图':'局部阅读'} · {wholeGraph?'点击上方功能区或图中模块，放大阅读说明。':'拖动或滚动画布查看；模块下拉可精确定位。'}</span>
      {selectedNodes.length>0&&<button type="button" data-diagram-detail-trigger="true" aria-controls={inspectorId} aria-expanded={inspectorOpen} onClick={()=>inspectorOpen?closeInspector():setInspectorOpen(true)}>查看当前说明与来源 <ChevronRight size={14}/></button>}</div>
    <div className={`diagram-reader ${directoryOpen?'has-directory':''}`}><nav className="diagram-module-list" id={directoryId} hidden={!directoryOpen} aria-label={`${name}模块目录`}><strong>原始模块目录</strong>{sortedNodes.map((node,index) => <button type="button" key={node.id} aria-current={selectedIds.includes(node.id)?'true':undefined} onClick={() => selectNode(node.id,true)}><span>{String(index+1).padStart(2,'0')}</span><span>{node.label}</span><ChevronRight size={13}/></button>)}</nav><div className={`diagram-viewport-area ${inspectorOpen?'has-inspector':''}`}>
      <div className={`diagram-canvas ${wholeGraph?'is-overview':''} ${vertical ? 'diagram-flow' : 'diagram-network'} ${dragging?'is-dragging':''}`} data-diagram-mode={wholeGraph?'overview':'reading'} ref={canvas} tabIndex={0} aria-label={`${graph.title}画布，可拖动、滚动或使用方向键移动`} onScroll={syncViewport}
        onPointerDown={event => {if(event.button!==0||(event.target as Element).closest('[role="button"]'))return;queueViewport(null);drag.current={x:event.clientX,y:event.clientY,left:event.currentTarget.scrollLeft,top:event.currentTarget.scrollTop};event.currentTarget.setPointerCapture(event.pointerId);}}
        onPointerMove={event => {if(!drag.current)return;setDragging(true);event.currentTarget.scrollLeft=drag.current.left+drag.current.x-event.clientX;event.currentTarget.scrollTop=drag.current.top+drag.current.y-event.clientY;}}
        onPointerUp={event => {drag.current=null;setDragging(false);if(event.currentTarget.hasPointerCapture(event.pointerId))event.currentTarget.releasePointerCapture(event.pointerId);}} onPointerCancel={() => {drag.current=null;setDragging(false);}}
        onKeyDown={event => {if(event.target!==event.currentTarget)return;const delta:Record<string,number[]>={ArrowLeft:[-120,0],ArrowRight:[120,0],ArrowUp:[0,-120],ArrowDown:[0,120]};if(delta[event.key]){event.preventDefault();queueViewport(null);event.currentTarget.scrollBy(...delta[event.key] as [number,number]);}}}>
        {rendered.document ? <ScientificDiagramSvg document={rendered.document} svgRef={svg} style={{width:width*zoom,height:height*zoom}}
          onSelectNodeIds={selectDisplayed} highlightNodeIds={selectedIds} />
          : <div className="diagram-render-status">{error ? <><p role="alert">科研绘图无法显示：{error}</p><button type="button" className="btn small" onClick={() => setRenderRetry(value => value+1)}><RefreshCw size={15}/>重新读取图示</button></>
            : <p role="status">正在读取当前保存架构的科研图示…</p>}</div>}
      </div>
      {rendered.document && <div className="diagram-overview"><span>总览定位</span><svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="button" tabIndex={0} aria-label={`${name}总览，点击定位或使用方向键移动`} onClick={event => {
        const rect=event.currentTarget.getBoundingClientRect(),target=canvas.current;if(!target)return;
        queueViewport({point:{x:(event.clientX-rect.left)/rect.width*width,y:(event.clientY-rect.top)/rect.height*height},zoom});
      }} onKeyDown={event => {if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)||!canvas.current)return;event.preventDefault();queueViewport(null);canvas.current.scrollBy(event.key==='ArrowLeft'?-240:event.key==='ArrowRight'?240:0,event.key==='ArrowUp'?-240:event.key==='ArrowDown'?240:0);}}>
        <ScientificDiagramSvg document={rendered.document} interactive={false} />
        <rect className="diagram-overview-window" x={overview.x} y={overview.y} width={overview.width} height={overview.height}/>
      </svg><span className="diagram-navigation-hint">拖动空白移动<br/>点击功能区或模块查看说明</span></div>}
      <aside ref={inspector} id={inspectorId} className="diagram-inspector" hidden={!inspectorOpen} aria-label="当前模块说明与来源">
        <div className="diagram-inspector-heading"><strong>说明与来源</strong><button type="button" className="btn small" aria-label="关闭模块说明与来源" onClick={closeInspector}><X size={16}/></button></div>
        {selectedNodes.length>1&&<label className="diagram-inspector-picker"><span>功能区内 {selectedNodes.length} 个原始模块</span><select aria-label="查看功能区内原始模块说明" value={activeDetail?.id??''} onChange={event=>showDetailNode(event.target.value)}>{selectedNodes.map(node=><option key={node.id} value={node.id}>{node.label}</option>)}</select></label>}
        <div className="diagram-inspector-tabs" role="tablist" aria-label="模块说明与来源阅读"><button type="button" role="tab" aria-selected={inspectorTab==='module'} aria-controls={`${inspectorId}-body`} onClick={()=>{setInspectorTab('module');if(inspectorBody.current)inspectorBody.current.scrollTop=0;}}>模块说明</button><button type="button" role="tab" aria-selected={inspectorTab==='source'} aria-controls={`${inspectorId}-body`} disabled={!anchor&&!activeDetail?.sources.length} onClick={()=>{if(anchor){setInspectorTab('source');if(inspectorBody.current)inspectorBody.current.scrollTop=0;}else if(activeDetail?.sources[0])openSource(activeDetail.sources[0]);}}>来源原文</button></div>
        <div ref={inspectorBody} id={`${inspectorId}-body`} className="diagram-inspector-body" role="tabpanel">
        <section className="diagram-node-detail" hidden={inspectorTab!=='module'} aria-label="当前模块说明">
      {selectedNodes.map(selection => <article key={selection.id} hidden={selection.id!==activeDetail?.id} data-diagram-source-node={selection.id}><div className="diagram-node-detail-heading"><h5>{vertical?'2':'1'}.{sortedNodes.findIndex(node=>node.id===selection.id)+1} {selection.label}</h5><button type="button" className="btn small" onClick={() => {const point=diagramNodeGeometry(selection.id,geometries);if(point)focusPoint(point);}}><LocateFixed size={14}/>定位模块</button></div>
        {selection.lines.length>0&&<><h6>关键标注</h6><ul className="diagram-shape-labels">{selection.lines.map((line,index) => <li key={index}>{line}</li>)}</ul></>}
        <h6>方法与约束</h6><ScientificDiagramText text={selection.detail}/><details><summary>文档来源 · {selection.sources.length} 处</summary><div className="diagram-source-buttons">{selection.sources.map((source,index) => <button type="button" className="btn small" key={index} onClick={() => openSource(source)}>{sourceLabel(source,documents)}</button>)}</div></details>
      </article>)}
        </section>
        {inspectorOpen&&inspectorTab==='source'&&anchor&&<>
          {Boolean(activeDetail?.sources.length)&&<label className="diagram-inspector-picker diagram-inspector-source-picker"><span>当前模块的文档来源</span><select aria-label="查看当前模块的文档来源" value={activeSourceIndex} onChange={event=>{const source=activeDetail?.sources[Number(event.target.value)];if(source)openSource(source);}}>{activeSourceIndex<0&&<option value={-1}>当前解释的文档来源</option>}{activeDetail.sources.map((source,index)=><option key={index} value={index}>{sourceLabel(source,documents)}</option>)}</select></label>}
          <SourcePreview key={JSON.stringify([anchor.document_id,documents.find(doc=>doc.document_id===anchor.document_id)?.sha256,anchor.section_id,anchor.char_start,anchor.char_end])} runId={runId} anchor={anchor} documents={documents} onPageChange={()=>{if(inspectorBody.current)inspectorBody.current.scrollTop=0;}}/>
        </>}
        </div>
      </aside>
    </div></div>
    <figcaption className="diagram-paper-caption"><strong>图 {vertical?'2':'1'}.</strong> {graph.title}<small>按已保存的 agent 结构与来源绘制；局部图案为示意，实际层宽、形状与约束以原始标注及文档为准。</small></figcaption>
    <details className="diagram-paragraphs"><summary>整体解释与文档来源 · {graph.paragraphs.length} 节</summary>{graph.paragraphs.map((paragraph,index) => <article key={index}><span className="diagram-paragraph-number">{String(index+1).padStart(2,'0')}</span><div><h5>架构说明 {index+1}</h5><ScientificDiagramText text={paragraph.text}/><details><summary>本节解释的文档来源</summary>{paragraph.sources.map((source,i) => <button type="button" className="btn small" key={i} onClick={() => openSource(source)}>{sourceLabel(source,documents)}</button>)}</details></div></article>)}</details>
  </figure>;
}

export function DiagramWorkspace({ network, flowchart, runId, documents = [], architectureDocumentId, documentSha256, bundleHash, historical = false }:
  {network: AgentGraph; flowchart: AgentGraph; runId: string; documents?: Json[]; architectureDocumentId?: string; documentSha256?: string; bundleHash?: string; historical?: boolean}) {
  const [active,setActive]=useState<'network'|'flowchart'>('network'), [expanded,setExpanded]=useState(false);
  const workspace=useRef<HTMLDivElement>(null), expandTrigger=useRef<HTMLElement|null>(null), closeButton=useRef<HTMLButtonElement>(null);
  const uid=useId().replace(/[^a-zA-Z0-9_-]/g,'');
  useEffect(()=>{const panel=workspace.current?.querySelector<HTMLElement>('[role="tabpanel"]');if(panel)panel.scrollTop=0;},[active]);
  useEffect(() => {
    if(!expanded)return;
    closeButton.current?.focus();
    const previousOverflow=document.body.style.overflow; document.body.style.overflow='hidden';
    const keyboard=(event:KeyboardEvent) => {
      if(event.key==='Escape'){
        event.preventDefault();event.stopImmediatePropagation();
        const closeInspector=workspace.current?.querySelector<HTMLButtonElement>('.diagram-inspector:not([hidden]) [aria-label="关闭模块说明与来源"]');
        if(closeInspector){closeInspector.click();return;}
        setExpanded(false);
      }
      if(event.key==='Tab'){
        event.stopImmediatePropagation();
        const focusable=Array.from(workspace.current?.querySelectorAll<HTMLElement>('button:not(:disabled),select,[tabindex="0"],summary,a[href]')??[]).filter(element=>element.getClientRects().length>0 && element.tabIndex>=0);
        const first=focusable[0],last=focusable[focusable.length-1];
        if(!workspace.current?.contains(document.activeElement)){event.preventDefault();first?.focus();}
        else if(event.shiftKey&&document.activeElement===first){event.preventDefault();event.stopImmediatePropagation();last?.focus();}
        else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();event.stopImmediatePropagation();first?.focus();}
      }
    };
    document.addEventListener('keydown',keyboard,true);
    return()=>{document.body.style.overflow=previousOverflow;document.removeEventListener('keydown',keyboard,true);const restore=expandTrigger.current?.isConnected?expandTrigger.current:workspace.current?.querySelector<HTMLElement>('[data-diagram-expand]');restore?.focus();};
  },[expanded]);
  const tabs=['network','flowchart'] as const;
  return <div ref={workspace} className={`diagram-workspace ${expanded?'diagram-workspace-expanded':''}`} aria-label={expanded?'架构图完整查看':'架构图查看器'}>
    <div className="diagram-workspace-heading"><div role="tablist" aria-label="架构图类型">{tabs.map((type,index)=><button type="button" key={type} id={`${uid}-${type}-tab`} role="tab" aria-selected={active===type} aria-controls={`${uid}-panel`} tabIndex={active===type?0:-1} onClick={()=>setActive(type)} onKeyDown={event=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();const next=event.key==='Home'?'network':event.key==='End'?'flowchart':tabs[(index+1)%2];setActive(next);document.getElementById(`${uid}-${next}-tab`)?.focus();}}}>{type==='network'?<Network size={17}/>:<Workflow size={17}/>}<span>{type==='network'?'神经网络图':'流程图'}</span><small>{(type==='network'?network:flowchart).nodes.length} 模块</small></button>)}</div>{expanded&&<button type="button" ref={closeButton} className="btn" aria-label="退出架构图完整查看" onClick={()=>setExpanded(false)}><X size={18}/>收起</button>}</div>
    <div id={`${uid}-panel`} role="tabpanel" aria-labelledby={`${uid}-${active}-tab`}><AgentDiagram key={active} graph={active==='network'?network:flowchart} vertical={active==='flowchart'} runId={runId} documents={documents}
      architectureDocumentId={architectureDocumentId} documentSha256={documentSha256} bundleHash={bundleHash} historical={historical} expanded={expanded} onExpand={()=>{if(expanded){setExpanded(false);return;}expandTrigger.current=document.activeElement as HTMLElement;setExpanded(true);}}/></div>
  </div>;
}

const finishedDiagrams = new Map<string, Json>();
export function ArchitectureDiagrams({ runId, document: doc, demo = false, approved = true, historical = false }: { runId: string; document: Json; demo?: boolean; approved?: boolean; historical?: boolean }) {
  const [retry, setRetry] = useState(0);
  const scope = JSON.stringify([runId,doc.document_id,doc.sha256]), key = `${scope}:${retry}`;
  const [state, setState] = useState<{key:string;data:Json|null;error:string}>({key,data:finishedDiagrams.get(scope)??null,error:''});
  useEffect(() => {
    if (demo || !approved) return;
    const saved = finishedDiagrams.get(scope);
    setState({key,data:saved??null,error:''});
    if (saved) return;
    let queued = false;
    const params = new URLSearchParams({document_id:doc.document_id,expected_sha256:doc.sha256});
    if (historical) params.set('historical', 'true');
    const path = `/runs/${encodeURIComponent(runId)}/architecture-diagrams`;
    const session = createResourceSession<Json>({request:async signal => {
      if (!historical && !queued) { const result = await api<Json>(path,{method:'POST',body:JSON.stringify({document_id:doc.document_id,expected_sha256:doc.sha256,retry:retry>0}),signal}); queued=true; return result; }
      return api<Json>(`${path}?${params}`,{signal});
    }, success:data => {
      if (data.document_sha256!==doc.sha256 || data.architecture_document_id && data.architecture_document_id!==doc.document_id) {setState({key,data:null,error:'绘图返回版本与所选架构不一致，请重新读取。'});session.stop();return;}
      setState({key,data,error:''});
      if (data.status==='completed') {finishedDiagrams.set(scope,data);if(finishedDiagrams.size>16)finishedDiagrams.delete(finishedDiagrams.keys().next().value!);session.stop();}
      if (['failed','superseded'].includes(data.status)) session.stop();
    },failure:error=>setState(previous=>({...previous,key,error:error.message})),interval:historical?0:3000,timeoutMs:RESOURCE_READ_TIMEOUT_MS,visible:()=>document.visibilityState!=='hidden'});
    const visibility=()=>session.visibilityChanged();document.addEventListener('visibilitychange',visibility);void session.start();
    return()=>{session.stop();document.removeEventListener('visibilitychange',visibility);};
  },[scope,key,retry,runId,doc.document_id,doc.sha256,demo,approved,historical]);
  const data=state.key===key?state.data:finishedDiagrams.get(scope), error=state.key===key?state.error:'';
  return <section className="architecture-diagrams" aria-label="Agent 自动绘制的架构图"><h3><Network size={18}/>架构图与流程解释</h3>
    {demo?<p>演示任务未调用真实 agent 绘图。</p>:!approved?<p role="status">架构尚未通过评议；通过后工程师 agent 自动调用绘图工具生成架构图和流程图。</p>:<>
      <p className="diagram-provenance">{historical?'读取原架构版本已保存的图示':'由实验工程师 agent 根据当前架构文档自动绘制'} · 绑定版本 {doc.sha256.slice(0,12)}</p>
      {error&&<p role="alert">{error}</p>}
      {data?.status==='failed'?<div role="alert"><p>Agent 绘图或来源校验未完成：{data.error}</p>{!historical&&<button className="btn" onClick={()=>setRetry(value=>value+1)}><RefreshCw size={15}/>重新绘制</button>}</div>
        :data?.status==='completed'&&data.bundle?<><DiagramWorkspace key={scope} network={data.bundle.network} flowchart={data.bundle.flowchart} runId={runId} documents={data.documents}
          architectureDocumentId={doc.document_id} documentSha256={doc.sha256} bundleHash={data.bundle_hash} historical={historical}/>
          {data.bundle.limitations?.length>0&&<details><summary>图示范围与未定义内容</summary>{data.bundle.limitations.map((text:string,index:number)=><p key={index}>{text}</p>)}</details>}<p className="muted">图示已通过版本、连接和阅读来源校验；科学审批与实现验收见各自记录。</p></>
        :<p role="status">{historical?(data?'该版本尚未保存可展示的图示。':'正在读取原版本图示…'):data?.status==='generating'?'Agent 正在读取架构、绘制神经网络图和流程图，并编写段落解释…':'已安排自动绘图，研究运行时由 agent 继续；完成后两张图和解释自动显示。'}</p>}
      {error&&<button className="btn" onClick={()=>setRetry(value=>value+1)}>重新读取绘图状态</button>}
    </>}
  </section>;
}
