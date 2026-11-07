"""Capy Appraiser - a specialist uAgent that turns a photo/video of clutter into priced, real-pixel listings.

Other agents (e.g. Capy Kondo) send it an AppraiseRequest; people can also chat with it directly on ASI:One.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import httpx
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec

from .. import commerce, config, pipeline, store
from .common import card_msg, money, now, seed_for, text_msg
from .protocols import AppraiseRequest, AppraiseResult, make_appraisal_protocol

log = logging.getLogger("capy.appraiser")
README = config.ROOT / "capykondo" / "agents" / "README_appraiser.md"
MAX_BYTES = 40 * 1024 * 1024

agent = Agent(
    name="capy-appraiser",
    seed=seed_for("appraiser", config.APPRAISER_SEED),
    port=config.APPRAISER_PORT,
    mailbox=True,
    readme_path=str(README),
    description=("Capy Appraiser: send a photo or short video of things you want to sell. It finds every item "
                 "(Muse Spark), segments it with SAM 3.1, grades condition (Gemini 4 Argon) and prices it from live "
                 "listings - with real-pixel evidence."),
    publish_agent_details=True,
    handle_messages_concurrently=True,
)
chat = Protocol(spec=chat_protocol_spec)
appraisal_protocol = make_appraisal_protocol("appraiser")


async def download(url: str) -> tuple[Path, str]:
    up = config.MEDIA_DIR / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as c:
        r = await c.get(url)
        r.raise_for_status()
        if len(r.content) > MAX_BYTES:
            raise ValueError("file too large")
        ctype = r.headers.get("content-type", "")
    is_video = ctype.startswith("video/") or re.search(r"\.(mp4|mov|webm|m4v)(\?|$)", url, re.I)
    suffix = ".mp4" if is_video else ".jpg"
    if "webm" in ctype:
        suffix = ".webm"
    dst = up / f"{int(time.time() * 1000)}{suffix}"
    dst.write_bytes(r.content)
    return dst, "video" if is_video else "photo"


async def appraise_url(url: str, seller: str) -> AppraiseResult:
    t0 = time.time()
    try:
        path, mode = await download(url)
    except Exception as e:  # noqa: BLE001
        return AppraiseResult(session="", ok=False, error=f"could not download the media ({e})")
    sid = store.create_scan(mode, str(path), seller=seller)
    store.bus.publish("scan_started", scan_id=sid, mode=mode, via="capy-appraiser")
    await pipeline.run_scan(sid, path, mode)
    scan = store.get_scan(sid)
    items = [i for i in store.items_for_scan(sid) if i["status"] in ("listed", "reserved")]
    if not scan or scan["status"] != "done" or not items:
        return AppraiseResult(session="", ok=False, scan_id=sid, error="no sellable items found")
    total = sum(float(i.get("price") or 0) for i in items)
    return AppraiseResult(session="", ok=True, scan_id=sid, count=len(items), total=total,
                          seconds=round(time.time() - t0, 1))


@appraisal_protocol.on_message(AppraiseRequest)
async def on_request(ctx: Context, sender: str, msg: AppraiseRequest):
    store.log_activity("a2a", frm="capy-kondo", to="capy-appraiser", what="AppraiseRequest")
    res = await appraise_url(msg.media_url, msg.seller)
    res.session = msg.session
    await ctx.send(sender, res)
    store.log_activity("a2a", frm="capy-appraiser", to="capy-kondo",
                       what=f"AppraiseResult ({res.count} items, {money(res.total)})")


@chat.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=now(), acknowledged_msg_id=msg.msg_id))
    text = " ".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()
    if not text:
        return
    m = re.search(r"https?://\S+", text)
    if not m:
        await ctx.send(sender, text_msg(
            "Hi, I'm Capy Appraiser 🍊 Send me a link to a photo or short video of things you want to sell and "
            "I'll find every item, check its condition and price it from live listings. "
            f"You can also snap one at {config.PUBLIC_BASE_URL or f'http://localhost:{config.PORT}'}."))
        return
    await ctx.send(sender, text_msg("Looking closely... finding items with Muse, cutting them out with SAM 3.1, "
                                    "inspecting with Gemini 4 Argon, and checking live prices. ~40s 🔎"))
    res = await appraise_url(m.group(0).rstrip(").,"), sender)
    if not res.ok:
        await ctx.send(sender, text_msg(f"I couldn't appraise that: {res.error}."))
        return
    items = [commerce.public_item(i) for i in store.items_for_scan(res.scan_id) if i["status"] == "listed"]
    lines = "\n".join(f"- **{p['title']}**: {money(p['price'])} ({p.get('condition')})" for p in items)
    cards = []
    for p in items[:10]:
        e = {"id": p["id"], "title": p["title"], "subtitle": (p.get("condition") or "").title(),
             "secondary_text": money(p["price"]),
             "badges": [{"label": "Real pixels", "variant": "info"}],
             "primary_cta": {"label": "Details", "selection": {"action": "view_item", "item_id": p["id"]}}}
        if p.get("image_url"):
            e["image"] = p["image_url"]
        cards.append(e)
    await ctx.send(sender, card_msg(
        f"Appraised **{res.count} items** worth about **{money(res.total)}** in {res.seconds:.0f}s:\n\n{lines}\n\n"
        "They're now listed with **Capy Kondo**, who will sell them for you.", "carousel",
        {"title": "Appraisal", "subtitle": f"{res.count} items · {money(res.total)}", "items": cards}))


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


agent.include(chat, publish_manifest=True)
agent.include(appraisal_protocol, publish_manifest=False)
