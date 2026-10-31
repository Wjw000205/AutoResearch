import { Children, cloneElement, createContext, isValidElement, useContext, useMemo, type ReactNode } from 'react';
import { referenceNames, referenceText, type NameIndex } from './referenceLabels';

const Names = createContext<NameIndex>(new Map());

export function ReferenceNames({ records, children }: { records: unknown; children: ReactNode }) {
  const parent = useContext(Names);
  const names = useMemo(() => referenceNames(records, parent), [records, parent]);
  return <Names.Provider value={names}>{children}</Names.Provider>;
}

export function ReferenceText({ value }: { value: unknown }) {
  return <>{referenceText(value, useContext(Names))}</>;
}

/** Formats Markdown's rendered text, preserving link destinations and code source files. */
export function ReadableContent({ children }: { children: ReactNode }) {
  const names = useContext(Names);
  const readable = (value: ReactNode): ReactNode => Children.map(value, item => {
    if (typeof item === 'string') return referenceText(item, names);
    if (isValidElement<{ children?: ReactNode }>(item) && item.props.children != null)
      return cloneElement(item, {}, readable(item.props.children));
    return item;
  });
  return <>{readable(children)}</>;
}
