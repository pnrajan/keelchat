import { Fragment, useCallback, useEffect, useState } from "react";
import { api, Handoff, HandoffStatus } from "./api";

const REASONS: Record<string, string> = {
  user_request: "Asked for a person",
  max_reasks: "Couldn't give a valid answer",
  disengaged: "Kept going off-topic",
  tool_unavailable: "A tool was down",
};
const ago = (iso: string) => {
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
};

// The handoff inbox: conversations passed to a person. Claim one, read what the user said, resolve it with a note.
export default function Handoffs({ focus, onChange }: { focus?: string | null; onChange?: () => void }) {
  const [status, setStatus] = useState<HandoffStatus | "">("open");
  const [rows, setRows] = useState<Handoff[] | null>(null);
  const [open, setOpen] = useState<string | null>(focus ?? null);
  const [me, setMe] = useState(() => { try { return localStorage.getItem("keelchat.assignee") ?? ""; } catch { return ""; } });
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(() => {
    api.handoffs(status || undefined).then((r) => { setRows(r); setErr(null); }, (e: Error) => setErr(e.message));
  }, [status]);
  useEffect(() => { load(); const t = window.setInterval(load, 10000); return () => window.clearInterval(t); }, [load]);
  useEffect(() => { if (focus) { setOpen(focus); setStatus(""); } }, [focus]);

  const resolve = (h: Handoff, resume: boolean) =>
    act(h, { status: "resolved", resume, ...(me.trim() ? { assignee: me.trim() } : {}), ...(note.trim() ? { note: note.trim() } : {}) });

  async function act(h: Handoff, change: { status?: HandoffStatus; assignee?: string; note?: string; resume?: boolean }) {
    try {
      await api.updateHandoff(h.id, change);
      try { if (change.assignee) localStorage.setItem("keelchat.assignee", change.assignee); } catch { /* per-browser convenience only */ }
      setNote(""); load(); onChange?.();
    } catch (e) { setErr((e as Error).message); }
  }

  return (
    <div className="inbox">
      <div className="field-row">
        <select className="compact" aria-label="Show handoffs" value={status} onChange={(e) => setStatus(e.target.value as HandoffStatus | "")}>
          <option value="open">Open</option><option value="claimed">Claimed</option><option value="resolved">Resolved</option><option value="">All</option>
        </select>
        <input id="handoff-me" className="grow compact" value={me} onChange={(e) => setMe(e.target.value)} placeholder="Your name or email (to claim)" aria-label="Your name" />
        <button className="ghost compact" onClick={load}>Refresh</button>
      </div>
      {err && <div className="callout err">{err}</div>}
      {rows === null ? <div className="empty">Loading…</div> : rows.length === 0 ? (
        <div className="empty">No {status || ""} handoffs. When a user asks for a person, or the agent reaches a dead end the policy lists under <code>handoff.when</code>, the conversation shows up here.</div>
      ) : rows.map((h) => (
        <div key={h.id} className={`handoff ${h.status}${open === h.id ? " expanded" : ""}`}>
          <button className="handoff-head" onClick={() => setOpen(open === h.id ? null : h.id)} aria-expanded={open === h.id}>
            <span className={`badge ${h.status === "open" ? "partial" : h.status === "claimed" ? "info" : "ok"}`}>{h.status}</span>
            <b>{REASONS[h.reason] ?? h.reason}</b>
            <span className="muted small">{h.agent} · {h.channel} · {ago(h.created_at)}{h.assignee ? ` · ${h.assignee}` : ""}</span>
          </button>
          {open === h.id && (
            <div className="handoff-body">
              <dl className="facts">
                <dt>Ticket</dt><dd><code>{h.id}</code>{h.external_id && <> · helpdesk <code>{h.external_id}</code></>}</dd>
                <dt>User</dt><dd><code>{h.user_id}</code> · session <code>{h.session_id}</code></dd>
                {Object.entries(h.fields).map(([k, v]) => <Fragment key={k}><dt>{k.replace(/_/g, " ")}</dt><dd>{v}</dd></Fragment>)}
                {h.note && <><dt>Note</dt><dd>{h.note}</dd></>}
              </dl>
              <div className="transcript">
                {h.transcript.map((l, i) => <div key={i} className={`line ${l.role}`}><span className="muted small">{l.role === "user" ? "User" : "Agent"}</span> {l.text}</div>)}
              </div>
              <div className="field-row">
                {h.status !== "claimed" && <button className="primary compact" disabled={!me.trim()} title={me.trim() ? "" : "Enter your name above"}
                  onClick={() => void act(h, { status: "claimed", assignee: me.trim() })}>Claim</button>}
                <input id={`note-${h.id}`} className="grow compact" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Note (what you did)" aria-label="Note" />
                {h.status !== "resolved"
                  ? <>
                    <button className="compact" title="Hand the conversation back to the assistant" onClick={() => void resolve(h, true)}>Resolve and resume</button>
                    <button className="compact" title="Close the conversation" onClick={() => void resolve(h, false)}>Resolve and end</button>
                  </>
                  : <button className="compact" disabled={!note.trim()} onClick={() => void act(h, { note: note.trim() })}>Add note</button>}
              </div>
              <p className="muted small">The user's later messages are added here. Resolve and resume hands the conversation back to the assistant at their next message; a helpdesk does the same through the callback URL it was sent.</p>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
