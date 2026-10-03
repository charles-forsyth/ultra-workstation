// Context rail (People / Person / Search tabs), the bucket, and staged ledger cards.
// Every ledger write goes: stage -> operator edits -> Commit click -> server runs the
// CLI -> read-back -> chips turn green or red. Nothing here writes on its own.

import { api, esc, toast, busy, copyText, localDay } from "./app.js";
import { setComposerContext } from "./compose.js";
import { renderMd } from "./tools.js";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const R = { key: null, item: null, tab: "people", people: [], person: null, bucket: [], confirmed: [], ents: null, aiSuggest: [], extraEnts: [] };

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
  if (tab === "tools") $("#tw-q")?.focus();
}

// ---------------------------------------------------------------- people tab (v0.8)
// Everyone in the item: header addresses AND people named anywhere in the bodies (or a
// task's text), matched exactly against the ledger. Possible matches are listed apart
// and join only when confirmed. The Full tab loads the item's context on its own.
export async function loadPeople(it) {
  R.key = it.key; R.item = it; R.person = null; R.confirmed = []; R.ents = null;
  setComposerContext("");
  const el = $("#rt-people");
  el.innerHTML = `<div class="dim">Reading the whole ${it.source === "task" ? "task" : "conversation"} for people and ledger records...</div>`;
  $("#rt-person").innerHTML = `<div class="dim">Pick someone on the People tab.</div>`;
  loadItemFull(it.key);  // runs by itself (about 10-50 s the first time, then cached)
  let r;
  try { r = await api(`/api/item/people/${encodeURIComponent(it.key)}`); }
  catch (e) { el.innerHTML = `<div class="dim">${esc(e.message)}</div>`; return; }
  if (R.key !== it.key) return;
  R.ents = r;
  R.people = r.people || [];
  renderPeople();
  loadAiSuggestions(it.key);
  const first = R.people.find((p) => p.in_ledger && (p.addrs || []).length) || R.people.find((p) => (p.addrs || []).length);
  if (first) openPerson({ addr: first.addrs[0], name: first.name }, false);
}

function renderPeople() {
  const r = R.ents || {}, el = $("#rt-people");
  $('#rail-tabs button[data-tab="people"]').textContent = `People ${R.people.length || ""}`.trim();
  if (r.unavailable) { el.innerHTML = `<div class="dim">Ledger unavailable: ${esc(r.reason || "")}</div>`; return; }
  const roleTxt = (p) => {
    const bits = [];
    if (p.roles.includes("from")) bits.push(`wrote${p.sent > 1 ? ` (${p.sent})` : ""}`);
    else if (p.roles.includes("to")) bits.push("to");
    else if (p.roles.includes("cc")) bits.push("cc");
    if (p.named_in?.length) bits.push(`named in message ${p.named_in.join(", ")}`);
    else if (p.source === "task") bits.push("named in the task");
    else if (p.source === "confirmed") bits.push("confirmed by you");
    return bits.join(" &middot; ");
  };
  const rows = R.people.map((p, i) => {
    const tag = p.in_ledger ? `<span class="badge ok">${esc(p.netid)}</span>` : `<span class="badge dim">not in ledger</span>`;
    const dos = p.dossier ? "" : p.in_ledger ? `<span class="badge dim" title="Only the first 10 people get a full dossier in the Full tab">listed</span>` : "";
    const add = !p.in_ledger && (p.addrs || []).length ? `<button class="btn tiny" data-addp="${i}" title="Add this person to the ledger (checked, then confirmed twice)">Add to ledger</button>` : "";
    return `<div class="prow" data-i="${i}" draggable="${p.in_ledger ? "true" : "false"}" tabindex="0">
      <div class="pmain"><b>${esc(p.name || p.addrs?.[0] || "")}</b> ${tag} ${dos}</div>
      <div class="dim small-t">${esc((p.addrs || []).join(", "))}${(p.addrs || []).length ? " &middot; " : ""}${roleTxt(p)}${p.title ? ` &middot; ${esc(p.title)}` : ""}</div>
      ${add ? `<div class="pacts">${add}</div>` : ""}
    </div>`;
  }).join("");
  const th = r.things || {};
  const thingList = [["Labs", th.labs], ["GCP projects", th.gcp], ["Projects", th.projects], ["Grants", th.grants], ["Assets", th.assets]]
    .filter(([, xs]) => xs?.length)
    .map(([label, xs]) => `<div class="small-t"><span class="label">${label}</span> ${xs.map((x) => `<span class="chip-s" title="${esc(x.label || "")}">${esc(x.name)}</span>`).join(" ")}</div>`).join("");
  el.innerHTML = `
    ${R.people.length ? `<div class="plist">${rows}</div>` : `<div class="dim">No one but you on this one.</div>`}
    ${thingList ? `<div class="sect"><span class="label">Named in the text</span>${thingList}</div>` : ""}
    <div id="pp-suggest">${suggestHtml()}</div>
    <div class="dim small-t hint">Everyone in the headers and everyone named in the text. Click a person for their card; the Full tab has the whole group's history.</div>`;
  el.onclick = (e) => {
    const a = e.target.closest("[data-addp]");
    if (a) { e.stopPropagation(); addPersonDialog(R.people[Number(a.dataset.addp)]); return; }
    const s = e.target.closest("[data-sug]");
    if (s) { e.stopPropagation(); confirmSuggestion(s.dataset.sug, s.dataset.id, s.dataset.name, s.dataset.type); return; }
    const row = e.target.closest(".prow"); if (!row) return;
    const p = R.people[Number(row.dataset.i)];
    if ((p.addrs || []).length) openPerson({ addr: p.addrs[0], name: p.name });
    else if (p.netid) openPerson({ addr: `${p.netid}@${R.domain || "ledger"}`, name: p.name, netid: p.netid });
  };
  el.onkeydown = (e) => { if (e.key === "Enter") { const row = e.target.closest(".prow"); if (row) row.click(); } };
  el.ondragstart = (e) => {
    const row = e.target.closest(".prow"); if (!row) return;
    const p = R.people[Number(row.dataset.i)];
    if (!p.in_ledger || !p.id) { e.preventDefault(); return; }
    e.dataTransfer.setData("application/x-ultra-entity", JSON.stringify({ id: p.id, name: p.name, type: "Researcher" }));
  };
}

// possible matches: exact-rule suggestions (shared name, nickname, surname only) and
// AI suggestions (picked only from ledger search results). Nothing joins until confirmed.
function suggestHtml() {
  const rule = (R.ents?.suggest || []);
  const ai = R.aiSuggest || [];
  if (!rule.length && !ai.length) return "";
  const r1 = rule.map((s) => `<div class="sugg"><span class="badge warn">possible</span> "${esc(s.text)}" <span class="dim small-t">(${esc(s.why)})</span>
      ${s.options.map((o) => `<button class="btn tiny" data-sug="person" data-id="${esc(o.id)}" data-name="${esc(o.name)}" title="Confirm: add ${esc(o.name)} to this item's group">${esc(o.name)}?</button>`).join(" ")}</div>`).join("");
  const r2 = ai.map((s) => `<div class="sugg"><span class="badge ai">AI possible</span> ${esc(s.type)}: <b>${esc(s.name)}</b> <span class="dim small-t">${esc(s.why)}</span>
      <button class="btn tiny" data-sug="entity" data-id="${esc(s.id)}" data-name="${esc(s.name)}" data-type="${esc(s.type)}">Confirm</button></div>`).join("");
  // calm layout: one line ("2 possible matches"), opened on click; classic shows them open
  const n = rule.length + ai.length;
  const open = document.body.classList.contains("calm") ? "" : " open";
  return `<details class="sect sugg-wrap"${open}><summary class="label">${n} possible match${n === 1 ? "" : "es"} (confirm to include)</summary>${r1}${r2}</details>`;
}

async function loadAiSuggestions(key) {
  try {
    const r = await api("/api/item/suggest", { method: "POST", body: { key } });
    if (R.key !== key) return;
    R.aiSuggest = r.suggest || [];
    const box = $("#pp-suggest"); if (box) box.innerHTML = suggestHtml();
  } catch { /* suggestions are optional */ }
}

function confirmSuggestion(kind, id, name, type) {
  if (kind === "person") {
    if (!R.confirmed.includes(id)) R.confirmed.push(id);
    R.ents.suggest = (R.ents.suggest || []).filter((s) => !s.options.some((o) => o.id === id));
    toast(`${name} added to this item's group. Rebuilding the Full context...`, "ok");
  } else {
    R.aiSuggest = (R.aiSuggest || []).filter((s) => s.id !== id);
    R.extraEnts = [...(R.extraEnts || []), { id, name, type }];
    toast(`${name} will be linked when you save the briefing to the ledger.`, "ok");
  }
  renderPeople();
  if (kind === "person") loadItemFull(R.key, true);
}

// ---------------------------------------------------------------- add to ledger
// Strict checks on the server, then two separate confirmations (like sending mail).
// The only path in Ultra that may run `people add`.
function addPersonDialog(p) {
  const addr = (p.addrs || [])[0] || "";
  const local = addr.split("@")[0] || "";
  const guessNetid = /^[a-z][a-z0-9]{1,15}$/.test(local) ? local : "";
  const nm = String(p.name || "").replace(/\s*\([^)]*\)\s*$/, "").replace(/[^A-Za-z' .-]/g, " ").replace(/\s+/g, " ").trim();
  const dlg = $("#ledger-card");
  dlg.hidden = false;
  const render = (step, f, warnings = [], err = "") => {
    const locked = step > 1;
    dlg.innerHTML = `<div class="lc-card" role="dialog" aria-modal="true" aria-labelledby="pa-head">
      <div class="lc-head"><h3 id="pa-head">Add to ledger ${step === 1 ? "" : `<span class="badge warn">step ${step - 1} of 2</span>`}</h3><span class="dim small-t">${esc(addr)}</span><span class="grow"></span><button class="btn tiny ghost" id="pa-x">Cancel</button></div>
      <div class="lc-grid">
        <label for="pa-netid">NetID</label><input id="pa-netid" value="${esc(f.netid)}" ${locked ? "disabled" : ""} placeholder="lowercase letters and digits" autocomplete="off">
        <label for="pa-name">Name</label><input id="pa-name" value="${esc(f.name)}" ${locked ? "disabled" : ""} placeholder="First Last" autocomplete="off">
        <label for="pa-jobtitle">Title</label><input id="pa-jobtitle" value="${esc(f.title || "")}" ${locked ? "disabled" : ""} placeholder="optional">
        <label for="pa-dept">Dept</label><input id="pa-dept" value="${esc(f.dept || "")}" ${locked ? "disabled" : ""} placeholder="optional">
      </div>
      ${warnings.map((w) => `<div class="warnline">${esc(w)}</div>`).join("")}
      ${err ? `<div class="lint error">${esc(err)}</div>` : ""}
      ${step === 2 ? `<div class="dim small-t">Checked: nobody in the ledger has this NetID. Confirm once to continue.</div>` : ""}
      ${step === 3 ? `<div class="lint warning">Last check. This creates <b>${esc(f.name)} (${esc(f.netid)})</b> in the ledger. It cannot be undone from Ultra.</div>` : ""}
      <div class="lc-row"><span class="grow"></span>
        ${step === 1 ? `<button class="btn small primary" id="pa-go">Check</button>` : ""}
        ${step === 2 ? `<button class="btn small" id="pa-back">Edit</button><button class="btn small primary" id="pa-go">Confirm (1 of 2)</button>` : ""}
        ${step === 3 ? `<button class="btn small primary" id="pa-no" autofocus>Not yet</button><button class="btn small danger" id="pa-go" disabled>Add to ledger (2 of 2)</button>` : ""}
      </div></div>`;
    const close = () => { dlg.hidden = true; dlg.innerHTML = ""; };
    $("#pa-x").onclick = close;
    if ($("#pa-no")) $("#pa-no").onclick = close;
    if ($("#pa-back")) $("#pa-back").onclick = () => render(1, f);
    dlg.onkeydown = (e) => { if (e.key === "Escape") close(); };
    const read = () => ({ netid: $("#pa-netid").value.trim(), name: $("#pa-name").value.trim(), title: $("#pa-jobtitle").value.trim(), dept: $("#pa-dept").value.trim() });
    const go = $("#pa-go");
    if (step === 3) setTimeout(() => { if (go.isConnected) go.disabled = false; }, 2000);  // no reflex clicks
    go.onclick = () => busy(go, async () => {
      try {
        if (step === 1) {
          const v = read();
          const bad = !/^[a-z][a-z0-9]{1,15}$/.test(v.netid) ? "NetID must be 2-16 lowercase letters/digits, starting with a letter."
            : !/^[A-Z][A-Za-z'.-]*( [A-Za-z][A-Za-z'.-]*){1,4}$/.test(v.name) ? "Name must be First Last (2-5 words, letters only)." : "";
          if (bad) { render(1, v, [], bad); return; }  // same rules the server enforces
          const r = await api("/api/person/add/check", { method: "POST", body: { ...v, addr } });
          f = { ...r.fields, token: r.token }; render(2, f, r.warnings);
        } else if (step === 2) {
          const r = await api("/api/person/add/confirm", { method: "POST", body: { ...f } });
          f = { ...r.fields, token: r.token }; render(3, f);
        } else {
          const r = await api("/api/person/add/commit", { method: "POST", body: { ...f } });
          if (r.ok) {
            close();
            toast(`Added ${r.record?.name || f.name} (${f.netid}) to the ledger. Read back OK.`, "ok");
            loadPeople(R.item);
          } else {
            render(1, f, [], `The ledger did not confirm the add: ${r.output_tail || "unknown"}`);
          }
        }
      } catch (e) { render(1, { ...f, token: "" }, [], e.message); }
    });
  };
  render(1, { netid: guessNetid, name: nm, title: "", dept: "" });
}

export function openPersonByAddr(p) {
  R.key = null;
  return openPerson({ addr: p.addr, name: p.name || "" });
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
    <div class="pacts"><button class="btn tiny primary" data-pa="full" title="Everything the ledger has on them: all logs, mentions, tasks, labs, projects, assets">Full context</button><button class="btn tiny" data-pa="bucket">Add to bucket</button><button class="btn tiny ghost" data-pa="copy">Copy id</button></div>
    <div class="sect"><span class="label">Labs</span>${list(c.lab_refs || [], (x) => ent(x, "Lab"))}</div>
    <div class="sect"><span class="label">Projects</span>${list(c.project_refs || [], (x) => ent(x, "Project"))}</div>
    <div class="sect"><span class="label">Open tasks ${tasks.length}</span>${list(tasks, (x) => `<li class="task" data-id="${esc(x.id)}"><span class="pri ${esc((x.priority || "").toLowerCase())}">${esc((x.priority || "").slice(0, 1))}</span> ${esc(x.summary)} <button class="btn tiny ghost" data-done="${esc(x.id)}" title="Mark done in the ledger">Done</button></li>`)}</div>
    <div class="sect"><span class="label">Recent logs ${esc(c.interaction_count ?? "")}</span>${list(c.interactions || [], (x) => ent(x, "Interaction"))}</div>`;
  $('[data-pa="bucket"]', el).onclick = () => addEntity({ id: c.id, name: c.name, type: "Researcher" });
  $('[data-pa="full"]', el).onclick = () => openFull({ addr: p.addr, name: p.name || c.name });
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

// ---------------------------------------------------------------- item full context (v0.8)
// The whole group behind one stream item: dossiers for the first 10 people, trees for
// the labs/projects/grants named in it, one merged history (ledger + this item's
// messages), open tasks, and an AI briefing that cites its sources. Runs by itself when
// the item opens.
let IFULL = null, IBRIEF = null, IJOB = 0;
export async function loadItemFull(key, fresh = false) {
  const el = $("#rt-full");
  const my = ++IJOB;
  IFULL = null; IBRIEF = null;
  $('#rail-tabs button[data-tab="full"]').textContent = "Full ...";
  el.innerHTML = `<div class="dim">Gathering the whole group behind this item: everyone's dossier, the labs and projects it names, every related log, and open tasks... <span id="if-sec"></span></div>`;
  let job;
  try { job = await api("/api/item/full", { method: "POST", body: { key, confirmed: R.confirmed || [], fresh } }); }
  catch (e) { if (my === IJOB) el.innerHTML = `<div class="dim">${esc(e.message)}</div>`; return; }
  for (let n = 0; n < 240; n++) {
    await new Promise((r) => setTimeout(r, n < 5 ? 600 : 1500));
    if (my !== IJOB) return;
    let s;
    try { s = await api(`/api/item/full/job/${job.job}`); } catch { continue; }
    const sec = $("#if-sec"); if (sec) sec.textContent = `(${Math.round(s.seconds)} s)`;
    if (s.state === "running") continue;
    if (s.state === "error") { el.innerHTML = `<div class="lint error">${esc(s.error || "failed")}</div>`; $('#rail-tabs button[data-tab="full"]').textContent = "Full"; return; }
    IFULL = s.result;
    renderItemFull();
    loadBriefing(key);
    return;
  }
}

function renderItemFull() {
  const c = IFULL, el = $("#rt-full");
  if (!c) return;
  $('#rail-tabs button[data-tab="full"]').textContent = `Full ${c.counts.history || ""}`.trim();
  const g = c.group || {};
  const ent = (x, type) => `<li class="ent" ${x.id ? `draggable="true" data-ent='${esc(JSON.stringify({ id: x.id, name: x.name, type }))}'` : ""} title="${x.via ? "via " + esc(x.via.join(", ")) : ""}">${esc(x.name)}${x.links > 40 ? ` <span class="badge dim" title="${esc(x.links)} links: a shared platform; its full history is not merged in">platform</span>` : ""}</li>`;
  const sect = (label, xs, f, open = true) => xs?.length ? `<details class="fsect" ${open ? "open" : ""}><summary class="label">${label} ${xs.length}</summary><ul class="clist">${xs.map(f).join("")}</ul></details>` : "";
  const hist = (h) => `<li class="ix ${h.kind === "item" ? "itemmsg" : ""}" data-ix="${esc(h.id)}">
      <span class="mono dim small-t">${esc(h.date ? h.date.replace("T", " ") : "undated")}</span>
      <span class="badge ${h.kind === "item" ? "vip" : "dim"}">${h.kind === "item" ? esc(h.why[0] || "this item") : "ledger"}</span>
      ${h.who?.length ? `<span class="dim small-t">${esc(h.who.slice(0, 3).join(", "))}</span>` : ""}
      <div class="ix-t">${esc(h.summary)}</div></li>`;
  const pp = (c.people || []).map((p) => `<li>${esc(p.name)} ${p.netid ? `<span class="dim mono small-t">${esc(p.netid)}</span>` : `<span class="badge dim">not in ledger</span>`} ${p.dossier ? "" : p.in_ledger ? `<span class="dim small-t">(no dossier: over 10)</span>` : ""}</li>`);
  el.innerHTML = `
    <div class="cname">${esc(c.subject || "This item")}</div>
    <div class="dim small-t">${c.counts.people} people (${c.counts.dossiers_loaded} dossiers), ${c.counts.history} ledger logs, ${c.counts.tasks} open tasks${c.age ? ` &middot; from ${Math.round(c.age / 60)} min ago` : ""}</div>
    <div class="pacts">
      <button class="btn tiny" data-fa="copy" title="Copy the whole context as text">Copy all</button>
      <button class="btn tiny ai" data-fa="compose" title="Give all of this to the AI when drafting a reply">Use for AI draft</button>
      <button class="btn tiny ghost" data-fa="fresh" title="Re-read everything from the ledger now">&#8635;</button>
    </div>
    <div id="if-brief" class="ibrief"><div class="dim small-t">Writing the briefing...</div></div>
    ${c.broad?.length ? `<div class="dim small-t">Shared platforms named (listed, history not merged): ${c.broad.map((b) => `${esc(b.name)} (${esc(b.links)} links)`).join(", ")}</div>` : ""}
    ${sect("Open tasks", c.tasks, (t) => `<li class="task" data-id="${esc(t.id)}"><span class="pri ${esc((t.priority || "").toLowerCase())}">${esc((t.priority || "").slice(0, 1))}</span> ${esc(t.summary)} <span class="badge dim" title="${esc(t.why.join(", "))}">${esc(t.why[0] || "")}</span></li>`)}
    ${sect("People", pp, (x) => x, false)}
    ${sect("Labs", g.labs, (x) => ent(x, "Lab"), false)}
    ${sect("GCP projects", g.gcp_projects, (x) => ent(x, "GCPProject"), false)}
    ${sect("Projects", g.projects, (x) => ent(x, "ResearchProject"), false)}
    ${sect("Grants", g.grants, (x) => ent(x, "Grant"), false)}
    ${sect("Assets", g.assets, (x) => ent(x, "Asset"), false)}
    ${sect("History, newest first", c.history, hist)}`;
  $("#context").scrollTop = 0;
  el.ondragstart = (e) => { const li = e.target.closest("[data-ent]"); if (li) e.dataTransfer.setData("application/x-ultra-entity", li.dataset.ent); };
  el.onclick = (e) => {
    const cite = e.target.closest("[data-cite]");
    if (cite) { jumpTo(cite.dataset.cite); return; }
    const li = e.target.closest("li.ix"); if (li && !e.target.closest("button")) { li.classList.toggle("open"); return; }
    const b = e.target.closest("[data-fa],[data-ba]"); if (!b) return;
    const a = b.dataset.fa || b.dataset.ba;
    if (a === "copy") copyText(itemText(c, IBRIEF));
    else if (a === "compose") { setComposerContext(itemText(c, IBRIEF)); toast("The AI will use all of it for drafts on this item (tick 'ledger context').", "ok"); }
    else if (a === "fresh") loadItemFull(c.key, true);
    else if (a === "regen") loadBriefing(c.key, true);
    else if (a === "edit") editBriefing();
    else if (a === "listen") listenBriefing();
    else if (a === "save") saveBriefing(b);
  };
  if (IBRIEF) renderBriefing();
}

function jumpTo(ref) {
  const id = ref.startsWith("M") ? `${IFULL.key}#${ref.slice(1)}` : ref;
  const li = $$("#rt-full li.ix").find((x) => x.dataset.ix === id || x.dataset.ix.startsWith(ref));
  if (!li) { toast(`Source ${ref} is not in the list.`, "err"); return; }
  li.closest("details").open = true; li.classList.add("open", "flash");
  li.scrollIntoView({ block: "center", behavior: "smooth" });
  setTimeout(() => li.classList.remove("flash"), 1600);
}

async function loadBriefing(key, fresh = false) {
  const box = $("#if-brief"); if (!box) return;
  box.innerHTML = `<div class="dim small-t">Writing the briefing from everything above... (10-20 s)</div>`;
  try {
    const r = await api("/api/item/briefing", { method: "POST", body: { key, confirmed: R.confirmed || [], fresh } });
    if (!IFULL || IFULL.key !== key) return;
    IBRIEF = { ...r, text: r.markdown };
    renderBriefing();
  } catch (e) { box.innerHTML = `<div class="dim small-t">Briefing unavailable: ${esc(e.message)}</div>`; }
}

function citeLinks(html) {
  // [L:abcd1234], [T:abcd1234], [M3] and lists like [M1, L:abcd1234] -> clickable chips
  return html.replace(/\[((?:(?:L|T):[0-9a-f]{8}|M\d{1,3})(?:,\s*(?:(?:L|T):[0-9a-f]{8}|M\d{1,3}))*)\]/g, (m, inner) =>
    inner.split(/,\s*/).map((ref) => {
      const r = ref.replace(/^[LT]:/, "");
      return `<button class="cite" data-cite="${esc(r)}" title="Show the source">${esc(ref)}</button>`;
    }).join(""));
}

function renderBriefing() {
  const box = $("#if-brief"); if (!box || !IBRIEF) return;
  box.innerHTML = `<div class="aisum-head"><span class="badge ai">AI briefing</span>
      <span class="dim mono small-t">${esc(IBRIEF.model || "")}${IBRIEF.edited ? " &middot; edited" : ""}${IBRIEF.cached ? " &middot; cached" : ""}</span>
      <span class="grow"></span>
      <button class="btn tiny" data-ba="edit">Edit</button>
      <button class="btn tiny" data-ba="listen" title="Read it aloud">Listen</button>
      <button class="btn tiny primary" data-ba="save" title="Save as a ledger log, after you review it on the card">Save to ledger</button>
      <button class="btn tiny ghost" data-ba="regen" title="Write it again">&#8635;</button></div>
    <div class="md brief-md">${citeLinks(renderMd(IBRIEF.text))}</div>`;
}

function editBriefing() {
  const box = $("#if-brief");
  box.innerHTML = `<div class="aisum-head"><span class="badge ai">AI briefing</span><span class="dim small-t">editing</span><span class="grow"></span>
      <button class="btn tiny primary" id="ib-done">Done</button><button class="btn tiny ghost" id="ib-cancel">Cancel</button></div>
    <textarea id="ib-text" rows="18">${esc(IBRIEF.text)}</textarea>`;
  $("#ib-done").onclick = (e) => { e.stopPropagation(); IBRIEF.text = $("#ib-text").value; IBRIEF.edited = true; renderBriefing(); };
  $("#ib-cancel").onclick = (e) => { e.stopPropagation(); renderBriefing(); };
  $("#ib-text").focus();
}

function listenBriefing() {
  const plain = IBRIEF.text.replace(/\[[^\]]*\]/g, "").replace(/^#+\s*/gm, "").replace(/^\s*[-*]\s+/gm, "");
  window.dispatchEvent(new CustomEvent("ultra:listen", { detail: { text: plain, title: `Briefing: ${IFULL.subject || ""}` } }));
}

async function saveBriefing(btn) {
  const c = IFULL;
  const ents = [
    ...(c.people || []).filter((p) => p.in_ledger && p.id).map((p) => ({ id: p.id, name: p.name, type: "Researcher" })),
    ...["labs", "gcp_projects", "projects", "grants"].flatMap((k) => (c.group?.[k] || []).filter((x) => x.id && (x.via || []).includes("named in the item")).map((x) => ({ id: x.id, name: x.name, type: k }))),
    ...(R.extraEnts || []),
  ];
  const text = `Briefing: ${c.subject || ""}\n\n` + IBRIEF.text.replace(/\s*\[(?:(?:L|T):[0-9a-f]{8}|M\d{1,3})(?:,\s*(?:(?:L|T):[0-9a-f]{8}|M\d{1,3}))*\]/g, "");
  const dlg = $("#ledger-card");
  busy(btn, async () => {
    dlg.hidden = false;
    dlg.innerHTML = `<div class="lc-card"><div class="dim">Building the log entry...</div></div>`;
    try {
      CARD = await api("/api/ledger/stage-briefing", { method: "POST", body: { key: c.key.startsWith("t-") ? "" : c.key, text, entities: ents } });
    } catch (e) { dlg.hidden = true; toast(e.message, "err"); return; }
    renderCard();
  });
}

function itemText(c, brief) {
  const L = [`Stream item: ${c.subject || ""}`];
  if (brief?.text) { L.push("", "Briefing:", brief.text, ""); }
  L.push("People: " + (c.people || []).map((p) => `${p.name}${p.netid ? ` (${p.netid})` : ""}`).join("; "));
  const g = c.group || {};
  for (const [label, k] of [["Labs", "labs"], ["GCP projects", "gcp_projects"], ["Projects", "projects"], ["Grants", "grants"], ["Assets", "assets"]]) {
    if (g[k]?.length) L.push(`${label}: ${g[k].map((x) => x.name).join("; ")}`);
  }
  if (c.tasks?.length) { L.push("Open tasks:"); c.tasks.forEach((t) => L.push(`- [${t.priority}/${t.status}] ${t.summary}`)); }
  L.push("History (newest first):");
  (c.history || []).forEach((h) => L.push(`- ${h.date || "undated"} (${h.kind === "item" ? "this item" : "ledger"}; ${(h.who || []).join(", ")}): ${h.summary}`));
  return L.join("\n");
}

// ---------------------------------------------------------------- full context tab
let FULL = null;
export async function openFull(p, fresh = false) {
  const el = $("#rt-full");
  showTab("full");
  el.innerHTML = `<div class="dim">Gathering everything on ${esc(p.name || p.addr)}: their dossier, every linked log, logs that mention them, and related tasks... (about 10 seconds; then cached for 15 minutes)</div>`;
  let c;
  try { c = await api(`/api/person/full?addr=${encodeURIComponent(p.addr)}&name=${encodeURIComponent(p.name || "")}${fresh ? "&fresh=1" : ""}`); }
  catch (e) { el.innerHTML = `<div class="dim">${esc(e.message)}</div>`; return; }
  if (c.unavailable) { el.innerHTML = `<div class="dim">Ledger unavailable: ${esc(c.reason || "")}</div>`; return; }
  if (c.unresolved) { el.innerHTML = `<div class="dim">${esc(p.addr)} is not in the ledger.</div>`; return; }
  FULL = { ...c, addr: p.addr };
  renderFull();
}

function renderFull() {
  const c = FULL, el = $("#rt-full");
  const ent = (x, type) => `<li class="ent" draggable="true" data-ent='${esc(JSON.stringify({ id: x.id, name: x.name, type: x.entity_type || type }))}' title="Drag into the bucket to link">${esc(x.name)}${x.role ? ` <span class="dim small-t">${esc(x.role)}</span>` : ""}</li>`;
  const sect = (label, xs, f, open = true) => xs?.length ? `<details class="fsect" ${open ? "open" : ""}><summary class="label">${label} ${xs.length}</summary><ul class="clist">${xs.map(f).join("")}</ul></details>` : "";
  const ix = (i) => `<li class="ix${i.linked ? "" : " unlinked"}" data-ix="${esc(i.id)}">${i.date ? `<span class="mono dim small-t">${esc(i.date)}</span> ` : ""}<span class="ix-t">${esc(i.summary)}</span>${i.linked ? "" : ` <span class="badge dim" title="${esc(i.reason || "found by search")}">mentions</span>`}</li>`;
  el.innerHTML = `
    <div class="cname">${esc(c.name)} <span class="dim mono">${esc(c.netid)}</span></div>
    <div class="dim">${esc([c.title, c.dept].filter(Boolean).join(" - "))}</div>
    ${c.matched_by === "name" ? `<div class="warnline">Matched by name, not address: check it's the right person.</div>` : ""}
    <div class="dim small-t">${c.counts.linked} logs linked, ${c.counts.mentions} more mention them, ${c.counts.tasks} open tasks${c.age ? ` &middot; from ${Math.round(c.age / 60)} min ago` : ""}</div>
    <div class="pacts">
      <button class="btn tiny" data-fa="copy" title="Copy all of it as text">Copy all</button>
      <button class="btn tiny ai" data-fa="compose" title="Give all of this to the AI when drafting">Use for AI draft</button>
      <button class="btn tiny" data-fa="bucket">Add to bucket</button>
      <button class="btn tiny ghost" data-fa="fresh" title="Re-read from the ledger now">&#8635;</button>
    </div>
    ${sect("Open tasks", c.tasks, (t) => `<li class="task" data-id="${esc(t.id)}"><span class="pri ${esc((t.priority || "").toLowerCase())}">${esc((t.priority || "").slice(0, 1))}</span> ${esc(t.summary)} <span class="badge dim">${esc(t.why === "linked" ? "linked" : t.why === "search" ? "related" : "mentions")}</span></li>`)}
    ${sect("Labs", c.labs, (x) => ent(x, "Lab"))}
    ${sect("Projects", c.projects, (x) => ent(x, "ResearchProject"))}
    ${sect("GCP projects", c.gcp_projects, (x) => ent(x, "GCPProject"))}
    ${c.cloud?.length ? `<div class="dim small-t">Cloud audit: ${c.cloud.map((x) => `${esc(x.project)} (scanned ${esc(x.scanned)})`).join(", ")}</div>` : ""}
    ${sect("Grants", c.grants, (x) => ent(x, "Grant"))}
    ${sect("Assets", c.assets, (x) => ent(x, "Asset"), false)}
    ${sect("People and labs connected", c.connections, (x) => ent(x, x.entity_type), false)}
    ${sect("All logged interactions, newest first", c.interactions, ix)}
    ${sect("Other logs that mention them (not linked)", c.mentions, ix)}
    ${!c.tasks.length && !c.interactions.length && !c.mentions.length ? `<div class="dim">Nothing logged yet.</div>` : ""}`;
  el.ondragstart = (e) => { const li = e.target.closest("[data-ent]"); if (li) e.dataTransfer.setData("application/x-ultra-entity", li.dataset.ent); };
  el.onclick = (e) => {
    const li = e.target.closest("li.ix"); if (li && !e.target.closest("button")) { li.classList.toggle("open"); return; }
    const b = e.target.closest("[data-fa]"); if (!b) return;
    const a = b.dataset.fa;
    if (a === "copy") copyText(fullText(c));
    else if (a === "bucket") addEntity({ id: c.id, name: c.name, type: "Researcher" });
    else if (a === "fresh") openFull({ addr: c.addr, name: c.name }, true);
    else if (a === "compose") { setComposerContext(fullText(c)); toast("The AI will use all of it for drafts on this conversation (tick 'ledger context').", "ok"); }
  };
}

function fullText(c) {
  const L = [`${c.name} (${c.netid})${c.title ? ", " + c.title : ""}${c.dept ? ", " + c.dept : ""}`];
  const names = (xs) => (xs || []).map((x) => x.name).join("; ");
  if (c.labs?.length) L.push(`Labs: ${names(c.labs)}`);
  if (c.projects?.length) L.push(`Projects: ${names(c.projects)}`);
  if (c.gcp_projects?.length) L.push(`GCP projects: ${names(c.gcp_projects)}`);
  if (c.grants?.length) L.push(`Grants: ${names(c.grants)}`);
  if (c.assets?.length) L.push(`Assets: ${names(c.assets)}`);
  if (c.tasks?.length) { L.push("Open tasks:"); c.tasks.forEach((t) => L.push(`- [${t.priority}/${t.status}] ${t.summary}`)); }
  if (c.interactions?.length) { L.push("Logged interactions (newest first):"); c.interactions.forEach((i) => L.push(`- ${i.date}: ${i.summary}`)); }
  if (c.mentions?.length) { L.push("Other logs that mention them:"); c.mentions.forEach((i) => L.push(`- ${i.summary}`)); }
  return L.join("\n");
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
  $("#bucket").classList.toggle("has", R.bucket.length > 0);  // calm layout: pill only when not empty
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

// ---------------------------------------------------------------- task helpers
export function showTaskPeople(links) {
  R.key = null; R.people = [];
  const el = $("#rt-people");
  const people = links.filter((x) => x.type === "Researcher");
  $('#rail-tabs button[data-tab="people"]').textContent = `People ${people.length || ""}`.trim();
  el.innerHTML = people.length ? `<div class="plist">${people.map((p) => `<div class="prow ent" draggable="true" data-ent='${esc(JSON.stringify({ id: p.id, name: p.name, type: "Researcher" }))}'><b>${esc(p.name)}</b> <span class="dim small-t">${esc(p.edge)}</span></div>`).join("")}</div>
    <div class="dim small-t hint">People linked to this task. Drag one into the bucket to link a log to them.</div>` : `<div class="dim">No people linked to this task.</div>`;
  el.onclick = null;
  el.ondragstart = (e) => { const r = e.target.closest("[data-ent]"); if (r) e.dataTransfer.setData("application/x-ultra-entity", r.dataset.ent); };
}

export async function stageTaskLog(task, links) {
  // Log progress on a task: a card with the task and its people as chips.
  const dlg = $("#ledger-card");
  dlg.hidden = false;
  dlg.innerHTML = `<div class="lc-card"><div class="dim">Building the log entry...</div></div>`;
  try {
    CARD = await api("/api/ledger/stage-task-log", { method: "POST", body: { task: task.id, summary: task.summary, links } });
  } catch (e) { dlg.hidden = true; toast(e.message, "err"); return; }
  renderCard();
}

// ---------------------------------------------------------------- staged card
let CARD = null;

export async function stage(action, key = null, sent = false) {
  if (action === "block") {
    const conv = key ? { key, subject: "" } : R.bucket.find((b) => b.kind === "conversation");
    window.dispatchEvent(new CustomEvent("ultra:block", { detail: conv ? { key: conv.key, subject: conv.subject || "" } : { subject: R.bucket[0]?.subject || "follow-up" } }));
    return;
  }
  const dlg = $("#ledger-card");
  dlg.hidden = false;
  dlg.innerHTML = `<div class="lc-card"><div class="dim">Building the ${action === "log" ? "log entry" : "task"}: resolving people, labs and projects...</div></div>`;
  try {
    CARD = await api("/api/ledger/stage", { method: "POST", body: { action, key, sent } });
  } catch (e) { dlg.hidden = true; toast(e.message, "err"); return; }
  renderCard();
}

// A log card from free text (the end-of-day report): same review-then-commit card.
window.addEventListener("ultra:stage-text", async (ev) => {
  const dlg = $("#ledger-card");
  dlg.hidden = false;
  dlg.innerHTML = `<div class="lc-card"><div class="dim">Building the log entry...</div></div>`;
  try {
    CARD = await api("/api/ledger/stage-briefing", { method: "POST", body: { key: "", text: ev.detail.text, title: ev.detail.title, allow_unlinked: true, entities: [] } });
  } catch (e) { dlg.hidden = true; toast(e.message, "err"); return; }
  renderCard();
});

// A log or task card from an Ask Hermes answer (v0.14): the item it was about gives
// the chips; the answer is the text. Same review-then-commit card as every other.
window.addEventListener("ultra:stage-answer", async (ev) => {
  const d = ev.detail || {};
  const dlg = $("#ledger-card");
  dlg.hidden = false;
  dlg.innerHTML = `<div class="lc-card"><div class="dim">Building the ${d.action === "task" ? "task" : "log entry"} from the answer: resolving people...</div></div>`;
  try {
    CARD = await api("/api/ledger/stage-answer", { method: "POST", body: { action: d.action, text: d.text, key: d.key || "", task: d.task || "", title: d.title || "" } });
  } catch (e) { dlg.hidden = true; toast(e.message, "err"); return; }
  renderCard();
});
window.addEventListener("ultra:bucket-snippet", (ev) => {
  const d = ev.detail || {};
  addSnippet(d.text || "", d.key || "", d.title || "Snippet", new Date().toISOString());
});

// A log card for a calendar meeting (Today / Week view): guests become chips.
window.addEventListener("ultra:stage-meeting", async (ev) => {
  const dlg = $("#ledger-card");
  dlg.hidden = false;
  dlg.innerHTML = `<div class="lc-card"><div class="dim">Building the meeting log: resolving the guests...</div></div>`;
  try {
    CARD = await api("/api/ledger/stage-meeting", { method: "POST", body: { event: ev.detail.event, notes: ev.detail.notes || "" } });
  } catch (e) { dlg.hidden = true; toast(e.message, "err"); return; }
  renderCard();
});

// Everyone on a calendar invite in the People / Full tabs.
window.addEventListener("ultra:item-context", (ev) => {
  const d = ev.detail || {};
  showTab("people");
  loadPeople({ key: d.key, subject: d.subject || "", source: d.source || "calendar" });
});

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
        : `<label for="lc-pri">Priority</label><select id="lc-pri">${["LOW", "MEDIUM", "HIGH", "CRITICAL"].map((p) => `<option ${p === c.priority ? "selected" : ""}>${p}</option>`).join("")}</select>
      <label for="lc-due">Due</label><span class="lc-due"><input type="date" id="lc-due" value="${esc(c.due || "")}" ${c.state !== "staged" ? "disabled" : ""}>
        <button type="button" class="btn tiny ghost" data-due="0">Today</button><button type="button" class="btn tiny ghost" data-due="1">Tomorrow</button><button type="button" class="btn tiny ghost" data-due="7">+1 week</button><button type="button" class="btn tiny ghost" data-due="">None</button></span>`}
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
  $$("[data-due]", dlg).forEach((b) => {
    b.onclick = () => {
      if (CARD.state !== "staged") return;
      const n = b.dataset.due;
      let v = "";
      if (n !== "") v = localDay(Number(n));
      $("#lc-due").value = v; CARD.due = v;
    };
  });
  if ($("#lc-due")) $("#lc-due").onchange = (e) => { CARD.due = e.target.value; };
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
  const due = $("#lc-due")?.value || "";
  if (due && !/^\d{4}-\d{2}-\d{2}$/.test(due)) { toast("The due date must be a full date.", "err"); return; }
  CARD.due = due;
  const body = { card: CARD.id, text, chips, date: $("#lc-date")?.value || "", priority: $("#lc-pri")?.value || "MEDIUM", due };
  const summary = CARD.action === "log"
    ? `Write this interaction to the ledger?\n\nDate: ${body.date}\nLinks: ${chips.map((c) => c.name).join(", ") || "(only you)"}\n\n${text.slice(0, 400)}${text.length > 400 ? "..." : ""}`
    : `Add this task (assigned to you)?\n\nPriority: ${body.priority}\nDue: ${due || "(no due date)"}\nLinks: ${chips.map((c) => c.name).join(", ") || "(none)"}\n\n${text}`;
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
