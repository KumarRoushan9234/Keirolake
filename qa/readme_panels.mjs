// Close-up README screenshots of single panels. Usage:
//   node readme_panels.mjs demo <ui-url> <out-dir>        Models panels from a clean demo lake
//   node readme_panels.mjs app <debug-port> <out-dir>     Sharing panels from the installed app, with addresses replaced by examples
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import puppeteer from "puppeteer-core";

const [mode, target, outDir] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
fs.mkdirSync(outDir, { recursive: true });

let browser, page;
if (mode === "demo") {
  const exe = ["C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", "C:/Program Files/Microsoft/Edge/Application/msedge.exe"].find((p) => fs.existsSync(p));
  const port = 9400 + Math.floor(Math.random() * 400);
  spawn(exe, ["--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${fs.mkdtempSync(path.join(os.tmpdir(), "kl-panels-"))}`,
    "--no-first-run", "--hide-scrollbars", "about:blank"], { stdio: "ignore", detached: true }).unref();
  for (let i = 0; i < 60 && !browser; i++) { try { browser = await puppeteer.connect({ browserURL: `http://127.0.0.1:${port}` }); } catch { await sleep(500); } }
  page = await browser.newPage();
  // Desktop-only panels (Sharing) render when the shell is present: stand in for it, pointing at the demo engine.
  await page.evaluateOnNewDocument(() => {
    window.__TAURI_INTERNALS__ = { invoke: async (cmd) => (cmd === "engine_info" ? { url: "", token: "", ready: true } : null),
      transformCallback: () => 0, metadata: { currentWindow: { label: "main" }, currentWebview: { label: "main" } } };
  });
  await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
  await page.goto(target, { waitUntil: "networkidle2" });
  await sleep(2000);
  await page.evaluate(() => [...document.querySelectorAll('[role="dialog"] button')].find((b) => /close/i.test(b.getAttribute("aria-label") ?? ""))?.click());
  await page.keyboard.press("Escape");
} else {
  browser = await puppeteer.connect({ browserURL: `http://127.0.0.1:${target}`, defaultViewport: null });
  page = (await browser.pages()).find((p) => p.url().includes("tauri.localhost"));
  const cdp = await page.target().createCDPSession();
  await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
}
await sleep(2000);

async function goTo(name) {
  await page.evaluate(async (name) => {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const find = () => [...document.querySelectorAll("aside nav button:not([aria-expanded])")].find((b) => (b.getAttribute("aria-label") ?? b.textContent.trim()) === name);
    for (const s of [...document.querySelectorAll("aside nav button[aria-expanded]")]) {
      if (find()) break;
      if (s.getAttribute("aria-expanded") === "false") { s.click(); await sleep(150); }
    }
    find()?.click();
  }, name);
  await sleep(2000);
}

// Screenshot the card whose heading starts with `heading`.
async function card(heading, file) {
  const box = await page.evaluate((heading) => {
    const h = [...document.querySelectorAll("main *")].find((e) => e.children.length <= 2 && e.textContent.trim().startsWith(heading) && e.textContent.trim().length < heading.length + 40);
    const c = h?.closest(".card, section");
    if (!c) return null;
    c.scrollIntoView({ block: "start" });
    window.scrollBy(0, -16);
    return null;
  }, heading);
  await sleep(700);
  const rect = await page.evaluate((heading) => {
    const h = [...document.querySelectorAll("main *")].find((e) => e.children.length <= 2 && e.textContent.trim().startsWith(heading) && e.textContent.trim().length < heading.length + 40);
    const r = h?.closest(".card, section")?.getBoundingClientRect();
    return r && { x: r.x, y: r.y, width: r.width, height: Math.min(r.height, 960) };
  }, heading);
  if (!rect) { console.log("not found:", heading); return; }
  await page.screenshot({ path: `${outDir}/${file}.png`, clip: { x: rect.x - 12, y: Math.max(0, rect.y - 12), width: rect.width + 24, height: rect.height + 24 } });
  console.log("saved", file, Math.round(rect.width), "x", Math.round(rect.height));
}

if (mode === "demo") {
  await goTo("Models");
  await card("Quantized models", "models-quantized");
  await card("Heretic", "models-heretic");
  await goTo("Sharing");
  await card("Use your lake from other devices", "sharing");
  await card("Your own internet address", "sharing-own-address");
  await card("Stay reachable", "sharing-stay-reachable");
} else {
  await goTo("Sharing");
  // Show example addresses instead of this computer's real ones (display only; nothing is changed in the app).
  await page.evaluate(() => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n; (n = walker.nextNode());) {
      n.nodeValue = n.nodeValue
        .replace(/https:\/\/[a-z-]+\.trycloudflare\.com/g, "https://example-words-here.trycloudflare.com")
        .replace(/https:\/\/(10|172|192)\.\d+\.\d+\.\d+:(\d+)/g, "https://192.168.1.20:$2");
    }
    for (const i of document.querySelectorAll("input")) if (/trycloudflare|\d+\.\d+\.\d+\.\d+/.test(i.value)) i.value = "";
  });
  await card("Use your lake from other devices", "sharing");
  await card("Your own internet address", "sharing-own-address");
  await card("Stay reachable", "sharing-stay-reachable");
  const cdp = await page.target().createCDPSession();
  await cdp.send("Emulation.clearDeviceMetricsOverride");
  await page.reload();
}
browser.disconnect();
