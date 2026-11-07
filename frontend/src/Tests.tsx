import { useCallback, useEffect, useRef, useState } from "react";
import { AgentTest, api, TestCase, TestDraft, TestRun } from "./api";
import { Conversation } from "./Review";

const caseName = (s: string) => s.toLowerCase().replace(/[^a-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 80) || "test";
const size = (c: TestCase) => (c.persona ? "played persona" : `${c.messages?.length ?? 0} messages`);
const words = (s: string) => s.replace(/_/g, " ");
const title = (s: string) => { const w = words(s.replace(/^gen_/, "")); return w.charAt(0).toUpperCase() + w.slice(1); };
const said = (v: string) => (v === "None" ? "nothing" : v.replace(/^'(.*)'$/, "$1").replace(/_/g, " "));
/** a check's failure in words: "expect: exit_reason: expected 'handed_off', got None" → "exit reason: expected handed off, got nothing" */
function problem(p: string): string {
  const m = /^expect: (\w+): expected (.+?), got (.+)$/.exec(p);
  return m ? `${words(m[1])}: expected ${said(m[2])}, got ${said(m[3])}` : p.replace(/^(\w+)(?=[ :(])/, (w) => words(w));
}
const SOURCE: Record<string, string> = { chat: "from a chat", simulate: "from a simulation", review: "from a rating", draft: "drafted", manual: "written" };

/** what a test expects, in words: only the keys it sets */
function expects(c: TestCase): string[] {
  const out: string[] = [];
  for (const [k, v] of Object.entries(c.expect ?? {})) {
    if (v === null || v === undefined) continue;
    if (k === "completed") out.push(v ? "finishes" : "does not finish");
    else if (k === "exit_reason") out.push(`ends: ${words(String(v))}`);
    else if (k === "language") out.push(`speaks ${v}`);
    else if (k === "rules_fired") out.push(...(v as string[]).map((r) => `rule ${r} matches`));
    else if (k === "rules_not_fired") out.push(...(v as string[]).map((r) => `rule ${r} stays quiet`));
    else if (typeof v === "object") out.push(...Object.entries(v as Record<string, unknown>).map(([kk, vv]) => `${words(k)} ${words(kk)}: ${String(vv)}`));
    else out.push(`${words(k)}: ${String(v)}`);
  }
  return out;
}

function Turns({ turns }: { turns: { user: string; agent?: string; rules?: string[] }[] }) {
  return (
    <div className="transcript">
      {turns.map((t, i) => (
        <div key={i} className="exchange">
          <div className="line user"><span className="who">User</span><span>{t.user}</span></div>
          {t.agent !== undefined && (
            <div className="line agent"><span className="who">Agent</span><span>{t.agent}{t.rules?.length ? <span className="tag">rule {t.rules.join(", ")}</span> : null}</span></div>
          )}
        </div>
      ))}
    </div>
  );
}

// Tests kept with the agent: save a conversation as one, draft them from the policy, run them all on the current build.
// What a test expects comes from code (how the conversation ended, what the policy covers), never from the model.
export default function Tests({ agent, conversations }: { agent: string | null; conversations: Conversation[] }) {
  const [saved, setSaved] = useState<{ agent: string; tests: AgentTest[] } | null>(null);
  const [run, setRun] = useState<{ agent: string; run: TestRun } | null>(null);
  const [draft, setDraft] = useState<{ agent: string; draft: TestDraft } | null>(null);
  const [busy, setBusy] = useState<"run" | "draft" | null>(null);
  const [running, setRunning] = useState<Set<string>>(new Set());
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [pick, setPick] = useState("");
  const [name, setName] = useState("");
  const [sameEnd, setSameEnd] = useState(true);
  const current = useRef(agent);
  current.current = agent;

  const tests = saved && saved.agent === agent ? saved.tests : [];
  const results = run && run.agent === agent ? run.run : null;
  const drafted = draft && draft.agent === agent ? draft.draft : null;
  const load = useCallback(() => {
    if (!agent) return;
    api.tests(agent).then(
      (t) => { if (current.current === agent) setSaved({ agent, tests: t }); },
      (e: Error) => setMsg({ kind: "err", text: e.message }),
    );
  }, [agent]);
  useEffect(load, [load]);

  if (!agent) return <div className="empty">Build a policy first: tests belong to the agent you built.</div>;
  const conv = conversations.find((c) => `${c.source}:${c.id}` === pick) ?? conversations[0];
  const fail = (e: unknown) => setMsg({ kind: "err", text: (e as Error).message });

  async function keep(c: TestCase, source: "chat" | "simulate" | "draft") {
    if (!agent) return false;
    try { await api.saveTest(agent, source, c); return true; } catch (e) { fail(e); return false; }
  }

  async function saveConversation() {
    if (!conv) return;
    const ending = conv.outcome && sameEnd ? { completed: conv.outcome.completed, ...(conv.outcome.exit_reason ? { exit_reason: conv.outcome.exit_reason } : {}) } : undefined;
    const c: TestCase = { name: caseName(name || conv.label), messages: conv.turns.map((t) => t.user), ...(ending ? { expect: ending } : {}), note: `saved from ${conv.label}` };
    if (await keep(c, conv.source)) { setMsg({ kind: "ok", text: `Saved test ${c.name}.` }); setName(""); load(); }
  }

  async function runAll(ids?: string[]) {
    if (!agent) return;
    setBusy("run"); setMsg(null); setRunning(new Set(ids ?? tests.map((t) => t.id)));
    try {
      const r = await api.runTests(agent, ids);
      // a partial run updates only its tests; the others keep their last result
      setRun((prev) => {
        const kept = prev && prev.agent === agent && ids ? prev.run.results.filter((x) => !ids.includes(x.id)) : [];
        return { agent, run: { ...r, results: [...kept, ...r.results] } };
      });
    } catch (e) { fail(e); } finally { setBusy(null); setRunning(new Set()); }
  }

  async function draftAll() {
    if (!agent) return;
    setBusy("draft"); setMsg(null);
    try {
      const d = await api.draftTests(agent);
      setDraft({ agent, draft: d });
      if (!d.targets.length) setMsg({ kind: "ok", text: "Every target in the policy already has a drafted test." });
    } catch (e) { fail(e); } finally { setBusy(null); }
  }

  async function keepDrafted(cases: TestCase[]) {
    let n = 0;
    for (const c of cases) if (await keep(c, "draft")) n += 1;
    setDraft((d) => d && { ...d, draft: { ...d.draft, cases: d.draft.cases.filter((x) => !cases.includes(x)) } });
    if (n) { setMsg({ kind: "ok", text: `Kept ${n} drafted test${n === 1 ? "" : "s"}.` }); load(); }
  }

  async function remove(t: AgentTest) {
    if (!agent) return;
    try { await api.deleteTest(agent, t.id); load(); } catch (e) { fail(e); }
  }

  async function download() {
    if (!agent) return;
    try {
      const text = await api.testsFile(agent);
      const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(new Blob([text], { type: "application/yaml" })), download: "replay.yaml" });
      document.body.appendChild(a); a.click(); a.remove();
      window.setTimeout(() => URL.revokeObjectURL(a.href), 0);
    } catch (e) { fail(e); }
  }

  const byId = new Map((results?.results ?? []).map((r) => [r.id, r]));
  const ran = tests.filter((t) => byId.has(t.id));
  const passed = ran.filter((t) => byId.get(t.id)?.passed).length;
  const failed = ran.filter((t) => !byId.get(t.id)?.passed).map((t) => t.id);

  return (
    <div className="review">
      <p className="muted small">Each test replays a conversation on the agent you built (sample tools). It passes when what it expects holds and
        nothing breaks: no errors, loops or broken limits, and, with a judge, replies in scope and rules followed.</p>
      <div className="test-bar">
        <button className="primary" onClick={() => runAll()} disabled={!!busy || !tests.length}>
          {busy === "run" ? `Running ${running.size}…` : tests.length ? `Run all ${tests.length}` : "Run tests"}</button>
        {failed.length > 0 && <button onClick={() => runAll(failed)} disabled={!!busy}>Run {failed.length} failed again</button>}
      </div>

      {tests.length > 0 && (
        <div className="tiles four">
          <div><span className="num big">{tests.length}</span><span className="muted small">tests</span></div>
          <div className={passed ? "good" : ""}><span className="num big">{passed}</span><span className="muted small">passed</span></div>
          <div className={failed.length ? "bad" : ""}><span className="num big">{failed.length}</span><span className="muted small">failed</span></div>
          <div><span className="num big">{tests.length - ran.length}</span><span className="muted small">not run</span></div>
        </div>
      )}
      {busy === "run" && <p className="muted small">Each test replays its conversation with the real model: about a minute each, up to three at a time.</p>}
      {results && <p className="muted small">Last run on policy <code>{results.policy_hash}</code>. {results.judge
        ? <>Judged checks on (<code>{results.judge}</code>).</>
        : <>Judged checks (scope, rules) are off: set <code>EVALS_JUDGE_MODEL</code> on the server to add them.</>}</p>}
      {msg && <div className={`callout ${msg.kind}`} role="status">{msg.text}</div>}

      {drafted && drafted.cases.length > 0 && (
        <section>
          <h3 className="review-h">Drafted: keep the ones that test what you want</h3>
          {drafted.cases.map((c) => (
            <div key={c.name} className="review-turn">
              <div className="row-between"><b>{title(c.name)}</b>{drafted.failures[c.name] ? <span className="tag fail">fails now</span> : <span className="tag pass">passes</span>}</div>
              <div className="expects"><span className="muted small">{size(c)}</span>{expects(c).map((x) => <span key={x} className="tag">{x}</span>)}</div>
              {c.note && <span className="muted small">{c.note}</span>}
              {(drafted.failures[c.name] ?? []).map((f) => <div key={f} className="problem">{problem(f)}</div>)}
              <div className="field-row">
                <button className="compact" onClick={() => keepDrafted([c])}>Keep</button>
                <button className="ghost compact" onClick={() => setDraft((d) => d && { ...d, draft: { ...d.draft, cases: d.draft.cases.filter((x) => x !== c) } })}>Discard</button>
              </div>
            </div>
          ))}
          <div className="field-row"><button className="compact" onClick={() => keepDrafted(drafted.cases)}>Keep all {drafted.cases.length}</button></div>
          {Object.keys(drafted.not_reached).length > 0 && (
            <p className="muted small">Not reached: {Object.entries(drafted.not_reached).map(([t, why]) => `${t} (${why})`).join("; ")}. Draft again to retry them.</p>
          )}
        </section>
      )}

      <div className="row-between list-head">
        <h3 className="review-h">Tests for {agent}</h3>
        <span className="actions">
          <button className="ghost compact" onClick={draftAll} disabled={!!busy} title="One test per thing the policy should handle: the flow, bad answers, each rule, scope, handoff, languages">
            {busy === "draft" ? "Drafting… (a few minutes)" : "+ Draft from policy"}</button>
          <button className="ghost compact" onClick={download} disabled={!tests.length} title="The tests as a replay set, for evals/<agent>/ and CI">Download YAML</button>
        </span>
      </div>
      {tests.length === 0 ? <p className="muted small">None yet. Save a conversation below, or draft tests from the policy.</p> : tests.map((t) => {
        const r = byId.get(t.id);
        const state = running.has(t.id) ? "running" : r ? (r.passed ? "pass" : "fail") : "not run";
        return (
          <div key={t.id} className={`handoff test ${state.replace(" ", "-")}`}>
            <button className="handoff-head" onClick={() => setOpen(open === t.id ? null : t.id)} aria-expanded={open === t.id}>
              <span className={`badge ${state === "pass" ? "ok" : state === "fail" ? "failed" : "info"}`}>{state}</span>
              <b>{title(t.case.name)}</b>
              <span className="muted small">{SOURCE[t.source] ?? t.source} · {size(t.case)}</span>
              {expects(t.case).length > 0 && <span className="expects">{expects(t.case).map((x) => <span key={x} className="tag">{x}</span>)}</span>}
              {r && !r.passed && <span className="problem-line">{problem(r.problems[0])}{r.problems.length > 1 ? ` (+${r.problems.length - 1} more)` : ""}</span>}
            </button>
            {open === t.id && (
              <div className="handoff-body">
                {t.case.note && <p className="muted small">{t.case.note}</p>}
                {r && r.problems.length > 0 && <div className="callout err"><strong>Why it failed</strong><ul>{r.problems.map((p) => <li key={p}>{problem(p)}</li>)}</ul></div>}
                {r?.outcome && (
                  <p className="muted small">Ended {r.outcome.completed ? "completed" : "not completed"}{r.outcome.exit_reason && r.outcome.exit_reason !== "completed" ? ` (${words(r.outcome.exit_reason)})` : ""}
                    {Object.keys(r.outcome.fields).length > 0 && <> · collected {Object.entries(r.outcome.fields).map(([k, v]) => `${words(k)} ${v}`).join(", ")}</>}</p>
                )}
                {r?.turns.length ? <Turns turns={r.turns} /> : <Turns turns={(t.case.messages ?? []).map((m) => ({ user: m }))} />}
                {!r && t.case.persona && <p className="muted small">The model plays: {t.case.persona}</p>}
                <div className="field-row">
                  <button className="compact" onClick={() => runAll([t.id])} disabled={!!busy}>Run this one</button>
                  <button className="ghost compact" onClick={() => remove(t)}>Delete</button>
                </div>
              </div>
            )}
          </div>
        );
      })}

      <h3 className="review-h">Save a conversation as a test</h3>
      {!conv ? <p className="muted small">Chat with the agent or run a simulation first.</p> : (
        <>
          <div className="field-row">
            <select aria-label="Conversation to save" value={`${conv.source}:${conv.id}`} onChange={(e) => setPick(e.target.value)}>
              {conversations.map((c) => <option key={`${c.source}:${c.id}`} value={`${c.source}:${c.id}`}>{c.label} · {c.turns.length} turns</option>)}
            </select>
            <input className="compact" aria-label="Test name" placeholder={caseName(conv.label)} maxLength={80} value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          {conv.outcome && (
            <label className="check small">
              <input type="checkbox" checked={sameEnd} onChange={(e) => setSameEnd(e.target.checked)} />
              Expect the same ending ({conv.outcome.completed ? "completed" : "not completed"}{conv.outcome.exit_reason ? `, ${conv.outcome.exit_reason}` : ""})
            </label>
          )}
          <p className="muted small">The test replays what the user said; the agent's replies are written fresh on each run.</p>
          <div className="field-row"><button onClick={saveConversation}>Save as test</button></div>
        </>
      )}
    </div>
  );
}
