"""Helpers shared by the Capy Kondo uAgents: chat messages, ASI:One interactive cards, seeds."""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from uuid import uuid4

from uagents_core.contrib.protocols.chat import ChatMessage, EndSessionContent, MetadataContent, TextContent

from .. import config

SEEDS_FILE = config.DATA_DIR / "agent_seeds.json"


def seed_for(name: str, configured: str) -> str:
    """Use the configured seed, else a random one persisted locally (stable agent address per machine)."""
    if configured:
        return configured
    seeds = json.loads(SEEDS_FILE.read_text()) if SEEDS_FILE.exists() else {}
    if name not in seeds:
        seeds[name] = f"capy-kondo-{name}-{secrets.token_hex(16)}"
        SEEDS_FILE.write_text(json.dumps(seeds, indent=2))
    return seeds[name]


def now() -> datetime:
    return datetime.now(timezone.utc)


def text_msg(text: str, end: bool = False) -> ChatMessage:
    content: list = [TextContent(type="text", text=text)]
    if end:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(timestamp=now(), msg_id=uuid4(), content=content)


def card_msg(text: str, kind: str, payload: dict, terminal: bool = False, width: int | None = None) -> ChatMessage:
    md = {
        "card_protocol_version": "1",
        "requires_card_interaction": "false" if terminal else "true",
        "card_kind": kind,
        "card_payload": json.dumps(payload),
    }
    if terminal:
        md["is_terminal"] = "true"
    if width:
        md["preferred_drawer_width_px"] = str(width)
    return ChatMessage(timestamp=now(), msg_id=uuid4(),
                       content=[TextContent(type="text", text=text), MetadataContent(type="metadata", metadata=md)])


def parse_selection(text: str) -> dict | None:
    """Card selections arrive as JSON (direct @mention) or prose mentioning our ids (planner-mediated)."""
    t = (text or "").strip()
    if t.startswith("{"):
        try:
            d = json.loads(t)
            if isinstance(d, dict):
                return d
        except json.JSONDecodeError:
            pass
    m = re.search(r"\baction\W+([a-z_]+)", t)
    ids = re.findall(r"\b(item_[0-9a-f]{10}|order_[0-9a-f]{10}|scan_[0-9a-f]{10})\b", t)
    if m and ids:
        sel: dict = {"action": m.group(1)}
        for i in ids:
            sel[i.split("_")[0] + "_id"] = i
        amt = re.search(r"\$?\b(\d+(?:\.\d{1,2})?)\b", t.replace(ids[0], ""))
        if amt:
            sel.setdefault("amount", amt.group(1))
        return sel
    return None


def money(v: float | None) -> str:
    if v is None:
        return "-"
    return f"${v:,.0f}" if float(v).is_integer() else f"${v:,.2f}"


def image_url(public_item: dict) -> str | None:
    return public_item.get("image_url")
