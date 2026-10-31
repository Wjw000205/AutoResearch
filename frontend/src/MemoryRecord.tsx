import { useState } from 'react';
import { ChevronDown, LoaderCircle } from 'lucide-react';
import { type Json } from './api';
import { ReferenceNames, ReferenceText } from './ReferenceNames';
import { MemorySourceCitation } from './MemoryIndexPanel';
import { memoryDetailPath } from './RecordPagination';
import { useRemoteResource } from './useRemoteResource';

export function MemoryRecord({ memory, runId = '', revision = 0 }: { memory: Json; runId?: string; revision?: number }) {
  const [open, setOpen] = useState(false);
  const full = useRemoteResource<Json>(memory.id ? memoryDetailPath(memory.id, runId, open) : null, revision);
  return <><p><ReferenceText value={memory.text ?? memory.content} /></p><MemorySourceCitation memory={memory} />
    {memory.id && <details className="memory-record-detail" onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>查看完整记忆与元数据<ChevronDown size={11} /></summary>
      {open && (full.loading ? <p><LoaderCircle size={14} className="spin" />正在读取完整记忆…</p>
        : full.error ? <p role="alert">完整记忆读取失败：<ReferenceText value={full.error} /></p>
          : full.data && <ReferenceNames records={full.data}><div className="memory-full-text"><ReferenceText value={full.data.text ?? full.data.content} /></div>
            <MemorySourceCitation memory={full.data} />{full.data.metadata && <pre><ReferenceText value={full.data.metadata} /></pre>}</ReferenceNames>)}
    </details>}
  </>;
}
