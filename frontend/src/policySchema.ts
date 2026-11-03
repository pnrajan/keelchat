// Reads the policy JSON Schema (GET /v1/schema) for the editor: what can go at a YAML path, where a build error's path
// sits in the text, and a starter snippet for each flow step. The server's build stays the judge of validity.
import { isMap, isPair, isScalar, isSeq, LineCounter, parseDocument, type Document, type Node } from "yaml";

export type Schema = {
  type?: string | string[]; properties?: Record<string, Schema>; items?: Schema; required?: string[];
  oneOf?: Schema[]; anyOf?: Schema[]; enum?: unknown[]; $ref?: string; description?: string; title?: string;
  default?: unknown; pattern?: string; "x-dataset"?: string; additionalProperties?: boolean | Schema;
};
export type Root = Schema & { $defs?: Record<string, Schema> };
export type Path = (string | number)[];
export type Dataset = { name: string; kind: string };

function deref(root: Root, s: Schema): Schema {
  return s.$ref?.startsWith("#/$defs/") ? root.$defs?.[s.$ref.slice(8)] ?? {} : s;
}

/** Every alternative a schema allows, refs resolved (oneOf/anyOf flattened). */
function variants(root: Root, s: Schema | undefined): Schema[] {
  if (!s) return [];
  const d = deref(root, s);
  const alts = d.oneOf ?? d.anyOf;
  return alts ? alts.flatMap((a) => variants(root, a)) : [d];
}

/** The schemas that may hold the value at `path`. */
export function schemasAt(root: Root, path: Path): Schema[] {
  let cur: Schema[] = [root];
  for (const key of path) {
    cur = cur.flatMap((s) => variants(root, s)).flatMap((s) => {
      if (typeof key === "number") return s.items ? [s.items] : [];
      const p = s.properties?.[key];
      if (p) return [p];
      return typeof s.additionalProperties === "object" ? [s.additionalProperties] : [];
    });
  }
  return cur.flatMap((s) => variants(root, s));
}

export type Option = { label: string; detail?: string; info?: string; kind: "key" | "value" };

/** Keys an object at `path` may still take, or values a scalar there may take. */
export function optionsAt(root: Root, path: Path, present: string[], datasets: Dataset[], asKey: boolean): Option[] {
  const out = new Map<string, Option>();
  for (const s of schemasAt(root, path)) {
    if (asKey) {
      for (const [k, p] of Object.entries(s.properties ?? {})) {
        if (!present.includes(k)) out.set(k, { label: k, info: deref(root, p).description ?? variants(root, p)[0]?.description, kind: "key", detail: s.required?.includes(k) ? "required" : undefined });
      }
    } else {
      for (const v of s.enum ?? []) out.set(String(v), { label: String(v), kind: "value" });
      if (s["x-dataset"]) for (const d of datasets.filter((d) => d.kind === s["x-dataset"])) out.set(d.name, { label: d.name, detail: `${d.kind} dataset`, kind: "value" });
    }
  }
  return [...out.values()];
}

/** Description of whatever sits at `path`, for hover. */
export function describeAt(root: Root, path: Path): string | undefined {
  const key = path[path.length - 1];
  if (path.length === 2 && path[0] === "flow" && typeof key === "string") {  // ["flow", <step name>]
    return schemasAt(root, ["flow", 0]).find((s) => s.title === key)?.description;
  }
  return schemasAt(root, path).map((s) => s.description).find(Boolean);
}

export const parse = (text: string) => {
  const lines = new LineCounter();
  return { doc: parseDocument(text, { lineCounter: lines, keepSourceTokens: true }), lines };
};

/** The YAML path of the key or value at `offset`, whether the cursor is on a key, and the keys already in that map. */
export function pathAt(doc: Document, offset: number): { path: Path; onKey: boolean; present: string[] } {
  let path: Path = [];
  let onKey = true;
  let present: string[] = [];
  const walk = (node: Node | null | undefined, at: Path): void => {
    if (!node || !node.range) return;
    if (isMap(node)) {
      const [start, , end] = node.range;
      if (offset < start || offset > end) return;
      path = at; onKey = true; present = node.items.map((p) => String(isScalar(p.key) ? p.key.value : ""));
      for (const pair of node.items) {
        const k = isScalar(pair.key) ? String(pair.key.value) : "";
        const kr = isScalar(pair.key) ? pair.key.range : undefined;
        if (kr && offset >= kr[0] && offset <= kr[1]) { path = at; onKey = true; return; }
        if (pair.value && (pair.value as Node).range) {
          const vr = (pair.value as Node).range!;
          if (offset >= vr[0] && offset <= vr[1]) {
            path = [...at, k]; onKey = false;
            walk(pair.value as Node, [...at, k]);
            return;
          }
        } else if (kr && offset > kr[1]) { path = [...at, k]; onKey = false; }
      }
    } else if (isSeq(node)) {
      const [start, , end] = node.range;
      if (offset < start || offset > end) return;
      path = [...at, 0]; onKey = false;
      node.items.forEach((item, i) => {
        const r = (item as Node)?.range;
        if (r && offset >= r[0] && offset <= r[1]) { path = [...at, i]; onKey = false; walk(item as Node, [...at, i]); }
      });
    }
  };
  walk(doc.contents as Node, []);
  return { path, onKey, present };
}

/** "flow[1].form_fill.fields[0].name" → ["flow", 1, "form_fill", "fields", 0, "name"] */
export function splitPath(p: string): Path {
  return [...p.matchAll(/([^.[\]]+)|\[(\d+)\]/g)].map((m) => (m[2] !== undefined ? Number(m[2]) : m[1]));
}

/** Text range of a build error: its YAML path (or "line L, column C"), falling back to the nearest parent. */
export function rangeOf(text: string, errorPath: string): { from: number; to: number } {
  const { doc, lines } = parse(text);
  const lc = /^line (\d+), column (\d+)/.exec(errorPath);
  if (lc) {
    const line = Number(lc[1]);
    const from = line <= lines.lineStarts.length ? lines.lineStarts[line - 1] + Number(lc[2]) - 1 : text.length;
    return { from: Math.min(from, text.length), to: Math.min(text.length, from + 1) };
  }
  let path = splitPath(errorPath);
  if (path[0] === "<root>") path = [];
  for (; path.length > 0; path = path.slice(0, -1)) {
    const parent = path.length > 1 ? doc.getIn(path.slice(0, -1), true) : doc.contents;
    if (isMap(parent)) {  // point at the key, which is what the message is about
      const pair = parent.items.find((p) => isPair(p) && isScalar(p.key) && String(p.key.value) === String(path[path.length - 1]));
      const r = pair && isScalar(pair.key) ? pair.key.range : undefined;
      if (r) return { from: r[0], to: r[1] };
    }
    const node = doc.getIn(path, true) as Node | undefined;
    if (node?.range) return { from: node.range[0], to: Math.max(node.range[0] + 1, node.range[1]) };
  }
  const first = text.indexOf("\n");
  return { from: 0, to: first > 0 ? first : Math.min(1, text.length) };
}

function placeholder(root: Root, key: string, s: Schema, datasets: Dataset[]): string {
  const d = variants(root, s)[0] ?? {};
  if (d["x-dataset"]) return datasets.find((x) => x.kind === d["x-dataset"])?.name ?? "my_dataset";
  if (d.enum?.length) return String(d.enum[0]);
  if (d.type === "integer" || d.type === "number") return String(d.default ?? 1);
  if (d.type === "boolean") return String(d.default ?? false);
  if (d.pattern) return `my_${key}`;
  return `"${key.replace(/_/g, " ")}"`;
}

/** A starter for a flow step: its required settings, plus one example item of a list setting (e.g. form fields). */
export function stepSnippet(root: Root, name: string, datasets: Dataset[]): string[] {
  const variant = schemasAt(root, ["flow", 0]).find((s) => s.title === name);
  const cfg = variant?.properties?.[name] ? variants(root, variant.properties[name]).find((s) => s.properties) : undefined;
  if (!cfg) return [name];
  const lines: string[] = [];
  for (const [k, p] of Object.entries(cfg.properties ?? {})) {
    const item = variants(root, p).find((v) => v.items)?.items;
    const itemSchema = item ? variants(root, item).find((v) => v.properties) : undefined;
    if (cfg.required?.includes(k)) lines.push(`  ${k}: ${placeholder(root, k, p, datasets)}`);
    else if (itemSchema?.required?.length) {
      lines.push(`  ${k}:`);
      itemSchema.required.forEach((rk, i) => lines.push(`    ${i ? " " : "-"} ${rk}: ${placeholder(root, rk, itemSchema.properties![rk], datasets)}`));
    }
  }
  return lines.length ? [`${name}:`, ...lines] : [name];
}

export function flowSteps(root: Root): { name: string; description: string }[] {
  return schemasAt(root, ["flow", 0]).filter((s) => s.title).map((s) => ({ name: s.title!, description: (s.description ?? "").split("\n")[0] }));
}

/** Steps that belong at the start of a flow (consent before anything is collected). */
const FIRST = new Set(["greet_consent"]);

/** The policy text with a step added to its flow (or a new flow), matching the flow's indentation: at the start for
 *  consent, at the end otherwise. An existing step of the same name is moved instead of duplicated. */
export function appendStep(text: string, snippet: string[]): string {
  const name = snippet[0].replace(/:$/, "");
  const { doc } = parse(text);
  const flow = doc.get("flow", true);
  if (isSeq(flow)) {
    const at = flow.items.findIndex((it) => (isScalar(it) ? String(it.value) : isMap(it) && isScalar(it.items[0]?.key) ? String(it.items[0].key.value) : "") === name);
    if (at >= 0) {
      if (!FIRST.has(name) || at === 0) return text;  // already there
      const [item] = flow.items.splice(at, 1);
      flow.items.unshift(item);
      return doc.toString({ lineWidth: 0 });
    }
    if (FIRST.has(name) && flow.items.length) {
      flow.items.unshift(doc.createNode(snippet.length === 1 ? name : parseDocument(snippet.join("\n")).toJS()));
      return doc.toString({ lineWidth: 0 });
    }
  }
  return appendAtEnd(text, snippet);
}

function appendAtEnd(text: string, snippet: string[]): string {
  const { doc } = parse(text);
  const flow = doc.get("flow", true);
  const body = (indent: string) => `${indent}- ${snippet[0]}\n` + snippet.slice(1).map((l) => `${indent}  ${l}\n`).join("");
  if (isSeq(flow) && flow.items.length && flow.range) {
    const first = (flow.items[0] as Node).range![0];
    const lineStart = text.lastIndexOf("\n", first - 1) + 1;
    const indent = text.slice(lineStart, first).replace(/-\s*$/, "");
    let end = (flow.items[flow.items.length - 1] as Node).range![1];
    while (end < text.length && text[end - 1] !== "\n") end++;  // after the last item's line
    const head = text.slice(0, end);
    return head + (head.endsWith("\n") ? "" : "\n") + body(indent) + text.slice(end);
  }
  const base = text.endsWith("\n") || !text ? text : text + "\n";
  return flow === undefined ? `${base}flow:\n${body("  ")}` : base;
}
