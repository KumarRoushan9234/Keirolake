// Checks the real desktop window: connects to its WebView2 through a debug port and reports what it is showing.
// Usage: node native_check.mjs <debugPort> [screenshot.png]
import fs from "node:fs";
import puppeteer from "puppeteer-core";

const port = process.argv[2];
const shot = process.argv[3];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let browser;
for (let i = 0; i < 90 && !browser; i++) { try { browser = await puppeteer.connect({ browserURL: `http://127.0.0.1:${port}`, defaultViewport: null }); } catch { await sleep(1000); } }
if (!browser) { console.error("could not reach the app's web view"); process.exit(2); }
const pages = await browser.pages();
const page = pages.find((p) => /tauri|localhost|127\.0\.0\.1/.test(p.url())) ?? pages[0];
await page.waitForSelector("main#content, [role=status]", { timeout: 60000 }).catch(() => {});
await sleep(1500);
const info = await page.evaluate(() => ({
  url: location.href, title: document.title, desktop: "__TAURI_INTERNALS__" in window,
  nav: [...document.querySelectorAll("aside nav button")].map((b) => b.getAttribute("aria-label")),
  h1: document.querySelector("main h1")?.textContent ?? null, font: getComputedStyle(document.body).fontFamily.slice(0, 40),
  size: { w: innerWidth, h: innerHeight }, text: document.body.innerText.slice(0, 160).replace(/\n+/g, " | "),
}));
console.log(JSON.stringify(info, null, 2));
if (shot) { await page.screenshot({ path: shot }); console.log("screenshot saved"); }
await browser.disconnect();
process.exit(info.url.includes("localhost:5173") ? 1 : 0);
