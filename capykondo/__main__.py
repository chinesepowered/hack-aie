"""Run Capy Kondo: the local demo web app + both Fetch.ai agents, in one process.

    python -m capykondo              # web app on :8787 + agents
    python -m capykondo --no-agents  # web app only
"""
from __future__ import annotations

import argparse
import asyncio
import logging

import uvicorn

from . import config


async def main(with_agents: bool) -> None:
    from .server import AGENT_INFO, app

    server = uvicorn.Server(uvicorn.Config(app, host=config.HOST, port=config.PORT, log_level="warning"))
    tasks = [server.serve()]
    if with_agents:
        from .agents import runtime

        async def agents():
            try:
                await runtime.run_agents()
            except Exception:  # noqa: BLE001
                logging.getLogger("capy").exception("agents stopped")

        AGENT_INFO.update(runtime.INFO)
        tasks.append(agents())

        async def sync_info():
            for _ in range(30):
                await asyncio.sleep(1)
                AGENT_INFO.update(runtime.INFO)
        tasks.append(sync_info())
    print(f"\n  Capy Kondo is running:  http://localhost:{config.PORT}\n")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-agents", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "openai", "google_genai", "google.genai", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    asyncio.run(main(not args.no_agents))
