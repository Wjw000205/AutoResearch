import type { Literal, Parent, Root } from 'mdast';
import type { CompileContext, Extension as MdastExtension } from 'mdast-util-from-markdown';
import type { Construct, Effects, Extension, State, Token, TokenizeContext, Tokenizer } from 'micromark-util-types';
import type { Processor } from 'unified';

interface MarkdownMathNode extends Literal {
  type: 'markdownMath';
  display: boolean;
}

declare module 'mdast' {
  interface RootContentMap { markdownMath: MarkdownMathNode }
  interface PhrasingContentMap { markdownMath: MarkdownMathNode }
}
declare module 'micromark-util-types' {
  interface TokenTypeMap {
    markdownMath: 'markdownMath';
    markdownMathMarker: 'markdownMathMarker';
    markdownMathBody: 'markdownMathBody';
    markdownMathLineEnding: 'markdownMathLineEnding';
  }
  interface Token { _markdownMathDisplay?: boolean; _markdownMathOpening?: string }
}

const ending = (code: number | null) => code === -5 || code === -4 || code === -3;

/** Delimited math only; numeric dollar prices stay ordinary Markdown. */
function inlineExpression(value: string) {
  if (!value || /^\s|\s$/.test(value)) return false;
  if (/^[\d.,\s]+$/.test(value)) return false;
  if (/^\d[\d.,]*(?:[，。；：!?]|\s+[\u3400-\u9fff])/.test(value)) return false;
  if (/^\d/.test(value) && /美元|美金|人民币|元|\b(?:USD|CNY|RMB|dollars?|price|cost)\b/i.test(value)) return false;
  if (/^\d/.test(value) && /[，。；：、]/.test(value)) return false;
  if (/^\d/.test(value) && /[\u3400-\u9fff]/.test(value) && !value.includes('\\')) return false;
  if (/^\d[\d.,]*\/(?:calls?|requests?|tokens?|months?|years?|days?|hours?|M(?:illion)?\s+tokens?)\b/i.test(value)) return false;
  return true;
}

function tokenizer(flow: boolean): Tokenizer {
  return function (effects, ok, nok) {
    const context = this;
    let token: Token;
    let body: Token;
    let prefix = '';
    let closing: number[] = [];
    let display = false;
    let escaped = false;
    let size = 0;
    const close: Construct = { tokenize: closingSequence, partial: true };
    return start;

    function start(code: number | null): State | undefined {
      token = effects.enter('markdownMath');
      effects.enter('markdownMathMarker');
      if (code === 36) {
        prefix = '$'; effects.consume(code); return dollar;
      }
      if (code === 92) {
        prefix = '\\'; effects.consume(code); return bracket;
      }
      return nok(code);
    }
    function dollar(code: number | null): State | undefined {
      if (code === 36) {
        prefix = '$$'; display = true; effects.consume(code); return opened;
      }
      if (flow) return nok(code);
      closing = [36];
      return opened(code);
    }
    function bracket(code: number | null): State | undefined {
      if (code !== 40 && code !== 91 || flow && code !== 91) return nok(code);
      prefix += String.fromCharCode(code);
      display = code === 91;
      closing = [92, display ? 93 : 41];
      effects.consume(code);
      return opened;
    }
    function opened(code: number | null): State | undefined {
      effects.exit('markdownMathMarker');
      closing = prefix === '$$' ? [36, 36] : closing;
      token._markdownMathDisplay = display;
      token._markdownMathOpening = prefix;
      body = effects.enter('markdownMathBody');
      return content(code);
    }
    function content(code: number | null): State | undefined {
      if (code === null || !display && ending(code) || size > 16_000) return nok(code);
      if (!escaped && code === closing[0]) return effects.attempt(close, closed, consume)(code);
      return consume(code);
    }
    function consume(code: number | null): State | undefined {
      if (code === null) return nok(code);
      escaped = code === 92 && !escaped;
      size += 1;
      if (ending(code)) {
        effects.enter('markdownMathLineEnding'); effects.consume(code); effects.exit('markdownMathLineEnding');
      } else effects.consume(code);
      return content;
    }
    function closingSequence(this: TokenizeContext, closeEffects: Effects, success: State, failure: State): State {
      let index = 0;
      return next;
      function next(code: number | null): State | undefined {
        if (code !== closing[index]) return failure(code);
        closeEffects.consume(code);
        index += 1;
        return index === closing.length ? success : next;
      }
    }
    function closed(code: number | null): State | undefined {
      // An attempted closer is inside the body token until now. Exclude its
      // exact delimiters when checking single-dollar currency ambiguity.
      effects.exit('markdownMathBody');
      const literal = context.sliceSerialize(body);
      const expression = literal.slice(0, -closing.length);
      if (!expression.trim() || prefix === '$' && (!inlineExpression(expression) || code !== null && code >= 48 && code <= 57)) return nok(code);
      if (flow && code !== null && !ending(code) && code !== 32 && code !== -2 && code !== -1) return nok(code);
      if (flow && (code === 32 || code === -2 || code === -1)) return trailing(code);
      effects.exit('markdownMath');
      return ok(code);
    }
    function trailing(code: number | null): State | undefined {
      if (code === 32 || code === -2 || code === -1) { effects.consume(code); return trailing; }
      if (code !== null && !ending(code)) return nok(code);
      effects.exit('markdownMath');
      return ok(code);
    }
  };
}

const syntax: Extension = {
  text: {36: {name: 'markdownMathText', tokenize: tokenizer(false)}, 92: {name: 'markdownMathText', tokenize: tokenizer(false)}},
  flow: {36: {name: 'markdownMathFlow', concrete: true, tokenize: tokenizer(true)}, 92: {name: 'markdownMathFlow', concrete: true, tokenize: tokenizer(true)}},
};

function enter(this: CompileContext, token: Token) {
  this.enter({type: 'markdownMath', value: '', display: token._markdownMathDisplay === true}, token);
}
function exit(this: CompileContext, token: Token) {
  const node = this.stack[this.stack.length - 1] as MarkdownMathNode;
  const source = this.sliceSerialize(token).trimEnd();
  const opening = token._markdownMathOpening ?? '$';
  node.value = source.slice(opening.length, -opening.length);
  node.data = {hName: 'span', hProperties: {
    className: [node.display ? 'markdown-math-block' : 'markdown-math-inline'],
    dataMarkdownMath: node.display ? 'block' : 'inline', dataMathSource: source, dataMathTex: node.value,
  }, hChildren: [{type: 'text', value: source}]};
  this.exit(token);
}
const fromMarkdown: MdastExtension = {
  enter: {markdownMath: enter}, exit: {markdownMath: exit},
};

/** Split embedded display formulas into siblings, avoiding a block inside <p>. */
function standaloneDisplays(parent: Parent) {
  for (let index = 0; index < parent.children.length; index += 1) {
    const child = parent.children[index];
    if ('children' in child) standaloneDisplays(child as Parent);
    if (child.type !== 'paragraph' || !child.children.some(node => node.type === 'markdownMath' && node.display)) continue;
    const replacements: Root['children'] = [];
    let contents: typeof child.children = [];
    const paragraph = () => { if (contents.length) replacements.push({...child, children: contents}); contents = []; };
    for (const node of child.children) {
      if (node.type === 'markdownMath' && node.display) { paragraph(); replacements.push(node); }
      else contents.push(node);
    }
    paragraph();
    parent.children.splice(index, 1, ...replacements);
    index += replacements.length - 1;
  }
}

export default function remarkMarkdownMath(this: Processor) {
  const data = this.data() as {micromarkExtensions?: Extension[]; fromMarkdownExtensions?: MdastExtension[]};
  (data.micromarkExtensions ??= []).push(syntax);
  (data.fromMarkdownExtensions ??= []).push(fromMarkdown);
  return (tree: Root) => standaloneDisplays(tree);
}
