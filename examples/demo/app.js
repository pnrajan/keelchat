// The demo landing page: "Make a bot" drafts YAML from a description (POST /demo/draft); "Try this bot" puts the draft
// up to chat with (POST /demo/try); "Try a bot" chats through the public chat API, with the support assistant until then.
// ?describe=... (from the GitHub Pages site) sends that description straight away. Before a first draft, a few questions
// with options to click (POST /demo/clarify) fill in what the description leaves open.
let EMBED = document.body.dataset.embed;
const $ = (id) => document.getElementById(id);
const text = $("text"), send = $("send");
const PLACEHOLDER = {
  make: "Describe the bot you want",
  change: "Ask for a change, e.g. \"also ask for an email\"",
  try: "Message the support assistant",
  mine: "Message your bot",
};
let mode = "make", busy = false, yaml = "", session = null, suggested = [], mine = false;

function add(where, kind, body) {
  $(`${where}-empty`).hidden = true;
  const log = $(`${where}-msgs`);
  log.hidden = false;
  const el = document.createElement("div");
  if (kind === "bot") {
    el.className = "bot";
    el.innerHTML = '<div class="dot" aria-hidden="true">k</div><div class="msg"></div>';
    el.lastChild.textContent = body;
  } else {
    el.className = kind === "you" ? "msg you" : "note";
    el.textContent = body;
  }
  log.append(el);
  el.scrollIntoView({ block: "end", behavior: "smooth" });
  return el;
}

function typing(where) {
  const el = add(where, "bot", "");
  el.lastChild.className = "msg typing";
  el.lastChild.innerHTML = "<span></span><span></span><span></span>";
  return el.lastChild;
}

// "Working on it", the way chat apps show it: a spinner, a shimmering line, the seconds so far, and placeholder lines
// that draw themselves like a document being written. The server doesn't report progress, so nothing here claims a
// step: the line just changes from time to time, and says it's nearly there after a while. Returns the bubble to fill,
// and stop() to clear the timers.
function working(where, lines) {
  const bubble = add(where, "bot", "").lastChild;
  bubble.className = "msg work";
  bubble.setAttribute("role", "status");
  bubble.innerHTML = '<div class="now"><span class="spin" aria-hidden="true"></span><span class="label"></span><span class="time"></span></div>'
    + '<div class="sketch" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></div>';
  const label = bubble.querySelector(".label"), time = bubble.querySelector(".time");
  const start = Date.now();
  label.textContent = lines[0];
  const tick = setInterval(() => {
    const s = Math.floor((Date.now() - start) / 1000);
    time.textContent = s >= 3 ? `${s}s` : "";
    label.textContent = s >= 40 ? "Nearly there…" : lines[Math.floor(s / 6) % lines.length];
  }, 250);
  return { bubble, stop: () => clearInterval(tick) };
}

//: what the visitor reads while the model works: general, never a step the server hasn't reported
const ASKING = ["Thinking…", "Reading your description…", "Thinking about what your bot needs…"];
const DRAFTING = ["Writing your bot…", "Thinking…", "Putting it together…"];

async function post(path, body, token) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json", ...(token ? { authorization: "Bearer " + token } : {}) },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Something went wrong. Try again.");
  return data;
}

async function copy(value, button) {
  try { await navigator.clipboard.writeText(value); button.textContent = "Copied"; }
  catch { button.textContent = "Select and copy"; }
  setTimeout(() => (button.textContent = "Copy"), 1500);
}

function result(into, draft) {
  into.className = "msg result";
  into.replaceChildren($("result").content.cloneNode(true));
  into.querySelector("pre").textContent = draft.yaml;
  if (draft.errors.length) {
    const w = into.querySelector(".warn");
    w.hidden = false;
    w.textContent = "The build still reports: " + draft.errors.map((e) => `${e.path}: ${e.message}`).join("; ") + ". Ask for a fix below.";
  }
  needs(into.querySelector(".needs ul"), [...suggested, ...draft.needs]);
  const t = into.querySelector("[data-try]");
  t.disabled = draft.errors.length > 0;
  t.onclick = () => tryIt(draft.yaml, t);
  into.querySelector("[data-copy-yaml]").onclick = (e) => copy(draft.yaml, e.currentTarget);
  into.querySelector("[data-copy-steps]").onclick = (e) => copy(e.currentTarget.closest(".file").querySelector("pre").textContent, e.currentTarget);
  into.querySelector("[data-download]").onclick = () => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([draft.yaml], { type: "text/yaml" }));
    a.download = "my_bot.yaml";
    a.click();
    URL.revokeObjectURL(a.href);
  };
}

function needs(list, items) {
  list.closest(".needs").hidden = !items.length;
  for (const n of items) {
    const li = document.createElement("li");
    li.innerHTML = '<div><span class="name"></span></div><p class="why"></p><div class="chips"></div><div class="how"><span></span> · <a target="_blank" rel="noopener">how</a></div>';
    li.querySelector(".name").textContent = n.name;
    if (n.source === "suggested") li.firstChild.insertAdjacentHTML("beforeend", '<span class="tag">for your bot</span>');
    li.querySelector(".why").textContent = n.why;
    for (const o of n.options) li.querySelector(".chips").append(Object.assign(document.createElement("span"), { textContent: o }));
    li.querySelector(".how span").textContent = n.how;
    li.querySelector(".how a").href = n.docs;
    list.append(li);
  }
}

// The questions as chips: one choice (or several, when `multi`), or the visitor's own words. Resolves with the answers,
// or null to skip them.
function ask(into, questions, understood) {
  return new Promise((resolve) => {
    const answers = {};
    into.className = "msg ask";
    into.removeAttribute("role");
    into.replaceChildren();
    if (understood) into.append(Object.assign(document.createElement("p"), { textContent: understood }));
    into.append(Object.assign(document.createElement("p"), { className: "why", textContent: "A few choices so the bot fits. Pick what applies, or answer in your own words; skip any." }));
    for (const q of questions) {
      const set = document.createElement("fieldset");
      set.append(Object.assign(document.createElement("legend"), { textContent: q.ask }));
      if (q.why) set.append(Object.assign(document.createElement("p"), { className: "why", textContent: q.why }));
      const pick = Object.assign(document.createElement("div"), { className: "pick" });
      const buttons = q.options.map((o) => {
        const b = Object.assign(document.createElement("button"), { type: "button", textContent: o.label });
        b.setAttribute("aria-pressed", "false");
        b.onclick = () => {
          const on = b.getAttribute("aria-pressed") !== "true";
          if (!q.multi) buttons.forEach((x) => x.setAttribute("aria-pressed", "false"));
          b.setAttribute("aria-pressed", String(on));
          answers[q.id] = buttons.filter((x) => x.getAttribute("aria-pressed") === "true").map((x) => x.textContent);
          if (on && !q.multi) other.value = "";
        };
        return b;
      });
      const other = Object.assign(document.createElement("input"), { placeholder: "Or in your words", maxLength: 200 });
      other.setAttribute("aria-label", `${q.ask} (your own answer)`);
      other.oninput = () => {
        if (!q.multi) buttons.forEach((x) => x.setAttribute("aria-pressed", "false"));
        const picked = buttons.filter((x) => x.getAttribute("aria-pressed") === "true").map((x) => x.textContent);
        answers[q.id] = other.value.trim() ? [...picked, other.value.trim()] : picked;
      };
      pick.append(...buttons, other);
      set.append(pick);
      into.append(set);
    }
    const row = Object.assign(document.createElement("div"), { className: "row" });
    const go = Object.assign(document.createElement("button"), { type: "button", className: "solid", textContent: "Build my bot" });
    const skip = Object.assign(document.createElement("button"), { type: "button", className: "ghost", textContent: "Skip, just build it" });
    go.onclick = () => { go.disabled = skip.disabled = true; resolve(answers); };
    skip.onclick = () => { go.disabled = skip.disabled = true; resolve(null); };
    row.append(go, skip);
    into.append(row);
    into.scrollIntoView({ block: "end", behavior: "smooth" });
  });
}

async function make(message) {
  add("make", "you", message);
  let questions = [], understood = "", answers = null, w = null;
  try {
    if (!yaml) {  // a first draft: ask what the description leaves open
      w = working("make", ASKING);
      ({ questions, understood } = await post("/demo/clarify", { text: message }).catch(() => ({ questions: [], understood: "" })));
      w.stop();
      if (questions.length) {
        answers = await ask(w.bubble, questions, understood);  // sending stays off until they build or skip
        w = null;
      }
    }
    if (w) w.bubble.parentElement.remove();
    w = working("make", yaml ? ["Making your change…", "Thinking…"] : DRAFTING);
    const out = w.bubble;
    const draft = await post("/demo/draft", { text: message, yaml, ...(answers ? { questions, answers } : {}) });
    w.stop();
    yaml = draft.yaml;
    if (draft.suggested) suggested = draft.suggested;
    result(out, draft);
    text.placeholder = PLACEHOLDER.change;
  } catch (e) {
    if (w) { w.stop(); w.bubble.parentElement.remove(); }
    add("make", "note", e.message);
  }
}

async function tryIt(source, button) {
  if (busy) return;
  busy = true; button.disabled = true; button.textContent = "Starting…";
  try {
    const r = await post("/demo/try", { yaml: source });
    EMBED = r.embed; session = null; mine = true;
    $("try-msgs").replaceChildren();
    show("try");
    add("try", "note", `Now chatting with your bot (${r.agent}). Its tools answer with sample data. Say hello.`);
  } catch (e) {
    add("make", "note", e.message);
  }
  busy = false; button.disabled = false; button.textContent = "Try this bot"; update();
}

async function chat(message) {
  add("try", "you", message);
  const out = typing("try");
  try {
    session ??= await post(`/v1/public/embeds/${EMBED}/sessions`, {});
    const r = await post(`/v1/public/sessions/${session.session_id}/turns`, { text: message, message_id: crypto.randomUUID() }, session.token);
    out.className = "msg";
    out.textContent = r.text;
    if (r.end_of_conversation) {
      session = null;
      add("try", "note", "Conversation ended. Send a message to start a new one.");
    }
  } catch (e) {
    out.parentElement.remove();
    add("try", "note", e.message);
  }
}

async function say(message) {
  message = message.trim();
  if (!message || busy) return;
  busy = true; text.value = ""; resize(); update();
  await (mode === "try" ? chat(message) : make(message));
  busy = false; update(); text.focus();
}

function show(next) {
  mode = next;
  $("make").hidden = next !== "make";
  $("try").hidden = next !== "try";
  $("tab-make").setAttribute("aria-selected", String(next === "make"));
  $("tab-try").setAttribute("aria-selected", String(next === "try"));
  text.placeholder = next === "try" ? (mine ? PLACEHOLDER.mine : PLACEHOLDER.try) : yaml ? PLACEHOLDER.change : PLACEHOLDER.make;
  text.focus();
}

function resize() { text.style.height = "auto"; text.style.height = text.scrollHeight + "px"; }
function update() { send.disabled = busy || !text.value.trim(); }

text.addEventListener("input", () => { resize(); update(); });
text.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); say(text.value); } });
$("form").addEventListener("submit", (e) => { e.preventDefault(); say(text.value); });
document.querySelectorAll(".card").forEach((c) => c.addEventListener("click", () => say(c.dataset.say)));
$("tab-make").onclick = () => show("make");
$("tab-try").onclick = () => show("try");
$("show-yaml").onclick = (e) => { e.preventDefault(); $("yaml").showModal(); };
$("close-yaml").onclick = () => $("yaml").close();
$("yaml").addEventListener("click", (e) => { if (e.target === $("yaml")) $("yaml").close(); });
show(location.hash === "#try" ? "try" : "make");
const described = new URLSearchParams(location.search).get("describe");
if (described) { history.replaceState(null, "", location.pathname); show("make"); say(described.slice(0, 1000)); }
