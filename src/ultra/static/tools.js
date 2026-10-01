// Research panel, web search, explain, read aloud and AI audio (SPEC 7.8-7.10).
// Research "Start" spends money: it only runs from the launcher dialog's Start button,
// after the estimate is shown. Nothing here starts a run from a keyboard shortcut.

import { api, esc, toast, busy, copyText } from "./app.js";
import { addSnippet } from "./ledger.js";
import { askButton, openAsk } from "./ask.js";

const $ = (s, el = document) => el.querySelector(s);

const T = { status: null, thread: null, subject: "" };

export function setToolsThread(key, subject) { T.thread = key; T.subject = subject || ""; }

// ---------------------------------------------------------------- tab
export async function initTools() {
  try { T.status = await api("/api/research/status"); } catch { T.status = { enabled: false }; }
  const el = $("#rt-tools");
  el.innerHTML = `
    <div class="tsect">
      <div class="label">Web search <span class="dim small-t">(Google, grounded)</span></div>
      <div class="trow"><input id="tw-q" class="rinput" placeholder="Ask the web..."><button class="btn small" id="tw-go">Ask</button></div>
      <label class="dim small-t"><input type="checkbox" id="tw-thread"> include this conversation</label>
      <div id="tw-out"></div>
    </div>
    <div class="tsect">
      <div class="label">Research <span class="dim small-t">${T.status.enabled ? "" : "(deep-research not found)"}</span></div>
      <div class="trow"><input id="tr-q" class="rinput" placeholder="Search past research..." ${T.status.enabled ? "" : "disabled"}><button class="btn small" id="tr-go" ${T.status.enabled ? "" : "disabled"}>Search</button></div>
      <div class="trow"><button class="btn tiny" id="tr-runs" ${T.status.enabled ? "" : "disabled"}>Recent runs</button><button class="btn tiny" id="tr-new" ${T.status.enabled ? "" : "disabled"}>New research...</button>
      ${T.status.dashboard_url ? `<a class="btn tiny ghost" href="${esc(T.status.dashboard_url)}" target="_blank" rel="noopener noreferrer">Open deep-research</a>` : ""}</div>
      <div id="tr-out"></div>
    </div>`;
  $("#tw-go").onclick = (e) => busy(e.currentTarget, () => webSearch($("#tw-q").value, $("#tw-thread").checked));
  $("#tw-q").onkeydown = (e) => { if (e.key === "Enter") $("#tw-go").click(); };
  $("#tr-go").onclick = (e) => busy(e.currentTarget, () => researchSearch($("#tr-q").value));
  $("#tr-q").onkeydown = (e) => { if (e.key === "Enter") $("#tr-go").click(); };
  $("#tr-runs").onclick = (e) => busy(e.currentTarget, recentRuns);
  $("#tr-new").onclick = () => launcher("");
}

function showToolsTab() {
  document.querySelector('#rail-tabs button[data-tab="tools"]')?.click();
}

// ---------------------------------------------------------------- web
export async function webSearch(q, includeThread = false) {
  q = (q || "").trim();
  if (q.length < 3) { toast("Type a question first."); return; }
  showToolsTab(); $("#tw-q").value = q;
  const out = $("#tw-out");
  out.innerHTML = `<div class="dim">Searching the web...</div>`;
  try {
    const r = await api("/api/ai/web", { method: "POST", body: { q, include_thread: includeThread, thread: T.thread } });
    out.innerHTML = aiCard("Web answer", r, `
      ${r.sources.length ? `<ol class="srcs">${r.sources.map((s) => `<li><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title)}</a></li>`).join("")}</ol>` : `<div class="warnline">No sources returned: treat this as unverified.</div>`}
      <div class="trow"><button class="btn tiny" data-c="copy">Copy</button><button class="btn tiny" data-c="bucket">Add to bucket</button><button class="btn tiny" data-c="research" ${T.status?.enabled ? "" : "disabled"}>Research this...</button></div>`);
    wireCard(out, r.text + (r.sources.length ? "\n\nSources:\n" + r.sources.map((s, i) => `${i + 1}. ${s.title} ${s.url}`).join("\n") : ""), q);
  } catch (e) { out.innerHTML = `<div class="lint error">${esc(e.message)}</div>`; }
}

function aiCard(title, r, extra = "") {
  return `<div class="aicard"><div class="aisum-head"><span class="badge ai">${esc(title)}</span><span class="dim mono small-t">${esc(r.model || "")} ${r.seconds != null ? `&middot; ${esc(r.seconds)} s` : ""}</span></div>
    <div class="body">${esc(r.text || r.answer || "")}</div>${extra}</div>`;
}

function wireCard(el, text, question) {
  el.onclick = (e) => {
    const b = e.target.closest("[data-c]"); if (!b) return;
    if (b.dataset.c === "copy") copyText(text);
    else if (b.dataset.c === "bucket") addSnippet(text, T.thread || "", `Web: ${question}`.slice(0, 200), new Date().toISOString());
    else if (b.dataset.c === "research") launcher(question);
    else if (b.dataset.c === "listen") listen(text, question);
  };
}

// ---------------------------------------------------------------- explain
export async function explain(text) {
  const box = $("#aisum"); if (!box) return;
  box.hidden = false; box.innerHTML = `<span class="dim">Explaining...</span>`;
  try {
    const r = await api("/api/ai/explain", { method: "POST", body: { text, thread: T.thread } });
    box.innerHTML = `<div class="aisum-head"><span class="badge ai">Explain</span><span class="dim mono small-t">${esc(r.model)} &middot; ${esc(r.seconds)} s</span><button class="btn tiny ghost" id="aisum-x">Hide</button></div>
      <div class="dim small-t">"${esc(text.slice(0, 120))}${text.length > 120 ? "..." : ""}"</div><div class="body">${esc(r.text)}</div>`;
    $("#aisum-x").onclick = () => { box.hidden = true; };
  } catch (e) { box.innerHTML = `<div class="lint error">${esc(e.message)}</div>`; }
}

// ---------------------------------------------------------------- research
export async function researchSearch(q) {
  q = (q || "").trim();
  if (q.length < 2) { toast("Type something to search for."); return; }
  showToolsTab(); $("#tr-q").value = q;
  const out = $("#tr-out");
  out.innerHTML = `<div class="dim">Searching past research (about 20 s)...</div>`;
  try {
    const r = await api("/api/research/search", { method: "POST", body: { q, limit: 5 } });
    out.innerHTML = aiCard("From your research", { text: r.answer, model: r.model }, `
      <div class="runs">${(r.matches || []).map((m) => `<div class="run"><span class="mono">#${esc(m.session_id)}</span> <span class="grow ell">${esc(m.prompt)}</span> <span class="dim mono small-t">${Number(m.score || 0).toFixed(2)}</span> <button class="btn tiny" data-open="${esc(m.session_id)}">Open</button></div>`).join("")}</div>
      <div class="trow"><button class="btn tiny" data-c="copy">Copy</button><button class="btn tiny" data-c="bucket">Add to bucket</button></div>`);
    wireCard(out, r.answer || "", q);
    wireRunOpen(out);
  } catch (e) { out.innerHTML = `<div class="lint error">${esc(e.message)}</div>`; }
}

async function recentRuns() {
  const out = $("#tr-out");
  out.innerHTML = `<div class="dim">Loading runs...</div>`;
  const r = await api("/api/research/runs");
  out.innerHTML = `<div class="runs">${r.runs.map((x) => `<div class="run"><span class="mono">#${esc(x.id)}</span> <span class="badge ${x.status === "completed" ? "ok" : x.status === "running" ? "warn" : ""}">${esc(x.status)}</span> <span class="grow ell" title="${esc(x.prompt)}">${esc(x.prompt)}</span> ${x.status === "completed" ? `<button class="btn tiny" data-open="${esc(x.id)}">Open</button>` : ""}</div>`).join("") || `<div class="dim">No runs.</div>`}</div>`;
  wireRunOpen(out);
}

function wireRunOpen(el) {
  el.addEventListener("click", (e) => {
    const b = e.target.closest("[data-open]"); if (!b) return;
    busy(b, () => openReport(Number(b.dataset.open)));
  });
}

async function openReport(id) {
  const r = await api(`/api/research/show/${id}`);
  const dlg = $("#report");
  dlg.innerHTML = `<div class="lc-card rep" role="dialog" aria-modal="true">
    <div class="lc-head"><h3>Research #${esc(r.id)}</h3><span class="dim small-t ell">${esc(r.prompt)}</span><span class="grow"></span>
      <button class="btn tiny" id="rp-copy">Copy</button><button class="btn tiny" id="rp-bucket">Add to bucket</button>
      <button class="btn tiny" id="rp-read">Read aloud</button><button class="btn tiny ai" id="rp-ai">AI audio...</button>${askButton("btn tiny ai")}
      ${T.status?.dashboard_url ? `<a class="btn tiny ghost" href="${esc(T.status.dashboard_url)}" target="_blank" rel="noopener noreferrer">deep-research</a>` : ""}
      <button class="btn tiny ghost" id="rp-x">Close</button></div>
    <div class="md" id="rp-body">${renderMd(r.report || "(no report text)")}</div></div>`;
  dlg.hidden = false;
  const close = () => { dlg.hidden = true; dlg.innerHTML = ""; };
  $("#rp-x").onclick = close;
  dlg.onclick = (e) => { if (e.target === dlg) close(); };
  dlg.onkeydown = (e) => { if (e.key === "Escape") close(); };
  $("#rp-copy").onclick = () => copyText(r.report || "");
  $("#rp-bucket").onclick = () => addSnippet(`Research #${r.id}: ${r.prompt}\n\n${(r.report || "").slice(0, 1500)}`, T.thread || "", `Research #${r.id}`, r.created_at || "");
  $("#rp-read").onclick = () => readAloud($("#rp-body"));
  $("#rp-ai").onclick = () => audioDialog({ run: r.id }, `Research #${r.id}`);
  dlg.querySelector("[data-ask]").onclick = () => { close(); openAsk({ type: "report", id: r.id }, `Research #${r.id}`); };
}

// Minimal, safe Markdown: escape first, then add a few inline/blocks. No raw HTML.
export function renderMd(md) {
  const lines = esc(md).split("\n");
  let html = "", inList = false;
  for (const ln of lines) {
    const h = ln.match(/^(#{1,4})\s+(.*)$/);
    const li = ln.match(/^\s*[-*]\s+(.*)$/);
    if (li) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${inline(li[1])}</li>`; continue; }
    if (inList) { html += "</ul>"; inList = false; }
    if (h) html += `<h${h[1].length + 2}>${inline(h[2])}</h${h[1].length + 2}>`;
    else if (ln.trim()) html += `<p>${inline(ln)}</p>`;
  }
  if (inList) html += "</ul>";
  return html;
}
function inline(s) {
  return s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/\[cite:[^\]]*\]/g, "")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (m, t, u) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${t}</a>`);
}

// ---------------------------------------------------------------- launcher (costs money)
export function launcher(question) {
  const dlg = $("#report");
  const hasThread = !!T.thread;
  dlg.innerHTML = `<div class="lc-card" role="dialog" aria-modal="true" aria-labelledby="rl-title">
    <div class="lc-head"><h3 id="rl-title">New deep research</h3><span class="grow"></span><button class="btn tiny ghost" id="rl-x">Close</button></div>
    <textarea id="rl-q" rows="4" placeholder="What should be researched?">${esc(question || "")}</textarea>
    <div class="trow"><label class="small-t">Depth <select id="rl-depth">${[1, 2, 3].map((n) => `<option ${n === (T.status?.depth || 1) ? "selected" : ""}>${n}</option>`).join("")}</select></label>
      <label class="small-t">Breadth <select id="rl-breadth">${[1, 2, 3, 4, 5].map((n) => `<option ${n === (T.status?.breadth || 3) ? "selected" : ""}>${n}</option>`).join("")}</select></label>
      <label class="small-t" title="${hasThread ? "" : "Open a conversation first"}"><input type="checkbox" id="rl-thread" ${hasThread ? "" : "disabled"}> Include this conversation</label></div>
    <div class="warnline" id="rl-warn" hidden>The full conversation text will be uploaded to Gemini's research agent for this run.</div>
    <div id="rl-est" class="dim small-t">Press Estimate to see the cost before starting.</div>
    <div class="lc-row"><button class="btn small" id="rl-est-b">Estimate</button><span class="grow"></span>
      <button class="btn small" id="rl-cancel">Cancel</button><button class="btn small send" id="rl-start" disabled>Start research</button></div>
  </div>`;
  dlg.hidden = false;
  const close = () => { dlg.hidden = true; dlg.innerHTML = ""; };
  $("#rl-x").onclick = close; $("#rl-cancel").onclick = close;
  const body = () => ({ prompt: $("#rl-q").value, depth: Number($("#rl-depth").value), breadth: Number($("#rl-breadth").value), include_thread: $("#rl-thread").checked, thread: T.thread });
  const invalidate = () => { $("#rl-start").disabled = true; $("#rl-est").textContent = "Press Estimate to see the cost before starting."; };
  for (const id of ["rl-q", "rl-depth", "rl-breadth"]) $("#" + id).addEventListener("input", invalidate);
  $("#rl-thread").onchange = () => { $("#rl-warn").hidden = !$("#rl-thread").checked; invalidate(); };
  $("#rl-est-b").onclick = (e) => busy(e.currentTarget, async () => {
    const r = await api("/api/research/estimate", { method: "POST", body: body() });
    $("#rl-est").innerHTML = `Estimated <b>$${esc(r.cost_usd)}</b> for ${esc(r.nodes || 1)} research task(s)${r.thread_chars ? `, including ${esc(r.thread_chars)} characters of conversation` : ""}. ${esc(r.note || "")}`;
    $("#rl-start").disabled = false;
  });
  $("#rl-start").onclick = (e) => busy(e.currentTarget, async () => {
    const r = await api("/api/research/start", { method: "POST", body: { ...body(), confirm: true } });
    close();
    toast(`Research #${r.session_id} started. It usually takes 5-15 minutes.`, "ok", { label: "Runs", fn: () => { showToolsTab(); recentRuns(); } });
    watchRun(r.session_id);
  });
  $("#rl-q").focus();
}

function watchRun(id) {
  let n = 0;
  const tick = async () => {
    n += 1;
    if (n > 60) return;  // stop after about 30 minutes; the Runs list still has it
    try {
      const r = await api("/api/research/runs");
      const run = r.runs.find((x) => String(x.id) === String(id));
      if (run && run.status === "completed") { toast(`Research #${id} finished.`, "ok", { label: "Open", fn: () => openReport(id) }); return; }
      if (run && ["failed", "cancelled"].includes(run.status)) { toast(`Research #${id} ${run.status}.`, "err"); return; }
    } catch { /* keep trying */ }
    setTimeout(tick, 30000);
  };
  setTimeout(tick, 30000);
}

// ---------------------------------------------------------------- read aloud (browser)
let RA = null;

function blocksOf(root) {
  const els = [...root.querySelectorAll(".msg .body, .md p, .md li, .md h3, .md h4, .md h5, .aisum .body")];
  return els.filter((e) => !e.closest(".quoted") && e.textContent.trim());
}

function cleanForSpeech(t) {
  return t.replace(/https?:\/\/\S+/g, "link").replace(/\S+@\S+\.\w+/g, (m) => m.split("@")[0]).replace(/[*_`#>|]+/g, " ").replace(/\s+/g, " ").trim();
}

export function readAloud(root, onlyText = null) {
  if (!("speechSynthesis" in window)) { toast("This browser has no speech voice."); return; }
  stopReading();
  const blocks = onlyText ? [{ el: null, text: onlyText }] : blocksOf(root).map((el) => ({ el, text: el.textContent }));
  if (!blocks.length) { toast("Nothing to read."); return; }
  RA = { blocks, i: 0, rate: Number(localStorage.getItem("ultra.rate") || 1), voice: localStorage.getItem("ultra.voice") || "", paused: false };
  showListenBar();
  speakBlock();
}

function speakBlock() {
  if (!RA) return;
  document.querySelectorAll(".reading").forEach((e) => e.classList.remove("reading"));
  const b = RA.blocks[RA.i];
  if (!b) { stopReading(); return; }
  if (b.el) { b.el.classList.add("reading"); b.el.scrollIntoView({ block: "center", behavior: "smooth" }); }
  const u = new SpeechSynthesisUtterance(cleanForSpeech(b.text));
  u.rate = RA.rate;
  const v = speechSynthesis.getVoices().find((x) => x.name === RA.voice);
  if (v) u.voice = v;
  u.onend = () => { if (RA && !RA.paused) { RA.i += 1; speakBlock(); } };
  speechSynthesis.speak(u);
  paintListenBar();
}

function stopReading() {
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  document.querySelectorAll(".reading").forEach((e) => e.classList.remove("reading"));
  RA = null;
  const bar = $("#listenbar"); if (bar && !bar.dataset.audio) bar.hidden = true;
}

function showListenBar() {
  const bar = $("#listenbar"); bar.hidden = false; delete bar.dataset.audio;
  const voices = speechSynthesis.getVoices().filter((v) => v.lang.startsWith("en"));
  bar.innerHTML = `<span class="label">Reading</span>
    <button class="btn tiny" data-l="prev" title="Previous block">&lt;&lt;</button>
    <button class="btn tiny" data-l="pause">Pause</button>
    <button class="btn tiny" data-l="next" title="Next block">&gt;&gt;</button>
    <span class="dim small-t" id="lb-pos"></span>
    <select id="lb-rate">${[0.8, 1, 1.2, 1.4, 1.7, 2].map((r) => `<option value="${r}" ${r === RA.rate ? "selected" : ""}>${r}x</option>`).join("")}</select>
    ${voices.length ? `<select id="lb-voice"><option value="">Default voice</option>${voices.map((v) => `<option ${v.name === RA.voice ? "selected" : ""}>${esc(v.name)}</option>`).join("")}</select>` : ""}
    <button class="btn tiny ghost" data-l="stop">Stop</button>`;
  bar.onclick = (e) => {
    const b = e.target.closest("[data-l]"); if (!b || !RA) return;
    const a = b.dataset.l;
    if (a === "stop") { stopReading(); return; }
    if (a === "pause") {
      if (RA.paused) { RA.paused = false; speechSynthesis.resume(); b.textContent = "Pause"; }
      else { RA.paused = true; speechSynthesis.pause(); b.textContent = "Play"; }
      return;
    }
    speechSynthesis.cancel(); RA.paused = false;
    RA.i = Math.max(0, Math.min(RA.blocks.length - 1, RA.i + (a === "next" ? 1 : -1)));
    setTimeout(speakBlock, 50);
  };
  $("#lb-rate").onchange = (e) => { RA.rate = Number(e.target.value); localStorage.setItem("ultra.rate", RA.rate); speechSynthesis.cancel(); setTimeout(speakBlock, 50); };
  const vs = $("#lb-voice"); if (vs) vs.onchange = (e) => { RA.voice = e.target.value; localStorage.setItem("ultra.voice", RA.voice); speechSynthesis.cancel(); setTimeout(speakBlock, 50); };
}

function paintListenBar() { const p = $("#lb-pos"); if (p && RA) p.textContent = `${RA.i + 1} / ${RA.blocks.length}`; }

export function listen(text, title) { readAloud(null, text); }

// ---------------------------------------------------------------- AI audio (Gemini TTS)
export async function audioDialog(source, title) {
  const dlg = $("#report");
  dlg.hidden = false;
  dlg.innerHTML = `<div class="lc-card" role="dialog" aria-modal="true"><div class="dim">Estimating...</div></div>`;
  let est;
  const voice = localStorage.getItem("ultra.aivoice") || "";
  try { est = await api("/api/audio/estimate", { method: "POST", body: { ...source, mode: "summary", voice, title } }); }
  catch (e) { dlg.hidden = true; toast(e.message, "err"); return; }
  const v = voice && est.voices.includes(voice) ? voice : est.default_voice;
  dlg.innerHTML = `<div class="lc-card" role="dialog" aria-modal="true" aria-labelledby="au-title">
    <div class="lc-head"><h3 id="au-title">AI audio</h3><span class="dim small-t ell">${esc(est.title)}</span><span class="grow"></span><button class="btn tiny ghost" id="au-x">Close</button></div>
    <div class="trow"><label><input type="radio" name="au-mode" value="summary" checked> Spoken summary (1-3 min)</label>
      <label><input type="radio" name="au-mode" value="full"> Full read, word for word</label></div>
    <div class="trow"><label class="small-t">Voice <select id="au-voice">${est.voices.map((x) => `<option ${x === v ? "selected" : ""}>${esc(x)}</option>`).join("")}</select></label>
      <span class="dim small-t" id="au-est"></span></div>
    <div class="dim small-t">The text is sent to Gemini (${esc(est.model)}). The audio file stays on this laptop.</div>
    <div class="lc-row"><span class="grow"></span><button class="btn small" id="au-cancel">Cancel</button><button class="btn small ai" id="au-make">Create audio</button></div>
    <div id="au-prog" class="dim small-t"></div></div>`;
  const close = () => { dlg.hidden = true; dlg.innerHTML = ""; };
  $("#au-x").onclick = close; $("#au-cancel").onclick = close;
  const mode = () => document.querySelector('input[name="au-mode"]:checked').value;
  const paintEst = async () => {
    const r = await api("/api/audio/estimate", { method: "POST", body: { ...source, mode: mode(), voice: $("#au-voice").value, title } });
    $("#au-est").textContent = `about ${Math.max(1, Math.round(r.seconds / 60))} min${r.cost_usd != null ? `, about $${r.cost_usd}` : ""}${r.cached ? " (already made: free)" : ""}`;
  };
  document.querySelectorAll('input[name="au-mode"]').forEach((i) => { i.onchange = paintEst; });
  $("#au-voice").onchange = () => { localStorage.setItem("ultra.aivoice", $("#au-voice").value); paintEst(); };
  paintEst();
  $("#au-make").onclick = (e) => busy(e.currentTarget, async () => {
    const r = await api("/api/audio/make", { method: "POST", body: { ...source, mode: mode(), voice: $("#au-voice").value, title } });
    const done = await pollAudio(r.job, (s) => { const p = $("#au-prog"); if (p) p.textContent = s.step || "working..."; });
    close();
    if (done) playAudio(done, title);
  });
}

async function pollAudio(job, onStep) {
  for (let i = 0; i < 400; i += 1) {
    const s = await api(`/api/audio/job/${job}`);
    if (s.state === "done") return s.result;
    if (s.state === "error") { toast(`Audio failed: ${s.error}`, "err"); return null; }
    onStep(s);
    await new Promise((r) => setTimeout(r, 1500));
  }
  toast("Audio is taking too long; check back later.", "err");
  return null;
}

function playAudio(res, title) {
  stopReading();
  const bar = $("#listenbar"); bar.hidden = false; bar.dataset.audio = "1";
  const src = `/api/audio/file/${res.id}`;
  bar.innerHTML = `<span class="label">AI audio</span><span class="dim small-t ell">${esc(title)} &middot; ${esc(res.voice)} &middot; ${Math.round(res.seconds / 60 * 10) / 10} min${res.cost_usd != null ? ` &middot; $${esc(res.cost_usd)}` : ""}${res.cached ? " (cached)" : ""}</span>
    <audio id="lb-audio" controls preload="auto" src="${src}"></audio>
    <select id="lb-speed">${[1, 1.25, 1.5, 1.75, 2].map((r) => `<option value="${r}">${r}x</option>`).join("")}</select>
    <a class="btn tiny" href="${src}?download=1">Download</a>
    <button class="btn tiny ghost" id="lb-script">Script</button>
    <button class="btn tiny ghost" id="lb-close">Close</button>`;
  const a = $("#lb-audio");
  a.play().catch(() => {});
  $("#lb-speed").onchange = (e) => { a.playbackRate = Number(e.target.value); };
  $("#lb-close").onclick = () => { a.pause(); bar.hidden = true; delete bar.dataset.audio; };
  $("#lb-script").onclick = () => { copyText(res.script || ""); };
}
