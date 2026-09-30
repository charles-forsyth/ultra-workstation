// Context rail (People / Person / Search tabs), the bucket, and staged ledger cards.
// Every ledger write goes: stage -> operator edits -> Commit click -> server runs the
// CLI -> read-back -> chips turn green or red. Nothing here writes on its own.

import { api, esc, toast, busy, copyText } from "./app.js";
import { setComposerContext } from "./compose.js";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const R = { key: null, item: null, tab: "people", people: [], person: null, bucket: [] };

// ---------------------------------------------------------------- tabs
export function initRail() {
  $("#rail-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]"); if (!b) return;
    showTab(b.dataset.tab);
  });
  wireBucket();
  loadBucket();
}

function showTab(tab) {
  R.tab = tab;
  $$("#rail-tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  $$(".rtab").forEach((p) => { p.hidden = p.id !== `rt-${tab}`; });
  if (tab === "search") $("#ls-q")?.focus();
}

// ---------------------------------------------------------------- people tab
export async function loadPeople(it) {
  R.key = it.key; R.item = it; R.person = null;
  setComposerContext("");
  const el = $("#rt-people");
  el.innerHTML = `<div class="dim">Finding everyone on this conversation...</div>`;
  $("#rt-person").innerHTML = `<div class="dim">Pick someone on the People tab.</div>`;
  let r;
  try { r = await api(`/api/people/${encodeURIComponent(it.key)}`); }
  catch (e) { el.innerHTML = `<div class="dim">${esc(e.message)}</div>`; return; }
  if (R.key !== it.key) return;
  R.people = r.people || [];
  $('#rail-tabs button[data-tab="people"]').textContent = `People ${R.people.length || ""}`.trim();
  if (!R.people.length) { el.innerHTML = `<div class="dim">Only you on this one.</div>`; return; }
  el.innerHTML = `<div class="plist">${R.people.map((p, i) => {
    const l = p.ledger;
    const role = p.roles.includes("from") ? "wrote" : p.roles.includes("to") ? "to" : "cc";
    const tag = !r.ledger ? "" : l && l.id ? `<span class="badge ok">${esc(l.netid || "in ledger")}</span>`
      : l && l.error ? `<span class="badge warn" title="${esc(l.error)}">?</span>` : `<span class="badge dim">not in ledger</span>`;
    return `<div class="prow" data-i="${i}" draggable="${l && l.id ? "true" : "false"}" tabindex="0">
      <div class="pmain"><b>${esc(p.name || p.addr)}</b> ${tag}</div>
      <div class="dim small-t">${esc((p.addrs || [p.addr]).join(", "))} &middot; ${role}${p.sent > 1 ? ` (${p.sent})` : ""}${l?.title ? ` &middot; ${esc(l.title)}` : ""}</div>
    </div>`;
  }).join("")}</div>
  <div class="dim small-t hint">Click a person for their labs, projects, open tasks and history. Drag a person into the bucket to link them.</div>`;
  el.onclick = (e) => { const row = e.target.closest(".prow"); if (row) openPerson(R.people[Number(row.dataset.i)]); };
  el.onkeydown = (e) => { if (e.key === "Enter") { const row = e.target.closest(".prow"); if (row) openPerson(R.people[Number(row.dataset.i)]); } };
  el.ondragstart = (e) => {
    const row = e.target.closest(".prow"); if (!row) return;
    const p = R.people[Number(row.dataset.i)];
    if (!p.ledger?.id) { e.preventDefault(); return; }
    e.dataTransfer.setData("application/x-ultra-entity", JSON.stringify({ id: p.ledger.id, name: p.ledger.name, type: "Researcher" }));
  };
  // Primary person: the most recent sender who is not me; open their card now so the
  // composer's "ledger context" has something, and so the Person tab is ready.
  const first = R.people.find((p) => p.ledger?.id) || R.people[0];
  if (first) openPerson(first, false);
}

async function openPerson(p, jump = true) {
  const el = $("#rt-person");
  if (jump) showTab("person");
  el.innerHTML = `<div class="dim">Loading ${esc(p.name || p.addr)} from the ledger... (a few seconds the first time)</div>`;
  const key = R.key;
  let c;
  try { c = await api(`/api/person?addr=${encodeURIComponent(p.addr)}&name=${encodeURIComponent(p.name || "")}`); }
  catch (e) { el.innerHTML = `<div class="dim">${esc(e.message)}</div>`; return; }
  if (R.key !== key) return;
  if (c.unavailable) { el.innerHTML = `<div class="dim">Ledger unavailable: ${esc(c.reason || "")}</div>`; return; }
  if (c.unresolved) { el.innerHTML = `<div class="cname">${esc(p.name || p.addr)}</div><div class="dim">${esc(p.addr)} is not in the ledger.</div>`; return; }
  R.person = c;
  const tasks = c.open_tasks || [];
  setComposerContext([
    `${c.name}${c.title ? ", " + c.title : ""}`,
    (c.labs || []).length ? `Labs: ${c.labs.join("; ")}` : "",
    (c.projects || []).length ? `Projects: ${c.projects.join("; ")}` : "",
    tasks.length ? `Open tasks with them: ${tasks.map((x) => x.summary).join("; ")}` : "",
    (c.interactions || []).length ? `Recent history: ${c.interactions.map((x) => x.summary).join("; ")}` : "",
  ].filter(Boolean).join("\n"));
  const ent = (x, type) => `<li class="ent" draggable="true" data-ent='${esc(JSON.stringify({ id: x.id, name: x.name || x.summary, type }))}' title="Drag into the bucket to link">${esc(x.name || x.summary)}</li>`;
  const list = (xs, f) => xs.length ? `<ul class="clist">${xs.map(f).join("")}</ul>` : `<div class="dim small-t">none</div>`;
  const showId = c.netid && !String(c.name || "").includes(`(${c.netid})`);
  el.innerHTML = `<div class="cname">${esc(c.name)}${showId ? ` <span class="dim mono">${esc(c.netid)}</span>` : ""}</div>
    <div class="dim">${esc([c.title, c.dept].filter(Boolean).join(" - "))}</div>
    ${c.matched_by === "name" ? `<div class="warnline">Matched by name, not address: check it's the right person.</div>` : ""}
    <div class="pacts"><button class="btn tiny" data-pa="bucket">Add to bucket</button><button class="btn tiny ghost" data-pa="copy">Copy id</button></div>
    <div class="sect"><span class="label">Labs</span>${list(c.lab_refs || [], (x) => ent(x, "Lab"))}</div>
    <div class="sect"><span class="label">Projects</span>${list(c.project_refs || [], (x) => ent(x, "Project"))}</div>
    <div class="sect"><span class="label">Open tasks ${tasks.length}</span>${list(tasks, (x) => `<li class="task" data-id="${esc(x.id)}"><span class="pri ${esc((x.priority || "").toLowerCase())}">${esc((x.priority || "").slice(0, 1))}</span> ${esc(x.summary)} <button class="btn tiny ghost" data-done="${esc(x.id)}" title="Mark done in the ledger">Done</button></li>`)}</div>
    <div class="sect"><span class="label">Recent logs ${esc(c.interaction_count ?? "")}</span>${list(c.interactions || [], (x) => ent(x, "Interaction"))}</div>`;
  $('[data-pa="bucket"]', el).onclick = () => addEntity({ id: c.id, name: c.name, type: "Researcher" });
  $('[data-pa="copy"]', el).onclick = () => copyText(c.id || c.netid);
  el.ondragstart = (e) => { const li = e.target.closest("[data-ent]"); if (li) e.dataTransfer.setData("application/x-ultra-entity", li.dataset.ent); };
  el.onclick = (e) => {
    const d = e.target.closest("[data-done]"); if (!d) return;
    const li = d.closest("li");
    if (!confirm(`Mark this task DONE in the ledger?\n\n${li.textContent.replace("Done", "").trim()}`)) return;
    busy(d, async () => {
      const r = await api("/api/ledger/task-status", { method: "POST", body: { id: d.dataset.done, status: "DONE" } });
      if (r.ok) { li.classList.add("done-t"); d.remove(); toast("Task marked done.", "ok"); }
      else toast(`The ledger did not confirm: ${r.output_tail || "unknown"}`, "err");
    });
  };
}

// ---------------------------------------------------------------- search tab
let searchTimer = null;
export function wireSearch() {
  const q = $("#ls-q");
  q.oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 450); };
  q.onkeydown = (e) => { if (e.key === "Enter") { clearTimeout(searchTimer); runSearch(); } };
}

export function searchFor(text) {
  showTab("search");
  $("#ls-q").value = text.slice(0, 200);
  runSearch();
}

async function runSearch() {
  const term = $("#ls-q").value.trim();
  const out = $("#ls-out");
  if (term.length < 2) { out.innerHTML = ""; return; }
  out.innerHTML = `<div class="dim">Searching the ledger...</div>`;
  try {
    const r = await api(`/api/ledger/search?q=${encodeURIComponent(term)}`);
    if ($("#ls-q").value.trim() !== term) return;
    out.innerHTML = (r.results || []).map((x) => `<div class="srow" draggable="true" data-ent='${esc(JSON.stringify({ id: x.id, name: x.name, type: x.type }))}'>
      <span class="badge">${esc(x.type)}</span> <span>${esc(x.name)}</span>
      <span class="grow"></span><span class="dim mono small-t">${x.score != null ? Number(x.score).toFixed(2) : ""}</span>
      <button class="btn tiny" data-add>+</button></div>`).join("") || `<div class="dim">No match.</div>`;
  } catch (e) { out.innerHTML = `<div class="dim">${esc(e.message)}</div>`; }
}

// ---------------------------------------------------------------- bucket
async function loadBucket() {
  try { R.bucket = (await api("/api/bucket")).items || []; } catch { R.bucket = []; }
  renderBucket();
}

function renderBucket() {
  $("#bucket-count").textContent = R.bucket.length || "";
  const el = $("#bucket-items");
  el.innerHTML = R.bucket.map((b) => `<div class="bitem" title="${esc(b.subject || "")}">
    <span class="badge">${esc(b.kind === "entity" ? (b.entity_type || "entity") : b.kind)}</span>
    <span class="grow ell">${esc(b.subject || b.key)}</span>
    ${(b.people || []).length ? `<span class="dim small-t">${b.people.length} ppl</span>` : ""}
    <button class="btn tiny ghost" data-rm="${b.id}" title="Remove">x</button></div>`).join("");
  $("#bucket-drop").textContent = R.bucket.length ? "Drop more here" : "Drag email, Slack, tickets or people here";
  $$("#bucket-acts button[data-act]").forEach((b) => { b.disabled = !R.bucket.length; });
  $("#bucket-clear").hidden = !R.bucket.length;
}

export async function addConversation(key) {
  try { R.bucket = (await api("/api/bucket", { method: "POST", body: { kind: "conversation", key } })).items; renderBucket(); toast("Added to the bucket.", "ok"); }
  catch (e) { toast(e.message, "err"); }
}

export async function addEntity(ent) {
  try { R.bucket = (await api("/api/bucket", { method: "POST", body: { kind: "entity", ...ent } })).items; renderBucket(); toast(`Added ${ent.name} to the bucket.`, "ok"); }
  catch (e) { toast(e.message, "err"); }
}

export async function addSnippet(text, source, subject, ts) {
  try { R.bucket = (await api("/api/bucket", { method: "POST", body: { kind: "snippet", text, source, subject, ts } })).items; renderBucket(); toast("Snippet added to the bucket.", "ok"); }
  catch (e) { toast(e.message, "err"); }
}

function readDrop(e) {
  const ent = e.dataTransfer.getData("application/x-ultra-entity");
  if (ent) return { entity: JSON.parse(ent) };
  const it = e.dataTransfer.getData("application/x-ultra-item");
  if (it) return { key: JSON.parse(it).key };
  return null;
}

function wireBucket() {
  const zone = $("#bucket");
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
  zone.addEventListener("dragleave", (e) => { if (!zone.contains(e.relatedTarget)) zone.classList.remove("over"); });
  zone.addEventListener("drop", (e) => {
    e.preventDefault(); zone.classList.remove("over");
    const d = readDrop(e); if (!d) return;
    const act = e.target.closest("button[data-act]");
    if (act && d.key) { stage(act.dataset.act, d.key); return; }  // straight onto Log/Task
    if (d.entity) addEntity(d.entity); else if (d.key) addConversation(d.key);
  });
  $("#bucket-items").onclick = async (e) => {
    const b = e.target.closest("[data-rm]"); if (!b) return;
    R.bucket = (await api("/api/bucket/remove", { method: "POST", body: { id: Number(b.dataset.rm) } })).items; renderBucket();
  };
  $("#bucket-clear").onclick = async () => { R.bucket = (await api("/api/bucket/clear", { method: "POST" })).items; renderBucket(); };
  $$("#bucket-acts button[data-act]").forEach((b) => { b.onclick = () => busy(b, () => stage(b.dataset.act)); });
  document.addEventListener("click", (e) => {
    const s = e.target.closest("#ls-out [data-add]"); if (!s) return;
    addEntity(JSON.parse(s.closest("[data-ent]").dataset.ent));
  });
  document.addEventListener("dragstart", (e) => {
    const s = e.target.closest?.("#ls-out [data-ent]"); if (s) e.dataTransfer.setData("application/x-ultra-entity", s.dataset.ent);
  });
}

// ---------------------------------------------------------------- staged card
let CARD = null;

export async function stage(action, key = null, sent = false) {
  if (action === "block") { toast("Calendar blocks arrive with the Today view (v0.6)."); return; }
  const dlg = $("#ledger-card");
  dlg.hidden = false;
  dlg.innerHTML = `<div class="lc-card"><div class="dim">Building the ${action === "log" ? "log entry" : "task"}: resolving people, labs and projects...</div></div>`;
  try {
    CARD = await api("/api/ledger/stage", { method: "POST", body: { action, key, sent } });
  } catch (e) { dlg.hidden = true; toast(e.message, "err"); return; }
  renderCard();
}

export function stageAfterSend(threadKey) {
  stage("log", threadKey, true);
}

function chipHtml(c, i) {
  const st = c.state ? ` ${c.state}` : "";
  return `<label class="chip${c.derived ? " derived" : ""}${st}" title="${esc(c.kind)} - ${esc(c.source)}${c.via === "ref" ? " (referenced)" : " (participant)"}">
    <input type="checkbox" data-chip="${i}" ${c.checked ? "checked" : ""} ${CARD.state !== "staged" ? "disabled" : ""}>
    <span class="ck">${esc(c.kind[0].toUpperCase())}</span>${esc(c.name)}
    ${c.state === "missing" ? `<button class="btn tiny" data-linknow="${i}">Link now</button>` : ""}
  </label>`;
}

function renderCard() {
  const c = CARD, dlg = $("#ledger-card");
  const isLog = c.action === "log";
  dlg.innerHTML = `<div class="lc-card" role="dialog" aria-modal="true" aria-labelledby="lc-title">
    <div class="lc-head"><h3 id="lc-title">${isLog ? "Log interaction" : "New task"}</h3>
      <span class="dim small-t">${c.items.map((x) => esc(x.subject || x.kind)).join(" + ")}</span>
      <span class="grow"></span><button class="btn tiny ghost" id="lc-x">Close</button></div>
    ${c.ledger === false ? `<div class="lint error">The ledger CLI is not available; nothing can be written.</div>` : ""}
    <div class="lc-grid">
      ${isLog ? `<label for="lc-date">Date</label><input id="lc-date" value="${esc(c.date)}" title="Local time, YYYY-MM-DD HH:MM">`
        : `<label for="lc-pri">Priority</label><select id="lc-pri">${["LOW", "MEDIUM", "HIGH", "CRITICAL"].map((p) => `<option ${p === c.priority ? "selected" : ""}>${p}</option>`).join("")}</select>`}
      <label>Links</label><div class="chips" id="lc-chips">${c.chips.map(chipHtml).join("") || `<span class="dim small-t">No ledger matches. Add some below.</span>`}</div>
      <label></label><div class="lc-add"><input id="lc-find" placeholder="Add a link: search the ledger"><div id="lc-found" class="lc-found"></div></div>
    </div>
    ${c.unresolved?.length ? `<div class="dim small-t">Not in the ledger: ${c.unresolved.map((u) => esc(u.name || u.addr)).join(", ")}</div>` : ""}
    <textarea id="lc-text" rows="${isLog ? 9 : 3}" ${c.state !== "staged" ? "disabled" : ""}>${esc(c.text)}</textarea>
    <div class="lc-row">
      <button class="btn small ai" id="lc-ai" ${c.state !== "staged" ? "disabled" : ""}>Rewrite with AI</button>
      <span class="dim small-t" id="lc-count"></span>
      <span class="grow"></span>
      ${c.state === "committed" ? `<button class="btn small primary" id="lc-cancel">Done</button>` : `<button class="btn small" id="lc-cancel">Discard</button>
      <button class="btn small primary" id="lc-commit" ${c.state !== "staged" || c.ledger === false ? "disabled" : ""}>Commit to ledger</button>`}
    </div>
    <div class="lc-progress" id="lc-progress" hidden></div>
  </div>`;
  const close = () => { dlg.hidden = true; dlg.innerHTML = ""; CARD = null; };
  $("#lc-x").onclick = close;
  $("#lc-cancel").onclick = close;
  dlg.onkeydown = (e) => { if (e.key === "Escape" && CARD?.state !== "committing") close(); };
  const count = () => { $("#lc-count").textContent = `${$("#lc-text").value.length} chars`; };
  $("#lc-text").oninput = count; count();
  $("#lc-chips").onchange = (e) => { const i = e.target.dataset.chip; if (i != null) CARD.chips[Number(i)].checked = e.target.checked; };
  $("#lc-chips").onclick = (e) => {
    const b = e.target.closest("[data-linknow]"); if (!b) return;
    e.preventDefault();
    const ch = CARD.chips[Number(b.dataset.linknow)];
    busy(b, async () => {
      const r = await api("/api/ledger/link", { method: "POST", body: { source: CARD.result.kind === "task" ? CARD.result.id : ch.id, target: CARD.result.kind === "task" ? ch.id : CARD.result.id, type: CARD.result.kind === "task" || ch.via === "ref" ? "REFERENCED_IN" : "PARTICIPATED_IN" } });
      ch.state = r.ok ? "linked" : "missing"; renderCard(); showResult();
      if (!r.ok) toast(`Still not linked: ${r.output_tail || ""}`, "err");
    });
  };
  wireFind();
  $("#lc-ai").onclick = (e) => busy(e.currentTarget, async () => {
    const r = await api("/api/ledger/ai-text", { method: "POST", body: { text: $("#lc-text").value, mode: CARD.action } });
    $("#lc-text").value = r.text; count(); toast(`Rewritten by ${r.model}. Check it before committing.`, "ok");
  });
  const cb = $("#lc-commit"); if (cb) cb.onclick = (e) => busy(e.currentTarget, commit);
  if (c.result) showResult();
  $("#lc-text").focus();
}

function wireFind() {
  const q = $("#lc-find"); let t = null;
  q.oninput = () => {
    clearTimeout(t);
    t = setTimeout(async () => {
      const term = q.value.trim(); const out = $("#lc-found");
      if (term.length < 2) { out.innerHTML = ""; return; }
      out.innerHTML = `<span class="dim small-t">searching...</span>`;
      try {
        const r = await api(`/api/ledger/search?q=${encodeURIComponent(term)}`);
        out.innerHTML = (r.results || []).slice(0, 8).map((x, i) => `<button class="btn tiny" data-pick="${i}">${esc(x.type)}: ${esc(x.name)}</button>`).join("") || `<span class="dim small-t">no match</span>`;
        out.onclick = (e) => {
          const b = e.target.closest("[data-pick]"); if (!b) return;
          const x = r.results[Number(b.dataset.pick)];
          if (CARD.chips.some((c) => c.id === x.id)) return;
          const participant = CARD.action === "log" && (x.type === "Researcher" || x.type === "Lab");
          CARD.chips.push({ id: x.id, name: x.name, kind: (x.type || "entity").toLowerCase(), source: "added by you", checked: true, via: participant ? "log" : "ref" });
          const text = $("#lc-text").value; renderCard(); $("#lc-text").value = text;
        };
      } catch (e) { out.innerHTML = `<span class="dim small-t">${esc(e.message)}</span>`; }
    }, 400);
  };
}

async function commit() {
  const text = $("#lc-text").value.trim();
  if (!text) { toast("The text is empty.", "err"); return; }
  const chips = CARD.chips.filter((c) => c.checked);
  const body = { card: CARD.id, text, chips, date: $("#lc-date")?.value || "", priority: $("#lc-pri")?.value || "MEDIUM" };
  const summary = CARD.action === "log"
    ? `Write this interaction to the ledger?\n\nDate: ${body.date}\nLinks: ${chips.map((c) => c.name).join(", ") || "(only you)"}\n\n${text.slice(0, 400)}${text.length > 400 ? "..." : ""}`
    : `Add this task (assigned to you)?\n\nPriority: ${body.priority}\nLinks: ${chips.map((c) => c.name).join(", ") || "(none)"}\n\n${text}`;
  if (!confirm(summary)) return;
  CARD.text = text;
  await api("/api/ledger/commit", { method: "POST", body });
  CARD.state = "committing";
  $("#lc-commit").disabled = true; $("#lc-ai").disabled = true; $("#lc-text").disabled = true;
  $$("#lc-chips input").forEach((i) => { i.disabled = true; });
  pollCommit();
}

function pollCommit() {
  const box = $("#lc-progress"); box.hidden = false;
  const id = CARD.id;
  const tick = async () => {
    if (!CARD || CARD.id !== id) return;
    let p;
    try { p = await api(`/api/ledger/commit/${id}`); } catch { setTimeout(tick, 1500); return; }
    box.innerHTML = p.steps.map((s) => `<div class="step ${s.ok === true ? "ok" : s.ok === false ? "bad" : ""}"><span class="mono dim">${s.t}s</span> ${esc(s.text)}</div>`).join("");
    if (p.state === "running") { setTimeout(tick, 1200); return; }
    CARD.state = p.state === "done" ? "committed" : "failed";
    CARD.result = p.result;
    if (p.result?.id) {
      const linked = new Set(p.result.linked || []);
      CARD.chips.forEach((c) => { if (c.checked) c.state = linked.has(c.id) ? "linked" : "missing"; });
      renderCard(); showResult();
      try { await navigator.clipboard.writeText(p.result.id); } catch { /* clipboard blocked */ }
      toast(`${p.result.kind === "task" ? "Task" : "Interaction"} ${p.result.id.slice(0, 8)} saved. Id copied.`, "ok");
      loadBucket();
      document.dispatchEvent(new CustomEvent("ultra:ledger-written"));
    } else {
      renderCard(); showResult();
      toast("Nothing was saved. See the card for details.", "err");
    }
  };
  tick();
}

function showResult() {
  const r = CARD.result, box = $("#lc-progress"); if (!r) return;
  box.hidden = false;
  const steps = box.innerHTML;
  const missing = (r.missing || []).length;
  box.innerHTML = (r.id
    ? `<div class="lc-done ${missing ? "warn" : "ok"}">${r.kind === "task" ? "Task" : "Interaction"} <span class="mono">${esc(r.id)}</span>
        ${missing ? `<br>${missing} link(s) missing: use Link now on the red chips.` : "<br>All links confirmed by read-back."}
        ${r.ai_error ? "<br>The ledger's summarizer failed, so the full text is the summary." : ""}
        ${(r.extra || []).length ? `<br>The ledger also linked: ${r.extra.map((x) => esc(x.name)).join(", ")}. Check these are right.` : ""}
        ${r.summary ? `<br><span class="dim">Ledger summary: ${esc(r.summary)}</span>` : ""}</div>`
    : `<div class="lc-done bad">Not saved. ${esc(r.error || "")}${r.output ? `<pre>${esc(r.output)}</pre>` : ""}<br>Nothing was retried, so there is no duplicate. Check the ledger before trying again.</div>`)
    + (steps.includes("step") ? `<details><summary class="dim small-t">steps</summary>${steps.replace(/<div class="lc-done[\s\S]*$/, "")}</details>` : "");
}
