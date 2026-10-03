// Board (v0.15, SPEC 7.3): the stream as court columns.
// My Court | Waiting On (grouped by person, Nudge) | Watching (local flag) | Done today.
// Moves stage the same actions the rest of Ultra uses; nothing external is written
// without the usual card or confirmation:
//   to Watching -> local flag (optional date), no external write
//   to Done     -> a card with checkboxes: archive / mark done, log it, complete task;
//                  nothing runs until "Do it"; logs go through the normal log card
//   to Waiting  -> a task card ("Follow up ...") with a due date you pick
//   to My Court -> clears a Watching flag
//   Nudge       -> one new-email draft to the person (two approvals to send)

import { api, esc, toast, busy } from "./app.js";
import { openAsk } from "./ask.js";

const $ = (s, el = document) => el.querySelector(s);
const B = { open: false, data: null, onOpen: null, onClose: null, openItem: null, drag: null };
const COLS = [
  ["mine", "My court", "Your move"],
  ["waiting", "Waiting on", "They owe you"],
  ["watching", "Watching", "Parked, local only"],
  ["done", "Done today", "From Ultra's journal"],
];

export function initBoard({ onOpen, onClose, openItem }) {
  B.onOpen = onOpen; B.onClose = onClose; B.openItem = openItem;
  $("#btn-board")?.addEventListener("click", () => (B.open ? closeBoard() : openBoard()));
}
export function boardOpen() { return B.open; }
export function closeBoard() {
  B.open = false; $("#btn-board")?.classList.remove("on");
  B.onClose?.();
}
export async function openBoard() {
  B.open = true; B.onOpen?.();
  $("#btn-board")?.classList.add("on");
  $("#thread-empty").hidden = true;
  const th = $("#thread"); th.hidden = false;
  th.innerHTML = head() + `<div class="dim">Building the board...</div>`;
  wireHead();
  await load();
}

function head() {
  const c = B.data?.counts || {};
  return `<div class="today-head">
    <h2>Board</h2><span class="dim small-t bd-head-counts">${B.data ? `${c.mine} your move &middot; ${c.waiting} waiting &middot; ${c.watching} watching &middot; ${c.done} done today` : ""}</span>
    <button class="btn small ghost calm-only" data-goto="inbox" title="Back to the list (Esc)">List</button>
    <span class="grow"></span>
    <button class="btn small ghost" id="bd-fresh" title="Rebuild">&#8635;</button>
    <button class="btn small ghost" id="bd-close" title="Back (Esc)">Close</button>
  </div>`;
}
function wireHead() {
  $("#bd-close").onclick = closeBoard;
  $("#bd-fresh").onclick = (e) => busy(e.currentTarget, load);
}

async function load() {
  try { B.data = await api("/api/board"); }
  catch (e) { $("#thread").innerHTML = head() + `<div class="alert">${esc(e.message)}</div>`; wireHead(); return; }
  if (B.open) render();
}

const ageBadge = (r) => r.waiting_days ? `<span class="badge ${r.waiting_days >= 5 ? "warn" : r.waiting_days >= 3 ? "wait" : "dim"}">${esc(r.waiting_days)}d</span>` : "";
const srcLabel = (r) => ({ email: "mail", ticket: "ticket", slack: "slack", task: "task" })[r.source] || r.source || "";

function cardHtml(r, col) {
  const badges = (r.badges || []).filter((b) => b !== "SLOW" && b !== "OVERDUE").map((b) => `<span class="badge ${b === "READY" ? "ready" : ""}">${esc(b)}</span>`).join("");
  const w = r.watch ? `<div class="dim small-t">${r.watch.until ? `until ${esc(r.watch.until)}` : "no date"}${r.watch.note ? ` &middot; ${esc(r.watch.note)}` : ""}</div>` : "";
  return `<div class="bd-card" draggable="true" data-key="${esc(r.key)}" data-col="${col}" tabindex="0" role="button" aria-label="${esc(r.subject || "")}">
    <div class="bd-top"><span class="src">${esc(srcLabel(r))}</span><b class="bd-from" title="${esc(r.from || "")}">${esc(r.from || "")}</b>${ageBadge(r)}</div>
    <div class="bd-subj">${esc(r.subject || "(no subject)")}</div>
    ${badges ? `<div class="badges">${badges}</div>` : ""}${w}
    <div class="bd-acts">
      ${col !== "watching" ? `<button class="btn tiny ghost" data-bd="watch" title="Park it in Watching (local only)">Watch</button>` : `<button class="btn tiny ghost" data-bd="unwatch" title="Back to its column">Unwatch</button>`}
      ${col === "mine" ? `<button class="btn tiny ghost" data-bd="waiting" title="Make a follow-up task with a due date">Wait</button>` : ""}
      <button class="btn tiny ghost" data-bd="done" title="Archive / log / complete, on one card">Done</button>
      <button class="btn tiny ghost" data-bd="ask" title="Ask Hermes about it">Ask</button>
    </div>
  </div>`;
}

function render() {
  const d = B.data, cols = d.columns;
  const body = {
    mine: cols.mine.map((r) => cardHtml(r, "mine")).join("") || `<div class="dim small-t">Nothing is your move.</div>`,
    waiting: cols.waiting.map((g, gi) => `<div class="bd-group">
        <div class="bd-ghead"><b>${esc(g.who)}</b> <span class="dim small-t">${g.items.length} item${g.items.length === 1 ? "" : "s"}${g.oldest_days ? `, oldest ${g.oldest_days}d` : ""}</span><span class="grow"></span>
          ${g.can_nudge ? `<button class="btn tiny ai" data-nudge="${gi}" title="One follow-up email to ${esc(g.addr)} listing every thread waiting on them (a draft: you edit and approve twice)">Nudge</button>` : ""}</div>
        ${g.items.map((r) => cardHtml(r, "waiting")).join("")}</div>`).join("") || `<div class="dim small-t">Nobody owes you a reply.</div>`,
    watching: cols.watching.map((r) => cardHtml(r, "watching")).join("") || `<div class="dim small-t">Drag something here to park it. Flags are local and lapse on their date.</div>`,
    done: cols.done.map((x) => `<div class="bd-done"><span class="mono dim small-t">${esc(x.time)}</span> ${esc(x.what)}${x.subject ? `: ${esc(x.subject)}` : ""}</div>`).join("") || `<div class="dim small-t">Nothing finished yet today. Drag a card here.</div>`,
  };
  const th = $("#thread");
  th.innerHTML = head() + `<div class="bd-cols">${COLS.map(([k, label, sub]) => `<section class="bd-col" data-drop="${k}" aria-label="${label}">
      <div class="bd-chead"><span class="label">${label}</span> <span class="dim">${d.counts[k]}</span><div class="dim small-t">${sub}</div></div>
      <div class="bd-body">${body[k]}</div></section>`).join("")}</div>
    <div class="dim small-t">Drag cards between columns, or use the buttons on a card. Watching is local to Ultra; Done and Wait open a card first; Nudge makes a draft.</div>`;
  wireHead();
  wire(th);
}

function find(key) {
  const c = B.data.columns;
  return c.mine.find((r) => r.key === key) || c.watching.find((r) => r.key === key)
    || c.waiting.flatMap((g) => g.items).find((r) => r.key === key);
}

function wire(th) {
  th.onclick = (e) => {
    const n = e.target.closest("[data-nudge]");
    if (n) { const g = B.data.columns.waiting[Number(n.dataset.nudge)]; busy(n, () => nudge(g)); return; }
    const b = e.target.closest("[data-bd]");
    const card = e.target.closest(".bd-card");
    if (b && card) { e.stopPropagation(); return act(b.dataset.bd, find(card.dataset.key), b); }
    if (card) { closeBoard(); B.openItem?.(card.dataset.key); }
  };
  th.onkeydown = (e) => {
    const card = e.target.closest?.(".bd-card");
    if (card && e.key === "Enter") { closeBoard(); B.openItem?.(card.dataset.key); }
  };
  th.ondragstart = (e) => {
    const card = e.target.closest(".bd-card"); if (!card) return;
    B.drag = { key: card.dataset.key, from: card.dataset.col };
    e.dataTransfer.setData("text/plain", card.dataset.key);
    e.dataTransfer.effectAllowed = "move";
  };
  th.ondragover = (e) => { const col = e.target.closest("[data-drop]"); if (col && B.drag) { e.preventDefault(); col.classList.add("over"); } };
  th.ondragleave = (e) => { const col = e.target.closest("[data-drop]"); if (col && !col.contains(e.relatedTarget)) col.classList.remove("over"); };
  th.ondrop = (e) => {
    const col = e.target.closest("[data-drop]"); if (!col || !B.drag) return;
    e.preventDefault(); col.classList.remove("over");
    const { key, from } = B.drag; B.drag = null;
    const to = col.dataset.drop;
    if (to === from) return;
    const r = find(key); if (!r) return;
    if (to === "watching") return act("watch", r);
    if (to === "done") return act("done", r);
    if (to === "waiting") return act("waiting", r);
    if (to === "mine" && from === "watching") return act("unwatch", r);
    toast(to === "mine" ? "My court is set by who wrote last; it moves there when they reply." : "That move is not available.");
  };
}

async function act(what, r, btn = null) {
  if (!r) return;
  if (what === "ask") return openAsk({ type: "item", key: r.key }, r.subject || "");
  if (what === "unwatch") {
    await api("/api/board/unwatch", { method: "POST", body: { key: r.key } });
    toast("Back in its column.", "ok"); return load();
  }
  if (what === "watch") return watchCard(r);
  if (what === "waiting") return window.dispatchEvent(new CustomEvent("ultra:stage-answer", { detail: { action: "task", text: `Follow up: ${r.subject || ""}${r.from ? ` (${r.from})` : ""}`, key: /^(g|k|s)-/.test(r.key) ? r.key : "", task: "", title: r.subject || "" } }));
  if (what === "done") return doneCard(r);
}

// ---------------------------------------------------------------- watch dialog
function watchCard(r) {
  const dlg = $("#review");
  const day = (n) => { const d = new Date(); d.setDate(d.getDate() + n); return d.toISOString().slice(0, 10); };
  dlg.innerHTML = `<div class="rv-card" role="dialog" aria-modal="true" aria-labelledby="bw-h">
    <h3 id="bw-h">Watch</h3>
    <div class="dim small-t">${esc(r.subject || "")}</div>
    <p class="small-t">Parks it in Watching on this laptop only. Nothing is sent or changed anywhere else. On the date, it goes back to its column.</p>
    <div class="row-g"><label for="bw-until" class="dim small-t">Until</label><input type="date" id="bw-until" min="${day(1)}">
      <button class="btn tiny ghost" data-d="1">Tomorrow</button><button class="btn tiny ghost" data-d="3">3 days</button><button class="btn tiny ghost" data-d="7">1 week</button><button class="btn tiny ghost" data-d="">No date</button></div>
    <input id="bw-note" class="rinput" placeholder="Note (optional): what you are waiting for" maxlength="200">
    <div class="rv-acts"><span class="grow"></span><button class="btn small" id="bw-x">Cancel</button><button class="btn small primary" id="bw-go">Watch</button></div></div>`;
  dlg.hidden = false;
  const close = () => { dlg.hidden = true; dlg.innerHTML = ""; };
  dlg.querySelectorAll("[data-d]").forEach((b) => b.onclick = () => { $("#bw-until").value = b.dataset.d ? day(Number(b.dataset.d)) : ""; });
  $("#bw-x").onclick = close;
  dlg.onclick = (e) => { if (e.target === dlg) close(); };
  $("#bw-go").onclick = (e) => busy(e.currentTarget, async () => {
    try { await api("/api/board/watch", { method: "POST", body: { key: r.key, until: $("#bw-until").value, note: $("#bw-note").value } }); }
    catch (err) { toast(err.message, "err"); return; }
    close(); toast("Watching (local only).", "ok"); load();
  });
}

// ---------------------------------------------------------------- done card
function doneCard(r) {
  const isTask = r.key.startsWith("t-");
  const isSlack = r.source === "slack";
  const canArchive = !isTask && (r.key.startsWith("g-") || (r.threads || []).length);
  const dlg = $("#review");
  dlg.innerHTML = `<div class="rv-card" role="dialog" aria-modal="true" aria-labelledby="bd-h">
    <h3 id="bd-h">Done</h3>
    <div class="dim small-t">${esc(r.subject || "")}</div>
    <p class="small-t">Tick what should happen. Nothing runs until you press Do it.</p>
    <div class="bd-checks">
      ${isTask ? `<label class="cx-chk"><input type="checkbox" id="bd-complete" checked> Mark the task DONE in the ledger</label>` : ""}
      ${canArchive ? `<label class="cx-chk"><input type="checkbox" id="bd-archive" checked> Archive it (leaves the inbox; Undo in the toast)</label>` : ""}
      ${isSlack ? `<label class="cx-chk"><input type="checkbox" id="bd-slack" checked> Mark done here (nothing is sent to Slack)</label>` : ""}
      <label class="cx-chk"><input type="checkbox" id="bd-log" ${isTask ? "" : "checked"}> Log it in the ledger (opens the log card to review)</label>
      ${r.watch ? `<label class="cx-chk"><input type="checkbox" id="bd-unwatch" checked> Stop watching</label>` : ""}
    </div>
    <div class="rv-acts"><span class="grow"></span><button class="btn small" id="bd-x">Cancel</button><button class="btn small primary" id="bd-go">Do it</button></div></div>`;
  dlg.hidden = false;
  const close = () => { dlg.hidden = true; dlg.innerHTML = ""; };
  $("#bd-x").onclick = close;
  dlg.onclick = (e) => { if (e.target === dlg) close(); };
  $("#bd-go").onclick = (e) => busy(e.currentTarget, async () => {
    const want = (id) => !!$(`#${id}`)?.checked;
    const done = [];
    try {
      if (want("bd-complete")) {
        if (!confirm(`Mark this task DONE in the ledger?\n\n${r.subject || ""}`)) return;
        const x = await api("/api/task/action", { method: "POST", body: { id: r.key.slice(2), action: "complete" } });
        if (x.ok === false) { toast(`The ledger did not confirm: ${x.output_tail || "unknown"}`, "err"); return; }
        done.push("task completed");
      }
      if (want("bd-archive")) {
        const tids = r.key.startsWith("g-") ? [r.key.slice(2)] : r.threads || [];
        await api("/api/mail/archive", { method: "POST", body: { threads: tids } });
        done.push("archived");
      }
      if (want("bd-slack")) { await api("/api/slack/done", { method: "POST", body: { key: r.key } }); done.push("marked done"); }
      if (want("bd-unwatch")) await api("/api/board/unwatch", { method: "POST", body: { key: r.key } });
    } catch (err) { toast(err.message, "err"); return; }
    const log = want("bd-log");
    close();
    if (done.length) toast(`Done: ${done.join(", ")}.`, "ok");
    if (log) {
      if (/^(g|k|s)-/.test(r.key)) window.dispatchEvent(new CustomEvent("ultra:stage-log", { detail: { key: r.key } }));
      else window.dispatchEvent(new CustomEvent("ultra:stage-answer", { detail: { action: "log", text: `Done: ${r.subject || ""}`, key: "", task: isTask ? r.key.slice(2) : "", title: r.subject || "" } }));
    }
    setTimeout(load, 1500);
  });
}

// ---------------------------------------------------------------- nudge
async function nudge(g) {
  let r;
  try { r = await api("/api/board/nudge", { method: "POST", body: { addr: g.addr } }); }
  catch (e) { toast(e.message, "err"); return; }
  toast(`Nudge draft for ${g.who} (${r.threads.length} thread${r.threads.length === 1 ? "" : "s"}). Edit it, then approve twice to send.`, "ok");
  closeBoard();
  window.dispatchEvent(new CustomEvent("ultra:open-draft", { detail: { id: r.draft.id } }));
}
