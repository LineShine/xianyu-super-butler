"""Run one ASGI service with graceful, per-generation Windows event shutdown."""
from __future__ import annotations

import argparse
import asyncio
import importlib
import os
from pathlib import Path
import sys

from windows_process import Event


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--service", choices=("backend-web", "websocket", "scheduler"), required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path[:0] = [str(root / args.service), str(root)]
    # main.py preserves the existing service bootstrap and business lifespan.
    module = importlib.import_module("main")
    bootstrap = sys.modules["_bootstrap"]
    import uvicorn
    config = uvicorn.Config(module.app, host="127.0.0.1", port=bootstrap.settings.service_port,
                            log_level=bootstrap.settings.log_level.lower(), log_config=None,
                            loop="asyncio", timeout_graceful_shutdown=60)
    server = uvicorn.Server(config)

    async def serve():
        with Event(os.environ["YTX_STOP_EVENT"], existing=True) as stop:
            async def watch():
                while not stop.wait():
                    await asyncio.sleep(.2)
                server.should_exit = True
            watcher = asyncio.create_task(watch())
            try:
                await server.serve()
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
    # Default Proactor loop supports Playwright subprocesses on Windows.
    asyncio.run(serve())


if __name__ == "__main__":
    main()
