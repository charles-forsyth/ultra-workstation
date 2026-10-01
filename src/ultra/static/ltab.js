// Ledger tab (v0.12): the whole ledger in one place. Reads are instant through
// `nexus serve` (or slower through the CLI). Every write is a review card first; the
// server builds the exact command and returns a single-use token, nothing runs until
// "Write to ledger". Deletes and unlinks need a second, separate confirmation. After
// the write the card shows what the ledger read back.

import { api, esc, toast, busy, copyText } from "./app.js";
import { renderMd } from "./tools.js";
import { askButton, openAsk } from "./ask.js";

const $ = (s, el = document) => el.querySelector(s);
const L = { open: false, view: "home", page: null, onOpen: null, onClose: null, brief: null, listKind: "people" };
const KIND_LABEL = { people: "People", labs: "Labs and units", gcp: "GCP projects", projects: "Projects", grants: "Grants", assets: "Assets", tasks: "Task", interactions: "Interaction" };
const TYPE_KIND = { Researcher: "people", Lab: "labs", GCPProject: "gcp", ResearchProject: "projects", Grant: "grants", Asset: "assets", Task: "tasks", Interaction: "interactions" };
const CONN = ["MEMBER_OF", "PI_OF", "LEADS", "SUPPORTS", "OVERSIGHT_BY", "FUNDS", "OPERATES", "USES_ASSET", "USES_RESOURCE", "HOSTED_ON", "COLLABORATES_WITH", "LINKS_TO", "PARTICIPATED_IN", "REFERENCED_IN", "LOCATED_IN", "PART_OF", "ASSIGNED_TO", "BLOCKS", "DEPENDS_ON"];

export function initLedgerTab({ onOpen, onClose }) {
  L.onOpen = onOpen; L.onClose = onClose;
  $("#btn-ledger")?.addEventListener("click", () => (L.open ? closeLedgerTab() : openLedgerTab()));
}
export function ledgerTabOpen() { return L.open; }
export function closeLedgerTab() {
  L.open = false; $("#btn-ledger")?.classList.remove("on");
  L.onClose?.();
}
export function openLedgerTab(view = null, arg = null) {
  L.open = true; L.onOpen?.();
  $("#btn-ledger")?.classList.add("on");
  $("#thread-empty").hidden = true; $("#thread").hidden = false;
  if (view === "entity" && arg) return openEntity(arg.kind, arg.key);
  show(view || L.view);
}

// ---------------------------------------------------------------- frame
function head(title, extra = "") {
  const tabs = [["home", "Home"], ["browse", "Browse"], ["tasks", "Tasks"], ["interactions", "Interactions"], ["org", "Org"], ["reports", "Health and audit"]];
  return `<div class="today-head lt-head">
    <h2>Ledger</h2><span class="dim small-t" id="lt-src"></span>
    <div class="seg" id="lt-seg">${tabs.map(([k, t]) => `<button data-v="${k}" class="${L.view === k ? "on" : ""}">${t}</button>`).join("")}</div>
    <span class="grow"></span>
    <input id="lt-q" class="rinput lt-q" placeholder="Search the ledger..." autocomplete="off" aria-label="Search the ledger">
    <button class="btn small" id="lt-new" title="Add a record or a task">+ New</button>
    <button class="btn small ghost" id="lt-fresh" title="Reload from the ledger">&#8635;</button>
    <button class="btn small ghost" id="lt-close" title="Back (Esc)">Close</button>
  </div>${title ? `<div class="lt-title">${title}${extra}</div>` : ""}`;
}

function wireHead() {
  $("#lt-seg").onclick = (e) => { const b = e.target.closest("[data-v]"); if (b) show(b.dataset.v); };
  $("#lt-close").onclick = closeLedgerTab;
  $("#lt-fresh").onclick = () => (L.view === "entity" && L.page ? openEntity(L.page.kind, L.page.key, true) : show(L.view, true));
  $("#lt-new").onclick = newMenu;
  const q = $("#lt-q");
  q.onkeydown = (e) => { if (e.key === "Enter" && q.value.trim()) search(q.value.trim()); };
  api("/api/lt/status").then((s) => {
    const el = $("#lt-src"); if (!el) return;
    el.textContent = s.serve ? `live via nexus serve (${s.serve_env})` : "via the nexus command (slower)";
    el.title = s.serve ? `nexus ${s.serve_version}; last call ${s.last?.ms ?? "?"} ms` : "Start it with: nexus serve --start";
  }).catch(() => {});
}

function frame(title, body, extra = "") {
  const th = $("#thread");
  th.innerHTML = head(title, extra) + `<div id="lt-body">${body}</div>`;
  wireHead();
  return $("#lt-body");
}

async function show(view, fresh = false) {
  L.view = view;
  const loading = `<div class="dim">Loading...</div>`;
  const body = frame("", loading);
  try {
    if (view === "home") return renderHome(body, await api(`/api/lt/home${fresh ? "?fresh=1" : ""}`));
    if (view === "browse") return renderBrowse(body, fresh);
    if (view === "tasks") return renderTasks(body, await api(`/api/lt/tasks${fresh ? "?fresh=1" : ""}`));
    if (view === "interactions") return renderInteractions(body);
    if (view === "org") return renderOrg(body);
    if (view === "reports") return renderReports(body);
  } catch (e) { body.innerHTML = `<div class="alert">${esc(e.message)}</div>`; }
}

const fmtDay = (iso) => (iso ? String(iso).slice(0, 10) : "");
const PRI_SHORT = { CRITICAL: "CRIT", HIGH: "HIGH", MEDIUM: "MED", LOW: "LOW" };
const pri = (p) => `<span class="badge pri ${esc(String(p || "").toLowerCase())}">${esc(PRI_SHORT[p] || String(p || ""))}</span>`;
const ent = (kind, key, label) => `<a href="#" class="lt-ent" data-kind="${esc(kind)}" data-key="${esc(key)}">${esc(label)}</a>`;
function wireEnts(root) {
  root.querySelectorAll(".lt-ent").forEach((a) => (a.onclick = (e) => { e.preventDefault(); openEntity(a.dataset.kind, a.dataset.key); }));
}
// A connection row only carries an id + type; tasks and interactions open by id,
// others need their key, so resolve through search when needed.
// The key an entity page is opened by: NetID for people, name for other records
// (the ledger's `show` commands take those, not UUIDs), the UUID for tasks/interactions.
export function entityKey(c) {
  const kind = c.kind || TYPE_KIND[c.entity_type] || "";
  if (kind === "tasks" || kind === "interactions") return { kind, key: c.id };
  if (kind === "people") { const m = /\(([a-z][a-z0-9]{1,15})\)\s*$/.exec(c.name || ""); if (m) return { kind, key: m[1] }; }
  return { kind, key: (c.name || "").replace(/\s*\([a-z0-9]+\)\s*$/, "") };
}
function connLink(c) {
  const k = entityKey(c);
  return ent(k.kind, k.key, c.name);
}

// ---------------------------------------------------------------- home
function renderHome(body, h) {
  const s = h.stats || {}, c = s.counts || {};
  const tile = (n, t, v) => `<div class="lt-tile" ${v ? `data-go="${v}"` : ""}><b>${esc(n ?? "-")}</b><span>${esc(t)}</span></div>`;
  const tl = (xs, empty) => xs.length ? `<ul class="day-list">${xs.map((t) => `<li class="day-item">${pri(t.priority)} ${ent("tasks", t.id, t.summary)} <span class="dim small-t lt-nowrap">${esc(t.status)}${t.due_date ? ` &middot; due ${esc(fmtDay(t.due_date))}` : ""}</span></li>`).join("")}</ul>` : `<div class="dim small-t">${empty}</div>`;
  body.innerHTML = `
    <div class="dim small-t">As of ${esc(h.now)} (server clock)</div>
    <div class="lt-tiles">
      ${tile(c.researchers, "people", "browse:people")}${tile(c.labs, "labs and units", "browse:labs")}${tile(c.gcp_projects, "GCP projects", "browse:gcp")}
      ${tile(c.projects, "projects", "browse:projects")}${tile(c.grants, "grants", "browse:grants")}${tile(c.assets, "assets", "browse:assets")}
      ${tile(h.open_tasks, "open tasks", "tasks")}${tile(s.tasks_overdue, "overdue", "tasks")}${tile((s.interactions_recent || {}).last_7d, "logs this week", "interactions")}
    </div>
    <div class="lt-cols">
      <section class="card"><div class="label">Overdue <span class="dim">${h.overdue.length}</span></div>${tl(h.overdue, "Nothing overdue.")}</section>
      <section class="card"><div class="label">Due next</div>${tl(h.upcoming, "No due dates set.")}</section>
      <section class="card"><div class="label">Blocked</div>${tl(h.blocked, "Nothing blocked.")}</section>
      <section class="card"><div class="label">Recent interactions</div>
        <ul class="day-list">${(h.recent || []).map((i) => `<li class="day-item"><span class="dim small-t">${esc(fmtDay(i.date))}</span> ${ent("interactions", i.id, (i.summary || "").slice(0, 140))}</li>`).join("")}</ul></section>
    </div>`;
  body.querySelectorAll("[data-go]").forEach((t) => (t.onclick = () => {
    const [v, k] = t.dataset.go.split(":"); if (k) L.listKind = k; show(v);
  }));
  wireEnts(body);
}

// ---------------------------------------------------------------- browse + search
async function renderBrowse(body, fresh) {
  const kinds = ["people", "labs", "gcp", "projects", "grants", "assets"];
  body.innerHTML = `<div class="seg" id="lt-kinds">${kinds.map((k) => `<button data-k="${k}" class="${L.listKind === k ? "on" : ""}">${KIND_LABEL[k]}</button>`).join("")}</div>
    <input id="lt-filter" class="rinput" placeholder="Filter this list..." autocomplete="off">
    <div id="lt-rows" class="dim">Loading...</div>`;
  $("#lt-kinds").onclick = (e) => { const b = e.target.closest("[data-k]"); if (b) { L.listKind = b.dataset.k; renderBrowse(body); } };
  const load = async () => {
    const f = $("#lt-filter").value.trim();
    const r = await api(`/api/lt/list/${L.listKind}?limit=5000${fresh ? "&fresh=1" : ""}${f ? `&q=${encodeURIComponent(f)}` : ""}`);
    fresh = false;
    const keyOf = { people: (x) => x.netid, labs: (x) => x.name, gcp: (x) => x.project_id, projects: (x) => x.name, grants: (x) => x.c_number, assets: (x) => x.name }[L.listKind];
    const sub = { people: (x) => [x.title, x.dept].filter(Boolean).join(", "), labs: (x) => [x.type, x.strategic_status].filter(Boolean).join(" / "), gcp: (x) => [x.name, x.status].filter(Boolean).join(" / "), projects: (x) => x.status || "", grants: (x) => x.title || "", assets: (x) => [x.type, x.location].filter(Boolean).join(" / ") }[L.listKind];
    const label = { people: (x) => `${x.name} (${x.netid})`, gcp: (x) => x.project_id, grants: (x) => x.c_number }[L.listKind] || ((x) => x.name);
    $("#lt-rows").innerHTML = `<div class="dim small-t">${r.count} shown</div><ul class="day-list lt-rows">${r.rows.slice(0, 500).map((x) => `<li class="day-item">${ent(L.listKind, keyOf(x), label(x))} <span class="dim small-t">${esc(sub(x) || "")}</span></li>`).join("")}</ul>${r.count > 500 ? `<div class="dim small-t">Showing 500; filter to narrow.</div>` : ""}`;
    wireEnts($("#lt-rows"));
  };
  let t; $("#lt-filter").oninput = () => { clearTimeout(t); t = setTimeout(() => load().catch((e) => toast(e.message, "err")), 250); };
  await load();
}

async function search(q) {
  L.view = "search";
  const body = frame(`Search: ${esc(q)}`, `<div class="dim">Searching...</div>`);
  try {
    const r = await api(`/api/lt/search?q=${encodeURIComponent(q)}`);
    body.innerHTML = r.results.length ? `<ul class="day-list">${r.results.map((x) => `<li class="day-item"><span class="src">${esc(x.type)}</span> ${connLink({ ...x, entity_type: x.type })} <span class="dim small-t">${esc(x.reason || "")} ${x.score ? `(${Number(x.score).toFixed(2)})` : ""}</span></li>`).join("")}</ul>` : `<div class="dim">No matches.</div>`;
    wireEnts(body);
  } catch (e) { body.innerHTML = `<div class="alert">${esc(e.message)}</div>`; }
}

// ---------------------------------------------------------------- entity page
async function openEntity(kind, key, fresh = false) {
  L.view = "entity"; L.brief = null;
  const body = frame(`${esc(KIND_LABEL[kind] || kind)}: ${esc(key)}`, `<div class="dim">Loading...</div>`);
  let p;
  try { p = await api(`/api/lt/entity?kind=${encodeURIComponent(kind)}&key=${encodeURIComponent(key)}${fresh ? "&fresh=1" : ""}`); }
  catch (e) { body.innerHTML = `<div class="alert">${esc(e.message)}</div>`; return; }
  L.page = p;
  const r = p.record;
  const title = r.name || r.summary || r.project_id || r.c_number || key;
  $(".lt-title").innerHTML = `${esc(KIND_LABEL[kind] || kind)}: <b>${esc(title)}</b>${r.netid ? ` <span class="dim">(${esc(r.netid)})</span>` : ""}`;
  const skip = new Set(["id", "details", "vector", "bio_vector", "external_state", "links"]);
  const facts = Object.entries(r).filter(([k, v]) => !skip.has(k) && v !== null && v !== "" && typeof v !== "object");
  const det = Object.entries(r.details || {}).filter(([k]) => k !== "superseded");
  const groups = Object.entries(p.groups || {});
  const es = r.external_state || {};
  body.innerHTML = `
    <div class="lt-cols">
      <section class="card"><div class="label">Record</div>
        <dl class="lt-dl">${facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(String(v))}</dd>`).join("")}
        ${det.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(typeof v === "object" ? JSON.stringify(v) : String(v))}</dd>`).join("")}
        <dt>id</dt><dd class="lt-id"><span class="mono small-t">${esc(r.id || "")}</span><button class="btn tiny" id="lt-copyid">Copy</button></dd></dl>
        ${es.data ? `<div class="label">Cached cloud scan <span class="dim">${esc(fmtDay(es.last_updated))}</span></div><pre class="lt-pre">${esc(JSON.stringify(es.data, null, 1).slice(0, 3000))}</pre>` : ""}
        ${(r.details?.documents || []).length ? `<div class="label">Documents</div><ul class="day-list">${r.details.documents.map((d) => `<li><a href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">${esc(d.title || d.url)}</a></li>`).join("")}</ul>` : ""}
        ${r.links ? `<div class="label">Linked to</div><ul class="day-list">${r.links.map((x) => `<li class="day-item"><span class="src">${esc(x.connection_type || "")}</span> ${connLink(x)}</li>`).join("")}</ul>` : ""}
      </section>
      <section class="card"><div class="label">Actions</div>
        <div class="lt-acts">${p.write_actions.map((a) => `<button class="btn small ${a.destructive ? "danger" : ""}" data-act="${esc(a.action)}">${esc(a.label)}</button>`).join("")}</div>
        <div class="label lt-gap">AI briefing <span class="dim small-t">(cited; nothing is saved unless you log it)</span></div>
        <div id="lt-brief"><button class="btn small ai" id="lt-brief-go">Write briefing</button> ${askButton("btn small ai")} <button class="btn small" id="lt-graph" title="See this record's neighborhood">Graph</button></div>
      </section>
    </div>
    ${groups.length ? `<section class="card"><div class="label">Connections</div><div class="lt-groups">${groups.map(([g, xs]) => `<div><div class="dim small-t">${esc(g)} (${xs.length})</div><ul class="day-list">${xs.slice(0, 60).map((x) => `<li class="day-item" data-cid="${esc(x.id)}">${connLink(x)} <span class="dim small-t">${esc(x.type || "")}${x.role ? ` &middot; ${esc(x.role)}` : ""}</span> <button class="btn tiny lt-unlink" data-id="${esc(x.id)}" data-name="${esc(x.name)}" title="Remove the link">unlink</button></li>`).join("")}</ul></div>`).join("")}</div></section>` : ""}
    ${(p.tasks_open || []).length || p.tasks_done ? `<section class="card"><div class="label">Tasks <span class="dim">${p.tasks_open.length} open, ${p.tasks_done} done</span></div><ul class="day-list">${p.tasks_open.map((t) => `<li class="day-item">${ent("tasks", t.id, t.name)}</li>`).join("")}</ul></section>` : ""}
    ${p.interaction_count ? `<section class="card"><div class="label">Interactions <span class="dim">${p.interaction_count}${p.interaction_count > p.interactions.length ? `, newest ${p.interactions.length} shown` : ""}</span></div><ul class="day-list">${p.interactions.map((i) => `<li class="day-item" id="ix-${esc(i.id.slice(0, 8))}">${ent("interactions", i.id, (i.name || "").slice(0, 160))}</li>`).join("")}</ul></section>` : ""}`;
  wireEnts(body);
  $("#lt-copyid").onclick = () => copyText(r.id || "");
  body.querySelectorAll("[data-act]").forEach((b) => (b.onclick = () => actionForm(b.dataset.act, p)));
  body.querySelectorAll(".lt-unlink").forEach((b) => (b.onclick = () => review({ action: "unlink", source: r.id, target: b.dataset.id, source_name: title, target_name: b.dataset.name })));
  $("#lt-brief-go").onclick = (e) => busy(e.currentTarget, () => loadBrief(kind, key, false));
  $("#lt-graph").onclick = () => window.dispatchEvent(new CustomEvent("ultra:graph", { detail: { id: r.id } }));
  body.querySelector("#lt-brief [data-ask]").onclick = () => openAsk({ type: "entity", kind, key }, title);
}

async function loadBrief(kind, key, fresh) {
  const box = $("#lt-brief");
  box.innerHTML = `<div class="dim small-t">Reading the record, its links and its interactions... (10-30 s)</div>`;
  try {
    const b = await api("/api/lt/brief", { method: "POST", body: { kind, key, fresh } });
    if (!L.page || L.page.key !== key) return;
    L.brief = b;
    const cite = (html) => html.replace(/\[((?:L|T|E):[0-9a-f]{8})\]/g, (m, ref) => `<button class="cite" data-cite="${esc(ref.slice(2))}" title="Show the source">${esc(ref)}</button>`);
    box.innerHTML = `${b.uncited_ids.length ? `<div class="warnline">The AI cited ids that are not in this record's data: ${esc(b.uncited_ids.join(", "))}. Treat those lines as unverified.</div>` : ""}
      <div class="md brief-md" contenteditable="true" spellcheck="true" id="lt-brief-md">${cite(renderMd(b.markdown))}</div>
      <div class="pacts"><button class="btn small" id="lt-b-copy">Copy</button><button class="btn small" id="lt-b-listen">Listen</button><button class="btn small" id="lt-b-redo">Rewrite</button>
      <span class="dim small-t lt-nowrap">${esc(b.model || "")}${b.cached ? " (cached)" : b.seconds ? `, ${esc(b.seconds)} s` : ""}</span></div>`;
    box.querySelectorAll(".cite").forEach((c) => (c.onclick = () => {
      const full = b.cites[c.dataset.cite]; if (!full) return toast("That id is not in this record's data.", "err");
      const row = document.getElementById(`ix-${c.dataset.cite}`) || document.querySelector(`[data-cid="${full}"]`);
      if (row) { row.scrollIntoView({ block: "center" }); row.classList.add("flash"); setTimeout(() => row.classList.remove("flash"), 1500); }
      else openEntity(L.page.tasks_open.some((t) => t.id === full) ? "tasks" : "interactions", full);
    }));
    $("#lt-b-copy").onclick = () => copyText($("#lt-brief-md").innerText);
    $("#lt-b-listen").onclick = () => window.dispatchEvent(new CustomEvent("ultra:listen", { detail: { text: $("#lt-brief-md").innerText, title: "Ledger briefing" } }));
    $("#lt-b-redo").onclick = (e) => busy(e.currentTarget, () => loadBrief(kind, key, true));
  } catch (e) { box.innerHTML = `<div class="dim small-t">Briefing unavailable: ${esc(e.message)}</div>`; }
}

// ---------------------------------------------------------------- tasks board
function renderTasks(body, r) {
  const cols = ["TODO", "IN_PROGRESS", "BLOCKED"];
  const by = Object.fromEntries(cols.map((c) => [c, []]));
  for (const t of r.tasks) (by[t.status] || (by[t.status] = [])).push(t);
  const order = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 };
  for (const c of cols) by[c].sort((a, b) => (order[a.priority] ?? 9) - (order[b.priority] ?? 9) || String(a.due_date || "9").localeCompare(String(b.due_date || "9")));
  body.innerHTML = `<div class="dim small-t">Today is ${esc(r.today)}. Drag a card to another column to change its status (a review card opens first).</div>
    <div class="lt-board">${cols.map((c) => `<div class="lt-col" data-status="${c}"><div class="label">${c.replace("_", " ")} <span class="dim">${by[c].length}</span></div>
      ${by[c].map((t) => `<div class="lt-task ${t.overdue ? "overdue" : ""}" draggable="true" data-id="${esc(t.id)}">${pri(t.priority)} ${ent("tasks", t.id, t.summary)}
        <div class="dim small-t">${t.due_date ? `${t.overdue ? "overdue, " : ""}due ${esc(fmtDay(t.due_date))}` : "no due date"}${t.assigned_to ? ` &middot; ${esc(t.assigned_to)}` : ""}</div></div>`).join("")}</div>`).join("")}</div>`;
  wireEnts(body);
  body.querySelectorAll(".lt-task").forEach((el) => (el.ondragstart = (e) => e.dataTransfer.setData("text/lt-task", el.dataset.id)));
  body.querySelectorAll(".lt-col").forEach((col) => {
    col.ondragover = (e) => { if (e.dataTransfer.types.includes("text/lt-task")) { e.preventDefault(); col.classList.add("over"); } };
    col.ondragleave = () => col.classList.remove("over");
    col.ondrop = (e) => { e.preventDefault(); col.classList.remove("over"); const id = e.dataTransfer.getData("text/lt-task"); if (id) review({ action: "task_update", id, status: col.dataset.status }); };
  });
}

// ---------------------------------------------------------------- interactions
async function renderInteractions(body) {
  body.innerHTML = `<div class="lt-filters"><input id="lt-ix-q" class="rinput" placeholder="Text contains..."><input id="lt-ix-since" type="date" class="rinput" title="Since"><button class="btn small" id="lt-ix-go">Filter</button><button class="btn small" id="lt-ix-log">+ Log</button></div><div id="lt-ix-rows" class="dim">Loading...</div>`;
  const load = async () => {
    const q = $("#lt-ix-q").value.trim(), s = $("#lt-ix-since").value;
    const r = await api(`/api/lt/interactions?limit=100${q ? `&contains=${encodeURIComponent(q)}` : ""}${s ? `&since=${s}` : ""}`);
    $("#lt-ix-rows").innerHTML = r.interactions.length ? `<ul class="day-list">${r.interactions.map((i) => `<li class="day-item"><span class="dim small-t">${esc(fmtDay(i.date))}</span> ${ent("interactions", i.id, (i.summary || "").slice(0, 200))}</li>`).join("")}</ul>` : `<div class="dim">None.</div>`;
    wireEnts($("#lt-ix-rows"));
  };
  $("#lt-ix-go").onclick = (e) => busy(e.currentTarget, load);
  $("#lt-ix-q").onkeydown = (e) => { if (e.key === "Enter") load(); };
  $("#lt-ix-log").onclick = () => actionForm("log", null);
  await load();
}

// ---------------------------------------------------------------- org
function renderOrg(body) {
  body.innerHTML = `<div class="lt-filters"><input id="lt-org-q" class="rinput" placeholder="Unit name (a department, college or lab)" value="${esc(localStorage.getItem("ultra.ltorg") || "")}"><button class="btn small" id="lt-org-go">Show</button></div><div id="lt-org"></div>`;
  const go = async () => {
    const u = $("#lt-org-q").value.trim(); if (!u) return;
    localStorage.setItem("ultra.ltorg", u);
    $("#lt-org").innerHTML = `<div class="dim">Loading...</div>`;
    try {
      const r = await api(`/api/lt/org?unit=${encodeURIComponent(u)}`);
      const node = (n) => `<li><b>${ent("labs", n.name, n.name)}</b>
        ${(n.leaders || []).length ? `<div class="small-t">Leads: ${n.leaders.map((p) => `${ent("people", p.netid, p.name)}${p.role ? ` <span class="dim">(${esc(p.role)})</span>` : ""}`).join(", ")}</div>` : ""}
        ${(n.members || []).length ? `<div class="small-t dim">Members: ${n.members.map((p) => ent("people", p.netid, p.name)).join(", ")}</div>` : ""}
        ${(n.sub_units || []).length ? `<ul class="lt-tree">${n.sub_units.map(node).join("")}</ul>` : ""}</li>`;
      $("#lt-org").innerHTML = r.tree ? `<ul class="lt-tree">${node(r.tree)}</ul>` : `<div class="dim">No unit by that name.</div>`;
      wireEnts($("#lt-org"));
    } catch (e) { $("#lt-org").innerHTML = `<div class="alert">${esc(e.message)}</div>`; }
  };
  $("#lt-org-go").onclick = go;
  $("#lt-org-q").onkeydown = (e) => { if (e.key === "Enter") go(); };
  if ($("#lt-org-q").value) go();
}

// ---------------------------------------------------------------- reports
function renderReports(body) {
  const reps = [["doctor", "Graph health (doctor)", "Placeholder people, dangling links and orphan interactions. Read-only: fixes are only in the terminal."],
    ["health", "Relationship health", "Who has gone cold (90 days) and who you saw recently."],
    ["audit", "GCP audit", "Live cloud scan through skywalker: slow, minutes."]];
  body.innerHTML = reps.map(([k, t, d]) => `<section class="card" id="rep-${k}"><div class="label">${t}</div><div class="dim small-t">${d}</div><div class="rep-out"><button class="btn small" data-rep="${k}">Run</button></div></section>`).join("");
  body.querySelectorAll("[data-rep]").forEach((b) => (b.onclick = () => runReport(b.dataset.rep, false)));
  for (const [k] of reps) api(`/api/lt/report/${k}`).then((r) => r.state === "done" && paintReport(k, r)).catch(() => {});
}
async function runReport(name, fresh) {
  const box = $(`#rep-${name} .rep-out`); if (!box) return;
  let r = await api(`/api/lt/report/${name}${fresh ? "?fresh=1" : ""}`);
  while (r.state === "running") {
    box.innerHTML = `<div class="dim small-t">Running... ${Math.round(Date.now() / 1000 - r.started)} s</div>`;
    await new Promise((ok) => setTimeout(ok, 2000));
    if (!$(`#rep-${name}`)) return;
    r = await api(`/api/lt/report/${name}`);
  }
  paintReport(name, r);
}
function paintReport(name, r) {
  const box = $(`#rep-${name} .rep-out`); if (!box) return;
  if (r.state === "error") { box.innerHTML = `<div class="alert">${esc(r.error)}</div><button class="btn small" data-again>Run again</button>`; }
  else if (name === "doctor") {
    const d = r.data || {};
    box.innerHTML = `<div class="lt-tiles small">${[["researchers_total", "people"], ["edges_total", "links"], ["placeholder_edges", "placeholder links"], ["dangling_orphaned", "orphan links"], ["dangling_half_linked", "half-linked"], ["orphan_interactions", "unlinked logs"]].map(([k, t]) => `<div class="lt-tile ${d[k] === 0 ? "zero" : ""}"><b>${esc(d[k] ?? "-")}</b><span>${t}</span></div>`).join("")}</div>
      ${(d.placeholder_researchers || []).length ? `<div class="small-t">Placeholder people: ${d.placeholder_researchers.slice(0, 40).map((p) => ent("people", p.netid, p.netid)).join(", ")}${d.placeholder_researchers.length > 40 ? " ..." : ""}</div>` : ""}
      <div class="dim small-t">To fix, run <span class="mono">nexus doctor --fix-safe</span> in a terminal; Ultra never runs fixes.</div>`;
  } else if (name === "health") {
    const d = r.data || {}, rows = (d.researchers || []).filter((x) => x.status !== "HEALTHY").slice(0, 80);
    box.innerHTML = `<div class="small-t">${Object.entries(d.counts || {}).map(([k, v]) => `<span class="badge">${esc(k)} ${esc(v)}</span>`).join(" ")} <span class="dim">(listed below: stale and cold, ${rows.length} shown)</span></div>
      <ul class="day-list">${rows.map((x) => `<li class="day-item"><span class="badge ${x.status === "COLD" ? "warn" : ""}">${esc(x.status)}</span> ${ent("people", x.netid, x.name)}${x.vip ? ' <span class="badge">VIP</span>' : ""} <span class="dim small-t">${x.days_ago == null ? "never logged" : `${x.days_ago} days`}${x.title ? ` &middot; ${esc(x.title)}` : ""}</span></li>`).join("")}</ul>`;
  } else {
    box.innerHTML = `<pre class="lt-pre">${esc(JSON.stringify(r.data, null, 1).slice(0, 20000))}</pre>`;
  }
  box.insertAdjacentHTML("beforeend", `<div class="pacts"><span class="dim small-t">${r.cached ? "cached" : "fresh"}</span><button class="btn tiny ghost" data-again>Run again</button></div>`);
  box.querySelectorAll("[data-again]").forEach((b) => (b.onclick = () => runReport(name, true)));
  wireEnts(box);
}

// ---------------------------------------------------------------- write forms
function newMenu() {
  const opts = [["task_add", "Task"], ["log", "Log an interaction"], ["labs_add", "Lab or unit"], ["projects_add", "Project"], ["gcp_add", "GCP project"], ["grants_add", "Grant"], ["assets_add", "Asset"]];
  modal(`<h3>New in the ledger</h3><div class="dim small-t">People are added only with the Add to ledger button on a conversation.</div><div class="lt-acts">${opts.map(([a, t]) => `<button class="btn small" data-new="${a}">${t}</button>`).join("")}</div><div class="rv-acts"><button class="btn small ghost" data-x>Cancel</button></div>`);
  document.querySelectorAll("[data-new]").forEach((b) => (b.onclick = () => actionForm(b.dataset.new, L.view === "entity" ? L.page : null)));
}

const F = {
  text: (id, label, v = "", ph = "") => `<label class="lt-f">${label}<input id="${id}" class="rinput" value="${esc(v)}" placeholder="${esc(ph)}"></label>`,
  area: (id, label, v = "") => `<label class="lt-f">${label}<textarea id="${id}" rows="6" class="rinput">${esc(v)}</textarea></label>`,
  sel: (id, label, opts, v = "") => `<label class="lt-f">${label}<select id="${id}">${opts.map((o) => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></label>`,
  date: (id, label, v = "") => `<label class="lt-f">${label}<input id="${id}" type="date" value="${esc(v)}"></label>`,
};
const val = (id) => ($(`#${id}`)?.value || "").trim();

async function actionForm(action, page) {
  const r = page?.record || {}, key = page?.key || "", rid = r.id || "";
  const nowIso = await api("/api/now").then((n) => `${n.date} ${n.time}`).catch(() => "");
  let html = "", build;
  switch (action) {
    case "task_add":
      html = F.text("f-sum", "Task") + F.sel("f-pri", "Priority", ["MEDIUM", "LOW", "HIGH", "CRITICAL"]) + F.date("f-due", "Due (optional)");
      build = () => ({ action, summary: val("f-sum"), priority: val("f-pri"), due: val("f-due") });
      break;
    case "task_update":
      html = F.sel("f-st", "Status", ["", "TODO", "IN_PROGRESS", "BLOCKED", "DONE"], "") + F.sel("f-pri", "Priority", ["", "LOW", "MEDIUM", "HIGH", "CRITICAL"], "") + F.date("f-due", "Due") + `<label class="lt-f cx-chk"><input type="checkbox" id="f-clear"> Clear the due date</label>`;
      build = () => ({ action, id: rid, status: val("f-st"), priority: val("f-pri"), due: val("f-due"), clear_due: $("#f-clear").checked });
      break;
    case "log":
      html = F.area("f-text", "What happened") + F.text("f-date", "Date and time", (nowIso || "").slice(0, 16), "YYYY-MM-DD HH:MM") + (rid ? `<div class="dim small-t">Linked to ${esc(r.name || r.summary || key)}</div>` : `<div class="dim small-t">Not linked to anything. Open a record first to link the log to it.</div>`);
      build = () => ({ action, text: val("f-text"), date: val("f-date"), links: rid ? [rid] : [], link_names: rid ? [r.name || key] : [] });
      break;
    case "interaction_edit":
      html = F.area("f-sum", "Corrected text (leave as is to keep)", r.summary || "") + F.text("f-date", "Corrected date (optional)", "", "YYYY-MM-DD HH:MM") + F.text("f-note", "Why (kept with the record)");
      build = () => ({ action, id: rid, summary: val("f-sum") !== (r.summary || "") ? val("f-sum") : "", date: val("f-date"), note: val("f-note") });
      break;
    case "link":
      html = `<div class="dim small-t">From ${esc(r.name || r.summary || key)}</div>` + F.text("f-find", "Link to (search)") + `<div id="f-hits" class="ls-out"></div>` + F.sel("f-type", "Link type", CONN, "LINKS_TO") + F.text("f-role", "Role (optional)");
      build = () => ({ action, source: rid, target: $("#f-hits").dataset.pick || "", target_name: $("#f-hits").dataset.name || "", source_name: r.name || r.summary || key, type: val("f-type"), role: val("f-role") });
      break;
    case "people_update": case "labs_update":
      html = `<div class="dim small-t">One field per line: field=value (e.g. title=Chair). Names and ids cannot be changed here.</div>` + F.area("f-set", "Fields");
      build = () => ({ action, key, set: Object.fromEntries(val("f-set").split("\n").map((l) => l.split("=")).filter((x) => x.length >= 2).map(([k, ...v]) => [k.trim(), v.join("=").trim()])) });
      break;
    case "people_tag":
      html = F.text("f-tag", "Tag", "", "VIP");
      build = () => ({ action, key, tag: val("f-tag") });
      break;
    case "labs_add":
      html = F.text("f-name", "Name") + F.text("f-desc", "Description (optional)");
      build = () => ({ action, name: val("f-name"), description: val("f-desc") });
      break;
    case "projects_add":
      html = F.text("f-name", "Name") + F.text("f-sum", "One-line summary");
      build = () => ({ action, name: val("f-name"), summary: val("f-sum") });
      break;
    case "projects_doc_add":
      html = F.text("f-title", "Title") + F.text("f-url", "Link (https://)");
      build = () => ({ action, key, title: val("f-title"), url: val("f-url") });
      break;
    case "projects_doc_rm":
      html = F.sel("f-url", "Document", (r.details?.documents || []).map((d) => d.url));
      build = () => ({ action, key, url: val("f-url") });
      break;
    case "gcp_add":
      html = F.text("f-pid", "Project id") + F.text("f-name", "Name (optional)");
      build = () => ({ action, project_id: val("f-pid"), name: val("f-name") });
      break;
    case "grants_add":
      html = F.text("f-c", "Grant number") + F.text("f-title", "Title") + F.text("f-ag", "Agency (optional)") + F.text("f-amt", "Amount in whole dollars (optional)");
      build = () => ({ action, c_number: val("f-c"), title: val("f-title"), agency: val("f-ag"), amount: val("f-amt") });
      break;
    case "assets_add":
      html = F.text("f-name", "Name") + F.sel("f-type", "Type", ["OTHER", "GPU", "SERVER", "WORKSTATION", "STORAGE", "LICENSE"]) + F.text("f-loc", "Location (optional)");
      build = () => ({ action, name: val("f-name"), type: val("f-type"), location: val("f-loc") });
      break;
    case "task_delete": case "interaction_delete":
      return review({ action, id: rid });
    default:
      if (action.endsWith("_delete")) return review({ action, key });
      return toast("That action is not available here.", "err");
  }
  modal(`<h3>${esc(action.replace(/_/g, " "))}</h3><div class="lt-form">${html}</div>
    <div class="rv-acts"><button class="btn small primary" data-rv>Review</button><button class="btn small ghost" data-x>Cancel</button></div>`);
  if (action === "link") wireFind();
  document.querySelector("[data-rv]").onclick = () => review(build());
}

function wireFind() {
  let t;
  $("#f-find").oninput = () => {
    clearTimeout(t);
    t = setTimeout(async () => {
      const q = val("f-find"); if (q.length < 2) return;
      const r = await api(`/api/lt/search?q=${encodeURIComponent(q)}`).catch(() => ({ results: [] }));
      const box = $("#f-hits");
      box.innerHTML = r.results.slice(0, 10).map((x) => `<div class="ls-row" data-id="${esc(x.id)}" data-name="${esc(x.name)}"><span class="src">${esc(x.type)}</span> ${esc(x.name)}</div>`).join("") || `<div class="dim small-t">No matches.</div>`;
      box.querySelectorAll("[data-id]").forEach((row) => (row.onclick = () => {
        box.dataset.pick = row.dataset.id; box.dataset.name = row.dataset.name;
        box.querySelectorAll(".ls-row").forEach((x) => x.classList.toggle("on", x === row));
      }));
    }, 300);
  };
}

// ---------------------------------------------------------------- review -> confirm -> commit
async function review(body) {
  let rv;
  try { rv = await api("/api/lt/write/review", { method: "POST", body }); }
  catch (e) { toast(e.message, "err"); return; }
  const c = rv.card;
  modal(`<h3 id="lt-rv-title">${esc(c.label)}${c.destructive ? ' <span class="badge warn">destructive</span>' : ""}</h3>
    <div class="rv-body">${c.lines.map((l) => esc(l)).join("\n")}</div>
    <details><summary class="dim small-t">Exact command</summary><div class="mono small-t lt-cmd">${esc(c.command)}</div></details>
    <div class="dim small-t">Nothing has been written yet. This review expires in ${Math.round(rv.expires_in / 60)} minutes.</div>
    <div class="rv-acts" id="lt-rv-acts">
      ${c.destructive ? `<button class="btn small danger" data-c1>Yes, ${esc(c.label.toLowerCase())}</button>` : `<button class="btn small primary" data-go>Write to ledger</button>`}
      <button class="btn small ghost" data-x autofocus>Not now</button></div>`, c.destructive);
  const acts = $("#lt-rv-acts");
  const go = async (extra) => {
    acts.innerHTML = `<div class="dim small-t">Writing and reading back...</div>`;
    try {
      const out = await api("/api/lt/write/commit", { method: "POST", body: { token: rv.token, ...extra } });
      const res = out.result || {};
      acts.innerHTML = `<div class="${out.ok ? "badge ok" : "alert"}">${out.ok ? "Done" : "Not confirmed"}: ${esc(res.read_back || "")}</div>
        ${!out.ok && res.output_tail ? `<pre class="lt-pre">${esc(res.output_tail)}</pre>` : ""}<button class="btn small" data-x>Close</button>`;
      acts.querySelector("[data-x]").onclick = () => { closeModal(); refreshAfterWrite(body, out); };
    } catch (e) { acts.innerHTML = `<div class="alert">${esc(e.message)}</div><button class="btn small" data-x>Close</button>`; acts.querySelector("[data-x]").onclick = closeModal; }
  };
  acts.querySelector("[data-go]")?.addEventListener("click", () => go({}));
  const c1 = acts.querySelector("[data-c1]");
  if (c1) c1.onclick = async () => {
    // second, separate confirmation; the button arms after 2 s so it is not a double-click
    let c2;
    try { c2 = await api("/api/lt/write/confirm", { method: "POST", body: { token: rv.token, confirm_1: rv.confirm_1 } }); }
    catch (e) { toast(e.message, "err"); closeModal(); return; }
    acts.innerHTML = `<div class="warnline">Last check. The ledger has no undo. <span id="lt-arm">The button unlocks in 2 seconds.</span></div><button class="btn small danger" data-c2 disabled>Delete for good</button><button class="btn small ghost" data-x>Not now</button>`;
    const b2 = acts.querySelector("[data-c2]");
    setTimeout(() => { b2.disabled = false; const a = $("#lt-arm"); if (a) a.textContent = ""; }, 2000);
    b2.onclick = () => go({ confirm_2: c2.confirm_2 });
    acts.querySelector("[data-x]").onclick = closeModal;
  };
}

function refreshAfterWrite(body, out) {
  const deleted = /_delete$/.test(body.action) && out.ok;
  if (L.view === "entity" && L.page && !deleted) openEntity(L.page.kind, L.page.key, true);
  else if (L.view === "entity" && deleted) show("home", true);
  else show(L.view, true);
}

function modal(html, destructive = false) {
  const box = $("#ledger-card");
  box.innerHTML = `<div class="rv-card lt-modal ${destructive ? "danger" : ""}" role="dialog" aria-modal="true" aria-labelledby="lt-rv-title">${html}</div>`;
  box.hidden = false;
  box.querySelectorAll("[data-x]").forEach((b) => (b.onclick = closeModal));
  box.onclick = (e) => { if (e.target === box) closeModal(); };
  box.onkeydown = (e) => { if (e.key === "Escape") { e.stopPropagation(); closeModal(); } };
  (box.querySelector("[autofocus]") || box.querySelector("input, textarea, select, button"))?.focus();
}
function closeModal() { const b = $("#ledger-card"); b.hidden = true; b.innerHTML = ""; }
