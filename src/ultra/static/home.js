// Home: the personal notes vault, read only (SPEC 8.10, v1.10).
// A Home line on Today (overdue and due-this-week todos from the vault), palette search
// ("Search the vault for ..."), and a note reader. Nothing here writes; note text is
// shown as text (renderMd escapes everything first). "Open in Obsidian" hands the note
// to the Obsidian app by its obsidian:// link.

import { api, esc, toast } from "./app.js";
import { renderMd } from "./tools.js";

const $ = (s, el = document) => el.querySelector(s);
let enabled = null;

export async function vaultOn() {
  if (enabled !== null) return enabled;
  try { enabled = !!(await api("/api/vault/status")).enabled; } catch { enabled = false; }
  return enabled;
}

// The Home line on Today: shown only when something is overdue or due this week.
export async function homeLine(box) {
  if (!box || !(await vaultOn())) return;
  let h;
  try { h = await api("/api/vault/home"); } catch { return; }
  if (!h.enabled) return;
  if (h.error) { box.innerHTML = `<div class="tp-head"><span class="label">Home</span><span class="tp-chip wait" title="${esc(h.error)}">vault unreachable</span></div>`; return; }
  const items = [...(h.overdue || []), ...(h.soon || [])];
  if (!items.length) { box.innerHTML = ""; return; }
  const when = (x) => (x.days < 0 ? `${-x.days}d overdue` : x.days === 0 ? "today" : x.days === 1 ? "tomorrow" : `in ${x.days}d`);
  const chips = [];
  if ((h.overdue || []).length) chips.push(`<span class="tp-chip bad">${h.overdue.length} overdue</span>`);
  if ((h.soon || []).length) chips.push(`<span class="tp-chip warn">${h.soon.length} this week</span>`);
  box.innerHTML = `<div class="tp-head"><span class="label">Home</span>${chips.join("")}<span class="grow"></span>
      <button class="btn tiny ghost" id="home-search" title="Search the vault (read only)">Search notes</button></div>
    <ul class="day-list tp-list">${items.slice(0, 5).map((x) => `<li class="day-item home-item" data-path="${esc(x.path)}" title="${esc(x.path)}${x.line ? ` (line ${esc(x.line)})` : ""}"><span class="badge ${x.days < 0 ? "bad" : ""}">${esc(when(x))}</span> ${esc(x.text)}</li>`).join("")}</ul>`;
  box.onclick = (e) => {
    if (e.target.closest("#home-search")) return searchVault("");
    const li = e.target.closest(".home-item[data-path]"); if (li) openNote(li.dataset.path);
  };
}

export async function searchVault(q0) {
  const dlg = $("#review");
  dlg.innerHTML = `<div class="rv-card vt-card" role="dialog" aria-modal="true" aria-labelledby="vt-h">
    <div class="lc-head"><h3 id="vt-h">Search your notes</h3><span class="grow"></span><span class="dim small-t">read only</span></div>
    <input id="vt-q" class="rinput" placeholder="garlic, Jayco warranty, coop door..." value="${esc(q0 || "")}" autocomplete="off">
    <div id="vt-res" class="vt-res"></div>
    <div class="rv-acts"><span class="grow"></span><button class="btn ghost" id="vt-x">Close</button></div></div>`;
  dlg.hidden = false;
  const close = () => { dlg.hidden = true; dlg.innerHTML = ""; };
  const run = async () => {
    const q = $("#vt-q").value.trim(); const box = $("#vt-res");
    if (!q) { box.innerHTML = ""; return; }
    box.innerHTML = `<div class="dim small-t">Searching...</div>`;
    try {
      const r = await api(`/api/vault/search?q=${encodeURIComponent(q)}`);
      box.innerHTML = r.results.length ? r.results.map((x) => `<div class="vt-row" data-path="${esc(x.path)}" tabindex="0"><b>${esc(x.title)}</b><div class="dim small-t ell">${esc(x.path)}</div>${x.snippet && x.snippet !== x.title ? `<div class="small-t vt-snip">${esc(x.snippet)}</div>` : ""}</div>`).join("") : `<div class="dim small-t">No notes match.</div>`;
    } catch (e) { box.innerHTML = `<div class="alert">${esc(e.message)}</div>`; }
  };
  $("#vt-q").onkeydown = (e) => { if (e.key === "Enter") run(); if (e.key === "Escape") close(); };
  $("#vt-res").onclick = (e) => { const r = e.target.closest(".vt-row"); if (r) openNote(r.dataset.path); };
  $("#vt-res").onkeydown = (e) => { const r = e.target.closest(".vt-row"); if (r && e.key === "Enter") openNote(r.dataset.path); };
  $("#vt-x").onclick = close;
  dlg.onclick = (e) => { if (e.target === dlg) close(); };
  $("#vt-q").focus();
  if (q0) run();
}

export async function openNote(path) {
  const dlg = $("#review");
  dlg.innerHTML = `<div class="rv-card vt-card"><div class="dim">Reading ${esc(path)}...</div></div>`;
  dlg.hidden = false;
  let n;
  try { n = await api(`/api/vault/note?path=${encodeURIComponent(path)}`); }
  catch (e) { dlg.innerHTML = `<div class="rv-card vt-card"><div class="alert">${esc(e.message)}</div><div class="rv-acts"><span class="grow"></span><button class="btn" id="vt-x">Close</button></div></div>`; $("#vt-x").onclick = () => (dlg.hidden = true); return; }
  dlg.innerHTML = `<div class="rv-card vt-card vt-note" role="dialog" aria-modal="true" aria-labelledby="vt-nh">
    <div class="lc-head"><h3 id="vt-nh" class="ell">${esc(n.path.split("/").pop())}</h3><span class="grow"></span><span class="dim small-t">read only</span></div>
    <div class="dim small-t">${esc(n.path)}</div>
    <div class="vt-body md">${renderMd(n.contents)}</div>
    <div class="rv-acts">
      <button class="btn ghost" id="vt-back">Search notes</button>
      <button class="btn ghost" id="vt-copy">Copy text</button>
      ${n.open_url ? `<a class="btn ghost" href="${esc(n.open_url)}" title="Open this note in the Obsidian app">Open in Obsidian</a>` : ""}
      <span class="grow"></span><button class="btn" id="vt-x">Close</button></div></div>`;
  $("#vt-x").onclick = () => { dlg.hidden = true; dlg.innerHTML = ""; };
  $("#vt-back").onclick = () => searchVault("");
  $("#vt-copy").onclick = async () => { try { await navigator.clipboard.writeText(n.contents); toast("Copied.", "ok"); } catch { toast("Copy failed.", "err"); } };
  dlg.onkeydown = (e) => { if (e.key === "Escape") { dlg.hidden = true; dlg.innerHTML = ""; } };
  dlg.onclick = (e) => { if (e.target === dlg) { dlg.hidden = true; dlg.innerHTML = ""; } };
}
