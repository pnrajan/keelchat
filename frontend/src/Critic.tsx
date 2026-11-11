// A stronger model judges a trial conversation: what went wrong on which turn, then fixes. Policy fixes go to the
// drafter and land in the editor; code fixes are kept as fix tasks that Claude Code picks up over MCP.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, Critique, Fix, FixTask } from "./api";
import type { Conversation } from "./Review";

type Props = { agent: string | null; conversations: Conversation[]; policy: string; onRevised: (yaml: string) => void };
type Msg = { kind: "ok" | "err"; text: string };

export default function Critic({ agent, conversations, policy, onRevised }: Props) {
  const [model, setModel] = useState<string | null | undefined>(undefined);
  const [pick, setPick] = useState("");
  // the verdict and the conversation it is about: a different pick or a longer chat shows it as out of date
  const [verdict, setVerdict] = useState<{ key: string; c: Critique } | null>(null);
  const [busy, setBusy] = useState<"judge" | "apply" | null>(null);
  const [chosen, setChosen] = useState<Set<number>>(new Set());
  const [sent, setSent] = useState<Record<number, string>>({});
  const [tasks, setTasks] = useState<FixTask[]>([]);
  const [msg, setMsg] = useState<Msg | null>(null);
  const current = useRef(agent);
  current.current = agent;

  useEffect(() => { api.critic().then((r) => setModel(r.model), () => setModel(null)); }, []);
  const loadTasks = useCallback(() => {
    if (!agent) return;
    api.fixes(agent, "open").then((t) => { if (current.current === agent) setTasks(t); }, () => setTasks([]));
  }, [agent]);
  useEffect(loadTasks, [loadTasks]);

  const conv = conversations.find((c) => `${c.source}:${c.id}` === pick) ?? conversations[0];
  const convKey = conv ? `${agent}:${conv.source}:${conv.id}:${conv.turns.length}` : "";
  const c = verdict?.c ?? null;
  const stale = !!verdict && verdict.key !== convKey;
  const policyFixes = c ? c.fixes.map((f, i) => [f, i] as const).filter(([f]) => f.kind === "policy") : [];
  const codeFixes = c ? c.fixes.map((f, i) => [f, i] as const).filter(([f]) => f.kind === "code") : [];

  if (model === undefined) return <p className="muted small">Checking…</p>;
  if (!model) return (
    <div className="callout">
      <strong>No judge model set.</strong>
      <p className="small">Set <code>KEELCHAT_MODEL_CRITIC</code> to a stronger model than the agent uses (for example <code>claude-opus-5-5</code>) and restart the server. It reads a trial conversation, says what went wrong and proposes fixes.</p>
    </div>
  );
  if (!agent || !conv) return <p className="muted small">Build the agent and have a conversation in the chat (or run a simulation), then judge it here.</p>;

  async function judge() {
    if (!agent || !conv) return;
    setBusy("judge"); setMsg(null); setSent({});
    try {
      const r = await api.critique(agent, conv.turns, policy);
      setVerdict({ key: convKey, c: r });
      setChosen(new Set(r.fixes.map((f, i) => (f.kind === "policy" ? i : -1)).filter((i) => i >= 0)));
    } catch (e) { setMsg({ kind: "err", text: (e as Error).message }); }
    finally { setBusy(null); }
  }

  async function apply() {
    const picked = policyFixes.filter(([, i]) => chosen.has(i)).map(([f]) => f);
    if (!picked.length) return;
    setBusy("apply"); setMsg(null);
    try {
      const r = await api.editPolicy(policy, picked.map((f) => `- ${f.title}: ${f.change}`).join("\n"));
      onRevised(r.yaml);
      setMsg({ kind: r.errors.length ? "err" : "ok", text: r.errors.length ? `Changed, with ${r.errors.length} build problem(s) marked in the editor.` : `${r.changes.length || picked.length} change(s) are in the editor. Build & try, then save.` });
    } catch (e) { setMsg({ kind: "err", text: (e as Error).message }); }
    finally { setBusy(null); }
  }

  async function send(f: Fix, i: number) {
    if (!agent || !conv || !c) return;
    const problem = c.findings.filter((x) => f.turns.includes(x.turn)).map((x) => `Turn ${x.turn}: ${x.problem}`).join("\n") || f.title;
    try {
      const t = await api.saveFix(agent, { title: f.title, problem, change: f.change, test: f.test, files: f.files, turns: conv.turns, policy_hash: conv.policy_hash });
      setSent((s) => ({ ...s, [i]: t.id })); loadTasks();
    } catch (e) { setMsg({ kind: "err", text: (e as Error).message }); }
  }

  async function close(t: FixTask, status: "done" | "dismissed") {
    try { await api.updateFix(t.id, { status }); loadTasks(); } catch (e) { setMsg({ kind: "err", text: (e as Error).message }); }
  }

  const turnText = (n: number) => conv.turns[n - 1];
  return (
    <div className="critic">
      <div className="field-row">
        <select aria-label="Conversation" value={`${conv.source}:${conv.id}`} onChange={(e) => { setPick(e.target.value); setMsg(null); }}>
          {conversations.map((x) => <option key={`${x.source}:${x.id}`} value={`${x.source}:${x.id}`}>{x.label} · {x.turns.length} turns</option>)}
        </select>
        <button className="primary" onClick={judge} disabled={busy !== null}>{busy === "judge" ? "Judging…" : c && !stale ? "Judge again" : "Judge this conversation"}</button>
      </div>
      <p className="muted small">Judged by <code>{model}</code>. It reads the conversation and the policy in the editor, finds what went wrong, and proposes fixes.</p>
      {msg && <div className={`callout ${msg.kind}`} role="status">{msg.text}</div>}

      {c && (
        <>
          {stale && <div className="callout">The conversation changed since this verdict. Judge again to include the new turns.</div>}
          <p className="critic-summary">{c.summary}</p>

          <h3 className="review-h">What went wrong</h3>
          {c.findings.length === 0 ? <p className="muted small">Nothing: every turn looks right.</p> : (
            <ul className="findings">{c.findings.map((f, k) => (
              <li key={k}>
                <span className={`badge sev-${f.severity}`}>{f.severity}</span>
                <div>
                  <p><b>Turn {f.turn}.</b> {f.problem}</p>
                  {turnText(f.turn) && c.findings.findIndex((x) => x.turn === f.turn) === k && <p className="muted small">User: “{turnText(f.turn).user}” · Agent: “{turnText(f.turn).agent}”</p>}
                </div>
              </li>
            ))}</ul>
          )}

          {policyFixes.length > 0 && (
            <section className="fix-group">
              <h3 className="review-h">Fix in the policy</h3>
              {policyFixes.map(([f, i]) => (
                <label key={i} className="check fix">
                  <input type="checkbox" checked={chosen.has(i)} onChange={() => setChosen((s) => { const n = new Set(s); if (n.has(i)) n.delete(i); else n.add(i); return n; })} />
                  <span><b>{f.title}</b><span className="muted small"> {f.change} (turn {f.turns.join(", ")})</span></span>
                </label>
              ))}
              <div className="field-row">
                <button onClick={apply} disabled={busy !== null || !policyFixes.some(([, i]) => chosen.has(i))}>{busy === "apply" ? "Changing the policy…" : "Apply to the policy"}</button>
                <span className="muted small">Changes the YAML in the editor; nothing is saved.</span>
              </div>
            </section>
          )}

          {codeFixes.length > 0 && (
            <section className="fix-group">
              <h3 className="review-h">Fix in code</h3>
              <p className="muted small">The policy can't express these. Send one to Claude Code: it shows up there as a fix task, and Claude Code can open a GitHub issue for it.</p>
              {codeFixes.map(([f, i]) => (
                <div key={i} className="fix-card">
                  <p><b>{f.title}</b> <span className="muted small">(turn {f.turns.join(", ")})</span></p>
                  <p className="small">{f.change}</p>
                  {f.test && <p className="small"><span className="muted">Test: </span>{f.test}</p>}
                  {f.files.length > 0 && <p className="small muted">{f.files.map((x) => <code key={x} className="file">{x}</code>)}</p>}
                  {sent[i] ? <span className="tag pass">sent as {sent[i]}</span> : <button className="compact" onClick={() => send(f, i)}>Send to Claude Code</button>}
                </div>
              ))}
            </section>
          )}
        </>
      )}

      <h3 className="review-h">Open code fixes for {agent}</h3>
      {tasks.length === 0 ? <p className="muted small">None.</p> : (
        <>
          <p className="muted small">In Claude Code with the keelchat plugin, run <code>/keelchat:fix-findings</code>: it picks these up, writes a failing test, fixes the code in its own branch, and can file each as a GitHub issue.</p>
          <ul className="fix-tasks">{tasks.map((t) => (
            <li key={t.id}>
              <div><p><b>{t.title}</b></p><p className="muted small"><code>{t.id}</code>{t.issue && <> · <a href={t.issue} target="_blank" rel="noreferrer">issue</a></>}</p></div>
              <span className="step-actions">
                <button className="compact ghost" onClick={() => close(t, "done")}>Done</button>
                <button className="compact ghost" onClick={() => close(t, "dismissed")}>Dismiss</button>
              </span>
            </li>
          ))}</ul>
        </>
      )}
    </div>
  );
}
