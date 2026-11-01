import ReactMarkdown from 'react-markdown';
import { Fragment } from 'react';
import remarkGfm from 'remark-gfm';
import { ExternalLink } from 'lucide-react';
import { safeUrl } from './api';
import { ReadableContent } from './ReferenceNames';
import remarkMarkdownMath from './MarkdownMathSyntax';
import { MarkdownMath } from './MarkdownMath';

export default function MarkdownReport({ text, preserveSourceText = false }: { text: string; preserveSourceText?: boolean }) {
  // Saved documents retain their exact reference text even inside a names provider.
  const Content = preserveSourceText ? Fragment : ReadableContent;
  return <article className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm, remarkMarkdownMath]} skipHtml components={{
    a: ({ href, children }) => safeUrl(href) ? <a href={safeUrl(href)} target="_blank" rel="noreferrer"><Content>{children}</Content><ExternalLink size={11} /></a> : <span><Content>{children}</Content></span>,
    p: ({ children }) => <p><Content>{children}</Content></p>,
    li: ({ children }) => <li><Content>{children}</Content></li>,
    code: ({ children, className }) => <code className={className}><Content>{children}</Content></code>,
    h1: ({ children }) => <h1><Content>{children}</Content></h1>,
    h2: ({ children }) => <h2><Content>{children}</Content></h2>,
    h3: ({ children }) => <h3><Content>{children}</Content></h3>,
    h4: ({ children }) => <h4><Content>{children}</Content></h4>,
    h5: ({ children }) => <h5><Content>{children}</Content></h5>,
    h6: ({ children }) => <h6><Content>{children}</Content></h6>,
    td: ({ children }) => <td><Content>{children}</Content></td>,
    th: ({ children }) => <th><Content>{children}</Content></th>,
    table: ({ children }) => <div className="table-scroll"><table>{children}</table></div>,
    span: ({node, children}) => {
      const properties = node?.properties;
      return typeof properties?.dataMathTex === 'string' && typeof properties?.dataMathSource === 'string'
        ? <MarkdownMath tex={properties.dataMathTex} source={properties.dataMathSource} display={properties.dataMarkdownMath === 'block'} />
        : <span><Content>{children}</Content></span>;
    },
  }}>{text}</ReactMarkdown></article>;
}
