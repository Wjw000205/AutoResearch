import { useEffect, useId, useState, type RefObject } from 'react';

type Edge = { id: string; d: string; complete: boolean; loop?: boolean };

/** Measure the rendered grid so wrapped rows keep their real execution order. */
export function StageConnections({ track, revision }: { track: RefObject<HTMLDivElement | null>; revision: string }) {
  const marker = useId().replace(/:/g, '');
  const [edges, setEdges] = useState<Edge[]>([]);
  useEffect(() => {
    const root = track.current;
    if (!root) return;
    let frame = 0;
    const measure = () => {
      const box = root.getBoundingClientRect();
      const nodes = Array.from(root.querySelectorAll<HTMLElement>('.stage-item')).map(item => {
        const circle = item.querySelector('.stage-circle')!.getBoundingClientRect();
        return { id: item.dataset.stage!, x: circle.left + circle.width / 2 - box.left,
          y: circle.top + circle.height / 2 - box.top, radius: circle.width / 2,
          bottom: item.getBoundingClientRect().bottom - box.top, complete: item.classList.contains('done') };
      });
      const measured: Edge[] = nodes.slice(1).map((to, i) => {
        const from = nodes[i];
        const start = from.x + from.radius + 2, end = to.x - to.radius - 6;
        const middle = (from.bottom + to.y - to.radius) / 2;
        const d = Math.abs(from.y - to.y) < 2
          ? `M ${start} ${from.y} H ${end}`
          : `M ${start} ${from.y} H ${box.width - 12} V ${middle} H 12 V ${to.y} H ${end}`;
        return { id: `${from.id}-${to.id}`, d, complete: from.complete };
      });
      const from = nodes.find(n => n.id === 'architecture_tuning'), to = nodes.find(n => n.id === 'experiments');
      if (from && to) {
        const lane = Math.min(from.y - from.radius, to.y - to.radius) - 15;
        const d = Math.abs(from.y - to.y) < 2
          ? `M ${from.x} ${from.y - from.radius - 3} V ${lane} H ${to.x} V ${to.y - to.radius - 7}`
          : `M ${from.x} ${from.y - from.radius - 3} V ${from.y - from.radius - 28} H 7 V ${lane} H ${to.x} V ${to.y - to.radius - 7}`;
        measured.push({ id: 'architecture_tuning-experiments', d, complete: false, loop: true });
      }
      setEdges(measured);
    };
    const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(measure); };
    const observer = new ResizeObserver(schedule);
    observer.observe(root);
    root.querySelectorAll('.stage-item').forEach(item => observer.observe(item));
    measure();
    let active = true;
    void document.fonts?.ready.then(() => { if (active) schedule(); });
    return () => { active = false; cancelAnimationFrame(frame); observer.disconnect(); };
  }, [track, revision]);
  return <svg className="stage-connections" aria-hidden="true"><defs>{['pending', 'complete', 'loop'].map(state =>
    <marker key={state} id={`${marker}-${state}`} markerWidth="5" markerHeight="5" refX="4" refY="2.5" orient="auto"><path d="M0,0 L5,2.5 L0,5" className={`stage-arrow ${state}`} /></marker>
  )}</defs>{edges.map(edge => <path key={edge.id} data-connection={edge.id} d={edge.d} className={`stage-edge ${edge.loop ? 'loop' : edge.complete ? 'complete' : 'pending'}`} markerEnd={`url(#${marker}-${edge.loop ? 'loop' : edge.complete ? 'complete' : 'pending'})`} />)}</svg>;
}
