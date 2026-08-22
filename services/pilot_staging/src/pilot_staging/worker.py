from __future__ import annotations

import logging
import os
import signal
import time
import uuid

from .composition import create_worker
from .config import Settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("researchops.pilot.worker")
    settings = Settings()
    worker_id = "pilot-worker-" + uuid.uuid4().hex[:12]
    worker, store = create_worker(settings, worker_id)
    stopping = False

    def stop(*_) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logger.info("pilot_worker_started pid=%s", os.getpid())
    try:
        while not stopping:
            try:
                attempt = worker.process_one()
            except Exception:
                logger.error("pilot_worker_iteration_failed error_code=worker_runtime_error")
                time.sleep(min(5.0, settings.worker_poll_seconds * 2))
                continue
            if attempt is None:
                time.sleep(settings.worker_poll_seconds)
    finally:
        store.close()
    logger.info("pilot_worker_stopped")


if __name__ == "__main__":
    main()
