// Life (v1.13, SPEC 8.11): the Personal workspace's Ledger place. Work's Ledger is
// Nexus; Personal's is the operator's own life, read from the notes vault and arranged
// by area (home, animals, garden, vehicles, family, spirit, money, fun by default;
// the operator's own list in a private life.toml).
//
// Home: today (the daily note's Log lines, check-ins), what is due in the next two
// weeks, one tile per area, and recently written journal notes. An area: its key notes
// as cards, its open tasks, its recent notes, quick Log / Task / Search.
// Reads only; Log and Task open the usual review card (it writes through vault-mcp).
// Every piece of vault text is escaped before it reaches the page.

import { api, esc, toast } from "./app.js";
import { openNote, searchVault } from "./home.js";

const $ = (s, el = document) => el.querySelector(s);
const F = { open: false, view: "home", area: null, onOpen: null, onClose: null, icons: {} };
const ICON = { home: "&#127969;", animals: "&#128020;", garden: "&#127793;", vehicles: "&#128665;", family: "&#128106;", spirit: "&#127769;", money: "&#128176;", fun: "&#128758;" };

export function initLife({ onOpen, onClose }) { F.onOpen = onOpen; F.onClose = onClose; }
export function lifeOpen() { return F.open; }
export function closeLife() {
  F.open = false; F.onClose?.();
  $("#btn-ledger")?.classList.remove("on");
  const t = $("#thread"); if (t?.dataset.view === "life") { t.innerHTML = ""; delete t.dataset.view; }
}
export function openLife(area = null) {
  F.open = true; F.onOpen?.();
  $("#btn-ledger")?.classList.add("on");
  $("#thread-empty").hidden = true; $("#thread").hidden = false;
  $("#thread").dataset.view = "life";
  return area ? showArea(area) : showHome();
}

const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function nice(iso) {  // "2026-10-03" -> "Oct 3"
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || ""); return m ? `${MON[Number(m[2]) - 1]} ${Number(m[3])}` : "";
}
function clip(t, n) { if (t.length <= n) return t; const c = t.slice(0, n); return `${c.slice(0, Math.max(c.lastIndexOf(" "), n - 15))}...`; }

function when(t) {
  if (t.days == null) return "";
  if (t.days < 0) return `${-t.days}d overdue`;
  if (t.days === 0) return "today";
  if (t.days === 1) return "tomorrow";
  return `in ${t.days}d`;
}

function head(title, back = false) {
  return `<div class="today-head lt-head">
    ${back ? `<button class="btn small ghost" id="lf-home" title="All areas">&#8592; Life</button>` : ""}
    <h2>${title}</h2><span class="dim small-t" id="lf-src"></span>
    <span class="grow"></span>
    <input id="lf-q" class="rinput lt-q" placeholder="Search your notes..." autocomplete="off" aria-label="Search your notes">
    <button class="btn small" id="lf-log" title="Log something to today's daily note (l)">+ Log</button>
    <button class="btn small" id="lf-task" title="Add a personal task (t)">+ Task</button>
    <button class="btn small ghost" id="lf-fresh" title="Reload">&#8635;</button>
    <button class="btn small ghost" id="lf-close" title="Back (Esc)">Close</button>
  </div>`;
}

function stage(action, where = null, text = "") {
  window.dispatchEvent(new CustomEvent("ultra:stage-answer", { detail: { action, text: text || (action === "log" ? " " : "New task"), key: "", task: "", title: "Life", where } }));
}

function wireHead(area) {
  $("#lf-close").onclick = closeLife;
  if ($("#lf-home")) $("#lf-home").onclick = () => showHome();
  $("#lf-fresh").onclick = () => (F.view === "area" ? showArea(F.area) : showHome());
  const q = $("#lf-q");
  q.onkeydown = (e) => { if (e.key === "Enter" && q.value.trim()) searchVault(q.value.trim()); };
  const topic = area?.log_topic;
  $("#lf-log").onclick = () => stage("log", topic ? { kind: "daily" } : null, area ? `${area.name}: ` : "");
  $("#lf-task").onclick = () => stage("task", null, area ? `${area.name}: ` : "");
}

function taskRow(t, showArea = false) {
  return `<li class="day-item lf-task" data-path="${esc(t.path)}" title="${esc(t.path)}${t.line ? ` (line ${esc(t.line)})` : ""}${t.section ? ` - ${esc(t.section)}` : ""}">
    ${t.due ? `<span class="badge lf-when ${t.days < 0 ? "lf-bad" : t.days <= 2 ? "lf-warn" : ""}" title="${esc(t.due)}">${esc(when(t))}</span>` : `<span class="badge lf-when lf-none"></span>`}
    ${showArea && t.area !== "other" ? `<span class="lf-ico" aria-hidden="true">${F.icons[t.area] ? esc(F.icons[t.area]) : ICON[t.area] || ""}</span>` : ""}
    <span class="lf-tt">${esc(t.text)}</span></li>`;
}

async function showHome() {
  F.view = "home"; F.area = null;
  const el = $("#thread");
  el.innerHTML = head("Life") + `<div class="dim">Reading your notes...</div>`;
  wireHead(null);
  let d;
  try { d = await api("/api/life"); } catch (e) { el.innerHTML = head("Life") + `<div class="alert">${esc(e.message)}</div>`; wireHead(null); return; }
  if (!d.enabled) { el.innerHTML = head("Life") + `<div class="dim">No notes vault in this workspace. Add a [vault] section to its config.</div>`; wireHead(null); return; }
  if (d.error) { el.innerHTML = head("Life") + `<div class="alert">The vault did not answer: ${esc(d.error)}</div>`; wireHead(null); return; }
  const day = d.day || {};
  F.icons = Object.fromEntries(d.areas.map((a) => [a.key, a.icon || ""]));
  const tiles = d.areas.map((a) => `<button class="lf-tile" data-area="${esc(a.key)}" title="${esc(a.blurb)}">
      <span class="lf-ico" aria-hidden="true">${a.icon ? esc(a.icon) : ICON[a.key] || ""}</span>
      <b>${esc(a.name)}</b>
      <span class="lf-counts">${a.overdue ? `<span class="tp-chip bad">${a.overdue} overdue</span>` : ""}${a.soon ? `<span class="tp-chip warn">${a.soon} soon</span>` : ""}${!a.overdue && !a.soon ? `<span class="tp-chip ${a.open ? "" : "ok"}">${a.open ? `${a.open} open` : "all clear"}</span>` : ""}</span>
      <span class="lf-next dim small-t">${a.next ? `${a.next.due ? `<b class="lf-nd">${esc(nice(a.next.due))}</b> ` : ""}${esc(clip(a.next.text, 64))}` : esc(a.blurb)}</span>
    </button>`).join("");
  const due = d.due || [];
  const DUE_SHOW = 6;
  el.innerHTML = head("Life") + `
    <div class="lf-tiles">${tiles}</div>
    <div class="lt-cols lf-cols">
    <div class="lf-due"><div class="tp-head"><span class="label">Coming up</span><span class="dim small-t">overdue and the next two weeks</span></div>
      ${due.length ? `<ul class="day-list tp-list">${due.slice(0, DUE_SHOW).map((t) => taskRow(t, true)).join("")}</ul>
        ${due.length > DUE_SHOW ? `<button class="btn small ghost" id="lf-more">Show all ${due.length}</button>` : ""}` : `<div class="dim small-t">Nothing due. Enjoy it.</div>`}</div>
    <div class="lf-today">
      <div class="tp-head"><span class="label">Today</span><span class="dim small-t">${esc(nice(d.today))}</span><span class="grow"></span>
        ${day.daily ? `<button class="btn tiny ghost" data-open="${esc(day.daily)}">Daily note</button>` : ""}
        ${(day.checkins || []).map((c) => `<button class="btn tiny ghost" data-open="${esc(c)}">${esc(c.split("_").pop().replace(".md", ""))} check-in</button>`).join("")}
        ${day.captains_log ? `<button class="btn tiny ghost" data-open="${esc(day.captains_log)}">Captain's Log</button>` : ""}</div>
      ${(day.log || []).length ? `<ul class="day-list tp-list lf-log">${day.log.map((l) => `<li class="day-item">${esc(l.replace(/^- /, ""))}</li>`).join("")}</ul>`
        : `<div class="dim small-t">Nothing logged today yet.${d.writes ? " Press + Log to add a line." : ""}</div>`}
      ${(d.recent || []).length ? `<div class="tp-head lf-gap"><span class="label">Lately</span><span class="dim small-t">recent journal notes and check-ins</span></div>
      <ul class="day-list tp-list">${d.recent.slice(0, 6).map((r) => `<li class="day-item lf-rec" data-open="${esc(r.path)}" title="${esc(r.path)}"><span class="lf-tt">${esc(r.title)}</span><span class="dim small-t">${esc(nice(r.date))}</span></li>`).join("")}</ul>` : ""}
    </div>
    </div>`;
  wireHead(null);
  wireBody(el);
  if ($("#lf-more")) $("#lf-more").onclick = (e) => { e.target.previousElementSibling.innerHTML = due.map((t) => taskRow(t, true)).join(""); e.target.remove(); };
  api("/api/vault/status").then((s) => { const x = $("#lf-src"); if (x) x.textContent = s.writes ? "from your notes" : "from your notes (read only)"; x.title = `Obsidian vault: ${s.name}`; }).catch(() => {});
}

async function showArea(key) {
  F.view = "area"; F.area = key;
  const el = $("#thread");
  el.innerHTML = head("Life", true) + `<div class="dim">Reading...</div>`;
  wireHead(null);
  let a;
  try { a = await api(`/api/life/area/${encodeURIComponent(key)}`); } catch (e) { el.innerHTML = head("Life", true) + `<div class="alert">${esc(e.message)}</div>`; wireHead(null); return; }
  el.innerHTML = head(`<span class="lf-ico" aria-hidden="true">${a.icon ? esc(a.icon) : ICON[a.key] || ""}</span> ${esc(a.name)}`, true) + `
    <div class="dim small-t lf-blurb">${esc(a.blurb)}</div>
    ${a.notes.length ? `<div class="lf-cards">${a.notes.map((n) => `<button class="lf-card" data-open="${esc(n.path)}" title="${esc(n.path)}"><b>${esc(n.label)}</b><span class="dim small-t">${esc(n.path.split("/").slice(0, -1).join(" / "))}</span></button>`).join("")}</div>` : ""}
    <div class="lt-cols">
      <div><div class="tp-head"><span class="label">Open tasks</span><span class="dim small-t">${a.tasks.length}</span></div>
        ${a.tasks.length ? `<ul class="day-list tp-list">${a.tasks.map((t) => taskRow(t)).join("")}</ul>` : `<div class="dim small-t">Nothing open here.</div>`}</div>
      <div><div class="tp-head"><span class="label">Recently written</span></div>
        ${a.recent.length ? `<ul class="day-list tp-list">${a.recent.map((r) => `<li class="day-item lf-rec" data-open="${esc(r.path)}" title="${esc(r.path)}"><span class="lf-tt">${esc(r.title)}</span><span class="dim small-t">${esc(nice(r.date))}</span></li>`).join("")}</ul>` : `<div class="dim small-t">No recent notes in this area.</div>`}</div>
    </div>`;
  wireHead(a);
  wireBody(el);
}

function wireBody(el) {
  el.onclick = (e) => {
    const t = e.target.closest("[data-area]"); if (t) return showArea(t.dataset.area);
    const o = e.target.closest("[data-open]"); if (o) return openNote(o.dataset.open);
    const k = e.target.closest(".lf-task[data-path]"); if (k) return openNote(k.dataset.path);
  };
}
