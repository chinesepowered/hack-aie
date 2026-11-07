"""Scan pipeline: photo or video of clutter -> verified, priced listings.

Muse Spark 1.3 finds and boxes every item  ->  SAM 3.1 segments / tracks them (one request)
->  real-pixel cutouts  ->  Muse + live web search prices them  ->  Gemini 4 Argon inspects condition
->  Nano Banana 2.1 paints a studio backdrop (the item stays real pixels).
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from pathlib import Path

import meta_sam_parser as msp
from PIL import Image

from . import brain, config, llm, media, segment, store, studio

log = logging.getLogger("capy.pipeline")

# ------------------------------------------------------------------ schemas

_ITEM_PROPS = {
    "name": {"type": "string", "description": "short human name"},
    "sam_phrase": {"type": "string", "description": "2-4 word noun phrase, color + object"},
    "category": {"type": "string", "description": "one of: clothing, electronics, drinkware, kitchen, toy, books, stationery, accessories, home, sports, other"},
    "box": {"type": "array", "items": {"type": "integer"},
            "description": "[x1,y1,x2,y2] tight box around the WHOLE item, normalized 0-1000"},
    "visible_label": {"type": "string", "description": "price sticker/label text right next to the item, verbatim, else empty"},
}
DISCOVER_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "properties": _ITEM_PROPS,
        "required": list(_ITEM_PROPS), "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}
_VIDEO_PROPS = dict(_ITEM_PROPS)
_VIDEO_PROPS["time_s"] = {"type": "number", "description": "timestamp in seconds where the item is most clearly and fully visible"}
_VIDEO_PROPS["box"] = {"type": "array", "items": {"type": "integer"},
                       "description": "[x1,y1,x2,y2] tight box around the whole item AT time_s, normalized 0-1000"}
DISCOVER_VIDEO_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "properties": _VIDEO_PROPS,
        "required": list(_VIDEO_PROPS), "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}

APPRAISE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"}, "brand": {"type": "string"}, "model": {"type": "string"},
        "category": {"type": "string"},
        "new_retail_price": {"type": "number"}, "used_low": {"type": "number"}, "used_high": {"type": "number"},
        "suggested_price": {"type": "number"}, "floor_price": {"type": "number"},
        "comps": {"type": "array", "items": {"type": "object", "properties": {
            "source": {"type": "string"}, "url": {"type": "string"}, "price": {"type": "number"}},
            "required": ["source", "url", "price"], "additionalProperties": False}},
        "description": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    "required": ["title", "brand", "model", "category", "new_retail_price", "used_low", "used_high",
                 "suggested_price", "floor_price", "comps", "description", "keywords", "confidence"],
    "additionalProperties": False,
}
CONDITION_SCHEMA = {
    "type": "object",
    "properties": {
        "condition": {"type": "string", "enum": ["new", "like new", "good", "fair", "poor"]},
        "flaws": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["condition", "flaws", "evidence", "confidence"],
}

DISCOVER_PROMPT = (
    "Someone is decluttering and wants to sell the items in front of the camera. "
    "List EVERY distinct sellable item in the foreground (be exhaustive: include small or flat things such as "
    "sticker sheets, cables sold as accessories, bottles, toys). Ignore people, furniture, background tables, "
    "power strips, and sticky notes / price labels themselves (but report a label's text under visible_label "
    "for the item it belongs to). Give each item a 2-4 word noun phrase (color + object) for a segmentation "
    "model, and a tight bounding box covering the WHOLE item as [x1,y1,x2,y2] normalized 0-1000 of the image "
    "width/height."
)


def _emit(scan_id: str, kind: str, **data) -> None:
    store.bus.publish(kind, scan_id=scan_id, **data)


def _stage(scan_id: str, stage: str, status: str, **data) -> None:
    _emit(scan_id, "stage", stage=stage, status=status, **data)


def media_url(path: Path) -> str:
    rel = Path(path).resolve().relative_to(config.MEDIA_DIR.resolve())
    return "/media/" + str(rel).replace("\\", "/")


def _price_from_label(label: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d{1,2})?)", label or "")
    if not m:
        return None
    v = float(m.group(1))
    return v if 0 < v < 100000 else None


def friendly_price(p: float) -> float:
    if p <= 0:
        return 1.0
    if p < 10:
        return float(max(1, round(p)))
    if p < 50:
        return float(round(p))
    if p < 200:
        return float(5 * round(p / 5))
    return float(10 * round(p / 10))


# ------------------------------------------------------------------ discovery

async def discover_photo(image_path: Path) -> tuple[list[dict], dict]:
    prompt = DISCOVER_PROMPT + brain.playbook_section("discovery")
    out, meta = await llm.muse_json(
        [{"type": "input_text", "text": prompt},
         {"type": "input_image", "image_url": llm.data_url(image_path)}],
        DISCOVER_SCHEMA, name="items", effort="low")
    return out.get("items", [])[: config.MAX_ITEMS], meta


async def discover_video(file_id: str, duration: float) -> tuple[list[dict], dict]:
    prompt = (DISCOVER_PROMPT + f" The video is {duration:.1f} seconds long and may pan across the items; for each "
              "item give time_s (when it is most clearly fully visible) and its box at that moment."
              + brain.playbook_section("discovery"))
    out, meta = await llm.muse_json(
        [{"type": "input_text", "text": prompt}, {"type": "input_video", "file_id": file_id}],
        DISCOVER_VIDEO_SCHEMA, name="items", effort="low")
    return out.get("items", [])[: config.MAX_ITEMS], meta


def _box_px(box: list[int], w: int, h: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = (list(box) + [0, 0, 1000, 1000])[:4]
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    l, t = max(0, int(x1 * w / 1000)), max(0, int(y1 * h / 1000))
    r, b = min(w, max(l + 2, int(x2 * w / 1000))), min(h, max(t + 2, int(y2 * h / 1000)))
    return l, t, r, b


async def segment_photo(image_path: Path, items: list[dict]) -> dict[int, segment.Detection]:
    """One SAM 3.1 request with every item's box as an object prompt; concept-prompt fallback."""
    w, h = Image.open(image_path).size
    objs = []
    for i, it in enumerate(items, start=1):
        l, t, r, b = _box_px(it["box"], w, h)
        objs.append(msp.PromptObject(id=i, prompts=[msp.FramePrompt(frame=0, box=msp.PromptBox(l, t, r, b))]))
    found: dict[int, segment.Detection] = {}
    for batch_start in range(0, len(objs), 16):
        batch = objs[batch_start: batch_start + 16]
        text = msp.build_object_prompt(size=(w, h), objects=batch)
        res = await llm.sam_segment(text, image_path=image_path)
        for d in segment.detections_from_result(res):
            if d.mask is not None:
                found[int(d.object_id)] = d

    async def fallback(i: int, it: dict) -> None:
        l, t, r, b = _box_px(it["box"], w, h)
        box_area = max(1, (r - l) * (b - t))
        d = found.get(i)
        if d is not None and d.mask_area() >= 0.12 * box_area:
            return
        try:
            res = await llm.sam_segment(it["sam_phrase"], image_path=image_path)
            cand = segment.pick_detection(segment.detections_from_result(res), ((l + r) / 2, (t + b) / 2))
            if cand is not None:
                cand.object_id = str(i)
                found[i] = cand
        except Exception as e:  # noqa: BLE001
            log.warning("concept fallback failed for %s: %s", it.get("sam_phrase"), e)

    await asyncio.gather(*[fallback(i, it) for i, it in enumerate(items, start=1)])
    return found


async def track_video(file_id: str, items: list[dict], w: int, h: int, fps: float, n_frames: int
                      ) -> dict[int, segment.Track]:
    """Track every item through the analysis video with SAM 3.1: one box-prompted request per item, in parallel.

    The box comes from Muse at the moment the item is most visible; SAM propagates it both ways in time.
    Falls back to a concept prompt (the item's noun phrase) if the box prompt yields no track.
    """
    sem = asyncio.Semaphore(8)

    async def one(i: int, it: dict) -> tuple[int, segment.Track | None]:
        f = int(min(max(0, round(float(it.get("time_s", 0)) * fps)), max(0, n_frames - 1)))
        l, t, r, b = _box_px(it["box"], w, h)
        async with sem:
            # 1) concept prompt (the item's noun phrase); Muse's rough box only disambiguates between matches
            try:
                res = await llm.sam_segment(it["sam_phrase"], video_file_id=file_id)
                trs = [tr for tr in segment.tracks_from_result(res).values() if len(tr.detections) >= 2]
                if trs:
                    cx, cy = (l + r) / 2, (t + b) / 2

                    def rank(tr: segment.Track) -> float:
                        d = tr.detections.get(f) or tr.detections[min(tr.detections, key=lambda k: abs(k - f))]
                        dist = (((d.left + d.right) / 2 - cx) / w) ** 2 + (((d.top + d.bottom) / 2 - cy) / h) ** 2
                        return len(tr.detections) * tr.mean_area() / (1 + 25 * dist)
                    return i, max(trs, key=rank)
            except Exception as e:  # noqa: BLE001
                log.warning("concept track failed for %s: %s", it.get("sam_phrase"), e)
            # 2) fall back to Muse's box on its best frame
            try:
                text = msp.build_object_prompt(size=(w, h), objects=[
                    msp.PromptObject(id=1, prompts=[msp.FramePrompt(frame=f, box=msp.PromptBox(l, t, r, b))])])
                res = await llm.sam_segment(text, video_file_id=file_id)
                tr = segment.tracks_from_result(res).get("1")
                if tr and tr.detections:
                    return i, tr
            except Exception as e:  # noqa: BLE001
                log.warning("box track failed for %s: %s", it.get("sam_phrase"), e)
        return i, None

    results = await asyncio.gather(*[one(i, it) for i, it in enumerate(items, start=1)])
    tracks = {i: tr for i, tr in results if tr is not None}
    # drop duplicates: two phrases that ended up tracking the same physical object
    keep: dict[int, segment.Track] = {}
    for i in sorted(tracks):
        if not any(_track_iou(tracks[i], tracks[j]) > 0.6 for j in keep):
            keep[i] = tracks[i]
        else:
            log.info("dropping duplicate track for item %d", i)
    return keep


def _track_iou(a: segment.Track, b: segment.Track) -> float:
    common = set(a.detections) & set(b.detections)
    if not common:
        return 0.0
    vals = []
    for f in list(common)[:: max(1, len(common) // 8)]:
        x, y = a.detections[f], b.detections[f]
        iw = max(0, min(x.right, y.right) - max(x.left, y.left))
        ih = max(0, min(x.bottom, y.bottom) - max(x.top, y.top))
        inter = iw * ih
        union = x.area_box + y.area_box - inter
        vals.append(inter / union if union else 0.0)
    return sum(vals) / len(vals)


def best_detection(track: segment.Track, w: int, h: int) -> segment.Detection | None:
    """Largest, most confident view of the item that isn't cut off by the frame edge (no mask decoding)."""
    best, best_score = None, -1.0
    for d in track.detections.values():
        edge = d.left <= 2 or d.top <= 2 or d.right >= w - 2 or d.bottom >= h - 2
        score = d.area_box * (0.35 if edge else 1.0) * (d.confidence or 0.8)
        if score > best_score:
            best, best_score = d, score
    return best


_POOL = None


def _pool():
    global _POOL
    if _POOL is None:
        import concurrent.futures
        import multiprocessing
        import os
        _POOL = concurrent.futures.ProcessPoolExecutor(
            max_workers=max(1, (os.cpu_count() or 2)), mp_context=multiprocessing.get_context("spawn"))
    return _POOL


async def track_outlines(track: segment.Track, best_frame: int, keyframes: int = 14) -> dict[int, str]:
    """Real SAM outlines on keyframes (decoded in a process pool), refitted to SAM's box on every other frame."""
    frames = sorted(track.detections)
    if not frames:
        return {}
    step = max(1, math.ceil(len(frames) / keyframes))
    keys = sorted(set(frames[::step]) | {best_frame, frames[-1]})
    loop = asyncio.get_running_loop()
    paths = await asyncio.gather(*[loop.run_in_executor(_pool(), segment.outline_job,
                                                         track.detections[f].job_args()) for f in keys])
    decoded = {f: p for f, p in zip(keys, paths) if p}
    if not decoded:
        return {}
    out: dict[int, str] = {}
    dk = sorted(decoded)
    for f in frames:
        if f in decoded:
            out[f] = decoded[f]
            continue
        k = min(dk, key=lambda x: abs(x - f))
        src, dst = track.detections[k], track.detections[f]
        out[f] = segment.refit_path(decoded[k], (src.left, src.top, src.right, src.bottom),
                                    (dst.left, dst.top, dst.right, dst.bottom))
    return out


# ------------------------------------------------------------------ appraisal

async def appraise(item: dict, image_path: Path) -> tuple[dict, dict]:
    label = (item.get("data") or {}).get("visible_label") or ""
    prompt = (
        "Identify this item for a secondhand listing. Use visible details for the best guess at brand and model "
        "(say 'Unbranded' / 'Unknown' if not identifiable). Search the web for current resale prices of comparable "
        "USED items and the new retail price, and cite real listings in comps (real URLs only). Suggest a quick-sale "
        "price for a casual seller who wants to declutter, and a floor price (the lowest they should accept). "
        "Write a warm, honest 2-sentence description. Keep keywords short."
        f" Item hint: {item.get('name')} ({item.get('category')})."
        + (f" The seller wrote this price label next to it: '{label}'." if label else "")
        + brain.playbook_section("pricing") + brain.playbook_section("identification")
    )
    return await llm.muse_json(
        [{"type": "input_text", "text": prompt},
         {"type": "input_image", "image_url": llm.data_url(image_path)}],
        APPRAISE_SCHEMA, name="appraisal", effort="low", web_search=True)


async def inspect(image_paths: list[Path]) -> tuple[dict, dict]:
    parts: list[dict] = [{"type": "text", "text": (
        "You are a meticulous, honest resale inspector. Grade the visible condition of this item for a used "
        "listing and list visible flaws (scuffs, stains, wear, missing parts). Only claim what you can see; if "
        "you cannot tell, say so in evidence. Multiple images are views of the same item."
        + brain.playbook_section("condition"))}]
    for p in image_paths[:4]:
        parts.append({"type": "image", "data": llm.b64(p), "mime_type": "image/jpeg"})
    return await llm.gemini_json(parts, CONDITION_SCHEMA, thinking="low")


def _flatten_on_white(cutout_path: Path, out_path: Path) -> Path:
    im = Image.open(cutout_path).convert("RGBA")
    bg = Image.new("RGB", im.size, (255, 255, 255))
    bg.paste(im, (0, 0), im)
    bg.save(out_path, quality=90)
    return out_path


async def finish_item(scan_id: str, item_id: str, inspect_imgs: list[Path]) -> None:
    item = store.get_item(item_id)
    d = item["data"]
    cutout_path = config.MEDIA_DIR / d["cutout"].removeprefix("/media/")
    item_dir = cutout_path.parent
    white = _flatten_on_white(cutout_path, item_dir / "white.jpg")
    t0 = time.time()

    async def _appraise():
        try:
            return await appraise(item, white)
        except Exception as e:  # noqa: BLE001
            log.exception("appraise failed")
            return None, {"error": str(e)}

    async def _inspect():
        try:
            return await inspect(inspect_imgs or [white])
        except Exception as e:  # noqa: BLE001
            log.exception("inspect failed")
            return None, {"error": str(e)}

    async def _studio():
        try:
            return await studio.studio_photo(cutout_path, item.get("category") or "", item_dir / "studio.jpg")
        except Exception as e:  # noqa: BLE001
            log.exception("studio failed")
            return None

    (ap, ap_meta), (cond, cond_meta), studio_path = await asyncio.gather(_appraise(), _inspect(), _studio())

    fields: dict = {}
    data: dict = {"timings": {"appraise": ap_meta.get("seconds"), "inspect": cond_meta.get("seconds"),
                              "total": round(time.time() - t0, 1)}}
    if ap:
        label_price = _price_from_label(d.get("visible_label", ""))
        market = friendly_price(float(ap.get("suggested_price") or 0))
        price = label_price or market
        floor = float(ap.get("floor_price") or 0)
        if not floor or floor >= price:
            floor = friendly_price(price * 0.75)
        floor = min(floor, price)
        fields.update(title=ap["title"], brand=ap["brand"], category=ap["category"] or item.get("category"),
                      description=ap["description"], price=price, floor_price=floor)
        data.update(model=ap.get("model"), new_retail_price=ap.get("new_retail_price"),
                    used_range=[ap.get("used_low"), ap.get("used_high")], market_price=market,
                    price_source="seller label" if label_price else "market comps",
                    comps=[c for c in ap.get("comps", []) if str(c.get("url", "")).startswith("http")][:4],
                    keywords=ap.get("keywords", []), id_confidence=ap.get("confidence"),
                    citations=ap_meta.get("citations", []), searches=ap_meta.get("searches", 0),
                    original={"title": ap["title"], "brand": ap["brand"], "model": ap.get("model"),
                              "category": ap["category"], "price": price})
    if cond:
        fields["condition"] = cond["condition"]
        data.update(flaws=cond.get("flaws", []), condition_evidence=cond.get("evidence"),
                    condition_confidence=cond.get("confidence"))
        data.setdefault("original", {})["condition"] = cond["condition"]
    if studio_path:
        data["studio"] = media_url(studio_path)
    data["white"] = media_url(white)
    fields["status"] = "listed"
    updated = store.update_item(item_id, data=data, **fields)
    _emit(scan_id, "item_ready", item=updated)


# ------------------------------------------------------------------ orchestration

def scan_dir(scan_id: str) -> Path:
    p = config.MEDIA_DIR / "scans" / scan_id
    p.mkdir(parents=True, exist_ok=True)
    return p


async def run_photo(scan_id: str, src: Path) -> None:
    t_start = time.time()
    sd = scan_dir(scan_id)
    full = media.load_image(src)
    full_path = sd / "full.jpg"
    full.save(full_path, quality=92)
    analysis_path, s = media.save_scaled(full, sd / "analysis.jpg", 1280)
    aw, ah = Image.open(analysis_path).size
    store.update_scan(scan_id, width=aw, height=ah, extra={"analysis": media_url(analysis_path),
                                                           "full": media_url(full_path)})
    _emit(scan_id, "scan_media", mode="photo", url=media_url(analysis_path), width=aw, height=ah)

    _stage(scan_id, "discover", "start", model=config.MUSE_FAST)
    items, meta = await discover_photo(analysis_path)
    _stage(scan_id, "discover", "done", model=config.MUSE_FAST, seconds=meta["seconds"], count=len(items))
    ids = []
    for it in items:
        iid = store.create_item(scan_id, name=it["name"], sam_phrase=it["sam_phrase"], category=it["category"],
                                data={"box": it["box"], "visible_label": it.get("visible_label", ""),
                                      "seen": "photo"})
        ids.append(iid)
    _emit(scan_id, "items_found", items=[{"id": i, **it} for i, it in zip(ids, items)])

    _stage(scan_id, "segment", "start", model=config.SAM)
    t0 = time.time()
    dets = await segment_photo(analysis_path, items)
    _stage(scan_id, "segment", "done", model=config.SAM, seconds=round(time.time() - t0, 1), count=len(dets))

    scale = full.size[0] / aw
    jobs = []
    for idx, (iid, it) in enumerate(zip(ids, items), start=1):
        d = dets.get(idx)
        if d is None:
            store.update_item(iid, status="failed")
            _emit(scan_id, "item_failed", item_id=iid)
            continue
        idir = sd / iid
        idir.mkdir(exist_ok=True)

        def _prep(d=d, idir=idir):
            cut = segment.save_rgba(segment.cutout(full, d, scale=scale), idir / "cutout.png")
            ctx_path = idir / "context.jpg"
            segment.bbox_crop(full, d, scale=scale, margin=0.15).save(ctx_path, quality=90)
            return cut, ctx_path, d.outline()

        cut, ctx_path, outline = await asyncio.to_thread(_prep)
        upd = store.update_item(iid, data={"cutout": media_url(cut), "context": media_url(ctx_path),
                                           "outline": outline, "bbox": [d.left, d.top, d.right, d.bottom],
                                           "sam_confidence": d.confidence, "frame": media_url(analysis_path)},
                                status="appraising")
        _emit(scan_id, "item_segmented", item=upd)
        jobs.append(finish_item(scan_id, iid, [ctx_path]))

    _stage(scan_id, "appraise", "start", models=[config.MUSE_FAST, config.ARGON, config.NANO_BANANA])
    t0 = time.time()
    await asyncio.gather(*jobs)
    _stage(scan_id, "appraise", "done", seconds=round(time.time() - t0, 1))
    _finish(scan_id, t_start)


async def run_video(scan_id: str, src: Path) -> None:
    t_start = time.time()
    sd = scan_dir(scan_id)
    full_path = media.normalize_video(src, sd / "full.mp4", width=1280, max_seconds=20)
    info = media.probe(full_path)
    ana_path = media.normalize_video(full_path, sd / "analysis.mp4", width=config.SAM_VIDEO_WIDTH,
                                     fps=config.SAM_VIDEO_FPS)
    ainfo = media.probe(ana_path)
    aw, ah, fps = ainfo["width"], ainfo["height"], config.SAM_VIDEO_FPS
    n_frames = max(1, int(round(ainfo["duration"] * fps)))
    store.update_scan(scan_id, width=aw, height=ah, duration=info["duration"],
                      extra={"video": media_url(full_path), "analysis": media_url(ana_path), "fps": fps})
    _emit(scan_id, "scan_media", mode="video", url=media_url(full_path), width=aw, height=ah, fps=fps,
          duration=info["duration"])

    _stage(scan_id, "upload", "start")
    file_id = await llm.upload_meta_file(ana_path)
    _stage(scan_id, "upload", "done")

    _stage(scan_id, "discover", "start", model=config.MUSE_FAST)
    items, meta = await discover_video(file_id, ainfo["duration"])
    _stage(scan_id, "discover", "done", model=config.MUSE_FAST, seconds=meta["seconds"], count=len(items))
    ids = []
    for it in items:
        # price labels are unreliable at analysis resolution in video, so video prices come from market comps
        ids.append(store.create_item(scan_id, name=it["name"], sam_phrase=it["sam_phrase"], category=it["category"],
                                     data={"box": it["box"], "visible_label": "", "time_s": it.get("time_s")}))
    _emit(scan_id, "items_found", items=[{"id": i, **it} for i, it in zip(ids, items)])

    _stage(scan_id, "segment", "start", model=config.SAM)
    t0 = time.time()
    tracks = await track_video(file_id, items, aw, ah, fps, n_frames)
    _stage(scan_id, "segment", "done", model=config.SAM, seconds=round(time.time() - t0, 1), count=len(tracks))

    async def prepare(idx: int, iid: str) -> list[Path] | None:
        tr = tracks.get(idx)
        best = best_detection(tr, aw, ah) if tr else None
        if best is None:
            store.update_item(iid, status="failed")
            _emit(scan_id, "item_failed", item_id=iid)
            return None
        idir = sd / iid
        idir.mkdir(exist_ok=True)
        t_best = best.frame / fps
        frame_path = await asyncio.to_thread(media.extract_frame, full_path, t_best, idir / "frame.jpg")
        frame_img = media.load_image(frame_path)
        scale = frame_img.size[0] / aw
        # refine on the full-resolution frame with a box prompt for crisp edges
        det_full = None
        try:
            fw, fh = frame_img.size
            box = msp.PromptBox(int(best.left * scale), int(best.top * scale),
                                int(best.right * scale), int(best.bottom * scale))
            text = msp.build_object_prompt(size=(fw, fh), objects=[
                msp.PromptObject(id=1, prompts=[msp.FramePrompt(frame=0, box=box)])])
            res = await llm.sam_segment(text, image_path=frame_path)
            dl = segment.detections_from_result(res)
            det_full = dl[0] if dl and dl[0].mask is not None else None
        except Exception as e:  # noqa: BLE001
            log.warning("full-res refine failed: %s", e)
        src_det, sc = (det_full, 1.0) if det_full is not None else (best, scale)
        cut_img = await asyncio.to_thread(segment.cutout, frame_img, src_det, sc)
        ctx = segment.bbox_crop(frame_img, src_det, scale=sc, margin=0.15)
        cut = segment.save_rgba(cut_img, idir / "cutout.png")
        ctx_path = idir / "context.jpg"
        ctx.save(ctx_path, quality=90)
        # a few other views of the same item for the inspector
        views = [ctx_path]
        frames_sorted = sorted(tr.detections)
        for f in (frames_sorted[len(frames_sorted) // 5], frames_sorted[-len(frames_sorted) // 5]):
            if f == best.frame:
                continue
            try:
                vp = await asyncio.to_thread(media.extract_frame, full_path, f / fps, idir / f"view_{f}.jpg")
                vimg = media.load_image(vp)
                segment.bbox_crop(vimg, tr.detections[f], scale=vimg.size[0] / aw, margin=0.15).save(vp, quality=88)
                views.append(vp)
            except Exception:  # noqa: BLE001
                pass
        track_paths = await track_outlines(tr, best.frame)
        upd = store.update_item(iid, data={
            "cutout": media_url(cut), "context": media_url(ctx_path), "frame": media_url(frame_path),
            "outline": track_paths.get(best.frame, ""), "bbox": [best.left, best.top, best.right, best.bottom],
            "best_time": round(t_best, 2), "seen": f"{t_best:.1f}s", "track": track_paths,
            "sam_confidence": best.confidence}, status="appraising")
        _emit(scan_id, "item_segmented", item=upd)
        await finish_item(scan_id, iid, views)
        return views

    _stage(scan_id, "appraise", "start", models=[config.MUSE_FAST, config.ARGON, config.NANO_BANANA])
    t0 = time.time()
    await asyncio.gather(*[prepare(idx, iid) for idx, iid in enumerate(ids, start=1)])
    _stage(scan_id, "appraise", "done", seconds=round(time.time() - t0, 1))
    _finish(scan_id, t_start)


def _finish(scan_id: str, t_start: float) -> None:
    items = [i for i in store.items_for_scan(scan_id) if i["status"] != "failed"]
    total = sum(float(i.get("price") or 0) for i in items)
    secs = round(time.time() - t_start, 1)
    store.update_scan(scan_id, status="done")
    _emit(scan_id, "scan_done", count=len(items), total=total, seconds=secs)
    store.log_activity("scan_done", scan_id=scan_id, count=len(items), total=total, seconds=secs)


async def run_scan(scan_id: str, src: Path, mode: str) -> None:
    try:
        if mode == "video":
            await run_video(scan_id, src)
        else:
            await run_photo(scan_id, src)
    except Exception as e:  # noqa: BLE001
        log.exception("scan %s failed", scan_id)
        store.update_scan(scan_id, status="failed")
        _emit(scan_id, "scan_failed", error=str(e))
