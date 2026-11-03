/* yachat website widget.
   <script src="https://YOUR-HOST/widget.js" data-agent="pk_..." async></script>
   Options: data-position="left|right", data-label="Chat with us", data-open="true".
   The chat runs in an iframe from YOUR-HOST: the host page's styles cannot change it and its scripts cannot read it. */
(() => {
  const me = document.currentScript;
  if (!me || !me.dataset.agent || window.__yachatWidget) return;
  window.__yachatWidget = true;
  const origin = new URL(me.src).origin;
  start(me.dataset.agent);

  function start(agent) {
  const left = me.dataset.position === "left";
  const side = left ? "left" : "right";

  const root = document.createElement("div");
  root.setAttribute("data-yachat", "");
  const shadow = root.attachShadow({ mode: "closed" });
  shadow.innerHTML = `
    <style>
      :host { all: initial; }
      .btn { position: fixed; ${side}: 20px; bottom: 20px; z-index: 2147483000; display: flex; align-items: center; gap: 8px;
        font: 600 14px -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; color: #fff; background: #0b0c0c;
        border: 0; border-radius: 999px; padding: 13px 18px; cursor: pointer; box-shadow: 0 10px 30px rgba(0,0,0,.25); }
      .btn:focus-visible { outline: 2px solid #0b0c0c; outline-offset: 3px; }
      .btn svg { width: 18px; height: 18px; }
      .panel { position: fixed; ${side}: 20px; bottom: 84px; z-index: 2147483000; width: min(400px, calc(100vw - 40px));
        height: min(640px, calc(100vh - 120px)); border-radius: 18px; overflow: hidden; box-shadow: 0 24px 60px rgba(0,0,0,.3);
        border: 1px solid rgba(0,0,0,.08); background: #fff; }
      .panel[hidden] { display: none; }
      iframe { width: 100%; height: 100%; border: 0; display: block; }
      @media (max-width: 480px) { .panel { ${side}: 0; bottom: 0; width: 100vw; height: 100%; border-radius: 0; } }
    </style>
    <div class="panel" hidden></div>
    <button class="btn" type="button" aria-expanded="false">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 21l1.9-5.4A8 8 0 1 1 21 12z"/></svg>
      <span></span>
    </button>`;
  const btn = shadow.querySelector(".btn"), panel = shadow.querySelector(".panel");
  btn.querySelector("span").textContent = me.dataset.label || "Chat with us";
  let frame = null;
  function toggle(open) {
    if (open && !frame) {
      frame = document.createElement("iframe");
      frame.src = `${origin}/embed/${encodeURIComponent(agent)}`;
      frame.title = "Chat";
      frame.allow = "clipboard-write; microphone";
      panel.appendChild(frame);
    }
    panel.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
  }
  btn.addEventListener("click", () => toggle(panel.hidden));
  window.addEventListener("message", (e) => { if (e.origin === origin && e.data && e.data.yachat === "close") toggle(false); });
  (document.body || document.documentElement).appendChild(root);
  if (me.dataset.open === "true") toggle(true);
  }
})();
