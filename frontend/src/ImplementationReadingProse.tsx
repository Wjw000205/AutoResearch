import MarkdownReport from './MarkdownReport';
import { MarkdownMath } from './MarkdownMath';
import { implementationReadingProse } from './implementationProseLayout';

export function ImplementationReadingProse({ text }: { text: string }) {
  return <>{implementationReadingProse(text).map((block, index) => block.type === 'markdown'
    ? <MarkdownReport key={index} text={block.text} preserveSourceText />
    : <section className="implementation-prose" key={index}>
      {block.title && <header className="implementation-metric-title"><code>{block.title.trim()}</code></header>}
      <dl className="implementation-prose-rows">{block.parts.filter(part => part.source.trim()).map((part, n) =>
        <div className={`implementation-prose-row is-${part.kind}`} key={n}>
          <dt>{part.label}</dt>
          <dd>{part.tex !== undefined
            ? <MarkdownMath tex={part.tex} source={part.source} display />
            : part.kind === 'text' ? part.source.trim()
              : <div className="implementation-formula-line" tabIndex={0} role="region" aria-label={`${part.label}，可横向滚动`}><code>{part.source.trim()}</code></div>}</dd>
        </div>)}</dl>
    </section>)}</>;
}
