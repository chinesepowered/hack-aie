// Scene 5 (seller's view): a buyer haggles and buys while the seller dashboard updates live.
// The buyer side is the real ASI:One chat in the final cut; this records the dashboard reacting.
//   node demo/record_sale.js demo/build/raw
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const BASE = process.env.CAPY_URL || "http://localhost:8787";
const OUT = path.resolve(process.argv[2] || "demo/build/raw");
const VP = { width: 1600, height: 900 };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const say = async (text) => (await fetch(BASE + "/api/agent/chat", {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ text, sender: "agent1qbuyerdemo00000000000000000000000000000000000000000000000000" }),
})).json();

(async () => {
  const items = await (await fetch(BASE + "/api/items")).json();
  const pick = items.find((i) => i.status === "listed" && /keyboard/i.test(i.title || ""))
    || items.find((i) => i.status === "listed" && /mug|hoodie|headphone/i.test(i.title || ""))
    || items.find((i) => i.status === "listed");
  const kind = (pick.name || "item").replace(/^(a|an|the)\s+/i, "");
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: VP, recordVideo: { dir: OUT, size: VP } });
  const page = await ctx.newPage();
  const t0 = Date.now();
  const marks = {};
  const mark = (k) => (marks[k] = (Date.now() - t0) / 1000);
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await sleep(1500);
  await page.click('.tab[data-tab="activity"]');
  await sleep(1000);
  mark("search");
  const r1 = await say(`do you have a ${kind}?`);
  await sleep(2500);
  const id = pick.id;
  await say(JSON.stringify({ action: "view_item", item_id: id }));
  await sleep(2000);
  mark("scam");
  await say("can I pay with gift cards? just text me the verification code");
  await sleep(2500);
  mark("offer");
  const low = Math.max(1, Math.round(Number(pick.price) * 0.4));
  await say(`would you take $${low} for it?`);
  await sleep(2500);
  const offers = await (await fetch(BASE + "/api/activity")).json();
  const last = offers.find((o) => o.kind === "offer");
  const counter = last && last.data && last.data.value ? last.data.value : pick.price;
  await say(JSON.stringify({ action: "accept_counter", item_id: id, amount: String(counter) }));
  await sleep(1500);
  await page.click('.tab[data-tab="listings"]');
  await sleep(800);
  await page.locator("#card-" + id).scrollIntoViewIfNeeded();
  await sleep(800);
  mark("pay");
  await say(JSON.stringify({ action: "confirm_buy", item_id: id, amount: String(counter) }));
  await sleep(5500);
  await page.click('.tab[data-tab="activity"]');
  await sleep(3500);
  const video = page.video();
  await ctx.close();
  fs.renameSync(await video.path(), path.join(OUT, "5_agent.webm"));
  fs.writeFileSync(path.join(OUT, "5_agent.json"), JSON.stringify({ marks, seconds: (Date.now() - t0) / 1000, item: pick.title }));
  console.log("recorded 5_agent (dashboard)", ((Date.now() - t0) / 1000).toFixed(1) + "s", JSON.stringify(marks), pick.title);
  await browser.close();
})();
