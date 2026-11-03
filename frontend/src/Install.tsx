// Channels: every place an agent can go, in one list, with what to paste or set for each.
import { useCallback, useEffect, useState } from "react";
import { api, CampaignRec, EmbedRec, ShopRec } from "./api";
import { Channels, Copy } from "./Reach";

const PLACES = [
  { id: "orders", label: "Order emails", note: "Shopify or Klaviyo, with 0-10 buttons", tag: "surveys" },
  { id: "shopify", label: "Shopify", note: "Paste one line into your theme", tag: "store" },
  { id: "wordpress", label: "WordPress", note: "One Custom HTML block", tag: "website" },
  { id: "website", label: "Any website", note: "Wix, Squarespace, Webflow, your own", tag: "website" },
  { id: "app", label: "Link and phone app", note: "Share by email, QR code or bio", tag: "link" },
  { id: "whatsapp", label: "WhatsApp", note: "Your business number", tag: "paid plans" },
  { id: "phone", label: "Phone calls", note: "Customers speak instead of typing", tag: "voice" },
  { id: "developers", label: "API and MCP", note: "Keys, tools and Claude", tag: "developers" },
];

/** A survey link for the order emails a store already sends (Shopify notifications, a Klaviyo flow): 0-10 buttons
 * that open the survey with the score given, labelled with the product and where it came from. Nothing personal goes
 * in the link. */
export function OrderEmail({ bot }: { bot: EmbedRec }) {
  const [from, setFrom] = useState<"shopify" | "klaviyo">("shopify");
  const product = from === "shopify" ? "&product={{ line_items.first.product.title | url_encode }}" : "";
  const link = (n: number | null) => `${bot.app_url}?${n === null ? "" : `score=${n}&`}source=${from}${product}`;
  const buttons = [
    `<p style="font:16px sans-serif">How likely are you to recommend us to a friend?</p>`,
    `<table role="presentation" cellspacing="0" cellpadding="0"><tr>`,
    ...Array.from({ length: 11 }, (_, n) =>
      `<td style="padding:2px"><a href="${link(n)}" style="display:inline-block;width:32px;padding:8px 0;text-align:center;border:1px solid #ccc;border-radius:6px;color:#111;text-decoration:none;font:14px sans-serif">${n}</a></td>`),
    `</tr></table>`,
  ].join("\n");
  return (
    <div className="order-email">
      <div className="order-tabs" role="tablist">
        {(["shopify", "klaviyo"] as const).map((f) => (
          <button key={f} type="button" className={from === f ? "on" : ""} onClick={() => setFrom(f)}>{f === "shopify" ? "Shopify" : "Klaviyo"}</button>
        ))}
      </div>
      {from === "shopify"
        ? <p className="small">In Shopify: Settings, then Notifications, then <b>Shipping confirmation</b> (or Delivery). Paste the buttons below where the survey should go and save. Each customer's product is filled in by Shopify.</p>
        : <p className="small">In Klaviyo: open the email in your <b>Fulfilled Order</b> flow, add an HTML block and paste the buttons below.</p>}
      <label className="small">0–10 buttons for the email <Copy text={buttons} /></label>
      <pre className="order-snippet">{buttons}</pre>
      <label className="small">Or a plain link <Copy text={link(null)} /></label>
      <pre className="order-snippet">{link(null)}</pre>
      <p className="muted small">Results are split by product and by where they came from. Survey answers count toward your monthly replies.</p>
    </div>
  );
}

export default function Install({ onNew }: { onNew: () => void }) {
  const [bots, setBots] = useState<EmbedRec[] | null>(null);
  const [botId, setBotId] = useState("");
  const [place, setPlace] = useState(location.hash.startsWith("#install:") ? "shopify" : "");
  const refresh = useCallback(() => api.embeds().then((l) => { setBots(l); setBotId((b) => b || l[0]?.id || ""); }, () => setBots([])), []);
  useEffect(() => { refresh(); }, [refresh]);
  const bot = bots?.find((b) => b.id === botId) ?? null;

  return (
    <main className="dash install">
      <div className="dash-head"><div>
        <h1>Install</h1>
        <p className="muted small">Every place your chatbot can go. Pick one; most take a minute.</p>
      </div></div>
      {bots && !bots.length ? (
        <div className="callout">You don't have a chatbot yet. <button className="primary compact" type="button" onClick={onNew}>Create one</button></div>
      ) : (
        <label className="install-bot">Chatbot
          <select id="install-bot" value={botId} onChange={(e) => setBotId(e.target.value)}>
            {(bots ?? []).map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
          </select>
        </label>
      )}
      <div className="platforms directory" role="list">
        {PLACES.map((p) => (
          <button key={p.id} role="listitem" type="button" className={`platform${place === p.id ? " on" : ""}`} onClick={() => setPlace(p.id)}
            disabled={p.id !== "developers" && !bot}>
            <span className="tag mono">{p.tag}</span><strong>{p.label}</strong><span className="muted small">{p.note}</span>
          </button>
        ))}
      </div>
      {place && (
        <section className="panel wizard">
          <h3>{PLACES.find((p) => p.id === place)?.label}</h3>
          {place === "orders" && bot && <OrderEmail bot={bot} />}
          {place === "developers" && <p>Create API keys in Settings, then see <a href="/developers/">the developer page</a> for the API, custom tools and the MCP server for Claude.</p>}
          {place !== "developers" && place !== "orders" && bot && <Channels e={bot} initial={place} fixed onChange={refresh} />}
        </section>
      )}
    </main>
  );
}
