// Verifies an installed KeiroLake: connects to its window over a WebView2 debug port, waits for the engine,
// then calls the engine API with the app's own address and token. Usage: node installed_check.mjs <port> [shot.png]
import puppeteer from "puppeteer-core";

const port = process.argv[2];
const shot = process.argv[3];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let browser;
for (let i = 0; i < 90 && !browser; i++) { try { browser = await puppeteer.connect({ browserURL: `http://127.0.0.1:${port}`, defaultViewport: null }); } catch { await sleep(1000); } }
if (!browser) { console.error("could not reach the installed app's window"); process.exit(2); }
const page = (await browser.pages()).find((p) => p.url().includes("tauri.localhost")) ?? (await browser.pages())[0];
let info;
for (let i = 0; i < 150; i++) {
  info = await page.evaluate(() => window.__TAURI_INTERNALS__.invoke("engine_info")).catch(() => null);
  if (info?.ready || info?.error) break;
  await sleep(1000);
}
console.log("window:", page.url(), "| engine ready:", info?.ready, "| version:", info?.version, info?.error ? `| error: ${info.error}` : "");
const call = (path, init = {}) => page.evaluate(async (url, token, path, init) => {
  const r = await fetch(url + "/api" + path, { ...init, headers: { "X-KeiroLake-Token": token, "Content-Type": "application/json" } });
  return { status: r.status, body: await r.json().catch(() => null) };
}, info.url, info.token, path, init);
const about = await call("/about");
const health = await call("/health");
const diag = await call("/support/diagnostics");
console.log("about:", JSON.stringify(about.body));
console.log("vector store:", JSON.stringify(health.body?.qdrant));
console.log("diagnostics:", (diag.body ?? []).map((d) => `${d.id}=${d.status}`).join(" "));
const noToken = await page.evaluate(async (url) => (await fetch(url + "/api/net")).status, info.url);
console.log("request without token ->", noToken);
await page.waitForSelector("main#content", { timeout: 30000 }).catch(() => {});
await sleep(1200);
if (shot) await page.screenshot({ path: shot });
console.log("ENGINE", JSON.stringify({ url: info.url, token: info.token }));
await browser.disconnect();
