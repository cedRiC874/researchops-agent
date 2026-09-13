"""Explicit v5 CLI; no automatic upgrade of the legacy invocation entrypoint."""
from .first_live_run import main_v5 as main, run_timed_first_live_v5


if __name__ == '__main__':
    raise SystemExit(main())
