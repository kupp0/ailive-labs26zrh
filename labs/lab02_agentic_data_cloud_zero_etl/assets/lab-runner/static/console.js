// Lab console (Lab 10 and Lab 11 carry identical copies): POST a fixed action, swap the returned HTML fragment in.
// No framework and no CDN: the image must build and run with nothing but pip. The only
// library, Cytoscape.js, is vendored under static/vendor/ (MIT, license alongside).
"use strict";

// --- Graph widget ------------------------------------------------------------
// innerHTML does not run scripts, so graphs are plain <div data-graph='…'> and are
// mounted here after every swap (and once on page load for static graphs).
const GRAPH_COLOURS = { Customer: "#4285F4", Product: "#34A853", Category: "#FBBC04" };

function graphStyle() {
  return [
    { selector: "node", style: {
      "background-color": (n) => GRAPH_COLOURS[n.data("label")] || "#9AA0A6",
      label: "data(name)", "font-size": 9, color: "#e6ebf5",
      "text-valign": "bottom", "text-margin-y": 3, width: 18, height: 18,
      "text-wrap": "ellipsis", "text-max-width": 110,
    } },
    { selector: "node[ring = 0]", style: { width: 30, height: 30, "font-weight": "bold", "border-width": 3, "border-color": "#1A73E8" } },
    { selector: "edge", style: {
      width: 1.2, "line-color": "#3a4a6b", "target-arrow-color": "#3a4a6b",
      "target-arrow-shape": "triangle", "curve-style": "bezier", "arrow-scale": 0.7,
    } },
    { selector: "edge[kind = 'schema']", style: { label: "data(label)", "font-size": 10, width: 2, "line-color": "#8d99b3", "target-arrow-color": "#8d99b3", color: "#e6ebf5", "text-background-color": "#111827", "text-background-opacity": 1 } },
    { selector: "edge[kind = 'in_category']", style: { "line-style": "dashed" } },
    // Lab 11: a purchase only ONE engine holds (e.g. written by the agent in Step 8)
    // takes that engine's colour. Seeded purchases sit in all three and stay neutral.
    { selector: "edge[origin = 'BigQuery']", style: { width: 2.5, "line-color": "#4285F4", "target-arrow-color": "#4285F4" } },
    { selector: "edge[origin = 'AlloyDB']", style: { width: 2.5, "line-color": "#34A853", "target-arrow-color": "#34A853" } },
    { selector: "edge[origin = 'Spanner']", style: { width: 2.5, "line-color": "#EA4335", "target-arrow-color": "#EA4335" } },
    { selector: ".faded", style: { opacity: 0.15 } },
    { selector: ".trace", style: { "line-color": "#EA4335", "target-arrow-color": "#EA4335", width: 2.5, opacity: 1 } },
    { selector: "node.trace", style: { "border-width": 3, "border-color": "#EA4335", opacity: 1 } },
  ];
}

function graphLayout(kind) {
  if (kind === "schema") {
    return { name: "grid", rows: 1, padding: 20 };
  }
  // Concentric rings = hop distance from the seed customer (graph_story.RING_*).
  return {
    name: "concentric", padding: 20, minNodeSpacing: 18, startAngle: 1.5 * Math.PI,
    concentric: (n) => 10 - n.data("ring"), levelWidth: () => 1,
  };
}

// Evidence for one recommendation: the peers who bought it, the products those
// peers share with the seed, and the seed's own PURCHASED edges to those products.
function traceRecommendation(cy, rec) {
  cy.elements().removeClass("trace").addClass("faded");
  const peerEdges = rec.incomers("edge[kind = 'peer_recs']");
  const peers = peerEdges.sources();
  const shared = peers.outgoers("edge[kind = 'peer_owns']");
  const seedEdges = cy.edges("[kind = 'owns']").filter((e) => shared.targets().contains(e.target()));
  const path = rec.union(rec.outgoers()).union(peerEdges).union(peers).union(shared)
    .union(shared.targets()).union(seedEdges).union(seedEdges.sources());
  path.removeClass("faded").addClass("trace");
}

function mountGraphs(root) {
  if (typeof cytoscape !== "function") return;
  root.querySelectorAll(".graph-viz[data-graph]:not([data-mounted])").forEach((el) => {
    el.dataset.mounted = "1";
    const g = JSON.parse(el.dataset.graph);
    const cy = cytoscape({
      container: el,
      elements: [
        ...g.nodes.map((n) => ({ data: n })),
        ...g.edges.map((e) => ({ data: e })),
      ],
      style: graphStyle(),
      layout: graphLayout(el.dataset.layout),
      wheelSensitivity: 0.2,
    });
    cy.on("tap", "node", (evt) => {
      const n = evt.target;
      if (n.data("ring") === 3) traceRecommendation(cy, n);
    });
    cy.on("tap", (evt) => {
      if (evt.target === cy) cy.elements().removeClass("trace faded");
    });
    cy.on("mouseover", "node", (evt) => { el.title = `${evt.target.data("label")}: ${evt.target.data("full")}`; });
    cy.on("mouseover", "edge", (evt) => {
      const origin = evt.target.data("origin");
      el.title = origin ? `${evt.target.data("label")} · held by ${origin}` : evt.target.data("label");
    });
  });
}

// --- Chip picker ---------------------------------------------------------------
// A picker block writes the chosen value into a hidden field of its form; the
// first chip is pre-selected so "Recommend" works straight away.
function initPickers(root) {
  root.querySelectorAll(".picker[data-pick-form]").forEach((picker) => {
    const on = picker.querySelector(".pick-on");
    if (on) setPick(picker, on);
  });
}

function setPick(picker, chip) {
  picker.querySelectorAll(".pick").forEach((c) => c.classList.toggle("pick-on", c === chip));
  const input = document.querySelector(`#${picker.dataset.pickForm} [name="${picker.dataset.pickField}"]`);
  if (input) input.value = chip.dataset.pick;
}

document.addEventListener("DOMContentLoaded", () => mountGraphs(document));

// --- Paired queries ------------------------------------------------------------
// In a split, opening or closing one query does the same to its siblings, so the
// two statements are always compared side by side. `toggle` does not bubble,
// hence the capture-phase listener; the equality check stops the echo.
document.addEventListener("toggle", (event) => {
  const q = event.target;
  if (!(q instanceof HTMLDetailsElement) || !q.classList.contains("query")) return;
  q.closest(".split")?.querySelectorAll("details.query").forEach((other) => {
    if (other.open !== q.open) other.open = q.open;
  });
}, true);

document.addEventListener("click", async (event) => {
  const pick = event.target.closest("[data-pick]");
  if (pick) {
    setPick(pick.closest(".picker"), pick);
    return;
  }

  const fill = event.target.closest("[data-fill]");
  if (fill) {
    const values = JSON.parse(fill.dataset.fill);
    for (const [name, value] of Object.entries(values)) {
      const input = document.querySelector(`#write-form [name="${name}"]`);
      if (input) input.value = value;
    }
    document.getElementById("write-form")?.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }

  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;

  const target = document.getElementById(button.dataset.target);
  let body;
  if (button.dataset.form) {
    const form = document.getElementById(button.dataset.form);
    if (!form.reportValidity()) return;
    body = JSON.stringify(Object.fromEntries(new FormData(form)));
  }

  const label = button.textContent;
  const started = performance.now();
  button.disabled = true;
  button.classList.add("busy");
  const tick = setInterval(() => {
    button.textContent = `${label} · ${((performance.now() - started) / 1000).toFixed(0)}s`;
  }, 1000);
  target.classList.add("loading");

  try {
    const resp = await fetch(`/api/${button.dataset.action}`, {
      method: "POST",
      headers: body ? { "Content-Type": "application/json" } : {},
      body,
    });
    target.innerHTML = resp.ok
      ? await resp.text()
      : `<div class="status status-error">HTTP ${resp.status}</div>`;
    initPickers(target);
    mountGraphs(target);
  } catch (err) {
    target.innerHTML = `<div class="status status-error">Network error: ${err}</div>`;
  } finally {
    clearInterval(tick);
    button.textContent = label;
    button.disabled = false;
    button.classList.remove("busy");
    target.classList.remove("loading");
  }
});
