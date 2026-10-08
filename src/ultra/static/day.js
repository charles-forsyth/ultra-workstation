// Day view (v0.9): the morning check-in plan and the end-of-day report.
// Reads only. Suggested blocks open the normal block card (nothing is created until you
// press Add to calendar there); the report saves to the ledger (Personal: the notes) only
// through a staged log card you review and commit.

import { api, esc, toast, busy, copyText, WS, newTasksOn } from "./app.js";
import { askButton, openAsk } from "./ask.js";
import { openNote } from "./home.js";

const $ = (s, el = document) => el.querySelector(s);
const D = { open: false, tab: "plan", day: null, onOpen: null, onClose: null, plan: null, report: null };

export function initDay({ onOpen, onClose, openItem }) {
  D.onOpen = onOpen; D.onClose = onClose; D.openItem = openItem;
  $("#btn-day")?.addEventListener("click", () => (D.open ? closeDay() : openDay()));
}
export function dayOpen() { return D.open; }
export function closeDay() {
  D.open = false; $("#btn-day")?.classList.remove("on");
  D.onClose?.();
}
export async function openDay(tab = null) {
  D.open = true; D.onOpen?.();
  $("#btn-day")?.classList.add("on");
  // Check the real date and time first (server clock, the configured time zone): a
  // phone tab or a laptop waking from sleep can be hours or days stale.
  try { D.now = await api("/api/now"); } catch { D.now = null; }
  if (tab) D.tab = tab;
  else D.tab = (D.now ? D.now.hour : new Date().getHours()) >= 15 ? "report" : "plan";  // afternoon: the report
  $("#thread-empty").hidden = true;
  const th = $("#thread"); th.hidden = false;
  render();
}

function head() {
  return `<div class="today-head">
    <h2>Day</h2>${D.now ? `<span class="dim small-t" id="day-now" title="Read from the clock when this page was built">${esc(D.now.weekday)} ${esc(D.now.date)}, ${esc(D.now.time)}</span>` : ""}
    <div class="seg" id="day-seg"><button data-dt="plan" class="${D.tab === "plan" ? "on" : ""}">Check-in plan</button><button data-dt="report" class="${D.tab === "report" ? "on" : ""}">End-of-day report</button></div>
    <button class="btn small ghost calm-only" data-goto="today" title="Calendar (g)">Calendar</button>
    <span class="grow"></span>
    <a class="btn small ghost" href="${WS.url("/api/day/agenda.ics")}" download title="Today's agenda as a calendar file">Agenda .ics</a>
    <a class="btn small ghost" href="${WS.url("/api/day/journal.csv")}" download title="Everything Ultra did today, as CSV">Journal .csv</a>
    <button class="btn small ghost" id="day-fresh" title="Rebuild">&#8635;</button>
    <button class="btn small ghost" id="day-close" title="Back (Esc)">Close</button>
  </div>`;
}

async function render() {
  const th = $("#thread");
  // re-read the clock on every build (the tab may have sat open past midnight)
  try { D.now = await api("/api/now"); } catch { /* keep the last reading */ }
  th.innerHTML = head() + `<div class="dim">Building the ${D.tab === "plan" ? "plan" : "report"}...</div>`;
  wireHead();
  try {
    if (D.tab === "plan") { D.plan = await api("/api/day/plan"); if (D.open && D.tab === "plan") renderPlan(); }
    else { D.report = await api("/api/day/report"); if (D.open && D.tab === "report") renderReport(); }
  } catch (e) { th.innerHTML = head() + `<div class="alert">${esc(e.message)}</div>`; wireHead(); }
}

function wireHead() {
  $("#day-seg").onclick = (e) => { const b = e.target.closest("[data-dt]"); if (b) { D.tab = b.dataset.dt; render(); } };
  $("#day-close").onclick = closeDay;
  $("#day-fresh").onclick = render;
}

const hm = (m) => { const h = Math.floor(m / 60), mm = m % 60; return `${((h + 11) % 12) + 1}:${String(mm).padStart(2, "0")}${h < 12 ? "am" : "pm"}`; };
const tOf = (iso) => { try { return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); } catch { return ""; } };
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const dShort = (d) => { const m = /^(\d{4})-(\d\d)-(\d\d)/.exec(String(d || "")); return m ? `${MON[Number(m[2]) - 1]} ${Number(m[3])}` : ""; };
const pri = (p) => `<span class="pri ${esc(String(p || "").toLowerCase())}">${esc(String(p || "").slice(0, 1))}</span>`;

function renderPlan() {
  const p = D.plan, th = $("#thread");
  const sec = (title, body, n = null) => body ? `<section class="day-sec"><div class="label">${title}${n != null ? ` <span class="dim">${n}</span>` : ""}</div>${body}</section>` : "";
  const items = (xs) => xs.length ? `<ul class="day-list">${xs.map((i) => `<li class="day-item" data-key="${esc(i.key)}"><span class="src">${esc(i.source)}</span> <b>${esc(i.from || "")}</b> ${esc(i.subject || "")} ${(i.badges || []).map((b) => `<span class="badge">${esc(b)}</span>`).join(" ")}${i.waiting_days ? ` <span class="badge wait">${esc(i.waiting_days)}d</span>` : ""}</li>`).join("")}</ul>` : "";
  const P = !!p.personal;
  // v1.15: where ledger tasks are archive-only, the Work Day's task lists say so
  const arch = !P && !newTasksOn();
  const tasks = (xs) => xs.length ? `<ul class="day-list">${xs.map((t) => P
    ? `<li class="day-item" data-note="${esc(t.path || "")}" title="${esc(t.path || "")}">${esc(t.summary)}${t.due_date ? ` <span class="dim small-t">${esc(dShort(t.due_date))}</span>` : ""}</li>`
    : `<li class="day-item" data-key="t-${esc(t.id)}">${pri(t.priority)} ${esc(t.summary)} <span class="dim small-t">${esc(t.status)}${t.due_date ? ` &middot; due ${esc(String(t.due_date).slice(0, 10))}` : ""}</span></li>`).join("")}</ul>` : "";
  const nt = p.notes || {};
  const logged = P ? sec("Logged today", (nt.log || []).length ? `<ul class="day-list">${nt.log.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : `<div class="dim small-t">Nothing in today's daily note yet.</div>`) : "";
  th.innerHTML = head() + `
    ${(p.warnings || []).map((w) => `<div class="warnline">${esc(w)}</div>`).join("")}
    <div class="day-sum">${P
      ? `${p.meetings.length} on the calendar &middot; ${p.mine_count} emails are your move &middot; ${p.overdue.length} overdue &middot; ${p.due_today.length} due today &middot; ${p.top_tasks.length} coming up`
      : `${p.meetings.length} meetings (${(p.meeting_minutes / 60).toFixed(1)} h) &middot; ${p.free.reduce((a, f) => a + f.minutes, 0)} min free in work hours &middot; ${p.mine_count} items are your move &middot; ${p.open_tasks} open tasks`}</div>
    <div class="pacts"><button class="btn small ai" id="day-note">AI read of the day</button><button class="btn small" id="day-copy">Copy plan</button><button class="btn small" id="day-listen">Listen</button>${askButton("btn small ai")}</div>
    <div id="day-note-out"></div>
    ${sec(P ? "On the calendar" : "Meetings", p.meetings.length ? `<ul class="day-list">${p.meetings.map((m) => `<li>${esc(tOf(m.start))}-${esc(tOf(m.end))} <b>${esc(m.summary)}</b>${m.attendees > 1 ? ` <span class="dim small-t">${m.attendees} people</span>` : ""}${m.response === "needsAction" ? ` <span class="badge warn">not answered</span>` : ""}${m.mine ? ` <span class="badge dim">your block</span>` : ""}</li>`).join("")}</ul>` : `<div class="dim small-t">${P ? "Nothing on the calendar." : "No meetings."}</div>`)}
    ${P ? "" : sec("Suggested focus blocks", p.blocks.length ? `<ul class="day-list">${p.blocks.map((b, i) => `<li>${esc(b.label)} ${esc(b.title)} <button class="btn tiny" data-block="${i}" title="Open the block card for this slot (nothing is added until you confirm there)">Block it</button></li>`).join("")}</ul><div class="dim small-t">Free: ${p.free.map((f) => esc(f.label)).join(", ")}</div>` : (p.free.length ? `<div class="dim small-t">Free: ${p.free.map((f) => esc(f.label)).join(", ")}</div>` : `<div class="dim small-t">No free time left in work hours.</div>`))}
    ${sec(P ? "Email that is your move" : "Your move", items(p.mine) || `<div class="dim small-t">Nothing waiting on you.</div>`, p.mine_count)}
    ${sec(P ? "Overdue" : arch ? "Nexus tasks (archive): overdue" : "Overdue tasks", tasks(p.overdue), p.overdue.length || null)}
    ${sec(P || !arch ? "Due today" : "Nexus tasks (archive): due today", tasks(p.due_today), p.due_today.length || null)}
    ${sec(P ? "Coming up (two weeks)" : arch ? "Nexus tasks (archive): high priority" : "High-priority tasks", tasks(p.top_tasks), p.top_tasks.length || null)}
    ${logged}
    ${sec("Waiting on others (3+ days)", items(p.waiting), p.waiting.length || null)}`;
  wireHead();
  th.onclick = (e) => {
    const b = e.target.closest("[data-block]");
    if (b) { const x = p.blocks[Number(b.dataset.block)]; window.dispatchEvent(new CustomEvent("ultra:block", { detail: { key: x.key, subject: x.title.replace(/^Focus: /, ""), day: p.day, startMin: x.start, minutes: x.end - x.start } })); return; }
    const n = e.target.closest(".day-item[data-note]");
    if (n) { if (n.dataset.note) openNote(n.dataset.note); return; }
    const li = e.target.closest(".day-item[data-key]");
    if (li) { closeDay(); D.openItem?.(li.dataset.key); }
  };
  $("#day-copy").onclick = () => copyText(p.text);
  th.querySelector(".pacts [data-ask]").onclick = (ev) => { ev.stopPropagation(); openAsk({ type: "day", view: "plan", day: p.day }, `Plan for ${p.day}`); };
  $("#day-listen").onclick = () => window.dispatchEvent(new CustomEvent("ultra:listen", { detail: { text: p.text, title: `Plan for ${p.day}` } }));
  $("#day-note").onclick = (e) => busy(e.currentTarget, async () => {
    const out = $("#day-note-out");
    out.innerHTML = `<div class="dim small-t">Reading your day...</div>`;
    try {
      const r = await api("/api/day/plan/note", { method: "POST", body: { day: p.day } });
      out.innerHTML = `<div class="aisum"><div class="aisum-head"><span class="badge ai">AI read</span><span class="dim mono small-t">${esc(r.model)}</span></div><div class="body">${esc(r.text)}</div></div>`;
    } catch (err) { out.innerHTML = `<div class="lint error">${esc(err.message)}</div>`; }
  });
}

function renderReport() {
  const r = D.report, th = $("#thread");
  const P = !!r.personal;
  const sections = (P ? ["Sent", "Notes", "Triage", "Calendar"] : ["Sent", "Ledger", "Triage", "Calendar", "Research", "Other"]).filter((s) => (r.sections[s] || []).length);
  th.innerHTML = head() + `
    <div class="day-sum">${r.day} &middot; ${r.counts.sent || 0} sent &middot; ${r.counts.archive || 0} archived &middot; ${P ? `${(r.notes?.log || []).length} lines in today's note &middot; ${r.counts.vault_task_done || 0} to-dos done` : `${r.counts.ledger_log || 0} logged &middot; ${r.counts.ledger_task_status || 0} task changes`}${r.failures ? ` &middot; <span class="badge warn">${r.failures} failed actions</span>` : ""}</div>
    <div class="pacts"><button class="btn small" id="rep-copy">Copy</button><button class="btn small" id="rep-listen">Listen</button>${askButton("btn small ai")}<button class="btn small primary" id="rep-log" title="${P ? "Save to your notes (you review it on the card first)" : "Save as a ledger log (you review it on the card first)"}">${P ? "Save to notes" : "Save to ledger"}</button></div>
    <textarea id="rep-text" rows="14" class="rep-text">${esc(r.text)}</textarea>
    <div class="dim small-t">Edit freely; Copy, Listen and Save use what is in the box.</div>
    ${sections.map((s) => `<section class="day-sec"><div class="label">${s} <span class="dim">${r.sections[s].length}</span></div><ul class="day-list">${r.sections[s].map((x) => `<li><span class="mono dim small-t">${esc(x.time)}</span> ${esc(x.text)}${x.ok ? "" : ` <span class="badge warn">failed</span>`}</li>`).join("")}</ul></section>`).join("") || `<div class="dim">Nothing in Ultra's journal for today yet.</div>`}`;
  wireHead();
  const text = () => $("#rep-text").value;
  $("#rep-copy").onclick = () => copyText(text());
  th.querySelector(".pacts [data-ask]").onclick = () => openAsk({ type: "day", view: "report", day: r.day }, `End of day ${r.day}`);
  $("#rep-listen").onclick = () => window.dispatchEvent(new CustomEvent("ultra:listen", { detail: { text: text(), title: `End of day ${r.day}` } }));
  $("#rep-log").onclick = (e) => busy(e.currentTarget, async () => {
    window.dispatchEvent(new CustomEvent("ultra:stage-text", { detail: { text: text(), title: `End of day ${r.day}` } }));
  });
}
