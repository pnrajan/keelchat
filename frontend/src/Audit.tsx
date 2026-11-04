import { useCallback, useEffect, useState } from "react";
import { api, AuditEvent, AuditSummary } from "./api";

const KINDS = ["", "turn", "answer", "count", "step", "exit", "handoff", "guardrail", "tool"];
const pct = (x: number) => `${Math.round(x * 100)}%`;
const top = (m: Record<string, number>) => Object.entries(m).sort((a, b) => b[1] - a[1]);

function Counts({ title, data, empty }: { title: string; data: Record<string, number>; empty: string }) {
  const rows = top(data);
  const max = Math.max(1, ...rows.map((r) => r[1]));
  return (
    <div className="counts">
      <h4>{title}</h4>
      {rows.length === 0 ? <p className="muted small">{empty}</p> : rows.map(([k, n]) => (
        <div key={k} className="count-row">
          <span>{k.replace(/_/g, " ")}</span>
          <span className="count-bar"><span style={{ width: `${(n / max) * 100}%` }} /></span>
          <span className="num">{n}</span>
        </div>
      ))}
    </div>
  );
}

// Outcomes and the decision log: where conversations end, why they reach a person, which questions fail.
export default function Audit({ agent, session }: { agent: string | null; session: string | null }) {
  const [scope, setScope] = useState<"agent" | "session">(session ? "session" : "agent");
  const [kind, setKind] = useState("");
  const [summary, setSummary] = useState<AuditSummary | null>(null);
  const [events, setEvents] = useState<AuditEvent[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(() => {
    const q = scope === "session" && session ? { session_id: session } : { agent: agent ?? undefined };
    Promise.all([api.auditSummary(agent ?? undefined), api.audit({ ...q, kind: kind || undefined })])
      .then(([s, e]) => { setSummary(s); setEvents(e); setErr(null); }, (e: Error) => setErr(e.message));
  }, [agent, session, scope, kind]);
  useEffect(load, [load]);

  return (
    <div className="audit">
      {err && <div className="callout err">{err}</div>}
      {summary && (
        <>
          <div className="tiles">
            <div><span className="num big">{summary.sessions}</span><span className="muted small">conversations</span></div>
            <div><span className="num big">{pct(summary.completion_rate)}</span><span className="muted small">completed</span></div>
            <div><span className="num big">{pct(summary.handoff_rate)}</span><span className="muted small">handed to a person</span></div>
          </div>
          <p className="muted small">{agent ? <>Agent <code>{agent}</code></> : "All agents"}, last 90 days.</p>
          <div className="counts-grid">
            <Counts title="How conversations ended" data={summary.outcomes} empty="None finished yet." />
            <Counts title="Why they went to a person" data={summary.handoff_reasons} empty="No handoffs." />
            <Counts title="Rejected answers by question" data={summary.rejected_answers} empty="No rejected answers." />
            <Counts title="Guardrail blocks" data={summary.guardrails} empty="Nothing blocked." />
            <Counts title="Tool failures" data={summary.tool_failures} empty="No failures." />
            <Counts title="Handoff actions by people" data={summary.handoff_actions} empty="Nothing claimed or resolved yet." />
          </div>
        </>
      )}
      <div className="field-row">
        <h4 className="grow">Decision log</h4>
        <select className="compact" aria-label="Log scope" value={scope} onChange={(e) => setScope(e.target.value as "agent" | "session")}>
          <option value="agent">This agent</option>
          <option value="session" disabled={!session}>This session</option>
        </select>
        <select className="compact" aria-label="Kind" value={kind} onChange={(e) => setKind(e.target.value)}>
          {KINDS.map((k) => <option key={k} value={k}>{k || "all kinds"}</option>)}
        </select>
        <button className="ghost compact" onClick={load}>Refresh</button>
      </div>
      {events === null ? <div className="empty">Loading…</div> : events.length === 0 ? <div className="empty">No decisions recorded yet.</div> : (
        <div className="table-wrap">
          <table className="log">
            <thead><tr><th>When</th><th>Kind</th><th>Decision</th><th>Detail</th><th>By</th></tr></thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.id} className={e.ok === false ? "bad" : ""}>
                  <td className="num">{new Date(e.at).toLocaleTimeString()}</td>
                  <td><span className="tag">{e.kind}</span></td>
                  <td><b>{e.decision}</b></td>
                  <td>{[e.field && `field ${e.field}`, e.step && !e.field && `step ${e.step}`, e.ref, e.reason, e.text && `“${e.text}”`].filter(Boolean).join(" · ")}</td>
                  <td className="muted">{e.actor}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
