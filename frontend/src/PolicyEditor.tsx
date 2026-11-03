// The policy YAML editor: completions and hover docs from the policy schema, and the server's own build errors shown
// on the lines they point at while you type (POST /v1/agents/validate; nothing is registered).
import { autocompletion, type CompletionContext, type CompletionResult } from "@codemirror/autocomplete";
import { yaml } from "@codemirror/lang-yaml";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { linter, lintGutter, type Diagnostic } from "@codemirror/lint";
import { EditorView, hoverTooltip } from "@codemirror/view";
import { tags } from "@lezer/highlight";
import { basicSetup } from "codemirror";
import { useEffect, useRef } from "react";
import { api, type ValidationError } from "./api";
import { describeAt, optionsAt, parse, pathAt, rangeOf, type Dataset, type Root } from "./policySchema";

// Colours from the console's tokens, so the editor follows light and dark mode.
const highlight = HighlightStyle.define([
  { tag: [tags.propertyName, tags.definition(tags.propertyName)], color: "var(--accent)" },
  { tag: [tags.string, tags.special(tags.string)], color: "var(--ok)" },
  { tag: [tags.number, tags.bool, tags.null, tags.keyword], color: "var(--warn)" },
  { tag: [tags.comment, tags.lineComment], color: "var(--faint)", fontStyle: "italic" },
  { tag: [tags.punctuation, tags.separator, tags.squareBracket, tags.brace], color: "var(--muted)" },
]);

type Props = {
  value: string;
  onChange: (text: string) => void;
  schema: Root | null;
  datasets: Dataset[];
  /** the build's errors for the current text (empty when it builds); undefined while a check is in flight */
  onIssues: (issues: ValidationError[] | undefined, agent: string | null) => void;
};

export default function PolicyEditor({ value, onChange, schema, datasets, onIssues }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  // the extensions read the latest props through this ref, so the editor is built once
  const props = useRef({ schema, datasets, onChange, onIssues });
  props.current = { schema, datasets, onChange, onIssues };

  useEffect(() => {
    const complete = (ctx: CompletionContext): CompletionResult | null => {
      const root = props.current.schema;
      if (!root) return null;
      const word = ctx.matchBefore(/[\w-]*/);
      if (!word || (word.from === word.to && !ctx.explicit)) return null;
      const text = ctx.state.doc.toString();
      const line = ctx.state.doc.lineAt(ctx.pos);
      const before = line.text.slice(0, word.from - line.from);
      const keyLine = /^\s*$/.test(before);  // "  cha|": a key in a mapping
      const itemLine = /^\s*- $/.test(before);  // "- gre|": a list item (a step name, or the first key of a mapping item)
      // parse the text with a placeholder where the word is, so the path resolves even mid-edit
      const { doc } = parse(text.slice(0, word.from) + (keyLine ? "__probe__: x" : "__probe__") + text.slice(word.to));
      const at = pathAt(doc, word.from);
      const { datasets } = props.current;
      let options = optionsAt(root, at.path, at.present.filter((k) => k !== "__probe__"), datasets, keyLine);
      if (!options.length && itemLine) options = optionsAt(root, at.path, [], datasets, true);
      if (!options.length) return null;
      return {
        from: word.from,
        options: options.map((o) => ({
          label: o.label,
          type: o.kind === "key" ? "property" : "enum",
          detail: o.detail,
          info: o.info,
          apply: o.kind === "key" ? `${o.label}: ` : o.label,
        })),
        validFor: /^[\w-]*$/,
      };
    };

    const hover = hoverTooltip((v, pos) => {
      const root = props.current.schema;
      if (!root) return null;
      const { doc } = parse(v.state.doc.toString());
      const word = v.state.wordAt(pos);
      if (!word) return null;
      const at = pathAt(doc, pos);
      const name = v.state.sliceDoc(word.from, word.to);
      const own = at.onKey ? [...at.path, name] : at.path;
      // a step name ("- job_search" or "- form_fill:") shows its node's description
      const text = (at.path[0] === "flow" && at.path.length === 2 ? describeAt(root, ["flow", name]) : undefined) ?? describeAt(root, own);
      if (!text) return null;
      return {
        pos: word.from,
        end: word.to,
        create: () => {
          const dom = document.createElement("div");
          dom.className = "cm-doc";
          dom.textContent = text;
          return { dom };
        },
      };
    });

    const check = linter(async (v) => {
      const text = v.state.doc.toString();
      props.current.onIssues(undefined, null);
      try {
        const r = await api.validate(text);
        if (v.state.doc.toString() !== text) return [];  // edited meanwhile: the next check reports
        props.current.onIssues(r.errors, r.agent);
        return r.errors.map((e): Diagnostic => ({ ...rangeOf(text, e.path), severity: "error", message: `${e.path}: ${e.message}` }));
      } catch {
        props.current.onIssues([], null);  // API offline: the status pill says so
        return [];
      }
    }, { delay: 600 });

    view.current = new EditorView({
      parent: host.current!,
      doc: value,
      extensions: [
        basicSetup,
        yaml(),
        syntaxHighlighting(highlight),
        autocompletion({ override: [complete], activateOnTyping: true }),
        hover,
        check,
        lintGutter(),
        EditorView.updateListener.of((u) => { if (u.docChanged) props.current.onChange(u.state.doc.toString()); }),
        EditorView.contentAttributes.of({ "aria-label": "Policy YAML" }),
      ],
    });
    return () => view.current?.destroy();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- built once; props are read through the ref
  }, []);

  useEffect(() => {  // a template, a saved version or a draft replaced the text
    const v = view.current;
    if (v && v.state.doc.toString() !== value) {
      v.dispatch({ changes: { from: 0, to: v.state.doc.length, insert: value }, selection: { anchor: 0 }, scrollIntoView: true });
      v.scrollDOM.scrollLeft = 0;
    }
  }, [value]);

  return <div className="editor" ref={host} />;
}
