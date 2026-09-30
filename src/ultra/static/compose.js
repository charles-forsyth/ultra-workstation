// Composer: versions, AI draft/revise, lint, approval 1, review + approval 2, send
// with a cancel window. Every state change is decided by the server; this file only
// renders what the server says and never sends on its own.

import { api, esc, toast, busy, copyText } from "./app.js";

const $ = (s, el = document) => el.querySelector(s);

let D = null;           // current draft (server shape)
let ctxText = "";       // ledger context for AI drafting, set by app.js
let onDone = () => {};  // called after a send lands, with (draft)
let saveTimer = null;

export function setComposerContext(text) { ctxText = text || ""; }

// Draft Studio hands over finished text: open (or resume) the reply draft for the
// thread and save the text as a new AI version. It still needs both approvals.
export async function applyStudioDraft(threadKey, body, label) {
  if (!D || D.thread_id !== threadKey.slice(2) || !["reply", "reply_all"].includes(D.kind)) {
    await openDraft("reply_all", threadKey);
  }
  if (!D) return;
  if (D.state !== "DRAFT" && D.state !== "APPROVED") { toast(`The draft is ${D.state.toLowerCase()}; start a new one.`, "err"); return; }
  await saveNow();
  D = await api(`/api/drafts/${D.id}/studio`, { method: "POST", body: { body, label } });
  render();
  box().scrollIntoView({ block: "nearest" });
}
// Draft Studio task mode: the server makes (or reuses) the draft for the task and
// saves the text as an AI version. New email or reply-all on the chosen thread.
export async function applyTaskDraft(taskId, body, envelope, label) {
  if (D && D.state !== "SENT" && D.state !== "DISCARDED") await saveNow();
  D = await api("/api/drafts/from-task", { method: "POST", body: { task: taskId, body, envelope, label } });
  render();
  box().scrollIntoView({ block: "nearest" });
}
export async function resumeForTask(taskId) {
  const el = box(); if (!el) return;
  try { const r = await api(`/api/drafts/task/${encodeURIComponent(taskId)}`); D = (r.drafts || [])[0] || null; }
  catch { D = null; }
  if (D) render(); else el.hidden = true;
}
export function onSent(fn) { onDone = fn; }

const STATE_LABEL = {
  DRAFT: ["draft", "Draft: edit freely. Approve when it reads right."],
  APPROVED: ["approved", "Approved (1 of 2). Any edit sends it back to Draft."],
  QUEUED: ["queued", "Sending..."],
  SENT: ["sent", "Sent."],
  DISCARDED: ["dim", "Discarded."],
};

function box() { return $("#composer"); }

export async function openDraft(kind, threadKey) {
  const tid = threadKey && threadKey.startsWith("g-") ? threadKey : null;
  if (kind !== "new" && !tid) { toast("Replies from here work for email threads only.", "err"); return; }
  if (tid) {
    const ex = await api(`/api/drafts/thread/${encodeURIComponent(tid.slice(2))}`);
    const same = (ex.drafts || []).find((d) => d.kind === kind);
    if (same) {
      D = same; render(); box().scrollIntoView({ block: "nearest" });
      toast(`Resumed your ${kind.replace("_", " ")} draft (v${D.current?.version}, ${D.state.toLowerCase()}). Discard it to start over.`);
      return;
    }
  }
  D = await api("/api/drafts", { method: "POST", body: { kind, thread: tid } });
  render();
  box().scrollIntoView({ block: "nearest" });
  $("#cx-body")?.focus();
}

// Slack reply: the server picks the channel and thread from the stream row.
export async function openSlackDraft(key) {
  D = await api("/api/slack/draft", { method: "POST", body: { key } });
  D.stream_key = key;
  render();
  box().scrollIntoView({ block: "nearest" });
  if (D.current?.version > 1) toast(`Resumed your Slack draft (v${D.current.version}, ${D.state.toLowerCase()}).`);
  $("#cx-body")?.focus();
}

export async function resumeForThread(threadKey) {
  const el = box();
  if (!threadKey?.startsWith("g-")) { el.hidden = true; D = null; return; }
  try {
    const ex = await api(`/api/drafts/thread/${encodeURIComponent(threadKey.slice(2))}`);
    D = (ex.drafts || [])[0] || null;
  } catch { D = null; }
  if (D) render(); else el.hidden = true;
}

function fields() {
  if (D?.kind === "slack") return { body: $("#cx-body").value };
  return {
    to_addrs: $("#cx-to").value, cc: $("#cx-cc").value, bcc: $("#cx-bcc").value,
    subject: $("#cx-subj").value, body: $("#cx-body").value,
  };
}

async function saveNow() {
  clearTimeout(saveTimer); saveTimer = null;
  if (!D || D.state === "SENT" || D.state === "QUEUED") return;
  const f = fields(), c = D.current || {};
  if (Object.keys(f).every((k) => (f[k] || "") === (c[k] || ""))) return;
  D = await api(`/api/drafts/${D.id}/versions`, { method: "POST", body: f });
  renderMeta();
}

function scheduleSave() {
  clearTimeout(saveTimer);
  // Typing after approval voids it; say so right away rather than on the next save.
  if (D?.state === "APPROVED") { D.state = "DRAFT"; renderMeta(); }
  saveTimer = setTimeout(() => saveNow().catch((e) => toast(e.message, "err")), 900);
}

function lintHtml(v) {
  const xs = v?.lint || [];
  const ref = D?.ticket_ref ? `<div class="dim small-t">Ticket reply: ${esc(D.ticket_ref)} goes last so the ticket system files it on the right request.</div>` : "";
  if (!xs.length) return `<span class="ok-t">No issues.</span>${ref}`;
  return xs.map((i) => `<div class="lint ${i.level}">${i.level === "error" ? "Fix:" : "Check:"} ${esc(i.message)}${i.code === "ticket_ref" ? ` <button class="btn tiny" id="cx-fixref">Put back the Ref line</button>` : ""}</div>`).join("") + ref;
}

function versionsHtml() {
  return (D.versions || []).slice().reverse().map((v) => `
    <div class="ver ${v.version === D.current?.version ? "cur" : ""}">
      <span class="mono">v${v.version}</span>
      <span class="badge ${v.author === "ai" ? "ai" : ""}">${v.author === "ai" ? "AI" : "you"}</span>
      <span class="dim grow">${esc(v.instruction || "")}</span>
      ${v.version !== D.current?.version ? `<button class="btn tiny" data-restore="${v.version}">Restore</button>` : ""}
    </div>`).join("");
}

function render() {
  const el = box(); el.hidden = false;
  const v = D.current || {};
  const locked = D.state === "QUEUED" || D.state === "SENT" || D.state === "DISCARDED";
  const kindLabel = { reply: "Reply", reply_all: "Reply all", forward: "Forward", new: "New message", slack: "Slack reply" }[D.kind];
  const slack = D.kind === "slack";
  const head = slack
    ? `<div class="cx-grid"><label>To</label><div class="strong">${esc(v.to_addrs || "Slack")}</div></div>
       <div class="dim small-t cx-slack-note">Posted as you by Claude through the Slack connector, after both approvals. Slack formatting: *bold*, _italic_, \`code\`.</div>`
    : `<div class="cx-grid">
      <label>From</label><div class="mono dim">${esc(v.from_addr || "")}</div>
      <label for="cx-to">To</label><input id="cx-to" value="${esc(v.to_addrs || "")}" ${locked ? "disabled" : ""}>
      <label for="cx-cc">Cc</label><input id="cx-cc" value="${esc(v.cc || "")}" ${locked ? "disabled" : ""}>
      <label for="cx-bcc">Bcc</label><input id="cx-bcc" value="${esc(v.bcc || "")}" ${locked ? "disabled" : ""}>
      <label for="cx-subj">Subject</label><input id="cx-subj" value="${esc(v.subject || "")}" ${locked ? "disabled" : ""}>
    </div>`;
  el.innerHTML = `
    <div class="cx-head">
      <span class="label">${esc(kindLabel)}</span>
      <span class="cx-state" id="cx-state"></span>
      <span class="grow"></span>
      <button class="btn tiny ghost" id="cx-hist" title="Version history">History</button>
      <button class="btn tiny ghost" id="cx-close" title="Hide (the draft is kept)">Hide</button>
    </div>
    ${head}
    <textarea id="cx-body" rows="${slack ? 5 : 12}" spellcheck="true" ${locked ? "disabled" : ""} placeholder="Write, or use Draft with AI below.">${esc(v.body || "")}</textarea>
    <div class="cx-ai" ${locked ? "hidden" : ""}>
      <input id="cx-instr" placeholder="${v.body ? "What to change (e.g. shorter, warmer)" : "What to say (optional)"}" title="${v.body ? "Tell AI what to change: shorter, warmer, add the Friday date..." : "Tell AI what the reply should say. Leave empty for a sensible reply."}">
      <button class="btn small ai" id="cx-ai">${v.body ? "Revise with AI" : "Draft with AI"}</button>
      <label class="dim small-t cx-chk" title="Give the AI the sender's labs, projects, open tasks and recent logs"><input type="checkbox" id="cx-ctx" ${ctxText ? "checked" : "disabled"}><span>ledger context</span></label>
    </div>
    <div class="cx-lint" id="cx-lint"></div>
    <div class="cx-hist" id="cx-histbox" hidden></div>
    <div class="cx-acts" id="cx-acts"></div>`;
  wire();
  renderMeta();
}

function renderMeta() {
  if (!D) return;
  const [cls, text] = STATE_LABEL[D.state] || ["", D.state];
  const st = $("#cx-state"); if (st) { st.className = `cx-state ${cls}`; st.textContent = `v${D.current?.version || 0} - ${text}`; }
  const ln = $("#cx-lint"); if (ln) {
    ln.innerHTML = lintHtml(D.current);
    const fr = $("#cx-fixref", ln);
    if (fr) fr.onclick = () => busy(fr, async () => { await saveNow(); D = await api(`/api/drafts/${D.id}/fix-ref`, { method: "POST" }); render(); });
  }
  const hb = $("#cx-histbox"); if (hb && !hb.hidden) hb.innerHTML = versionsHtml();
  const acts = $("#cx-acts"); if (!acts) return;
  const hasErr = (D.current?.lint || []).some((i) => i.level === "error");
  if (D.state === "DRAFT") {
    acts.innerHTML = `
      <button class="btn small" id="cx-ascii">Fix ASCII</button>
      ${D.kind === "slack" ? "" : `<button class="btn small" id="cx-gmail" title="Save to Gmail Drafts so you can see it on your phone">Save to Gmail drafts</button>`}
      <button class="btn small" id="cx-copy">Copy</button>
      <button class="btn small ghost" id="cx-discard">Discard</button>
      <span class="grow"></span>
      <button class="btn small primary" id="cx-approve" ${hasErr ? "disabled title=\"Fix the errors first\"" : ""}>Approve (1 of 2)</button>`;
  } else if (D.state === "APPROVED") {
    acts.innerHTML = `
      ${D.send_error ? `<div class="lint error">Last send failed: ${esc(D.send_error)}</div>` : ""}
      <button class="btn small ghost" id="cx-unapprove">Back to draft</button>
      <span class="grow"></span>
      <button class="btn small send" id="cx-review">Review and send (2 of 2)</button>`;
  } else if (D.state === "QUEUED") {
    acts.innerHTML = `<span class="dim">Sending shortly.</span><span class="grow"></span><button class="btn small danger" id="cx-cancel">Cancel send</button>`;
  } else if (D.state === "SENT" && D.kind === "slack") {
    const r = D.sent_result || {};
    acts.innerHTML = r.verified
      ? `<span class="ok-t">Posted to Slack and verified.</span>${r.permalink ? ` <a class="btn tiny" href="${esc(r.permalink)}" target="_blank" rel="noopener noreferrer">Open in Slack</a>` : ""}`
      : `<div class="lint warning">Posted to Slack, not verified: ${esc(r.verify_note || "unknown")}${r.posted_text ? `<pre class="rv-body">${esc(r.posted_text)}</pre>` : ""}</div>${r.permalink ? `<a class="btn tiny" href="${esc(r.permalink)}" target="_blank" rel="noopener noreferrer">Check in Slack</a>` : ""}`;
  } else if (D.state === "SENT") {
    acts.innerHTML = `<span class="ok-t">Sent and verified in Gmail Sent.</span>`;
  } else acts.innerHTML = "";
  wireActs();
}

function wire() {
  for (const id of ["cx-to", "cx-cc", "cx-bcc", "cx-subj", "cx-body"]) $("#" + id)?.addEventListener("input", scheduleSave);
  $("#cx-close").onclick = async () => { await saveNow().catch(() => {}); box().hidden = true; };
  $("#cx-hist").onclick = () => { const hb = $("#cx-histbox"); hb.hidden = !hb.hidden; hb.innerHTML = versionsHtml(); };
  $("#cx-histbox").onclick = (e) => {
    const b = e.target.closest("[data-restore]"); if (!b) return;
    busy(b, async () => { D = await api(`/api/drafts/${D.id}/restore`, { method: "POST", body: { version: Number(b.dataset.restore) } }); render(); $("#cx-histbox").hidden = false; renderMeta(); });
  };
  const ai = $("#cx-ai");
  if (ai) ai.onclick = () => busy(ai, async () => {
    await saveNow();
    const instruction = $("#cx-instr").value.trim();
    if ((D.current?.body || "").trim() && !instruction) { toast("Say what to change, or clear the text for a fresh draft."); return; }
    const context = $("#cx-ctx")?.checked ? ctxText : "";
    D = await api(`/api/drafts/${D.id}/ai`, { method: "POST", body: { instruction, context } });
    render();
    toast(`AI wrote v${D.current.version} (${D.ai?.model || "AI"}, ${D.ai?.seconds ?? "?"} s). Read it before approving.`, "ok");
  });
  $("#cx-instr")?.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#cx-ai").click(); } });
}

function wireActs() {
  const on = (id, fn) => { const b = $("#" + id); if (b) b.onclick = () => busy(b, fn); };
  on("cx-ascii", async () => { await saveNow(); D = await api(`/api/drafts/${D.id}/fix-ascii`, { method: "POST" }); render(); });
  on("cx-copy", async () => { await saveNow(); copyText(D.current?.body || ""); });
  on("cx-gmail", async () => { await saveNow(); await api(`/api/drafts/${D.id}/gmail`, { method: "POST" }); toast("Saved to Gmail Drafts.", "ok"); });
  on("cx-discard", async () => {
    if (!confirm("Discard this draft? Its versions stay in the local history.")) return;
    await api(`/api/drafts/${D.id}/discard`, { method: "POST" }); box().hidden = true; D = null;
  });
  on("cx-approve", async () => { await saveNow(); D = await api(`/api/drafts/${D.id}/approve`, { method: "POST" }); render(); toast("Approved (1 of 2). Review and send when ready.", "ok"); });
  on("cx-unapprove", async () => { D = await api(`/api/drafts/${D.id}/unapprove`, { method: "POST" }); render(); });
  on("cx-review", async () => { const r = await api(`/api/drafts/${D.id}/review`, { method: "POST" }); showReview(r); });
  on("cx-cancel", async () => { D = await api(`/api/send/${D.id}/cancel`, { method: "POST" }); render(); toast("Send cancelled. Nothing went out.", "ok"); stopCountdown(); });
}

// ---------------------------------------------------------------- review dialog (approval 2)
let countdown = null;
function stopCountdown() { clearInterval(countdown); countdown = null; $("#sendbar").hidden = true; }

function showReview(r) {
  const dlg = $("#review");
  const m = r.message;
  const warn = (r.lint || []).filter((i) => i.level !== "error");
  if (r.kind === "slack") return showSlackReview(r, dlg, m, warn);
  dlg.innerHTML = `
    <div class="rv-card" role="dialog" aria-modal="true" aria-labelledby="rv-title">
      <h3 id="rv-title">Send this email? <span class="dim">(approval 2 of 2)</span></h3>
      <div class="kv">
        <span class="k">From</span><span class="mono">${esc(m.from_addr)}</span>
        <span class="k">To</span><span class="strong">${esc(m.to_addrs)}</span>
        ${m.cc ? `<span class="k">Cc</span><span>${esc(m.cc)}</span>` : ""}
        ${m.bcc ? `<span class="k">Bcc</span><span>${esc(m.bcc)}</span>` : ""}
        <span class="k">Subject</span><span>${esc(m.subject)}</span>
      </div>
      ${r.dropped?.length ? `<div class="lint warning">Not included from the thread: ${esc(r.dropped.join(", "))}</div>` : ""}
      ${r.ticket_ref ? `<div class="dim small-t">Ticket reference ${esc(r.ticket_ref)} is the last line.</div>` : ""}
      ${warn.map((i) => `<div class="lint warning">${esc(i.message)}</div>`).join("")}
      <pre class="rv-body">${esc(m.body)}</pre>
      <div class="dim small-t">Version ${esc(r.version)}. Sends ${esc(r.delay)} s after you confirm; you can cancel until then. This approval expires in ${Math.round(r.expires_in / 60)} minutes.</div>
      <div class="rv-acts">
        <button class="btn" id="rv-no">Not yet</button>
        <span class="grow"></span>
        <button class="btn send" id="rv-yes">Send</button>
      </div>
    </div>`;
  dlg.hidden = false;
  $("#rv-no").focus();  // default focus is the safe choice
  const close = () => { dlg.hidden = true; dlg.innerHTML = ""; };
  $("#rv-no").onclick = close;
  dlg.onclick = (e) => { if (e.target === dlg) close(); };
  dlg.onkeydown = (e) => { if (e.key === "Escape") close(); };
  $("#rv-yes").onclick = (e) => busy(e.currentTarget, async () => {
    const q = await api("/api/send", { method: "POST", body: { draft: D.id, token: r.token, version: r.version } });
    close();
    D.state = "QUEUED"; render();
    startCountdown(q.send_in);
  });
}

function showSlackReview(r, dlg, m, warn) {
  const s = r.slack || {};
  dlg.innerHTML = `
    <div class="rv-card" role="dialog" aria-modal="true" aria-labelledby="rv-title">
      <h3 id="rv-title">Post this to Slack? <span class="dim">(approval 2 of 2)</span></h3>
      <div class="kv">
        <span class="k">Where</span><span class="strong">${esc(s.label || "")}</span>
        <span class="k">Channel</span><span class="mono">${esc(s.channel_id || "")}${s.thread_ts ? ` &middot; thread ${esc(s.thread_ts)}` : ""}</span>
      </div>
      <div class="lint warning">${esc(s.notice || "")}</div>
      ${warn.map((i) => `<div class="lint warning">${esc(i.message)}</div>`).join("")}
      <pre class="rv-body">${esc(m.body)}</pre>
      <div class="dim small-t">Version ${esc(r.version)}. Posts ${esc(r.delay)} s after you confirm; you can cancel until then. Posting and checking take up to a couple of minutes. This approval expires in ${Math.round(r.expires_in / 60)} minutes.</div>
      <div class="rv-acts">
        <button class="btn" id="rv-no">Not yet</button>
        <span class="grow"></span>
        <button class="btn send" id="rv-yes" disabled>Post to Slack</button>
      </div>
    </div>`;
  dlg.hidden = false;
  $("#rv-no").focus();
  setTimeout(() => { const y = $("#rv-yes"); if (y) y.disabled = false; }, 2000);  // no double-click posts
  const close = () => { dlg.hidden = true; dlg.innerHTML = ""; };
  $("#rv-no").onclick = close;
  dlg.onclick = (e) => { if (e.target === dlg) close(); };
  dlg.onkeydown = (e) => { if (e.key === "Escape") close(); };
  $("#rv-yes").onclick = (e) => busy(e.currentTarget, async () => {
    const q = await api("/api/send", { method: "POST", body: { draft: D.id, token: r.token, version: r.version } });
    close();
    D.state = "QUEUED"; render();
    startCountdown(q.send_in, 240);
  });
}

function startCountdown(secs, waitMax = 60) {
  const bar = $("#sendbar"); bar.hidden = false;
  let left = secs;
  const id = D.id;
  const paint = () => { bar.innerHTML = `<span>Sending in <b>${left}</b> s</span><button class="btn small danger" id="sb-cancel">Cancel</button>`; $("#sb-cancel").onclick = () => $("#cx-cancel")?.click(); };
  paint();
  clearInterval(countdown);
  countdown = setInterval(async () => {
    left -= 1;
    if (left > 0) { paint(); return; }
    bar.innerHTML = `<span>Sending...</span>`;
    try {
      const d = await api(`/api/drafts/${id}`);
      if (d.state === "SENT") {
        if (D?.id === id && D.stream_key) d.stream_key = D.stream_key;
        stopCountdown(); if (D?.id === id) { D = d; render(); }
        if (d.kind === "slack") toast(d.sent_result?.verified ? "Posted to Slack and verified." : `Posted to Slack, not verified: ${d.sent_result?.verify_note || ""}`, d.sent_result?.verified ? "ok" : "err");
        else toast("Sent and verified.", "ok");
        onDone(d);
      } else if (d.state === "APPROVED") {
        stopCountdown(); if (D?.id === id) { D = d; render(); }
        toast(`Send failed: ${d.send_error || "unknown error"}`, "err");
      }
    } catch { /* keep polling */ }
    if (left < -waitMax) { stopCountdown(); toast(D?.kind === "slack" ? "No confirmation yet; check Slack before trying again." : "No confirmation after a minute; check Gmail Sent.", "err"); }
  }, 1000);
}
