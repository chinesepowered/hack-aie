/* Capy Kondo - local demo UI. Vanilla JS, driven by server-sent events from /api/events. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const COLORS = ["#f39a1e", "#4fa463", "#4c8df6", "#e8578a", "#8e6cf0", "#1fb5a8", "#e2b93b", "#e07a5f", "#5cc6f2", "#b07a4b", "#7cc35a", "#f06292"];
const S = {
  scan: null, mode: null, w: 0, h: 0, fps: 8, items: new Map(), order: [], camStream: null, recorder: null,
  agents: {}, brain: null, stats: null, raf: null, pendingLocal: null,
};
const money = (v) => v == null ? "-" : "$" + (Number(v) % 1 === 0 ? Number(v).toFixed(0) : Number(v).toFixed(2));
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const pct = (v) => v == null ? "-" : Math.round(v * 100) + "%";

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

/* ------------------------------------------------------------------ boot */
window.addEventListener("load", async () => {
  bindUI();
  connectSSE();
  await Promise.all([refreshItems(), refreshBrain(), refreshActivity(), refreshStats(), refreshAgents()]);
  await loadLatestScan();
  setInterval(refreshAgents, 8000);
  window.addEventListener("resize", () => { layoutMediaBox(); declutterTags(); });
});

function bindUI() {
  $("#btn-start-cam").onclick = startCamera;
  $("#btn-cam").onclick = () => startCamera();   // always visible: back to the live camera after a scan
  $("#btn-snap").onclick = snapPhoto;
  $("#btn-rec").onclick = recordVideo;
  $("#file").onchange = (e) => e.target.files[0] && uploadFile(e.target.files[0]);
  $("#btn-sample").onclick = () => uploadUrl("/static/sample/table.jpg", "table.jpg");
  $("#btn-sample-video").onclick = () => uploadUrl("/static/sample/table_pan.mp4", "table_pan.mp4");
  $("#cam-select").onchange = (e) => startCamera(e.target.value);
  $$(".tab").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
  $("#drawer-bg").onclick = closeDrawer;
  document.addEventListener("keydown", (e) => e.key === "Escape" && closeDrawer());
}

function showTab(name) {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  ["listings", "brain", "activity"].forEach((n) => ($("#panel-" + n).hidden = n !== name));
}

/* ------------------------------------------------------------------ SSE */
function connectSSE() {
  const es = new EventSource("/api/events");
  es.onmessage = (e) => { try { handle(JSON.parse(e.data)); } catch (err) { console.error(err); } };
  es.onerror = () => setHud("Reconnecting…", false);
}

function handle(evt) {
  const mine = !evt.scan_id || evt.scan_id === S.scan;
  switch (evt.kind) {
    case "scan_started":
      if (!S.pendingLocal && S.scan && evt.via !== "capy-appraiser") break;
      beginScan(evt.scan_id, evt.mode);
      if (evt.via) toast(`📨 <b>Capy Appraiser</b> got a photo from ASI:One`, "capy_scan");
      break;
    case "scan_media": if (mine) showMedia(evt); break;
    case "stage": if (mine) onStage(evt); break;
    case "items_found": if (mine) onItemsFound(evt.items); break;
    case "item_segmented": if (mine) onSegmented(evt.item); upsertItem(evt.item); break;
    case "trust_start": break;
    case "item_ready": upsertItem(evt.item); if (mine) placeTag(evt.item); refreshStats(); break;
    case "item_failed": break;
    case "scan_done": if (mine) onScanDone(evt); refreshStats(); break;
    case "scan_failed": if (mine) { setHud("Scan failed: " + evt.error, false); toast("😿 Scan failed: " + esc(evt.error)); } break;
    case "brain": onBrain(evt); break;
    case "sold": onSold(evt); break;
    case "activity": addFeed(evt, true); break;
  }
}

/* ------------------------------------------------------------------ camera + capture */
async function startCamera(deviceId) {
  try {
    if (S.camStream) S.camStream.getTracks().forEach((t) => t.stop());
    const constraints = { video: deviceId && typeof deviceId === "string" ? { deviceId: { exact: deviceId }, width: { ideal: 1920 } }
      : { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } }, audio: false };
    S.camStream = await navigator.mediaDevices.getUserMedia(constraints);
    const cam = $("#cam");
    cam.srcObject = S.camStream;
    await cam.play();
    showLayer("cam");
    $("#stage").classList.remove("has-result");
    S.w = 0;
    $("#btn-snap").disabled = false;
    $("#btn-rec").disabled = false;
    $("#empty").hidden = true;
    setHud("Live camera - point at your clutter", false);
    const devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
    const sel = $("#cam-select");
    if (devs.length > 1) {
      sel.hidden = false;
      sel.innerHTML = devs.map((d, i) => `<option value="${d.deviceId}">${esc(d.label || "Camera " + (i + 1))}</option>`).join("");
      const cur = S.camStream.getVideoTracks()[0].getSettings().deviceId;
      if (cur) sel.value = cur;
    }
    clearOverlay();
    layoutMediaBox();
  } catch (e) {
    toast("📷 Camera unavailable (" + esc(e.message) + "). Use Upload or a sample instead.");
  }
}

function snapPhoto() {
  const cam = $("#cam");
  if (!cam.videoWidth) return;
  const c = document.createElement("canvas");
  c.width = cam.videoWidth; c.height = cam.videoHeight;
  c.getContext("2d").drawImage(cam, 0, 0);
  flash();
  c.toBlob((b) => uploadFile(new File([b], "snap.jpg", { type: "image/jpeg" })), "image/jpeg", 0.92);
}

function recordVideo() {
  if (!S.camStream) return;
  const types = ["video/mp4;codecs=avc1", "video/mp4", "video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm"];
  const mimeType = types.find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || "";
  const rec = new MediaRecorder(S.camStream, mimeType ? { mimeType, videoBitsPerSecond: 6_000_000 } : {});
  const chunks = [];
  rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  rec.onstop = () => {
    const type = rec.mimeType || "video/webm";
    const ext = type.includes("mp4") ? "mp4" : "webm";
    uploadFile(new File([new Blob(chunks, { type })], "scan." + ext, { type }));
    $("#btn-rec").classList.remove("rec");
  };
  rec.start(250);
  $("#btn-rec").classList.add("rec");
  let left = 6;
  setHud(`Recording… ${left}s - pan slowly across your things`, true);
  const t = setInterval(() => {
    left -= 1;
    if (left <= 0) { clearInterval(t); rec.stop(); }
    else setHud(`Recording… ${left}s - pan slowly across your things`, true);
  }, 1000);
}

async function uploadUrl(url, name) {
  const b = await (await fetch(url)).blob();
  uploadFile(new File([b], name, { type: b.type }));
}

async function uploadFile(file) {
  S.pendingLocal = URL.createObjectURL(file);
  const fd = new FormData();
  fd.append("file", file);
  setHud("Uploading…", true);
  showLayer(null);
  $("#empty").hidden = true;
  if (file.type.startsWith("image/")) { $("#shot").src = S.pendingLocal; showLayer("shot"); }
  try {
    const r = await fetch("/api/scan", { method: "POST", body: fd });
    const j = await r.json();
    beginScan(j.scan_id, j.mode);
  } catch (e) {
    toast("Upload failed: " + esc(e.message));
  }
}

function flash() {
  const st = $("#stage");
  st.animate([{ filter: "brightness(2.2)" }, { filter: "brightness(1)" }], { duration: 350 });
}

/* ------------------------------------------------------------------ scan rendering */
function beginScan(id, mode) {
  if (S.scan === id) return;
  S.scan = id; S.mode = mode; S.order = [];
  clearOverlay();
  $$(".pipeline li").forEach((li) => { li.classList.remove("active", "done"); $(".p-time", li).textContent = ""; });
  setHud("Capy is looking…", true);
  $("#hud-total").hidden = false;
  $("#hud-total-v").textContent = "$0";
  showTab("listings");
}

function showLayer(which) {
  ["cam", "shot", "clip"].forEach((k) => ($("#" + k).style.display = k === which ? "block" : "none"));
  if (which !== "clip") { const c = $("#clip"); c.pause(); }
}

function showMedia(evt) {
  S.w = evt.width; S.h = evt.height; S.fps = evt.fps || 8; S.mode = evt.mode;
  if (evt.mode === "video") {
    const clip = $("#clip");
    clip.src = evt.url;
    clip.play().catch(() => {});
    showLayer("clip");
    startVideoOverlay();
  } else {
    $("#shot").src = evt.url;
    showLayer("shot");
  }
  $("#overlay").setAttribute("viewBox", `0 0 ${S.w} ${S.h}`);
  $("#stage").classList.add("has-result");
  layoutMediaBox();
  $("#empty").hidden = true;
}

function layoutMediaBox() {
  const box = $("#media-box"), stage = $("#stage");
  const live = $("#cam").style.display === "block" || !S.w;
  if (live) { box.style.width = "100%"; box.style.height = "100%"; return; }
  const W = stage.clientWidth, H = stage.clientHeight, a = S.w / S.h;
  let w = W, h = W / a;
  if (h > H) { h = H; w = H * a; }
  box.style.width = w + "px"; box.style.height = h + "px";
}

function clearOverlay() {
  $("#overlay").innerHTML = `<defs><filter id="glow" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="2.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>`;
  $("#tags").innerHTML = "";
  if (S.raf) cancelAnimationFrame(S.raf);
  S.raf = null;
}

function colorFor(id) {
  let i = S.order.indexOf(id);
  if (i < 0) { S.order.push(id); i = S.order.length - 1; }
  return COLORS[i % COLORS.length];
}

function onStage(evt) {
  const map = { upload: "discover", discover: "discover", segment: "segment", appraise: "appraise" };
  const li = $(`.pipeline li[data-stage="${map[evt.stage] || evt.stage}"]`);
  if (!li) return;
  if (evt.status === "start") {
    li.classList.add("active");
    const msg = { discover: "Muse Spark is finding every item…", segment: "SAM 3.1 is cutting out real pixels…",
      appraise: "Inspecting, pricing from live listings, verifying with SubQ…" }[evt.stage];
    if (msg) setHud(msg, true);
  } else {
    if (evt.stage === "upload") return;
    li.classList.remove("active"); li.classList.add("done");
    if (evt.seconds != null) $(".p-time", li).textContent = evt.seconds + "s";
  }
}

function onItemsFound(items) {
  const svg = $("#overlay");
  items.forEach((it, k) => {
    const c = colorFor(it.id);
    const [x1, y1, x2, y2] = it.box.map((v, i) => (i % 2 === 0 ? (v * S.w) / 1000 : (v * S.h) / 1000));
    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    g.id = "det-" + it.id;
    g.innerHTML = `<rect class="det-box" x="${x1}" y="${y1}" width="${x2 - x1}" height="${y2 - y1}" rx="8" style="stroke:${c};animation-delay:${k * 90}ms"/>`;
    svg.appendChild(g);
    addTag(it.id, (x1 + x2) / 2, y1, `<span>${esc(it.name)}</span><small>Muse found it</small>`, true, c, k * 90);
  });
  setHud(`Muse found ${items.length} items - segmenting…`, true);
}

function onSegmented(item) {
  const d = item.data || {};
  const det = $("#det-" + item.id);
  if (det) det.remove();
  const c = colorFor(item.id);
  const svg = $("#overlay");
  let p = $("#mask-" + item.id);
  if (!p) {
    p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.id = "mask-" + item.id;
    p.setAttribute("class", "mask");
    p.setAttribute("fill", c + "55");
    p.setAttribute("stroke", c);
    p.setAttribute("stroke-width", "2.5");
    p.setAttribute("stroke-linejoin", "round");
    p.setAttribute("filter", "url(#glow)");
    svg.appendChild(p);
  }
  p.setAttribute("d", d.outline || "");
  if (d.track) p._track = Object.entries(d.track).map(([f, path]) => [Number(f), path]).sort((a, b) => a[0] - b[0]);
  const [l, t, r] = d.bbox || [0, 0, 0, 0];
  addTag(item.id, (l + r) / 2, t, `<span>${esc(item.name)}</span><small>pricing…</small>`, true, c);
}

function addTag(id, x, y, html, pending, color, delay = 0) {
  let el = $("#tag-" + id);
  if (!el) {
    el = document.createElement("div");
    el.id = "tag-" + id;
    el.className = "tag";
    el.onclick = () => openDrawer(id);
    $("#tags").appendChild(el);
  }
  el.classList.toggle("pending", !!pending);
  el.style.setProperty("--c", color || colorFor(id));
  el.style.animationDelay = delay + "ms";
  el.style.left = (x / S.w) * 100 + "%";
  el.style.top = Math.max(4, (y / S.h) * 100 - 1) + "%";
  el.innerHTML = html;
  el.dataset.x = x; el.dataset.y = y;
  if (S.mode !== "video") requestAnimationFrame(declutterTags);
}

function declutterTags() {
  if (!S.h) return;
  const tags = $$("#tags .tag").sort((a, b) => Number(a.dataset.y) - Number(b.dataset.y));
  const placed = [];
  const H = $("#media-box").clientHeight || 1;
  tags.forEach((t) => {
    t.style.top = Math.max(4, (Number(t.dataset.y) / S.h) * 100 - 1) + "%";
    let r = t.getBoundingClientRect();
    for (let guard = 0; guard < 6; guard++) {
      const hit = placed.find((p) => !(r.right < p.left + 2 || r.left > p.right - 2 || r.bottom < p.top + 2 || r.top > p.bottom - 2));
      if (!hit) break;
      const shift = r.bottom - hit.top + 4;
      t.style.top = `calc(${t.style.top} - ${(shift / H) * 100}%)`;
      r = t.getBoundingClientRect();
    }
    placed.push(r);
  });
}

function placeTag(item) {
  const d = item.data || {};
  if (!d.bbox) return;
  const [l, t, r] = d.bbox;
  const sold = item.status === "sold";
  const verified = d.trust && d.trust.verdict && d.trust.verdict.startsWith("verified");
  const sub = sold ? "SOLD" : `${esc(item.condition || "")}${verified ? " · ✓ SubQ" : ""}`;
  addTag(item.id, (l + r) / 2, t, `<span>${money(item.price)}</span><small>${sub}</small>`, false, colorFor(item.id));
  $("#tag-" + item.id).classList.toggle("sold", sold);
  updateHaulTotal();
}

function updateHaulTotal() {
  const tot = [...S.items.values()].filter((i) => i.scan_id === S.scan && i.price && !["failed", "blocked"].includes(i.status))
    .reduce((s, i) => s + Number(i.price), 0);
  animateNumber($("#hud-total-v"), tot);
}

function animateNumber(el, to) {
  const from = Number(el.dataset.v || 0);
  el.dataset.v = to;
  const t0 = performance.now();
  const step = (t) => {
    const k = Math.min(1, (t - t0) / 600);
    el.textContent = money(Math.round(from + (to - from) * (1 - Math.pow(1 - k, 3))));
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function startVideoOverlay() {
  const clip = $("#clip");
  const tick = () => {
    const f = Math.round(clip.currentTime * S.fps);
    $$("#overlay path.mask").forEach((p) => {
      if (!p._track || !p._track.length) return;
      let best = null;
      for (const [k, path] of p._track) { if (k <= f) best = path; else break; }
      if (!best) { p.style.visibility = "hidden"; return; }
      const last = p._track[p._track.length - 1][0];
      p.style.visibility = f - last > 3 ? "hidden" : "visible";
      p.setAttribute("d", best);
      const id = p.id.slice(5);
      const tag = $("#tag-" + id);
      if (tag) {
        const nums = best.match(/-?\d+(\.\d+)?/g).map(Number);
        let minx = 1e9, maxx = -1e9, miny = 1e9;
        for (let i = 0; i < nums.length; i += 2) { minx = Math.min(minx, nums[i]); maxx = Math.max(maxx, nums[i]); miny = Math.min(miny, nums[i + 1]); }
        tag.style.left = (((minx + maxx) / 2) / S.w) * 100 + "%";
        tag.style.top = Math.max(4, (miny / S.h) * 100 - 1) + "%";
        tag.style.visibility = p.style.visibility;
      }
    });
    S.raf = requestAnimationFrame(tick);
  };
  if (S.raf) cancelAnimationFrame(S.raf);
  S.raf = requestAnimationFrame(tick);
}

function onScanDone(evt) {
  const li = $('.pipeline li[data-stage="listed"]');
  li.classList.add("done");
  $(".p-time", li).textContent = evt.seconds + "s";
  setHud(`Listed ${evt.count} items in ${evt.seconds}s - now selling on ASI:One`, false);
  toast(`🍊 Listed <b>${evt.count} items</b> worth <b>${money(evt.total)}</b> in ${evt.seconds}s`, "capy_logo");
  S.pendingLocal = null;
}

function setHud(text, busy) {
  $("#hud-text").textContent = text;
  $("#hud-state").classList.toggle("busy", !!busy);
}

async function loadLatestScan() {
  try {
    const scans = await api("/api/scans");
    const s = scans.find((x) => x.status === "done");
    if (!s) return;
    const d = await api("/api/scans/" + s.id);
    S.scan = s.id;
    const ex = s.extra || {};
    showMedia({ mode: s.mode, url: s.mode === "video" ? ex.video : ex.analysis, width: s.width, height: s.height, fps: ex.fps });
    d.items.filter((i) => i.status !== "failed").forEach((it) => { onSegmented(it); upsertItem(it); placeTag(it); });
    $$(".pipeline li").forEach((li) => li.classList.add("done"));
    $("#hud-total").hidden = false;
    setHud("Last scan - snap a new one any time", false);
  } catch (e) { console.warn(e); }
}

/* ------------------------------------------------------------------ listings */
async function refreshItems() {
  const items = await api("/api/items");
  items.reverse().forEach((it) => upsertItem(it, true));
}

function upsertItem(it, quiet) {
  const prev = S.items.get(it.id) || {};
  const merged = { ...prev, ...it, data: { ...(prev.data || {}), ...(it.data || {}) } };
  S.items.set(it.id, merged);
  if (["failed", "removed"].includes(merged.status)) { const el = $("#card-" + it.id); if (el) el.remove(); }
  else renderCard(merged, quiet);
  const n = [...S.items.values()].filter((i) => ["listed", "reserved", "sold", "appraising", "processing", "review"].includes(i.status)).length;
  $("#tab-n").textContent = n;
  $("#grid-empty").hidden = n > 0;
  if ($("#drawer").dataset.id === it.id && !$("#drawer").hidden) renderDrawer(merged);
}

function condClass(c) {
  c = (c || "").toLowerCase();
  return c.startsWith("like") ? "c-like" : c ? "c-" + c.split(" ")[0] : "";
}

function renderCard(it, quiet) {
  const d = it.data || {};
  let el = $("#card-" + it.id);
  if (!el) {
    el = document.createElement("div");
    el.id = "card-" + it.id;
    el.className = "card";
    el.onclick = () => openDrawer(it.id);
    $("#grid").prepend(el);
  }
  const img = d.studio || d.white || d.cutout || "";
  const real = d.context || "";
  const busy = ["processing", "appraising"].includes(it.status);
  el.classList.toggle("processing", busy);
  const t = d.trust || {};
  const trustChip = t.verdict ? (t.verdict.startsWith("verified")
    ? `<span class="chip" style="position:absolute;right:8px;bottom:8px;color:#2f7d46">✓ SubQ ${pct(t.photo_matches)}</span>`
    : `<span class="chip" style="position:absolute;right:8px;bottom:8px;color:#b8352b">⚠ ${esc(t.verdict)}</span>`) : "";
  const stamp = it.status === "sold" ? `<div class="stamp"><span>SOLD</span></div>`
    : it.status === "reserved" ? `<div class="stamp reserved"><span>RESERVED</span></div>` : "";
  el.innerHTML = `
    <div class="card-img">${img ? `<img src="${img}" alt="" class="${d.studio ? "" : "contain"}">` : ""}${real ? `<img class="real" src="${real}" alt="">` : ""}
      ${it.condition ? `<span class="chip ${condClass(it.condition)}">${esc(it.condition)}</span>` : ""}${trustChip}${stamp}</div>
    <div class="card-body">
      <div class="card-title">${esc(it.title || it.name || "…")}</div>
      <div class="card-row"><span class="price">${it.price != null ? money(it.price) : "…"}</span>
        <span class="meta">${(d.comps || []).length ? (d.comps.length + " comps") : busy ? "appraising" : ""}${d.claimed ? " · brand fixed" : ""}</span></div>
    </div>`;
}

/* ------------------------------------------------------------------ drawer */
function openDrawer(id) {
  const it = S.items.get(id);
  if (!it) return;
  $("#drawer").dataset.id = id;
  $("#drawer").hidden = false;
  $("#drawer-bg").hidden = false;
  renderDrawer(it);
}

function closeDrawer() { $("#drawer").hidden = true; $("#drawer-bg").hidden = true; }

function renderDrawer(it) {
  const d = it.data || {};
  const t = d.trust || {};
  const comps = (d.comps || []).map((c) => `<li><a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.source || "listing")}</a> - ${money(c.price)}</li>`).join("");
  const flaws = (d.flaws || []).map((f) => `<li>${esc(f)}</li>`).join("");
  const probs = t.condition_probs ? Object.entries(t.condition_probs).sort((a, b) => b[1] - a[1]).slice(0, 3)
    .map(([k, v]) => `${k.replace("_", " ")} ${pct(v)}`).join(" · ") : "";
  const tm = d.timings || {};
  $("#drawer").innerHTML = `
    <div class="d-head"><div><div class="d-title">${esc(it.title || it.name)}</div>
      <div class="meta">${esc(it.brand || "")} ${d.model && d.model !== "Unknown" ? "· " + esc(d.model) : ""} · ${esc(it.category || "")} · ${esc(it.status)}</div></div>
      <button class="d-close" onclick="closeDrawer()">×</button></div>
    <div class="d-imgs">
      <figure><img src="${d.studio || d.white || ""}" alt=""><figcaption>Studio shot - the item is <b>real camera pixels</b> (SAM 3.1 mask); only the backdrop is painted by Nano Banana 2.1</figcaption></figure>
      <figure><img src="${d.context || ""}" alt=""><figcaption>Unedited photo${d.seen ? " · seen at " + esc(d.seen) : ""}</figcaption></figure>
    </div>
    <div class="d-grid">
      <div class="d-box"><h4>Price</h4><div class="v">${money(it.price)}</div>
        <div class="meta">${d.price_source === "seller label" ? "from your price sticker" : "from live market comps"} · market ${money(d.market_price)} · floor ${money(it.floor_price)} (never shown to buyers)</div></div>
      <div class="d-box"><h4>Condition · Gemini 4 Argon</h4><div class="v">${esc(it.condition || "-")}</div>
        ${flaws ? `<ul>${flaws}</ul>` : ""}${d.condition_note ? `<div class="meta">${esc(d.condition_note)}</div>` : ""}</div>
      <div class="d-box"><h4>Trust check · SubQ Snap 1.1</h4>
        ${t.verdict ? `<div class="v">${t.verdict.startsWith("verified") ? "✓ " : "⚠ "}${esc(t.verdict.replace(/_/g, " "))}</div>
        <div class="meta">photo matches ${pct(t.photo_matches)}${t.brand_visible != null ? " · brand visible " + pct(t.brand_visible) : ""}${probs ? "<br>condition: " + probs : ""}${(t.flags || []).length ? "<br>flags: " + t.flags.join(", ") : ""}${t.seconds ? `<br>${t.seconds}s decision` : ""}</div>
        ${d.claimed ? `<div class="meta" style="margin-top:4px">Muse first claimed <b>${esc(d.claimed.brand)}</b> (${money(d.claimed.price)}); no logo was visible, so the brand was removed and the item re-priced as unbranded.</div>` : ""}` : `<div class="meta">pending…</div>`}</div>
      <div class="d-box"><h4>Price comps · Muse + web search</h4>${comps ? `<ul>${comps}</ul>` : `<div class="meta">-</div>`}</div>
    </div>
    <div class="d-sec"><h3>Fix this listing (Capy learns from it)</h3>
      <div class="form-row">
        <select class="inp" id="fx-field"><option value="price">Price</option><option value="brand">Brand</option><option value="title">Title</option><option value="condition">Condition</option><option value="category">Category</option></select>
        <input class="inp" id="fx-value" placeholder="Correct value, e.g. 5" style="flex:1">
        <button class="btn dark" id="fx-go">Save &amp; teach Capy</button>
      </div>
      <input class="inp" id="fx-note" placeholder="Optional note, e.g. 'hackathon swag sells cheap'" style="width:100%;margin-top:8px">
    </div>
    <div class="d-sec"><h3>Try it as a buyer</h3>
      <div class="form-row"><input class="inp" id="by-amt" type="number" placeholder="Offer $" style="width:120px">
        <button class="btn ghost" id="by-offer">Make offer</button>
        <button class="btn primary" id="by-buy" ${it.status !== "listed" ? "disabled" : ""}>Buy now ${money(it.price)}</button>
        <button class="btn ghost" id="by-delist">${it.status === "listed" ? "Delist" : "Relist"}</button></div>
      <div id="by-reply"></div>
    </div>
    <div class="meta" style="margin-top:14px">Timings: appraise ${tm.appraise ?? "-"}s (Muse ${d.searches ?? 0} web searches) · inspect ${tm.inspect ?? "-"}s · SAM confidence ${d.sam_confidence ? pct(d.sam_confidence) : "-"}</div>`;
  $("#fx-go").onclick = async () => {
    const field = $("#fx-field").value, value = $("#fx-value").value.trim();
    if (!value) return;
    await api(`/api/items/${it.id}/correct`, { method: "POST", body: JSON.stringify({ field, value, note: $("#fx-note").value }) });
    toast(`📝 Fixed ${field} → <b>${esc(value)}</b>. Capy is turning it into a lesson…`, "capy_brain");
    closeDrawer(); showTab("brain");
  };
  $("#by-offer").onclick = async () => {
    const amount = Number($("#by-amt").value);
    if (!amount) return;
    const r = await api(`/api/items/${it.id}/offer`, { method: "POST", body: JSON.stringify({ amount, buyer: "web-buyer" }) });
    const s = r.strategy ? `<div class="meta">SubQ strategy: <b>${esc(r.strategy.strategy.replace(/_/g, " "))}</b> (${pct(r.strategy.confidence)} confidence) · floor enforced in code</div>` : "";
    $("#by-reply").innerHTML = `<div class="reply">🍊 ${esc(r.message)}${s}</div>`;
  };
  $("#by-buy").onclick = async () => {
    const r = await api(`/api/items/${it.id}/buy`, { method: "POST", body: JSON.stringify({ buyer: "web-buyer" }) });
    if (r.checkout && r.checkout.url) window.open(r.checkout.url, "_blank");
    closeDrawer();
  };
  $("#by-delist").onclick = async () => {
    await api(`/api/items/${it.id}/status`, { method: "POST", body: JSON.stringify({ status: it.status === "listed" ? "draft" : "listed" }) });
  };
}

/* ------------------------------------------------------------------ brain */
async function refreshBrain() {
  S.brain = await api("/api/brain");
  renderBrain();
}

function renderBrain() {
  const b = S.brain;
  if (!b) return;
  $("#brain-v").textContent = "v" + b.version;
  $("#tab-v").textContent = "v" + b.version;
  $("#brain-list").innerHTML = b.lessons.map(lessonHTML).join("") || `<div class="panel-empty">No lessons yet. Fix a listing and Capy will try to learn from it.</div>`;
  $("#brain-log").textContent = b.log.map((l) => `${l.hash}  ${l.message}`).join("\n");
}

function lessonHTML(l) {
  const d = l.data || {};
  const sb = l.score_before, sa = l.score_after;
  const bars = sb != null ? `<div class="scorebar"><span>held-out</span><div class="bar ${sa < sb ? "down" : ""}"><i class="a" style="width:${sa * 100}%"></i><i class="b" style="width:${Math.min(sb, sa) * 100}%"></i></div>
    <span>${pct(sb)} → <b>${pct(sa)}</b> · n=${l.n_eval}</span></div>` : "";
  const cases = (d.cases || []).map((c, i) => `<span class="case ${i === 0 && c.source !== "starter benchmark" ? (c.after >= 1 ? "fix" : "bad") : c.after < c.before ? "bad" : c.after > 0 ? "ok" : ""}" title="${esc(c.source)}: ${c.before}→${c.after}"></span>`).join("");
  const label = { adopted: `✅ adopted · v${l.version}`, rolled_back: "↩️ rolled back", evaluating: "🧪 testing" }[l.status] || l.status;
  return `<div class="lesson"><div class="lesson-top"><span class="pill ${l.status}">${label}</span><span class="pill task">${esc(l.field)}</span>
    ${d.reason ? `<span class="meta">${esc(d.reason)}</span>` : ""}</div>
    <div class="lesson-text">${esc(l.text)}</div>${bars}${cases ? `<div class="cases">${cases}</div>` : ""}</div>`;
}

function onBrain(evt) {
  const live = $("#brain-live");
  if (evt.phase === "proposing") {
    live.innerHTML = `<div class="lesson live-eval"><div class="lesson-top"><span class="pill evaluating">🧪 coaching</span><span class="pill task">${esc(evt.field)}</span></div>
      <div class="lesson-text">Gemini 4 Argon is turning “${esc(evt.old)} → ${esc(evt.new)}” into a general lesson…</div></div>`;
    $("#tab-v").textContent = "learning…";
  } else if (evt.phase === "evaluating") {
    live.innerHTML = `<div class="lesson live-eval"><div class="lesson-top"><span class="pill evaluating">🧪 testing on ${evt.n_held_out} held-out cases</span><span class="pill task">${esc(evt.task)}</span></div>
      <div class="lesson-text">${esc(evt.lesson)}</div><div class="meta" style="margin-top:6px">Running the playbook with and without this lesson · SubQ Snap referees the answers…</div></div>`;
  } else if (evt.phase === "decided") {
    live.innerHTML = "";
    const ok = evt.status === "adopted";
    toast(ok ? `✅ Lesson adopted → <b>playbook v${evt.version}</b> (held-out ${pct(evt.score_before)} → ${pct(evt.score_after)})`
      : `↩️ Lesson rolled back: ${esc(evt.reason)}`, ok ? "capy_brain" : "capy_scan");
    refreshBrain();
  }
}

/* ------------------------------------------------------------------ activity */
async function refreshActivity() {
  const rows = await api("/api/activity");
  $("#feed").innerHTML = "";
  rows.reverse().forEach((r) => addFeed({ activity_kind: r.kind, ts: r.ts, ...(r.data || {}) }, false));
}

function feedLine(e) {
  switch (e.activity_kind) {
    case "chat": return ["💬", `${e.sender === "test console" ? "Buyer" : "ASI:One buyer"} → <b>Capy Kondo</b>: ${chatText(e.text)}`];
    case "offer": return ["🤝", `Offer ${money(e.amount)} on ${esc(e.title)} → <b>${esc(e.decision)}</b> ${e.decision === "counter" ? money(e.value) : ""}${e.subq ? ` <span class="s">(SubQ: ${esc(e.subq.strategy)} ${pct(e.subq.confidence)})</span>` : ""}`];
    case "reserved": return ["⏳", `${esc(e.title)} reserved for ${money(e.amount)}`];
    case "sold": return ["🎉", `<b>Sold</b> ${esc(e.title)} for ${money(e.amount)} (${esc(e.method)})`];
    case "correction": return ["📝", `${e.source === "subq" ? "SubQ verifier" : "Seller"} fixed ${esc(e.field)}: ${esc(e.old)} → <b>${esc(e.new)}</b>`];
    case "lesson": return [e.status === "adopted" ? "✅" : "↩️", `Lesson ${e.status === "adopted" ? "adopted (v" + e.version + ")" : "rolled back"}: ${esc(e.lesson)}`];
    case "scan_done": return ["📸", `Scan listed ${e.count} items (${money(e.total)}) in ${e.seconds}s`];
    case "a2a": return ["🔁", `Agent-to-agent: ${esc(e.frm)} → ${esc(e.to)} · ${esc(e.what)}`];
    case "subq": return ["⚡", `SubQ Snap · ${esc(e.what)}${e.query ? ` “${esc(e.query)}” → ${esc((e.top || []).join(", "))}` : ""}${e.scam != null ? ` (scam ${e.scam})` : ""}`];
    case "agents_online": return ["🟢", `Fetch.ai agents online`];
    default: return ["•", esc(e.activity_kind)];
  }
}


const CARD_ACTIONS = { view_item: "opened an item card", offer: "sent an offer", offer_form: "asked to make an offer",
  accept_counter: "accepted the counter-offer", confirm_buy: "tapped Pay", buy: "tapped Buy", browse: "kept browsing", cancel: "declined" };
function chatText(t) {
  t = String(t || "").trim();
  if (t.startsWith("{")) {
    try { const j = JSON.parse(t); return `<i>${esc(CARD_ACTIONS[j.action] || "tapped a card")}${j.amount ? " (" + money(Number(j.amount)) + ")" : ""}</i>`; }
    catch (e) { /* fall through */ }
  }
  return `“${esc(t)}”`;
}

function addFeed(e, live) {
  const [ico, html] = feedLine(e);
  const li = document.createElement("li");
  const ts = new Date((e.ts || Date.now() / 1000) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  li.innerHTML = `<div class="ico">${ico}</div><div><div class="t">${html}</div><div class="s">${ts}</div></div>`;
  $("#feed").prepend(li);
  while ($("#feed").children.length > 80) $("#feed").lastChild.remove();
  if (live && e.activity_kind === "chat" && !String(e.text || "").trim().startsWith("{")) toast(`💬 ${e.sender === "test console" ? "Buyer" : "ASI:One buyer"}: “${esc(e.text)}”`);
  if (live && ["offer", "sold", "lesson"].includes(e.activity_kind)) refreshStats();
}

/* ------------------------------------------------------------------ sold, stats, agents */
function onSold(evt) {
  upsertItem(evt.item);
  placeTag(evt.item);
  confetti();
  toast(`🎉 <b>Sold!</b> ${esc(evt.item.title)} for <b>${money(evt.amount)}</b> to ${esc(evt.buyer)}`, "capy_sold");
  refreshStats();
}

async function refreshStats() {
  try {
    const s = await api("/api/stats");
    S.stats = s;
    $("#st-listed").textContent = s.listed;
    $("#st-value").textContent = money(s.listed_value);
    $("#st-sold").textContent = s.sold;
    $("#st-rev").textContent = money(s.revenue);
  } catch (e) { /* ignore */ }
}

async function refreshAgents() {
  try {
    const a = await api("/api/agents");
    S.agents = a;
    const k = a["capy-kondo"], ap = a["capy-appraiser"];
    $("#agent-pill").classList.toggle("on", !!k);
    $("#agent-pill-text").textContent = k ? "Capy Kondo live on ASI:One" : "Agents offline";
    $("#agents-box").innerHTML = k ? `
      <div><b>Fetch.ai agents</b> · payments: <code>${esc(a.payments)}</code></div>
      <div style="margin-top:6px">🍊 Capy Kondo <code>${esc(k.address.slice(0, 14))}…</code> <a href="${esc(k.profile)}" target="_blank">profile</a> · <a href="${esc(k.inspector)}" target="_blank">inspector</a></div>
      ${ap ? `<div>🔎 Capy Appraiser <code>${esc(ap.address.slice(0, 14))}…</code> <a href="${esc(ap.profile)}" target="_blank">profile</a> · <a href="${esc(ap.inspector)}" target="_blank">inspector</a></div>` : ""}
      ${a.public_url ? `<div style="margin-top:4px">Public URL: <a href="${esc(a.public_url)}" target="_blank">${esc(a.public_url)}</a></div>` : ""}`
      : `<b>Fetch.ai agents</b> not running (start with <code>uv run python -m capykondo</code>).`;
  } catch (e) { /* ignore */ }
}

/* ------------------------------------------------------------------ toasts + confetti */
function toast(html, mascot) {
  const el = document.createElement("div");
  el.className = "toast";
  el.innerHTML = `${mascot ? `<img src="/static/img/${mascot}.png" alt="">` : ""}<div>${html}</div>`;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.style.transition = "opacity .4s"; el.style.opacity = 0; setTimeout(() => el.remove(), 400); }, 5200);
}

function confetti() {
  const c = $("#confetti"), ctx = c.getContext("2d");
  c.width = innerWidth; c.height = innerHeight;
  const parts = Array.from({ length: 160 }, () => ({
    x: innerWidth / 2 + (Math.random() - .5) * 200, y: innerHeight / 3, vx: (Math.random() - .5) * 14, vy: -Math.random() * 14 - 4,
    s: 5 + Math.random() * 6, r: Math.random() * 6, c: COLORS[(Math.random() * COLORS.length) | 0], o: Math.random() < .25 }));
  let t = 0;
  const step = () => {
    ctx.clearRect(0, 0, c.width, c.height);
    parts.forEach((p) => {
      p.vy += .35; p.x += p.vx; p.y += p.vy; p.r += .1;
      ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.r); ctx.fillStyle = p.c;
      if (p.o) { ctx.beginPath(); ctx.arc(0, 0, p.s / 1.4, 0, Math.PI * 2); ctx.fill(); } else ctx.fillRect(-p.s / 2, -p.s / 4, p.s, p.s / 2);
      ctx.restore();
    });
    if (++t < 150) requestAnimationFrame(step); else ctx.clearRect(0, 0, c.width, c.height);
  };
  step();
}
