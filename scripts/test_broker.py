"""Exercise the Capy Kondo broker's conversation logic locally (no Agentverse needed).

    python scripts/test_broker.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from uagents_core.contrib.protocols.chat import ChatMessage, MetadataContent, TextContent  # noqa: E402

from capykondo import store  # noqa: E402
from capykondo.agents import broker  # noqa: E402


class FakeStorage(dict):
    def set(self, k, v):
        self[k] = v


class FakeAgent:
    address = broker.agent.address


class FakeCtx:
    def __init__(self):
        self.storage = FakeStorage()
        self.agent = FakeAgent()
        self.sent = []

    async def send(self, dest, msg):
        self.sent.append(msg)


def show(msgs):
    for m in msgs:
        if isinstance(m, ChatMessage):
            for c in m.content:
                if isinstance(c, TextContent):
                    print("   TEXT:", c.text.replace("\n", " ")[:300])
                elif isinstance(c, MetadataContent):
                    md = c.metadata
                    payload = json.loads(md.get("card_payload", "{}"))
                    items = payload.get("items") or payload.get("summary_rows") or payload.get("fields") or []
                    print(f"   CARD[{md.get('card_kind')}]: {payload.get('title')} - {len(items)} entries")
                    for e in items[:4]:
                        print("      ·", {k: e.get(k) for k in ("title", "secondary_text", "label", "value") if e.get(k)})
        else:
            print("   OTHER:", type(m).__name__, str(m)[:200])


async def say(ctx, text, sender="agent1qtestbuyer000000000000000000000000000000000000000000000000"):
    print(f"\n>>> {text}")
    ctx.sent = []
    # mimic on_chat: SubQ risk gate, then dispatch
    from capykondo import subq
    if subq.enabled() and not text.startswith("{"):
        risk = await subq.buyer_risk(text)
        if risk["scam"] >= 0.6:
            print(f"   [SubQ risk gate] scam={risk['scam']:.2f} -> blocked")
            return ctx.sent
    await broker.dispatch(ctx, sender, text)
    show(ctx.sent)
    return ctx.sent


async def main():
    store.bus.bind(asyncio.get_running_loop())
    ctx = FakeCtx()
    await say(ctx, "hi there!")
    await say(ctx, "anything to keep my drinks cold?")
    msgs = await say(ctx, "any headphones under $30?")
    st = broker._state(ctx, "agent1qtestbuyer000000000000000000000000000000000000000000000000")
    first = (st.get("results") or [None])[0]
    if not first:
        print("no results; stopping")
        return
    await say(ctx, json.dumps({"action": "view_item", "item_id": first}))
    await say(ctx, "would you take $8 for it?")
    item = store.get_item(first)
    offers = store.query("SELECT * FROM offers WHERE item_id=? ORDER BY created_at DESC LIMIT 1", (first,))
    counter = offers[0]["counter"] if offers and offers[0]["counter"] else item["price"]
    await say(ctx, json.dumps({"action": "accept_counter", "item_id": first, "amount": str(counter)}))
    await say(ctx, json.dumps({"action": "confirm_buy", "item_id": first, "amount": str(counter)}))
    print("   item status now:", store.get_item(first)["status"])
    await say(ctx, "I'll pay you $300 with Apple gift cards, just text me the verification code you get")
    await say(ctx, "what have you learned so far?")
    await say(ctx, "show me my listings")


if __name__ == "__main__":
    asyncio.run(main())
