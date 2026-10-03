// Action table (SPEC 7.13, v1.5): one row per action, so hiding never loses anything.
//
// The calm layout keeps the thread, task and row buttons the views already render (and
// their handlers), then arranges them by this table: a few stay on the toolbar, the
// rest move into the Reply, AI and "..." menus. Menus are built from the real buttons
// when they open, so a disabled or relabelled button ("Mark done" for Slack) shows the
// same way in the menu, and keys that click a button by its data-a still work.
// The palette lists every item action from the same table. tests/test_v150_calm.py
// checks that every button has a row here, every item key maps to one, and the toolbar
// stays inside its budget.
//
// place: "primary" stays on the toolbar; "reply" is the Reply split button (first
// visible one is the button, the rest its menu); "ai" the AI menu; "more" the "..." menu.

export const ACTIONS = [
  // email / Slack / ticket thread (data-a)
  { id: "reply_all", label: "Reply all", place: "reply", key: "a" },
  { id: "reply", label: "Reply", place: "reply", key: "r" },
  { id: "forward", label: "Forward", place: "reply", key: "f" },
  { id: "archive", label: "Archive", place: "primary", key: "e" },
  { id: "summary", label: "Summarize", place: "ai", key: "s" },
  { id: "studio", label: "Draft reply (Draft Studio)", place: "ai", virtual: "#st-go" },
  { id: "ask", label: "Ask Hermes", place: "ai", key: "h" },
  { id: "listen", label: "Read aloud", place: "ai" },
  { id: "aiaudio", label: "AI audio", place: "ai" },
  { id: "bucket", label: "Add to bucket", place: "more", key: "b" },
  { id: "log", label: "Log in the ledger", place: "more", key: "l" },
  { id: "task", label: "Make a ledger task", place: "more", key: "t" },
  { id: "block", label: "Block time", place: "more" },
  { id: "labels", label: "Labels", place: "more" },
  { id: "copy", label: "Copy", place: "more" },
  { id: "export", label: "Export", place: "more" },
  { id: "permalink", label: "Open in Slack", place: "more" },
];

// ledger task view (data-t)
export const TASK_ACTIONS = [
  { id: "complete", label: "Complete", place: "primary", key: "e" },
  { id: "log", label: "Log update", place: "primary", key: "l" },
  { id: "reopen", label: "Reopen", place: "more" },
  { id: "start", label: "Start", place: "more" },
  { id: "block", label: "Blocked", place: "more" },
  { id: "bucket", label: "Add to bucket", place: "more", key: "b" },
  { id: "blocktime", label: "Block time", place: "more" },
  { id: "copy", label: "Copy", place: "more" },
  { id: "ask", label: "Ask Hermes", place: "ai", key: "h" },
  { id: "email", label: "Draft email (Draft Studio)", place: "ai" },
];

// stream row actions (data-ra): Archive/Done stays on the row, the rest in its "..." menu
export const ROW_ACTIONS = [
  { id: "archive", label: "Archive / Done", place: "primary" },
  { id: "bucket", label: "Add to bucket", place: "more" },
  { id: "log", label: "Log in the ledger", place: "more" },
  { id: "task", label: "Make a ledger task", place: "more" },
  { id: "block", label: "Block time", place: "more" },
  { id: "ask", label: "Ask Hermes", place: "more" },
];

export function isCalm() { return document.body.classList.contains("calm"); }

// ---------------------------------------------------------------- pop-up menu
let MENU = null;
export function menuOpen() { return !!MENU; }
export function closeMenu() {
  if (!MENU) return;
  const { el, anchor } = MENU;
  MENU = null;
  el.remove();
  document.removeEventListener("mousedown", outside, true);
  anchor?.setAttribute("aria-expanded", "false");
}
function outside(e) { if (MENU && !MENU.el.contains(e.target) && e.target !== MENU.anchor) closeMenu(); }

// items: { label, key?, run, disabled?, cls?, title? } | { info, cls? } | { sep: true }
export function popMenu(anchor, items, { label = "Menu" } = {}) {
  const again = MENU && MENU.anchor === anchor;
  closeMenu();
  if (again) return;  // a second click on the same button closes it
  const m = document.createElement("div");
  m.className = "pop"; m.setAttribute("role", "menu"); m.setAttribute("aria-label", label);
  for (const it of items) {
    if (it.sep) { const s = document.createElement("div"); s.className = "pop-sep"; m.appendChild(s); continue; }
    if (it.info !== undefined) {
      const d = document.createElement("div");
      d.className = `pop-info ${it.cls || ""}`; d.textContent = it.info;
      if (it.title) d.title = it.title;
      m.appendChild(d); continue;
    }
    const b = document.createElement("button");
    b.type = "button"; b.className = `pop-item ${it.cls || ""}`; b.setAttribute("role", "menuitem");
    const t = document.createElement("span"); t.textContent = it.label; b.appendChild(t);
    if (it.key) { const k = document.createElement("span"); k.className = "kbd"; k.textContent = it.key; b.appendChild(k); }
    if (it.title) b.title = it.title;
    b.disabled = !!it.disabled;
    b.onclick = (e) => { e.stopPropagation(); closeMenu(); it.run?.(); };
    m.appendChild(b);
  }
  m.addEventListener("keydown", (e) => {
    const list = [...m.querySelectorAll(".pop-item:not(:disabled)")];
    const i = list.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); list[(i + 1) % list.length]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); list[(i - 1 + list.length) % list.length]?.focus(); }
    else if (e.key === "Escape" || e.key === "Tab") { e.preventDefault(); closeMenu(); anchor?.focus(); }
    e.stopPropagation();
  });
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect();
  const w = m.offsetWidth, h = m.offsetHeight;
  m.style.left = `${Math.max(8, Math.min(window.innerWidth - w - 8, r.left))}px`;
  m.style.top = `${r.bottom + 4 + h > window.innerHeight - 8 ? Math.max(8, r.top - h - 4) : r.bottom + 4}px`;
  MENU = { el: m, anchor };
  anchor.setAttribute("aria-expanded", "true");
  document.addEventListener("mousedown", outside, true);
  m.querySelector(".pop-item:not(:disabled)")?.focus();
}

// ---------------------------------------------------------------- toolbar arrangement
// Which table row an element in a toolbar is.
function actionId(el, attr) {
  if (el.matches?.("[data-ask]")) return "ask";
  if (el.matches?.("a.btn[href]")) return "permalink";
  return el.dataset?.[attr] || "";
}

function menuButton(text, cls, title) {
  const b = document.createElement("button");
  b.type = "button"; b.className = `btn small ${cls}`; b.textContent = text; b.title = title;
  b.setAttribute("aria-haspopup", "menu"); b.setAttribute("aria-expanded", "false");
  return b;
}

// Menu entries from held elements, read when the menu opens (labels and disabled state
// are current). Hidden elements (not offered for this item) are left out.
function entries(els, table, attr) {
  return els.filter((el) => !el.hidden).map((el) => {
    const row = table.find((a) => a.id === actionId(el, attr)) || {};
    return { label: el.textContent.trim() || row.label, key: row.key, disabled: el.disabled, title: el.title, cls: el.classList.contains("ai") ? "ai" : "", run: () => el.click() };
  });
}

// Arrange a rendered toolbar for the calm layout. attr: "a" (thread) or "t" (task).
export function arrange(bar, table, attr) {
  if (!bar || !isCalm() || bar.dataset.arranged) return;
  bar.dataset.arranged = "1";
  bar.classList.add("calm-acts");
  const held = document.createElement("div");
  held.className = "acts-held"; held.hidden = true;
  const groups = { reply: [], ai: [], more: [] };
  for (const el of [...bar.children]) {
    if (!el.matches("button, a.btn")) continue;  // selects and date fields stay where they are
    const row = table.find((a) => a.id === actionId(el, attr));
    const place = row?.place || "more";
    if (place === "primary") continue;
    groups[place].push(el);
  }
  // Reply split button: the first visible reply action (table order, so Reply all) is the
  // button, the rest its menu.
  const order = (el) => table.findIndex((a) => a.id === actionId(el, attr));
  groups.reply.sort((x, y) => order(x) - order(y));
  const replies = groups.reply.filter((el) => !el.hidden);
  if (replies.length) {
    const main = replies[0];
    main.classList.add("primary");
    const wrap = document.createElement("span");
    wrap.className = "split";
    bar.insertBefore(wrap, bar.firstChild);
    wrap.appendChild(main);
    const rest = groups.reply.filter((el) => el !== main);
    for (const el of rest) held.appendChild(el);
    if (rest.some((el) => !el.hidden)) {
      const caret = menuButton("\u25BE", "split-caret", "More ways to reply");
      caret.setAttribute("aria-label", "More ways to reply");
      caret.onclick = () => popMenu(caret, entries(rest, table, attr), { label: "Reply" });
      wrap.appendChild(caret);
    }
  } else for (const el of groups.reply) held.appendChild(el);
  for (const el of [...groups.ai, ...groups.more]) held.appendChild(el);
  const virtual = table.filter((a) => a.virtual);
  if (groups.ai.length || virtual.length) {
    const ai = menuButton("AI \u25BE", "ai", "Summarize, draft, ask, listen");
    ai.onclick = () => {
      const extra = virtual.map((a) => ({ a, el: document.querySelector(a.virtual) })).filter((x) => x.el)
        .map(({ a, el }) => ({ label: a.label, key: a.key, cls: "ai", run: () => el.click() }));
      const items = [...extra, ...entries(groups.ai, table, attr)];
      popMenu(ai, items.length ? items : [{ info: "Nothing here for this item." }], { label: "AI" });
    };
    bar.appendChild(ai);
  }
  if (groups.more.length) {
    const more = menuButton("\u22EF", "more-btn", "More actions");
    more.setAttribute("aria-label", "More actions");
    more.onclick = () => popMenu(more, entries(groups.more, table, attr), { label: "More actions" });
    bar.appendChild(more);
  }
  bar.appendChild(held);
}
