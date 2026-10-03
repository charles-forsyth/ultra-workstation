// Keyboard shortcuts (v0.16): one table drives the help panel (?) and the docs test.
// Each entry: [key, label, group, scope]. scope "item" = needs an opened item.

export const KEYS = [
  ["j / k", "Next / previous item in the stream", "Move", "any"],
  ["Enter", "Open the selected item", "Move", "any"],
  ["/", "Search mail", "Move", "any"],
  ["Ctrl+K", "Command palette (every action, people, open any item)", "Move", "any"],
  ["Esc", "Close the open view or dialog", "Move", "any"],
  ["r", "Reply (email, Slack)", "Item", "item"],
  ["a", "Reply all", "Item", "item"],
  ["f", "Forward", "Item", "item"],
  ["s", "AI summary", "Item", "item"],
  ["h", "Ask Hermes about it", "Item", "item"],
  ["e", "Archive (mail) / Complete (task) / Mark done (Slack)", "Item", "item"],
  ["b", "Add to the bucket", "Item", "item"],
  ["l", "Log it in the ledger", "Item", "item"],
  ["t", "Make a ledger task", "Item", "item"],
  ["c", "Compose a new email", "Write", "any"],
  ["o", "Board: my court, waiting on, watching, done", "Views", "any"],
  ["v", "Graph: your ledger neighborhood", "Views", "any"],
  ["d", "Day: check-in plan and end-of-day report", "Views", "any"],
  ["g", "Today: calendar", "Views", "any"],
  ["n", "Ledger tab", "Views", "any"],
  ["m", "Stream: Mine", "Stream", "any"],
  ["w", "Stream: Waiting", "Stream", "any"],
  ["T", "Stream: Tasks", "Stream", "any"],
  ["R", "Refresh mail and Slack", "Stream", "any"],
  ["L", "Switch layout: calm / classic (this browser)", "Views", "any"],
  ["W", "Switch workspace (Work / Personal)", "Views", "any"],
  ["?", "This help", "Stream", "any"],
  ["Ctrl+Enter", "Ask (in the Ask Hermes box)", "Write", "any"],
];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

export function helpOpen() { const d = document.querySelector("#keyhelp"); return !!d && !d.hidden; }

export function toggleHelp() {
  const dlg = document.querySelector("#keyhelp");
  if (!dlg) return;
  if (!dlg.hidden) { dlg.hidden = true; return; }
  const groups = [...new Set(KEYS.map((k) => k[2]))];
  dlg.innerHTML = `<div class="rv-card kh-card" role="dialog" aria-modal="true" aria-labelledby="kh-h">
    <div class="lc-head"><h3 id="kh-h">Keyboard</h3><span class="dim small-t">Keys work when you are not typing in a box. Item keys need an item open.</span><span class="grow"></span><button class="btn tiny ghost" id="kh-x">Close</button></div>
    <div class="kh-grid">${groups.map((g) => `<section><div class="label">${esc(g)}</div><dl>${KEYS.filter((k) => k[2] === g).map((k) => `<dt><span class="kbd">${esc(k[0])}</span></dt><dd>${esc(k[1])}</dd>`).join("")}</dl></section>`).join("")}</div>
    <div class="kh-touch dim small-t">On a phone or tablet these work with a keyboard attached; every action is also a button.</div>
    <div class="dim small-t">Nothing is ever sent by a key alone: email and Slack need two approvals; ledger writes open a card first.</div>
  </div>`;
  dlg.hidden = false;
  dlg.querySelector("#kh-x").onclick = () => { dlg.hidden = true; };
  dlg.onclick = (e) => { if (e.target === dlg) dlg.hidden = true; };
  dlg.querySelector("#kh-x").focus();
}
