from __future__ import annotations

import logging
import os
import signal
import time
import uuid

from .composition import create_worker
from .config import Settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("researchops.worker")
    settings = Settings()
    worker_id = f"worker-{uuid.uuid4().hex[:12]}"
    worker = create_worker(settings, worker_id)
    stopping = False

    def stop(*_) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logger.info("worker_started pid=%s", os.getpid())
    while not stopping:
        try:
            reconciled = worker.reconcile_one()
            job = worker.process_one()
        except Exception:
            logger.error("worker_iteration_failed error_code=worker_runtime_error")
            time.sleep(min(5.0, settings.worker_poll_seconds * 2))
            continue
        if job is None and reconciled is None:
            time.sleep(settings.worker_poll_seconds)
    logger.info("worker_stopped")


if __name__ == "__main__":
    main()
