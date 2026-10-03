// Mail extras (SPEC 8.1, v0.11): search, labels, attachments, highlights and notes,
// thread export. Nothing here sends mail. Label changes are journaled with Undo;
// system labels are never offered. Highlights and notes stay on this machine.

import { api, esc, toast, busy, copyText, WS } from "./app.js";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const X = { labels: null, notes: [], thread: null, key: null };

// ---------------------------------------------------------------- search
export function initSearch({ onResults, onClear }) {
  const head = $("#left .panel-head");
  const bar = document.createElement("div");
  bar.className = "msearch";
  bar.innerHTML = `<input id="ms-q" type="search" placeholder="Search mail" title="Gmail syntax works: from: subject: has:attachment" autocomplete="off" maxlength="300">
    <select id="ms-scope" title="Where to search"><option value="inbox">Inbox</option><option value="all">All mail</option></select>
    <button class="btn tiny ghost" id="ms-saved" title="Saved searches">&#9733;</button>`;
  head.after(bar);
  const menu = document.createElement("div");
  menu.className = "ms-menu"; menu.id = "ms-menu"; menu.hidden = true;
  bar.after(menu);
  const run = async () => {
    const q = $("#ms-q").value.trim();
    if (!q) { bar.classList.remove("active"); onClear(); return; }
    try {
      const r = await api(`/api/mail/search?q=${encodeURIComponent(q)}&scope=${$("#ms-scope").value}`);
      bar.classList.add("active");
      onResults(r);
    } catch (e) { toast(e.message, "err"); }
  };
  $("#ms-q").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); run(); }
    if (e.key === "Escape") { $("#ms-q").value = ""; bar.classList.remove("active"); onClear(); $("#ms-q").blur(); }
  });
  $("#ms-q").addEventListener("search", () => { if (!$("#ms-q").value) onClear(); });
  $("#ms-scope").onchange = () => { if ($("#ms-q").value.trim()) run(); };
  $("#ms-saved").onclick = () => savedMenu(run);
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !e.target.matches("input, textarea, select")) { e.preventDefault(); $("#ms-q").focus(); }
  });
}

async function savedMenu(run) {
  const m = $("#ms-menu");
  if (!m.hidden) { m.hidden = true; return; }
  const r = await api("/api/mail/searches");
  const q = $("#ms-q").value.trim();
  m.innerHTML = `${r.searches.map((s) => `<div class="ms-row"><button class="btn tiny ghost" data-q="${esc(s.query)}">${esc(s.name)}</button><span class="dim small-t mono">${esc(s.query)}</span><span class="grow"></span><button class="btn tiny ghost" data-del="${s.id}" title="Forget this search">x</button></div>`).join("") || `<div class="dim small-t">No saved searches yet.</div>`}
    ${q ? `<div class="ms-row"><button class="btn tiny" id="ms-save">Save "${esc(q.slice(0, 40))}"</button></div>` : ""}`;
  m.hidden = false;
  m.onclick = async (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.q) { $("#ms-q").value = b.dataset.q; m.hidden = true; run(); }
    else if (b.dataset.del) { await api(`/api/mail/searches/${b.dataset.del}/delete`, { method: "POST" }); m.hidden = true; savedMenu(run); }
    else if (b.id === "ms-save") {
      const name = prompt("Name for this search:", q.slice(0, 40)); if (!name) return;
      await api("/api/mail/searches", { method: "POST", body: { name, query: q } }); toast("Search saved.", "ok"); m.hidden = true;
    }
  };
}

// ---------------------------------------------------------------- labels
async function labels(fresh = false) {
  if (!X.labels || fresh) X.labels = (await api(`/api/mail/labels${fresh ? "?fresh=1" : ""}`)).labels;
  return X.labels;
}

export async function labelMenu(anchor, it, onDone) {
  const tids = it.key.startsWith("g-") ? [it.key.slice(2)] : (it.threads || []);
  if (!tids.length) return;
  const all = (await labels()).filter((l) => !l.protected);
  const pop = document.createElement("div");
  pop.className = "lab-pop card";
  const have = new Set(it.labels || []);
  pop.innerHTML = `<div class="label">Labels <span class="dim small-t">tick to add, untick to remove</span></div>
    <input class="rinput" id="lab-f" placeholder="Filter or new label name" maxlength="60">
    <div class="lab-list">${all.map((l) => `<label class="chk lab-row" data-name="${esc(l.name.toLowerCase())}"><input type="checkbox" value="${esc(l.id)}" ${have.has(l.id) ? "checked" : ""}> ${esc(l.name)}</label>`).join("") || `<div class="dim small-t">No labels yet.</div>`}</div>
    <div class="row-g"><button class="btn tiny primary" id="lab-apply">Apply</button><button class="btn tiny" id="lab-new" hidden>Create label</button><button class="btn tiny ghost" id="lab-x">Close</button></div>`;
  document.body.appendChild(pop);
  const r = anchor.getBoundingClientRect();
  pop.style.left = `${Math.max(8, Math.min(window.innerWidth - 300, r.left))}px`;
  pop.style.top = `${Math.min(window.innerHeight - 320, r.bottom + 4)}px`;
  const close = () => pop.remove();
  $("#lab-x", pop).onclick = close;
  $("#lab-f", pop).oninput = (e) => {
    const v = e.target.value.trim().toLowerCase();
    $$(".lab-row", pop).forEach((row) => { row.hidden = v && !row.dataset.name.includes(v); });
    $("#lab-new", pop).hidden = !v || all.some((l) => l.name.toLowerCase() === v);
  };
  $("#lab-new", pop).onclick = (e) => busy(e.target, async () => {
    const name = $("#lab-f", pop).value.trim();
    if (!confirm(`Create the Gmail label "${name}"?`)) return;
    await api("/api/mail/labels", { method: "POST", body: { name } });
    X.labels = null; close(); labelMenu(anchor, it, onDone);
  });
  $("#lab-apply", pop).onclick = (e) => busy(e.target, async () => {
    const now = new Set($$("input[type=checkbox]", pop).filter((c) => c.checked).map((c) => c.value));
    const add = [...now].filter((x) => !have.has(x)), remove = [...have].filter((x) => !now.has(x) && all.some((l) => l.id === x));
    if (!add.length && !remove.length) { close(); return; }
    const names = (ids) => ids.map((id) => all.find((l) => l.id === id)?.name || id).join(", ");
    const res = await api("/api/mail/labels/apply", { method: "POST", body: { threads: tids, add, remove } });
    it.labels = [...now];
    close();
    toast(`${add.length ? `Added ${names(add)}. ` : ""}${remove.length ? `Removed ${names(remove)}.` : ""}${res.ok === false ? " (check in Gmail: the read-back differed)" : ""}`, res.ok === false ? "err" : "ok", {
      label: "Undo",
      fn: async () => { await api("/api/mail/labels/apply", { method: "POST", body: { threads: tids, add: remove, remove: add } }); it.labels = [...have]; toast("Labels put back.", "ok"); onDone?.(); },
    });
    onDone?.();
  });
  $("#lab-f", pop).focus();
}

// ---------------------------------------------------------------- attachments
export function attHtml(m) {
  if (!(m.attachments || []).length) return "";
  return `<div class="atts">${m.attachments.map((a) => {
    const kb = Math.max(1, Math.round((a.size || 0) / 1024));
    const prev = /^(text\/plain|text\/csv|image\/(png|jpeg|gif)|application\/pdf)$/.test(a.mime || "");
    return `<span class="att" title="${esc(a.mime)}"><span class="att-n">${esc(a.name)}</span> <span class="dim">${kb} KB</span>
      ${a.id ? `${prev ? `<button class="btn tiny ghost" data-att="preview" data-m="${esc(m.id)}" data-id="${esc(a.id)}" data-name="${esc(a.name)}" data-mime="${esc(a.mime)}">Preview</button>` : ""}
      <a class="btn tiny ghost" href="${WS.url(`/api/mail/attachment/${encodeURIComponent(m.id)}/${encodeURIComponent(a.id)}`)}" download="${esc(a.name)}">Download</a>
      <button class="btn tiny ghost" data-att="save" data-m="${esc(m.id)}" data-id="${esc(a.id)}" title="Save to Ultra's private attachments folder">Save</button>
      <button class="btn tiny ghost" data-att="attach" data-m="${esc(m.id)}" data-id="${esc(a.id)}" data-name="${esc(a.name)}" title="Attach to the open draft (for a forward)">Attach to draft</button>` : ""}</span>`;
  }).join("")}</div>`;
}

export function wireAttachments(th, getDraftId) {
  th.addEventListener("click", (e) => {
    const b = e.target.closest("[data-att]"); if (!b) return;
    const { m, id } = b.dataset;
    if (b.dataset.att === "preview") return preview(m, id, b.dataset.name, b.dataset.mime);
    if (b.dataset.att === "save") {
      return busy(b, async () => { const r = await api(`/api/mail/attachment/${m}/${id}/save`, { method: "POST" }); toast(`Saved: ${r.path}`, "ok"); });
    }
    if (b.dataset.att === "attach") {
      return busy(b, async () => {
        const did = getDraftId();
        if (!did) { toast("Open a reply or forward first, then attach.", "err"); return; }
        await api(`/api/drafts/${did}/attach-from`, { method: "POST", body: { message: m, attachment: id } });
        toast(`Attached ${b.dataset.name}. Approval, if any, was reset.`, "ok");
        window.dispatchEvent(new CustomEvent("ultra:draft-files"));
      });
    }
  });
}

function preview(m, id, name, mime) {
  const dlg = $("#review");
  const url = WS.url(`/api/mail/attachment/${encodeURIComponent(m)}/${encodeURIComponent(id)}?inline=1`);
  const img = /^image\//.test(mime);
  dlg.innerHTML = `<div class="rv-card att-prev" role="dialog" aria-modal="true" aria-label="Preview ${esc(name)}">
    <h3>${esc(name)} <span class="dim small-t">${esc(mime)} &middot; preview is sandboxed</span></h3>
    ${img ? `<img src="${url}" alt="${esc(name)}">` : `<iframe src="${url}" sandbox title="${esc(name)}"></iframe>`}
    <div class="rv-acts"><span class="grow"></span><a class="btn" href="${url.replace("?inline=1", "")}" download="${esc(name)}">Download</a><button class="btn" id="ap-x" autofocus>Close</button></div></div>`;
  dlg.hidden = false;
  $("#ap-x").onclick = () => { dlg.hidden = true; dlg.innerHTML = ""; };
}

// ---------------------------------------------------------------- highlights and notes
const HL_COLORS = ["amber", "cyan", "magenta", "green"];

export async function loadNotes(th, key, messages) {
  X.key = key; X.thread = messages;
  if (!/^(g|k|s)-/.test(key)) { X.notes = []; return; }
  try { X.notes = (await api(`/api/notes/${encodeURIComponent(key)}`)).notes; } catch { X.notes = []; }
  if (X.key !== key) return;
  paintNotes(th);
}

function paintNotes(th) {
  $$("mark.hl", th).forEach((mk) => mk.replaceWith(...mk.childNodes));
  $$(".msg .body", th).forEach((b) => b.normalize());
  for (const n of X.notes) {
    const bodies = $$(`.msg[data-mid="${CSS.escape(n.message_id)}"] > .body`, th);
    for (const b of bodies.length ? bodies : $$(".msg > .body", th)) {
      if (wrapText(b, n)) break;
    }
  }
  const list = $("#notes-list", th);
  if (list) {
    list.innerHTML = X.notes.length ? `<div class="label">Highlights and notes (${X.notes.length}) <span class="dim small-t">only on this machine</span></div>
      ${X.notes.map((n) => `<div class="note-row hl-${esc(n.color)}"><span class="q">"${esc(n.quote.slice(0, 140))}${n.quote.length > 140 ? "..." : ""}"</span>${n.note ? `<span class="nt">${esc(n.note)}</span>` : ""}<span class="grow"></span><button class="btn tiny ghost" data-note="${n.id}">Edit</button></div>`).join("")}` : "";
    list.onclick = (e) => { const b = e.target.closest("[data-note]"); if (b) editNote(th, Number(b.dataset.note), b); };
  }
}

function wrapText(root, n) {
  // find the quote in one text node (highlights inside a single paragraph run)
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const i = node.data.indexOf(n.quote);
    if (i < 0) continue;
    const r = document.createRange();
    r.setStart(node, i); r.setEnd(node, i + n.quote.length);
    const mk = document.createElement("mark");
    mk.className = `hl hl-${n.color}`; mk.dataset.note = n.id;
    if (n.note) mk.title = n.note;
    r.surroundContents(mk);
    return true;
  }
  return false;
}

export async function addHighlight(th, text, color = "amber", withNote = false) {
  const sel = window.getSelection();
  const msg = sel?.anchorNode?.parentElement?.closest(".msg");
  const quote = text.trim().slice(0, 2000);
  if (!quote) return;
  if (quote.includes("\n")) { toast("Highlight within one paragraph.", "err"); return; }
  const note = withNote ? (prompt("Private note (only on this machine):", "") ?? null) : "";
  if (note === null) return;
  try {
    const n = await api("/api/notes", { method: "POST", body: { thread_key: X.key, message_id: msg?.dataset.mid || "", quote, color, note } });
    X.notes.push(n); paintNotes(th);
  } catch (e) { toast(e.message, "err"); }
}

function editNote(th, id, anchor) {
  const n = X.notes.find((x) => x.id === id); if (!n) return;
  const pop = document.createElement("div");
  pop.className = "lab-pop card";
  pop.innerHTML = `<div class="label">Highlight</div><div class="dim small-t">"${esc(n.quote.slice(0, 200))}"</div>
    <div class="row-g">${HL_COLORS.map((c) => `<button class="btn tiny hl-sw hl-${c} ${c === n.color ? "on" : ""}" data-c="${c}" title="${c}">&nbsp;</button>`).join("")}</div>
    <textarea id="nt-t" rows="3" maxlength="2000" placeholder="Private note">${esc(n.note)}</textarea>
    <div class="row-g"><button class="btn tiny primary" id="nt-s">Save</button><button class="btn tiny danger ghost" id="nt-d">Remove</button><button class="btn tiny ghost" id="nt-x">Close</button></div>`;
  document.body.appendChild(pop);
  const r = anchor.getBoundingClientRect();
  pop.style.left = `${Math.max(8, Math.min(window.innerWidth - 320, r.left))}px`;
  pop.style.top = `${Math.min(window.innerHeight - 260, r.bottom + 4)}px`;
  let color = n.color;
  $$("[data-c]", pop).forEach((b) => b.onclick = () => { color = b.dataset.c; $$("[data-c]", pop).forEach((x) => x.classList.toggle("on", x === b)); });
  $("#nt-x", pop).onclick = () => pop.remove();
  $("#nt-s", pop).onclick = async () => {
    const u = await api(`/api/notes/${id}`, { method: "POST", body: { color, note: $("#nt-t", pop).value } });
    Object.assign(n, u); pop.remove(); paintNotes(th);
  };
  $("#nt-d", pop).onclick = async () => {
    await api(`/api/notes/${id}/delete`, { method: "POST" });
    X.notes = X.notes.filter((x) => x.id !== id); pop.remove(); paintNotes(th);
  };
}

export function wireHighlightClicks(th) {
  th.addEventListener("click", (e) => {
    const mk = e.target.closest("mark.hl"); if (!mk || window.getSelection()?.toString()) return;
    editNote(th, Number(mk.dataset.note), mk);
  });
}

// ---------------------------------------------------------------- export
function mdOf(subject, msgs, fmt) {
  const withHl = (text, mid) => {
    let t = text || "";
    for (const n of X.notes.filter((x) => !x.message_id || x.message_id === mid)) {
      if (t.includes(n.quote)) t = t.replace(n.quote, fmt === "md" ? `==${n.quote}==${n.note ? ` [note: ${n.note}]` : ""}` : `${n.quote}${n.note ? ` [note: ${n.note}]` : ""}`);
    }
    return t;
  };
  if (fmt === "md") {
    return `# ${subject}\n\n` + msgs.map((m) => `## ${m.from} - ${m.ts}\n\n${m.to ? `To: ${m.to}${m.cc ? `  \nCc: ${m.cc}` : ""}\n\n` : ""}${withHl(m.body, m.id)}\n${(m.attachments || []).length ? `\nAttachments: ${m.attachments.map((a) => a.name).join(", ")}\n` : ""}`).join("\n");
  }
  return `${subject}\n${"=".repeat(Math.min(78, subject.length))}\n\n` + msgs.map((m) => `From: ${m.from}\nDate: ${m.ts}\n${m.to ? `To: ${m.to}\n` : ""}${m.cc ? `Cc: ${m.cc}\n` : ""}\n${withHl(m.body, m.id)}\n`).join("\n----------\n\n");
}

function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a"); a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

export function exportMenu(anchor, subject, msgs) {
  const pop = document.createElement("div");
  pop.className = "lab-pop card";
  const safe = (subject || "thread").replace(/[^A-Za-z0-9 _-]/g, "").trim().slice(0, 60).replace(/\s+/g, "-") || "thread";
  pop.innerHTML = `<div class="label">Export this conversation</div>
    <button class="btn tiny ghost" data-x="md">Markdown (.md), highlights as ==text==</button>
    <button class="btn tiny ghost" data-x="txt">Plain text (.txt)</button>
    <button class="btn tiny ghost" data-x="json">JSON (.json)</button>
    <button class="btn tiny ghost" data-x="copymd">Copy as Markdown</button>
    <button class="btn tiny ghost" data-x="print">Print / PDF</button>
    <button class="btn tiny ghost" data-x="close">Close</button>`;
  document.body.appendChild(pop);
  const r = anchor.getBoundingClientRect();
  pop.style.left = `${Math.max(8, Math.min(window.innerWidth - 300, r.left))}px`;
  pop.style.top = `${Math.min(window.innerHeight - 260, r.bottom + 4)}px`;
  pop.onclick = (e) => {
    const b = e.target.closest("[data-x]"); if (!b) return;
    const x = b.dataset.x;
    if (x === "md") download(`${safe}.md`, mdOf(subject, msgs, "md"), "text/markdown");
    else if (x === "txt") download(`${safe}.txt`, mdOf(subject, msgs, "txt"), "text/plain");
    else if (x === "json") download(`${safe}.json`, JSON.stringify({ subject, messages: msgs.map((m) => ({ from: m.from, to: m.to, cc: m.cc, date: m.ts, body: m.body, attachments: (m.attachments || []).map((a) => a.name) })), highlights: X.notes.map((n) => ({ quote: n.quote, note: n.note, color: n.color })) }, null, 2), "application/json");
    else if (x === "copymd") copyText(mdOf(subject, msgs, "md"));
    else if (x === "print") { document.body.classList.add("printing"); window.print(); setTimeout(() => document.body.classList.remove("printing"), 500); }
    pop.remove();
  };
}
