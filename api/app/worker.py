"""Sender worker entrypoint: `python -m app.worker`."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from app.db import dispose_engine, ping
from app.logging import configure_logging
from app.services.sender import SenderService

logger = logging.getLogger("app.worker")


async def wait_for_database(*, attempts: int = 30, delay: float = 2.0) -> None:
    for attempt in range(1, attempts + 1):
        try:
            await ping()
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Database not ready (attempt %d/%d): %s", attempt, attempts, exc)
            await asyncio.sleep(delay)
    raise SystemExit("Database is unreachable; refusing to start the worker")


def _install_stop_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _stop(*_: object) -> None:
        logger.info("Shutdown signal received; finishing the in-flight message")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except (NotImplementedError, RuntimeError):  # Windows
            signal.signal(sig, _stop)


async def main() -> int:
    configure_logging()
    await wait_for_database()
    stop = asyncio.Event()
    _install_stop_handlers(stop)

    service = SenderService()
    try:
        await service.run_forever(stop)
    finally:
        await dispose_engine()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
