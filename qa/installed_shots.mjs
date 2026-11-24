// Screenshots of the installed app's pages at several widths, with the horizontal overflow of each. Read-only.
// Usage: node installed_shots.mjs <debug-port> <out-dir>
import fs from "node:fs";
import puppeteer from "puppeteer-core";

const [port, outDir] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
fs.mkdirSync(outDir, { recursive: true });
const browser = await puppeteer.connect({ browserURL: `http://127.0.0.1:${port}`, defaultViewport: null });
const page = (await browser.pages()).find((p) => p.url().includes("tauri.localhost"));
const cdp = await page.target().createCDPSession();

async function goTo(name, wide) {
  await page.evaluate(async (name, wide) => {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const header = document.querySelector(`header button[aria-label="${name}"]`);
    if (header) { header.click(); return; }
    if (!wide) { document.querySelector('button[aria-label="Open navigation"]')?.click(); await sleep(250); }
    const root = wide ? "aside nav" : '[role="dialog"][aria-label="Navigation"] nav';
    const find = () => [...document.querySelectorAll(`${root} button:not([aria-expanded])`)].find((b) => (b.getAttribute("aria-label") ?? b.textContent.trim()) === name);
    for (const section of [...document.querySelectorAll(`${root} button[aria-expanded]`)]) {  // sections are dropdowns
      if (find()) break;
      if (section.getAttribute("aria-expanded") === "false") { section.click(); await sleep(150); }
    }
    find()?.click();
  }, name, wide);
  await sleep(1400);
}

for (const width of [375, 768, 1280, 1920]) {
  await cdp.send("Emulation.setDeviceMetricsOverride", { width, height: width < 768 ? 812 : 900, deviceScaleFactor: 1, mobile: width < 768 });
  await sleep(500);
  for (const name of ["Home", "Ask", "Map", "Topics", "Build with AI", "Tables", "Collections", "Files", "Data tools", "Libraries", "Vault", "API keys", "LLM gateway", "Sharing", "Webhooks", "Models", "Jobs", "Settings", "Troubleshoot"]) {
    await goTo(name, width >= 1024);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    await page.screenshot({ path: `${outDir}/${name}-${width}.png` });
    console.log(`${name}@${width} overflow=${overflow}`);
  }
}
await cdp.send("Emulation.clearDeviceMetricsOverride");
await goTo("Home", true);
await browser.disconnect();
