// Compose a policy without writing YAML: start from a sentence or a bot type, change it in plain words, and set every
// part on cards. Each card edits the same YAML (comments kept), which the preview shows with what changed.
import { lazy, Suspense, useState } from "react";
import { diffLines } from "diff";
import { api, BotType, Dataset, ValidationError } from "./api";
import { Memory, memoryValue, readConfig, Rule, rulesValue, setKey, setStep } from "./policyDoc";
import { type Root } from "./policySchema";
import Setup from "./Setup";

const PolicyEditor = lazy(() => import("./PolicyEditor"));

type Props = {
  policy: string; onChange: (yaml: string) => void; schema: Root | null; datasets: Dataset[]; botTypes: BotType[];
  issues: ValidationError[] | undefined; onIssues: (i: ValidationError[] | undefined, agent: string | null) => void;
  onSave: () => void; onAddStep: (name: string) => void; onTry: () => void; onTests: () => void; savedTick: number;
};
type Change = { before: string; yaml: string; changes: string[]; errors: ValidationError[] };

const TRIGGERS: { id: string; label: string }[] = [
  { id: "user_request", label: "they ask for a person" },
  { id: "max_reasks", label: "an answer fails too often" },
  { id: "disengaged", label: "they keep wandering off" },
  { id: "tool_unavailable", label: "a tool is down" },
];
const IDEAS = ["Also ask for their email address", "Be warmer and more casual", "Hand off to a person if they mention a refund over $100",
  "Add Spanish", "Offer a discount code when an item arrived late"];
const snake = (s: string) => s.toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "");

export default function Compose(p: Props) {
  const cfg = readConfig(p.policy);
  const [describe, setDescribe] = useState("");
  const [ask, setAsk] = useState("");
  const [busy, setBusy] = useState<"draft" | "edit" | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [change, setChange] = useState<Change | null>(null);
  const [base, setBase] = useState(p.policy);  // what "changes" in the preview are measured from
  const [view, setView] = useState<"yaml" | "diff">("yaml");

  const set = (path: string[], value: unknown) => p.onChange(setKey(p.policy, path, value));
  const fail = (e: unknown) => setMsg({ kind: "err", text: (e as Error).message });

  async function draft() {
    setBusy("draft"); setMsg(null);
    try {
      const d = await api.draft(describe.trim());
      setBase(p.policy); p.onChange(d.yaml); setView("diff");
      setMsg(d.errors.length ? { kind: "err", text: `Drafted; ${d.errors.length} problem(s) remain, marked in the YAML.` } : { kind: "ok", text: "Drafted a policy that builds. Adjust it below or ask for changes." });
    } catch (e) { fail(e); } finally { setBusy(null); }
  }

  async function edit(instruction = ask) {
    if (!instruction.trim()) return;
    setBusy("edit"); setMsg(null);
    try {
      const r = await api.editPolicy(p.policy, instruction.trim());
      setChange({ before: p.policy, ...r });
    } catch (e) { fail(e); } finally { setBusy(null); }
  }

  function accept() {
    if (!change) return;
    setBase(change.before); p.onChange(change.yaml); setChange(null); setAsk(""); setView("diff");
    setMsg({ kind: "ok", text: `Applied ${change.changes.length || "the"} change${change.changes.length === 1 ? "" : "s"}.` });
  }

  const startFrom = (yaml: string) => { setBase(p.policy); p.onChange(yaml); setChange(null); setMsg(null); };
  const setRules = (rules: Rule[]) => set(["rules"], rules.length ? rulesValue(rules) : undefined);
  const setMemory = (items: Memory[]) => set(["memory"], items.length ? memoryValue(items) : undefined);
  const changed = base !== p.policy;

  return (
    <main className="compose">
      <section className="card compose-left">
        <div className="card-head"><h2>Compose</h2><span className="muted small">Every change edits the YAML on the right</span></div>

        <div className="compose-start">
          <h3>1 · Start</h3>
          <div className="field-row">
            <textarea rows={2} value={describe} onChange={(e) => setDescribe(e.target.value)} aria-label="Describe the bot"
              placeholder="Describe the bot, e.g. A returns desk for a shoe store: ask consent, the order number and what went wrong; hand off refunds over $100." />
            <button className="primary" onClick={draft} disabled={!!busy || describe.trim().length < 10}>{busy === "draft" ? "Drafting…" : "Draft it"}</button>
          </div>
          {p.botTypes.length > 0 && (
            <div className="chips-row"><span className="muted small">or start from</span>
              {p.botTypes.map((t) => <button key={t.id} className="chip" title={t.description} onClick={() => startFrom(t.yaml)}>{t.name}</button>)}
            </div>
          )}
        </div>

        <div className="compose-ask">
          <h3>2 · Ask for a change</h3>
          <div className="field-row">
            <input className="grow" value={ask} onChange={(e) => setAsk(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") void edit(); }}
              aria-label="Change to make" placeholder="Say what to change, in plain words" maxLength={2000} />
            <button className="primary" onClick={() => edit()} disabled={!!busy || ask.trim().length < 3}>{busy === "edit" ? "Changing…" : "Change it"}</button>
          </div>
          <div className="chips-row">{IDEAS.map((x) => <button key={x} className="chip" disabled={!!busy} onClick={() => { setAsk(x); void edit(x); }}>{x}</button>)}</div>
          {change && (
            <div className={`callout ${change.errors.length ? "err" : "ok"}`}>
              <strong>{change.changes.length ? `${change.changes.length} change${change.changes.length === 1 ? "" : "s"}` : "Suggested change"}</strong>
              {change.changes.length > 0 && <ul>{change.changes.map((c) => <li key={c}>{c}</li>)}</ul>}
              {change.errors.length > 0 && <p className="small">Still {change.errors.length} build problem(s): {change.errors.slice(0, 2).map((e) => `${e.path}: ${e.message}`).join("; ")}</p>}
              <Diff from={change.before} to={change.yaml} />
              <div className="field-row"><button className="primary compact" onClick={accept}>Apply</button><button className="ghost compact" onClick={() => setChange(null)}>Discard</button></div>
            </div>
          )}
          {msg && <div className={`callout ${msg.kind}`} role="status">{msg.text}</div>}
        </div>

        <h3>3 · Set each part</h3>
        {!cfg ? <div className="callout err">The YAML doesn't parse, so the cards can't show it. Fix the line marked on the right.</div> : (
          <>
            <details className="compose-card" open>
              <summary><b>Purpose, steps and safety</b><span className="muted small">charter, languages, checks, tools, limits, steps and questions</span></summary>
              <Setup policy={p.policy} refresh={p.savedTick} onChange={p.onChange} schema={p.schema} onSave={p.onSave} onAddStep={p.onAddStep} onTests={p.onTests} />
            </details>

            <details className="compose-card" open>
              <summary><b>Behaviour</b><span className="muted small">small talk, and how carefully each step replies</span></summary>
              <label className="inline-field">Small talk
                <select value={cfg.small_talk} onChange={(e) => set(["small_talk"], e.target.value === "off" ? undefined : e.target.value)}>
                  <option value="off">off: counts as wandering off</option><option value="brief">brief: one friendly line, then back</option>
                  <option value="warm">warm: picks up what they mention</option>
                </select>
              </label>
              {cfg.steps.length === 0 ? <p className="muted small">Add steps to set how each one replies.</p> : (
                <table className="log compose-steps"><thead><tr><th>Step</th><th title="Plan each reply before writing it">Think first</th><th title="One follow-up when an answer is too thin">Curious</th><th title="Large model and always checked">High stakes</th><th>Model</th></tr></thead>
                  <tbody>{cfg.steps.map((s, i) => (
                    <tr key={`${i}-${s.name}`}>
                      <td>{s.name.replace(/_/g, " ")}</td>
                      <td><input type="checkbox" aria-label={`Think first on ${s.name}`} checked={s.think} onChange={() => p.onChange(setStep(p.policy, i, "think", s.think ? undefined : true))} /></td>
                      <td>{s.fields ? <input type="checkbox" aria-label={`Curious on ${s.name}`} checked={s.curious} onChange={() => p.onChange(setStep(p.policy, i, "curious", s.curious ? undefined : true))} /> : <span className="muted small">-</span>}</td>
                      <td><input type="checkbox" aria-label={`High stakes on ${s.name}`} checked={s.high_stakes} onChange={() => p.onChange(setStep(p.policy, i, "high_stakes", s.high_stakes ? undefined : true))} /></td>
                      <td><select className="compact" aria-label={`Model for ${s.name}`} value={s.model} onChange={(e) => p.onChange(setStep(p.policy, i, "model", e.target.value || undefined))}>
                        <option value="">usual</option><option value="small">small</option><option value="large">large</option></select></td>
                    </tr>
                  ))}</tbody></table>
              )}
            </details>

            <details className="compose-card" open>
              <summary><b>Rules</b><span className="muted small">{cfg.rules.length ? `${cfg.rules.length} rule${cfg.rules.length === 1 ? "" : "s"}` : "when this happens, the reply does that"}</span></summary>
              {cfg.rules.map((r, i) => (
                <div key={i} className="rule-edit">
                  <div className="field-row">
                    <input className="compact" aria-label="Rule name" value={r.name} onChange={(e) => setRules(cfg.rules.map((x, k) => (k === i ? { ...x, name: snake(e.target.value) } : x)))} />
                    <label className="check small"><input type="checkbox" checked={r.high_stakes} onChange={() => setRules(cfg.rules.map((x, k) => (k === i ? { ...x, high_stakes: !x.high_stakes } : x)))} />high stakes</label>
                    <button className="ghost compact" aria-label={`Remove rule ${r.name}`} onClick={() => setRules(cfg.rules.filter((_, k) => k !== i))}>✕</button>
                  </div>
                  <label>When<input aria-label="When" value={r.when} placeholder="the customer says an item arrived damaged" onChange={(e) => setRules(cfg.rules.map((x, k) => (k === i ? { ...x, when: e.target.value } : x)))} /></label>
                  <label>Then<input aria-label="Then" value={r.then} placeholder="say a free replacement can be sent" onChange={(e) => setRules(cfg.rules.map((x, k) => (k === i ? { ...x, then: e.target.value } : x)))} /></label>
                </div>
              ))}
              <button className="compact" onClick={() => setRules([...cfg.rules, { name: `rule_${cfg.rules.length + 1}`, when: "", then: "", high_stakes: false }])}>+ Add rule</button>
            </details>

            <details className="compose-card" open>
              <summary><b>Handoff</b><span className="muted small">{cfg.handoff ? `to a person when ${cfg.handoff.when.length} thing${cfg.handoff.when.length === 1 ? "" : "s"} happen` : "never passes to a person"}</span></summary>
              <label className="check"><input type="checkbox" checked={!!cfg.handoff} onChange={() => set(["handoff"], cfg.handoff ? undefined : { when: ["user_request"] })} />Pass the conversation to a person</label>
              {cfg.handoff && (
                <>
                  <div className="checks-row">{TRIGGERS.map((t) => (
                    <label key={t.id} className="check"><input type="checkbox" checked={cfg.handoff!.when.includes(t.id)}
                      onChange={() => { const w = cfg.handoff!.when.includes(t.id) ? cfg.handoff!.when.filter((x) => x !== t.id) : [...cfg.handoff!.when, t.id]; if (w.length) set(["handoff", "when"], w); }} />when {t.label}</label>
                  ))}</div>
                  <label className="inline-field">Take it back after
                    <input className="compact" value={cfg.handoff.timeout} placeholder="e.g. 2h (empty: wait)" onChange={(e) => set(["handoff", "timeout"], e.target.value || undefined)} /></label>
                </>
              )}
            </details>

            <details className="compose-card" open>
              <summary><b>Memory</b><span className="muted small">{cfg.memoryItems.length ? `remembers ${cfg.memoryItems.length} thing${cfg.memoryItems.length === 1 ? "" : "s"} per user` : "remembers nothing between conversations"}</span></summary>
              {cfg.memoryItems.map((m, i) => (
                <div key={i} className="field-row">
                  <input className="compact" aria-label="What to remember" value={m.field} onChange={(e) => setMemory(cfg.memoryItems.map((x, k) => (k === i ? { ...x, field: snake(e.target.value) } : x)))} />
                  <input className="grow" aria-label="How to find it" value={m.extract} placeholder="How to find it, e.g. How they prefer to be contacted" onChange={(e) => setMemory(cfg.memoryItems.map((x, k) => (k === i ? { ...x, extract: e.target.value } : x)))} />
                  <input className="compact narrow" aria-label="Forget after" value={m.half_life} placeholder="90d" onChange={(e) => setMemory(cfg.memoryItems.map((x, k) => (k === i ? { ...x, half_life: e.target.value } : x)))} />
                  <button className="ghost compact" aria-label={`Forget ${m.field}`} onClick={() => setMemory(cfg.memoryItems.filter((_, k) => k !== i))}>✕</button>
                </div>
              ))}
              <button className="compact" onClick={() => setMemory([...cfg.memoryItems, { field: `item_${cfg.memoryItems.length + 1}`, extract: "", half_life: "" }])}>+ Remember something</button>
              {cfg.memoryItems.length > 0 && <p className="muted small">Memory needs the consent step first; the launch checklist says so if it is missing.</p>}
            </details>
          </>
        )}
      </section>

      <section className="card compose-right">
        <div className="card-head">
          <h2>Policy</h2>
          <span className={`live ${p.issues === undefined ? "" : p.issues.length ? "bad" : "good"}`} aria-live="polite">
            {p.issues === undefined ? "checking…" : p.issues.length ? `${p.issues.length} problem${p.issues.length === 1 ? "" : "s"}` : "builds"}</span>
        </div>
        <div className="tabs" role="tablist">
          <button role="tab" aria-selected={view === "yaml"} className={view === "yaml" ? "on" : ""} onClick={() => setView("yaml")}>YAML</button>
          <button role="tab" aria-selected={view === "diff"} className={view === "diff" ? "on" : ""} onClick={() => setView("diff")}>What changed{changed ? " •" : ""}</button>
        </div>
        {view === "yaml"
          ? <Suspense fallback={<pre className="code">{p.policy}</pre>}><PolicyEditor value={p.policy} onChange={p.onChange} schema={p.schema} datasets={p.datasets} onIssues={p.onIssues} /></Suspense>
          : changed ? <Diff from={base} to={p.policy} /> : <p className="muted small">No changes yet in this session.</p>}
        <div className="field-row">
          <button className="primary" onClick={p.onTry} disabled={!!p.issues?.length}>Build & try in chat</button>
          <button onClick={p.onSave} disabled={!!p.issues?.length}>Save version</button>
          {changed && <button className="ghost compact" onClick={() => setBase(p.policy)}>Mark as seen</button>}
        </div>
      </section>
    </main>
  );
}

function Diff({ from, to }: { from: string; to: string }) {
  const parts = diffLines(from, to);
  if (parts.every((x) => !x.added && !x.removed)) return <p className="muted small">No differences.</p>;
  return (
    <pre className="code diff">{parts.map((part, i) => (
      <span key={i} className={part.added ? "add" : part.removed ? "del" : ""}>
        {part.value.replace(/\n$/, "").split("\n").map((l) => `${part.added ? "+" : part.removed ? "-" : " "} ${l}`).join("\n") + "\n"}
      </span>
    ))}</pre>
  );
}
