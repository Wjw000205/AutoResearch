import { useEffect, useState } from 'react';

const superscripts: Record<string,string> = {'⁰':'0','¹':'1','²':'2','³':'3','⁴':'4','⁵':'5','⁶':'6','⁷':'7','⁸':'8','⁹':'9','⁺':'+','⁻':'-','⁽':'(','⁾':')'};
const subscripts: Record<string,string> = {'₀':'0','₁':'1','₂':'2','₃':'3','₄':'4','₅':'5','₆':'6','₇':'7','₈':'8','₉':'9','₊':'+','₋':'-','₍':'(','₎':')'};

/** Typesetting substitutions only. The original agent expression is retained alongside the rendering. */
export function diagramMathTex(text: string) {
  return text.replace(/[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁽⁾]+/g, value => `^{${Array.from(value).map(character=>superscripts[character]).join('')}}`)
    .replace(/[₀₁₂₃₄₅₆₇₈₉₊₋₍₎]+/g, value => `_{${Array.from(value).map(character=>subscripts[character]).join('')}}`)
    .replace(/([\u0370-\u03ff](?:\^\{[^}]*\})?)_([A-Za-z0-9]+)/g,'$1_{$2}')
    .replace(/Σ/g,'\\Sigma ').replace(/∑/g,'\\sum ').replace(/√/g,'\\sqrt ').replace(/≤/g,'\\leq ').replace(/≥/g,'\\geq ')
    .replace(/×/g,'\\times ').replace(/−/g,'-');
}

function mathParts(text: string) {
  const punctuation=text.match(/[，。；,.;]+\s*$/)?.[0] ?? '';
  return {expression:punctuation?text.slice(0,-punctuation.length):text,punctuation};
}
export async function renderScientificDiagramMath(text: string) {
  const {default:katex}=await import('katex');
  return katex.renderToString(diagramMathTex(mathParts(text).expression),{displayMode:true,throwOnError:true,trust:false,
    strict:'error',maxExpand:200,maxSize:10,macros:{},output:'htmlAndMathml'});
}

export function ScientificDiagramMath({ text }: {text: string}) {
  const [rendered,setRendered] = useState({text:'',html:''});
  useEffect(() => {
    let cancelled=false;
    // Only scientific detail views load the renderer; ordinary run navigation stays lightweight.
    void renderScientificDiagramMath(text).then(html=>{if(!cancelled)setRendered({text,html});})
      .catch(()=>{if(!cancelled)setRendered({text,html:''});});
    return()=>{cancelled=true;};
  },[text]);
  return <span className="scientific-diagram-math" data-math-source={text} title={text}>
    {rendered.text===text && rendered.html
      ? <><span dangerouslySetInnerHTML={{__html:rendered.html}} /><span className="diagram-equation-punctuation">{mathParts(text).punctuation}</span></>
      : <code>{text}</code>}
  </span>;
}
