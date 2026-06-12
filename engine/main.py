"""Engine entry point. Stub at Step 0 — fleshed out as each build step lands."""

from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Contract Amount Expiry Engine")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Override DRY_RUN_ASANA to true (no Asana writes). Default during build.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger(__name__)

    log.info("engine starting (dry_run=%s)", args.dry_run)
    log.info("scaffold only — Step 0. See tasks for build progress.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
