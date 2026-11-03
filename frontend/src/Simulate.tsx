import { useState } from "react";
import { api, SimConversation, SimResult } from "./api";

type Row = { id: number; name: string; kind: "persona" | "script"; text: string; maxTurns: number };

const STARTERS: Row[] = [
  { id: 1, name: "cooperative", kind: "persona", text: "A cooperative user who answers every question clearly and finishes the task.", maxTurns: 12 },
  { id: 2, name: "wants_human", kind: "persona", text: "An impatient user who agrees to share their answers, then insists on talking to a real person.", maxTurns: 6 },
  { id: 3, name: "off_topic", kind: "persona", text: "A chatty user who keeps asking unrelated questions and rarely answers.", maxTurns: 8 },
];
const OUTCOME: Record<string, string> = { completed: "ok", handed_off: "info", max_reasks: "failed", disengaged: "failed", declined: "partial" };

// Run simulated users against the agent you just built: scripted messages, or a persona the model plays. Fake tools,
// separate stores: nothing is submitted, sent to a helpdesk or written to the real audit log.
export default function Simulate({ agent }: { agent: string | null }) {
  const [rows, setRows] = useState<Row[]>(STARTERS);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<SimResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const set = (id: number, patch: Partial<Row>) => setRows((r) => r.map((x) => (x.id === id ? { ...x, ...patch } : x)));
  const add = () => setRows((r) => [...r, { id: Date.now(), name: `user_${r.length + 1}`, kind: "persona", text: "", maxTurns: 10 }]);

  async function run() {
    if (!agent) return;
    setRunning(true); setErr(null); setResult(null);
    const conversations: SimConversation[] = rows.filter((r) => r.text.trim()).map((r) => r.kind === "persona"
      ? { name: r.name, persona: r.text.trim(), max_turns: r.maxTurns }
      : { name: r.name, messages: r.text.split("\n").map((m) => m.trim()).filter(Boolean) });
    try { setResult(await api.simulate(agent, conversations)); } catch (e) { setErr((e as Error).message); } finally { setRunning(false); }
  }

  if (!agent) return <div className="empty">Build a policy first: the simulation runs against the agent you built.</div>;
  return (
    <div className="sim">
      <p className="muted small">Simulated users talk to <code>{agent}</code> as built. Personas are played by the model, so results vary between runs.</p>
      {rows.map((r) => (
        <div key={r.id} className="sim-row">
          <div className="field-row">
            <input id={`sim-name-${r.id}`} className="compact" value={r.name} aria-label="Name" maxLength={40}
              onChange={(e) => set(r.id, { name: e.target.value.replace(/[^A-Za-z0-9_-]/g, "_") })} />
            <select className="compact" aria-label="Kind" value={r.kind} onChange={(e) => set(r.id, { kind: e.target.value as Row["kind"] })}>
              <option value="persona">Persona (model plays it)</option><option value="script">Scripted messages</option>
            </select>
            {r.kind === "persona" && <label className="inline">max turns<input id={`sim-turns-${r.id}`} type="number" min={1} max={20} value={r.maxTurns}
              onChange={(e) => set(r.id, { maxTurns: Math.max(1, Math.min(20, Number(e.target.value) || 1)) })} /></label>}
            <button className="ghost compact" aria-label="Remove" onClick={() => setRows((x) => x.filter((y) => y.id !== r.id))}>Remove</button>
          </div>
          <textarea id={`sim-text-${r.id}`} rows={r.kind === "persona" ? 2 : 4} value={r.text} onChange={(e) => set(r.id, { text: e.target.value })}
            placeholder={r.kind === "persona" ? "Who this user is and what they want" : "One message per line"} aria-label="Persona or messages" />
        </div>
      ))}
      <div className="field-row">
        <button className="ghost compact" onClick={add} disabled={rows.length >= 10}>+ Add user</button>
        <button className="primary" onClick={run} disabled={running || !rows.some((r) => r.text.trim())}>{running ? "Running… (about a minute per user)" : `Run ${rows.length} simulation${rows.length === 1 ? "" : "s"}`}</button>
      </div>
      {err && <div className="callout err">{err}</div>}
      {result && (
        <>
          <div className="tiles">
            <div><span className="num big">{result.summary.sessions}</span><span className="muted small">conversations</span></div>
            <div><span className="num big">{Math.round(result.summary.completion_rate * 100)}%</span><span className="muted small">completed</span></div>
            <div><span className="num big">{Math.round(result.summary.handoff_rate * 100)}%</span><span className="muted small">handed to a person</span></div>
          </div>
          {result.conversations.map((c) => (
            <div key={c.name} className="handoff">
              <button className="handoff-head" onClick={() => setOpen(open === c.name ? null : c.name)} aria-expanded={open === c.name}>
                <span className={`badge ${OUTCOME[c.exit_reason ?? ""] ?? "partial"}`}>{c.exit_reason ?? "unfinished"}</span>
                <b>{c.name}</b>
                <span className="muted small">{c.turns} turns · {c.reasks} re-asks · {c.max_digressions} digressions · {c.llm_calls} model calls</span>
              </button>
              {open === c.name && (
                <div className="handoff-body">
                  {c.persona && <p className="muted small">{c.persona}</p>}
                  <div className="transcript">
                    {c.transcript.flatMap((t, i) => [
                      <div key={`u${i}`} className="line user"><span className="muted small">User</span> {t.user}</div>,
                      <div key={`a${i}`} className="line agent"><span className="muted small">Agent</span> {t.agent}</div>,
                    ])}
                  </div>
                  <details>
                    <summary className="muted small">{c.decisions.length} decisions</summary>
                    <table className="log"><tbody>
                      {c.decisions.map((d, i) => (
                        <tr key={i} className={d.ok === false ? "bad" : ""}><td><span className="tag">{d.kind}</span></td><td><b>{d.decision}</b></td>
                          <td>{[d.field && `field ${d.field}`, d.ref, d.reason].filter(Boolean).join(" · ")}</td></tr>
                      ))}
                    </tbody></table>
                  </details>
                </div>
              )}
            </div>
          ))}
        </>
      )}
    </div>
  );
}
