import { useEffect, useRef } from 'react';
import { EditorState } from '@codemirror/state';
import { EditorView, keymap, lineNumbers, highlightActiveLine } from '@codemirror/view';
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands';
import { json } from '@codemirror/lang-json';
import { bracketMatching, indentOnInput, syntaxHighlighting, HighlightStyle } from '@codemirror/language';
import { tags } from '@lezer/highlight';

export interface JsonEditorProps {
  /** The editor's accessible name (aria-label on its editable content), e.g. "POST /v1/decide request". */
  label: string;
  value: string;
  onChange: (next: string) => void;
  readOnly?: boolean;
  className?: string;
}

// Colours taken from the brand's own custom properties (packages/brand/tokens.css),
// not Tailwind classes — CodeMirror's theme is plain CSS-in-JS, not JSX.
const theme = EditorView.theme({
  '&': { height: '100%', fontSize: '13px', backgroundColor: 'var(--color-panel)' },
  '&.cm-focused': { outline: 'none' },
  '.cm-scroller': { fontFamily: 'var(--font-mono)', lineHeight: '1.65' },
  '.cm-content': { caretColor: 'var(--color-primary)', padding: '10px 0' },
  '.cm-gutters': { backgroundColor: 'var(--color-panel)', color: 'var(--color-faint)', border: 'none', minWidth: '40px' },
  '.cm-activeLine': { backgroundColor: 'var(--color-primary-light)' },
  '.cm-activeLineGutter': { backgroundColor: 'transparent', color: 'var(--color-text)' },
  '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection': {
    backgroundColor: 'var(--color-primary-border) !important',
  },
});

const highlight = HighlightStyle.define([
  { tag: tags.propertyName, color: 'var(--color-primary-dark)' },
  { tag: tags.string, color: 'var(--color-success)' },
  { tag: [tags.number, tags.bool, tags.null], color: 'var(--color-warning)' },
  { tag: tags.punctuation, color: 'var(--color-faint)' },
]);

/** A JSON editor styled from the brand tokens, for request/response bodies in the console. */
export function JsonEditor({ label, value, onChange, readOnly = false, className = '' }: JsonEditorProps) {
  const host = useRef<HTMLDivElement | null>(null);
  const view = useRef<EditorView | null>(null);
  const changed = useRef(onChange);
  changed.current = onChange;

  useEffect(() => {
    if (!host.current) throw new Error('JsonEditor host element is missing');
    view.current = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: value,
        extensions: [
          theme,
          syntaxHighlighting(highlight),
          lineNumbers(),
          highlightActiveLine(),
          bracketMatching(),
          indentOnInput(),
          history(),
          keymap.of([...defaultKeymap, ...historyKeymap]),
          json(),
          EditorView.lineWrapping,
          EditorView.contentAttributes.of({ 'aria-label': label }),
          EditorView.editable.of(!readOnly),
          EditorState.readOnly.of(readOnly),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) changed.current(update.state.doc.toString());
          }),
        ],
      }),
    });
    return () => view.current?.destroy();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [readOnly, label]);

  // Replace the text when it changes from outside (a different request selected).
  useEffect(() => {
    const editor = view.current;
    if (editor && editor.state.doc.toString() !== value) {
      editor.dispatch({ changes: { from: 0, to: editor.state.doc.length, insert: value } });
    }
  }, [value]);

  return <div ref={host} className={['h-full min-h-0 overflow-auto rounded-button border border-line font-mono', className].join(' ')} />;
}
