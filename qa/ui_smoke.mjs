// Browser checks for the KeiroLake UI. Usage: node ui_smoke.mjs <baseUrl> [--json out.json]
// Needs Microsoft Edge or Chrome (set BROWSER_PATH to override). Exits 1 on any failure.
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import puppeteer from "puppeteer-core";

const base = process.argv[2] ?? "http://127.0.0.1:4173";
const outFile = process.argv.includes("--json") ? process.argv[process.argv.indexOf("--json") + 1] : null;
const PAGES = ["Home", "Ask", "Capture", "Map", "Topics", "Build with AI", "Tables", "Collections", "Files", "Data tools", "Libraries", "Vault", "AI apps", "API keys", "LLM gateway", "Sharing", "Webhooks", "Models", "Jobs", "Settings", "Docs", "Troubleshoot"];
const WIDTHS = [375, 768, 1280, 1920];
const BROWSERS = [
  process.env.BROWSER_PATH,
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/microsoft-edge",
].filter(Boolean);
const executablePath = BROWSERS.find((p) => fs.existsSync(p));
if (!executablePath) { console.error("No Edge/Chrome found; set BROWSER_PATH"); process.exit(2); }
const axeSource = fs.readFileSync(new URL("./node_modules/axe-core/axe.min.js", import.meta.url), "utf8");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const failures = [];
const notes = [];
const fail = (where, what) => failures.push(`${where}: ${what}`);

const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "keirolake-qa-profile-"));
// Edge re-launches itself away from the process we start, so start it by hand and connect to its debug port.
const port = 9400 + Math.floor(Math.random() * 400);
spawn(executablePath, ["--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${userDataDir}`, "--no-sandbox", "--disable-gpu",
  "--no-first-run", "--no-default-browser-check", "--disable-extensions", "about:blank"], { stdio: "ignore", detached: true }).unref();
let browser;
for (let i = 0; i < 60 && !browser; i++) {
  try { browser = await puppeteer.connect({ browserURL: `http://127.0.0.1:${port}` }); } catch { await sleep(500); }
}
if (!browser) { console.error("Could not start the test browser"); process.exit(2); }
const page = await browser.newPage();
const consoleProblems = [];
page.on("console", (m) => { if (m.type() === "error") consoleProblems.push(m.text()); });
page.on("pageerror", (e) => consoleProblems.push(String(e)));
page.on("requestfailed", (r) => consoleProblems.push(`request failed: ${r.url()}`));

// Storage is seeded by a script that runs before the app's own scripts. (Clearing storage after the page has loaded and then
// reloading races with the app's own writes, which made earlier runs start with leftover settings.)
// The token makes the seed apply once per seed() call, so later reloads keep whatever the test saved.
let initScript = null;
async function seed(values) {
  if (initScript) await page.removeScriptToEvaluateOnNewDocument(initScript.identifier);
  const token = Math.random().toString(36).slice(2);
  initScript = await page.evaluateOnNewDocument((vals, token) => {
    if (localStorage.getItem("qa-token") !== token) {
      localStorage.clear();
      sessionStorage.clear();
      for (const [k, v] of Object.entries(vals)) localStorage.setItem(k, v);
      localStorage.setItem("qa-token", token);
    }
  }, values, token);
}

async function open(width, { theme = "light", display = {} } = {}) {
  await page.setViewport({ width, height: width < 500 ? 812 : 900, deviceScaleFactor: 1, isMobile: width < 500 });
  await seed({ "setup-skipped": "1", display: JSON.stringify({ theme, ...display, v: 2 }) });
  await page.goto(base, { waitUntil: "networkidle2" });
  await page.waitForSelector("main#content", { timeout: 20000 });
  await sleep(500);
}

async function goTo(name, width) {
  await page.evaluate(async (name, wide) => {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const header = document.querySelector(`header button[aria-label="${name}"]`);
    if (header) { header.click(); return; }
    if (!wide) { document.querySelector('button[aria-label="Open navigation"]')?.click(); await sleep(200); }
    const root = wide ? "aside nav" : '[role="dialog"][aria-label="Navigation"] nav';
    const find = () => [...document.querySelectorAll(`${root} button:not([aria-expanded])`)].find((b) => (b.getAttribute("aria-label") ?? b.textContent.trim()) === name);
    for (const section of [...document.querySelectorAll(`${root} button[aria-expanded]`)]) {  // sections are dropdowns: open each until the page shows
      if (find()) break;
      if (section.getAttribute("aria-expanded") === "false") { section.click(); await sleep(120); }
    }
    find()?.click();
  }, name, width >= 1024);
  await sleep(900);
}

async function audit(where) {
  return page.evaluate(() => {
    const out = { overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth, h1: document.querySelectorAll("main h1").length, unnamed: [], noAlt: 0 };
    const name = (el) => (el.getAttribute("aria-label") || el.getAttribute("title") || el.textContent || el.labels?.[0]?.textContent || el.getAttribute("placeholder") || "").trim();
    document.querySelectorAll("button, a[href], input:not([type=hidden]), select, textarea").forEach((el) => {
      if (el.hidden || el.closest("[hidden]")) return; // not exposed to assistive tech
      if (!name(el)) out.unnamed.push(el.outerHTML.slice(0, 90));
    });
    document.querySelectorAll("img:not([alt])").forEach(() => out.noAlt++);
    return out;
  });
}

async function axeRun(where) {
  await page.evaluate(axeSource);
  const res = await page.evaluate(async () => {
    // eslint-disable-next-line no-undef
    const r = await axe.run(document, { runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21aa", "best-practice"] }, resultTypes: ["violations"] });
    return r.violations.map((v) => ({ id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.slice(0, 3).map((n) => n.target.join(" ")), count: v.nodes.length }));
  });
  for (const v of res) {
    if (v.impact === "critical" || v.impact === "serious") fail(`${where} axe`, `${v.id} (${v.impact}) ×${v.count}: ${v.help} [${v.nodes[0]}]`);
    else notes.push(`${where} axe ${v.id} (${v.impact}) ×${v.count}`);
  }
}

// 1. every page, every width: renders, no overflow, one heading, named controls
for (const width of WIDTHS) {
  await open(width);
  for (const name of PAGES) {
    const where = `${name}@${width}`;
    await goTo(name, width);
    const text = await page.$eval("main", (m) => m.innerText.length);
    if (text < 20) fail(where, "page is empty");
    const a = await audit(where);
    if (a.overflow > 1) fail(where, `horizontal overflow ${a.overflow}px`);
    if (a.h1 !== 1) fail(where, `expected one h1, found ${a.h1}`);
    if (a.unnamed.length) fail(where, `${a.unnamed.length} control(s) without a name: ${a.unnamed[0]}`);
    if (a.noAlt) fail(where, `${a.noAlt} image(s) without alt`);
  }
}

// 2. accessibility scan with axe: light desktop, dark desktop, light mobile, high contrast
for (const [label, width, opts] of [["light", 1280, {}], ["dark", 1280, { theme: "dark" }], ["mobile", 375, {}], ["high-contrast", 1280, { contrast: true }]]) {
  await open(width, { theme: opts.theme ?? "light", display: opts });
  for (const name of PAGES) { await goTo(name, width); await axeRun(`${name} (${label})`); }
}

// 3. text sizer, access options, keyboard
await open(1280);
const rem = () => page.evaluate(() => parseFloat(getComputedStyle(document.documentElement).fontSize));
const base16 = await rem();
if (Math.abs(base16 - 16) > 0.1) fail("text size", `default should be 16px, got ${base16}`);
await page.click('button[aria-label="Display settings"]');
await page.waitForSelector('[role="dialog"][aria-label="Display settings"]');
for (let i = 0; i < 3; i++) await page.click('button[aria-label="Larger text"]');
const big = await rem();
if (!(big > base16 * 1.2)) fail("text size", `3 steps larger should grow text (16px -> ${big}px)`);
const bigOverflow = (await audit("big")).overflow;
if (bigOverflow > 1) fail("text size", `overflow at ${Math.round((big / 16) * 100)}%: ${bigOverflow}px`);
await page.click('button[aria-label="Reset text size"]');
if (Math.abs((await rem()) - 16) > 0.1) fail("text size", "reset didn't return to 100%");
await page.keyboard.press("Escape");
if (await page.$('[role="dialog"][aria-label="Display settings"]')) fail("display menu", "Escape didn't close it");
await page.keyboard.down("Control"); await page.keyboard.press("="); await page.keyboard.up("Control");
if (!((await rem()) > 16)) fail("text size", "Ctrl + didn't enlarge text");
await page.keyboard.down("Control"); await page.keyboard.press("0"); await page.keyboard.up("Control");
await page.click('button[aria-label="Display settings"]');
await page.waitForSelector('[role="switch"]');
{ // a real mouse click, so a menu hidden behind other content (as happened once) is caught
  const switches = await page.$$('[role="switch"]');
  const labels = await Promise.all(switches.map((s) => s.evaluate((x) => x.closest("label")?.textContent ?? "")));
  const at = labels.findIndex((l) => l.includes("High contrast"));
  if (at < 0) fail("high contrast", "switch not found"); else await switches[at].click();
}
const hcOn = async () => { for (let i = 0; i < 20; i++) { if (await page.evaluate(() => document.documentElement.classList.contains("hc"))) return true; await sleep(100); } return false; };
if (!(await hcOn())) fail("high contrast", "toggle didn't apply");
await page.keyboard.press("Escape");

// persistence across reload
await seed({ "setup-skipped": "1", display: JSON.stringify({ size: 1.25, theme: "dark", v: 2 }) });
await page.goto(base, { waitUntil: "networkidle2" });
await sleep(400);
if (Math.abs((await rem()) - 20) > 0.2 || !(await page.evaluate(() => document.documentElement.classList.contains("dark")))) fail("display", "size/theme not restored after reload");

// skip link is the first tab stop; main takes focus
await open(1280);
await page.keyboard.press("Tab");
if (!(await page.evaluate(() => document.activeElement?.textContent?.includes("Skip to content")))) fail("keyboard", "first Tab stop isn't the skip link");

// command palette: open, filter, run, close
await page.keyboard.down("Control"); await page.keyboard.press("k"); await page.keyboard.up("Control");
await page.waitForSelector('[role="dialog"][aria-label="Command palette"]');
if (!(await page.evaluate(() => document.activeElement?.getAttribute("role") === "combobox"))) fail("palette", "input isn't focused on open");
await page.keyboard.type("troubl");
await sleep(150);
const options = await page.$$eval('[role="option"]', (o) => o.map((x) => x.textContent));
if (!options.some((o) => o.includes("Troubleshoot"))) fail("palette", `filter should list Troubleshoot, got ${options.join("|")}`);
await page.keyboard.press("Enter");
await sleep(700);
if (!(await page.$eval("main h1", (h) => h.textContent.includes("Troubleshoot")))) fail("palette", "Enter didn't open Troubleshoot");
await page.keyboard.down("Control"); await page.keyboard.press("k"); await page.keyboard.up("Control");
await page.keyboard.press("Escape");
if (await page.$('[role="dialog"][aria-label="Command palette"]')) fail("palette", "Escape didn't close it");

// numbered shortcuts and sidebar collapse
await page.keyboard.down("Control"); await page.keyboard.press("2"); await page.keyboard.up("Control");
await sleep(600);
if (!(await page.$eval("main h1", (h) => h.textContent.includes("Ask")))) fail("shortcuts", "Ctrl 2 should open the second page (Ask)");
const wide = await page.$eval("aside", (a) => a.getBoundingClientRect().width);
await page.keyboard.down("Control"); await page.keyboard.press("b"); await page.keyboard.up("Control");
await sleep(500);
const slim = await page.$eval("aside", (a) => a.getBoundingClientRect().width);
if (!(slim < wide * 0.5)) fail("sidebar", `Ctrl B should collapse (${wide}px -> ${slim}px)`);
if ((await audit("slim")).unnamed.length) fail("sidebar", "collapsed sidebar has unnamed controls");
const mainWide = await page.$eval("main", (m) => m.getBoundingClientRect().width);
if (!(mainWide > 900)) fail("layout", `centre column is only ${Math.round(mainWide)}px at 1280`);

// 4. flows: sheets, open data, vault, settings
const clickText = (sel, text) => page.evaluate((sel, text) => { const el = [...document.querySelectorAll(sel)].find((b) => b.textContent.trim().includes(text)); el?.click(); return !!el; }, sel, text);
const bodyText = () => page.$eval("main", (m) => m.innerText);
const waitText = async (needle, ms = 8000) => { const end = Date.now() + ms; while (Date.now() < end) { if ((await bodyText()).includes(needle)) return true; await sleep(250); } return false; };

await open(1280);
await goTo("Tables", 1280);
await clickText("button", "Create");
if (!(await waitText("Blank sheet"))) fail("lake", "Create menu didn't open");
await clickText('[role="menuitem"]', "Blank sheet");
if (!(await waitText("New sheet"))) fail("lake", "blank sheet form didn't open");
await page.type('input[aria-label="Sheet name"]', "QA groceries");
await page.$eval('input[aria-label="Columns"]', (i) => { i.value = ""; });
await page.type('input[aria-label="Columns"]', "item, quantity");
await page.keyboard.press("Enter");
if (!(await waitText("No rows yet"))) fail("sheet", "new sheet didn't open with its grid");
await clickText("button", "Row");
await sleep(900);
await page.keyboard.type("Rice");
await page.keyboard.press("Tab");
await sleep(500);
await page.keyboard.type("5");
await page.keyboard.press("Enter");
if (!(await waitText("Rice"))) fail("sheet", "typed cell value wasn't saved");
await clickText('[role="tab"]', "Schema");
if (!(await waitText("Instruction"))) fail("sheet", "Schema tab didn't show columns");
await clickText('[role="tab"]', "History");
await clickText("button", "Save a copy now");
if (!(await waitText("1 rows", 6000)) && !(await waitText("rows", 3000))) fail("sheet", "history copy not listed");
await clickText('button[aria-label="Back to sheets"]', "");
await page.evaluate(() => document.querySelector('button[aria-label="Back to sheets"]')?.click());
await sleep(700);

await clickText("button", "Create");
await clickText('[role="menuitem"]', "Describe it");
if (!(await waitText("Describe the sheet"))) fail("describe", "describe screen didn't open");
const planDisabled = () => page.evaluate(() => [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === "Plan it")?.disabled);
if (!(await planDisabled())) fail("describe", "Plan it should be disabled with an empty description");
await clickText("button.chip", "electric scooter");
if (await planDisabled()) fail("describe", "choosing an example should enable Plan it");
await page.evaluate(() => [...document.querySelectorAll("main button.btn-ghost")].find((b) => b.textContent.includes("Sheets"))?.click());
await sleep(500);
await clickText("button", "Create");
await clickText('[role="menuitem"]', "From open data");
if (!(await waitText("Life expectancy by country", 10000))) fail("opendata", "catalog didn't list datasets");
if (!(await waitText("CC BY 4.0"))) fail("opendata", "licences aren't shown");
await page.evaluate(() => [...document.querySelectorAll("main button.btn-ghost")].find((b) => b.textContent.includes("Sheets"))?.click());
await sleep(400);

await goTo("Vault", 1280);
if (!(await waitText("There is no recovery"))) fail("vault", "create screen missing");
await page.type('input[type="password"]', "qa passphrase 123");
await clickText("button", "Create vault");
if (!(await waitText("Vault unlocked"))) fail("vault", "didn't unlock after creating");
await clickText("button", "New note");
await page.type('input[placeholder^="Title"]', "Passport");
await page.type("textarea", "Number X1234567");
await clickText("button", "Save encrypted");
if (!(await waitText("Passport"))) fail("vault", "saved note not listed");
await clickText("button", "Lock");
if (!(await waitText("Unlock"))) fail("vault", "lock didn't return to the unlock screen");
await page.type('input[type="password"]', "wrong wrong wrong");
await clickText("button", "Unlock");
if (!(await waitText("Wrong passphrase"))) fail("vault", "wrong passphrase wasn't rejected");

await goTo("Models", 1280);
if (!(await waitText("This computer", 10000))) fail("models", "hardware card missing");
if (!(await waitText("Local first", 10000))) fail("models", "local-first card missing");
if (!(await waitText("Quantized models", 10000))) fail("models", "quantized models card missing");
if (!(await waitText("Installed, as stored", 10000))) fail("models", "installed models table missing");
if (!(await waitText("Heretic", 10000))) fail("models", "Heretic card missing");
if (!(await waitText("AGPL", 5000))) fail("models", "Heretic licence note missing");
await goTo("Map", 1280);
if (!(await waitText("Qdrant vector store", 10000))) fail("map", "vector store box missing");
if (!(await waitText("KeiroLabs web", 5000))) fail("map", "sources column missing");
await clickText("button", "Qdrant vector store");
if (!(await waitText("Connected to", 5000))) fail("map", "details panel didn't open on click");
await clickText("button", "Add box");
if (!(await waitText("Add to the map", 5000))) fail("map", "Add box form didn't open");
await page.type("#bl", "QA dashboard");
await clickText("button", "Add to the map");
if (!(await waitText("QA dashboard", 8000))) fail("map", "the added box didn't appear on the map");
page.once("dialog", (d) => d.accept());
await clickText("button", "Remove");
{ let gone = false; for (let i = 0; i < 20 && !gone; i++) { gone = !(await bodyText()).includes("QA dashboard"); if (!gone) await sleep(250); } if (!gone) fail("map", "removed box still on the map"); }
await clickText("button", "Add connector");
if (!(await waitText("Connect and read now", 5000))) fail("map", "Add connector form didn't open");
if (await waitText("Folder on this computer", 500)) fail("map", "folder connectors must not be offered in a browser");
// welcome guide: opens on request, walks the steps, closes
await page.evaluate(() => window.dispatchEvent(new Event("keirolake:welcome")));
await page.waitForSelector('[role="dialog"][aria-label="Welcome to KeiroLake"]', { timeout: 5000 }).catch(() => fail("welcome", "guide didn't open"));
const guide = () => page.$eval('[role="dialog"][aria-label="Welcome to KeiroLake"]', (d) => d.innerText).catch(() => "");
if (!(await guide()).includes("Collect")) fail("welcome", "intro text missing");
for (const [text, expect] of [["Get started", "Set up on this computer"], ["Continue", "Web access"], ["Continue", "Build your first topic"], ["Continue", "Use your lake from other devices"], ["Continue", "You're ready"]]) {
  await page.evaluate((t) => [...document.querySelectorAll('[role="dialog"] footer button')].find((b) => b.textContent.includes(t))?.click(), text);
  await sleep(500);
  if (!(await guide()).includes(expect)) { fail("welcome", `expected "${expect}" after "${text}"`); break; }
}
await page.evaluate(() => [...document.querySelectorAll('[role="dialog"] footer button')].find((b) => b.textContent.includes("Start using"))?.click());
await sleep(600);
if (await page.$('[role="dialog"][aria-label="Welcome to KeiroLake"]')) fail("welcome", "guide didn't close");
// topics + clean
await goTo("Topics", 1280);
if (!(await waitText("New topic", 8000))) fail("topics", "Topics tab (first tab) missing the New topic button");
await clickText("button", "New topic");
if (!(await waitText("Build topic", 5000))) fail("topics", "new topic form didn't open");
await clickText("button", "Cancel");
await goTo("API keys", 1280);
if (!(await waitText("API keys", 10000))) fail("connect", "API keys card missing");
await goTo("Settings", 1280);
if (!(await waitText("Offline mode", 10000))) fail("settings", "general section missing");
await goTo("Sharing", 1280);
if (!(await waitText("Sharing is changed on the computer", 8000))) fail("sharing", "browser view should explain sharing is changed on the PC");
await goTo("LLM gateway", 1280);
if (!(await waitText("Use KeiroLake as your app's OpenAI", 8000))) fail("gateway", "gateway page missing its address card");
if (!(await waitText("Try it"))) fail("gateway", "playground missing");
await goTo("Tables", 1280);
await clickText('[role="tab"]', "Query");
if (!(await waitText("read-only", 5000))) fail("tables", "query tab missing");
await goTo("Files", 1280);
if (!(await waitText("curated", 8000))) fail("files", "lake folders not listed");
await goTo("Home", 1280);
if (!(await waitText("Start here", 8000))) fail("home", "home page missing quick actions");
await goTo("Troubleshoot", 1280);
if (!(await waitText("Health check", 10000))) fail("troubleshoot", "health check missing");
if (!(await waitText("Database", 15000))) fail("troubleshoot", "diagnostics didn't load");
if (!(await waitText("Save a support bundle"))) fail("troubleshoot", "support bundle button missing");
const bar = await page.$eval('[aria-label="Engine status"]', (f) => f.innerText).catch(() => "");
if (!bar.includes("Engine running")) fail("statusbar", `status bar should say Engine running, got "${bar.slice(0, 80)}"`);

const real = consoleProblems.filter((m) => !/Failed to load resource.*(40[0-9]|50[0-9])/.test(m) && !/favicon/.test(m));
for (const m of [...new Set(real)].slice(0, 8)) fail("console", m.slice(0, 160));

await browser.close();
await sleep(1500);
try { fs.rmSync(userDataDir, { recursive: true, force: true }); } catch { /* Edge may still hold files; the OS temp folder is cleaned later */ }
const summary = { base, browser: executablePath, failures, notes: [...new Set(notes)].slice(0, 20), checked: { pages: PAGES.length, widths: WIDTHS } };
if (outFile) fs.writeFileSync(outFile, JSON.stringify(summary, null, 2));
console.log(failures.length ? `FAIL (${failures.length})\n  - ${failures.join("\n  - ")}` : `PASS: ${PAGES.length} pages × ${WIDTHS.length} widths, axe, text size, keyboard, palette`);
if (notes.length) console.log(`notes: ${summary.notes.join(" | ")}`);
process.exit(failures.length ? 1 : 0);
