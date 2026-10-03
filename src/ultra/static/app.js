// Ultra client shell. Vanilla JS modules, no build step, no inline code (CSP).
// Helpers adapted from the deep-research dashboard (MIT, same author).

import { openDraft, openSlackDraft, resumeForThread, resumeForTask, onSent, openDraftById, applyStudioDraft } from "./compose.js";
import { initBoard, openBoard, closeBoard, boardOpen } from "./board.js";
import { toggleHelp, helpOpen } from "./keys.js";
import { initTidy, openTidy } from "./tidy.js";
import { initGraph, openGraph, closeGraph, graphOpen } from "./graph.js";
import { initRail, wireSearch, loadPeople, addConversation, addSnippet, addEntity as addEntityToBucket, searchFor, stage, stageAfterSend, stageTaskLog, openPersonByAddr } from "./ledger.js";
import { initTools, setToolsThread, webSearch, explain, researchSearch, launcher, readAloud, audioDialog, listen } from "./tools.js";
import { initDay, openDay, closeDay, dayOpen } from "./day.js";
import { initToday, openToday, closeToday, todayOpen } from "./today.js";
import { initLedgerTab, openLedgerTab, closeLedgerTab, ledgerTabOpen } from "./ltab.js";
import { studioStart } from "./studio.js";
import { initSearch, labelMenu, attHtml, wireAttachments, loadNotes, addHighlight, wireHighlightClicks, exportMenu } from "./mailx.js";
import { initAsk, openAsk, askButton } from "./ask.js";
import { searchVault, vaultOn } from "./home.js";
import { ACTIONS, TASK_ACTIONS, ROW_ACTIONS, arrange, popMenu, closeMenu, menuOpen, isCalm } from "./actions.js";

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

// Calendar dates in the operator's configured time zone (S.tz), read from the clock
// at call time. Not UTC (toISOString() rolls over at 8pm Eastern) and not the device
// zone (a phone on Pacific time while travelling would be a day off near midnight).
function tzParts(d) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone: S.tz || undefined, year: "numeric", month: "2-digit", day: "2-digit", weekday: "short" }).formatToParts(d).map((x) => [x.type, x.value]));
  return p;
}
export function localDay(n = 0) {
  const p = tzParts(new Date());
  const base = new Date(`${p.year}-${p.month}-${p.day}T12:00:00Z`);
  base.setUTCDate(base.getUTCDate() + n);
  return base.toISOString().slice(0, 10);
}
function nextFriday() {
  const wd = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 }[tzParts(new Date()).weekday] ?? new Date().getDay();
  const k = (5 - wd + 7) % 7 || 7;
  return localDay(k);
}
function dayName(iso) {
  const [y, mo, da] = iso.split("-").map(Number);
  return new Date(y, mo - 1, da).toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
}

const BADGE = { ARCHIVED: "", VIP: "vip", READY: "ready", OVERDUE: "ready", SLOW: "wait", ASSIGNED: "vip", CRITICAL: "ready", HIGH: "wait", BLOCKED: "ready", STARTED: "vip", "DUE TODAY": "wait", "DUE SOON": "" };

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
  const arch = it.source === "slack"
    ? `<button class="ra ra-arch ra-quick" data-ra="archive" title="Mark done: hide until someone writes again (nothing is sent)">Done</button>`
    : threadsOf(it).length ? `<button class="ra ra-arch ra-quick" data-ra="archive" title="Archive (leaves the inbox; Undo in the toast)">Archive</button>` : "";
  return `<span class="rq">${arch}<button class="ra ra-more" data-ra="more" title="More actions" aria-label="More actions" aria-haspopup="menu">&#8943;</button></span><div class="ract" role="group" aria-label="Actions">
    <button class="ra" data-ra="bucket" title="Add to bucket">+ Bucket</button>
    <button class="ra" data-ra="log" title="${task ? "Log progress on this task" : "Log this conversation in the ledger"}">Log</button>
    ${task ? "" : `<button class="ra" data-ra="task" title="Make a ledger task from it">Task</button>`}
    <button class="ra" data-ra="block" title="Block time for it on your calendar">Block</button>
    <button class="ra ra-ask" data-ra="ask" title="Ask Hermes about it (read-only)">Ask</button>
  </div>`;
}

async function rowAction(a, it, btn = null) {
  if (a === "more") {
    const task = it.source === "task";
    const items = ROW_ACTIONS.filter((r) => r.place === "more" && !(task && r.id === "task"))
      .map((r) => ({ label: r.id === "log" && task ? "Log progress" : r.label, cls: r.id === "ask" ? "ai" : "", run: () => rowAction(r.id, it).catch((e) => toast(e.message, "err")) }));
    popMenu(btn, items, { label: "Row actions" });
    return;
  }
  if (a === "ask") { closeDrawers(); return openAsk({ type: "item", key: it.key }, it.subject || ""); }
  if (a === "archive") return it.source === "slack" ? slackDone(it, { keepOpen: true }) : archive(it, { keepOpen: true });
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
  if (S.search) return;  // a search is showing; Esc or clearing the box returns to the stream
  try {
    const r = await api(`/api/stream?filter=${encodeURIComponent(S.filter)}`);
    const prevKey = S.items[S.sel]?.key;
    S.items = r.items || [];
    S.counts = r.counts || {};
    S.loading = !!r.loading;
    const i = keepSel ? S.items.findIndex((x) => x.key === prevKey) : -1;
    S.sel = i >= 0 ? i : (S.items.length ? 0 : -1);
    renderStream();
    tidyHint();
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
  if (ledgerTabOpen()) closeLedgerTab();
  if (boardOpen()) closeBoard();
  if (graphOpen()) closeGraph();
  if (it.source === "task") return openTask(i);
  S.sel = i; S.key = it.key; renderStream();
  $("#thread-empty").hidden = true;
  const th = $("#thread"); th.hidden = false;
  th.innerHTML = `<h2>${esc(it.subject)}</h2><div class="dim">Loading...</div>`;
  try {
    const t = await api(`/api/thread/${encodeURIComponent(it.key)}`);
    if (S.key !== it.key && S.items[S.sel]?.key !== it.key) return;  // user moved on
    const soon = (v) => `disabled title="Coming in ${v}"`;
    // A ticket card is a group of ServiceNow notice emails: replying to the newest one
    // (reply all keeps the desk on To and the Ref:MSG line) updates the ticket, so it
    // works like any email thread.
    const mailKey = it.key.startsWith("g-") ? it.key : (it.source === "ticket" && threadsOf(it).length ? `g-${newestThread(it, t)}` : "");
    const isMail = !!mailKey;
    const mailOnly = isMail ? "" : `disabled title="Email threads only for now"`;
    th.innerHTML = `<h2>${esc(it.subject)}</h2>
      ${it.reason ? `<div class="why dim">${esc(it.court)} &middot; ${esc(it.reason)}</div>` : ""}
      ${t.error ? `<div class="alert">${esc(t.error)}</div>` : ""}
      ${(t.events || []).length ? `<div class="badges ev">${t.events.map((e) => `<span class="badge">${esc(e)}</span>`).join("")}</div>` : ""}
      ${t.cluster ? `<div class="cl-chips">${(t.cluster.jobs || []).map((j) => `<button class="btn tiny cl-chip" data-job="${esc(j)}" title="Look up job ${esc(j)} on the cluster (read only)">Cluster: job ${esc(j)}</button>`).join("")}${t.cluster.script ? `<button class="btn tiny cl-chip" data-clscript="1" title="Check the batch script in this thread against the cluster (read only)">Check the script</button>` : ""}</div><div id="cl-panel"></div>` : ""}
      <div class="thread-acts">
        <button class="btn small" data-a="reply" ${mailOnly} title="Reply (r)">Reply</button>
        <button class="btn small" data-a="reply_all" ${mailOnly} title="Reply all (a)">Reply all</button>
        <button class="btn small" data-a="forward" ${mailOnly} title="Forward (f)">Forward</button>
        <button class="btn small ai" data-a="summary" ${isMail || it.key.startsWith("k-") ? "" : mailOnly} title="AI summary (s)">Summarize</button>
        ${askButton("btn small ai")}
        <button class="btn small" data-a="archive" ${isMail || it.key.startsWith("k-") ? "" : mailOnly} title="Archive (e). Never deletes.">Archive</button>
        <button class="btn small" data-a="labels" ${isMail ? "" : mailOnly} title="Add or remove Gmail labels">Labels</button>
        <button class="btn small" data-a="copy">Copy</button>
        <button class="btn small" data-a="export" title="Export: Markdown, text, JSON, Print/PDF (highlights included)">Export</button>
        <button class="btn small" data-a="listen" title="Read aloud with the browser voice (free)">Read aloud</button>
        <button class="btn small ai" data-a="aiaudio" title="AI voice: spoken summary or full read">AI audio</button>
        <button class="btn small" data-a="bucket" title="Add to bucket (b)">+ Bucket</button>
        <button class="btn small" data-a="log" title="Log this conversation in the ledger (l)">Log</button>
        <button class="btn small" data-a="task" title="Make a ledger task from it (t)">Task</button>
        <button class="btn small" data-a="block" title="Block time for this on your calendar">Block time</button>
        ${t.permalink ? `<a class="btn small" href="${esc(t.permalink)}" target="_blank" rel="noopener noreferrer">Open in Slack</a>` : ""}
      </div>
      <div class="aisum" id="aisum" hidden></div>
      <section class="studio" id="studio" hidden></section>
      <section class="composer" id="composer" hidden></section>
      <div class="notes-list" id="notes-list"></div>
      ${(t.messages || []).map((m) => `<div class="msg ${m.mine ? "mine" : ""}" data-mid="${esc(m.id || "")}">
        <div class="hdr"><b>${esc(m.from)}</b><span class="mono">${esc(fmtTime(m.ts))}</span></div>
        ${m.to ? `<div class="to dim">to ${esc(m.to)}${m.cc ? ` &middot; cc ${esc(m.cc)}` : ""}</div>` : ""}
        <div class="body">${esc(m.body) || `<span class="dim">(no text)</span>`}</div>
        ${m.quoted ? `<details class="quoted"><summary>quoted text</summary><div class="body">${esc(m.quoted)}</div></details>` : ""}
        ${isMail ? attHtml(m) : ((m.attachments || []).length ? `<div class="atts">${m.attachments.map((a) => `<span class="att" title="${esc(a.mime)}">${esc(a.name)} <span class="dim">${esc(Math.round((a.size || 0) / 1024))} KB</span></span>`).join("")}</div>` : "")}
      </div>`).join("") || `<div class="dim">No messages.</div>`}`;
    $('[data-a="copy"]', th).onclick = () => copyText((t.messages || []).map((m) => `${m.from} (${fmtTime(m.ts)})\n${m.body}`).join("\n\n"));
    for (const k of ["reply", "reply_all", "forward"]) {
      const b = $(`[data-a="${k}"]`, th);
      b.onclick = () => busy(b, () => openDraft(k, mailKey));
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
    $("[data-ask]", th).onclick = () => openAsk({ type: "item", key: it.key }, it.subject || "");
    const lb = $('[data-a="labels"]', th); lb.onclick = () => busy(lb, () => labelMenu(lb, it));
    const xb = $('[data-a="export"]', th); xb.onclick = () => exportMenu(xb, it.subject || "", t.messages || []);
    if (isMail) wireAttachments(th, () => Number($("#composer")?.dataset.did || 0));
    wireHighlightClicks(th);
    loadNotes(th, it.key, t.messages || []);
    $('[data-a="bucket"]', th).onclick = () => addConversation(it.key);
    $('[data-a="listen"]', th).onclick = () => readAloud(th);
    $('[data-a="aiaudio"]', th).onclick = () => audioDialog({ thread: it.key }, it.subject || "Conversation");
    setToolsThread(it.key, it.subject);
    if (S.aiOn && isMail) studioStart(mailKey); else studioStart(null);  // idle: runs only when asked
    $('[data-a="log"]', th).onclick = () => stage("log", it.key);
    $('[data-a="task"]', th).onclick = () => stage("task", it.key);
    $('[data-a="block"]', th).onclick = () => window.dispatchEvent(new CustomEvent("ultra:block", { detail: { key: it.key, subject: it.subject } }));
    wireSelection(th, it);
    if (t.cluster) wireCluster(th, it, t, mailKey);
    const ab = $('[data-a="archive"]', th);
    if (it.source === "slack") { ab.disabled = false; ab.textContent = "Mark done"; ab.title = "Hide until a new message arrives (e). Nothing is sent to Slack."; ab.onclick = () => busy(ab, () => slackDone(it)); }
    else ab.onclick = () => busy(ab, () => archive(it));
    arrange($(".thread-acts", th), ACTIONS, "a");  // calm layout: Reply, Archive, AI, ... (SPEC 7.13)
    if (it.source === "slack") await resumeSlack(it); else await resumeForThread(it.key);
  } catch (e) { th.innerHTML = `<div class="dim">${esc(e.message)}</div>`; }
  loadContext(it);
}

// ---------------------------------------------------------------- cluster (SPEC 8.9)
// A job id or a batch script in the thread shows a chip; opening it reads the cluster
// server (bifrost, read tiers only). Cluster text is data: always set as text here.
function wireCluster(th, it, t, mailKey) {
  const panel = $("#cl-panel", th);
  const lastText = () => {
    const theirs = (t.messages || []).filter((m) => !m.mine);
    const m = theirs[theirs.length - 1] || (t.messages || [])[t.messages.length - 1] || {};
    return `${m.subject || it.subject || ""}\n${m.body || ""}`.slice(0, 4000);
  };
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  th.querySelectorAll(".cl-chip[data-job]").forEach((b) => (b.onclick = () => busy(b, async () => {
    panel.replaceChildren(el("div", "dim small-t", `Reading job ${b.dataset.job} on the cluster...`));
    let r;
    try { r = await api("/api/cluster/job", { method: "POST", body: { job_id: b.dataset.job, ticket_text: lastText(), lines: 60 } }); }
    catch (e) { panel.replaceChildren(el("div", "lint warning", e.message)); return; }
    const card = el("section", "cl-card");
    const s = r.show || {}, x = r.explain || {}, d = r.draft;
    const head = el("div", "cl-head");
    head.append(el("b", "", `Job ${r.job_id}`), el("span", `badge ${s.state === "FAILED" || s.state === "TIMEOUT" || s.state === "OUT_OF_MEMORY" ? "ready" : s.state === "COMPLETED" ? "" : "wait"}`, s.state || "?"),
      el("span", "dim small-t", [s.name && `"${s.name}"`, s.user, s.partition, s.elapsed_s != null && `${Math.round(s.elapsed_s / 60)} min`, s.exit_code && `exit ${s.exit_code}`].filter(Boolean).join(" \u00b7 ")));
    card.append(head);
    const eff = s.efficiency || {};
    if (eff.cpu_percent != null) card.append(el("div", "dim small-t", `CPU ${eff.cpu_percent}% of allocated, memory peak ${eff.mem_peak_mb} MB of ${eff.mem_alloc_mb} MB`));
    for (const f of x.findings || []) {
      const row = el("div", `cl-find ${f.severity || ""}`);
      row.append(el("b", "", f.title || f.rule || "finding"));
      if ((f.evidence || []).length) row.append(el("div", "dim small-t", f.evidence.join("; ")));
      if (f.suggestion) row.append(el("div", "small-t", f.suggestion));
      card.append(row);
    }
    const logText = x.log_tail_untrusted?.text || "";
    if (logText) {
      const det = el("details", "cl-log");
      det.append(el("summary", "small-t", `Log (end)${x.log_path ? `: ${x.log_path}` : ""}`), el("pre", "mono small-t", logText.slice(-12000)));
      card.append(det);
    }
    const acts = el("div", "cl-acts");
    if (d && d.reply_draft && mailKey) {
      const use = el("button", "btn tiny ai", "Use as reply"); use.title = "Put the cluster's draft in the reply-all draft as a new AI version. Edit it; it still needs both approvals.";
      use.onclick = () => busy(use, async () => { await applyStudioDraft(mailKey, d.reply_draft, `Cluster: job ${r.job_id}`); toast("The cluster's draft is in the reply. Edit, then approve twice to send.", "ok"); });
      acts.append(use);
      acts.append(el("span", "dim small-t", `draft confidence: ${d.confidence || "?"}`));
    }
    const ask = el("button", "btn tiny ai", "Ask Hermes"); ask.onclick = () => openAsk({ type: "text", key: it.key, text: JSON.stringify({ job: s, findings: x.findings || [] }).slice(0, 20000), title: `Job ${r.job_id}` }, `Job ${r.job_id}`);
    const copy = el("button", "btn tiny", "Copy facts"); copy.onclick = () => copyText([`Job ${r.job_id}: ${s.state} on ${s.partition}, exit ${s.exit_code}`, ...(x.findings || []).map((f) => `- ${f.title}: ${(f.evidence || []).join("; ")}`)].join("\n"));
    const x2 = el("button", "btn tiny ghost", "Close"); x2.onclick = () => panel.replaceChildren();
    acts.append(ask, copy, x2);
    card.append(acts);
    panel.replaceChildren(card);
  })));
  const sc = th.querySelector(".cl-chip[data-clscript]");
  if (sc) sc.onclick = () => busy(sc, async () => {
    const text = (t.messages || []).map((m) => m.body || "").join("\n");
    const i = text.search(/^#!.*\n?(?:.*\n)*?#SBATCH|^#SBATCH/m);
    const script = text.slice(Math.max(0, i)).split(/\n\s*\n\s*\n/)[0].slice(0, 20000);
    let r;
    try { r = await api("/api/cluster/script", { method: "POST", body: { script } }); }
    catch (e) { panel.replaceChildren(el("div", "lint warning", e.message)); return; }
    const c = r.check || {}, card = el("section", "cl-card");
    card.append(el("b", "", `Script check: ${c.ok ? "no blocking problems" : "problems found"}`));
    if (c.est_max_cost_usd != null) card.append(el("div", "dim small-t", `Partition ${c.partition || "?"}; worst-case cost about $${c.est_max_cost_usd}`));
    for (const i2 of c.issues || []) card.append(el("div", `cl-find ${i2.severity || ""}`, `${i2.line ? `line ${i2.line}: ` : ""}${i2.message}`));
    const x2 = el("button", "btn tiny ghost", "Close"); x2.onclick = () => panel.replaceChildren();
    card.append(x2);
    panel.replaceChildren(card);
  });
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
    const canHl = /^(g|k|s)-/.test(it.key);
    bar.innerHTML = `<button class="btn tiny" data-s="copy">Copy</button>
      ${canHl ? `<button class="btn tiny hl-btn" data-s="hl" title="Highlight (local only)">Highlight</button>` : ""}
      <button class="btn tiny" data-s="quote">Quote in reply</button>
      <button class="btn tiny" data-s="search" ${short ? "" : "disabled title=\"Select 200 characters or fewer\""}>Search ledger</button>
      <button class="btn tiny" data-s="web">Web search</button>
      <button class="btn tiny" data-s="bucket">Add to bucket</button>
      <span class="more"><button class="btn tiny" data-s="more">More</button>
        <span class="more-menu" hidden>
          <button class="btn tiny ai" data-s="explain">Explain</button>
          <button class="btn tiny ai" data-s="ask">Ask Hermes</button>
          <button class="btn tiny" data-s="rsearch">Search research</button>
          <button class="btn tiny" data-s="research">Research this...</button>
          <button class="btn tiny" data-s="read">Read aloud</button>
          ${canHl ? `<button class="btn tiny" data-s="hlnote">Highlight with note</button><button class="btn tiny" data-s="hlcyan">Highlight cyan</button><button class="btn tiny" data-s="hlmag">Highlight magenta</button><button class="btn tiny" data-s="hlgreen">Highlight green</button>` : ""}
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
      else if (a === "ask") openAsk({ type: "text", text: text.slice(0, 20000), key: it.key, title: `Selection in ${it.subject || "item"}` }, `Selection: ${text.slice(0, 60)}`);
      else if (a === "rsearch") researchSearch(text.slice(0, 500));
      else if (a === "research") launcher(text.slice(0, 2000));
      else if (a === "read") readAloud(null, text);
      else if (a === "quote") quoteIntoReply(text);
      else if (a === "hl") addHighlight(th, text, "amber");
      else if (a === "hlnote") addHighlight(th, text, "amber", true);
      else if (a === "hlcyan") addHighlight(th, text, "cyan");
      else if (a === "hlmag") addHighlight(th, text, "magenta");
      else if (a === "hlgreen") addHighlight(th, text, "green");
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
async function slackDone(it, { keepOpen = false } = {}) {
  await api("/api/slack/done", { method: "POST", body: { key: it.key } });
  removeRow(it, keepOpen);
  toast("Marked done. It comes back if someone writes again.", "ok", {
    label: "Undo", fn: async () => { await api("/api/slack/undone", { method: "POST", body: { key: it.key } }); loadStream(true); },
  });
}

// Take a row out of the stream. From a row button (keepOpen) the open conversation stays
// open unless it is the one removed; from the thread view the next one opens.
function removeRow(it, keepOpen = false) {
  const idx = S.items.findIndex((x) => x.key === it.key);
  const openKey = S.key;
  S.items = S.items.filter((x) => x.key !== it.key);
  if (keepOpen && openKey && openKey !== it.key) {
    S.sel = S.items.findIndex((x) => x.key === openKey);
    renderStream();
    return;
  }
  S.sel = Math.min(idx, S.items.length - 1);
  renderStream();
  if (keepOpen && !openKey) return;
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
  const overdue = due && t.status !== "DONE" && due < localDay(0);
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
      <span class="due-edit" title="Due date in the ledger"><label for="t-due" class="dim small-t">Due</label>
        <input type="date" id="t-due" value="${esc(due)}">
        <select id="t-due-q" title="Quick due date"><option value="">Quick...</option><option value="0">Today</option><option value="1">Tomorrow</option><option value="fri">This Friday</option><option value="7">In a week</option><option value="14">In 2 weeks</option>${due ? `<option value="clear">Clear due date</option>` : ""}</select></span>
      <select id="t-snooze" title="Hide from the stream for a while (local only)"><option value="">Snooze...</option><option value="1">1 day</option><option value="3">3 days</option><option value="7">1 week</option>${t.snoozed_until ? `<option value="0">Unsnooze</option>` : ""}</select>
      <button class="btn small" data-t="log" title="Log progress on this task (l)">Log update</button>
      <button class="btn small" data-t="bucket" title="Add to bucket (b)">+ Bucket</button>
      <button class="btn small" data-t="blocktime" title="Block time for this task on your calendar">Block time</button>
      <button class="btn small" data-t="copy">Copy</button>
      ${askButton("btn small ai").replace('data-ask="1"', 'data-ask="1" data-t="ask"')}
      <button class="btn small ai" data-t="email" title="Draft an email that moves this task forward: Draft Studio reads the task, related mail, your past emails, policy pages and the ledger" ${S.aiOn ? "" : "disabled"}>Draft email</button>
    </div>
    <section class="studio" id="studio" hidden></section>
    <section class="composer" id="composer" hidden></section>
    <div class="sect"><span class="label">Linked in the ledger (${d.links.length})</span>
      ${d.links.length ? `<ul class="clist">${d.links.map((x) => `<li class="ent" draggable="true" data-ent='${esc(JSON.stringify({ id: x.id, name: x.name, type: x.type }))}'><span class="badge">${esc(x.type)}</span> ${esc(x.name)} <span class="dim small-t">${esc(x.edge)}</span></li>`).join("")}</ul>` : `<div class="dim small-t">No links. Drag people or projects onto it via the bucket.</div>`}</div>
    <div class="dim small-t mono">${esc(t.id)}</div>`;
  studioStart(null);
  resumeForTask(t.id);
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
    if (a === "blocktime") { window.dispatchEvent(new CustomEvent("ultra:block", { detail: { key: `t-${t.id}`, subject: t.summary } })); return; }
    if (a === "ask") { openAsk({ type: "item", key: `t-${t.id}` }, t.summary); return; }
    if (a === "email") { studioStart(`t-${t.id}`, { run: true }); $("#studio")?.scrollIntoView({ block: "nearest" }); return; }
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
  const setDue = async (want) => {
    const cur = due || "";
    if (want === cur) return;
    const msg = want ? `Set the due date in the ledger?\n\n${t.summary}\n\n${cur || "(none)"} -> ${want} (${dayName(want)})`
      : `Clear the due date in the ledger?\n\n${t.summary}\n\nWas ${cur}`;
    if (!confirm(msg)) { $("#t-due").value = cur; $("#t-due-q").value = ""; return; }
    $("#t-due").disabled = true; $("#t-due-q").disabled = true;
    let r;
    try { r = await api("/api/task/action", { method: "POST", body: { id: t.id, action: "due", due: want } }); }
    catch (err) { toast(err.message, "err"); openTask(S.items.indexOf(it)); return; }
    if (r.ok === false) { toast(`The ledger did not confirm: ${r.output_tail || "unknown"}`, "err"); openTask(S.items.indexOf(it)); return; }
    toast(want ? `Due ${want} (read back from the ledger).` : "Due date cleared.", "ok");
    it.due_date = want; openTask(S.items.indexOf(it)); setTimeout(() => loadStream(true), 2500);
  };
  $("#t-due").onchange = (e) => { const v = e.target.value; if (v && !/^\d{4}-\d{2}-\d{2}$/.test(v)) return; setDue(v); };
  $("#t-due-q").onchange = (e) => {
    const v = e.target.value; if (!v) return;
    setDue(v === "clear" ? "" : v === "fri" ? nextFriday() : localDay(Number(v)));
  };
  $("#t-snooze").onchange = async (e) => {
    const v = e.target.value; if (v === "") return;
    if (v === "0") { await act({ action: "unsnooze" }, "Unsnoozed."); loadStream(true); return; }
    if (await act({ action: "snooze", days: Number(v) }, `Snoozed for ${v} day(s). Only on this laptop.`)) removeRow(it);
  };
  th.ondragstart = (e) => { const li = e.target.closest("[data-ent]"); if (li) e.dataTransfer.setData("application/x-ultra-entity", li.dataset.ent); };
  arrange($(".thread-acts", th), TASK_ACTIONS, "t");
  if (S.key === it.key) loadPeople(it);  // v0.8: the task's people + everyone named in it, and the Full tab
}

function threadsOf(it) {
  if (it.key.startsWith("g-")) return [it.key.slice(2)];
  return it.threads || [];
}
// The ticket's thread with the newest message (its messages carry thread_id when the
// server knows it; otherwise the last thread the stream listed).
function newestThread(it, t) {
  const msgs = (t?.messages || []).filter((m) => m.thread_id);
  if (msgs.length) return msgs.reduce((a, b) => (String(b.ts) > String(a.ts) ? b : a)).thread_id;
  const ids = threadsOf(it);
  return ids[ids.length - 1];
}

async function archive(it, { keepOpen = false } = {}) {
  if (S.demo) { toast("Demo mode: nothing is archived."); return; }
  const tids = threadsOf(it);
  if (!tids.length) return;
  await api("/api/mail/archive", { method: "POST", body: { threads: tids } });
  removeRow(it, keepOpen);
  // Show it when it matters (SPEC 7.13): a READY thread was finished work, so offer the
  // log card right away (the toast below still offers Undo)
  if ((it.badges || []).includes("READY") && isCalm()) {
    setTimeout(() => toast("That one looked done. Log it?", "", { label: "Log it", fn: () => stage("log", it.key) }), 400);
  }
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
const SRC = { mail: "sb-mail", calendar: "sb-cal", slack: "sb-slack", ledger: "sb-ledger", ai: "sb-ai" };
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
    renderMcpStatus(st.sources.mcp || {}, st.sources.vault);
    if (st.sources.vault) S.vaultOn = !!st.sources.vault.enabled;
    paintDot();
    const mailAge = st.sources.mail?.age, slackAge = st.sources.slack?.age, taskAge = st.sources.tasks?.age;
    const newer = (a, b) => a != null && b != null && a < b;  // age went down = fresh data
    const changed = newer(mailAge, lastBuilt.mail) || newer(slackAge, lastBuilt.slack) || newer(taskAge, lastBuilt.tasks) || (S.loading && mailAge != null);
    lastBuilt = { mail: mailAge, slack: slackAge, tasks: taskAge };
    if (changed) loadStream(true);
    const err = st.sources.mail?.error;
    if (err && st.sources.mail?.auth && !S.authWarned) { S.authWarned = true; toast(err, "err"); }
  } catch { /* server restarting */ }
}

// Hosted MCP servers (SPEC 8.8): one light each, only for configured servers
function renderMcpStatus(mcp, vault) {
  const box = document.getElementById("sb-mcp");
  if (!box) return;
  box.replaceChildren();
  if (vault?.enabled) {
    // the notes vault (SPEC 8.10): a local stdio MCP server, started on first use
    const el = document.createElement("span");
    const cls = vault.ok === true ? "ok" : vault.ok === false ? "err" : "";
    el.className = `src ${cls}`; el.textContent = `Notes ${cls === "ok" ? "ok" : cls === "err" ? "!" : "--"}`;
    el.title = vault.error || `vault ${vault.name || ""}, read only`;
    box.appendChild(el);
  }
  const label = { nexus: "Ledger MCP", ursa: "Cluster" };
  for (const [name, s] of Object.entries(mcp)) {
    const el = document.createElement("span");
    let cls = "", txt = `${label[name] || name} --`, tip = s.error || "";
    if (!s.signed_in) { cls = "warn"; txt = `${label[name] || name} sign in`; tip = `Run: ultra auth ${name}`; }
    else if (s.ok === true) { cls = "ok"; txt = `${label[name] || name} ok`; tip = `${s.version ? String(s.version).replace(/^(\d+)$/, "tools v$1") : ""}${s.ms != null ? ` - last call ${s.ms} ms` : ""}`; }
    else if (s.ok === false) { cls = "err"; txt = `${label[name] || name} !`; }
    el.className = `src ${cls}`; el.textContent = txt; el.title = tip;
    box.appendChild(el);
  }
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
      if (b.dataset.ra === "more") { rowAction("more", S.items[Number(el.dataset.i)], b); return; }
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
    if (e.key === "Escape" && helpOpen()) { toggleHelp(); return; }
    if (menuOpen()) { if (e.key === "Escape") { e.preventDefault(); closeMenu(); } return; }
    // Browser and OS shortcuts (Ctrl+R, Ctrl+F, Ctrl+C, Ctrl+A, Alt+...) are never ours
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target.matches("input, textarea, select, [contenteditable=true]") || !$("#review").hidden || !$("#palette").hidden || !$("#ledger-card").hidden || !$("#ask").hidden || helpOpen()) return;
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
      if (e.key === "h") { e.preventDefault(); const hb = $("#thread [data-ask]"); if (hb && !hb.disabled) hb.click(); return; }
    }
    if (e.key === "c") { e.preventDefault(); composeNew(); return; }
    if (e.key === "o") { e.preventDefault(); boardOpen() ? closeBoard() : openBoard(); return; }
    if (e.key === "v") { e.preventDefault(); graphOpen() ? closeGraph() : openGraph(); return; }
    if (e.key === "Escape" && graphOpen()) { closeGraph(); return; }
    if (e.key === "Escape" && boardOpen()) { closeBoard(); return; }
    if (e.key === "?") { e.preventDefault(); toggleHelp(); return; }
    if (e.key === "g") { e.preventDefault(); todayOpen() ? closeToday() : openToday(); return; }
    if (e.key === "d") { e.preventDefault(); dayOpen() ? closeDay() : openDay(); return; }
    if (e.key === "n") { e.preventDefault(); ledgerTabOpen() ? closeLedgerTab() : openLedgerTab(); return; }
    if (e.key === "Escape" && todayOpen()) { closeToday(); return; }
    if (e.key === "Escape" && ledgerTabOpen()) { closeLedgerTab(); return; }
    if (e.key === "Escape" && dayOpen()) { closeDay(); return; }
    if (e.key === "j") { S.sel = Math.min(S.items.length - 1, S.sel + 1); renderStream(); }
    else if (e.key === "k") { S.sel = Math.max(0, S.sel - 1); renderStream(); }
    else if (e.key === "Enter" && S.sel >= 0) openItem(S.sel);
    else if (e.key === "R") $("#btn-refresh").click();
    else if (e.key === "m") $('#filter-seg button[data-f="mine"]').click();
    else if (e.key === "w") $('#filter-seg button[data-f="waiting"]').click();
    else if (e.key === "T") $('#filter-seg button[data-f="tasks"]').click();
    else if (e.key === "L") setLayout(isCalm() ? "classic" : "calm");
  });
  const drawer = (sel) => { $(sel).classList.toggle("open"); $("#scrim").hidden = !$$(".panel.open").length; };
  $("#btn-left").onclick = () => drawer("#left");
  $("#btn-right").onclick = () => drawer("#right");
  $("#scrim").onclick = closeDrawers;
  vaultOn().then((on) => { S.vaultOn = on; });
  $("#btn-palette").onclick = () => palette();
  $("#btn-help").onclick = () => toggleHelp();
  $("#btn-layout").onclick = () => setLayout("calm");
  $("#btn-refresh").onclick = (e) => busy(e.currentTarget, async () => {
    await api("/api/refresh", { method: "POST", body: { what: "all" } });
    toast("Refreshing mail" + (S.slackOn ? " and Slack" : "") + "...");
  });

}


// "Show it when it matters" (SPEC 7.13): Tidy comes forward as one line at the top of
// the stream when the rule finds enough threads; otherwise it lives in the filter menu.
let tidyAt = 0, tidyN = 0;
async function tidyHint() {
  if (!isCalm() || S.search) return;
  if (Date.now() - tidyAt > 60000) {
    tidyAt = Date.now();
    try { const r = await api("/api/mail/tidy/count"); tidyN = r.count >= r.suggest_at ? r.count : 0; } catch { tidyN = 0; }
  }
  $("#tidy-hint")?.remove();
  if (!tidyN || localStorage.getItem("ultra.tidyHintOff") === localDay(0)) return;
  const d = document.createElement("div");
  d.id = "tidy-hint"; d.className = "hint-line";
  const b = document.createElement("button"); b.className = "btn tiny"; b.textContent = "Preview";
  b.onclick = () => openTidy();
  const x = document.createElement("button"); x.className = "btn tiny ghost"; x.textContent = "Not today"; x.title = "Hide this until tomorrow";
  x.onclick = () => { localStorage.setItem("ultra.tidyHintOff", localDay(0)); d.remove(); };
  const t = document.createElement("span"); t.className = "grow"; t.textContent = `${tidyN} old threads can be tidied`;
  d.append(t, b, x);
  $("#stream").prepend(d);
}

// ---------------------------------------------------------------- calm shell (SPEC 7.13)
// Three places (Inbox, Today, Ledger), a filter menu, one status dot, the rail only when
// something is open, the Bucket as a pill only when it holds something. Every view,
// key and action is the same as the classic layout; only where they sit changes.
const PLACE_OF = { "btn-today": "today", "btn-day": "today", "btn-ledger": "ledger", "btn-graph": "ledger", "btn-board": "inbox" };

function syncPlaces() {
  let place = "inbox";
  for (const [id, pl] of Object.entries(PLACE_OF)) if ($(`#${id}`)?.classList.contains("on")) place = pl;
  $$("#places button").forEach((b) => b.classList.toggle("on", b.dataset.place === place));
  const view = boardOpen() || graphOpen() || dayOpen() || todayOpen() || ledgerTabOpen();
  document.body.classList.toggle("idle", $("#thread").hidden);
  document.body.classList.toggle("wide", !!view);  // Board, Today, Ledger use the rail's width
  document.body.classList.toggle("boardview", boardOpen());  // the Board takes the stream's width too
}

function closeViews() {
  if (boardOpen()) closeBoard();
  if (graphOpen()) closeGraph();
  if (dayOpen()) closeDay();
  if (todayOpen()) closeToday();
  if (ledgerTabOpen()) closeLedgerTab();
}

function goPlace(place) {
  if (place === "inbox") { closeViews(); if (S.sel >= 0 && S.items[S.sel]) openItem(S.sel); return; }
  if (place === "today") return todayOpen() || dayOpen() ? null : openToday();
  if (place === "ledger") return ledgerTabOpen() || graphOpen() ? null : openLedgerTab();
}

const EXTRA_FILTERS = [["slack", "Slack"], ["tasks", "Tasks", "T"], ["tickets", "Tickets"], ["low", "Low priority"]];
function filterMenu(btn) {
  const n = (f) => (S.counts[f] ? ` (${S.counts[f]})` : "");
  const items = EXTRA_FILTERS.filter(([f]) => f !== "slack" || S.slackOn)
    .map(([f, label, key]) => ({ label: `${S.filter === f ? "\u2713 " : ""}${label}${n(f)}`, key, run: () => $(`#filter-seg button[data-f="${f}"]`).click() }));
  items.push({ sep: true },
    { label: "Board view", key: "o", run: () => openBoard() },
    { label: "Refresh mail and Slack", key: "R", run: () => $("#btn-refresh").click() },
    { label: "Tidy the inbox...", run: () => openTidy() });
  popMenu(btn, items, { label: "Filters and views" });
}

function paintDot() {
  const lights = $$("#statusbar .src");
  const worst = lights.some((l) => l.classList.contains("err")) ? "err" : lights.some((l) => l.classList.contains("warn")) ? "warn" : lights.some((l) => l.classList.contains("ok")) ? "ok" : "";
  const dot = $("#sdot"); if (!dot) return;
  dot.className = `btn ghost calm-only sdot ${worst}`;
  const bad = lights.filter((l) => l.classList.contains("err") || l.classList.contains("warn")).map((l) => l.textContent.trim());
  dot.title = bad.length ? `Needs a look: ${bad.join(", ")}` : "All sources fine";
}

function dotMenu(btn) {
  const items = $$("#statusbar .src").filter((l) => l.textContent.trim())
    .map((l) => ({ info: l.textContent.trim() + (l.title ? `  -  ${l.title}` : ""), cls: `src ${[...l.classList].filter((c) => c !== "src").join(" ")}`, title: l.title }));
  if (S.demo) items.unshift({ info: "Demo data: nothing is sent or written." });
  items.push({ sep: true },
    { label: "Refresh mail and Slack", key: "R", run: () => $("#btn-refresh").click() },
    { label: "Keyboard shortcuts", key: "?", run: () => toggleHelp() },
    { label: "Use the classic layout", key: "L", title: "This browser only; [ui] layout in config sets the default. Switch back with L or the Calm layout button.", run: () => setLayout("classic") },
    { info: `ultra ${S.version || ""}`, cls: "dim mono" });
  popMenu(btn, items, { label: "Sources" });
}

function setLayout(v) { localStorage.setItem("ultra.layout", v); location.reload(); }

function initCalm() {
  $("#places")?.addEventListener("click", (e) => { const b = e.target.closest("[data-place]"); if (b) goPlace(b.dataset.place); });
  $("#btn-filters")?.addEventListener("click", (e) => filterMenu(e.currentTarget));
  $("#sdot")?.addEventListener("click", (e) => dotMenu(e.currentTarget));
  // view headers link to their neighbours in the same place (Calendar <-> Plan, Ledger <-> Map, Board -> List)
  document.addEventListener("click", (e) => {
    const g = e.target.closest("[data-goto]"); if (!g) return;
    const to = g.dataset.goto;
    if (to === "day") openDay(); else if (to === "today") openToday();
    else if (to === "graph") openGraph(); else if (to === "ledger") openLedgerTab();
    else if (to === "inbox") goPlace("inbox");
  });
  const watch = new MutationObserver(syncPlaces);
  for (const id of Object.keys(PLACE_OF)) { const b = $(`#${id}`); if (b) watch.observe(b, { attributes: true, attributeFilter: ["class"] }); }
  watch.observe($("#thread"), { attributes: true, attributeFilter: ["hidden"] });
  // Ledger search and Research (palette, selection bar) open in the rail: keep it showing
  const pin = () => document.body.classList.toggle("railpin", !$("#rt-search").hidden || !$("#rt-tools").hidden);
  const pins = new MutationObserver(pin);
  for (const id of ["#rt-search", "#rt-tools"]) pins.observe($(id), { attributes: true, attributeFilter: ["hidden"] });
  // the Bucket tray shows while something is dragged, so there is somewhere to drop it
  document.addEventListener("dragstart", () => document.body.classList.add("dragging"));
  for (const ev of ["dragend", "drop"]) document.addEventListener(ev, () => document.body.classList.remove("dragging"));
  $("#bucket-head-toggle")?.addEventListener("click", () => $("#bucket").classList.toggle("expanded"));
  if (!isCalm()) $("#sdot")?.remove();
  syncPlaces();
}

// ---------------------------------------------------------------- command palette
function commands() {
  const it = S.items[S.sel];
  const c = [
    { t: "Compose new email", k: "c", run: () => composeNew() },
    { t: "Keyboard shortcuts", k: "?", run: () => toggleHelp() },
    { t: isCalm() ? "Use the classic layout (this browser)" : "Use the calm layout (this browser)", k: "L", run: () => setLayout(isCalm() ? "classic" : "calm") },
    { t: "Day: check-in plan and end-of-day report", k: "d", run: () => openDay() },
    { t: "Tidy the inbox (preview a bulk archive)", k: "", run: () => openTidy() },
    { t: "Graph: your ledger neighborhood", k: "v", run: () => openGraph() },
    { t: "Board: my court, waiting on, watching, done", k: "o", run: () => openBoard() },
    { t: "Refresh everything", k: "Shift+R", run: () => $("#btn-refresh").click() },
    { t: "Log the bucket", run: () => stage("log") },
    { t: "Task from the bucket", run: () => stage("task") },
    { t: "Search the ledger", run: () => searchFor("") },
    ...(S.vaultOn ? [{ t: "Search my notes (vault)...", run: () => searchVault("") }] : []),
    { t: "Web search...", run: () => { $('#rail-tabs button[data-tab="tools"]').click(); $("#tw-q")?.focus(); } },
    { t: "Search past research...", run: () => { $('#rail-tabs button[data-tab="tools"]').click(); $("#tr-q")?.focus(); } },
    { t: "New deep research...", run: () => launcher("") },
    { t: "Ask Hermes (no item attached)...", run: () => openAsk({ type: "none" }, "") },
    { t: "Today (calendar)", run: () => openToday() },
    { t: "Ledger: home", k: "n", run: () => openLedgerTab("home") },
    { t: "Ledger: task board", run: () => openLedgerTab("tasks") },
    { t: "Ledger: browse people, labs, projects...", run: () => openLedgerTab("browse") },
    { t: "Ledger: interactions", run: () => openLedgerTab("interactions") },
    { t: "Ledger: org tree", run: () => openLedgerTab("org") },
    { t: "Ledger: health and audit reports", run: () => openLedgerTab("reports") },
    { t: "Search mail...", k: "/", run: () => $("#ms-q")?.focus() },
    ...["mine", "waiting", "all", "tasks", "tickets", "slack", "low"].map((f) => ({ t: `Show ${f}`, run: () => $(`#filter-seg button[data-f="${f}"]`).click() })),
  ];
  if (it && S.key === it.key) {
    // every action on the open item, from the action table (SPEC 7.13): hidden in a menu
    // is never more than a palette search away
    const task = it.source === "task";
    const table = task ? TASK_ACTIONS : ACTIONS, attr = task ? "t" : "a";
    const found = (a) => (a.virtual ? $(a.virtual) : a.id === "ask" ? $("#thread [data-ask]") : a.id === "permalink" ? $("#thread .thread-acts a.btn[href]") : $(`#thread [data-${attr}="${a.id}"]`));
    for (const a of [...table].reverse()) {
      const el = found(a);
      if (!el || el.hidden || el.disabled) continue;
      c.unshift({ t: `${el.textContent.trim() || a.label}: ${it.subject || ""}`.slice(0, 90), k: a.key || "", run: () => el.click() });
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
    // the search box is the palette: whatever was typed can also be searched for
    const text = q.value.trim();
    if (text) {
      shown = shown.concat([
        { t: `Search mail for "${text.slice(0, 60)}"`, k: "/", run: () => { const m = $("#ms-q"); if (!m) return; closeViews(); m.value = text.slice(0, 300); m.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); } },
        { t: `Search the ledger for "${text.slice(0, 60)}"`, run: () => searchFor(text) },
        { t: `Search past research for "${text.slice(0, 60)}"`, run: () => researchSearch(text.slice(0, 300)) },
        ...(S.vaultOn ? [{ t: `Search my notes for "${text.slice(0, 60)}"`, run: () => searchVault(text.slice(0, 300)) }] : []),
      ]);
    }
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

// A new email from anywhere: leave the Board / Day / Today / Ledger views so the
// composer has the center pane, keep an open item's composer if one is showing.
function composeNew() {
  if (boardOpen()) closeBoard();
  if (graphOpen()) closeGraph();
  if (dayOpen()) closeDay();
  if (todayOpen()) closeToday();
  if (ledgerTabOpen()) closeLedgerTab();
  return openDraft("new", null).catch((x) => toast(x.message, "err"));
}

async function boot() {
  wire();
  try {
    const s = await api("/api/session");
    TOKEN = s.token; S.tz = s.timezone || "UTC"; S.demo = s.demo; S.slackOn = s.slack; S.aiOn = s.ai;
    S.version = s.version;
    const want = localStorage.getItem("ultra.layout") || s.layout || "calm";
    document.body.classList.toggle("calm", want !== "classic");
    if (!s.slack) $('#filter-seg button[data-f="slack"]').hidden = true;
    $("#sb-version").textContent = `ultra ${s.version}${s.demo ? " demo" : ""}`;
  } catch (e) { toast(`Server: ${e.message}`, "err"); }
  $$("#filter-seg button").forEach((b) => { b.dataset.label = b.textContent; });
  tick(); setInterval(tick, 30000);
  onSent((d) => {
    setTimeout(() => loadStream(true), 3000);
    // send-then-log: the log card is for the conversation that was answered
    if (d?.kind === "slack") { const k = d.stream_key || S.key; if (k?.startsWith("s-")) stageAfterSend(k); }
    else if (d?.task_id) {
      // an email from a task: offer a progress log on the task (same review card)
      api(`/api/thread/${encodeURIComponent(`t-${d.task_id}`)}`).then((r) => { if (r.task) stageTaskLog(r.task, r.links || []); }).catch(() => {});
    }
    else if (d?.thread_id) stageAfterSend("g-" + d.thread_id);
  });
  initRail(); wireSearch(); initTools(); initAsk(); initCalm();
  initSearch({
    onResults: (r) => {
      S.search = r; S.items = r.items || []; S.sel = S.items.length ? 0 : -1;
      renderStream();
      const head = document.createElement("div");
      head.className = "ms-head dim small-t";
      head.innerHTML = `${r.count} result${r.count === 1 ? "" : "s"} for <b>${esc(r.query)}</b> in ${r.scope === "all" ? "all mail" : "the inbox"}${r.cached ? " (cached)" : ""} <button class="btn tiny ghost" id="ms-clear">Back to the stream</button>`;
      $("#stream").prepend(head);
      $("#ms-clear").onclick = () => { $("#ms-q").value = ""; $(".msearch")?.classList.remove("active"); S.search = null; loadStream(); };
    },
    onClear: () => { if (S.search) { S.search = null; loadStream(); } },
  });
  initToday(S.tz, {
    onOpen: () => { if (graphOpen()) closeGraph(); if (dayOpen()) closeDay(); if (ledgerTabOpen()) closeLedgerTab(); if (boardOpen()) closeBoard(); S.key = null; S.sel = -1; renderStream(); },
    onClose: () => { $("#thread").hidden = true; $("#thread").innerHTML = ""; $("#thread-empty").hidden = false; },
  });
  initLedgerTab({
    onOpen: () => { if (graphOpen()) closeGraph(); if (todayOpen()) closeToday(); if (dayOpen()) closeDay(); if (boardOpen()) closeBoard(); S.key = null; S.sel = -1; renderStream(); },
    onClose: () => { $("#thread").hidden = true; $("#thread").innerHTML = ""; $("#thread-empty").hidden = false; },
  });
  initBoard({
    onOpen: () => { if (graphOpen()) closeGraph(); if (todayOpen()) closeToday(); if (dayOpen()) closeDay(); if (ledgerTabOpen()) closeLedgerTab(); S.key = null; S.sel = -1; renderStream(); },
    onClose: () => { $("#thread").hidden = true; $("#thread").innerHTML = ""; $("#thread-empty").hidden = false; },
    openItem: (key) => { const i = S.items.findIndex((x) => x.key === key); if (i >= 0) openItem(i); else toast("That item is not in the current filter; switch the stream to All.", "err"); },
  });
  initTidy({ onDone: () => loadStream(true) });
  initGraph({
    onOpen: () => { if (todayOpen()) closeToday(); if (dayOpen()) closeDay(); if (ledgerTabOpen()) closeLedgerTab(); if (boardOpen()) closeBoard(); S.key = null; S.sel = -1; renderStream(); $("#btn-graph").classList.add("on"); },
    onClose: () => { $("#btn-graph").classList.remove("on"); $("#thread").hidden = true; $("#thread").innerHTML = ""; $("#thread-empty").hidden = false; },
    openEntity: (kind, key) => { closeGraph(); openLedgerTab("entity", { kind, key }); },
  });
  $("#btn-graph").onclick = () => (graphOpen() ? closeGraph() : openGraph());
  window.addEventListener("ultra:graph", (ev) => openGraph(ev.detail?.id || ""));
  window.addEventListener("ultra:open-draft", (ev) => { if (ev.detail?.id) openDraftById(ev.detail.id).catch((x) => toast(x.message, "err")); });
  window.addEventListener("ultra:stage-log", (ev) => { if (ev.detail?.key) stage("log", ev.detail.key); });
  initDay({
    onOpen: () => { if (graphOpen()) closeGraph(); if (todayOpen()) closeToday(); if (ledgerTabOpen()) closeLedgerTab(); if (boardOpen()) closeBoard(); S.key = null; S.sel = -1; renderStream(); },
    onClose: () => { $("#thread").hidden = true; $("#thread").innerHTML = ""; $("#thread-empty").hidden = false; },
    openItem: (key) => { const i = S.items.findIndex((x) => x.key === key); if (i >= 0) openItem(i); else toast("That item is not in the current filter.", "err"); },
  });
  window.addEventListener("ultra:person", (ev) => openPersonByAddr(ev.detail));
  // open a stream item by key from any view (Today's plan strip); tasks open even when
  // the stream filter hides them
  window.addEventListener("ultra:open-key", async (ev) => {
    const key = ev.detail?.key; if (!key) return;
    let i = S.items.findIndex((x) => x.key === key);
    if (i < 0 && key.startsWith("t-")) { $('#filter-seg button[data-f="tasks"]').click(); await new Promise((r) => setTimeout(r, 600)); i = S.items.findIndex((x) => x.key === key); }
    if (i < 0) { $('#filter-seg button[data-f="all"]').click(); await new Promise((r) => setTimeout(r, 600)); i = S.items.findIndex((x) => x.key === key); }
    if (i >= 0) { closeViews(); openItem(i); } else toast("That item is not in the stream right now.", "err");
  });
  // Ask Hermes "Use as reply": which item is open, and open one by key (resolves when
  // its thread view, and so the composer slot, is on screen)
  window.__ultraOpenKey = () => (S.key && !$("#thread").hidden ? S.key : null);
  window.__ultraOpenItem = async (key) => {
    const i = S.items.findIndex((x) => x.key === key);
    if (i < 0) return false;
    await openItem(i);
    return S.key === key && !!$("#thread #composer");
  };
  window.addEventListener("ultra:listen", (ev) => listen(ev.detail.text, ev.detail.title));
  await loadStream();
  pollStatus(); setInterval(pollStatus, 5000);
}
boot();
