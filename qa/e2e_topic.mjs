// End to end on an installed KeiroLake: a topic from one sentence to a cleaned sheet, searchable pages, a CSV and a map node.
// Only touches what it creates (the topic, its sheet and collection) and puts the welcome-guide flag back as it found it.
// Usage: node e2e_topic.mjs <debug-port>
import puppeteer from "puppeteer-core";

const port = process.argv[2];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const browser = await puppeteer.connect({ browserURL: `http://127.0.0.1:${port}`, defaultViewport: null });
const page = (await browser.pages()).find((p) => p.url().includes("tauri.localhost"));
const info = await page.evaluate(() => window.__TAURI_INTERNALS__.invoke("engine_info"));
const call = (path, init = {}) => page.evaluate(async (u, t, path, init) => {
  const r = await fetch(u + "/api" + path, { ...init, headers: { "X-KeiroLake-Token": t, "Content-Type": "application/json" } });
  const type = r.headers.get("content-type") ?? "";
  return { s: r.status, b: r.status === 204 ? null : type.includes("json") ? await r.json().catch(() => null) : (await r.text()).slice(0, 4000) };
}, info.url, info.token, path, init);
const results = [];
const check = (name, ok, detail = "") => { results.push(ok); console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  " + detail : ""}`); };

const was = (await call("/onboarding")).b.done;
let topicId;
try {
  // welcome guide
  await call("/onboarding/reset", { method: "POST" });
  await page.evaluate(() => window.dispatchEvent(new Event("keirolake:welcome")));
  await page.waitForSelector('[role="dialog"][aria-label="Welcome to KeiroLake"]', { timeout: 8000 }).catch(() => {});
  await sleep(600);
  const guide = await page.$eval('[role="dialog"][aria-label="Welcome to KeiroLake"]', (d) => d.innerText).catch(() => "");
  check("welcome guide opens in the installed window", guide.includes("Collect") && guide.includes("Hold") && guide.includes("Use"));
  await page.keyboard.press("Escape");
  await sleep(500);

  // topic, start to finish
  const name = "QA solar panels India";
  const made = await call("/topics", { method: "POST", body: JSON.stringify({ name, question: "Which solar panel brands are sold in India, and what are their wattage and price range?" }) });
  check("topic created and build queued", made.s === 201 && !!made.b.job_id, made.b?.detail ?? "");
  topicId = made.b.id;
  let t;
  for (let i = 0; i < 240; i++) { await sleep(2500); t = (await call(`/topics/${topicId}`)).b; if (t.status === "ready" || t.status === "error") break; }
  check("topic finished", t.status === "ready", t.status === "error" ? t.summary.error : `${t.summary.pages} pages, ${t.summary.rows} rows, ${t.summary.columns} columns`);
  if (t.status !== "ready") throw new Error("build did not finish");

  const s = t.summary;
  check("pages gathered", s.pages >= 2);
  check("rows and columns built", s.rows >= 2 && s.columns >= 3, `${s.rows} x ${s.columns}`);
  const rows = (await call(`/sheets/${t.sheet_id}/rows?limit=5`)).b;
  check("every row links to its page", rows.rows.every((r) => /^https?:\/\//.test(r.source_url ?? "")), rows.columns.join(", "));
  const plan = (await call(`/sheets/${t.sheet_id}/clean`, { method: "POST", body: JSON.stringify({ apply: false }) })).b;
  check("cleaning preview works and leaves nothing undone", plan && plan.duplicates_removed === 0 && plan.rows_after === plan.rows_before, JSON.stringify({ trimmed: plan.trimmed_cells, retyped: plan.retyped }));
  const csv = await call(`/sheets/${t.sheet_id}/export?format=csv`);
  check("CSV export", csv.s === 200 && String(csv.b).split("\n").length > 2);
  const numeric = (await call(`/sheets/${t.sheet_id}`)).b.columns.find((c) => c.type === "number");
  const first = (await call(`/sheets/${t.sheet_id}`)).b.columns.find((c) => c.name !== "source_url" && c.type !== "number");
  const chart = await call(`/sheets/${t.sheet_id}/chart?x=${first.name}`);
  check("chart data", chart.s === 200 && chart.b.series.length >= 1, `${first.name}${numeric ? ", numeric " + numeric.name : ""}`);
  const found = await call(`/kbs/${t.kb_id}/search`, { method: "POST", body: JSON.stringify({ query: "solar panel price in India", top_k: 3 }) });
  check("pages searchable by meaning", found.s === 200 && (found.b.results ?? found.b.hits ?? []).length >= 1, `${(found.b.results ?? found.b.hits ?? []).length} hits`);
  const map = (await call("/map")).b;
  check("map shows the topic's sheet and collection", map.nodes.some((n) => n.id === `sheet:${t.sheet_id}`) && map.nodes.some((n) => n.id === `kb:${t.kb_id}`));
  check("map links the web to the sheet or collection", map.edges.some((e) => e.to === `kb:${t.kb_id}`));
  check("CSV kept in the lake folder", !!s.folder && !!s.csv, s.folder);
} catch (e) {
  check("run", false, String(e).slice(0, 200));
} finally {
  if (topicId) { const d = await call(`/topics/${topicId}?remove_data=true`, { method: "DELETE" }); check("cleanup (topic, sheet and collection removed)", d.s === 204); }
  if (was) await call("/onboarding/done", { method: "POST" }); else await call("/onboarding/reset", { method: "POST" });
}
console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
process.exit(results.every(Boolean) ? 0 : 1);
