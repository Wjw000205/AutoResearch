import { useLayoutEffect, useRef, useState } from 'react';

type NodePosition = { x: number; y: number; left: number; right: number; radius?: number };
type Layout = { width: number; height: number; nodes: Record<string, NodePosition>; ready: boolean };

export function useConversationLayout(roles: string) {
  const canvas = useRef<HTMLDivElement>(null);
  const [layout, setLayout] = useState<Layout>({ width: 1000, height: 900, ready: false, nodes: {
    harness: { x: 500, y: 100, left: 430, right: 570, radius: 26 },
    director: { x: 125, y: 270, left: 20, right: 230 },
    engineer: { x: 375, y: 270, left: 270, right: 480 },
    scout: { x: 625, y: 270, left: 520, right: 730 },
    codex: { x: 875, y: 270, left: 770, right: 980 },
  } });
  useLayoutEffect(() => {
    const element = canvas.current;
    if (!element) return;
    let frame = 0;
    const measure = () => {
      const bounds = element.getBoundingClientRect();
      if (!bounds.width || !bounds.height) return;
      const nodes: Layout['nodes'] = {};
      const round = (value: number) => Math.round(value * 100) / 100;
      element.querySelectorAll<HTMLElement>('.command-agent-card, .command-dispatcher').forEach(card => {
        const orbit = card.querySelector('.command-orbit')?.getBoundingClientRect();
        const role = card.dataset.role;
        if (!orbit || !role) return;
        const box = card.getBoundingClientRect();
        nodes[role] = { x: round(orbit.x + orbit.width / 2 - bounds.x),
          y: round(orbit.y + orbit.height / 2 - bounds.y),
          left: round(box.left - bounds.x), right: round(box.right - bounds.x), radius: round(orbit.width / 2) };
      });
      const next = { width: round(bounds.width), height: round(bounds.height), nodes, ready: true };
      setLayout(previous => JSON.stringify(previous) === JSON.stringify(next) ? previous : next);
    };
    measure();
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(measure);
    });
    observer.observe(element);
    element.querySelectorAll('.command-agent-card, .command-dispatcher').forEach(card => observer.observe(card));
    return () => { observer.disconnect(); cancelAnimationFrame(frame); };
  }, [roles]);
  return { canvas, layout };
}

export function conversationPath(nodes: Layout['nodes'], fromRole: string, toRole: string) {
  return conversationGeometry(nodes, fromRole, toRole)?.path ?? '';
}

export function conversationGeometry(nodes: Layout['nodes'], fromRole: string, toRole: string) {
  const from = nodes[fromRole], to = nodes[toRole];
  if (!from || !to) return null;
  if (fromRole === toRole) return { path: `M${from.x + 36},${from.y} C${from.x + 110},${from.y - 95} ${from.x - 110},${from.y - 95} ${from.x - 36},${from.y}`, label: { x: from.x, y: from.y - 70 } };
  const dx = to.x - from.x, dy = to.y - from.y;
  const length = Math.hypot(dx, dy) || 1;
  // Role-to-role exchanges travel above the row rather than through a third node.
  if (Math.abs(dy) < 12) {
    const startY = from.y - (from.radius ?? 37) - 7, endY = to.y - (to.radius ?? 37) - 9;
    const lane = Math.min(startY, endY) - (dx > 0 ? 80 : 110);
    return { path: `M${from.x},${startY} C${from.x},${lane} ${to.x},${lane} ${to.x},${endY}`, label: { x: (from.x + to.x) / 2, y: lane } };
  }
  const startX = from.x + dx / length * ((from.radius ?? 37) + 7), startY = from.y + dy / length * ((from.radius ?? 37) + 7);
  const endX = to.x - dx / length * ((to.radius ?? 37) + 9), endY = to.y - dy / length * ((to.radius ?? 37) + 9);
  const controlX = (startX + endX) / 2 - dy / length * 34, controlY = (startY + endY) / 2 + dx / length * 34;
  const t = .8;
  return { path: `M${startX},${startY} Q${controlX},${controlY} ${endX},${endY}`, label: {
    x: (1 - t) ** 2 * startX + 2 * (1 - t) * t * controlX + t ** 2 * endX,
    y: (1 - t) ** 2 * startY + 2 * (1 - t) * t * controlY + t ** 2 * endY,
  } };
}
