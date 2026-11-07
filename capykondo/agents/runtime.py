"""Start the Capy Kondo + Capy Appraiser uAgents inside the web server's event loop."""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

import httpx

from .. import config, store

log = logging.getLogger("capy.agents")
INFO: dict = {}


def _inspector(port: int, address: str) -> str:
    return f"https://agentverse.ai/inspect/?uri={quote(f'http://127.0.0.1:{port}')}&address={address}"


async def _auto_connect(port: int, name: str) -> None:
    """Register the agent's mailbox with Agentverse headlessly when AGENTVERSE_API_KEY is set."""
    if not config.AGENTVERSE_API_KEY:
        return
    await asyncio.sleep(4)
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"http://127.0.0.1:{port}/connect",
                             json={"user_token": config.AGENTVERSE_API_KEY, "agent_type": "mailbox"})
            ok = r.status_code == 200 and r.json().get("success", False)
            log.info("Agentverse connect %s: %s %s", name, r.status_code, r.text[:200])
            INFO.setdefault(name, {})["agentverse_connected"] = bool(ok)
    except Exception as e:  # noqa: BLE001
        log.warning("Agentverse auto-connect failed for %s: %s", name, e)


async def run_agents() -> None:
    from . import appraiser, broker

    broker.APPRAISER_ADDRESS["address"] = appraiser.agent.address
    for name, mod, port in (("capy-kondo", broker, config.BROKER_PORT),
                            ("capy-appraiser", appraiser, config.APPRAISER_PORT)):
        INFO[name] = {"address": mod.agent.address, "port": port,
                      "inspector": _inspector(port, mod.agent.address),
                      "profile": f"https://agentverse.ai/agents/details/{mod.agent.address}/profile"}
        log.info("%s address %s", name, mod.agent.address)
        log.info("%s inspector %s", name, INFO[name]["inspector"])
    store.log_activity("agents_online", agents={k: v["address"] for k, v in INFO.items()})
    asyncio.create_task(_auto_connect(config.BROKER_PORT, "capy-kondo"))
    asyncio.create_task(_auto_connect(config.APPRAISER_PORT, "capy-appraiser"))
    await asyncio.gather(broker.agent.run_async(), appraiser.agent.run_async())
