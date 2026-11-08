"""Run Capy Kondo: the local demo web app + both Fetch.ai agents, in one process.

    uv run python -m capykondo              # web app on :8787 + agents
    uv run python -m capykondo --no-agents  # web app only
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


def ensure_utf8_mode() -> None:
    """Windows defaults to cp1252, but our READMEs and data are UTF-8 (uagents reads readme_path without an
    encoding). Re-run this command in Python's UTF-8 mode and wait for it, so Ctrl+C still stops everything."""
    import os
    import subprocess
    import sys

    if os.name != "nt" or sys.flags.utf8_mode:
        return
    child = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "capykondo", *sys.argv[1:]])
    while True:
        try:
            sys.exit(child.wait())
        except KeyboardInterrupt:   # the child gets the same Ctrl+C; wait for it to shut down
            continue


if __name__ == "__main__":
    ensure_utf8_mode()
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-agents", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "openai", "google_genai", "google.genai", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    asyncio.run(main(not args.no_agents))
