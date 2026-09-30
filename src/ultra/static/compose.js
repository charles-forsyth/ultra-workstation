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
export async function applyStudioDraft(threadKey, body, label, check = null) {
  if (!D || D.thread_id !== threadKey.slice(2) || !["reply", "reply_all"].includes(D.kind)) {
    await openDraft("reply_all", threadKey);
  }
  if (!D) return;
  if (D.state !== "DRAFT" && D.state !== "APPROVED") { toast(`The draft is ${D.state.toLowerCase()}; start a new one.`, "err"); return; }
  await saveNow();
  D = await api(`/api/drafts/${D.id}/studio`, { method: "POST", body: { body, label, check } });
  render();
  box().scrollIntoView({ block: "nearest" });
}
// Draft Studio task mode: the server makes (or reuses) the draft for the task and
// saves the text as an AI version. New email or reply-all on the chosen thread.
export async function applyTaskDraft(taskId, body, envelope, label, check = null) {
  if (D && D.state !== "SENT" && D.state !== "DISCARDED") await saveNow();
  D = await api("/api/drafts/from-task", { method: "POST", body: { task: taskId, body, envelope, label, check } });
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
    from_addr: $("#cx-from")?.value || D.current?.from_addr || "",
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
      ${v.version > 1 ? `<button class="btn tiny ghost" data-cmp="${v.version}" title="What changed from v${v.version - 1}">Changes</button>` : ""}
      ${v.version !== D.current?.version ? `<button class="btn tiny" data-restore="${v.version}">Restore</button>` : ""}
    </div>`).join("");
}

// ---------------------------------------------------------------- before / after
export function diffHtml(c) {
  if (!c) return "";
  if (c.same) return `<div class="dim small-t">No changes.</div>`;
  const fields = (c.fields || []).map((f) => `<div class="cmp-field"><span class="k">${esc(f.field)}</span> <del>${esc(f.old || "(empty)")}</del> <ins>${esc(f.new || "(empty)")}</ins></div>`).join("");
  const body = (c.body || []).map((o) => o.op === "same" ? esc(o.text) : o.op === "add" ? `<ins>${esc(o.text)}</ins>` : `<del>${esc(o.text)}</del>`).join("");
  return `${fields}<div class="cmp-body">${body}</div><div class="dim small-t">+${c.added_words} / -${c.removed_words} words</div>`;
}

async function showCompare(a, b) {
  const q = new URLSearchParams(); if (a) q.set("a", a); if (b) q.set("b", b);
  const c = await api(`/api/drafts/${D.id}/compare?${q}`);
  const vs = c.versions || [];
  const opt = (sel) => vs.map((v) => `<option value="${v.version}" ${v.version === sel ? "selected" : ""}>v${v.version} ${v.author === "ai" ? "AI" : "you"}${v.label ? ` - ${esc(v.label.slice(0, 40))}` : ""}</option>`).join("");
  const hb = $("#cx-cmp");
  hb.hidden = false;
  hb.innerHTML = `<div class="cmp-head"><span class="label">Changes</span>
      <select id="cmp-a">${opt(c.from)}</select> <span class="dim">to</span> <select id="cmp-b">${opt(c.to)}</select>
      <button class="btn tiny ghost" id="cmp-ai" title="Everything you changed since the last AI version">Since the AI</button>
      <span class="grow"></span><button class="btn tiny ghost" id="cmp-x">Close</button></div>
    ${diffHtml(c)}`;
  $("#cmp-x").onclick = () => { hb.hidden = true; };
  $("#cmp-a").onchange = $("#cmp-b").onchange = () => showCompare($("#cmp-a").value, $("#cmp-b").value).catch((e) => toast(e.message, "err"));
  $("#cmp-ai").onclick = () => showCompare("ai", "").catch((e) => toast(e.message, "err"));
}

// ---------------------------------------------------------------- source check
function checkHtml() {
  const ck = D.current?.check;
  const eligible = D.kind === "reply" || D.kind === "reply_all" || D.task_id;
  if (!eligible || D.kind === "slack") return "";
  if (!ck) return `<div class="cx-check dim small-t">Not checked against the sources. <button class="btn tiny ghost" id="cx-check">Check sources</button></div>`;
  const rows = (ck.claims || []).filter((c) => c.verdict !== "supported");
  const head = `<span class="label">Source check v${ck.version}</span> <span class="ok-t">${ck.counts.supported} supported</span>, <span class="bad-t">${ck.counts.unsupported} unsupported</span>, ${ck.counts.unclear} unclear
    <button class="btn tiny ghost" id="cx-check" title="Check this version again">Re-check</button>`;
  if (ck.error) return `<div class="cx-check">${head}<div class="lint warning">${esc(ck.error)}</div></div>`;
  if (!rows.length) return `<div class="cx-check">${head} <span class="ok-t small-t">Every factual sentence has a source.</span></div>`;
  return `<div class="cx-check">${head}
    ${rows.map((c, i) => `<label class="st-claim ${esc(c.verdict)}"><input type="checkbox" data-cut="${i}" ${c.action === "cut" ? "checked" : ""}>
      <span class="badge ${c.verdict === "unsupported" ? "warn" : ""}">${esc(c.rule ? "rule" : c.verdict)}</span> ${esc(c.text)} <span class="dim small-t">${esc(c.note || "")}</span></label>`).join("")}
    <div class="st-acts"><button class="btn tiny" id="cx-cut">Cut the ticked sentences</button><span class="dim small-t">Ticked = cut by default (unsupported or unclear). Untick to keep.</span></div></div>`;
}

function render() {
  const el = box(); el.hidden = false;
  el.dataset.did = D?.kind === "slack" ? "" : String(D?.id || "");
  const v = D.current || {};
  const locked = D.state === "QUEUED" || D.state === "SENT" || D.state === "DISCARDED";
  const kindLabel = { reply: "Reply", reply_all: "Reply all", forward: "Forward", new: "New message", slack: "Slack reply" }[D.kind];
  const slack = D.kind === "slack";
  const head = slack
    ? `<div class="cx-grid"><label>To</label><div class="strong">${esc(v.to_addrs || "Slack")}</div></div>
       <div class="dim small-t cx-slack-note">Posted as you by Claude through the Slack connector, after both approvals. Slack formatting: *bold*, _italic_, \`code\`.</div>`
    : `<div class="cx-grid">
      <label for="cx-from">From</label><select id="cx-from" ${locked ? "disabled" : ""} title="Only your verified Gmail send-as addresses"><option value="${esc(v.from_addr || "")}" selected>${esc(v.from_addr || "")}</option></select>
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
    ${slack ? "" : `<div class="cx-files" id="cx-files"></div>`}
    <textarea id="cx-body" rows="${slack ? 5 : 12}" spellcheck="true" ${locked ? "disabled" : ""} placeholder="Write, or use Draft with AI below.">${esc(v.body || "")}</textarea>
    <div class="cx-ai" ${locked ? "hidden" : ""}>
      <input id="cx-instr" placeholder="${v.body ? "What to change (e.g. shorter, warmer)" : "What to say (optional)"}" title="${v.body ? "Tell AI what to change: shorter, warmer, add the Friday date..." : "Tell AI what the reply should say. Leave empty for a sensible reply."}">
      <button class="btn small ai" id="cx-ai">${v.body ? "Revise with AI" : "Draft with AI"}</button>
      <label class="dim small-t cx-chk" title="Give the AI the sender's labs, projects, open tasks and recent logs"><input type="checkbox" id="cx-ctx" ${ctxText ? "checked" : "disabled"}><span>ledger context</span></label>
    </div>
    <div class="cx-lint" id="cx-lint"></div>
    <div id="cx-checkbox"></div>
    <div id="cx-learn"></div>
    <div class="cx-cmp" id="cx-cmp" hidden></div>
    <div class="cx-hist" id="cx-histbox" hidden></div>
    <div class="cx-acts" id="cx-acts"></div>`;
  wire();
  renderMeta();
  if (!slack) { renderFiles(); fillFrom(v.from_addr || "", locked); }
  if (D.state === "DRAFT") renderLearn();
}

// ---------------------------------------------------------------- From (send-as) and files
let sendAs = null;
async function fillFrom(cur, locked) {
  const sel = $("#cx-from"); if (!sel) return;
  try { sendAs = sendAs || (await api("/api/mail/sendas")).addresses; } catch { sendAs = []; }
  if (!sendAs.length || $("#cx-from") !== sel) return;
  const list = sendAs.map((a) => a.email);
  if (cur && !list.includes(cur.toLowerCase())) list.unshift(cur);
  sel.innerHTML = list.map((e) => `<option value="${esc(e)}" ${e.toLowerCase() === cur.toLowerCase() ? "selected" : ""}>${esc(e)}</option>`).join("");
  sel.disabled = locked || list.length < 2;
  sel.onchange = () => { scheduleSave(); };
}

function filesHtml() {
  const fs = D.attachments || [];
  const locked = D.state !== "DRAFT" && D.state !== "APPROVED";
  return `${fs.map((f) => `<span class="att"><span class="att-n">${esc(f.name)}</span> <span class="dim">${Math.max(1, Math.round(f.size / 1024))} KB</span>${locked ? "" : ` <button class="btn tiny ghost" data-detach="${f.id}" title="Remove">x</button>`}</span>`).join("")}
    ${locked ? "" : `<label class="btn tiny ghost cx-attach" title="Attach a file (25 MB max). Changing files resets approval.">Attach file<input type="file" id="cx-file" multiple hidden></label>`}`;
}

function renderFiles() {
  const el = $("#cx-files"); if (!el) return;
  el.innerHTML = filesHtml();
  el.onclick = (e) => {
    const b = e.target.closest("[data-detach]"); if (!b) return;
    busy(b, async () => { D = await api(`/api/drafts/${D.id}/detach`, { method: "POST", body: { id: Number(b.dataset.detach) } }); renderFiles(); renderMeta(); });
  };
  const inp = $("#cx-file");
  if (inp) inp.onchange = () => uploadFiles([...inp.files]);
  const bx = box();
  bx.ondragover = (e) => { if ([...(e.dataTransfer?.types || [])].includes("Files")) { e.preventDefault(); bx.classList.add("drop"); } };
  bx.ondragleave = () => bx.classList.remove("drop");
  bx.ondrop = (e) => {
    if (!e.dataTransfer?.files?.length) return;
    e.preventDefault(); bx.classList.remove("drop");
    uploadFiles([...e.dataTransfer.files]);
  };
}

async function uploadFiles(list) {
  for (const f of list) {
    if (f.size > 25 * 1024 * 1024) { toast(`${f.name} is over 25 MB.`, "err"); continue; }
    const data = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1] || ""); r.onerror = rej; r.readAsDataURL(f); });
    try {
      await saveNow();
      D = await api(`/api/drafts/${D.id}/attach`, { method: "POST", body: { name: f.name, mime: f.type, data } });
      toast(`Attached ${f.name}.`, "ok");
    } catch (e) { toast(e.message, "err"); }
  }
  renderFiles(); renderMeta();
}

window.addEventListener("ultra:draft-files", async () => {
  if (!D) return;
  D = await api(`/api/drafts/${D.id}`); renderFiles(); renderMeta();
});

function renderMeta() {
  if (!D) return;
  const [cls, text] = STATE_LABEL[D.state] || ["", D.state];
  const st = $("#cx-state"); if (st) { st.className = `cx-state ${cls}`; st.textContent = `v${D.current?.version || 0} - ${text}`; }
  const cb = $("#cx-checkbox"); if (cb) { cb.innerHTML = checkHtml(); wireCheck(); }
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
      <button class="btn small" id="cx-tidy" title="Fix form only, no AI: plain ASCII, spacing, a repeated greeting or signature, the Ref line placement. Never changes facts.">Tidy</button>
      <button class="btn small" id="cx-cmpbtn" title="What changed between versions">Changes</button>
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
    const c = e.target.closest("[data-cmp]");
    if (c) { const v = Number(c.dataset.cmp); showCompare(String(v - 1), String(v)).catch((x) => toast(x.message, "err")); return; }
    const b = e.target.closest("[data-restore]"); if (!b) return;
    busy(b, async () => { D = await api(`/api/drafts/${D.id}/restore`, { method: "POST", body: { version: Number(b.dataset.restore) } }); render(); $("#cx-histbox").hidden = false; renderMeta(); });
  };
  const ai = $("#cx-ai");
  if (ai) ai.onclick = () => busy(ai, async () => {
    await saveNow();
    const instruction = $("#cx-instr").value.trim();
    if ((D.current?.body || "").trim() && !instruction) { toast("Say what to change, or clear the text for a fresh draft."); return; }
    const context = $("#cx-ctx")?.checked ? ctxText : "";
    const prev = D.current?.version || 0;
    D = await api(`/api/drafts/${D.id}/ai`, { method: "POST", body: { instruction, context } });
    render();
    const ck = D.current?.check;
    const flags = ck ? (ck.counts.unsupported + ck.counts.unclear) : 0;
    toast(`AI wrote v${D.current.version}${D.ai?.sourced ? " from the Draft Studio sources" : ""}${ck ? `; ${flags ? `${flags} sentence(s) flagged` : "every fact has a source"}` : ""}. Read it before approving.`, flags ? "err" : "ok");
    if (prev > 0 && (D.current?.body || "").trim()) showCompare(String(prev), String(D.current.version)).catch(() => {});
  });
  $("#cx-instr")?.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#cx-ai").click(); } });
}

// ---------------------------------------------------------------- learned from your edits
let learnCache = null;
async function renderLearn() {
  const el = $("#cx-learn"); if (!el || D?.kind === "slack") return;
  let r;
  if (learnCache && Date.now() - learnCache.at < 60000) r = learnCache.r;
  else { try { r = await api("/api/learn"); learnCache = { at: Date.now(), r }; } catch { return; } }
  const s = (r.suggestions || [])[0];
  if (!s) { el.innerHTML = ""; return; }
  el.innerHTML = `<div class="cx-learn lint warning">Learned from your edits: ${esc(s.text)}${s.examples ? `<div class="dim small-t">e.g. "${esc(s.examples[0])}"</div>` : ""}
    <span class="st-opts"><button class="btn tiny" data-learn="accept">Make it a style rule</button><button class="btn tiny ghost" data-learn="dismiss">No, leave it</button></span>
    <div class="dim small-t">A rule is a warning in the composer and a rule for the AI; it goes in your private style.toml, where you can edit or remove it.</div></div>`;
  el.querySelectorAll("[data-learn]").forEach((b) => b.onclick = () => busy(b, async () => {
    const act = b.dataset.learn;
    if (act === "accept" && !confirm(`Add this style rule?\n\n${s.rule.message}\n\nIt warns when a draft contains "${s.phrase}", and the AI is told to avoid it.`)) return;
    await api(`/api/learn/${act}`, { method: "POST", body: { key: s.key } });
    toast(act === "accept" ? "Style rule added. It applies to the next save and the next AI draft." : "Dismissed. It will not come back.", "ok");
    learnCache = null; renderLearn();
  }));
}

function wireCheck() {
  const b = $("#cx-check");
  if (b) b.onclick = () => busy(b, async () => { await saveNow(); D = await api(`/api/drafts/${D.id}/check`, { method: "POST" }); render(); });
  const cut = $("#cx-cut");
  if (cut) cut.onclick = () => busy(cut, async () => {
    const rows = (D.current?.check?.claims || []).filter((c) => c.verdict !== "supported");
    const texts = [...document.querySelectorAll("#cx-checkbox [data-cut]")].filter((x) => x.checked).map((x) => rows[Number(x.dataset.cut)]?.text).filter(Boolean);
    if (!texts.length) { toast("Nothing ticked."); return; }
    await saveNow();
    const prev = D.current?.version;
    D = await api(`/api/drafts/${D.id}/cut`, { method: "POST", body: { texts } });
    render(); toast(`Cut ${texts.length} sentence(s). Read it before approving.`, "ok");
    showCompare(String(prev), String(D.current.version)).catch(() => {});
  });
}

function wireActs() {
  const on = (id, fn) => { const b = $("#" + id); if (b) b.onclick = () => busy(b, fn); };
  on("cx-ascii", async () => { await saveNow(); D = await api(`/api/drafts/${D.id}/fix-ascii`, { method: "POST" }); render(); });
  on("cx-tidy", async () => {
    await saveNow();
    const prev = D.current?.version;
    const r = await api(`/api/drafts/${D.id}/tidy`, { method: "POST" });
    D = r; render();
    if (!r.tidy?.length) toast("Tidy: nothing to fix.", "ok");
    else { toast(`Tidy: ${r.tidy.join("; ")}.`, "ok"); showCompare(String(prev), String(D.current.version)).catch(() => {}); }
  });
  on("cx-copy", async () => { await saveNow(); copyText(D.current?.body || ""); });
  on("cx-cmpbtn", async () => { await saveNow(); if ((D.versions || []).length < 2) { toast("Only one version so far."); return; } await showCompare("", ""); });
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

function reviewCheckHtml(r) {
  const ck = r.check;
  if (!ck) return (r.kind === "reply" || r.kind === "reply_all") ? `<div class="dim small-t">This version was not checked against the sources.</div>` : "";
  const left = (ck.claims || []).filter((c) => c.verdict !== "supported");
  if (!left.length) return `<div class="ok-t small-t">Source check: every factual sentence has a source.</div>`;
  return `<div class="lint warning">Still in the text without a source (${left.length}):<ul>${left.map((c) => `<li>${esc(c.text)} <span class="dim small-t">${esc(c.rule ? c.note : c.verdict)}</span></li>`).join("")}</ul></div>`;
}

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
        ${(r.attachments || []).length ? `<span class="k">Files</span><span>${r.attachments.map((a) => `${esc(a.name)} <span class="dim">(${Math.max(1, Math.round(a.size / 1024))} KB)</span>`).join(", ")}</span>` : ""}
      </div>
      ${r.dropped?.length ? `<div class="lint warning">Not included from the thread: ${esc(r.dropped.join(", "))}</div>` : ""}
      ${r.ticket_ref ? `<div class="dim small-t">Ticket reference ${esc(r.ticket_ref)} is the last line.</div>` : ""}
      ${warn.map((i) => `<div class="lint warning">${esc(i.message)}</div>`).join("")}
      ${reviewCheckHtml(r)}
      ${r.last_ai && !r.last_ai.same ? `<details class="rv-cmp"><summary>Your changes since the AI version (+${r.last_ai.added_words} / -${r.last_ai.removed_words} words)</summary>${diffHtml(r.last_ai)}</details>` : ""}
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
