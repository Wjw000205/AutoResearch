import { useEffect, useState } from 'react';
import { diagramMathTex } from './ScientificDiagramMath';
import './markdown-math.css';

export async function renderMarkdownMath(tex: string, display: boolean) {
  const {default: katex} = await import('katex');
  return katex.renderToString(diagramMathTex(tex), {displayMode: display, throwOnError: true, trust: false,
    strict: 'error', maxExpand: 200, maxSize: 10, macros: {}, output: 'htmlAndMathml'});
}

export function MarkdownMath({tex, source, display}: {tex: string; source: string; display: boolean}) {
  const [rendered, setRendered] = useState({tex: '', display: false, html: ''});
  useEffect(() => {
    let cancelled = false;
    void renderMarkdownMath(tex, display).then(html => {
      if (!cancelled) setRendered({tex, display, html});
    }).catch(() => {if (!cancelled) setRendered({tex, display, html: ''});});
    return () => {cancelled = true;};
  }, [tex, display]);
  return <span className={display ? 'markdown-math-block' : 'markdown-math-inline'} role="math"
    data-math-source={source} title={source} aria-label={source}>
    {rendered.tex === tex && rendered.display === display && rendered.html
      ? <span dangerouslySetInnerHTML={{__html: rendered.html}} />
      : <code className="markdown-math-fallback">{source}</code>}
  </span>;
}
