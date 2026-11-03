// Read and change a policy's settings by path, keeping the rest of the YAML (comments included) as written.
import { isMap, isScalar, isSeq, parseDocument, YAMLMap, type Node } from "yaml";

export type Field = { name: string; ask: string; expect: string; optional: boolean };
export type Step = { name: string; verify: boolean; fields: Field[] | null };
export type Config = {
  agent: string; charter: string; languages: string[]; tools: string[];
  /** null: the key is absent and the platform defaults apply */
  input: string[] | null; output: string[] | null; reasks: number | null; digressions: number | null; steps: Step[]; memory: number;
};

const list = (v: unknown): string[] => (Array.isArray(v) ? v.map(String) : []);
const num = (v: unknown): number | null => (typeof v === "number" ? v : null);

export function readConfig(text: string): Config | null {
  const doc = parseDocument(text);
  if (doc.errors.length) return null;
  const js = (doc.toJS() ?? {}) as Record<string, unknown>;
  const steps: Step[] = (Array.isArray(js.flow) ? js.flow : []).map((s: unknown) => {
    if (typeof s === "string") return { name: s, verify: true, fields: null };
    const [name, body] = Object.entries((s ?? {}) as Record<string, Record<string, unknown> | null>)[0] ?? ["?", null];
    const fields = Array.isArray(body?.fields)
      ? (body!.fields as Record<string, unknown>[]).map((f) => ({ name: String(f.name ?? ""), ask: String(f.ask ?? ""), expect: String(f.expect ?? ""), optional: f.optional === true }))
      : name === "form_fill" ? [] : null;
    return { name, verify: body?.verify !== false, fields };
  });
  const g = (js.guardrails ?? {}) as Record<string, unknown>;
  const l = (js.limits ?? {}) as Record<string, unknown>;
  return {
    // the charter is not trimmed: the box is edited key by key, and a trimmed value would eat every space typed
    agent: String(js.agent ?? ""), charter: String(js.charter ?? "").replace(/\n$/, ""),
    languages: list(js.languages ?? ["en"]), tools: list(js.tools),
    input: "input" in g ? list(g.input) : null, output: "output" in g ? list(g.output) : null, reasks: num(l.reasks), digressions: num(l.digressions), steps,
    memory: Array.isArray(js.memory) ? js.memory.length : 0,
  };
}

function edit(text: string, change: (doc: ReturnType<typeof parseDocument>) => void): string {
  const doc = parseDocument(text);
  if (doc.errors.length) return text;
  change(doc);
  return doc.toString({ lineWidth: 0 });
}

/** Set (or, with undefined / an empty list, remove) a top-level or nested key. */
export function setKey(text: string, path: string[], value: unknown): string {
  return edit(text, (doc) => {
    if (value === undefined || value === null || (Array.isArray(value) && value.length === 0)) doc.deleteIn(path);
    else {
      const node = doc.createNode(value);
      if (Array.isArray(value) && value.every((v) => typeof v === "string") && value.join(", ").length < 60) (node as { flow?: boolean }).flow = true;
      doc.setIn(path, node);
    }
  });
}

/** The step's settings mapping, turning a bare `- name` item into `- name: {}` first. */
function stepBody(doc: ReturnType<typeof parseDocument>, index: number): YAMLMap {
  const flow = doc.get("flow", true);
  if (!isSeq(flow)) throw new Error("no flow");
  const item = flow.items[index] as Node;
  if (isScalar(item)) {
    const m = doc.createNode({ [String(item.value)]: {} }) as YAMLMap;
    flow.items[index] = m;
    return (m.items[0].value as YAMLMap);
  }
  if (!isMap(item)) throw new Error("bad step");
  const pair = item.items[0];
  if (!isMap(pair.value)) pair.value = doc.createNode({});
  return pair.value as YAMLMap;
}

export function setStep(text: string, index: number, key: string, value: unknown): string {
  return edit(text, (doc) => {
    const body = stepBody(doc, index);
    if (value === undefined) body.delete(key);
    else body.set(key, doc.createNode(value));
  });
}

export function removeStep(text: string, index: number): string {
  return edit(text, (doc) => { const flow = doc.get("flow", true); if (isSeq(flow)) flow.items.splice(index, 1); });
}

export function moveStep(text: string, index: number, by: -1 | 1): string {
  return edit(text, (doc) => {
    const flow = doc.get("flow", true);
    if (!isSeq(flow) || index + by < 0 || index + by >= flow.items.length) return;
    const [it] = flow.items.splice(index, 1);
    flow.items.splice(index + by, 0, it);
  });
}
