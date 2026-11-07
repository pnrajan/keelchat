// Compose: make a bot in two steps. Say what it should do; read what it will do, in plain words; ask for any change in
// plain words. Detailed settings and the YAML are one click away, folded, for people who want them.
import { lazy, Suspense, useEffect, useState } from "react";
import { diffLines } from "diff";
import { api, BotType, Dataset, ValidationError } from "./api";
import { Config, Memory, memoryValue, readConfig, Rule, rulesValue, setKey, setStep } from "./policyDoc";
import { type Root } from "./policySchema";
import Setup from "./Setup";

const PolicyEditor = lazy(() => import("./PolicyEditor"));

type Props = {
  policy: string; onChange: (yaml: string) => void; schema: Root | null; datasets: Dataset[]; botTypes: BotType[];
  issues: ValidationError[] | undefined; onIssues: (i: ValidationError[] | undefined, agent: string | null) => void;
  onSave: () => void; onAddStep: (name: string) => void; onTry: () => void; onTests: () => void; savedTick: number;
};
type Change = { before: string; yaml: string; changes: string[]; errors: ValidationError[] };
type Check = { errors: ValidationError[]; agent: string | null; summary: string[] };

const EXAMPLES = [
  "A returns desk for a shoe store: get the order number and what went wrong, and offer a replacement for damaged items.",
  "Book haircut appointments: ask which service, the day and time, and their phone number.",
  "Collect feedback after a purchase: how likely they are to recommend us, and why.",
];
const IDEAS = ["Also ask for their email", "Be warmer and more casual", "Let them ask for a person", "Add Spanish"];
const TRIGGERS: { id: string; label: string }[] = [
  { id: "user_request", label: "they ask for a person" },
  { id: "max_reasks", label: "an answer fails too often" },
  { id: "disengaged", label: "they keep wandering off" },
  { id: "tool_unavailable", label: "a tool is down" },
];
const snake = (s: string) => s.toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
const plainChange = (c: string) => c.replace(/`/g, "").replace(/^./, (x) => x.toUpperCase());

export default function Compose(p: Props) {
  const cfg = readConfig(p.policy);
  const [stage, setStage] = useState<"start" | "bot">("start");
  const [describe, setDescribe] = useState("");
  const [ask, setAsk] = useState("");
  const [busy, setBusy] = useState<"draft" | "edit" | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [change, setChange] = useState<Change | null>(null);
  const [check, setCheck] = useState<Check | null>(null);
  const { onIssues } = p;

  useEffect(() => {  // what the bot does in plain words, and whether it builds: half a second after the last change
    const t = window.setTimeout(() => {
      api.validate(p.policy).then((r) => { setCheck(r); onIssues(r.errors, r.agent); }, () => setCheck(null));
    }, 500);
    return () => window.clearTimeout(t);
  }, [p.policy, onIssues]);

  const fail = (e: unknown) => setMsg({ kind: "err", text: (e as Error).message });

  async function create(text = describe) {
    if (text.trim().length < 10) return;
    setBusy("draft"); setMsg(null); setChange(null);
    try {
      const d = await api.draft(text.trim());
      p.onChange(d.yaml); setStage("bot");
      if (d.errors.length) setMsg({ kind: "err", text: "Almost there: a few details need fixing. Press Fix it for me." });
    } catch (e) { fail(e); } finally { setBusy(null); }
  }

  async function edit(instruction = ask) {
    if (instruction.trim().length < 3) return;
    setBusy("edit"); setMsg(null);
    try { setChange({ before: p.policy, ...(await api.editPolicy(p.policy, instruction.trim())) }); } catch (e) { fail(e); } finally { setBusy(null); }
  }

  function apply() {
    if (!change) return;
    p.onChange(change.yaml); setChange(null); setAsk("");
    setMsg({ kind: "ok", text: "Done. Try it to see the difference." });
  }

  const errors = check?.errors ?? p.issues ?? [];
  const ready = check !== null && errors.length === 0;

  if (stage === "start") {
    return (
      <main className="compose">
        <div className="compose-col">
          <header className="compose-hero">
            <h1>What should your bot do?</h1>
            <p className="muted">Describe it in a sentence or two. You can change anything afterwards, in plain words.</p>
          </header>
          <textarea className="compose-big" rows={4} value={describe} autoFocus onChange={(e) => setDescribe(e.target.value)} aria-label="What should your bot do?"
            onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void create(); }}
            placeholder="e.g. A returns desk for a shoe store: get the order number and what went wrong, and offer a replacement for damaged items." />
          <div className="field-row">
            <button className="primary big" onClick={() => create()} disabled={!!busy || describe.trim().length < 10}>{busy === "draft" ? "Creating your bot… (about 20 s)" : "Create my bot"}</button>
          </div>
          {msg && <div className={`callout ${msg.kind}`} role="status">{msg.text}</div>}
          <div className="compose-or">
            <span className="muted small">Or try an example</span>
            {EXAMPLES.map((x) => <button key={x} className="example" disabled={!!busy} onClick={() => { setDescribe(x); void create(x); }}>{x}</button>)}
          </div>
          {p.botTypes.length > 0 && (
            <div className="chips-row"><span className="muted small">Or start from a ready-made bot</span>
              {p.botTypes.filter((t) => t.id !== "blank").map((t) => <button key={t.id} className="chip" title={t.description} onClick={() => { p.onChange(t.yaml); setStage("bot"); }}>{t.name}</button>)}
            </div>
          )}
          {cfg?.agent && <button className="ghost compact back" onClick={() => setStage("bot")}>Or keep working on {cfg.agent} →</button>}
        </div>
      </main>
    );
  }

  return (
    <main className="compose">
      <div className="compose-col">
        <section className="bot-card">
          <div className="row-between">
            <div>
              <span className="muted small">Your bot</span>
              <h1>{cfg?.agent || check?.agent || "New bot"}</h1>
            </div>
            <span className={`badge ${check === null ? "info" : ready ? "ok" : "partial"}`}>{check === null ? "checking" : ready ? "ready to try" : "needs a fix"}</span>
          </div>
          {cfg?.charter && <p className="bot-purpose">{cfg.charter.trim()}</p>}
          {check?.summary.length ? (
            <>
              <h2 className="bot-h">What it does</h2>
              <ul className="bot-does">{check.summary.map((x) => <li key={x}>{x.replace(/^\d+\.\s*/, "")}</li>)}</ul>
            </>
          ) : null}
          {!ready && check !== null && (
            <div className="callout err">
              <strong>Something needs fixing</strong>
              <ul>{errors.slice(0, 3).map((e) => <li key={e.path + e.message}>{e.message}</li>)}</ul>
              <button className="compact" onClick={() => edit("Fix the build problems, changing as little as possible")} disabled={!!busy}>Fix it for me</button>
            </div>
          )}
          <div className="field-row">
            <button className="primary big" onClick={p.onTry} disabled={!ready}>Try it</button>
            <button onClick={p.onSave} disabled={!ready} title="Keep this as a version; new chats use it">Save</button>
            <span className="grow" />
            <button className="ghost compact" onClick={() => { setStage("start"); setChange(null); setMsg(null); }}>Start over</button>
          </div>
        </section>

        <section className="compose-ask">
          <h2 className="bot-h">Change something</h2>
          <div className="field-row">
            <input className="grow big" value={ask} onChange={(e) => setAsk(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") void edit(); }}
              aria-label="Change to make" placeholder="e.g. hand off to a person if they ask for a refund over $100" maxLength={2000} />
            <button className="primary" onClick={() => edit()} disabled={!!busy || ask.trim().length < 3}>{busy === "edit" ? "Working…" : "Change it"}</button>
          </div>
          <div className="chips-row">{IDEAS.map((x) => <button key={x} className="chip" disabled={!!busy} onClick={() => { setAsk(x); void edit(x); }}>{x}</button>)}</div>
          {change && (
            <div className={`callout ${change.errors.length ? "err" : "ok"} change`}>
              <strong>{change.changes.length ? "Here's what will change" : "Suggested change"}</strong>
              {change.changes.length > 0 && <ul>{change.changes.map((c) => <li key={c}>{plainChange(c)}</li>)}</ul>}
              {change.errors.length > 0 && <p className="small">It still needs a fix after this; you can apply it and press Fix it for me.</p>}
              <div className="field-row">
                <button className="primary compact" onClick={apply}>Apply</button>
                <button className="ghost compact" onClick={() => setChange(null)}>Discard</button>
              </div>
              <details><summary className="muted small">Show the exact changes</summary><Diff from={change.before} to={change.yaml} /></details>
            </div>
          )}
          {msg && <div className={`callout ${msg.kind}`} role="status">{msg.text}</div>}
        </section>

        <details className="compose-more">
          <summary><b>Fine-tune</b><span className="muted small">steps and questions, rules, handoff, memory, safety, launch checklist</span></summary>
          {!cfg ? <div className="callout err">The settings can't be shown until the YAML parses. Open Show YAML to fix it.</div>
            : <FineTune {...p} cfg={cfg} />}
        </details>

        <details className="compose-more">
          <summary><b>Show YAML</b><span className="muted small">the file behind your bot, for developers</span></summary>
          <Suspense fallback={<pre className="code">{p.policy}</pre>}>
            <PolicyEditor value={p.policy} onChange={p.onChange} schema={p.schema} datasets={p.datasets} onIssues={p.onIssues} />
          </Suspense>
        </details>
      </div>
    </main>
  );
}

function FineTune(p: Props & { cfg: Config }) {
  const { cfg } = p;
  const set = (path: string[], value: unknown) => p.onChange(setKey(p.policy, path, value));
  const setRules = (rules: Rule[]) => set(["rules"], rules.length ? rulesValue(rules) : undefined);
  const setMemory = (items: Memory[]) => set(["memory"], items.length ? memoryValue(items) : undefined);
  return (
    <div className="fine-tune">
      <details className="compose-card">
        <summary><b>Steps, questions and safety</b><span className="muted small">what it asks, in what order, its checks and the launch checklist</span></summary>
        <Setup policy={p.policy} refresh={p.savedTick} onChange={p.onChange} schema={p.schema} onSave={p.onSave} onAddStep={p.onAddStep} onTests={p.onTests} />
      </details>

      <details className="compose-card">
        <summary><b>Personality and care</b><span className="muted small">small talk, and how carefully each step replies</span></summary>
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

      <details className="compose-card">
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

      <details className="compose-card">
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

      <details className="compose-card">
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
    </div>
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
