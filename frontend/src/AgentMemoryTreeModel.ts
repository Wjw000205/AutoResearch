export type MemoryNodeState = 'context' | 'retained' | 'retrieved' | 'reference' | 'omitted' | 'unavailable';
export interface MemoryTreeNode {
  id: string; parent_id: string | null; level: number; title: string; summary: string;
  state: MemoryNodeState; source_id?: string | null; detail_path?: string | null;
  metadata: Record<string, unknown>;
}
export interface MemoryTreeData {
  run_id: string; role: string; snapshot_id: string | null; operation_id?: string;
  created_at?: string; context_sha256?: string; provenance?: string; notice?: string;
  nodes: MemoryTreeNode[]; paths: { kind: string; nodes: string[]; order?: number }[];
  levels?: string[]; omitted_count: number;
}
export const MEMORY_ROLES = { director: '研究总监', engineer: '实验工程师', scout: '文献研究员' };
export const MEMORY_STATES: Record<MemoryNodeState, string> = {
  context: '上下文', retained: '输入中保留', retrieved: '已检索', reference: '仅引用', omitted: '未纳入输入', unavailable: '未取得内容',
};
export const TREE_CARD_WIDTH = 182;
export const TREE_CARD_HEIGHT = 132;

export function memoryTreePath(runId: string, role: string) {
  return `/runs/${encodeURIComponent(runId)}/memory/tree?role=${encodeURIComponent(role)}`;
}
export function memoryNodePath(runId: string, role: string, snapshot: string, node: string, offset = 0) {
  return `/runs/${encodeURIComponent(runId)}/memory/tree/${encodeURIComponent(snapshot)}/${encodeURIComponent(node)}?role=${encodeURIComponent(role)}&offset=${offset}&limit=12000`;
}

/** Parent spans keep subtrees together; siblings never overlap or cross. */
export function layoutMemoryTree(nodes: MemoryTreeNode[], expanded = new Set<string>()) {
  const byParent = new Map<string, MemoryTreeNode[]>();
  for (const node of nodes) if (node.parent_id) byParent.set(node.parent_id, [...(byParent.get(node.parent_id) ?? []), node]);
  const visible: (MemoryTreeNode & { x: number; y: number; hiddenChildren: number })[] = [];
  let cursor = 0;
  const seen = new Set<string>();
  function visit(node: MemoryTreeNode): number {
    if (seen.has(node.id)) return cursor;
    seen.add(node.id);
    const children = byParent.get(node.id) ?? [];
    const displayed = expanded.has(node.id) || node.level < 2 ? children : children.slice(0, 2);
    const positions = displayed.map(visit);
    const x = positions.length ? (positions[0] + positions[positions.length - 1]) / 2 : cursor++ * 206 + 112;
    visible.push({ ...node, x, y: node.level * 178 + 24, hiddenChildren: children.length - displayed.length });
    return x;
  }
  nodes.filter(node => !node.parent_id).forEach(visit);
  return { nodes: visible.sort((a, b) => a.level - b.level || a.x - b.x), width: Math.max(600, cursor * 206 + 24), height: 900 };
}

export function memoryEdgePath(parent: { x: number; y: number }, child: { x: number; y: number }) {
  const start = parent.y + TREE_CARD_HEIGHT, end = child.y, middle = (start + end) / 2;
  return `M${parent.x},${start} C${parent.x},${middle} ${child.x},${middle} ${child.x},${end}`;
}

export function activeMemoryEdges(tree: MemoryTreeData) {
  const edges = new Set<string>();
  for (const path of tree.paths) for (let i = 1; i < path.nodes.length; i++) edges.add(`${path.nodes[i - 1]}:${path.nodes[i]}`);
  return edges;
}
