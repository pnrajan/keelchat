import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ReplyRating, Review as Saved, ReviewTurn, Suggestion } from "./api";

/** `outcome`: how it ended, when known (a saved test can expect the same) */
export type Conversation = {
  source: "chat" | "simulate"; id: string; label: string; policy_hash: string; turns: ReviewTurn[];
  outcome?: { completed: boolean; exit_reason: string | null };
};

type Verdicts = Record<string, boolean>; // `${turn}|${check}` → pass
const key = (turn: number, check: string) => `${turn}|${check}`;
const checksFor = (t: ReviewTurn) => ["in_scope", ...t.rules.map((r) => `rule_followed:${r}`)];
const label = (check: string) => (check === "in_scope" ? "In scope" : `Rule ${check.split(":")[1]}`);
const remember = (k: string, v?: string) => { try { if (v === undefined) return localStorage.getItem(k) ?? ""; localStorage.setItem(k, v); } catch { /* storage blocked */ } return v ?? ""; };

// Rate a conversation from the test chat or a simulation: each reply in scope or not, each matched rule followed or
// not, and an overall score. Ratings become labels for the judge (labels.yaml) and replay cases for what failed.
type Props = { agent: string | null; conversations: Conversation[]; policy: string; onRevised: (yaml: string) => void };

export default function Review({ agent, conversations, policy, onRevised }: Props) {
  const [pick, setPick] = useState<string>("");
  const [verdicts, setVerdicts] = useState<Verdicts>({});
  const [notes, setNotes] = useState<Record<number, string>>({});
  const [score, setScore] = useState<number | null>(null);
  const [note, setNote] = useState("");
  const [rater, setRater] = useState(() => remember("yachat.rater"));
  const [id, setId] = useState<string | undefined>();
  // the agent whose ratings `saved` holds: a slow answer for an agent no longer shown is dropped
  const [saved, setSaved] = useState<{ agent: string; reviews: Saved[] } | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [suggestion, setSuggestion] = useState<{ agent: string; s: Suggestion } | null>(null);
  const [suggesting, setSuggesting] = useState(false);
  const current = useRef(agent);
  current.current = agent;

  const conv = conversations.find((c) => `${c.source}:${c.id}` === pick) ?? conversations[0];
  const convKey = conv ? `${conv.source}:${conv.id}:${conv.turns.length}` : "";
  const reviews = useMemo(() => (saved && saved.agent === agent ? saved.reviews : []), [saved, agent]);
  const loaded = !!saved && saved.agent === agent;
  const load = useCallback(() => {
    if (!agent) return;
    api.reviews(agent).then(
      (r) => { if (current.current === agent) setSaved({ agent, reviews: r }); },
      (e: Error) => setMsg({ kind: "err", text: e.message }),
    );
  }, [agent]);
  useEffect(load, [load]);

  // a conversation shown (or the ratings arriving): start fresh, or reopen its saved rating to change it. Reloading
  // after a save does not run this again, so clicks made meanwhile are kept.
  useEffect(() => {
    if (!loaded || !conv) return;
    const prev = reviews.find((r) => r.source === conv.source && r.conversation === conv.id && r.turns.length === conv.turns.length);
    setVerdicts(Object.fromEntries((prev?.replies ?? []).map((x) => [key(x.turn, x.check), x.ok])));
    setNotes(Object.fromEntries((prev?.replies ?? []).filter((x) => x.note).map((x) => [x.turn, x.note ?? ""])));
    setScore(prev?.score ?? null); setNote(prev?.note ?? ""); setId(prev?.id);
  }, [convKey, loaded]);

  const failures = useMemo(() => {
    const by: Record<string, { failed: number; rated: number }> = {};
    for (const r of reviews) for (const x of r.replies) {
      const b = (by[x.check] ??= { failed: 0, rated: 0 });
      b.rated += 1; if (!x.ok) b.failed += 1;
    }
    return Object.entries(by).sort((a, b) => b[1].failed - a[1].failed);
  }, [reviews]);
  const scored = reviews.filter((r) => r.score !== null);

  if (!agent) return <div className="empty">Build a policy first: reviews belong to the agent you built.</div>;
  if (!conv) return <div className="empty">Chat with the agent or run a simulation, then rate the conversation here.</div>;

  const set = (turn: number, check: string, ok: boolean) =>
    setVerdicts((v) => { const k = key(turn, check); const n = { ...v }; if (n[k] === ok) delete n[k]; else n[k] = ok; return n; });
  // a turn's note explains its failed checks (or every check, when none failed)
  const replies: ReplyRating[] = Object.entries(verdicts).map(([k, ok]) => {
    const [turn, check] = [Number(k.split("|")[0]), k.slice(k.indexOf("|") + 1)];
    const failedHere = Object.entries(verdicts).some(([k2, ok2]) => !ok2 && Number(k2.split("|")[0]) === turn);
    return { turn, check, ok, ...(notes[turn] && (!ok || !failedHere) ? { note: notes[turn] } : {}) };
  });

  async function save() {
    if (!agent || !conv) return;
    remember("yachat.rater", rater);
    try {
      const r = await api.saveReview(agent, { id, policy_hash: conv.policy_hash, source: conv.source, conversation: conv.id, turns: conv.turns, replies, score, note, rater });
      setId(r.id); setMsg({ kind: "ok", text: `Saved: ${replies.length} replies rated${score ? `, ${score}/5` : ""}.` }); load();
    } catch (e) { setMsg({ kind: "err", text: (e as Error).message }); }
  }

  async function suggest() {
    if (!agent) return;
    setSuggesting(true); setMsg(null);
    try { setSuggestion({ agent, s: await api.suggest(agent, policy) }); } catch (e) { setMsg({ kind: "err", text: (e as Error).message }); } finally { setSuggesting(false); }
  }
  const revised = suggestion && suggestion.agent === agent ? suggestion.s : null;

  async function download(kind: "labels" | "replay") {
    if (!agent) return;
    try {
      const text = await api.reviewFile(agent, kind);
      const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(new Blob([text], { type: "application/yaml" })), download: `${kind}.yaml` });
      document.body.appendChild(a); a.click(); a.remove();
      window.setTimeout(() => URL.revokeObjectURL(a.href), 0);
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
              <div className="transcript bare">
                <div className="line user"><span className="who">User</span><span>{t.user}</span></div>
                <div className="line agent"><span className="who">Agent</span><span>{t.agent}</span></div>
              </div>
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
                <input id={`review-note-${turn}`} className="compact" aria-label={`Why, for turn ${turn}`} placeholder="Why (optional)" maxLength={500} value={notes[turn] ?? ""}
                  onChange={(e) => setNotes((n) => ({ ...n, [turn]: e.target.value }))} />
              </div>
            </div>
          );
        })}
      </div>

      <div className="rate-row" role="group" aria-label="Overall score, 1 bad to 5 great">
        <span className="small">Overall</span>
        {[1, 2, 3, 4, 5].map((n) => (
          <button key={n} className={`chip${score === n ? (n <= 2 ? " on-bad" : " on-ok") : ""}`} aria-pressed={score === n} onClick={() => setScore(score === n ? null : n)}>{n}</button>
        ))}
        <span className="muted small">1 bad · 5 great</span>
      </div>
      <textarea id="review-note" aria-label="Note on the conversation" rows={2} maxLength={2000} placeholder="What was good or bad about this conversation (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
      <div className="field-row">
        <input id="review-rater" className="compact" aria-label="Your name" placeholder="Your name" maxLength={80} value={rater} onChange={(e) => setRater(e.target.value)} />
        <button className="primary" onClick={save} disabled={!replies.length && score === null}>{id ? "Update rating" : "Save rating"}</button>
      </div>
      {msg && <div className={`callout ${msg.kind}`} role="status">{msg.text}</div>}

      <h3 className="review-h">Ratings for {agent}</h3>
      {reviews.length === 0 ? <p className="muted small">None saved yet.</p> : (
        <>
          <p className="muted small">
            {reviews.length} conversation{reviews.length === 1 ? "" : "s"} rated
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
            <button onClick={suggest} disabled={suggesting} title="The model revises the policy in the editor to answer the failed replies and low scores; the build checks it">
              {suggesting ? "Suggesting…" : "Suggest policy changes"}</button>
          </div>
          {revised && (
            <div className={`callout ${revised.errors.length ? "err" : "ok"}`}>
              <strong>{revised.changes.length ? `${revised.changes.length} suggested change${revised.changes.length === 1 ? "" : "s"}` : "No change suggested"}</strong>
              {revised.changes.length > 0 && <ul>{revised.changes.map((c) => <li key={c}>{c}</li>)}</ul>}
              <p className="small">From {revised.feedback.length} reported problem{revised.feedback.length === 1 ? "" : "s"}.
                {revised.errors.length ? ` The revision still has ${revised.errors.length} build problem(s); they are marked in the editor.` : " The revision builds."}</p>
              {revised.changes.length > 0 && (
                <div className="field-row">
                  <button className="compact" onClick={() => onRevised(revised.yaml)}>Open in editor</button>
                  <span className="muted small">Replaces the editor text. Build and run the tests before you save it.</span>
                </div>
              )}
            </div>
          )}
          <div className="field-row">
            <button className="ghost compact" onClick={() => download("labels")}>Download labels.yaml</button>
            <button className="ghost compact" onClick={() => download("replay")}>Download failed conversations as replay cases</button>
          </div>
          <p className="muted small">
            Save labels as <code>labels.yaml</code> in the agent's eval folder (<code>evals/{agent.replace(/-/g, "_")}/</code>) and run <code>yachat evals calibrate</code> to see how often the judge agrees
            with you. Add the replay cases to <code>replay.yaml</code> so <code>yachat evals check</code> catches each failure again.
          </p>
        </>
      )}
    </div>
  );
}
