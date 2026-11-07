// Scene 5: buyer shops from chat (phone) while the seller dashboard updates live.
//   node demo/record_phone.js demo/build/raw
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");
const BASE = process.env.CAPY_URL || "http://localhost:8787";
const OUT = path.resolve(process.argv[2] || "demo/build/raw");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const items = await (await fetch(BASE + "/api/items")).json();
  const bottle = items.find((i) => /bottle/i.test(i.title || ""));
  if (bottle && bottle.status !== "listed") {
    await fetch(BASE + `/api/items/${bottle.id}/status`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: "listed" }) });
  }
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 }, recordVideo: { dir: OUT, size: { width: 1600, height: 900 } } });
  const page = await ctx.newPage();
  const t0 = Date.now();
  await page.goto(BASE + "/static/video/phone.html", { waitUntil: "networkidle" });
  await sleep(2500);
  await page.evaluate(() => window.runDemo("anything to keep my drinks cold?", "would you take $2 for it?"));
  for (let i = 0; i < 200 && !(await page.evaluate(() => window.DONE)); i++) await sleep(500);
  await sleep(6500);
  const v = page.video();
  await ctx.close();
  fs.renameSync(await v.path(), path.join(OUT, "5_agent.webm"));
  fs.writeFileSync(path.join(OUT, "5_agent.json"), JSON.stringify({ seconds: (Date.now() - t0) / 1000 }));
  console.log("recorded 5_agent (phone)", ((Date.now() - t0) / 1000).toFixed(1) + "s");
  await browser.close();
})();
