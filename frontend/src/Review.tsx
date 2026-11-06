import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ReplyRating, Review as Saved, ReviewTurn } from "./api";

export type Conversation = { source: "chat" | "simulate"; id: string; label: string; policy_hash: string; turns: ReviewTurn[] };

type Verdicts = Record<string, boolean>; // `${turn}|${check}` → pass
const key = (turn: number, check: string) => `${turn}|${check}`;
const checksFor = (t: ReviewTurn) => ["in_scope", ...t.rules.map((r) => `rule_followed:${r}`)];
const label = (check: string) => (check === "in_scope" ? "In scope" : `Rule ${check.split(":")[1]}`);
const remember = (k: string, v?: string) => { try { if (v === undefined) return localStorage.getItem(k) ?? ""; localStorage.setItem(k, v); } catch { /* storage blocked */ } return v ?? ""; };

// Rate a conversation from the test chat or a simulation: each reply in scope or not, each matched rule followed or
// not, and an overall score. Ratings become labels for the judge (labels.yaml) and replay cases for what failed.
export default function Review({ agent, conversations }: { agent: string | null; conversations: Conversation[] }) {
  const [pick, setPick] = useState<string>("");
  const [verdicts, setVerdicts] = useState<Verdicts>({});
  const [notes, setNotes] = useState<Record<number, string>>({});
  const [score, setScore] = useState<number | null>(null);
  const [note, setNote] = useState("");
  const [rater, setRater] = useState(() => remember("yachat.rater"));
  const [id, setId] = useState<string | undefined>();
  const [saved, setSaved] = useState<Saved[]>([]);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const conv = conversations.find((c) => `${c.source}:${c.id}` === pick) ?? conversations[0];
  const load = useCallback(() => { if (agent) api.reviews(agent).then(setSaved, (e: Error) => setMsg({ kind: "err", text: e.message })); }, [agent]);
  useEffect(load, [load]);

  // switching conversation: start fresh, or reopen this conversation's saved rating to change it
  useEffect(() => {
    const prev = conv && saved.find((r) => r.source === conv.source && r.conversation === conv.id && r.turns.length === conv.turns.length);
    setVerdicts(Object.fromEntries((prev?.replies ?? []).map((x) => [key(x.turn, x.check), x.ok])));
    setNotes(Object.fromEntries((prev?.replies ?? []).filter((x) => x.note).map((x) => [x.turn, x.note ?? ""])));
    setScore(prev?.score ?? null); setNote(prev?.note ?? ""); setId(prev?.id);
  }, [conv?.source, conv?.id, conv?.turns.length, saved]);

  const failures = useMemo(() => {
    const by: Record<string, { failed: number; rated: number }> = {};
    for (const r of saved) for (const x of r.replies) {
      const b = (by[x.check] ??= { failed: 0, rated: 0 });
      b.rated += 1; if (!x.ok) b.failed += 1;
    }
    return Object.entries(by).sort((a, b) => b[1].failed - a[1].failed);
  }, [saved]);
  const scored = saved.filter((r) => r.score !== null);

  if (!agent) return <div className="empty">Build a policy first: reviews belong to the agent you built.</div>;
  if (!conv) return <div className="empty">Chat with the agent or run a simulation, then rate the conversation here.</div>;

  const set = (turn: number, check: string, ok: boolean) =>
    setVerdicts((v) => { const k = key(turn, check); const n = { ...v }; if (n[k] === ok) delete n[k]; else n[k] = ok; return n; });
  const replies: ReplyRating[] = Object.entries(verdicts).map(([k, ok]) => {
    const [turn, check] = [Number(k.split("|")[0]), k.slice(k.indexOf("|") + 1)];
    return { turn, check, ok, ...(notes[turn] ? { note: notes[turn] } : {}) };
  });

  async function save() {
    if (!agent || !conv) return;
    remember("yachat.rater", rater);
    try {
      const r = await api.saveReview(agent, { id, policy_hash: conv.policy_hash, source: conv.source, conversation: conv.id, turns: conv.turns, replies, score, note, rater });
      setId(r.id); setMsg({ kind: "ok", text: `Saved: ${replies.length} replies rated${score ? `, ${score}/5` : ""}.` }); load();
    } catch (e) { setMsg({ kind: "err", text: (e as Error).message }); }
  }

  async function download(kind: "labels" | "replay") {
    if (!agent) return;
    try {
      const text = await api.reviewFile(agent, kind);
      const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(new Blob([text], { type: "application/yaml" })), download: `${agent}-${kind}.yaml` });
      a.click(); URL.revokeObjectURL(a.href);
    } catch (e) { setMsg({ kind: "err", text: (e as Error).message }); }
  }

  return (
    <div className="review">
      <div className="field-row">
        <select id="review-pick" aria-label="Conversation" value={`${conv.source}:${conv.id}`} onChange={(e) => { setPick(e.target.value); setMsg(null); }}>
          {conversations.map((c) => <option key={`${c.source}:${c.id}`} value={`${c.source}:${c.id}`}>{c.label} · {c.turns.length} turns</option>)}
        </select>
        {id && <span className="tag pass">rated before</span>}
      </div>
      <p className="muted small">Mark each reply. A rule appears only on the turns where it matched. Leave a box empty to skip it.</p>

      <div className="review-turns">
        {conv.turns.map((t, i) => {
          const turn = i + 1;
          return (
            <div key={turn} className="review-turn">
              <div className="line user"><span className="muted small">User</span> {t.user}</div>
              <div className="line agent"><span className="muted small">Agent</span> {t.agent}</div>
              <div className="rate-row">
                {checksFor(t).map((c) => {
                  const v = verdicts[key(turn, c)];
                  return (
                    <span key={c} className="rate" role="group" aria-label={label(c)}>
                      <span className="small">{label(c)}</span>
                      <button className={`chip${v === true ? " on-ok" : ""}`} aria-pressed={v === true} onClick={() => set(turn, c, true)}>Pass</button>
                      <button className={`chip${v === false ? " on-bad" : ""}`} aria-pressed={v === false} onClick={() => set(turn, c, false)}>Fail</button>
                    </span>
                  );
                })}
                <input id={`review-note-${turn}`} className="compact" placeholder="Why (optional)" maxLength={500} value={notes[turn] ?? ""}
                  onChange={(e) => setNotes((n) => ({ ...n, [turn]: e.target.value }))} />
              </div>
            </div>
          );
        })}
      </div>

      <div className="rate-row">
        <span className="small">Overall</span>
        {[1, 2, 3, 4, 5].map((n) => (
          <button key={n} className={`chip${score === n ? (n <= 2 ? " on-bad" : " on-ok") : ""}`} aria-pressed={score === n} onClick={() => setScore(score === n ? null : n)}>{n}</button>
        ))}
        <span className="muted small">1 bad · 5 great</span>
      </div>
      <textarea id="review-note" rows={2} maxLength={2000} placeholder="What was good or bad about this conversation (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
      <div className="field-row">
        <input id="review-rater" className="compact" placeholder="Your name" maxLength={80} value={rater} onChange={(e) => setRater(e.target.value)} />
        <button className="primary" onClick={save} disabled={!replies.length && score === null}>{id ? "Update rating" : "Save rating"}</button>
      </div>
      {msg && <div className={`callout ${msg.kind}`}>{msg.text}</div>}

      <h3 className="review-h">Ratings for {agent}</h3>
      {saved.length === 0 ? <p className="muted small">None saved yet.</p> : (
        <>
          <p className="muted small">
            {saved.length} conversation{saved.length === 1 ? "" : "s"} rated
            {scored.length > 0 && ` · average ${(scored.reduce((a, r) => a + (r.score ?? 0), 0) / scored.length).toFixed(1)}/5`}
          </p>
          {failures.length > 0 && (
            <table className="log"><tbody>
              {failures.map(([c, b]) => (
                <tr key={c} className={b.failed ? "bad" : ""}><td>{label(c)}</td><td><b>{b.failed}</b> failed of {b.rated} rated</td></tr>
              ))}
            </tbody></table>
          )}
          <p className="muted small">Failures that repeat point to the policy: reword a rule's <code>then</code>, tighten the charter's scope, or add a rule for what the notes describe.</p>
          <div className="field-row">
            <button className="ghost compact" onClick={() => download("labels")}>Download labels.yaml</button>
            <button className="ghost compact" onClick={() => download("replay")}>Download failed conversations as replay cases</button>
          </div>
          <p className="muted small">
            Save labels as <code>evals/{agent.replace(/-/g, "_")}/labels.yaml</code> and run <code>yachat evals calibrate</code> to see how often the judge agrees
            with you. Add the replay cases to <code>replay.yaml</code> so <code>yachat evals check</code> catches each failure again.
          </p>
        </>
      )}
    </div>
  );
}
