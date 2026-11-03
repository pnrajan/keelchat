import { useEffect, useState } from "react";
import { api, ApiKeyRec, tenant } from "./api";

const ACCESS: Record<string, { label: string; scopes: string[] }> = {
  runtime: { label: "Talk to bots (for your website or app backend)", scopes: ["runtime"] },
  read: { label: "Read only (reports, audit log)", scopes: ["read"] },
  build: { label: "Full access (create and change bots, issue keys)", scopes: ["build"] },
};
const EXPIRY = [7, 30, 90, 365];

function status(k: ApiKeyRec): string {
  if (k.revoked_at) return "revoked";
  if (k.expires_at && new Date(k.expires_at) <= new Date()) return "expired";
  return "active";
}
const day = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString() : "never");

/** API keys for this workspace: each is shown once, has an access level and an expiry, and can be revoked at once. */
export default function ApiKeys() {
  const [keys, setKeys] = useState<ApiKeyRec[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [access, setAccess] = useState("runtime");
  const [days, setDays] = useState(90);
  const [fresh, setFresh] = useState<{ name: string; key: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = () => api.keys().then((k) => { setKeys(k.slice().reverse()); setErr(null); }, (e: Error) => setErr(e.message));
  useEffect(() => { void load(); }, []);

  async function create(ev: React.FormEvent) {
    ev.preventDefault();
    if (!name.trim()) return setErr("Give the key a name, such as the site or app that will use it.");
    setBusy(true);
    try {
      const k = await api.createKey(name.trim(), ACCESS[access].scopes, days);
      setFresh({ name: k.name, key: k.key });
      setCopied(false);
      setName("");
      await load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function revoke(k: ApiKeyRec) {
    try { await api.revokeKey(k.id); await load(); } catch (e) { setErr((e as Error).message); }
  }

  async function copy(text: string) {
    try { await navigator.clipboard.writeText(text); setCopied(true); } catch { setCopied(false); }
  }

  return (
    <section className="panel">
      <h3>API keys</h3>
      <p className="muted small">
        Call your bots from your own code with <code>Authorization: Bearer &lt;key&gt;</code>. A key works for every bot in workspace <code>{tenant()}</code> and nothing else.
      </p>
      {err && <div className="callout err">{err}</div>}
      <form className="key-form" onSubmit={(e) => void create(e)}>
        <input aria-label="Key name" placeholder="Name, e.g. shop backend" value={name} maxLength={80} onChange={(e) => setName(e.target.value)} />
        <select aria-label="Access" value={access} onChange={(e) => setAccess(e.target.value)}>
          {Object.entries(ACCESS).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
        </select>
        <select aria-label="Expires" value={days} onChange={(e) => setDays(Number(e.target.value))}>
          {EXPIRY.map((d) => <option key={d} value={d}>Expires in {d} days</option>)}
        </select>
        <button className="primary" disabled={busy}>Create key</button>
      </form>
      {fresh && (
        <div className="callout ok">
          <strong>Copy “{fresh.name}” now.</strong> It is shown only once; afterwards only its id is kept.
          <div className="key-once"><code>{fresh.key}</code><button type="button" className="compact" onClick={() => void copy(fresh.key)}>{copied ? "copied" : "copy"}</button></div>
        </div>
      )}
      {keys && keys.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Name</th><th>Access</th><th>Created</th><th>Expires</th><th>Last used</th><th>Status</th><th /></tr></thead>
            <tbody>{keys.map((k) => (
              <tr key={k.id}>
                <td>{k.name}</td><td>{k.scopes.join(", ")}</td><td>{day(k.created_at)}</td><td>{day(k.expires_at)}</td><td>{day(k.last_used_at)}</td>
                <td>{status(k)}</td>
                <td className="r">{status(k) === "active" && <button className="compact" onClick={() => void revoke(k)}>Revoke</button>}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
      {keys && keys.length === 0 && <p className="muted small">No keys yet.</p>}
    </section>
  );
}
