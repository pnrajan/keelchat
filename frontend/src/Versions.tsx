// Saved versions of the agent being edited: open one in the editor, compare it with the editor text, activate it
// (which is also how to roll back). Running sessions keep the version they started on.
import { diffLines } from "diff";
import { useEffect, useState } from "react";
import { api, BotType, PolicyVersion } from "./api";

type Props = {
  agent: string | null; current: string; onOpen: (yaml: string) => void; onActivated: (v: PolicyVersion) => void;
  onCopied: (v: PolicyVersion) => void; onTemplate: (t: BotType) => void;
};
const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9-]/g, "-");

export default function Versions({ agent, current, onOpen, onActivated, onCopied, onTemplate }: Props) {
  const [copyName, setCopyName] = useState("");
  const [typeName, setTypeName] = useState("");
  const [versions, setVersions] = useState<PolicyVersion[] | null>(null);
  const [compare, setCompare] = useState<PolicyVersion | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (agent) api.versions(agent).then(setVersions, (e) => setError((e as Error).message));
  }, [agent]);

  if (!agent) return <div className="empty">Name the agent in the editor (`agent:`) to see its saved versions.</div>;
  if (error) return <div className="callout err">{error}</div>;
  if (!versions) return <div className="empty">Loading…</div>;
  if (!versions.length) return <div className="empty">{agent} has no saved versions yet. Save one from the policy panel.</div>;

  const withYaml = async (v: PolicyVersion) => (v.yaml !== undefined ? v : api.version(v.agent, v.version));
  const act = async (fn: () => Promise<void>) => { setError(null); try { await fn(); } catch (e) { setError((e as Error).message); } };

  return (
    <div className="versions">
      <section className="reuse">
        <div className="field-row">
          <input id="copy-name" className="grow" value={copyName} onChange={(e) => setCopyName(slug(e.target.value))} placeholder={`${agent}-copy`} aria-label="Name for the copy" maxLength={63} />
          <button className="compact" disabled={!copyName} onClick={() => act(async () => { onCopied(await api.copyAgent(agent, copyName)); setCopyName(""); })}
            title="A new agent with the active version's policy, as its v1">Duplicate</button>
        </div>
        <div className="field-row">
          <input id="type-name" className="grow" value={typeName} onChange={(e) => setTypeName(e.target.value)} placeholder="Bot type name, e.g. Returns desk" aria-label="Bot type name" maxLength={80} />
          <button className="compact" disabled={!typeName.trim() || !slug(typeName.trim()).replace(/-/g, "")}
            onClick={() => act(async () => { onTemplate(await api.saveTemplate(slug(typeName.trim()).replace(/^-+|-+$/g, ""), typeName.trim(), `Based on ${agent}`, agent)); setTypeName(""); })}
            title="Offer this agent's active policy as a starting point under New agent">Save as bot type</button>
        </div>
      </section>
      <ol className="version-list">
        {versions.map((v) => (
          <li key={v.version} className={v.active ? "active" : ""}>
            <div className="row-between">
              <strong>v{v.version}{v.active && <span className="badge ok">active</span>}</strong>
              <span className="muted small" title={v.created_at}>{new Date(v.created_at).toLocaleString()}</span>
            </div>
            <p className="small">{v.note || <span className="muted">no note</span>} <code className="muted">{v.hash}</code></p>
            <div className="field-row">
              <button className="compact" onClick={() => act(async () => onOpen((await withYaml(v)).yaml ?? ""))}>Open</button>
              <button className="compact" onClick={() => act(async () => setCompare(await withYaml(v)))}>Compare</button>
              {!v.active && <button className="compact" onClick={() => act(async () => {
                const a = await api.activate(v.agent, v.version);
                setVersions(versions.map((x) => ({ ...x, active: x.version === a.version })));
                onActivated(a);
              })}>{v.version < Math.max(...versions.map((x) => x.version)) ? "Roll back to this" : "Activate"}</button>}
            </div>
          </li>
        ))}
      </ol>
      {compare && (
        <section>
          <div className="row-between"><h3>v{compare.version} → editor</h3><button className="ghost compact" onClick={() => setCompare(null)}>Close</button></div>
          <pre className="code diff">{diffLines(compare.yaml ?? "", current).map((part, i) => (
            <span key={i} className={part.added ? "add" : part.removed ? "del" : ""}>
              {part.value.replace(/\n$/, "").split("\n").map((l) => `${part.added ? "+" : part.removed ? "-" : " "} ${l}`).join("\n") + "\n"}
            </span>
          ))}</pre>
          {diffLines(compare.yaml ?? "", current).every((p) => !p.added && !p.removed) && <p className="muted small">No differences.</p>}
        </section>
      )}
    </div>
  );
}
