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
const S = { filter: "mine", items: [], sel: -1, tz: "UTC", counts: {}, key: null, loading: false };

function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  return d.toLocaleString([], { timeZone: S.tz, month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

const BADGE = { VIP: "vip", READY: "ready", OVERDUE: "ready", SLOW: "wait", ASSIGNED: "vip" };

function renderStream() {
  const el = $("#stream");
  $$("#filter-seg button").forEach((b) => {
    const n = S.counts[b.dataset.f];
    b.textContent = b.dataset.label + (n ? ` ${n}` : "");
  });
  if (!S.items.length) {
    el.innerHTML = `<div class="empty dim">${S.loading ? "Loading your mail..." : "Nothing here."}</div>`;
    return;
  }
  el.innerHTML = S.items.map((it, i) => `
    <div class="item ${i === S.sel ? "sel" : ""}" role="option" data-i="${i}" draggable="true" aria-selected="${i === S.sel}">
      <div class="row1"><span class="src">${esc(it.source)}</span><span class="who ${it.unread ? "unread" : ""}">${esc(it.from)}</span>${it.count > 1 ? `<span class="cnt">${esc(it.count)}</span>` : ""}<span class="age">${esc(age(it.ts))}</span></div>
      <div class="subj">${esc(it.subject)}</div>
      <div class="snip">${esc(it.snippet)}</div>
      ${(it.badges || []).length || it.court === "WAITING" ? `<div class="badges">
        ${(it.badges || []).map((b) => `<span class="badge ${BADGE[b] || ""}">${esc(b)}</span>`).join("")}
        ${it.court === "WAITING" ? `<span class="badge wait">waiting ${esc(it.waiting_days)}d</span>` : ""}
      </div>` : ""}
    </div>`).join("");
}

async function loadStream(keepSel = false) {
  try {
    const r = await api(`/api/stream?filter=${encodeURIComponent(S.filter)}`);
    const prevKey = S.items[S.sel]?.key;
    S.items = r.items || [];
    S.counts = r.counts || {};
    S.loading = !!r.loading;
    const i = keepSel ? S.items.findIndex((x) => x.key === prevKey) : -1;
    S.sel = i >= 0 ? i : (S.items.length ? 0 : -1);
    renderStream();
  } catch (e) {
    S.items = []; renderStream();
    setSource("sb-mail", "warn", "Mail");
    $("#stream").innerHTML = `<div class="empty dim">${esc(e.message)}</div>`;
  }
}

async function openItem(i) {
  const it = S.items[i]; if (!it) return;
  S.sel = i; S.key = it.key; renderStream();
  $("#thread-empty").hidden = true;
  const th = $("#thread"); th.hidden = false;
  th.innerHTML = `<h2>${esc(it.subject)}</h2><div class="dim">Loading...</div>`;
  try {
    const t = await api(`/api/thread/${encodeURIComponent(it.key)}`);
    if (S.key !== it.key && S.items[S.sel]?.key !== it.key) return;  // user moved on
    const soon = (v) => `disabled title="Coming in ${v}"`;
    th.innerHTML = `<h2>${esc(it.subject)}</h2>
      ${it.reason ? `<div class="why dim">${esc(it.court)} &middot; ${esc(it.reason)}</div>` : ""}
      ${t.error ? `<div class="alert">${esc(t.error)}</div>` : ""}
      ${(t.events || []).length ? `<div class="badges ev">${t.events.map((e) => `<span class="badge">${esc(e)}</span>`).join("")}</div>` : ""}
      <div class="thread-acts">
        <button class="btn small" ${soon("v0.3")}>Reply</button>
        <button class="btn small" ${soon("v0.3")}>Reply all</button>
        <button class="btn small" ${soon("v0.3")}>Draft with AI</button>
        <button class="btn small" ${soon("v0.3")}>Archive</button>
        <button class="btn small" data-a="copy">Copy</button>
        ${t.permalink ? `<a class="btn small" href="${esc(t.permalink)}" target="_blank" rel="noopener noreferrer">Open in Slack</a>` : ""}
      </div>
      ${(t.messages || []).map((m) => `<div class="msg ${m.mine ? "mine" : ""}">
        <div class="hdr"><b>${esc(m.from)}</b><span class="mono">${esc(fmtTime(m.ts))}</span></div>
        ${m.to ? `<div class="to dim">to ${esc(m.to)}${m.cc ? ` &middot; cc ${esc(m.cc)}` : ""}</div>` : ""}
        <div class="body">${esc(m.body) || `<span class="dim">(no text)</span>`}</div>
        ${m.quoted ? `<details class="quoted"><summary>quoted text</summary><div class="body">${esc(m.quoted)}</div></details>` : ""}
        ${(m.attachments || []).length ? `<div class="atts">${m.attachments.map((a) => `<span class="att" title="${esc(a.mime)}">${esc(a.name)} <span class="dim">${esc(Math.round((a.size || 0) / 1024))} KB</span></span>`).join("")}</div>` : ""}
      </div>`).join("") || `<div class="dim">No messages.</div>`}`;
    $('[data-a="copy"]', th).onclick = () => copyText((t.messages || []).map((m) => `${m.from} (${fmtTime(m.ts)})\n${m.body}`).join("\n\n"));
  } catch (e) { th.innerHTML = `<div class="dim">${esc(e.message)}</div>`; }
  loadContext(it);
}

async function loadContext(it) {
  const el = $("#context");
  if (!it.addr) { el.innerHTML = `<div class="dim">No address to look up.</div>`; return; }
  el.innerHTML = `<div class="dim">Looking up ${esc(it.addr)} in the ledger... (a few seconds the first time)</div>`;
  const want = it.key;
  try {
    const c = await api(`/api/context/${encodeURIComponent(it.addr)}?name=${encodeURIComponent(it.from || "")}`);
    pollStatus();
    if (S.key !== want && S.items[S.sel]?.key !== want) return;
    if (c.self) { el.innerHTML = `<div class="dim">That's you.</div>`; return; }
    if (c.unavailable) { el.innerHTML = `<div class="dim">Ledger unavailable: ${esc(c.reason || "")}</div>`; return; }
    if (c.unresolved) { el.innerHTML = `<div class="dim">${esc(it.addr)} is not in the ledger.</div>`; return; }
    const list = (xs, f) => xs.length ? `<ul class="clist">${xs.map(f).join("")}</ul>` : `<div class="dim">none</div>`;
    const tasks = Array.isArray(c.open_tasks) ? c.open_tasks : [];
    el.innerHTML = `<div class="cname">${esc(c.name)} <span class="dim mono">${esc(c.netid || "")}</span></div>
      <div class="dim">${esc(c.title || "")}</div>
      ${c.matched_by === "name" ? `<div class="warnline">Matched by name, not address: check it's the right person.</div>` : ""}
      <div class="sect"><span class="label">Labs</span>${list(c.labs || [], (x) => `<li>${esc(x)}</li>`)}</div>
      <div class="sect"><span class="label">Projects</span>${list(c.projects || [], (x) => `<li>${esc(x)}</li>`)}</div>
      <div class="sect"><span class="label">Open tasks ${tasks.length}</span>${list(tasks, (x) => `<li title="${esc(x.id)}">${esc(x.summary)}</li>`)}</div>
      <div class="sect"><span class="label">Recent logs ${esc(c.interaction_count ?? "")}</span>${list(c.interactions || [], (x) => `<li title="${esc(x.id)}">${esc(x.summary)}</li>`)}</div>`;
  } catch (e) { el.innerHTML = `<div class="dim">${esc(e.message)}</div>`; }
}

// ---------------------------------------------------------------- status polling
const SRC = { mail: "sb-mail", slack: "sb-slack", ledger: "sb-ledger" };
$("#sb-cal") && ($("#sb-cal").title = "Calendar arrives in v0.6");
$("#sb-ai") && ($("#sb-ai").title = "AI arrives in v0.3 (needs GEMINI_API_KEY)");
let lastBuilt = { mail: null, slack: null };
async function pollStatus() {
  if (S.demo) return;
  try {
    const st = await api("/api/status");
    for (const [name, id] of Object.entries(SRC)) {
      const s = st.sources[name] || {};
      const el = document.getElementById(id);
      const label = name[0].toUpperCase() + name.slice(1);
      let cls = "", txt = `${label} --`;
      if (s.enabled === false) { txt = `${label} off`; }
      else if (s.busy || st.jobs[name]?.state === "running") { cls = "warn"; txt = `${label} ...`; }
      else if (s.ok === true) { cls = "ok"; txt = `${label} ${s.age != null ? age(Date.now() - s.age * 1000) : "ok"}`; }
      else if (s.ok === false) { cls = "err"; txt = `${label} !`; }
      el.className = `src ${cls}`; el.textContent = txt; el.title = s.error || "";
    }
    const mailAge = st.sources.mail?.age, slackAge = st.sources.slack?.age;
    const changed = (lastBuilt.mail !== null && mailAge < lastBuilt.mail) || (lastBuilt.slack !== null && slackAge != null && slackAge < lastBuilt.slack) || (S.loading && mailAge != null);
    lastBuilt = { mail: mailAge, slack: slackAge };
    if (changed) loadStream(true);
    const err = st.sources.mail?.error;
    if (err && st.sources.mail?.auth && !S.authWarned) { S.authWarned = true; toast(err, "err"); }
  } catch { /* server restarting */ }
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
    else if (e.key === "R") $("#btn-refresh").click();
    else if (e.key === "m") $('#filter-seg button[data-f="mine"]').click();
    else if (e.key === "w") $('#filter-seg button[data-f="waiting"]').click();
  });
  const drawer = (sel) => { $(sel).classList.toggle("open"); $("#scrim").hidden = !$$(".panel.open").length; };
  $("#btn-left").onclick = () => drawer("#left");
  $("#btn-right").onclick = () => drawer("#right");
  $("#scrim").onclick = closeDrawers;
  $("#btn-palette").onclick = () => toast("Command palette arrives in v0.3.");
  $("#btn-refresh").onclick = (e) => busy(e.currentTarget, async () => {
    await api("/api/refresh", { method: "POST", body: { what: "all" } });
    toast("Refreshing mail" + (S.slackOn ? " and Slack" : "") + "...");
  });

}

async function boot() {
  wire();
  try {
    const s = await api("/api/session");
    TOKEN = s.token; S.tz = s.timezone || "UTC"; S.demo = s.demo; S.slackOn = s.slack;
    if (!s.slack) $('#filter-seg button[data-f="slack"]').hidden = true;
    $("#sb-version").textContent = `ultra ${s.version}${s.demo ? " demo" : ""}`;
  } catch (e) { toast(`Server: ${e.message}`, "err"); }
  $$("#filter-seg button").forEach((b) => { b.dataset.label = b.textContent; });
  tick(); setInterval(tick, 30000);
  await loadStream();
  pollStatus(); setInterval(pollStatus, 5000);
}
boot();
