// Today view (SPEC 7.4): day timeline, drag to block, meeting prep, slot finder.
// Writes only ever touch blocks Ultra created (tagged ultra=1), and the server enforces
// that. Creating a block is one click after the block card; nothing is sent to anyone.

import { api, esc, toast, busy } from "./app.js";

const $ = (s, el = document) => el.querySelector(s);
const PX = 1.1;                 // pixels per minute
const SNAP = 15;                // minutes
const T = { day: null, data: null, tz: "UTC", open: false, onClose: null, dragItem: null };

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
      <span class="grow"></span>
      <button class="btn small" id="cal-slots">Find a time</button>
      <button class="btn small ghost" id="cal-fresh" title="Reload from Google">&#8635;</button>
      <button class="btn small ghost" id="cal-close" title="Back to the conversation (Esc)">Close</button>
    </div>
    ${d.error ? `<div class="alert">${esc(d.error)}</div>` : ""}
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
      <span class="k">Notes</span><textarea id="bk-desc" rows="6">${esc(t.description)}</textarea>
    </div>
    <div class="row-g"><button class="btn send" id="bk-create">Add to calendar</button><button class="btn ghost" id="bk-cancel">Cancel</button><span class="dim small-t" id="bk-msg"></span></div>
  </div>`;
  $("#bk-cancel").onclick = () => { panel.innerHTML = ""; };
  $("#bk-create").onclick = (e) => busy(e.target, async () => {
    const start = `${$("#bk-day").value}T${$("#bk-start").value}`;
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
    <div class="row-g"><button class="btn" id="bm-save">Save</button><button class="btn danger" id="bm-del">Delete block</button><button class="btn ghost" id="bm-x">Close</button>
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
    <div class="dim small-t">${e.organizer_self ? "You organise this." : `Organiser: ${esc(e.organizer)}`} ${e.recurring ? "&middot; recurring" : ""} &middot; read-only in Ultra</div>
    ${e.description ? `<details class="quoted" open><summary>Invite notes</summary><div class="body">${esc(e.description)}</div></details>` : `<div class="dim small-t">No invite notes.</div>`}
    <div class="sect"><div class="label">People (${e.attendee_count}${people.length && people.length < e.attendee_count ? `, ${people.length} besides you` : ""})</div>
      ${people.length ? `<div class="plist" id="mp-people">${people.map((p, i) => `<div class="prow" data-i="${i}" tabindex="0"><div class="pmain"><b>${esc(p.name || p.addr)}</b> <span class="badge ${p.response === "accepted" ? "ok" : p.response === "declined" ? "warn" : "dim"}">${esc(resp[p.response] || p.response || "?")}</span></div><div class="dim small-t">${esc(p.addr)}</div></div>`).join("")}</div>
      <div class="dim small-t hint">Click a person for their ledger context in the right panel.</div>` : `<div class="dim small-t">${e.attendee_count > 12 ? "Large meeting: attendees not listed." : "Only you."}</div>`}
    </div>
  </div>`;
  $("#mp-x").onclick = () => { panel.innerHTML = ""; };
  $("#mp-people")?.addEventListener("click", (ev) => {
    const row = ev.target.closest(".prow"); if (!row) return;
    const p = people[Number(row.dataset.i)];
    window.dispatchEvent(new CustomEvent("ultra:person", { detail: { addr: p.addr, name: p.name } }));
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
