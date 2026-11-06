import { useCallback, useEffect, useRef, useState } from "react";
import { AgentTest, api, TestCase, TestDraft, TestRun } from "./api";
import { Conversation } from "./Review";

const caseName = (s: string) => s.toLowerCase().replace(/[^a-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 80) || "test";
const size = (c: TestCase) => (c.persona ? "played persona" : `${c.messages?.length ?? 0} messages`);
const expects = (c: TestCase) => Object.entries(c.expect ?? {}).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join(" · ");

// Tests kept with the agent: save a conversation as one, draft them from the policy, run them all on the current build.
// What a test expects comes from code (how the conversation ended, what the policy covers), never from the model.
export default function Tests({ agent, conversations }: { agent: string | null; conversations: Conversation[] }) {
  const [saved, setSaved] = useState<{ agent: string; tests: AgentTest[] } | null>(null);
  const [run, setRun] = useState<{ agent: string; run: TestRun } | null>(null);
  const [draft, setDraft] = useState<{ agent: string; draft: TestDraft } | null>(null);
  const [busy, setBusy] = useState<"run" | "draft" | null>(null);
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
    setBusy("run"); setMsg(null);
    try { setRun({ agent, run: await api.runTests(agent, ids) }); } catch (e) { fail(e); } finally { setBusy(null); }
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
  const passed = results?.results.filter((r) => r.passed).length ?? 0;

  return (
    <div className="review">
      <div className="field-row">
        <button className="primary" onClick={() => runAll()} disabled={!!busy || !tests.length}>{busy === "run" ? "Running… (about a minute per test)" : `Run ${tests.length} test${tests.length === 1 ? "" : "s"}`}</button>
        <button className="ghost" onClick={draftAll} disabled={!!busy} title="One test per thing the policy should handle: the flow, bad answers, each rule, scope, handoff, languages">
          {busy === "draft" ? "Drafting… (a few minutes)" : "Draft tests from the policy"}</button>
        <button className="ghost compact" onClick={download} disabled={!tests.length}>Download replay.yaml</button>
      </div>
      <p className="muted small">Tests run on the agent you built, on sample tools, like a simulation. A test passes when what it expects holds and every
        conversation check passes: no errors, loops or broken limits{results && !results.judge ? "" : ", replies in scope, rules followed"}.</p>
      {results && !results.judge && <p className="muted small">Judged checks (scope, rules) are off: set <code>EVALS_JUDGE_MODEL</code> on the server to add them.</p>}
      {results && <div className={`callout ${passed === results.results.length ? "ok" : "err"}`} role="status">{passed} of {results.results.length} passed on policy <code>{results.policy_hash}</code>.</div>}
      {msg && <div className={`callout ${msg.kind}`} role="status">{msg.text}</div>}

      {drafted && drafted.cases.length > 0 && (
        <section>
          <h3 className="review-h">Drafted: keep the ones that test what you want</h3>
          {drafted.cases.map((c) => (
            <div key={c.name} className="review-turn">
              <div className="row-between"><b>{c.name}</b>{drafted.failures[c.name] ? <span className="tag fail">fails now</span> : <span className="tag pass">passes</span>}</div>
              <span className="muted small">{size(c)}{expects(c) && ` · expects ${expects(c)}`}{c.note && ` · ${c.note}`}</span>
              {(drafted.failures[c.name] ?? []).map((f) => <span key={f} className="small">{f}</span>)}
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

      <h3 className="review-h">Tests for {agent}</h3>
      {tests.length === 0 ? <p className="muted small">None yet. Save a conversation below, or draft tests from the policy.</p> : tests.map((t) => {
        const r = byId.get(t.id);
        return (
          <div key={t.id} className="handoff">
            <button className="handoff-head" onClick={() => setOpen(open === t.id ? null : t.id)} aria-expanded={open === t.id}>
              {r ? <span className={`badge ${r.passed ? "ok" : "failed"}`}>{r.passed ? "pass" : "fail"}</span> : <span className="badge info">not run</span>}
              <b>{t.case.name}</b>
              <span className="muted small">{t.source} · {size(t.case)}{expects(t.case) && ` · expects ${expects(t.case)}`}</span>
            </button>
            {open === t.id && (
              <div className="handoff-body">
                {t.case.note && <p className="muted small">{t.case.note}</p>}
                {r?.problems.map((p) => <div key={p} className="callout err small">{p}</div>)}
                <div className="transcript">
                  {r?.turns.length ? r.turns.flatMap((x, i) => [
                    <div key={`u${i}`} className="line user"><span className="muted small">User</span> {x.user}</div>,
                    <div key={`a${i}`} className="line agent"><span className="muted small">Agent</span> {x.agent}{x.rules.length > 0 && <span className="tag">{x.rules.join(", ")}</span>}</div>,
                  ]) : (t.case.messages ?? []).map((m, i) => <div key={i} className="line user"><span className="muted small">User</span> {m}</div>)}
                </div>
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
