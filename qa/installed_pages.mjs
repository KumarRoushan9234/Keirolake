// Screenshots of chosen pages in the installed app's own window (read-only). Usage: node installed_pages.mjs <port> <out-dir> <Page>...
import fs from "node:fs";
import puppeteer from "puppeteer-core";

const [port, outDir, ...pages] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
fs.mkdirSync(outDir, { recursive: true });
const browser = await puppeteer.connect({ browserURL: `http://127.0.0.1:${port}`, defaultViewport: { width: 1280, height: 860 } });
const page = (await browser.pages()).find((p) => p.url().includes("tauri.localhost"));
await page.waitForSelector("main#content", { timeout: 120000 });
for (const name of pages) {
  const [id, tab] = name.split(":");
  await page.evaluate((id) => { localStorage.setItem("page", id); location.reload(); }, id);
  await page.waitForSelector("main#content", { timeout: 60000 });
  await sleep(2500);
  if (tab) {
    await page.evaluate((tab) => [...document.querySelectorAll('[role="tab"], [role="radio"]')].find((b) => b.textContent.trim().startsWith(tab))?.click(), tab);
    await sleep(2500);
  }
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  await page.screenshot({ path: `${outDir}/${name.replace(":", "-")}.png` });
  console.log(`${name} overflow=${overflow}`);
}
await page.evaluate(() => { localStorage.setItem("page", "Home"); location.reload(); });
await browser.disconnect();
