// Draft Studio (SPEC 9.6): gather on open, brief, questions, draft with sources, check.
// The server does the work; this file renders the strip and panel and hands the draft
// to the composer as a new AI version (it still needs both approvals).

import { api, esc, toast, busy } from "./app.js";
import { applyStudioDraft, applyTaskDraft } from "./compose.js";

const $ = (s, el = document) => el.querySelector(s);
const ST = { key: null, poll: null, data: null, answers: {}, draft: null, check: null, open: false };
const STAGE_LABEL = { thread: "Thread", history: "History", precedents: "Your past replies", notes: "Notes", sources: "Policy pages", ledger: "Ledger", brief: "Brief" };
const TASK_LABEL = { ...STAGE_LABEL, thread: "Task", history: "Related mail", precedents: "Your past emails" };
const isTask = () => (ST.key || "").startsWith("t-");

// Opening an item no longer starts the gather + brief (operator's call: it spent model
// calls and time on items he only reads or archives). The strip shows a button; the run
// starts only when it is clicked, when "Draft with AI" needs it (the composer starts it
// on demand), or when run=true (e.g. "Email from this task"). A run already made for
// this item is shown as is.
export function studioStart(key, { run = false } = {}) {
  stopPoll();
  ST.key = key; ST.data = null; ST.answers = {}; ST.draft = null; ST.check = null;
  const host = $("#studio");
  if (!host) return;
  if (!key || !(key.startsWith("g-") || key.startsWith("t-"))) { host.hidden = true; return; }
  host.hidden = false;
  if (run) return studioRun(key);
  host.classList.add("idle");  // calm layout hides the idle strip; AI > Draft reply starts it
  host.innerHTML = idleStrip();
  $("#st-go", host)?.addEventListener("click", () => studioRun(key));
  api(`/api/studio/${encodeURIComponent(key)}`)
    .then((r) => { if (ST.key === key && r.state && r.state !== "idle") { ST.data = r; render(); schedule(); } })
    .catch(() => {});
}

function idleStrip() {
  return `<div class="st-strip"><b>Draft Studio</b><span class="dim small-t">Reads the thread, history, your past replies and policy pages, then writes a brief.</span><span class="grow"></span>
    <button class="btn tiny ai" id="st-go" title="Gather context and build a brief (uses AI)">Gather context</button></div>`;
}

function studioRun(key) {
  const host = $("#studio"); if (!host) return;
  host.classList.remove("idle");
  ST.autoOpened = false;
  host.innerHTML = `<div class="st-strip dim small-t">Draft Studio: starting...</div>`;
  api("/api/studio/start", { method: "POST", body: { key } })
    .then((r) => { if (ST.key === key) { ST.data = r; render(); schedule(); } })
    .catch((e) => { if (ST.key === key) host.innerHTML = `<div class="st-strip lint warning">Draft Studio unavailable: ${esc(e.message)}</div>`; });
}

function stopPoll() { clearTimeout(ST.poll); ST.poll = null; }
function schedule() {
  stopPoll();
  if (!ST.data || ST.data.state !== "running") return;
  const key = ST.key;
  ST.poll = setTimeout(async () => {
    try { const r = await api(`/api/studio/${encodeURIComponent(key)}`); if (ST.key === key) { ST.data = r; render(); } }
    catch { /* keep polling */ }
    if (ST.key === key) schedule();
  }, 900);
}

function strip(d) {
  const chips = Object.entries(d.stages || {}).map(([s, st]) => {
    const cls = st === "done" ? "ok" : st === "running" ? "run" : st === "failed" ? "bad" : st === "off" ? "dim" : "wait";
    return `<span class="st-chip ${cls}" title="${esc(st)}">${esc((isTask() ? TASK_LABEL : STAGE_LABEL)[s] || s)}</span>`;
  }).join("");
  const sm = d.summary;
  const facts = !sm ? "" : isTask()
    ? `${sm.history.length} related threads &middot; ${sm.precedents.length} past emails &middot; ${sm.policy.length} policy pages &middot; ${sm.facts.length} house facts`
    : `${sm.messages} msgs &middot; ${sm.history.length} other threads &middot; ${sm.precedents.length} past replies &middot; ${sm.policy.length} policy pages &middot; ${sm.facts.length} house facts`;
  return `<div class="st-strip"><b>Draft Studio</b> ${chips}<span class="grow"></span><span class="dim small-t">${facts}${d.state === "running" ? ` &middot; ${d.seconds}s` : ""}</span>
    <button class="btn tiny ghost" id="st-toggle">${ST.open ? "Hide" : "Open"}</button></div>`;
}

function render() {
  const host = $("#studio"); if (!host || !ST.data) return;
  const d = ST.data;
  if (d.state === "error") {
    host.innerHTML = strip(d) + `<div class="lint warning">${esc(d.error)}</div>`;
    wire(); return;
  }
  const br = d.brief;
  let body = "";
  if (ST.open) {
    body = `<div class="st-panel">${sourcesHtml(d.summary)}${br ? briefHtml(br) : `<div class="dim small-t">Building the brief...</div>`}${ST.draft ? draftHtml() : ""}</div>`;
  }
  host.innerHTML = strip(d) + body;
  if (br && !ST.open && !ST.autoOpened) { ST.autoOpened = true; ST.open = true; render(); return; }
  wire();
}

function sourcesHtml(sm) {
  if (!sm) return "";
  const li = (xs, f) => xs.length ? `<ul class="st-list">${xs.map(f).join("")}</ul>` : `<div class="dim small-t">None found.</div>`;
  const failed = [...(sm.policy_failed || []), ...Object.entries(sm.failures || {}).map(([k, v]) => ({ url: k, error: v }))];
  return `<details class="st-src"><summary>What was read${sm.cached ? " (cached)" : ""}</summary>
    <div class="st-grid">
      <div><div class="label">Your past replies (voice and shape)</div>${li(sm.precedents, (p) => `<li><b>${esc(p.subject)}</b> <span class="dim small-t">to ${esc(p.to)} &middot; ${esc(p.date.slice(0, 10))}</span></li>`)}</div>
      <div><div class="label">${isTask() ? "Mail about this task" : "Other threads with these people"}</div>${li(sm.history.slice(0, 8), (h) => `<li>${esc(h.subject || "(no subject)")} <span class="dim small-t">${esc(h.last.slice(0, 10))} &middot; ${h.n} msgs</span></li>`)}</div>
      <div><div class="label">Policy pages</div>${li(sm.policy, (p) => `<li><a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">${esc(p.title || p.url)}</a></li>`)}</div>
      <div><div class="label">House facts</div>${li(sm.facts, (f) => `<li>${esc(f.text)}</li>`)}
        <button class="btn tiny ghost" id="st-facts">Manage house facts</button></div>
    </div>
    ${sm.notes.length ? `<div class="dim small-t">Notes: ${sm.notes.map(esc).join(", ")}</div>` : ""}
    ${failed.length ? `<div class="lint warning">Could not read: ${failed.map((f) => `${esc(f.url)} (${esc(f.error)})`).join("; ")}</div>` : ""}
  </details>`;
}

function briefHtml(br) {
  const tag = (t) => t ? ` <span class="chip-s" title="${esc((br.tags || {})[t] || t)}">${esc(t)}</span>` : "";
  const qs = (br.unknown || []).map((u, i) => {
    const a = ST.answers[i] ?? "";
    return `<div class="st-q" data-q="${i}">
      <div><b>Q${i + 1}.</b> ${esc(u.question)} ${u.why ? `<span class="dim small-t">(${esc(u.why)})</span>` : ""}</div>
      <div class="st-opts">${(u.options || []).map((o) => `<button class="btn tiny ${a === o ? "on" : ""}" data-opt="${esc(o)}">${esc(o)}</button>`).join("")}
        <input class="st-ans" placeholder="Your answer" value="${esc(a)}">
        <label class="dim small-t"><input type="checkbox" class="st-save" ${ST.saveFact?.[i] ? "checked" : ""}> save as house fact</label></div>
    </div>`;
  }).join("");
  return `<div class="st-brief">
    ${br.mode === "task" ? envelopeHtml(br) : ""}
    <div class="st-row"><span class="label">${br.mode === "task" ? "The email should" : "Asks"}</span><ol>${(br.asks || []).map((a) => `<li>${esc(a)}</li>`).join("")}</ol></div>
    ${br.constraints?.length ? `<div class="st-row"><span class="label">Constraints</span><ul>${br.constraints.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></div>` : ""}
    <div class="st-row"><span class="label">Audience</span> <b>${esc(br.audience)}</b> <span class="dim small-t">${esc(br.audience_note || "")}</span></div>
    ${br.precedent_shape ? `<div class="st-row"><span class="label">Shape</span> ${esc(br.precedent_shape)}</div>` : ""}
    <details class="st-row"><summary><span class="label">Known (${(br.known || []).length})</span></summary><ul>${(br.known || []).map((k) => `<li>${esc(k.fact)}${tag(k.source)}</li>`).join("")}</ul></details>
    ${br.need_from_sender?.length ? `<div class="st-row"><span class="label">Ask them for</span><ul>${br.need_from_sender.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
    ${br.risks?.length ? `<div class="st-row"><span class="label">Flag</span><ul>${br.risks.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>` : ""}
    ${qs ? `<div class="st-row"><span class="label">Questions before drafting</span><div class="dim small-t">Anything you leave blank is written as "I'll confirm and follow up", never guessed.</div>${qs}</div>` : `<div class="st-row dim small-t">No open questions: every fact the reply needs has a source.</div>`}
    <div class="st-acts">
      <input id="st-extra" placeholder="Anything else the reply should do (optional)">
      <button class="btn small ai" id="st-draft">${br.mode === "task" ? "Draft the email" : "Draft the reply"}</button>
      <button class="btn small ghost" id="st-fresh" title="Re-read everything">Rebuild</button>
      <span class="dim small-t">${esc(br.model || "")} ${br.cached ? "&middot; cached" : ""}</span>
    </div>
  </div>`;
}

function envelopeHtml(br) {
  const sm = ST.data?.summary || {};
  const threads = sm.history || [];
  const known = new Set((sm.recipients || []).map((r) => r.addr));
  const opts = (sm.recipients || []).map((r) => `<option value="${esc(r.addr)}">${esc(r.name ? `${r.name} <${r.addr}>` : r.addr)}</option>`).join("");
  const typed = [...(br.to || []), ...(br.cc || [])].filter((a) => !known.has(a));
  return `<div class="st-row"><span class="label">Where it stands</span> ${esc(br.status || "")}</div>
    <div class="st-row"><span class="label">Purpose</span> <input id="st-purpose" class="st-wide" value="${esc(br.purpose || "")}"></div>
    <div class="st-row st-env">
      <label>Send as</label><select id="st-thread"><option value="">New email</option>${threads.map((h) => `<option value="${esc(h.thread_id)}" ${h.thread_id === br.thread ? "selected" : ""}>Reply all: ${esc((h.subject || "(no subject)").slice(0, 70))} (${esc(h.last.slice(0, 10))})</option>`).join("")}</select>
      <label for="st-to">To</label><input id="st-to" list="st-addrs" value="${esc((br.to || []).join(", "))}" placeholder="From the related mail or the ledger">
      <label for="st-cc">Cc</label><input id="st-cc" list="st-addrs" value="${esc((br.cc || []).join(", "))}">
      <label for="st-subj">Subject</label><input id="st-subj" value="${esc(br.subject || "")}">
      <datalist id="st-addrs">${opts}</datalist>
    </div>
    <div class="dim small-t">${br.thread ? "Reply all keeps the thread's own recipients and subject; To/Cc/Subject here apply to a new email." : "Addresses come only from the related mail and the ledger."}${typed.length ? ` <span class="bad-t">Typed by you, not seen in the material: ${typed.map(esc).join(", ")}</span>` : ""}</div>`;
}

function envelope() {
  const split = (v) => (v || "").split(/[,;\s]+/).map((x) => x.trim()).filter(Boolean);
  return { thread: $("#st-thread")?.value || "", to: split($("#st-to")?.value), cc: split($("#st-cc")?.value), subject: $("#st-subj")?.value || "" };
}

function draftHtml() {
  const d = ST.draft, ck = ST.check;
  const verdict = {};
  (ck?.claims || []).forEach((c) => { verdict[c.text] = c; });
  const rows = (ck?.claims || []).filter((c) => c.verdict !== "supported");
  return `<div class="st-draft">
    <div class="label">Draft ${ck ? `&middot; <span class="ok-t">${ck.counts.supported} supported</span>, <span class="bad-t">${ck.counts.unsupported} unsupported</span>, ${ck.counts.unclear} unclear` : "&middot; checking..."}</div>
    ${d.unanswered?.length ? `<div class="dim small-t">Left for you to confirm: ${d.unanswered.map(esc).join("; ")}</div>` : ""}
    ${rows.length ? `<div class="st-checks">${rows.map((c, i) => `<div class="st-claim ${c.verdict}" data-c="${i}">
        <span class="badge ${c.verdict === "unsupported" ? "warn" : ""}">${esc(c.verdict)}</span> ${esc(c.text)}
        <span class="dim small-t">${esc(c.note)}</span>
        <span class="st-opts"><button class="btn tiny ${c.action === "cut" ? "on" : ""}" data-act="cut">Cut</button><button class="btn tiny ${c.action === "keep" ? "on" : ""}" data-act="keep">Keep</button></span>
      </div>`).join("")}</div>` : ck ? `<div class="ok-t small-t">Every factual sentence has a source.</div>` : ""}
    <div class="st-acts"><button class="btn small primary" id="st-use">Put in the composer${rows.some((c) => c.action === "cut") ? " (with cuts)" : ""}</button>
      ${isTask() ? `<span class="small-t">${esc(envLabel())}</span>` : ""}
      <span class="dim small-t">It becomes a new AI version; you still approve twice.</span></div>
  </div>`;
}

function envLabel() {
  const e = envelope();
  if (e.thread) { const h = (ST.data?.summary?.history || []).find((x) => x.thread_id === e.thread); return `Reply all on "${(h?.subject || "").slice(0, 60)}"`; }
  return e.to.length ? `New email to ${e.to.join(", ")}` : "New email: add a To address first";
}

function wire() {
  const host = $("#studio");
  $("#st-toggle", host)?.addEventListener("click", () => { ST.open = !ST.open; render(); });
  $("#st-fresh", host)?.addEventListener("click", () => {
    ST.autoOpened = true;
    api("/api/studio/start", { method: "POST", body: { key: ST.key, fresh: true } }).then((r) => { ST.data = r; ST.draft = null; ST.check = null; render(); schedule(); });
  });
  $("#st-facts", host)?.addEventListener("click", factsDialog);
  host.querySelectorAll(".st-q").forEach((q) => {
    const i = Number(q.dataset.q);
    q.querySelectorAll("[data-opt]").forEach((b) => b.addEventListener("click", () => { ST.answers[i] = b.dataset.opt; render(); }));
    const inp = $(".st-ans", q); inp.addEventListener("input", () => { ST.answers[i] = inp.value; });
    const sv = $(".st-save", q); sv.addEventListener("change", () => { ST.saveFact = { ...(ST.saveFact || {}), [i]: sv.checked }; });
  });
  ["#st-thread", "#st-to", "#st-cc", "#st-subj"].forEach((s) => $(s, host)?.addEventListener("change", () => { const l = host.querySelector(".st-draft .small-t:not(.dim)"); if (l) l.textContent = envLabel(); }));
  const db = $("#st-draft", host);
  if (db) db.onclick = () => busy(db, draftNow);
  host.querySelectorAll(".st-claim").forEach((row) => {
    const rows = (ST.check?.claims || []).filter((c) => c.verdict !== "supported");
    const c = rows[Number(row.dataset.c)];
    row.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => { c.action = b.dataset.act; render(); }));
  });
  const ub = $("#st-use", host);
  if (ub) ub.onclick = () => busy(ub, useDraft);
}

async function saveEnvelope() {
  if (!isTask() || !ST.data?.brief) return;
  const e = envelope();
  const r = await api("/api/studio/brief", { method: "POST", body: { key: ST.key, ...e, purpose: $("#st-purpose")?.value || "" } });
  ST.data.brief = r.brief;
}

async function draftNow() {
  const br = ST.data?.brief; if (!br) return;
  await saveEnvelope();
  const answers = (br.unknown || []).map((u, i) => ({ question: u.question, answer: (ST.answers[i] || "").trim() })).filter((a) => a.answer);
  // save answers the operator ticked as house facts (their words, their choice)
  for (const [i, on] of Object.entries(ST.saveFact || {})) {
    const a = (ST.answers[i] || "").trim();
    if (on && a) {
      const q = br.unknown[Number(i)]?.question || "";
      try { await api("/api/house-facts", { method: "POST", body: { text: `${q} ${a}`.trim().slice(0, 600), topics: "" } }); }
      catch (e) { toast(`House fact not saved: ${e.message}`, "err"); }
    }
  }
  ST.draft = null; ST.check = null;
  const r = await api("/api/studio/draft", { method: "POST", body: { key: ST.key, answers, instruction: $("#st-extra")?.value || "" } });
  ST.draft = r;
  if (r.envelope && ST.data?.brief && !ST.data.brief.subject && r.envelope.subject) ST.data.brief.subject = r.envelope.subject;
  render();
  try {
    ST.check = await api("/api/studio/check", { method: "POST", body: { key: ST.key, body: r.body, claims: r.claims, answers } });
  } catch (e) { ST.check = { claims: [], counts: { supported: 0, unsupported: 0, unclear: 0 }, error: e.message }; toast(`Check failed: ${e.message}`, "err"); }
  render();
}

function applyCuts(body, claims) {
  let out = body;
  for (const c of claims) {
    if (c.verdict === "supported" || c.action !== "cut") continue;
    const i = out.indexOf(c.text);
    if (i >= 0) out = out.slice(0, i) + out.slice(i + c.text.length);
    else {
      // tolerate whitespace differences: match the claim with flexible spacing
      const rx = new RegExp(c.text.trim().split(/\s+/).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("\\s+"));
      out = out.replace(rx, "");
    }
  }
  return out.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").replace(/ {2,}/g, " ").trim();
}

async function useDraft() {
  const body = applyCuts(ST.draft.body, ST.check?.claims || []);
  const cuts = (ST.check?.claims || []).filter((c) => c.verdict !== "supported" && c.action === "cut").length;
  const label = `Draft Studio${isTask() ? " (task)" : ""}${cuts ? ` (${cuts} unverified cut)` : ""}`;
  if (isTask()) {
    const e = envelope();
    if (!e.thread && !e.to.length) { toast("Add a To address, or pick a thread to reply to.", "err"); return; }
    if (!e.thread && !e.subject.trim()) { toast("Add a subject for the new email.", "err"); $("#st-subj")?.focus(); return; }
    await applyTaskDraft(ST.key.slice(2), body, e, label, ST.check);
    toast(e.thread ? "In the composer as a reply-all on that thread. Read it, then approve twice." : "In the composer as a new email. Read it, then approve twice.", "ok");
    return;
  }
  await applyStudioDraft(ST.key, body, label, ST.check);
  toast("Draft Studio text is in the composer as a new AI version. Read it, then approve twice.", "ok");
}

async function factsDialog() {
  const dlg = document.createElement("dialog"); dlg.className = "facts-dlg";
  const load = async () => {
    const r = await api("/api/house-facts");
    dlg.innerHTML = `<h3>House facts</h3><div class="dim small-t">Your settled answers. Drafts treat them as true. Only you add or change them.</div>
      <div class="facts">${r.facts.map((f) => `<div class="fact" data-id="${f.id}">
        <label><input type="checkbox" class="f-on" ${f.enabled ? "checked" : ""}></label>
        <textarea class="f-text" rows="2">${esc(f.text)}</textarea>
        <input class="f-topics" value="${esc(f.topics)}" placeholder="topic words (blank = always applies)">
        <button class="btn tiny" data-save>Save</button><button class="btn tiny ghost" data-del>Delete</button>
        <div class="dim small-t">${esc(f.source)}</div></div>`).join("") || `<div class="dim">None yet.</div>`}</div>
      <div class="fact new"><textarea id="f-new" rows="2" placeholder="New fact, e.g. 'Credits means our research cloud program'"></textarea>
        <input id="f-new-topics" placeholder="topic words"><button class="btn small primary" id="f-add">Add</button></div>
      <div class="st-acts"><span class="grow"></span><button class="btn small" id="f-close">Close</button></div>`;
    dlg.querySelector("#f-close").onclick = () => { dlg.close(); dlg.remove(); };
    dlg.querySelector("#f-add").onclick = async () => {
      try { await api("/api/house-facts", { method: "POST", body: { text: dlg.querySelector("#f-new").value, topics: dlg.querySelector("#f-new-topics").value } }); load(); }
      catch (e) { toast(e.message, "err"); }
    };
    dlg.querySelectorAll(".fact[data-id]").forEach((row) => {
      const id = row.dataset.id;
      row.querySelector("[data-save]").onclick = async () => {
        try { await api(`/api/house-facts/${id}`, { method: "POST", body: { text: row.querySelector(".f-text").value, topics: row.querySelector(".f-topics").value, enabled: row.querySelector(".f-on").checked } }); toast("Saved.", "ok"); }
        catch (e) { toast(e.message, "err"); }
      };
      row.querySelector("[data-del]").onclick = async () => { if (confirm("Delete this house fact?")) { await api(`/api/house-facts/${id}/delete`, { method: "POST" }); load(); } };
    });
  };
  document.body.appendChild(dlg); await load(); dlg.showModal();
}

