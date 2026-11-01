import { ReferenceText } from './ReferenceNames';
import { useId, useState } from 'react';
import { ChevronDown } from 'lucide-react';

export function ResearchObjective({ objective }: { objective: string }) {
  const [expanded, setExpanded] = useState(false);
  const contentId = useId();
  const long = objective.length > 180 || objective.split('\n').length > 3;
  if (!objective.trim()) return null;
  return <section className="research-objective" aria-label="研究目标与要求">
    <div className="research-objective-heading"><h2>研究目标与要求</h2>{long && <button type="button" aria-expanded={expanded} aria-controls={contentId} onClick={() => setExpanded(value => !value)}>{expanded ? '收起完整要求' : '展开完整要求'}<ChevronDown size={15} className={expanded ? 'rotated' : ''} /></button>}</div>
    <p id={contentId} className={long && !expanded ? 'objective-preview' : ''}><ReferenceText value={objective} /></p>
  </section>;
}
