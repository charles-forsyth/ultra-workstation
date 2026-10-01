// Ask Hermes (v0.13, SPEC 7.12): ask the operator's own agent about what is on screen.
// Read-only: the server runs Hermes with read tools only, and only when Ask is pressed.
// Answers are text; acting on one goes through Ultra's normal cards and approvals.

import { api, esc, toast, busy, copyText } from "./app.js";
import { renderMd } from "./tools.js";
import { applyHermesDraft } from "./compose.js";

const $ = (s, el = document) => el.querySelector(s);

// One conversation at a time: the target it is about and the Hermes session id.
const A = { status: null, target: null, title: "", session: "", turns: [], polling: false };

export async function initAsk() {
  try { A.status = await api("/api/hermes/status"); } catch { A.status = { enabled: false }; }
  window.addEventListener("ultra:ask", (ev) => openAsk(ev.detail?.target || { type: "none" }, ev.detail?.title || ""));
  // Escape closes the panel wherever focus is (after an answer, focus is often on the page)
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#ask").hidden) { e.preventDefault(); e.stopImmediatePropagation(); close(); }
  }, true);
}

export function askEnabled() { return !!A.status?.enabled; }

// The standard button, for any view. `target` is what the server will rebuild.
export function askButton(cls = "btn small") {
  const off = A.status && !A.status.enabled;
  return `<button class="${cls} ask-btn" data-ask="1" ${off ? `disabled title="Hermes is not available (hermes CLI not found)"` : `title="Ask your Hermes agent about this (read-only; nothing runs until you press Ask)"`}>Ask Hermes</button>`;
}

export function wireAskButton(root, target, title) {
  for (const b of root.querySelectorAll("[data-ask]")) b.onclick = (e) => { e.stopPropagation(); openAsk(target, title); };
}

function sameTarget(a, b) { return JSON.stringify(a || {}) === JSON.stringify(b || {}); }

export async function openAsk(target, title = "") {
  const dlg = $("#ask");
  // a different item starts a new conversation; the same one keeps the thread open
  if (!sameTarget(target, A.target)) { A.target = target; A.title = title; A.session = ""; A.turns = []; A.preview = null; }
  dlg.hidden = false;
  render();
  $("#ask-q")?.focus();
}

function close() { $("#ask").hidden = true; }

async function loadPreview() {
  const want = A.target;
  try {
    const p = await api("/api/hermes/preview", { method: "POST", body: { target: want } });
    if (!sameTarget(want, A.target)) return;  // the operator moved on
    A.preview = p;
    if (!A.title) A.title = p.title;
    paintPreview();
  } catch (e) { const box = $("#ask-ctx"); if (box) box.innerHTML = `<div class="lint error">${esc(e.message)}</div>`; }
}

function paintPreview() {
  const p = A.preview, box = $("#ask-ctx");
  if (!p || !box) return;
  $("#ask-title").textContent = A.title || p.title || "";
  {
    box.innerHTML = `<details><summary>Context sent with the first question: <b>${esc(p.kind)}</b>, ${p.chars.toLocaleString()} characters <span class="dim">(click to read it)</span></summary><pre class="ask-pre">${esc(p.text.slice(0, 60000))}${p.text.length > 60000 ? "\n[... shown up to 60,000 characters; all of it is sent]" : ""}</pre></details>`;
    const full = $("#ask-full");
    if (full) { full.disabled = !p.can_full; full.closest("label").hidden = !p.can_full; }
  }
}

function render() {
  const dlg = $("#ask");
  const st = A.status || {};
  const tools = (st.toolsets || []).join(", ") || "session_search";
  const follow = !!A.session;
  dlg.innerHTML = `<div class="lc-card ask-card" role="dialog" aria-modal="true" aria-labelledby="ask-h">
    <div class="lc-head"><h3 id="ask-h">Ask Hermes</h3><span class="dim small-t ell" id="ask-title">${esc(A.title)}</span><span class="grow"></span>
      ${follow ? `<button class="btn tiny ghost" id="ask-new" title="Forget this conversation and start a new one">New conversation</button>` : ""}
      <button class="btn tiny ghost" id="ask-x">Close</button></div>
    ${A.target?.type === "none" ? `<div class="dim small-t">No item attached: a plain question to your agent.</div>` : `<div id="ask-ctx" class="ask-ctx"><span class="dim small-t">Building the context...</span></div>`}
    <div class="ask-turns" id="ask-turns">${A.turns.map(turnHtml).join("")}</div>
    <textarea id="ask-q" rows="3" placeholder="${follow ? "Follow-up question (same Hermes session)..." : "What do you want to know about this?"}"></textarea>
    <div class="ask-opts">
      ${A.target?.type === "item" ? `<label class="cx-chk" title="Also send the people, labs, history and open tasks the Full tab finds in the ledger (slower)"><input type="checkbox" id="ask-full" ${follow ? "" : "checked"}> Ledger context</label>` : ""}
      ${st.web_ok ? `<label class="cx-chk" title="Let Hermes search and read the web for this question"><input type="checkbox" id="ask-web"> Allow web</label>` : ""}
      ${follow && A.target?.type !== "none" ? `<label class="cx-chk" title="Send the item again (if it changed, or the agent lost track)"><input type="checkbox" id="ask-resend"> Re-send the item</label>` : ""}
      <span class="grow"></span>
      <button class="btn small ai" id="ask-go" ${st.enabled ? "" : "disabled"}>Ask</button>
    </div>
    <div class="dim small-t ask-note">Read-only: Hermes may use ${esc(tools)}${st.web_ok ? " (and the web if ticked)" : ""}. It cannot send, post, log or change anything. Session tag: <span class="mono">${esc(st.source || "ultra")}</span>.${A.session ? ` <span class="nowrap">Session <span class="mono">${esc(A.session)}</span> <button class="btn tiny ghost" id="ask-copy-sid" title="Copy the session id (resume with: hermes --resume ID)">Copy id</button></span>` : ""}</div>
  </div>`;
  $("#ask-x").onclick = close;
  dlg.onclick = (e) => { if (e.target === dlg) close(); };
  dlg.onkeydown = (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $("#ask-go")?.click(); }
  };
  $("#ask-new")?.addEventListener("click", () => { A.session = ""; A.turns = []; render(); });
  $("#ask-copy-sid")?.addEventListener("click", () => copyText(A.session));
  $("#ask-go").onclick = (e) => busy(e.currentTarget, ask);
  wireTurns();
  if (A.target?.type !== "none") { if (A.preview) paintPreview(); else loadPreview(); }
  const t = $("#ask-turns"); if (t) t.scrollTop = t.scrollHeight;
}

function turnHtml(t, i) {
  if (t.role === "q") return `<div class="ask-q"><span class="label">You</span><div class="body">${esc(t.text)}</div></div>`;
  if (t.role === "err") return `<div class="lint error">${esc(t.text)}</div>`;
  if (t.role === "wait") return `<div class="ask-a wait"><span class="badge ai">Hermes</span> <span class="dim small-t" id="ask-wait">Thinking... 0 s</span></div>`;
  return `<div class="ask-a"><div class="aisum-head"><span class="badge ai">Hermes</span><span class="dim mono small-t">${esc(t.seconds)} s${t.tools?.length ? ` &middot; used ${esc([...new Set(t.tools)].join(", "))}` : ""}</span><span class="grow"></span>
      <button class="btn tiny" data-ta="copy" data-i="${i}">Copy</button><button class="btn tiny" data-ta="listen" data-i="${i}">Listen</button></div>
    <div class="md">${renderMd(t.text)}</div>
    <div class="ask-acts" role="group" aria-label="Use this answer">${actsHtml(i)}</div></div>`;
}

// What an answer can become. Every one opens Ultra's normal card or composer; nothing
// is written or sent from here.
function replyKey() {
  const t = A.target || {};
  const k = t.type === "item" ? t.key : t.type === "text" ? t.key : "";
  return /^(g|s)-/.test(k || "") ? k : "";
}
function sourceKey() {
  const t = A.target || {};
  const k = t.type === "item" || t.type === "text" ? t.key || "" : "";
  return /^(g|k|s)-/.test(k) ? k : "";
}
function taskId() {
  const k = A.target?.type === "item" ? A.target.key || "" : "";
  return k.startsWith("t-") ? k.slice(2) : "";
}
function actsHtml(i) {
  const rk = replyKey();
  const reply = rk
    ? `<button class="btn tiny" data-ta="reply" data-i="${i}" title="Put this answer in the ${rk.startsWith("s-") ? "Slack reply" : "reply-all"} draft as a new version. Edit it there; it still needs both approvals.">Use as ${rk.startsWith("s-") ? "Slack reply" : "reply"}</button>`
    : "";
  return `${reply}<button class="btn tiny" data-ta="log" data-i="${i}" title="Open a ledger log card with this answer as the text (you review and commit it)">Log it</button>
    <button class="btn tiny" data-ta="task" data-i="${i}" title="Open a ledger task card from the answer's first line (you review and commit it)">Task from it</button>
    <button class="btn tiny" data-ta="bucket" data-i="${i}" title="Add the answer to the bucket as a snippet">+ Bucket</button>`;
}



function wireTurns() {
  $("#ask-turns").onclick = (e) => {
    const b = e.target.closest("[data-ta]"); if (!b) return;
    const t = A.turns[Number(b.dataset.i)]; if (!t) return;
    const act = b.dataset.ta;
    if (act === "copy") return copyText(t.text);
    if (act === "listen") return window.dispatchEvent(new CustomEvent("ultra:listen", { detail: { text: t.text, title: "Hermes answer" } }));
    if (act === "reply") return busy(b, async () => {
      const k = replyKey(); if (!k) return;
      close();
      // the composer lives in the open thread: open the item first if it is not on screen
      if (!document.querySelector("#thread:not([hidden]) #composer") || window.__ultraOpenKey?.() !== k) {
        const ok = await window.__ultraOpenItem?.(k);
        if (!ok) { toast("Open that conversation first, then use the answer again (it is still in Ask Hermes).", "err"); return; }
      }
      if (await applyHermesDraft(k, t.text)) toast("Answer is in the draft as a new version. Edit, then approve twice to send.", "ok");
    });
    if (act === "log" || act === "task") return busy(b, async () => {
      close();
      window.dispatchEvent(new CustomEvent("ultra:stage-answer", { detail: { action: act, text: t.text, key: sourceKey(), task: taskId(), title: A.title || "Hermes answer" } }));
    });
    if (act === "bucket") return window.dispatchEvent(new CustomEvent("ultra:bucket-snippet", { detail: { text: t.text.slice(0, 2000), key: sourceKey(), title: A.title ? `Hermes on ${A.title}` : "Hermes answer" } }));
  };
}

async function ask() {
  const q = $("#ask-q").value.trim();
  if (q.length < 2) { toast("Write a question first.", "err"); return; }
  const body = {
    target: A.target,
    question: q,
    session: A.session || undefined,
    full: !!$("#ask-full")?.checked,
    web: !!$("#ask-web")?.checked,
    resend: !!$("#ask-resend")?.checked,
  };
  A.turns.push({ role: "q", text: q }, { role: "wait" });
  render();
  let jid;
  try { jid = (await api("/api/hermes/ask", { method: "POST", body })).job; }
  catch (e) { A.turns.pop(); A.turns.push({ role: "err", text: e.message }); render(); $("#ask-q").value = q; return; }
  const t0 = Date.now();
  for (;;) {
    await new Promise((r) => setTimeout(r, 1500));
    const w = $("#ask-wait"); if (w) w.textContent = `Thinking... ${Math.round((Date.now() - t0) / 1000)} s`;
    let j;
    try { j = await api(`/api/hermes/job/${jid}`); } catch (e) { j = { state: "error", error: e.message }; }
    if (j.state === "running") continue;
    A.turns.pop();
    if (j.state === "done") {
      A.session = j.result.session_id || A.session;
      A.turns.push({ role: "a", text: j.result.text, seconds: j.result.seconds, tools: j.result.tools || [] });
    } else A.turns.push({ role: "err", text: `Hermes: ${j.error || "failed"}` });
    if ($("#ask").hidden) toast(j.state === "done" ? "Hermes answered (Ask Hermes panel)." : "Hermes failed; open Ask Hermes to see why.", j.state === "done" ? "ok" : "err",
      { label: "Open", fn: () => { $("#ask").hidden = false; render(); } });
    render();
    $("#ask-q")?.focus();
    return;
  }
}
