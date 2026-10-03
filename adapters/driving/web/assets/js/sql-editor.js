/* Build-time only: bundled CodeMirror 6; no Node/ESM resolution in production. */
import {basicSetup} from 'codemirror';
import {EditorView, keymap} from '@codemirror/view';
import {EditorState} from '@codemirror/state';
import {sql} from '@codemirror/lang-sql';
import {oneDark} from '@codemirror/theme-one-dark';

const textarea = document.getElementById('sql-input');
const mount = document.getElementById('sql-editor');
if (textarea && mount) {
  const view = new EditorView({
    state: EditorState.create({
      doc: textarea.value,
      extensions: [
        basicSetup, sql(), oneDark,
        keymap.of([{key:'Mod-Enter', run:() => {
          textarea.form?.requestSubmit(); return true;
        }}]),
        EditorView.updateListener.of(update => {
          if (update.docChanged) {
            textarea.value = update.state.doc.toString();
            textarea.dispatchEvent(new Event('input', {bubbles:true}));
          }
        }),
      ],
    }),
    parent:mount,
  });
  textarea.classList.add('visually-hidden');
  window.redbotSqlEditor = view;
}
