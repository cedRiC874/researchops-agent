"""Explicit implementation-v5/source-v4 campaign entry; no legacy auto-upgrade."""
from .campaign_run import main_v5 as main, run_timed_campaign_v5


if __name__ == '__main__':
    raise SystemExit(main())
