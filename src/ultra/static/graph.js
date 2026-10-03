// Graph view (v1.2, SPEC 7.6): the ledger neighborhood as nodes and edges.
// Read only. Click a node to open it in the Ledger tab; double-click to re-center on
// it; drag a stream item onto a node to add both to the bucket (then Log or Task
// builds the normal staged card). Interactions and tasks are counts, not nodes.
// Layout: a small force simulation in plain JS (no library), drawn as SVG. Every
// label is set with textContent (never innerHTML), so names cannot inject markup.

import { api, esc, toast } from "./app.js";
import { addConversation, addEntity } from "./ledger.js";
import { entityKey } from "./ltab.js";

const $ = (s, el = document) => el.querySelector(s);
const NS = "http://www.w3.org/2000/svg";
const G = { open: false, onOpen: null, onClose: null, openEntity: null, data: null, id: "", hide: new Set(["Asset"]), raf: 0, poll: 0, seq: 0 };
const TYPE_LABEL = { Researcher: "People", Lab: "Labs", GCPProject: "GCP", ResearchProject: "Projects", Grant: "Grants", Asset: "Assets" };
const KIND = { Researcher: "people", Lab: "labs", GCPProject: "gcp", ResearchProject: "projects", Grant: "grants", Asset: "assets" };

export function initGraph({ onOpen, onClose, openEntity }) {
  G.onOpen = onOpen; G.onClose = onClose; G.openEntity = openEntity;
}
export function graphOpen() { return G.open; }
export function closeGraph() {
  G.open = false; cancelAnimationFrame(G.raf); clearTimeout(G.poll);
  G.onClose?.();
}

export async function openGraph(id = "", fresh = false) {
  G.open = true; G.onOpen?.();
  G.id = id; clearTimeout(G.poll);
  const seq = ++G.seq;
  $("#thread-empty").hidden = true;
  const th = $("#thread"); th.hidden = false;
  th.innerHTML = `<div class="today-head gr-head"><h2>Graph</h2><button class="btn small ghost calm-only" data-goto="ledger" title="Back to the Ledger (n)">Ledger</button><span class="dim small-t" id="gr-sum">Reading the ledger...</span><span class="grow"></span>
      <div class="seg" id="gr-types"></div>
      <button class="btn small ghost" id="gr-me" title="Back to you">Me</button>
      <button class="btn small ghost" id="gr-fresh" title="Reload from the ledger">&#8635;</button>
      <button class="btn small ghost" id="gr-x" title="Close (Esc)">Close</button></div>
    <div class="gr-help dim small-t">Click a node to open it. Double-click to center on it. Drag an item from the stream onto a node to put both in the bucket. Interactions and tasks are counted on each node.</div>
    <div class="gr-wrap" id="gr-wrap"><svg id="gr-svg" role="img" aria-label="Ledger graph"></svg><div class="gr-tip" id="gr-tip" hidden></div></div>`;
  $("#gr-x").onclick = closeGraph;
  $("#gr-me").onclick = () => openGraph("");
  $("#gr-fresh").onclick = () => openGraph(G.id, true);
  let d;
  try { d = await load(id, fresh); }
  catch (e) { $("#gr-sum").textContent = ""; $("#gr-wrap").innerHTML = `<div class="alert">${esc(e.message)}</div>`; return; }
  if (!G.open || seq !== G.seq) return;
  show(d);
  // Links between neighbors arrive in the background (one ledger read each); ask
  // again every few seconds and redraw once when they are in.
  const again = async (tries) => {
    if (!G.open || seq !== G.seq || tries <= 0) return;
    let n; try { n = await load(id, false); } catch { return; }
    if (!G.open || seq !== G.seq) return;
    if (n.pending === 0 || tries === 1) { show(n); return; }
    $("#gr-sum").textContent = summary(n);
    G.poll = setTimeout(() => again(tries - 1), 4000);
  };
  if (d.pending > 0) G.poll = setTimeout(() => again(40), 4000);
}

function load(id, fresh) {
  const hide = [...G.hide].join(",");
  return api(`/api/graph?hops=2${id ? `&id=${encodeURIComponent(id)}` : ""}${hide ? `&hide=${encodeURIComponent(hide)}` : ""}${fresh ? "&fresh=1" : ""}`);
}

function summary(d) {
  const c = d.center;
  return `${c.name}: ${d.nodes.length - 1} linked records, ${d.interactions} interactions, ${d.tasks} tasks${d.truncated ? " (first 250 shown)" : ""}${d.pending ? ` - loading links between them (${d.pending} to go)...` : ""}`;
}

function show(d) {
  G.data = d;
  $("#gr-sum").textContent = summary(d);
  renderTypes(d);
  draw(d);
}

function renderTypes(d) {
  const seg = $("#gr-types");
  const all = Object.keys(TYPE_LABEL);
  seg.innerHTML = all.map((t) => `<button data-t="${t}" class="${G.hide.has(t) ? "" : "on"}" title="${G.hide.has(t) ? "Show" : "Hide"} ${esc(TYPE_LABEL[t])}">${esc(TYPE_LABEL[t])}${d.by_type[t] ? ` ${d.by_type[t]}` : ""}</button>`).join("");
  seg.onclick = (e) => {
    const b = e.target.closest("[data-t]"); if (!b) return;
    const t = b.dataset.t; G.hide.has(t) ? G.hide.delete(t) : G.hide.add(t);
    openGraph(G.id);
  };
}

function draw(d) {
  cancelAnimationFrame(G.raf);
  const wrap = $("#gr-wrap"), svg = $("#gr-svg");
  // Size the drawing to the SVG's real box so viewBox units are screen pixels:
  // a mismatched viewBox scales the drawing and clicks land beside the nodes.
  const box = svg.getBoundingClientRect();
  const W = Math.round(box.width || wrap.clientWidth || 800), H = Math.round(box.height || wrap.clientHeight || 560);
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  svg.replaceChildren();
  const idx = new Map(d.nodes.map((n, i) => [n.id, i]));
  const N = d.nodes.map((n, i) => {
    const a = (i / Math.max(1, d.nodes.length - 1)) * Math.PI * 2;
    const r = n.center ? 0 : Math.min(W, H) * 0.32;
    return { ...n, x: W / 2 + Math.cos(a) * r, y: H / 2 + Math.sin(a) * r, vx: 0, vy: 0, fixed: n.center };
  });
  const E = d.edges.map((e) => ({ ...e, s: idx.get(e.a), t: idx.get(e.b) })).filter((e) => e.s != null && e.t != null);
  const gE = document.createElementNS(NS, "g"), gN = document.createElementNS(NS, "g");
  svg.append(gE, gN);
  const lines = E.map((e) => {
    const l = document.createElementNS(NS, "line");
    l.setAttribute("class", `gr-edge ${N[e.s].center || N[e.t].center ? "c" : ""}`);
    const t = document.createElementNS(NS, "title"); t.textContent = `${e.type}${e.role ? ` (${e.role})` : ""}`; l.append(t);
    gE.append(l); return l;
  });
  const radius = (n) => (n.center ? 16 : 7 + Math.min(9, Math.sqrt(n.interactions + n.tasks)));
  const groups = N.map((n) => {
    const g = document.createElementNS(NS, "g");
    g.setAttribute("class", `gr-node t-${n.type}${n.center ? " center" : ""}`);
    g.setAttribute("tabindex", "0");
    g.dataset.id = n.id;
    const c = document.createElementNS(NS, "circle"); c.setAttribute("r", String(radius(n)));
    const label = document.createElementNS(NS, "text");
    label.setAttribute("dy", String(radius(n) + 12)); label.setAttribute("text-anchor", "middle");
    label.textContent = shortName(n.name);
    g.append(c, label);
    gN.append(g); return g;
  });
  // simulation
  let alpha = 1;
  const k = Math.sqrt((W * H) / Math.max(1, N.length)) * 0.55;
  function tick() {
    for (let i = 0; i < N.length; i++) {
      const a = N[i];
      for (let j = i + 1; j < N.length; j++) {
        const b = N[j];
        let dx = a.x - b.x, dy = a.y - b.y; let dd = dx * dx + dy * dy || 0.01;
        const f = (k * k) / dd * 0.6 * alpha;
        const dist = Math.sqrt(dd); dx /= dist; dy /= dist;
        a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
      }
    }
    for (const e of E) {
      const a = N[e.s], b = N[e.t];
      const dx = b.x - a.x, dy = b.y - a.y; const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const f = ((dist - k) / dist) * 0.06 * alpha;
      a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
    }
    for (const n of N) {
      if (n.fixed) { n.x = W / 2; n.y = H / 2; n.vx = n.vy = 0; continue; }
      n.vx += (W / 2 - n.x) * 0.004 * alpha; n.vy += (H / 2 - n.y) * 0.004 * alpha;
      n.x = Math.max(20, Math.min(W - 20, n.x + n.vx)); n.y = Math.max(20, Math.min(H - 28, n.y + n.vy));
      n.vx *= 0.55; n.vy *= 0.55;
    }
    paint();
    alpha *= 0.975;
    if (alpha > 0.02 && G.open) G.raf = requestAnimationFrame(tick);
  }
  function paint() {
    E.forEach((e, i) => { const l = lines[i]; l.setAttribute("x1", N[e.s].x); l.setAttribute("y1", N[e.s].y); l.setAttribute("x2", N[e.t].x); l.setAttribute("y2", N[e.t].y); });
    N.forEach((n, i) => groups[i].setAttribute("transform", `translate(${n.x.toFixed(1)},${n.y.toFixed(1)})`));
  }
  tick();
  // interaction
  const tip = $("#gr-tip");
  const nodeOf = (el) => { const g = el.closest?.(".gr-node"); return g ? N[idx.get(g.dataset.id)] : null; };
  gN.addEventListener("mousemove", (e) => {
    const n = nodeOf(e.target); if (!n) { tip.hidden = true; return; }
    tip.textContent = `${n.name} - ${TYPE_LABEL[n.type] || n.type}${n.interactions ? `, ${n.interactions} interactions` : ""}${n.tasks ? `, ${n.tasks} tasks` : ""}`;
    const r = wrap.getBoundingClientRect(); tip.style.left = `${e.clientX - r.left + 12}px`; tip.style.top = `${e.clientY - r.top + 12}px`; tip.hidden = false;
  });
  gN.addEventListener("mouseleave", () => { tip.hidden = true; });
  let clickTimer = 0;
  gN.addEventListener("click", (e) => {
    const n = nodeOf(e.target); if (!n) return;
    clearTimeout(clickTimer);
    clickTimer = setTimeout(() => openNode(n), 260);
  });
  gN.addEventListener("dblclick", (e) => { const n = nodeOf(e.target); if (!n) return; clearTimeout(clickTimer); openGraph(n.id); });
  gN.addEventListener("keydown", (e) => { const n = nodeOf(e.target); if (n && e.key === "Enter") openNode(n); });
  // drag a stream item onto a node -> bucket gets the item and the node
  gN.addEventListener("dragover", (e) => { const n = nodeOf(e.target); if (n) { e.preventDefault(); e.target.closest(".gr-node").classList.add("over"); } });
  gN.addEventListener("dragleave", (e) => { e.target.closest?.(".gr-node")?.classList.remove("over"); });
  gN.addEventListener("drop", async (e) => {
    const n = nodeOf(e.target); if (!n) return;
    e.preventDefault(); e.target.closest(".gr-node").classList.remove("over");
    const raw = e.dataTransfer.getData("application/x-ultra-item"); if (!raw) return;
    let it; try { it = JSON.parse(raw); } catch { return; }
    await addConversation(it.key);
    await addEntity({ id: n.id, name: n.name, type: n.type });
    toast(`In the bucket: "${it.subject || it.key}" and ${n.name}. Press Log or Task there.`, "ok");
  });
}

function openNode(n) {
  const k = entityKey({ kind: KIND[n.type], name: n.name, id: n.id });
  G.openEntity?.(k.kind, k.key);
}

function shortName(s) {
  const t = String(s || "").replace(/\s*\([^)]*\)\s*$/, "");
  return t.length > 22 ? `${t.slice(0, 21)}...` : t;
}
