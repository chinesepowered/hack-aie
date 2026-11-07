"""FastAPI app: local demo UI + REST + server-sent events. Fetch.ai agents run in the same process."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import brain, commerce, config, payments, pipeline, store

log = logging.getLogger("capy.server")
app = FastAPI(title="Capy Kondo")
app.mount("/media", StaticFiles(directory=config.MEDIA_DIR), name="media")
app.mount("/static", StaticFiles(directory=config.WEB_DIR / "static"), name="static")

VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v", ".mkv"}
AGENT_INFO: dict = {}


@app.on_event("startup")
async def _startup() -> None:
    store.bus.bind(asyncio.get_running_loop())
    brain.ensure_repo()


def _item_view(it: dict, with_track: bool = False) -> dict:
    it = dict(it)
    d = dict(it.get("data") or {})
    if not with_track:
        d.pop("track", None)
    it["data"] = d
    return it


@app.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return FileResponse(config.WEB_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "payments": payments.mode(), "public_url": config.PUBLIC_BASE_URL or None}


@app.post("/api/scan")
async def scan(file: UploadFile = File(...), mode: str | None = Form(None), seller: str = Form("you")) -> dict:
    suffix = Path(file.filename or "upload.jpg").suffix.lower() or ".jpg"
    if file.content_type and file.content_type.startswith("video/") and suffix not in VIDEO_EXT:
        suffix = ".webm" if "webm" in file.content_type else ".mp4"
    mode = mode or ("video" if suffix in VIDEO_EXT else "photo")
    up_dir = config.MEDIA_DIR / "uploads"
    up_dir.mkdir(parents=True, exist_ok=True)
    dst = up_dir / f"{int(time.time() * 1000)}{suffix}"
    with dst.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    sid = store.create_scan(mode, str(dst), seller=seller)
    store.bus.publish("scan_started", scan_id=sid, mode=mode)
    asyncio.create_task(pipeline.run_scan(sid, dst, mode))
    return {"scan_id": sid, "mode": mode}


@app.get("/api/scans")
async def scans() -> list[dict]:
    return store.list_scans()


@app.get("/api/scans/{sid}")
async def scan_detail(sid: str) -> dict:
    s = store.get_scan(sid)
    if not s:
        raise HTTPException(404)
    return {"scan": s, "items": [_item_view(i, with_track=True) for i in store.items_for_scan(sid)]}


@app.get("/api/items")
async def items() -> list[dict]:
    return [_item_view(i) for i in store.list_items()]


class Correction(BaseModel):
    field: str
    value: str
    note: str = ""
    learn: bool = True


@app.post("/api/items/{iid}/correct")
async def correct(iid: str, body: Correction) -> dict:
    try:
        cid = await brain.record_correction(iid, body.field, body.value, body.note)
    except KeyError:
        raise HTTPException(404)
    if body.learn and body.field in brain.FIELD_TASK:
        asyncio.create_task(brain.learn(cid))
    return {"correction_id": cid}


class StatusBody(BaseModel):
    status: str


@app.post("/api/items/{iid}/status")
async def set_status(iid: str, body: StatusBody) -> dict:
    if body.status not in ("listed", "draft", "removed"):
        raise HTTPException(400)
    it = store.update_item(iid, status=body.status)
    store.bus.publish("item_ready", item=_item_view(it))
    return _item_view(it)


class OfferBody(BaseModel):
    amount: float
    buyer: str = "web-buyer"


@app.post("/api/items/{iid}/offer")
async def offer(iid: str, body: OfferBody) -> dict:
    return await commerce.offer(iid, body.buyer, body.amount)


class BuyBody(BaseModel):
    buyer: str = "web-buyer"
    amount: float | None = None


@app.post("/api/items/{iid}/buy")
async def buy(iid: str, body: BuyBody) -> dict:
    try:
        order = commerce.create_order(iid, body.buyer, body.amount)
    except ValueError:
        raise HTTPException(409, "not available")
    if order["checkout"]["mode"] == "demo":
        commerce.complete_order(order["order_id"], "demo")
    return order


@app.get("/api/brain")
async def brain_summary() -> dict:
    return brain.summary()


@app.get("/api/activity")
async def activity() -> list[dict]:
    return store.recent_activity()


@app.get("/api/stats")
async def stats() -> dict:
    return commerce.stats()


@app.get("/api/agents")
async def agents() -> dict:
    return {**AGENT_INFO, "payments": payments.mode(), "public_url": config.PUBLIC_BASE_URL or None}


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    q = store.bus.subscribe()

    async def gen():
        try:
            yield "retry: 2000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(evt, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            store.bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


_PAGE = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Capy Kondo</title><style>body{{font-family:system-ui;background:#f6f1e9;color:#3b2f25;display:grid;place-items:center;
min-height:100vh;margin:0}}.c{{background:#fff;border-radius:20px;padding:32px;max-width:420px;text-align:center;
box-shadow:0 10px 40px #0001}}h1{{margin:.2em 0}}code{{background:#f2ebe0;padding:4px 8px;border-radius:8px}}</style></head>
<body><div class=c><div style="font-size:56px">{emoji}</div><h1>{title}</h1><p>{body}</p></div></body></html>"""


@app.get("/pay/done", response_class=HTMLResponse)
async def pay_done(order: str, session_id: str | None = None) -> HTMLResponse:
    o = commerce.get_order(order)
    if not o:
        return HTMLResponse(_PAGE.format(emoji="🤔", title="Order not found", body="Please return to the chat."))
    if o["status"] != "paid" and commerce.check_paid(o, session_id):
        o = commerce.complete_order(order, session_id)
    if o["status"] == "paid":
        return HTMLResponse(_PAGE.format(emoji="🎉", title="Paid - thank you!",
                                         body=f"Your pickup code is <code>{o['pickup_code']}</code>. "
                                              "You can return to ASI:One; Capy will confirm there too."))
    return HTMLResponse(_PAGE.format(emoji="⏳", title="Payment pending",
                                     body="Stripe has not confirmed the payment yet. Return to the chat and try again."))


@app.get("/pay/cancel", response_class=HTMLResponse)
async def pay_cancel(order: str) -> HTMLResponse:
    commerce.cancel_order(order)
    return HTMLResponse(_PAGE.format(emoji="🍊", title="Checkout cancelled",
                                     body="No charge was made. The item is back on the shelf."))
