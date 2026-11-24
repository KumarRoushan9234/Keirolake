// The README demo: a scripted tour of a clean demo lake, recorded as short clips (waits for the model are cut out),
// captioned, and joined into docs/media/demo.mp4 and docs/media/demo.gif.
//
//   node demo_video.mjs <ui-url> [out-dir]
//
// Start a clean demo lake first (an engine with a fresh DATA_DIR, and Vite with API_TARGET pointing at it), with
// Ollama running a chat model. Needs Edge or Chrome, and ffmpeg on PATH.
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import puppeteer from "puppeteer-core";

const [base = "http://localhost:5180", outDir = "../docs/media"] = process.argv.slice(2);
const W = 1280, H = 800;
const exe = ["C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe", "/usr/bin/google-chrome", "/usr/bin/chromium"].find((p) => fs.existsSync(p));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const work = fs.mkdtempSync(path.join(os.tmpdir(), "kl-demo-"));
fs.mkdirSync(outDir, { recursive: true });

const port = 9400 + Math.floor(Math.random() * 400);
spawn(exe, ["--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${path.join(work, "profile")}`, "--no-first-run",
  "--hide-scrollbars", `--window-size=${W},${H}`, "about:blank"], { stdio: "ignore", detached: true }).unref();
let browser;
for (let i = 0; i < 60 && !browser; i++) { try { browser = await puppeteer.connect({ browserURL: `http://127.0.0.1:${port}`, protocolTimeout: 600000 }); } catch { await sleep(500); } }
const page = await browser.newPage();
await page.setViewport({ width: W, height: H, deviceScaleFactor: 1 });
await page.evaluateOnNewDocument(() => {
  // Only for the Sharing scene: show the desktop app's pages against the demo engine. Light mode, no first-run windows.
  if (sessionStorage.getItem("demo-desktop") === "1") window.__TAURI_INTERNALS__ = { invoke: async (cmd) => (cmd === "engine_info" ? { url: "", token: "", ready: true } : null),
    transformCallback: () => 0, metadata: { currentWindow: { label: "main" }, currentWebview: { label: "main" } } };
  try { localStorage.setItem("display", JSON.stringify({ theme: "light", v: 2 })); localStorage.setItem("updates", JSON.stringify({ auto: false })); } catch {}
  window.addEventListener("DOMContentLoaded", () => {
    const cap = document.createElement("div");
    cap.id = "demo-caption";
    cap.style.cssText = "position:fixed;left:50%;bottom:44px;transform:translateX(-50%);z-index:99999;pointer-events:none;max-width:86%;"
      + "background:rgba(17,24,39,.92);color:#fff;font:600 17px/1.35 Geist,system-ui,sans-serif;padding:10px 18px;border-radius:10px;"
      + "box-shadow:0 8px 24px rgba(0,0,0,.18);text-align:center;opacity:0;transition:opacity .25s";
    document.body.appendChild(cap);
  });
});

// ---------- helpers ----------
// Frames are captured as JPEG screenshots at up to ~12 per second, each with its real duration (divided by the clip's
// speed), so pausing and resuming is instant and ffmpeg only runs once at the end.
const frames = [];  // { file, ms }
let speed = 1, capturing = false, loop = null;
async function capture() {
  let last = Date.now(), n = 0;
  while (capturing) {
    const file = path.join(work, `f${String(frames.length + 1).padStart(5, "0")}.jpg`);
    const shot = await page.screenshot({ type: "jpeg", quality: 82, optimizeForSpeed: true }).catch(() => null);
    const now = Date.now();
    if (shot) {
      fs.writeFileSync(file, shot);
      frames.push({ file, ms: Math.max(30, (now - last) / speed) });
      n++;
    }
    last = now;
    const wait = 80 - (Date.now() - now);
    if (wait > 0) await sleep(wait);
  }
}
async function record(s = 1) {
  if (capturing) return;
  speed = s; capturing = true; loop = capture();
}
async function pause() { if (capturing) { capturing = false; await loop; } }
const caption = (text) => page.evaluate((t) => { const c = document.getElementById("demo-caption"); if (!c) return; c.textContent = t; c.style.opacity = t ? "1" : "0"; }, text);
const click = (selector, text) => page.evaluate((s, t) => { const b = [...document.querySelectorAll(s)].find((x) => x.textContent.trim().startsWith(t)); b?.click(); return !!b; }, selector, text);
let step = "";
async function waitFor(fn, ms = 300000) {
  try { await page.waitForFunction(fn, { timeout: ms, polling: 500 }); }
  catch (e) { await page.screenshot({ path: path.join(outDir, "demo-failed.png") }); throw new Error(`Timed out at: ${step}`); }
}

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
  await sleep(1200);
}

async function type(selector, text) {
  await page.click(selector);
  await page.type(selector, text, { delay: 28 });
}

// ---------- the tour ----------
await page.goto(base, { waitUntil: "networkidle2" });
await sleep(2500);
await page.evaluate(() => [...document.querySelectorAll('[role="dialog"] button')].find((b) => /close/i.test(b.getAttribute("aria-label") ?? ""))?.click());
await page.keyboard.press("Escape");
await goTo("Home");

// 1. Home
await record();
await caption("KeiroLake: your private data lake, on your own computer");
await sleep(3200);

// 2. Ask
await goTo("Ask");
await caption("Ask in plain language. Answered by a model on your computer, with sources");
await sleep(600);
await type('textarea[aria-label="Message"]', "What can I do with KeiroLake, and how does it keep my data private?");
await page.keyboard.press("Enter");
await sleep(2500);
await pause();
step = "ask answer";
await waitFor(() => !document.querySelector('button[aria-label="Stop"]') && document.querySelectorAll("main li, main p").length > 4);
await sleep(800);
await page.evaluate(() => document.querySelector("main [data-scroll], main .overflow-y-auto")?.scrollTo({ top: 0 }));
await record();
await sleep(3500);

// 3. Build with AI
await goTo("Build with AI");
await caption("Build a dataset by talking: the AI proposes the columns, you shape them");
await click("main button", "New table");
await sleep(1200);
await click("main button", "Every Indian state");
await sleep(1500);
await pause();
step = "builder plan";
await waitFor(() => !document.querySelector("main .skeleton") && document.querySelectorAll("main section p.whitespace-pre-wrap").length > 0);
await record();
await sleep(1500);
await type('textarea[aria-label="Message"]', "Columns: state, capital, population and official language. Go ahead.");
await page.keyboard.press("Enter");
await sleep(1200);
await pause();
step = "builder reply 2";
await waitFor(() => !document.querySelector("main .skeleton"));
await record();
await page.evaluate(() => { const i = document.querySelector("aside input[type=number]"); const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value"); d.set.call(i, "8"); i.dispatchEvent(new Event("input", { bubbles: true })); });
await sleep(800);
await caption("It fills the rows, and keeps working when you switch pages");
await click("aside button", "Create and fill");
await sleep(1800);
await goTo("Tables");
await sleep(1000);
await pause();
await record(4);  // the fill keeps going while you're on another page: shown 4x faster
step = "fill notice";
await waitFor(() => /Done: Build/.test([...document.querySelectorAll('[role="status"]')].map((e) => e.textContent).join(" ")), 600000);
await pause();
await record();
await caption("A notice when it's done, wherever you are");
await sleep(2800);
await page.evaluate(() => [...document.querySelectorAll('[role="status"] button')].filter((b) => b.textContent === "Open").pop()?.click());
await sleep(1500);
await caption("Every cell records where it came from");
await sleep(3200);

// 4. Gateway
await goTo("LLM gateway");
await caption("Use it from any app or AI agent: OpenAI-compatible API and MCP");
await sleep(3500);

// 5. Sharing (a desktop-only page): reload as the desktop app for this scene
await pause();
await page.evaluate(() => sessionStorage.setItem("demo-desktop", "1"));
await page.reload({ waitUntil: "networkidle2" });
await sleep(2000);
await page.evaluate(() => [...document.querySelectorAll('[role="dialog"] button')].find((b) => /close/i.test(b.getAttribute("aria-label") ?? ""))?.click());
await page.keyboard.press("Escape");
await goTo("Sharing");
await record();
await caption("Reach it from anywhere: a private link with no open ports");
await sleep(3500);

// 6. Phone (back to a browser, as on a real phone)
await pause();
await page.evaluate(() => sessionStorage.removeItem("demo-desktop"));
await page.reload({ waitUntil: "networkidle2" });
await sleep(1500);
await page.setViewport({ width: 390, height: 780, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
await sleep(800);
await page.evaluate(() => document.querySelector('button[aria-label="Open navigation"]')?.click());
await sleep(400);
await page.evaluate(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const root = '[role="dialog"][aria-label="Navigation"] nav';
  for (const s of document.querySelectorAll(`${root} button[aria-expanded="false"]`)) { if (s.textContent.includes("Workspace")) { s.click(); await sleep(200); } }
  [...document.querySelectorAll(`${root} button`)].find((b) => (b.getAttribute("aria-label") ?? b.textContent.trim()) === "Ask")?.click();
});
await sleep(1500);
await record();
await caption("On your phone too");
await sleep(3500);
await pause();
await browser.close();

// ---------- assemble: the frames, each shown for its own duration, then a small GIF ----------
const list = frames.map((f) => `file '${f.file.replace(/\\/g, "/")}'\nduration ${(f.ms / 1000).toFixed(3)}`).join("\n")
  + `\nfile '${frames[frames.length - 1].file.replace(/\\/g, "/")}'\n`;
fs.writeFileSync(path.join(work, "frames.txt"), list);
const mp4 = path.join(outDir, "demo.mp4"), gif = path.join(outDir, "demo.gif");
execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", path.join(work, "frames.txt"),
  "-vf", `scale=${W}:${H}:force_original_aspect_ratio=decrease,pad=${W}:${H}:(ow-iw)/2:(oh-ih)/2:color=0xf5f7fa,fps=30,format=yuv420p`,
  "-c:v", "libx264", "-preset", "slow", "-crf", "24", "-movflags", "+faststart", mp4]);
for (const [width, fps] of [[960, 12], [840, 10], [720, 8]]) {  // smallest settings that keep the GIF under 10 MB
  execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-i", mp4, "-vf",
    `fps=${fps},scale=${width}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle`, gif]);
  if (fs.statSync(gif).size < 10 * 2 ** 20) break;
}
const mb = (f) => (fs.statSync(f).size / 2 ** 20).toFixed(1);
console.log(`frames: ${frames.length} | ${mp4}: ${mb(mp4)} MB | ${gif}: ${mb(gif)} MB`);
