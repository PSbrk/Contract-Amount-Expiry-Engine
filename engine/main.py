"""Engine entry point. Step 1 wires --audit; ingest/compute/write modes land
in later steps. The Step 5 writer will re-add a runtime --dry-run flag.
"""

from __future__ import annotations

import argparse
import logging
import sys


def _configure_logging() -> None:
    """Sets a safe default. Critically, caps the urllib3 + asana loggers at
    INFO so a future DEBUG toggle on the engine logger cannot cascade into the
    SDK and dump the Bearer PAT (which urllib3 logs in full request headers
    at DEBUG) into stdout / CI logs.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.INFO)
    logging.getLogger("asana").setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Contract Amount Expiry Engine")
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Read-only verify that Asana's project schema matches the engine's "
             "expectations (GIDs, options, sections). Exits 0 on pass, 1 on fail.",
    )
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    _configure_logging()
    log = logging.getLogger(__name__)

    if args.audit:
        from engine.audit import main as audit_main
        return audit_main([])

    log.info("Step 1 only — run `python -m engine.main --audit` to verify the "
             "Asana schema. Ingest / compute / write modes land in build steps 2–5.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
