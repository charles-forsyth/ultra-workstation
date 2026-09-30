// Ultra client shell (v0.1). Vanilla JS module, no build step, no inline code (CSP).
// Helpers adapted from the deep-research dashboard (MIT, same author).

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

export function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.textContent = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), kind === "err" ? 7000 : 3500);
}

let TOKEN = "";
export async function api(path, { method = "GET", body } = {}) {
  const opt = { method, headers: {} };
  if (method !== "GET") {
    opt.headers["Content-Type"] = "application/json";
    opt.headers["X-Ultra-Token"] = TOKEN;
    opt.body = JSON.stringify(body ?? {});
  }
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

export async function busy(btn, fn) {
  if (!btn || btn.disabled) return;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "...";
  try { return await fn(); }
  catch (e) { toast(e.message, "err"); }
  finally { btn.disabled = false; btn.textContent = label; }
}

export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
  }
  toast("Copied to clipboard", "ok");
}

function age(ts) {
  const m = Math.max(0, (Date.now() - new Date(ts).getTime()) / 60000);
  if (m < 60) return `${Math.round(m)}m`;
  if (m < 60 * 24) return `${Math.round(m / 60)}h`;
  return `${Math.round(m / 1440)}d`;
}

// ---------------------------------------------------------------- state
const S = { filter: "mine", items: [], sel: -1, tz: "UTC" };

function renderStream() {
  const el = $("#stream");
  if (!S.items.length) { el.innerHTML = `<div class="empty dim">Nothing here.</div>`; return; }
  el.innerHTML = S.items.map((it, i) => `
    <div class="item ${i === S.sel ? "sel" : ""}" role="option" data-i="${i}" draggable="true" aria-selected="${i === S.sel}">
      <div class="row1"><span class="src">${esc(it.source)}</span><span class="who">${esc(it.from)}</span><span class="age">${esc(age(it.ts))}</span></div>
      <div class="subj">${esc(it.subject)}</div>
      <div class="snip">${esc(it.snippet)}</div>
      ${(it.badges || []).length || it.court === "WAITING" ? `<div class="badges">
        ${(it.badges || []).map((b) => `<span class="badge ${b === "VIP" ? "vip" : b === "READY" ? "ready" : ""}">${esc(b)}</span>`).join("")}
        ${it.court === "WAITING" ? `<span class="badge wait">waiting ${esc(it.waiting_days)}d</span>` : ""}
      </div>` : ""}
    </div>`).join("");
}

async function loadStream() {
  try {
    const r = await api(`/api/stream?filter=${encodeURIComponent(S.filter)}`);
    S.items = r.items || [];
    S.sel = S.items.length ? 0 : -1;
    renderStream();
    setSource("sb-mail", "ok", "Mail");
  } catch (e) {
    S.items = []; renderStream();
    setSource("sb-mail", "warn", "Mail");
    $("#stream").innerHTML = `<div class="empty dim">${esc(e.message)}</div>`;
  }
}

async function openItem(i) {
  const it = S.items[i]; if (!it) return;
  S.sel = i; renderStream();
  $("#thread-empty").hidden = true;
  const th = $("#thread"); th.hidden = false;
  th.innerHTML = `<h2>${esc(it.subject)}</h2><div class="dim">Loading...</div>`;
  try {
    const t = await api(`/api/thread/${encodeURIComponent(it.key)}`);
    th.innerHTML = `<h2>${esc(it.subject)}</h2>
      <div class="thread-acts">
        <button class="btn small" disabled title="v0.3">Reply</button>
        <button class="btn small" disabled title="v0.3">Reply all</button>
        <button class="btn small" disabled title="v0.3">Draft with AI</button>
        <button class="btn small" disabled title="v0.3">Archive</button>
        <button class="btn small" data-a="copy">Copy</button>
      </div>
      ${(t.messages || []).map((m) => `<div class="msg"><div class="hdr"><b>${esc(m.from)}</b><span class="mono">${esc(m.ts)}</span></div><div class="body">${esc(m.body)}</div></div>`).join("") || `<div class="dim">No messages.</div>`}`;
    $('[data-a="copy"]', th).onclick = () => copyText((t.messages || []).map((m) => `${m.from} (${m.ts})\n${m.body}`).join("\n\n"));
  } catch (e) { th.innerHTML = `<div class="dim">${esc(e.message)}</div>`; }
  loadContext(it);
}

async function loadContext(it) {
  const el = $("#context");
  if (!it.addr) { el.innerHTML = `<div class="dim">No address to look up.</div>`; return; }
  el.innerHTML = `<div class="dim">Looking up ${esc(it.addr)}...</div>`;
  try {
    const c = await api(`/api/context/${encodeURIComponent(it.addr)}`);
    if (c.unresolved) { el.innerHTML = `<div class="dim">Not in ledger.</div>`; return; }
    el.innerHTML = `<div class="kv">
      <span class="k">name</span><span>${esc(c.name)}</span>
      <span class="k">title</span><span>${esc(c.title || "")}</span>
      <span class="k">projects</span><span>${esc((c.projects || []).join(", ") || "-")}</span>
      <span class="k">tasks</span><span>${esc(c.open_tasks ?? "-")} open</span>
      <span class="k">last log</span><span class="mono">${esc(c.last_log || "-")}</span>
      <span class="k">replied</span><span>${c.replied ? `<span class="badge ok">yes</span>` : `<span class="badge ready">no</span>`}</span>
    </div>`;
  } catch (e) { el.innerHTML = `<div class="dim">${esc(e.message)}</div>`; }
}

function setSource(id, cls, label) {
  const el = document.getElementById(id);
  el.className = `src ${cls}`;
  el.textContent = `${label} ${cls === "ok" ? "ok" : cls === "warn" ? "!" : "--"}`;
}

function tick() {
  const now = new Date();
  const t = now.toLocaleString([], { timeZone: S.tz, weekday: "short", month: "numeric", day: "numeric", hour: "numeric", minute: "2-digit" });
  $("#telemetry").innerHTML = `<span><b>${esc(t)}</b></span><span>${S.demo ? "DEMO DATA" : ""}</span>`;
}

// ---------------------------------------------------------------- wiring
function closeDrawers() {
  $$(".panel.open").forEach((p) => p.classList.remove("open"));
  $("#scrim").hidden = true;
}

function wire() {
  $("#filter-seg").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-f]"); if (!b) return;
    $$("#filter-seg button").forEach((x) => x.classList.toggle("on", x === b));
    S.filter = b.dataset.f; loadStream();
  });
  $("#stream").addEventListener("click", (e) => {
    const el = e.target.closest(".item"); if (!el) return;
    closeDrawers();  // on a phone, picking a conversation closes the drawer
    openItem(Number(el.dataset.i));
  });
  $("#stream").addEventListener("dragstart", (e) => {
    const el = e.target.closest(".item"); if (!el) return;
    e.dataTransfer.setData("application/x-ultra-item", JSON.stringify(S.items[Number(el.dataset.i)]));
  });
  const drop = $("#bucket-drop");
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault(); drop.classList.remove("over");
    toast("Bucket arrives in v0.4 (log and link to the ledger).");
  });
  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea, select")) return;
    if (e.key === "j") { S.sel = Math.min(S.items.length - 1, S.sel + 1); renderStream(); }
    else if (e.key === "k") { S.sel = Math.max(0, S.sel - 1); renderStream(); }
    else if (e.key === "Enter" && S.sel >= 0) openItem(S.sel);
  });
  const drawer = (sel) => { $(sel).classList.toggle("open"); $("#scrim").hidden = !$$(".panel.open").length; };
  $("#btn-left").onclick = () => drawer("#left");
  $("#btn-right").onclick = () => drawer("#right");
  $("#scrim").onclick = closeDrawers;
  $("#btn-palette").onclick = () => toast("Command palette arrives in v0.3.");
}

async function boot() {
  wire();
  try {
    const s = await api("/api/session");
    TOKEN = s.token; S.tz = s.timezone || "UTC"; S.demo = s.demo;
    $("#sb-version").textContent = `ultra ${s.version}${s.demo ? " demo" : ""}`;
  } catch (e) { toast(`Server: ${e.message}`, "err"); }
  tick(); setInterval(tick, 30000);
  loadStream();
}
boot();
