// Inbox Tidy (v1.1, SPEC 8.1): rule-based bulk archive, previewed, one Undo.
// The server builds the list from the same stream the Desk shows (no AI). Nothing is
// archived until "Archive N"; the run uses the preview's single-use token, and the
// server refuses any thread that was not in that preview. Undo stays available for
// 24 hours from the "Undo last tidy" button in this dialog (and 10 s in the toast).

import { api, esc, toast, busy } from "./app.js";

const $ = (s, el = document) => el.querySelector(s);
const T = { days: 7, lastUndo: null, onDone: null };

export function initTidy({ onDone }) {
  T.onDone = onDone;
  $("#btn-tidy")?.addEventListener("click", () => openTidy());
}

export async function openTidy(days = T.days) {
  const dlg = $("#review");
  dlg.innerHTML = `<div class="rv-card" role="dialog" aria-modal="true" aria-labelledby="td-h"><h3 id="td-h">Tidy the inbox</h3><div class="dim">Building the list...</div></div>`;
  dlg.hidden = false;
  let p;
  try { p = await api("/api/mail/tidy/preview", { method: "POST", body: { days } }); }
  catch (e) { dlg.innerHTML = `<div class="rv-card"><h3>Tidy the inbox</h3><div class="alert">${esc(e.message)}</div><div class="rv-acts"><button class="btn" id="td-x">Close</button></div></div>`; $("#td-x").onclick = close; return; }
  T.days = p.days;
  const row = (r, on) => `<label class="td-row"><input type="checkbox" ${on ? "checked" : ""} data-th="${esc(r.threads.join(","))}">
      <span class="td-main"><b>${esc(r.from)}</b> <span class="ell">${esc(r.subject)}</span></span>
      <span class="dim small-t td-why">${esc(r.reason)}</span></label>`;
  dlg.innerHTML = `<div class="rv-card td-card" role="dialog" aria-modal="true" aria-labelledby="td-h">
    <div class="lc-head"><h3 id="td-h">Tidy the inbox</h3><span class="grow"></span>
      <span class="td-daysbox"><label class="small-t" for="td-days">Older than</label> <input id="td-days" type="number" min="1" max="365" value="${esc(p.days)}" class="rinput td-days"> <span class="small-t">days</span>
      <button class="btn tiny ghost" id="td-re">Apply</button></span></div>
    <div class="dim small-t">Archives automated mail and anything older than ${esc(p.days)} days that is not your move, VIP, ready, assigned to you, on your Watching list, or active today. Archive only removes the Inbox label: nothing is deleted or marked read. Slack and tasks are never touched. Untick anything to keep it.</div>
    <div class="label td-gap">Will archive (${p.archive.length})</div>
    <div class="td-list" id="td-arch">${p.archive.length ? p.archive.map((r) => row(r, true)).join("") : `<div class="dim small-t">Nothing to archive. Your inbox is already tidy.</div>`}</div>
    <details class="td-gap"><summary class="label">Will keep (${p.keep.length})</summary><div class="td-list">${p.keep.map((r) => `<div class="td-row td-kept"><span class="td-main"><b>${esc(r.from)}</b> <span class="ell">${esc(r.subject)}</span></span><span class="dim small-t td-why">${esc(r.reason)}</span></div>`).join("")}</div></details>
    <div class="rv-acts">
      ${T.lastUndo ? `<button class="btn ghost" id="td-undo">Undo last tidy</button>` : ""}
      <span class="grow"></span>
      <button class="btn ghost" id="td-x">Cancel</button>
      <button class="btn send" id="td-go" ${p.archive.length ? "" : "disabled"}>Archive ${p.thread_count}</button>
    </div></div>`;
  const count = () => {
    const n = [...dlg.querySelectorAll("#td-arch input:checked")].reduce((a, c) => a + c.dataset.th.split(",").length, 0);
    const b = $("#td-go"); b.textContent = `Archive ${n}`; b.disabled = n === 0;
  };
  dlg.querySelectorAll("#td-arch input").forEach((c) => (c.onchange = count));
  $("#td-x").onclick = close;
  $("#td-re").onclick = () => openTidy(Number($("#td-days").value) || 7);
  $("#td-days").onkeydown = (e) => { if (e.key === "Enter") $("#td-re").click(); };
  if ($("#td-undo")) $("#td-undo").onclick = (e) => busy(e.currentTarget, undo);
  $("#td-go").onclick = (e) => busy(e.currentTarget, async () => {
    const threads = [...dlg.querySelectorAll("#td-arch input:checked")].flatMap((c) => c.dataset.th.split(","));
    const r = await api("/api/mail/tidy/run", { method: "POST", body: { token: p.token, threads } });
    T.lastUndo = r.undo;
    close();
    toast(`Archived ${r.archived.length} thread(s).`, "ok", { label: "Undo", fn: undo });
    T.onDone?.();
  });
  dlg.onkeydown = (e) => { if (e.key === "Escape") close(); };
  $("#td-go").focus();
}

async function undo() {
  if (!T.lastUndo) return;
  try {
    const r = await api("/api/mail/tidy/undo", { method: "POST", body: { undo: T.lastUndo } });
    T.lastUndo = null;
    close();
    toast(`Put ${r.unarchived.length} thread(s) back in the inbox.`, "ok");
    T.onDone?.();
  } catch (e) { toast(e.message, "err"); }
}

function close() { const d = $("#review"); d.hidden = true; d.innerHTML = ""; }
