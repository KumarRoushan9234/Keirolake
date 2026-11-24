// README screenshots from a clean demo lake (sample data only). Usage: node readme_shots.mjs <ui-url> <out-dir>
// Start the demo first: an engine with a fresh DATA_DIR on its own port, and Vite with API_TARGET pointing at it.
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import puppeteer from "puppeteer-core";

const [base = "http://localhost:5180", outDir = "../docs/screenshots"] = process.argv.slice(2);
const exe = ["C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe"].find((p) => fs.existsSync(p));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
fs.mkdirSync(outDir, { recursive: true });
const port = 9400 + Math.floor(Math.random() * 400);
spawn(exe, ["--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${fs.mkdtempSync(path.join(os.tmpdir(), "kl-shots-"))}`,
  "--no-first-run", "--disable-extensions", "--hide-scrollbars", "about:blank"], { stdio: "ignore", detached: true }).unref();
let browser;
for (let i = 0; i < 60 && !browser; i++) { try { browser = await puppeteer.connect({ browserURL: `http://127.0.0.1:${port}` }); } catch { await sleep(500); } }
const page = await browser.newPage();

async function goTo(name) {
  await page.evaluate(async (name) => {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const root = "aside nav";
    const find = () => [...document.querySelectorAll(`${root} button:not([aria-expanded])`)].find((b) => (b.getAttribute("aria-label") ?? b.textContent.trim()) === name);
    for (const section of [...document.querySelectorAll(`${root} button[aria-expanded]`)]) {
      if (find()) break;
      if (section.getAttribute("aria-expanded") === "false") { section.click(); await sleep(150); }
    }
    find()?.click();
  }, name);
  await sleep(1500);
}
const shot = async (file) => { await page.screenshot({ path: `${outDir}/${file}.png` }); console.log("saved", file); };
async function say(text, waitFor) {
  await page.click('textarea[aria-label="Message"]');
  await page.type('textarea[aria-label="Message"]', text);
  await page.keyboard.press("Enter");
  await page.waitForFunction(waitFor, { timeout: 240000, polling: 1000 }).catch(() => console.log("timed out waiting:", text));
  await sleep(1500);
}

await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
await page.goto(base, { waitUntil: "networkidle2" });
await sleep(2500);

// Build with AI, and the notice that appears when a fill finishes while you're on another page.
async function builder() {
  const click = (selector, text) => page.evaluate((s, t) => [...document.querySelectorAll(s)].find((b) => b.textContent.trim().startsWith(t))?.click(), selector, text);
  const idle = () => page.waitForFunction(() => !document.querySelector("main .skeleton"), { timeout: 240000, polling: 1000 });
  await goTo("Build with AI");
  await click("main button", "New table"); await sleep(1500);
  await click("main button", "Every Indian state"); await sleep(1500); await idle();
  await page.type('textarea[aria-label="Message"]', "Columns: state, capital, population (number) and official_language. Agreed, go ahead.");
  await page.keyboard.press("Enter"); await sleep(1500); await idle(); await sleep(1000);
  await page.evaluate(() => { const i = document.querySelector("aside input[type=number]"); const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value"); d.set.call(i, "6"); i.dispatchEvent(new Event("input", { bubbles: true })); });
  await sleep(300);
  await click("aside button", "Create and fill"); await sleep(1500);
  await goTo("Tables");
  let seen = "";
  for (let i = 0; i < 300 && !/Done: Build/.test(seen); i++) {
    await sleep(1000);
    seen = await page.evaluate(() => [...document.querySelectorAll('[role="status"]')].map((e) => e.textContent).join(" | "));
  }
  if (!/Done: Build/.test(seen)) { console.log("no notice; saw:", seen); await shot("notices-missing"); return; }
  await sleep(800); await shot("notices");
  await page.evaluate(() => [...document.querySelectorAll('[role="status"] button')].filter((b) => b.textContent === "Open").pop()?.click());
  await sleep(3000); await shot("build-with-ai");
}
if (process.argv[4] === "builder") { await builder(); await browser.close(); process.exit(0); }

await goTo("Home"); await shot("home");
await goTo("Tables"); await sleep(1500);
await page.evaluate(() => [...document.querySelectorAll("button, a")].find((b) => b.textContent.includes("World countries"))?.click());
await sleep(2500); await shot("tables");
await goTo("Ask");
await say("What can I do with KeiroLake, and how does it keep my data private?",
  () => !document.querySelector('button[aria-label="Stop"]') && document.querySelectorAll("main p").length > 3);
await sleep(4000); await shot("ask");
await goTo("Build with AI");
await say("A table of the 8 planets of the solar system with name, type (rocky or gas giant), number of moons and distance from the sun in million km.",
  () => document.querySelectorAll("aside ul li").length > 2);
await shot("build-with-ai");
for (const [name, file] of [["Map", "map"], ["Data tools", "data-tools"], ["Libraries", "libraries"], ["Collections", "collections"],
  ["Models", "models"], ["LLM gateway", "llm-gateway"], ["API keys", "api-keys"], ["Sharing", "sharing"]]) {
  await goTo(name); await shot(file);
}
await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 2, isMobile: true });
await page.reload({ waitUntil: "networkidle2" }); await sleep(2500); await shot("mobile-home");
await browser.close();
