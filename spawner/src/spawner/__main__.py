"""Spawner entry point (ECA-65 AC#6).

``spawner`` console script: configure logging, load ``Settings`` from env/``.env``, run the
``SpawnerApp`` lifecycle, and shut down cleanly on SIGINT/SIGTERM. The app itself owns boot
reconciliation-before-pull (AC#4); this module is only the process wrapper.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from .app import SpawnerApp
from .config import Settings

logger = logging.getLogger("spawner")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def _run() -> None:
    settings = Settings()
    app = SpawnerApp(settings)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(app.stop()))
        except (NotImplementedError, RuntimeError):
            # Signal handlers are unavailable on some platforms / non-main threads; the app
            # still exits on KeyboardInterrupt via the outer handler.
            pass

    await app.run()


def main() -> None:
    _configure_logging()
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("interrupted; exiting")


if __name__ == "__main__":
    main()
