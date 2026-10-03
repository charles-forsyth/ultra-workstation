// Today view (SPEC 7.4, 7.7): day timeline and week grid, drag to block, repeating
// blocks, meeting prep (people, cited briefing, log), RSVP, invitations, slot finder.
// Writes only ever touch blocks Ultra created (tagged ultra=1), and the server enforces
// that. Creating a block is one click after the block card; nothing is sent to anyone.
// The only things that reach other people: your RSVP (one confirmation) and a meeting
// invite (two approvals, like email).

import { homeLine } from "./home.js";
import { api, esc, toast, busy } from "./app.js";
import { askButton, openAsk } from "./ask.js";

const $ = (s, el = document) => el.querySelector(s);
const PX = 1.1;                 // pixels per minute
const SNAP = 15;                // minutes
const T = { day: null, data: null, tz: "UTC", open: false, onClose: null, dragItem: null, view: localStorage.getItem("ultra.calview") || "day", week: null };

function tzNow(tz) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date()).map((x) => [x.type, x.value]));
  return { day: `${p.year}-${p.month}-${p.day}`, min: Number(p.hour) * 60 + Number(p.minute) };
}
const addDays = (day, n) => { const d = new Date(`${day}T12:00:00Z`); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
const hm = (min) => `${String(Math.floor(min / 60)).padStart(2, "0")}:${String(min % 60).padStart(2, "0")}`;
const label12 = (min) => { const h = Math.floor(min / 60), m = min % 60; return `${((h + 11) % 12) + 1}${m ? ":" + String(m).padStart(2, "0") : ""}${h < 12 ? "a" : "p"}`; };
// minutes since local midnight for an RFC3339 string, in the operator's tz
function minOf(iso, tz) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone: tz, hour: "2-digit", minute: "2-digit", hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date(iso)).map((x) => [x.type, x.value]));
  return { day: `${p.year}-${p.month}-${p.day}`, min: Number(p.hour) * 60 + Number(p.minute) };
}
const dayLabel = (day, today) => day === today ? "Today" : day === addDays(today, 1) ? "Tomorrow" : day === addDays(today, -1) ? "Yesterday" : new Date(`${day}T12:00:00Z`).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });

export function initToday(tz, { onOpen, onClose }) {
  T.tz = tz || "UTC"; T.onClose = onClose; T.onOpen = onOpen;
  $("#btn-today")?.addEventListener("click", () => (T.open ? closeToday() : openToday()));
  // "Block" from the bucket or a thread: open Today and a block card at the next free slot
  window.addEventListener("ultra:invite", (ev) => inviteCard(ev.detail || {}));
  window.addEventListener("ultra:block", async (ev) => {
    const d = ev.detail || {};
    await openToday(d.day || null);
    newBlock(d, d.startMin ?? nextSlot(), d.minutes || 30);
  });
}

function nextSlot() {
  const now = tzNow(T.tz);
  let m = T.day === now.day ? Math.ceil((now.min + 5) / SNAP) * SNAP : 9 * 60;
  const busyAt = (x) => (T.timed || []).some((t) => x < t.en && x + 30 > t.s && t.e.my_response !== "declined");
  for (let i = 0; i < 48 && busyAt(m); i++) m += SNAP;
  return Math.min(m, 23 * 60);
}
export function todayOpen() { return T.open; }
export function setDragItem(it) { T.dragItem = it; }

export async function openToday(day = null) {
  T.open = true; T.onOpen?.();
  $("#btn-today")?.classList.add("on");
  const now = tzNow(T.tz);
  T.day = day || T.day || now.day;
  $("#thread-empty").hidden = true;
  const th = $("#thread"); th.hidden = false;
  th.innerHTML = `<div class="dim">Loading calendar...</div>`;
  await loadDay();
}
export function closeToday() {
  T.open = false; $("#btn-today")?.classList.remove("on");
  T.onClose?.();
}

async function loadDay(fresh = false) {
  if (T.view === "week") return loadWeek(fresh);
  let d;
  try { d = await api(`/api/cal/day?day=${T.day}${fresh ? "&fresh=1" : ""}`); }
  catch (e) { $("#thread").innerHTML = `<div class="alert">${esc(e.message)}</div>`; return; }
  if (!T.open) return;
  if (d.tz) T.tz = d.tz;
  T.data = d; render();
}

function layout(events) {
  // assign columns to overlapping timed events
  const timed = events.filter((e) => !e.all_day).map((e) => {
    const s = minOf(e.start, T.tz), en = minOf(e.end, T.tz);
    const s0 = s.day < T.day ? 0 : s.min, e0 = en.day > T.day ? 24 * 60 : Math.max(en.min, s0 + 10);
    return { e, s: s0, en: e0, col: 0, cols: 1 };
  }).sort((a, b) => a.s - b.s || b.en - a.en);
  let group = [], end = -1;
  const flush = () => { const n = Math.max(...group.map((g) => g.col)) + 1; group.forEach((g) => { g.cols = n; }); group = []; };
  for (const x of timed) {
    if (x.s >= end && group.length) flush();
    const used = new Set(group.filter((g) => g.en > x.s).map((g) => g.col));
    let c = 0; while (used.has(c)) c++;
    x.col = c; group.push(x); end = Math.max(end, x.en);
  }
  if (group.length) flush();
  return timed;
}

function render() {
  const d = T.data, now = tzNow(T.tz);
  const [ws, we] = (d.work_hours || ["08:00", "17:00"]).map((x) => { const [h, m] = x.split(":").map(Number); return h * 60 + m; });
  const timed = layout(d.events || []);
  const first = Math.min(ws - 60, ...timed.map((x) => x.s)), last = Math.max(we + 60, ...timed.map((x) => x.en));
  // an hour of headroom above the first thing so its label and block are not clipped
  T.v0 = Math.max(0, Math.floor(first / 60) * 60 - (first % 60 === 0 && first > ws - 60 ? 60 : 0));
  T.v1 = Math.min(24 * 60, Math.ceil(last / 60) * 60);
  const allDay = (d.events || []).filter((e) => e.all_day);
  const hours = []; for (let m = T.v0; m <= T.v1; m += 60) hours.push(m);
  const y = (min) => (min - T.v0) * PX;
  const cls = (e) => [e.ultra ? "mine" : "", e.my_response === "declined" ? "declined" : "", e.my_response === "needsAction" ? "tentative" : "", e.visibility === "private" ? "private" : ""].join(" ");
  $("#thread").innerHTML = `
    <div class="today-head">
      <button class="btn small ghost" data-nav="-1" title="Previous day">&larr;</button>
      <h2>${esc(dayLabel(T.day, now.day))} <span class="dim small-t">${esc(T.day)}</span></h2>
      <button class="btn small ghost" data-nav="1" title="Next day">&rarr;</button>
      ${T.day !== now.day ? `<button class="btn small ghost" data-nav="0">Today</button>` : ""}
      ${viewSeg()}
      <button class="btn small ghost calm-only" data-goto="day" title="Check-in plan and end-of-day report (d)">Plan and report</button>
      <span class="grow"></span>
      <button class="btn small" id="cal-new-meeting" title="Invite people (two approvals before anyone is emailed)">New meeting</button>
      <button class="btn small" id="cal-slots">Find a time</button>
      <button class="btn small ghost" id="cal-fresh" title="Reload from Google">&#8635;</button>
      <button class="btn small ghost" id="cal-close" title="Back to the conversation (Esc)">Close</button>
    </div>
    ${d.error ? `<div class="alert">${esc(d.error)}</div>` : ""}
    <div id="today-plan" class="today-plan calm-only"></div>
    <div id="today-home" class="today-plan"></div>
    <div class="dim small-t today-hint">Drag an email, Slack or task from the stream onto a time to block it. Drag your own blocks to move them. Click a meeting for prep.</div>
    ${allDay.length ? `<div class="allday">${allDay.map((e) => `<span class="badge">${esc(e.summary)}</span>`).join(" ")}</div>` : ""}
    <div class="tl" id="tl" data-h="${y(T.v1) + 8}">
      ${hours.map((m) => `<div class="tl-hour" data-top="${y(m)}"><span>${label12(m)}</span></div>`).join("")}
      <div class="tl-work" data-top="${y(ws)}" data-h="${(we - ws) * PX}"></div>
      ${T.day === now.day && now.min >= T.v0 && now.min <= T.v1 ? `<div class="tl-now" data-top="${y(now.min)}"></div>` : ""}
      <div class="tl-ghost" id="tl-ghost" hidden></div>
      ${timed.map((x, i) => `
        <div class="ev ${cls(x.e)}" data-i="${i}" ${x.e.ultra ? `draggable="true"` : ""} data-top="${y(x.s)}" data-h="${Math.max(18, (x.en - x.s) * PX - 2)}" data-col="${x.col}" data-cols="${x.cols}" title="${esc(x.e.summary)}">
          <div class="ev-t">${esc(x.e.summary)}</div>
          <div class="ev-m">${label12(x.s)}-${label12(x.en)}${x.e.attendee_count ? ` &middot; ${x.e.attendee_count} people` : ""}${x.e.link ? " &middot; video" : ""}${x.e.visibility === "private" ? " &middot; private" : ""}${x.e.my_response === "needsAction" ? " &middot; not answered" : ""}${x.e.my_response === "declined" ? " &middot; declined" : ""}</div>
        </div>`).join("")}
    </div>
    <div id="cal-panel"></div>`;
  T.timed = timed;
  place();
  wire();
  if (T.day === now.day) { planStrip(); homeLine($("#today-home")); }
}

// Calm layout: Today carries the check-in plan's headline (SPEC 7.13), so the Day view
// is one click away rather than a separate place. Read-only; every line opens the item.
async function planStrip() {
  const box = $("#today-plan"); if (!box || !document.body.classList.contains("calm")) return;
  let p;
  try { p = await api("/api/day/plan"); } catch { return; }
  if (!T.open || !$("#today-plan")) return;
  const chips = [];
  const chip = (n, label, cls, title) => { if (n) chips.push(`<span class="tp-chip ${cls}" title="${esc(title)}">${esc(String(n))} ${esc(label)}</span>`); };
  chip(p.mine_count, "your move", "", "Items waiting on you");
  chip((p.overdue || []).length, "overdue", "bad", "Overdue ledger tasks");
  chip((p.due_today || []).length, "due today", "warn", "Ledger tasks due today");
  chip((p.waiting || []).length, "waiting 3+ days", "wait", "Threads waiting on someone else");
  const free = (p.free || []).reduce((a, f) => a + f.minutes, 0);
  // a workspace with nothing to plan yet (no mail signed in, no ledger): no empty strip
  if (!p.mine_count && !(p.overdue || []).length && !(p.due_today || []).length && !(p.waiting || []).length && !free) { box.innerHTML = ""; return; }
  // the cluster line (SPEC 8.9): shown only when there is a problem, or a failure rate
  try {
    const h = await api("/api/cluster/health");
    if (h.enabled && h.ok === false) chip((h.issues || []).length || "!", "cluster issues", "bad", (h.issues || []).join("\n") || h.error || "");
    else if (h.enabled && h.ok === null) chip("?", "cluster unknown", "wait", h.error || "cluster server unreachable");
    else if (h.enabled && Number(h.failure_rate_24h_percent) >= 10) chip(`${h.failure_rate_24h_percent}%`, "jobs failing (24h)", "warn", `${h.jobs_ended_24h} jobs ended in the last day`);
  } catch { /* the plan still shows */ }
  const next = [...(p.overdue || []), ...(p.due_today || [])].slice(0, 3)
    .map((t) => `<li class="day-item" data-key="t-${esc(t.id)}"><span class="badge pri ${esc(String(t.priority || "").toLowerCase())}">${esc(String(t.priority || "").slice(0, 1))}</span> ${esc(t.summary)}</li>`)
    .concat((p.mine || []).slice(0, Math.max(0, 5 - Math.min(3, (p.overdue || []).length + (p.due_today || []).length)))
      .map((i) => `<li class="day-item" data-key="${esc(i.key)}"><span class="src">${esc(i.source)}</span> <b>${esc(i.from || "")}</b> ${esc(i.subject || "")}</li>`));
  box.innerHTML = `<div class="tp-head"><span class="label">Plan</span>${chips.join("")}<span class="dim small-t">${free} min free</span><span class="grow"></span>
      <button class="btn tiny ghost" data-goto="day" title="The full check-in plan and the end-of-day report (d)">Full plan</button></div>
    ${next.length ? `<ul class="day-list tp-list">${next.join("")}</ul>` : ""}`;
  box.onclick = (e) => {
    const li = e.target.closest(".day-item[data-key]"); if (!li) return;
    window.dispatchEvent(new CustomEvent("ultra:open-key", { detail: { key: li.dataset.key } }));
  };
}

// The CSP forbids inline style attributes, so positions ride in data-* and are set here
// through the CSSOM, which the policy allows.
function place() {
  const tl = $("#tl"); if (!tl) return;
  tl.style.height = `${tl.dataset.h}px`;
  tl.querySelectorAll("[data-top]").forEach((el) => {
    el.style.top = `${el.dataset.top}px`;
    if (el.dataset.h) el.style.height = `${el.dataset.h}px`;
    if (el.dataset.cols) {
      const c = Number(el.dataset.col), n = Number(el.dataset.cols);
      el.style.left = `calc(56px + (100% - 60px) * ${c / n})`;
      el.style.width = `calc((100% - 60px) / ${n} - 4px)`;
    }
  });
}

function minuteAt(clientY) {
  const r = $("#tl").getBoundingClientRect();
  const m = T.v0 + (clientY - r.top) / PX;
  return Math.max(T.v0, Math.min(T.v1 - SNAP, Math.round(m / SNAP) * SNAP));
}

function wire() {
  const th = $("#thread");
  th.querySelectorAll("[data-nav]").forEach((b) => b.onclick = () => {
    const n = Number(b.dataset.nav); T.day = n === 0 ? tzNow(T.tz).day : addDays(T.day, n); loadDay();
  });
  $("#cal-close").onclick = closeToday;
  $("#cal-fresh").onclick = (e) => busy(e.target, () => loadDay(true));
  $("#cal-slots").onclick = () => slotFinder();
  wireCommon();
  const tl = $("#tl"), ghost = $("#tl-ghost");
  tl.onclick = (e) => {
    const ev = e.target.closest(".ev"); if (!ev) return;
    const x = T.timed[Number(ev.dataset.i)];
    x.e.ultra ? blockPanel(x) : meetingPanel(x.e);
  };
  tl.ondragstart = (e) => {
    const ev = e.target.closest(".ev"); if (!ev) return;
    const x = T.timed[Number(ev.dataset.i)];
    e.dataTransfer.setData("application/x-ultra-block", JSON.stringify({ id: x.e.id, cal: x.e.cal, len: x.en - x.s }));
    e.dataTransfer.effectAllowed = "move";
  };
  const kinds = (e) => [...e.dataTransfer.types];
  tl.ondragover = (e) => {
    const k = kinds(e);
    if (!k.includes("application/x-ultra-item") && !k.includes("application/x-ultra-block")) return;
    e.preventDefault();
    const m = minuteAt(e.clientY);
    ghost.hidden = false; ghost.style.top = `${(m - T.v0) * PX}px`; ghost.style.height = `${30 * PX}px`;
    ghost.textContent = `${label12(m)}`;
  };
  tl.ondragleave = (e) => { if (!tl.contains(e.relatedTarget)) ghost.hidden = true; };
  tl.ondrop = async (e) => {
    ghost.hidden = true;
    const m = minuteAt(e.clientY);
    const blk = e.dataTransfer.getData("application/x-ultra-block");
    if (blk) { e.preventDefault(); const b = JSON.parse(blk); return moveBlock(b, m); }
    const raw = e.dataTransfer.getData("application/x-ultra-item");
    if (!raw) return;
    e.preventDefault();
    newBlock(JSON.parse(raw), m);
  };
}

// ---------------------------------------------------------------- blocks
async function newBlock(it, startMin, minutes = 30) {
  const panel = $("#cal-panel");
  panel.innerHTML = `<div class="card"><div class="dim">Preparing block...</div></div>`;
  panel.scrollIntoView({ block: "nearest" });
  let t = { summary: `Focus: ${it.subject || "follow-up"}`, description: "", personal: false };
  if (it.key && /^[gkst]-/.test(it.key)) {
    try { t = await api("/api/cal/block-text", { method: "POST", body: { key: it.key } }); } catch { /* use defaults */ }
  }
  panel.innerHTML = `<div class="card block-card">
    <div class="label">New calendar block <span class="dim small-t">only on your calendar, nobody is invited</span></div>
    <div class="lc-grid">
      <span class="k">Title</span><input id="bk-title" class="rinput" maxlength="200" value="${esc(t.summary)}">
      <span class="k">When</span><div class="row-g"><input id="bk-day" type="date" value="${esc(T.day)}"><input id="bk-start" type="time" step="900" value="${hm(startMin)}">
        <select id="bk-len">${[15, 30, 45, 60, 90, 120].map((n) => `<option value="${n}" ${n === minutes ? "selected" : ""}>${n} min</option>`).join("")}</select></div>
      <span class="k">Type</span><label class="chk"><input type="checkbox" id="bk-personal"> Personal (private visibility)</label>
      <span class="k">Repeat</span><div class="row-g"><select id="bk-rep"><option value="">Just this once</option><option value="weekly">Every week on...</option></select>
        <span id="bk-repopts" hidden>${["MO", "TU", "WE", "TH", "FR"].map((d) => `<label class="chk"><input type="checkbox" class="bk-wd" value="${d}"> ${d[0]}${d[1].toLowerCase()}</label>`).join(" ")}
        for <select id="bk-weeks">${[2, 4, 8, 12, 26].map((n) => `<option value="${n}" ${n === 4 ? "selected" : ""}>${n} weeks</option>`).join("")}</select></span></div>
      <span class="k">Notes</span><textarea id="bk-desc" rows="6">${esc(t.description)}</textarea>
    </div>
    <div class="row-g"><button class="btn send" id="bk-create">Add to calendar</button><button class="btn ghost" id="bk-cancel">Cancel</button><span class="dim small-t" id="bk-msg"></span></div>
  </div>`;
  $("#bk-cancel").onclick = () => { panel.innerHTML = ""; };
  const wdOf = (day) => ["SU", "MO", "TU", "WE", "TH", "FR", "SA"][new Date(`${day}T12:00:00Z`).getUTCDay()];
  $("#bk-rep").onchange = () => {
    const on = $("#bk-rep").value === "weekly"; $("#bk-repopts").hidden = !on;
    if (on) { const wd = wdOf($("#bk-day").value); document.querySelectorAll(".bk-wd").forEach((c) => { c.checked = c.value === wd; }); }
  };
  $("#bk-create").onclick = (e) => busy(e.target, async () => {
    const start = `${$("#bk-day").value}T${$("#bk-start").value}`;
    if ($("#bk-rep").value === "weekly") {
      const weekdays = [...document.querySelectorAll(".bk-wd")].filter((c) => c.checked).map((c) => c.value);
      const weeks = Number($("#bk-weeks").value);
      if (!weekdays.length) { $("#bk-msg").textContent = "Pick at least one day."; return; }
      if (!confirm(`Add "${$("#bk-title").value}" every ${weekdays.join(", ")} for ${weeks} weeks?\n\nOnly on your calendar; nobody is invited. You can delete the whole series from any of its blocks.`)) return;
      try {
        const r = await api("/api/cal/series", { method: "POST", body: { summary: $("#bk-title").value, start, minutes: Number($("#bk-len").value), description: $("#bk-desc").value, personal: $("#bk-personal").checked, weekdays, weeks } });
        toast(r.ok ? `Repeating block added (${r.rule || "weekly"}).` : `Series added but check it: ${r.problems.join(", ")}`, r.ok ? "ok" : "err");
        panel.innerHTML = ""; await loadDay(true);
      } catch (err) { $("#bk-msg").textContent = err.message; }
      return;
    }
    try {
      const r = await api("/api/cal/block", { method: "POST", body: {
        summary: $("#bk-title").value, start, minutes: Number($("#bk-len").value),
        description: $("#bk-desc").value, personal: $("#bk-personal").checked, key: it.key || "" } });
      const day = $("#bk-day").value;
      if (!r.ok) toast(`Block added but check it: ${r.problems.join(", ")}`, "err");
      else toast(`Blocked ${label12(minOf(r.event.start, T.tz).min)} on ${day}.`, "ok");
      panel.innerHTML = "";
      T.day = day; await loadDay(true);
    } catch (err) { const m = $("#bk-msg"); if (m) m.textContent = err.message; else toast(err.message, "err"); }
  });
  $("#bk-title").focus();
}

async function moveBlock(b, startMin) {
  const start = `${T.day}T${hm(startMin)}`, end = `${T.day}T${hm(Math.min(24 * 60 - 1, startMin + b.len))}`;
  try {
    const r = await api("/api/cal/block/move", { method: "POST", body: { cal: b.cal, id: b.id, start, end } });
    toast(r.ok ? `Moved to ${label12(startMin)}.` : `Moved, but check it: ${r.problems.join(", ")}`, r.ok ? "ok" : "err");
  } catch (e) { toast(e.message, "err"); }
  loadDay(true);
}

function blockPanel(x) {
  const e = x.e, panel = $("#cal-panel");
  panel.innerHTML = `<div class="card block-card">
    <div class="label">Your block <span class="dim small-t">made by Ultra</span></div>
    <div class="lc-grid">
      <span class="k">Title</span><input id="bm-title" class="rinput" maxlength="200" value="${esc(e.summary)}">
      <span class="k">When</span><div class="row-g"><input id="bm-start" type="time" step="900" value="${hm(x.s)}">
        <select id="bm-len">${[15, 30, 45, 60, 90, 120, 180, 240].map((n) => `<option value="${n}" ${n === x.en - x.s ? "selected" : ""}>${n} min</option>`).join("")}${[15, 30, 45, 60, 90, 120, 180, 240].includes(x.en - x.s) ? "" : `<option value="${x.en - x.s}" selected>${x.en - x.s} min</option>`}</select></div>
    </div>
    ${e.recurring ? `<div class="dim small-t">Part of a repeating series. Save and Delete change this one block; use Delete series for all of them.</div>` : ""}
    <div class="row-g"><button class="btn" id="bm-save">Save</button><button class="btn danger" id="bm-del">Delete block</button>${e.recurring ? `<button class="btn danger ghost" id="bm-delall">Delete series</button>` : ""}<button class="btn ghost" id="bm-x">Close</button>
    ${e.html_link ? `<a class="btn ghost" href="${esc(e.html_link)}" target="_blank" rel="noopener noreferrer">Open in Google</a>` : ""}<span class="dim small-t" id="bm-msg"></span></div>
  </div>`;
  $("#bm-x").onclick = () => { panel.innerHTML = ""; };
  $("#bm-save").onclick = (ev) => busy(ev.target, async () => {
    const [h, m] = $("#bm-start").value.split(":").map(Number), s = h * 60 + m, len = Number($("#bm-len").value);
    try {
      const r = await api("/api/cal/block/move", { method: "POST", body: { cal: e.cal, id: e.id, start: `${T.day}T${hm(s)}`, end: `${T.day}T${hm(Math.min(1439, s + len))}`, summary: $("#bm-title").value } });
      toast(r.ok ? "Block updated." : `Updated, but check it: ${r.problems.join(", ")}`, r.ok ? "ok" : "err");
      panel.innerHTML = ""; loadDay(true);
    } catch (err) { const m = $("#bm-msg"); if (m) m.textContent = err.message; else toast(err.message, "err"); }
  });
  $("#bm-delall")?.addEventListener("click", (ev) => busy(ev.target, async () => {
    if (!confirm(`Delete every block in the "${e.summary}" series from your calendar?`)) return;
    try { await api("/api/cal/series/delete", { method: "POST", body: { cal: e.cal, id: e.id } }); toast("Series deleted.", "ok"); panel.innerHTML = ""; loadDay(true); }
    catch (err) { $("#bm-msg").textContent = err.message; }
  }));
  $("#bm-del").onclick = (ev) => busy(ev.target, async () => {
    if (!confirm(`Delete "${e.summary}" from your calendar?`)) return;
    try { await api("/api/cal/block/delete", { method: "POST", body: { cal: e.cal, id: e.id } }); toast("Block deleted.", "ok"); panel.innerHTML = ""; loadDay(true); }
    catch (err) { const m = $("#bm-msg"); if (m) m.textContent = err.message; else toast(err.message, "err"); }
  });
}

// ---------------------------------------------------------------- meeting prep
async function meetingPanel(e0) {
  const panel = $("#cal-panel");
  panel.innerHTML = `<div class="card"><div class="dim">Loading ${esc(e0.summary)}...</div></div>`;
  panel.scrollIntoView({ block: "nearest" });
  let e;
  try { e = await api(`/api/cal/event?cal=${encodeURIComponent(e0.cal)}&id=${encodeURIComponent(e0.id)}`); }
  catch (err) { panel.innerHTML = `<div class="alert">${esc(err.message)}</div>`; return; }
  const resp = { accepted: "yes", declined: "no", tentative: "maybe", needsAction: "?" };
  const people = e.prep_people || [];
  panel.innerHTML = `<div class="card meeting-card">
    <div class="row-g"><b>${esc(e.summary)}</b><span class="grow"></span>
      ${e.link ? `<a class="btn small" href="${esc(e.link)}" target="_blank" rel="noopener noreferrer">Join</a>` : ""}
      ${e.html_link ? `<a class="btn small ghost" href="${esc(e.html_link)}" target="_blank" rel="noopener noreferrer">Open in Google</a>` : ""}
      <button class="btn small ghost" id="mp-x">Close</button></div>
    <div class="dim small-t">${e.organizer_self ? "You organise this." : `Organiser: ${esc(e.organizer)}`} ${e.recurring ? "&middot; recurring" : ""} &middot; ${esc(fmtWhen(e))}</div>
    ${!e.organizer_self && (e.attendees || []).some((a) => a.self) ? `<div class="row-g rsvp">Your answer: <b>${esc({ accepted: "Yes", tentative: "Maybe", declined: "No", needsAction: "not answered" }[e.my_response] || e.my_response || "?")}</b>
      <button class="btn tiny ${e.my_response === "accepted" ? "on" : ""}" data-rsvp="accepted">Yes</button><button class="btn tiny ${e.my_response === "tentative" ? "on" : ""}" data-rsvp="tentative">Maybe</button><button class="btn tiny ${e.my_response === "declined" ? "on" : ""}" data-rsvp="declined">No</button></div>` : ""}
    <div class="row-g"><button class="btn small ai" id="mp-brief" title="What you should know before this meeting, from the ledger and past mail, with citations">Prep briefing</button>
      <button class="btn small" id="mp-log" title="Log this meeting in the ledger (review card)">Log this meeting</button>
      <button class="btn small ghost" id="mp-ctx" title="Everyone on the invite in the right panel">People &amp; context</button>
      ${askButton("btn small ai")}</div>
    <div id="mp-out"></div>
    ${e.description ? `<details class="quoted" open><summary>Invite notes</summary><div class="body">${esc(e.description)}</div></details>` : `<div class="dim small-t">No invite notes.</div>`}
    <div class="sect"><div class="label">People (${e.attendee_count}${people.length && people.length < e.attendee_count ? `, ${people.length} besides you` : ""})</div>
      ${people.length ? `<div class="plist" id="mp-people">${people.map((p, i) => `<div class="prow" data-i="${i}" tabindex="0"><div class="pmain"><b>${esc(p.name || p.addr)}</b> <span class="badge ${p.response === "accepted" ? "ok" : p.response === "declined" ? "warn" : "dim"}">${esc(resp[p.response] || p.response || "?")}</span></div><div class="dim small-t">${esc(p.addr)}</div></div>`).join("")}</div>
      <div class="dim small-t hint">Click a person for their ledger context in the right panel.</div>` : `<div class="dim small-t">${e.attendee_count > 12 ? "Large meeting: attendees not listed." : "Only you."}</div>`}
    </div>
  </div>`;
  $("#mp-x").onclick = () => { panel.innerHTML = ""; };
  panel.querySelectorAll("[data-rsvp]").forEach((b) => b.onclick = () => busy(b, () => rsvp(e, b.dataset.rsvp)));
  const key = `c-${e.cal}~${e.id}`;
  panel.querySelector("[data-ask]").onclick = () => openAsk({ type: "item", key }, e.summary || "Meeting");
  $("#mp-ctx").onclick = () => window.dispatchEvent(new CustomEvent("ultra:item-context", { detail: { key, subject: e.summary, source: "calendar" } }));
  $("#mp-brief").onclick = (ev) => busy(ev.target, async () => {
    const out = $("#mp-out"); out.innerHTML = `<div class="dim small-t">Reading the ledger and past mail for everyone on the invite...</div>`;
    try {
      const r = await api("/api/item/briefing", { method: "POST", body: { key } });
      out.innerHTML = `<div class="card brief-card"><div class="label">Prep briefing <span class="dim small-t">${esc(r.model || "")}${r.cached ? " &middot; cached" : ""} &middot; citations link to the ledger</span></div>
        <textarea id="mp-brief-text" rows="14">${esc(r.markdown)}</textarea>
        <div class="row-g"><button class="btn tiny" id="mp-brief-copy">Copy</button><button class="btn tiny ghost" id="mp-brief-fresh">Rebuild</button><span class="dim small-t">Editable. Nothing is saved unless you log it.</span></div></div>`;
      $("#mp-brief-copy").onclick = () => navigator.clipboard.writeText($("#mp-brief-text").value).then(() => toast("Copied.", "ok"));
      $("#mp-brief-fresh").onclick = (x) => busy(x.target, async () => { const r2 = await api("/api/item/briefing", { method: "POST", body: { key, fresh: true } }); $("#mp-brief-text").value = r2.markdown; });
    } catch (err) { out.innerHTML = `<div class="alert">${esc(err.message)}</div>`; }
  });
  $("#mp-log").onclick = () => window.dispatchEvent(new CustomEvent("ultra:stage-meeting", { detail: { event: e } }));
  $("#mp-people")?.addEventListener("click", (ev) => {
    const row = ev.target.closest(".prow"); if (!row) return;
    const p = people[Number(row.dataset.i)];
    window.dispatchEvent(new CustomEvent("ultra:person", { detail: { addr: p.addr, name: p.name } }));
  });
}

function fmtWhen(e) {
  if (e.all_day) return `all day ${e.start}`;
  const s = minOf(e.start, T.tz), en = minOf(e.end, T.tz);
  return `${dayLabel(s.day, tzNow(T.tz).day)} ${label12(s.min)}-${label12(en.min)}`;
}

// ---------------------------------------------------------------- RSVP (one confirmation)
async function rsvp(e, response) {
  const word = { accepted: "Yes", tentative: "Maybe", declined: "No" }[response];
  const ask = await api("/api/cal/rsvp/ask", { method: "POST", body: { cal: e.cal, id: e.id, response } });
  const note = response === "declined" ? (prompt(`Answer "${word}" to "${e.summary}" (${fmtWhen(e)})?\n\n${e.organizer} is told by Google.${e.recurring ? " This answers only this occurrence." : ""}\n\nOptional note to the organiser (leave blank for none):`, "") ?? null) : "";
  if (note === null) { toast("Not answered."); return; }
  if (response !== "declined" && !confirm(`Answer "${word}" to "${e.summary}" (${fmtWhen(e)})?\n\n${e.organizer} is told by Google.${e.recurring ? " This answers only this occurrence." : ""}`)) { toast("Not answered."); return; }
  const r = await api("/api/cal/rsvp", { method: "POST", body: { cal: e.cal, id: e.id, response, token: ask.token, note } });
  toast(r.ok ? `Answered ${word}.` : `Answered, but check it: ${r.problems.join(", ")}`, r.ok ? "ok" : "err");
  $("#cal-panel").innerHTML = ""; loadDay(true);
}

// ---------------------------------------------------------------- view switch + week
function viewSeg() {
  return `<span class="seg cal-seg"><button data-view="day" class="${T.view === "day" ? "on" : ""}">Day</button><button data-view="week" class="${T.view === "week" ? "on" : ""}">Week</button></span>`;
}
function wireCommon() {
  document.querySelectorAll("#thread [data-view]").forEach((b) => b.onclick = () => { T.view = b.dataset.view; localStorage.setItem("ultra.calview", T.view); loadDay(); });
  $("#cal-new-meeting")?.addEventListener("click", () => inviteCard({}));
}

async function loadWeek(fresh = false) {
  let d;
  try { d = await api(`/api/cal/week?day=${T.day}${fresh ? "&fresh=1" : ""}`); }
  catch (e) { $("#thread").innerHTML = `<div class="alert">${esc(e.message)}</div>`; return; }
  if (!T.open) return;
  if (d.tz) T.tz = d.tz;
  T.week = d; renderWeek();
}

function renderWeek() {
  const d = T.week, now = tzNow(T.tz);
  const days = Object.keys(d.days || {});
  const WPX = 0.8;
  const [ws, we] = (d.work_hours || ["08:00", "17:00"]).map((x) => { const [h, m] = x.split(":").map(Number); return h * 60 + m; });
  const all = days.flatMap((k) => (d.days[k] || []).filter((e) => !e.all_day).map((e) => { const s = minOf(e.start, T.tz), en = minOf(e.end, T.tz); return { k, e, s: s.day < k ? 0 : s.min, en: en.day > k ? 1440 : Math.max(en.min, (s.day < k ? 0 : s.min) + 15) }; }));
  const v0 = Math.max(0, Math.floor(Math.min(ws - 60, ...all.map((x) => x.s)) / 60) * 60);
  const v1 = Math.min(1440, Math.ceil(Math.max(we + 60, ...all.map((x) => x.en)) / 60) * 60);
  const hours = []; for (let m = v0; m <= v1; m += 60) hours.push(m);
  const cls = (e) => [e.ultra ? "mine" : "", e.my_response === "declined" ? "declined" : "", e.my_response === "needsAction" ? "tentative" : ""].join(" ");
  T.wk = all;
  $("#thread").innerHTML = `
    <div class="today-head">
      <button class="btn small ghost" data-wnav="-7" title="Previous week">&larr;</button>
      <h2>Week of ${esc(new Date(`${d.week_start}T12:00:00Z`).toLocaleDateString([], { month: "short", day: "numeric", timeZone: "UTC" }))}</h2>
      <button class="btn small ghost" data-wnav="7" title="Next week">&rarr;</button>
      ${days.includes(now.day) ? "" : `<button class="btn small ghost" data-wnav="0">This week</button>`}
      ${viewSeg()}
      <button class="btn small ghost calm-only" data-goto="day" title="Check-in plan and end-of-day report (d)">Plan and report</button>
      <span class="grow"></span>
      <button class="btn small" id="cal-new-meeting">New meeting</button>
      <button class="btn small" id="cal-slots">Find a time</button>
      <button class="btn small ghost" id="cal-fresh" title="Reload from Google">&#8635;</button>
      <button class="btn small ghost" id="cal-close">Close</button>
    </div>
    ${d.error ? `<div class="alert">${esc(d.error)}</div>` : ""}
    ${(d.pending || []).length ? `<div class="card pending"><div class="label">Needs your answer (${d.pending.length})</div>${d.pending.map((e, i) => `<div class="row-g pend"><b>${esc(e.summary)}</b> <span class="dim small-t">${esc(fmtWhen(e))} &middot; from ${esc(e.organizer)}</span><span class="grow"></span><button class="btn tiny" data-pend="${i}" data-r="accepted">Yes</button><button class="btn tiny" data-pend="${i}" data-r="tentative">Maybe</button><button class="btn tiny" data-pend="${i}" data-r="declined">No</button><button class="btn tiny ghost" data-pend="${i}" data-r="open">Open</button></div>`).join("")}</div>` : ""}
    <div class="wk" id="wk">
      <div class="wk-hours">${hours.map((m) => `<div class="wk-h" data-top="${(m - v0) * WPX}"><span>${label12(m)}</span></div>`).join("")}</div>
      ${days.map((k) => `<div class="wk-day ${k === now.day ? "today" : ""}" data-day="${k}">
        <div class="wk-head" data-goto="${k}">${esc(new Date(`${k}T12:00:00Z`).toLocaleDateString([], { weekday: "short", timeZone: "UTC" }))} ${Number(k.slice(8))}</div>
        <div class="wk-all">${(d.days[k] || []).filter((e) => e.all_day).map((e) => `<span class="badge" title="${esc(e.summary)}">${esc(e.summary.slice(0, 18))}</span>`).join("")}</div>
        <div class="wk-col" data-h="${(v1 - v0) * WPX}">
          <div class="tl-work" data-top="${(ws - v0) * WPX}" data-h="${(we - ws) * WPX}"></div>
          ${k === now.day && now.min >= v0 && now.min <= v1 ? `<div class="tl-now" data-top="${(now.min - v0) * WPX}"></div>` : ""}
          ${all.map((x, i) => x.k !== k ? "" : `<div class="ev wev ${cls(x.e)}" data-w="${i}" data-top="${(x.s - v0) * WPX}" data-h="${Math.max(14, (x.en - x.s) * WPX - 1)}" title="${esc(x.e.summary)} ${label12(x.s)}-${label12(x.en)}"><div class="ev-t">${esc(x.e.summary)}</div></div>`).join("")}
        </div></div>`).join("")}
    </div>
    <div class="dim small-t today-hint"><span class="phone-only">Phones show 3 days; the arrows move a week. </span>Click a day's name to open it. Click a meeting for prep, your answer, and logging. Striped = not answered yet.</div>
    <div id="cal-panel"></div>`;
  document.querySelectorAll("#wk [data-top]").forEach((el) => { el.style.top = `${el.dataset.top}px`; if (el.dataset.h) el.style.height = `${el.dataset.h}px`; });
  document.querySelectorAll("#wk .wk-col, #wk .wk-hours").forEach((el) => { el.style.height = `${(v1 - v0) * WPX + 8}px`; });
  // Phones: 7 columns are too narrow to read; show 3 days from today (this week) or Mon-Wed.
  if (matchMedia("(max-width: 700px)").matches) {
    const i0 = Math.max(0, Math.min(4, days.indexOf(now.day)));
    document.querySelectorAll("#wk .wk-day").forEach((el, i) => el.classList.toggle("phone-hide", i < i0 || i > i0 + 2));
    $("#wk").style.setProperty("--wk-n", "3");
  }
  $("#cal-close").onclick = closeToday;
  $("#cal-fresh").onclick = (e) => busy(e.target, () => loadWeek(true));
  $("#cal-slots").onclick = () => slotFinder();
  wireCommon();
  document.querySelectorAll("[data-wnav]").forEach((b) => b.onclick = () => { const n = Number(b.dataset.wnav); T.day = n === 0 ? tzNow(T.tz).day : addDays(T.day, n); loadWeek(); });
  document.querySelectorAll("[data-goto]").forEach((b) => b.onclick = () => { T.day = b.dataset.goto; T.view = "day"; localStorage.setItem("ultra.calview", "day"); loadDay(); });
  $("#wk").onclick = (ev) => {
    const el = ev.target.closest(".wev"); if (!el) return;
    const x = T.wk[Number(el.dataset.w)];
    x.e.ultra ? (T.day = x.k, T.view = "day", loadDay()) : meetingPanel(x.e);
  };
  document.querySelectorAll("[data-pend]").forEach((b) => b.onclick = () => {
    const e = d.pending[Number(b.dataset.pend)];
    if (b.dataset.r === "open") return meetingPanel(e);
    busy(b, () => rsvp(e, b.dataset.r));
  });
}

// ---------------------------------------------------------------- new meeting (two approvals)
export async function inviteCard(pre = {}) {
  if (!T.open) await openToday(pre.day || null);
  const panel = $("#cal-panel");
  const now = tzNow(T.tz);
  const day = pre.day || (T.day >= now.day ? T.day : now.day);
  const startMin = pre.startMin ?? Math.max(9 * 60, Math.ceil((now.min + 30) / 30) * 30);
  panel.innerHTML = `<div class="card invite-card">
    <div class="label">New meeting <span class="dim small-t">guests are emailed by Google only after your two approvals</span></div>
    <div class="lc-grid">
      <span class="k">Title</span><input id="iv-title" class="rinput" maxlength="200" value="${esc(pre.summary || "")}">
      <span class="k">When</span><div class="row-g"><input id="iv-day" type="date" value="${esc(day)}"><input id="iv-start" type="time" step="900" value="${hm(Math.min(startMin, 23 * 60))}">
        <select id="iv-len">${[15, 30, 45, 60, 90, 120].map((n) => `<option value="${n}" ${n === (pre.minutes || 30) ? "selected" : ""}>${n} min</option>`).join("")}</select></div>
      <span class="k">Guests</span><input id="iv-guests" class="rinput" placeholder="email addresses, comma separated" value="${esc((pre.guests || []).join(", "))}">
      <span class="k">Video</span><label class="chk"><input type="checkbox" id="iv-video" checked> Add a Google Meet link</label>
      <span class="k">Location</span><input id="iv-loc" class="rinput" placeholder="optional" value="">
      <span class="k">Notes</span><textarea id="iv-desc" rows="5" placeholder="Agenda (shown to everyone on the invite)">${esc(pre.description || "")}</textarea>
    </div>
    <div class="row-g"><button class="btn small ghost" id="iv-find">Check their free time</button><span id="iv-fb" class="dim small-t"></span></div>
    <div class="row-g"><button class="btn primary" id="iv-approve">Approve (1 of 2)</button><button class="btn ghost" id="iv-cancel">Cancel</button><span class="dim small-t" id="iv-msg"></span></div>
  </div>`;
  panel.scrollIntoView({ block: "nearest" });
  const fields = () => ({ summary: $("#iv-title").value, start: `${$("#iv-day").value}T${$("#iv-start").value}`, minutes: Number($("#iv-len").value), guests: $("#iv-guests").value, video: $("#iv-video").checked, location: $("#iv-loc").value, description: $("#iv-desc").value, source: pre.source || "" });
  $("#iv-cancel").onclick = () => { panel.innerHTML = ""; };
  $("#iv-find").onclick = (ev) => busy(ev.target, async () => {
    const r = await api("/api/cal/slots", { method: "POST", body: { emails: [$("#iv-guests").value], minutes: Number($("#iv-len").value), days: 5 } });
    const w = r.windows.slice(0, 6).map((x) => { const s = minOf(x.start, T.tz), e = minOf(x.end, T.tz); return `<button class="btn tiny ghost" data-slot="${s.day}|${s.min}">${esc(dayLabel(s.day, tzNow(T.tz).day))} ${label12(s.min)}-${label12(e.min)}</button>`; }).join(" ");
    $("#iv-fb").innerHTML = (r.unknown?.length ? `Could not see: ${esc(r.unknown.join(", "))}. ` : "") + (w || "No shared free time in the next 5 work days.");
    $("#iv-fb").querySelectorAll("[data-slot]").forEach((b) => b.onclick = () => { const [dd, mm] = b.dataset.slot.split("|"); $("#iv-day").value = dd; $("#iv-start").value = hm(Number(mm)); });
  });
  $("#iv-approve").onclick = (ev) => busy(ev.target, async () => {
    try {
      const inv = await api("/api/invites", { method: "POST", body: fields() });
      await api(`/api/invites/${inv.id}/approve`, { method: "POST" });
      const rv = await api(`/api/invites/${inv.id}/review`, { method: "POST" });
      inviteReview(rv);
    } catch (err) { $("#iv-msg").textContent = err.message; }
  });
  $("#iv-title").focus();
}

function inviteReview(rv) {
  const d = rv.invite.data, c = rv.checks;
  const s = minOf(d.start, T.tz);
  const dlg = $("#review");
  dlg.innerHTML = `<div class="rv-card" role="dialog" aria-modal="true" aria-labelledby="iv-rv-title">
    <h3 id="iv-rv-title">Send this meeting invite? <span class="dim">(approval 2 of 2)</span></h3>
    <div class="kv">
      <span class="k">Title</span><span class="strong">${esc(d.summary)}</span>
      <span class="k">When</span><span>${esc(new Date(`${s.day}T12:00:00Z`).toLocaleDateString([], { weekday: "long", month: "short", day: "numeric", timeZone: "UTC" }))}, ${label12(s.min)}-${label12(s.min + d.minutes)} (${d.minutes} min)</span>
      <span class="k">Guests</span><span>${esc(d.guests.join(", "))}</span>
      <span class="k">Video</span><span>${d.video ? "Google Meet link" : "none"}</span>
      ${d.location ? `<span class="k">Location</span><span>${esc(d.location)}</span>` : ""}
    </div>
    ${c.busy?.length ? `<div class="lint warning">Busy then: ${esc(c.busy.join(", "))}</div>` : `<div class="ok-t small-t">Everyone Google can see is free then.</div>`}
    ${c.unknown?.length ? `<div class="lint warning">Can't see the calendars of: ${esc(c.unknown.join(", "))}</div>` : ""}
    ${c.external?.length ? `<div class="lint warning">Outside your domain: ${esc(c.external.join(", "))}</div>` : ""}
    ${d.description ? `<pre class="rv-body">${esc(d.description)}</pre>` : `<div class="dim small-t">No notes.</div>`}
    <div class="dim small-t">Google emails the invite to every guest as soon as you press Send. This approval expires in ${Math.round(rv.expires_in / 60)} minutes.</div>
    <div class="rv-acts"><button class="btn" id="iv-no" autofocus>Not yet</button><span class="grow"></span><button class="btn send" id="iv-yes" disabled>Send invite</button></div>
  </div>`;
  dlg.hidden = false;
  setTimeout(() => { const y = $("#iv-yes"); if (y) y.disabled = false; }, 2000);
  $("#iv-no").onclick = async () => { dlg.hidden = true; await api(`/api/invites/${rv.invite.id}/unapprove`, { method: "POST" }).catch(() => {}); toast("Not sent. The invite is back to a draft."); };
  $("#iv-yes").onclick = (ev) => busy(ev.target, async () => {
    try {
      const r = await api(`/api/invites/${rv.invite.id}/send`, { method: "POST", body: { token: rv.token } });
      dlg.hidden = true;
      toast(r.ok ? `Invite sent to ${d.guests.length} ${d.guests.length === 1 ? "person" : "people"}.` : `Sent, but check it in Google Calendar: ${r.problems.join(", ")}`, r.ok ? "ok" : "err");
      $("#cal-panel").innerHTML = ""; T.day = s.day; loadDay(true);
    } catch (err) { dlg.hidden = true; toast(err.message, "err"); }
  });
}

// ---------------------------------------------------------------- slot finder
function slotFinder() {
  const panel = $("#cal-panel");
  panel.innerHTML = `<div class="card">
    <div class="label">Find a time</div>
    <div class="lc-grid">
      <span class="k">With</span><input id="sf-who" class="rinput" placeholder="email addresses, comma separated (blank = just you)">
      <span class="k">Length</span><div class="row-g"><select id="sf-len">${[15, 30, 45, 60, 90].map((n) => `<option ${n === 30 ? "selected" : ""}>${n}</option>`).join("")}</select> min, next <select id="sf-days">${[1, 3, 5, 10].map((n) => `<option ${n === 5 ? "selected" : ""}>${n}</option>`).join("")}</select> days (work hours)</div>
    </div>
    <div class="row-g"><button class="btn" id="sf-go">Search</button><button class="btn ghost" id="sf-x">Close</button></div>
    <div id="sf-out"></div>
  </div>`;
  $("#sf-x").onclick = () => { panel.innerHTML = ""; };
  $("#sf-go").onclick = (ev) => busy(ev.target, async () => {
    const out = $("#sf-out");
    try {
      const r = await api("/api/cal/slots", { method: "POST", body: { emails: [$("#sf-who").value], minutes: Number($("#sf-len").value), days: Number($("#sf-days").value) } });
      const fmt = (iso) => { const x = minOf(iso, T.tz); return { day: x.day, min: x.min }; };
      out.innerHTML = `${r.unknown?.length ? `<div class="warnline">Could not see: ${esc(r.unknown.join(", "))} (outside your org or private). Windows only reflect the others.</div>` : ""}
        ${r.windows.length ? `<ul class="clist slots">${r.windows.map((w, i) => { const s = fmt(w.start), e = fmt(w.end); return `<li><b>${esc(dayLabel(s.day, tzNow(T.tz).day))}</b> ${label12(s.min)}-${label12(e.min)} <button class="btn tiny ghost" data-copy="${i}" title="Copy as text for an email">Copy</button> <button class="btn tiny ghost" data-hold="${i}" title="Hold this time on your calendar">Hold</button></li>`; }).join("")}</ul>` : `<div class="dim">No shared free time in that range.</div>`}`;
      out.onclick = async (e) => {
        const c = e.target.closest("[data-copy]"), h = e.target.closest("[data-hold]");
        const w = r.windows[Number((c || h)?.dataset.copy ?? (c || h)?.dataset.hold)];
        if (!w) return;
        const s = fmt(w.start), en = fmt(w.end);
        if (c) {
          const txt = `${new Date(`${s.day}T12:00:00Z`).toLocaleDateString([], { weekday: "long", month: "short", day: "numeric", timeZone: "UTC" })}, ${label12(s.min)}-${label12(en.min)} ET`;
          await navigator.clipboard.writeText(txt).catch(() => {}); toast(`Copied: ${txt}`, "ok");
        } else { T.day = s.day; await loadDay(); newBlock({ subject: "Hold" }, s.min, Number($("#sf-len")?.value || 30)); }
      };
    } catch (e) { out.innerHTML = `<div class="alert">${esc(e.message)}</div>`; }
  });
  $("#sf-who").focus();
}
