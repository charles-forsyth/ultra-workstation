// Ultra client shell. Vanilla JS modules, no build step, no inline code (CSP).
// Helpers adapted from the deep-research dashboard (MIT, same author).

import { openDraft, openSlackDraft, resumeForThread, onSent } from "./compose.js";
import { initRail, wireSearch, loadPeople, addConversation, addSnippet, addEntity as addEntityToBucket, searchFor, stage, stageAfterSend, stageTaskLog, openPersonByAddr } from "./ledger.js";
import { initTools, setToolsThread, webSearch, explain, researchSearch, launcher, readAloud, audioDialog, listen } from "./tools.js";
import { initDay, openDay, closeDay, dayOpen } from "./day.js";
import { initToday, openToday, closeToday, todayOpen } from "./today.js";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

export function toast(msg, kind = "", action = null) {
  for (const old of document.querySelectorAll("#toasts .toast")) if (old.dataset.msg === msg) old.remove();
  const t = document.createElement("div");
  t.dataset.msg = msg;
  t.className = `toast ${kind}`;
  t.textContent = msg;
  if (action) {
    const b = document.createElement("button");
    b.className = "btn tiny"; b.textContent = action.label;
    b.onclick = () => { t.remove(); action.fn(); };
    t.append(" ", b);
  }
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), action ? 10000 : kind === "err" ? 7000 : 3500);
}

let TOKEN = "";
// The server issues a new write token each time it starts. A tab left open across a
// restart holds the old one, so on a token refusal fetch the current token and retry
// the request once. The token only proves the request came from this page (CSRF);
// every action behind it still has its own checks and approvals.
async function refreshToken() {
  const r = await fetch("/api/session");
  const s = await r.json().catch(() => ({}));
  if (r.ok && s.token) { TOKEN = s.token; return true; }
  return false;
}
export async function api(path, { method = "GET", body } = {}, retried = false) {
  const opt = { method, headers: {} };
  if (method !== "GET") {
    if (!TOKEN) await refreshToken();
    opt.headers["Content-Type"] = "application/json";
    opt.headers["X-Ultra-Token"] = TOKEN;
    opt.body = JSON.stringify(body ?? {});
  }
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (r.status === 403 && method !== "GET" && !retried && /token/i.test(data.error || "")) {
    if (await refreshToken()) return api(path, { method, body }, true);
  }
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

const BADGE = { VIP: "vip", READY: "ready", OVERDUE: "ready", SLOW: "wait", ASSIGNED: "vip", CRITICAL: "ready", HIGH: "wait", BLOCKED: "ready", STARTED: "vip" };

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
    <div class="item ${i === S.sel ? "sel" : ""} ${it.source === "task" ? `task-row pri-${esc((it.priority || "").toLowerCase())} st-${esc((it.status || "").toLowerCase())}` : ""}" role="option" data-i="${i}" draggable="true" aria-selected="${i === S.sel}">
      <div class="row1"><span class="src">${esc(it.source)}</span><span class="who ${it.unread ? "unread" : ""}">${esc(it.from)}</span>${it.count > 1 ? `<span class="cnt">${esc(it.count)}</span>` : ""}<span class="age">${it.ts ? esc(age(it.ts)) : ""}</span></div>
      <div class="subj">${esc(it.subject)}</div>
      <div class="snip">${esc(it.snippet)}</div>
      ${(it.badges || []).length || it.court === "WAITING" ? `<div class="badges">
        ${(it.badges || []).map((b) => `<span class="badge ${BADGE[b] || ""}">${esc(b)}</span>`).join("")}
        ${it.court === "WAITING" ? `<span class="badge wait">waiting ${esc(it.waiting_days)}d</span>` : ""}
      </div>` : ""}
      ${rowActs(it)}
    </div>`).join("");
}

// Bucket actions on every stream row: the same things dragging does, one click each.
// (Dragging still works; these are for when you'd rather click.)
function rowActs(it) {
  const task = it.source === "task";
  return `<div class="ract" role="group" aria-label="Actions">
    <button class="ra" data-ra="bucket" title="Add to bucket">+ Bucket</button>
    <button class="ra" data-ra="log" title="${task ? "Log progress on this task" : "Log this conversation in the ledger"}">Log</button>
    ${task ? "" : `<button class="ra" data-ra="task" title="Make a ledger task from it">Task</button>`}
    <button class="ra" data-ra="block" title="Block time for it on your calendar">Block</button>
  </div>`;
}

async function rowAction(a, it) {
  if (a === "block") {
    window.dispatchEvent(new CustomEvent("ultra:block", { detail: { key: it.key, subject: it.subject } }));
    return;
  }
  if (it.source === "task") {
    const id = it.key.slice(2);
    if (a === "bucket") return addEntityToBucket({ id, name: it.subject, type: "Task" });
    if (a === "log") {
      const d = await api(`/api/thread/${encodeURIComponent(it.key)}`);
      return stageTaskLog(d.task || { id, summary: it.subject }, d.links || []);
    }
    return;
  }
  if (a === "bucket") return addConversation(it.key);
  if (a === "log" || a === "task") return stage(a, it.key);
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

async function resumeSlack(it) {
  const c = $("#composer");
  try {
    const r = await api(`/api/slack/draft/${encodeURIComponent(it.key)}`);
    if (r.draft) await openSlackDraft(it.key); else c.hidden = true;
  } catch { c.hidden = true; }
}

async function openItem(i) {
  const it = S.items[i]; if (!it) return;
  if (todayOpen()) closeToday();
  if (dayOpen()) closeDay();
  if (it.source === "task") return openTask(i);
  S.sel = i; S.key = it.key; renderStream();
  $("#thread-empty").hidden = true;
  const th = $("#thread"); th.hidden = false;
  th.innerHTML = `<h2>${esc(it.subject)}</h2><div class="dim">Loading...</div>`;
  try {
    const t = await api(`/api/thread/${encodeURIComponent(it.key)}`);
    if (S.key !== it.key && S.items[S.sel]?.key !== it.key) return;  // user moved on
    const soon = (v) => `disabled title="Coming in ${v}"`;
    const isMail = it.key.startsWith("g-");
    const mailOnly = isMail ? "" : `disabled title="Email threads only for now"`;
    th.innerHTML = `<h2>${esc(it.subject)}</h2>
      ${it.reason ? `<div class="why dim">${esc(it.court)} &middot; ${esc(it.reason)}</div>` : ""}
      ${t.error ? `<div class="alert">${esc(t.error)}</div>` : ""}
      ${(t.events || []).length ? `<div class="badges ev">${t.events.map((e) => `<span class="badge">${esc(e)}</span>`).join("")}</div>` : ""}
      <div class="thread-acts">
        <button class="btn small" data-a="reply" ${mailOnly} title="Reply (r)">Reply</button>
        <button class="btn small" data-a="reply_all" ${mailOnly} title="Reply all (a)">Reply all</button>
        <button class="btn small" data-a="forward" ${mailOnly} title="Forward (f)">Forward</button>
        <button class="btn small ai" data-a="summary" ${isMail || it.key.startsWith("k-") ? "" : mailOnly} title="AI summary (s)">Summarize</button>
        <button class="btn small" data-a="archive" ${isMail || it.key.startsWith("k-") ? "" : mailOnly} title="Archive (e). Never deletes.">Archive</button>
        <button class="btn small" data-a="copy">Copy</button>
        <button class="btn small" data-a="listen" title="Read aloud with the browser voice (free)">Read aloud</button>
        <button class="btn small ai" data-a="aiaudio" title="AI voice: spoken summary or full read">AI audio</button>
        <button class="btn small" data-a="bucket" title="Add to bucket (b)">+ Bucket</button>
        <button class="btn small" data-a="log" title="Log this conversation in the ledger (l)">Log</button>
        <button class="btn small" data-a="task" title="Make a ledger task from it (t)">Task</button>
        <button class="btn small" data-a="block" title="Block time for this on your calendar">Block time</button>
        ${t.permalink ? `<a class="btn small" href="${esc(t.permalink)}" target="_blank" rel="noopener noreferrer">Open in Slack</a>` : ""}
      </div>
      <div class="aisum" id="aisum" hidden></div>
      <section class="composer" id="composer" hidden></section>
      ${(t.messages || []).map((m) => `<div class="msg ${m.mine ? "mine" : ""}">
        <div class="hdr"><b>${esc(m.from)}</b><span class="mono">${esc(fmtTime(m.ts))}</span></div>
        ${m.to ? `<div class="to dim">to ${esc(m.to)}${m.cc ? ` &middot; cc ${esc(m.cc)}` : ""}</div>` : ""}
        <div class="body">${esc(m.body) || `<span class="dim">(no text)</span>`}</div>
        ${m.quoted ? `<details class="quoted"><summary>quoted text</summary><div class="body">${esc(m.quoted)}</div></details>` : ""}
        ${(m.attachments || []).length ? `<div class="atts">${m.attachments.map((a) => `<span class="att" title="${esc(a.mime)}">${esc(a.name)} <span class="dim">${esc(Math.round((a.size || 0) / 1024))} KB</span></span>`).join("")}</div>` : ""}
      </div>`).join("") || `<div class="dim">No messages.</div>`}`;
    $('[data-a="copy"]', th).onclick = () => copyText((t.messages || []).map((m) => `${m.from} (${fmtTime(m.ts)})\n${m.body}`).join("\n\n"));
    for (const k of ["reply", "reply_all", "forward"]) {
      const b = $(`[data-a="${k}"]`, th);
      b.onclick = () => busy(b, () => openDraft(k, it.key));
    }
    if (it.source === "slack") {
      // Slack: Reply posts in the same conversation (thread if it is one), after both approvals
      const rb = $('[data-a="reply"]', th);
      if (S.slackOn && t.reply_target) { rb.disabled = false; rb.title = "Reply in Slack (r), after two approvals"; rb.onclick = () => busy(rb, () => openSlackDraft(it.key)); }
      else if (!t.reply_target) rb.title = "No channel id for this conversation";
      const ra = $('[data-a="reply_all"]', th), fw = $('[data-a="forward"]', th);
      ra.hidden = true; fw.hidden = true;
    }
    const sb = $('[data-a="summary"]', th); sb.onclick = () => busy(sb, () => summarize(it));
    $('[data-a="bucket"]', th).onclick = () => addConversation(it.key);
    $('[data-a="listen"]', th).onclick = () => readAloud(th);
    $('[data-a="aiaudio"]', th).onclick = () => audioDialog({ thread: it.key }, it.subject || "Conversation");
    setToolsThread(it.key, it.subject);
    $('[data-a="log"]', th).onclick = () => stage("log", it.key);
    $('[data-a="task"]', th).onclick = () => stage("task", it.key);
    $('[data-a="block"]', th).onclick = () => window.dispatchEvent(new CustomEvent("ultra:block", { detail: { key: it.key, subject: it.subject } }));
    wireSelection(th, it);
    const ab = $('[data-a="archive"]', th);
    if (it.source === "slack") { ab.disabled = false; ab.textContent = "Mark done"; ab.title = "Hide until a new message arrives (e). Nothing is sent to Slack."; ab.onclick = () => busy(ab, () => slackDone(it)); }
    else ab.onclick = () => busy(ab, () => archive(it));
    if (it.source === "slack") await resumeSlack(it); else await resumeForThread(it.key);
  } catch (e) { th.innerHTML = `<div class="dim">${esc(e.message)}</div>`; }
  loadContext(it);
}

// ---------------------------------------------------------------- selection bar
function wireSelection(th, it) {
  const bar = $("#selbar");
  const hide = () => { bar.hidden = true; };
  th.onmouseup = () => setTimeout(() => {
    const sel = window.getSelection();
    const text = (sel?.toString() || "").trim();
    if (!text || !th.contains(sel.anchorNode)) { hide(); return; }
    const r = sel.getRangeAt(0).getBoundingClientRect();
    const short = text.length <= 200;
    bar.innerHTML = `<button class="btn tiny" data-s="copy">Copy</button>
      <button class="btn tiny" data-s="quote">Quote in reply</button>
      <button class="btn tiny" data-s="search" ${short ? "" : "disabled title=\"Select 200 characters or fewer\""}>Search ledger</button>
      <button class="btn tiny" data-s="web">Web search</button>
      <button class="btn tiny" data-s="bucket">Add to bucket</button>
      <span class="more"><button class="btn tiny" data-s="more">More</button>
        <span class="more-menu" hidden>
          <button class="btn tiny ai" data-s="explain">Explain</button>
          <button class="btn tiny" data-s="rsearch">Search research</button>
          <button class="btn tiny" data-s="research">Research this...</button>
          <button class="btn tiny" data-s="read">Read aloud</button>
        </span></span>`;
    bar.style.left = `${Math.max(8, Math.min(window.innerWidth - 300, r.left))}px`;
    bar.style.top = `${Math.max(50, r.top - 38)}px`;
    bar.hidden = false;
    bar.onmousedown = (e) => e.preventDefault();  // keep the selection
    bar.onclick = (e) => {
      const b = e.target.closest("[data-s]"); if (!b) return;
      const a = b.dataset.s;
      if (a === "more") { bar.querySelector(".more-menu").hidden = false; return; }
      if (a === "search") searchFor(text);
      else if (a === "bucket") addSnippet(text, it.key, it.subject, it.ts);
      else if (a === "web") webSearch(text.slice(0, 1000));
      else if (a === "explain") explain(text);
      else if (a === "rsearch") researchSearch(text.slice(0, 500));
      else if (a === "research") launcher(text.slice(0, 2000));
      else if (a === "read") readAloud(null, text);
      else if (a === "quote") quoteIntoReply(text);
      else copyText(text);
      hide(); sel.removeAllRanges();
    };
  }, 0);
  document.addEventListener("scroll", hide, { capture: true, once: true });
}

function quoteIntoReply(text) {
  const quoted = text.split("\n").map((l) => `> ${l}`).join("\n") + "\n\n";
  const body = $("#cx-body");
  if (body && !body.disabled) {
    const at = body.selectionStart ?? body.value.length;
    body.value = body.value.slice(0, at) + quoted + body.value.slice(at);
    body.dispatchEvent(new Event("input"));
    body.focus();
    return;
  }
  const b = $('#thread [data-a="reply"]');
  if (!b || b.disabled) { copyText(quoted); return; }
  b.click();
  const wait = setInterval(() => {
    const nb = $("#cx-body");
    if (nb && !nb.disabled) { clearInterval(wait); nb.value = quoted + nb.value; nb.dispatchEvent(new Event("input")); nb.focus(); }
  }, 150);
  setTimeout(() => clearInterval(wait), 8000);
}

// ---------------------------------------------------------------- slack done
async function slackDone(it) {
  await api("/api/slack/done", { method: "POST", body: { key: it.key } });
  removeRow(it);
  toast("Marked done. It comes back if someone writes again.", "ok", {
    label: "Undo", fn: async () => { await api("/api/slack/undone", { method: "POST", body: { key: it.key } }); loadStream(true); },
  });
}

function removeRow(it) {
  const idx = S.items.findIndex((x) => x.key === it.key);
  S.items = S.items.filter((x) => x.key !== it.key);
  S.sel = Math.min(idx, S.items.length - 1);
  renderStream();
  if (S.sel >= 0) openItem(S.sel); else { $("#thread").hidden = true; $("#thread-empty").hidden = false; }
}

// ---------------------------------------------------------------- tasks
const PRI = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];

async function openTask(i) {
  const it = S.items[i];
  S.sel = i; S.key = it.key; renderStream();
  $("#thread-empty").hidden = true;
  const th = $("#thread"); th.hidden = false;
  th.innerHTML = `<h2>${esc(it.subject)}</h2><div class="dim">Loading the task from the ledger...</div>`;
  let d;
  try { d = await api(`/api/thread/${encodeURIComponent(it.key)}`); }
  catch (e) { th.innerHTML = `<div class="dim">${esc(e.message)}</div>`; return; }
  if (S.key !== it.key) return;
  if (!d.task) { th.innerHTML = `<h2>${esc(it.subject)}</h2><div class="alert">${esc(d.error || "Task not found")}</div>`; return; }
  const t = d.task;
  const due = t.due_date ? String(t.due_date).slice(0, 10) : "";
  const overdue = due && due < new Date().toISOString().slice(0, 10);
  th.innerHTML = `<div class="task-head pri-${esc(t.priority.toLowerCase())}"><h2>${esc(t.summary)}</h2>
      <div class="badges"><span class="badge pri ${esc(t.priority.toLowerCase())}">${esc(t.priority)}</span><span class="badge">${esc(t.status.replace("_", " "))}</span>
      ${due ? `<span class="badge ${overdue ? "ready" : ""}">due ${esc(due)}${overdue ? " (overdue)" : ""}</span>` : ""}
      ${t.snoozed_until ? `<span class="badge wait">snoozed</span>` : ""}
      ${Object.entries(t.details).map(([k, v]) => `<span class="badge dim" title="${esc(k)}">${esc(v)}</span>`).join("")}</div></div>
    <div class="thread-acts">
      ${t.status !== "DONE" ? `<button class="btn small send" data-t="complete" title="Mark DONE in the ledger (e)">Complete</button>` : `<button class="btn small" data-t="reopen">Reopen</button>`}
      ${t.status !== "IN_PROGRESS" && t.status !== "DONE" ? `<button class="btn small" data-t="start">Start</button>` : ""}
      ${t.status !== "BLOCKED" && t.status !== "DONE" ? `<button class="btn small" data-t="block">Blocked</button>` : ""}
      ${t.status === "BLOCKED" || t.status === "IN_PROGRESS" ? `<button class="btn small" data-t="reopen">Back to to-do</button>` : ""}
      <select id="t-pri" title="Priority">${PRI.map((p) => `<option ${p === t.priority ? "selected" : ""}>${p}</option>`).join("")}</select>
      <select id="t-snooze" title="Hide from the stream for a while (local only)"><option value="">Snooze...</option><option value="1">1 day</option><option value="3">3 days</option><option value="7">1 week</option>${t.snoozed_until ? `<option value="0">Unsnooze</option>` : ""}</select>
      <button class="btn small" data-t="log" title="Log progress on this task (l)">Log update</button>
      <button class="btn small" data-t="bucket" title="Add to bucket (b)">+ Bucket</button>
      <button class="btn small" data-t="block" title="Block time for this task on your calendar">Block time</button>
      <button class="btn small" data-t="copy">Copy</button>
    </div>
    <div class="sect"><span class="label">Linked in the ledger (${d.links.length})</span>
      ${d.links.length ? `<ul class="clist">${d.links.map((x) => `<li class="ent" draggable="true" data-ent='${esc(JSON.stringify({ id: x.id, name: x.name, type: x.type }))}'><span class="badge">${esc(x.type)}</span> ${esc(x.name)} <span class="dim small-t">${esc(x.edge)}</span></li>`).join("")}</ul>` : `<div class="dim small-t">No links. Drag people or projects onto it via the bucket.</div>`}</div>
    <div class="dim small-t mono">${esc(t.id)}</div>`;
  const act = async (body, label) => {
    const r = await api("/api/task/action", { method: "POST", body: { id: t.id, ...body } });
    if (r.ok === false) { toast(`The ledger did not confirm: ${r.output_tail || "unknown"}`, "err"); return false; }
    toast(label, "ok"); return true;
  };
  th.onclick = (e) => {
    const b = e.target.closest("[data-t]"); if (!b) return;
    const a = b.dataset.t;
    if (a === "copy") { copyText(`${t.summary} (${t.priority}, ${t.status})`); return; }
    if (a === "bucket") { addEntityToBucket({ id: t.id, name: t.summary, type: "Task" }); return; }
    if (a === "log") { stageTaskLog(t, d.links); return; }
    if (a === "block") { window.dispatchEvent(new CustomEvent("ultra:block", { detail: { key: `t-${t.id}`, subject: t.summary } })); return; }
    busy(b, async () => {
      if (a === "complete") {
        if (!confirm(`Mark this task DONE in the ledger?\n\n${t.summary}`)) return;
        if (await act({ action: "complete" }, "Task completed.")) {
          removeRow(it);
          toast("Task completed in the ledger.", "ok", { label: "Undo", fn: async () => { await act({ action: "reopen" }, "Task reopened."); setTimeout(() => loadStream(true), 2500); } });
        }
      } else if (await act({ action: a }, `Task ${a === "start" ? "started" : a === "block" ? "marked blocked" : "back to to-do"}.`)) {
        it.status = { start: "IN_PROGRESS", block: "BLOCKED", reopen: "TODO" }[a]; openTask(S.items.indexOf(it)); setTimeout(() => loadStream(true), 2500);
      }
    });
  };
  $("#t-pri").onchange = async (e) => { if (await act({ action: "priority", priority: e.target.value }, `Priority set to ${e.target.value}.`)) setTimeout(() => loadStream(true), 2500); };
  $("#t-snooze").onchange = async (e) => {
    const v = e.target.value; if (v === "") return;
    if (v === "0") { await act({ action: "unsnooze" }, "Unsnoozed."); loadStream(true); return; }
    if (await act({ action: "snooze", days: Number(v) }, `Snoozed for ${v} day(s). Only on this laptop.`)) removeRow(it);
  };
  th.ondragstart = (e) => { const li = e.target.closest("[data-ent]"); if (li) e.dataTransfer.setData("application/x-ultra-entity", li.dataset.ent); };
  if (S.key === it.key) loadPeople(it);  // v0.8: the task's people + everyone named in it, and the Full tab
}

function threadsOf(it) {
  if (it.key.startsWith("g-")) return [it.key.slice(2)];
  return it.threads || [];
}

async function archive(it) {
  if (S.demo) { toast("Demo mode: nothing is archived."); return; }
  const tids = threadsOf(it);
  if (!tids.length) return;
  await api("/api/mail/archive", { method: "POST", body: { threads: tids } });
  const idx = S.items.findIndex((x) => x.key === it.key);
  S.items = S.items.filter((x) => x.key !== it.key);
  S.sel = Math.min(idx, S.items.length - 1);
  renderStream();
  if (S.sel >= 0) openItem(S.sel); else { $("#thread").hidden = true; $("#thread-empty").hidden = false; }
  toast(`Archived "${(it.subject || "").slice(0, 40)}"`, "ok", {
    label: "Undo",
    fn: async () => {
      await api("/api/mail/unarchive", { method: "POST", body: { threads: tids } });
      toast("Back in the inbox.", "ok");
      setTimeout(() => loadStream(true), 2500);
    },
  });
}

async function summarize(it) {
  const el = $("#aisum"); if (!el) return;
  if (!S.aiOn) { toast("AI is off: add GEMINI_API_KEY to ~/.config/ultra-workstation/.env", "err"); return; }
  const tid = threadsOf(it).slice(-1)[0];
  el.hidden = false; el.innerHTML = `<span class="dim">Summarizing...</span>`;
  try {
    const r = await api("/api/ai/summary", { method: "POST", body: { thread: tid } });
    if (S.key !== it.key) return;
    el.innerHTML = `<div class="aisum-head"><span class="badge ai">AI summary</span><span class="dim mono">${esc(r.model)} &middot; ${esc(r.seconds)} s</span><button class="btn tiny ghost" id="aisum-x">Hide</button></div><div class="body">${esc(r.text)}</div>`;
    $("#aisum-x").onclick = () => { el.hidden = true; };
  } catch (e) { el.innerHTML = `<div class="lint error">${esc(e.message)}</div>`; }
}

function loadContext(it) {
  loadPeople(it).then(() => pollStatus());
}

// ---------------------------------------------------------------- status polling
const SRC = { mail: "sb-mail", slack: "sb-slack", ledger: "sb-ledger", ai: "sb-ai" };
$("#sb-cal") && ($("#sb-cal").title = "Calendar arrives in v0.6");
let lastBuilt = { mail: null, slack: null, tasks: null };
async function pollStatus() {
  if (S.demo) return;
  try {
    const st = await api("/api/status");
    for (const [name, id] of Object.entries(SRC)) {
      const s = st.sources[name] || {};
      const el = document.getElementById(id);
      const label = name[0].toUpperCase() + name.slice(1);
      let cls = "", txt = `${label} --`;
      if (name === "ai") {
        const on = s.enabled;
        el.className = `src ${on ? (s.ok === false ? "err" : "ok") : ""}`;
        el.textContent = on ? `AI ${s.ok === false ? "!" : "on"}` : "AI off";
        el.title = on ? `${s.model}${s.tokens ? ` - ${s.tokens} tokens this session` : ""}${s.error ? ` - ${s.error}` : ""}` : "Set GEMINI_API_KEY to turn on AI";
        S.aiOn = !!on;
        continue;
      }
      if (s.enabled === false) { txt = `${label} off`; }
      else if (s.busy || st.jobs[name]?.state === "running") { cls = "warn"; txt = `${label} ...`; }
      else if (s.ok === true) { cls = "ok"; txt = `${label} ${s.age != null ? age(Date.now() - s.age * 1000) : "ok"}`; }
      else if (s.ok === false) { cls = "err"; txt = `${label} !`; }
      el.className = `src ${cls}`; el.textContent = txt; el.title = s.error || "";
    }
    const mailAge = st.sources.mail?.age, slackAge = st.sources.slack?.age, taskAge = st.sources.tasks?.age;
    const newer = (a, b) => a != null && b != null && a < b;  // age went down = fresh data
    const changed = newer(mailAge, lastBuilt.mail) || newer(slackAge, lastBuilt.slack) || newer(taskAge, lastBuilt.tasks) || (S.loading && mailAge != null);
    lastBuilt = { mail: mailAge, slack: slackAge, tasks: taskAge };
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
    const b = e.target.closest("[data-ra]");
    if (b) {  // a row action: do it without opening the item
      e.stopPropagation();
      busy(b, () => rowAction(b.dataset.ra, S.items[Number(el.dataset.i)]));
      return;
    }
    closeDrawers();  // on a phone, picking a conversation closes the drawer
    openItem(Number(el.dataset.i));
  });
  $("#stream").addEventListener("dragstart", (e) => {
    const el = e.target.closest(".item"); if (!el) return;
    e.dataTransfer.setData("application/x-ultra-item", JSON.stringify(S.items[Number(el.dataset.i)]));
  });

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); palette(); return; }
    if (e.target.matches("input, textarea, select") || !$("#review").hidden || !$("#palette").hidden || !$("#ledger-card").hidden) return;
    const it = S.items[S.sel];
    const click = (a) => { const b = $(`#thread [data-a="${a}"]`); if (b && !b.disabled) b.click(); };
    if (it && S.key === it.key) {
      if (e.key === "r") { e.preventDefault(); click("reply"); return; }
      if (e.key === "a") { e.preventDefault(); click("reply_all"); return; }
      if (e.key === "f") { e.preventDefault(); click("forward"); return; }
      if (e.key === "e") { e.preventDefault(); const c = $('#thread [data-t="complete"]'); if (c) c.click(); else click("archive"); return; }
      if (e.key === "s") { e.preventDefault(); click("summary"); return; }
      if (e.key === "b") { e.preventDefault(); click("bucket"); return; }
      if (e.key === "l") { e.preventDefault(); const tl = $('#thread [data-t="log"]'); if (tl) tl.click(); else click("log"); return; }
      if (e.key === "t") { e.preventDefault(); click("task"); return; }
    }
    if (e.key === "c") { e.preventDefault(); openDraft("new", null).catch((x) => toast(x.message, "err")); return; }
    if (e.key === "?") { toast("j/k move, Enter open, r reply, a reply all, f forward, s summary, e archive, b bucket, l log, t task, c compose, m Mine, w Waiting, g Today, Esc close Today, Ctrl+K commands"); return; }
    if (e.key === "g") { e.preventDefault(); todayOpen() ? closeToday() : openToday(); return; }
    if (e.key === "d") { e.preventDefault(); dayOpen() ? closeDay() : openDay(); return; }
    if (e.key === "Escape" && todayOpen()) { closeToday(); return; }
    if (e.key === "j") { S.sel = Math.min(S.items.length - 1, S.sel + 1); renderStream(); }
    else if (e.key === "k") { S.sel = Math.max(0, S.sel - 1); renderStream(); }
    else if (e.key === "Enter" && S.sel >= 0) openItem(S.sel);
    else if (e.key === "R") $("#btn-refresh").click();
    else if (e.key === "m") $('#filter-seg button[data-f="mine"]').click();
    else if (e.key === "w") $('#filter-seg button[data-f="waiting"]').click();
    else if (e.key === "T") $('#filter-seg button[data-f="tasks"]').click();
  });
  const drawer = (sel) => { $(sel).classList.toggle("open"); $("#scrim").hidden = !$$(".panel.open").length; };
  $("#btn-left").onclick = () => drawer("#left");
  $("#btn-right").onclick = () => drawer("#right");
  $("#scrim").onclick = closeDrawers;
  $("#btn-palette").onclick = () => palette();
  $("#btn-refresh").onclick = (e) => busy(e.currentTarget, async () => {
    await api("/api/refresh", { method: "POST", body: { what: "all" } });
    toast("Refreshing mail" + (S.slackOn ? " and Slack" : "") + "...");
  });

}

// ---------------------------------------------------------------- command palette
function commands() {
  const it = S.items[S.sel];
  const c = [
    { t: "Compose new email", k: "c", run: () => openDraft("new", null) },
    { t: "Refresh everything", k: "Shift+R", run: () => $("#btn-refresh").click() },
    { t: "Log the bucket", run: () => stage("log") },
    { t: "Task from the bucket", run: () => stage("task") },
    { t: "Search the ledger", run: () => searchFor("") },
    { t: "Web search...", run: () => { $('#rail-tabs button[data-tab="tools"]').click(); $("#tw-q")?.focus(); } },
    { t: "Search past research...", run: () => { $('#rail-tabs button[data-tab="tools"]').click(); $("#tr-q")?.focus(); } },
    { t: "New deep research...", run: () => launcher("") },
    { t: "Today (calendar)", run: () => openToday() },
    ...["mine", "waiting", "all", "tasks", "tickets", "slack", "low"].map((f) => ({ t: `Show ${f}`, run: () => $(`#filter-seg button[data-f="${f}"]`).click() })),
  ];
  if (it && S.key === it.key) {
    for (const [a, label, k] of [["reply", "Reply", "r"], ["reply_all", "Reply all", "a"], ["forward", "Forward", "f"], ["summary", "Summarize with AI", "s"], ["archive", "Archive", "e"], ["copy", "Copy thread", ""], ["bucket", "Add to bucket", "b"], ["log", "Log in ledger", "l"], ["task", "Ledger task", "t"]]) {
      c.unshift({ t: `${label}: ${it.subject || ""}`.slice(0, 90), k, run: () => $(`#thread [data-a="${a}"]`)?.click() });
    }
  }
  for (const [i, x] of S.items.entries()) c.push({ t: `Open: ${x.from} - ${x.subject}`.slice(0, 100), run: () => openItem(i) });
  return c;
}

function palette() {
  const box = $("#palette");
  if (!box.hidden) { box.hidden = true; return; }
  box.innerHTML = `<div class="pal-card"><input id="pal-q" placeholder="Type a command or a name..." autocomplete="off"><div id="pal-list" class="pal-list"></div></div>`;
  box.hidden = false;
  const all = commands();
  let sel = 0, shown = all;
  const paint = () => {
    $("#pal-list").innerHTML = shown.slice(0, 40).map((c, i) => `<div class="pal-item ${i === sel ? "sel" : ""}" data-i="${i}"><span>${esc(c.t)}</span>${c.k ? `<span class="kbd">${esc(c.k)}</span>` : ""}</div>`).join("") || `<div class="dim pal-item">No match.</div>`;
  };
  const go = (i) => { const c = shown[i]; box.hidden = true; if (c) Promise.resolve(c.run()).catch((e) => toast(e.message, "err")); };
  const q = $("#pal-q");
  q.oninput = () => {
    const words = q.value.toLowerCase().split(/\s+/).filter(Boolean);
    shown = all.filter((c) => words.every((w) => c.t.toLowerCase().includes(w)));
    sel = 0; paint();
  };
  q.onkeydown = (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(sel + 1, Math.min(shown.length, 40) - 1); paint(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(0, sel - 1); paint(); }
    else if (e.key === "Enter") { e.preventDefault(); go(sel); }
    else if (e.key === "Escape") { box.hidden = true; }
  };
  $("#pal-list").onclick = (e) => { const r = e.target.closest("[data-i]"); if (r) go(Number(r.dataset.i)); };
  box.onclick = (e) => { if (e.target === box) box.hidden = true; };
  paint(); q.focus();
}

async function boot() {
  wire();
  try {
    const s = await api("/api/session");
    TOKEN = s.token; S.tz = s.timezone || "UTC"; S.demo = s.demo; S.slackOn = s.slack; S.aiOn = s.ai;
    if (!s.slack) $('#filter-seg button[data-f="slack"]').hidden = true;
    $("#sb-version").textContent = `ultra ${s.version}${s.demo ? " demo" : ""}`;
  } catch (e) { toast(`Server: ${e.message}`, "err"); }
  $$("#filter-seg button").forEach((b) => { b.dataset.label = b.textContent; });
  tick(); setInterval(tick, 30000);
  onSent((d) => {
    setTimeout(() => loadStream(true), 3000);
    // send-then-log: the log card is for the conversation that was answered
    if (d?.kind === "slack") { const k = d.stream_key || S.key; if (k?.startsWith("s-")) stageAfterSend(k); }
    else if (d?.thread_id) stageAfterSend("g-" + d.thread_id);
  });
  initRail(); wireSearch(); initTools();
  initToday(S.tz, {
    onOpen: () => { if (dayOpen()) closeDay(); S.key = null; S.sel = -1; renderStream(); },
    onClose: () => { $("#thread").hidden = true; $("#thread").innerHTML = ""; $("#thread-empty").hidden = false; },
  });
  initDay({
    onOpen: () => { if (todayOpen()) closeToday(); S.key = null; S.sel = -1; renderStream(); },
    onClose: () => { $("#thread").hidden = true; $("#thread").innerHTML = ""; $("#thread-empty").hidden = false; },
    openItem: (key) => { const i = S.items.findIndex((x) => x.key === key); if (i >= 0) openItem(i); else toast("That item is not in the current filter.", "err"); },
  });
  window.addEventListener("ultra:person", (ev) => openPersonByAddr(ev.detail));
  window.addEventListener("ultra:listen", (ev) => listen(ev.detail.text, ev.detail.title));
  await loadStream();
  pollStatus(); setInterval(pollStatus, 5000);
}
boot();
