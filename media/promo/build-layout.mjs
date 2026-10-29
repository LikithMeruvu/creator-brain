// Deterministic layout of the live brain graph for the promo video.
// Usage: node build-layout.mjs [graphJsonUrl]  ->  writes layout.js (window.BRAIN = {...})
import { writeFileSync } from "node:fs";
import { forceSimulation, forceLink, forceManyBody, forceCollide, forceX, forceY } from "d3-force";

const url = process.argv[2] || "http://127.0.0.1:8787/graph.json";
const g = await (await fetch(url)).json();

const keep = new Set(["creator", "topic", "video"]);
const nodes = g.nodes.filter((n) => keep.has(n.group)).map((n) => ({ ...n }));
const ids = new Set(nodes.map((n) => n.id));
const links = g.links.filter((l) => ids.has(l.source) && ids.has(l.target)).map((l) => ({ ...l }));

const creators = nodes.filter((n) => n.group === "creator").sort((a, b) => b.label.localeCompare(a.label)); // Jeremy left, Jeff right
// pin creators apart so each brain gets its own territory (positions are what d3 starts from: deterministic)
const anchor = { [creators[0].label]: [-330, 40], [creators[1]?.label]: [430, -20] };
for (const c of creators) [c.fx, c.fy] = anchor[c.label];
const home = (n) => anchor[n.creator] || [0, 0];

const sim = forceSimulation(nodes)
  .randomSource(() => 0.5)
  .force("link", forceLink(links).id((d) => d.id).distance((l) => (l.source.group === "creator" || l.target.group === "creator" ? 150 : 70)).strength(0.25))
  .force("charge", forceManyBody().strength((d) => (d.group === "topic" ? -260 : d.group === "creator" ? -400 : -26)))
  .force("collide", forceCollide((d) => (d.group === "topic" ? 22 : d.group === "creator" ? 34 : 5)))
  .force("x", forceX((d) => (d.group === "topic" ? 40 : home(d)[0])).strength((d) => (d.group === "video" ? 0.06 : 0.12)))
  .force("y", forceY((d) => (d.group === "topic" ? 30 : home(d)[1])).strength((d) => (d.group === "video" ? 0.06 : 0.12)))
  .stop();
for (let i = 0; i < 600; i++) sim.tick();

// fit into the frame area (1920x1080, leave room for captions)
const xs = nodes.map((n) => n.x), ys = nodes.map((n) => n.y);
const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
const s = Math.min(1560 / (x1 - x0), 880 / (y1 - y0));
const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
const idx = new Map();
const out = nodes.map((n, i) => {
  idx.set(n.id, i);
  return {
    x: +(960 + (n.x - cx) * s).toFixed(1), y: +(560 + (n.y - cy) * s).toFixed(1),
    g: n.group, c: n.creator || (n.group === "creator" ? n.label : ""), label: n.group === "video" ? "" : n.label,
    d: n.degree || 0, t: n.created,
  };
});
const outLinks = links.map((l) => [idx.get(l.source.id), idx.get(l.target.id)]);
writeFileSync("layout.js", "window.BRAIN=" + JSON.stringify({ nodes: out, links: outLinks, creators: creators.map((c) => c.label) }) + ";\n");
console.log(`nodes ${out.length} links ${outLinks.length} creators ${creators.map((c) => c.label).join(", ")}`);
