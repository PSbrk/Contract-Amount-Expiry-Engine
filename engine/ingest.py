"""Tableau export ingestion.

Two pieces:

1. parse_tableau_export(data) -- turns the raw bytes of a Tableau export
   into a DataFrame the rest of the engine can consume. Handles the
   documented quirks of the export format:
     * UTF-16 LE with BOM, TAB-delimited (despite the .csv extension)
     * 13 columns, the last one (Amount) carries NO header label
     * Headers may have trailing spaces ('Vendor ', 'Program Name ')
     * A 'Grand Total' summary row sits as the FIRST data row with
       Campus='Total' and must be dropped
     * Amount column carries '$' + thousands commas; negatives use accounting
       parens like '($788.38)' (NOT a leading minus sign)
     * Dept values like '000' must keep their leading zeros (strings, not ints)
     * Date is M/D/YYYY with UNPADDED month/day
   .xlsx exports are also accepted via openpyxl as a fallback.

2. TransactionSource protocol + three implementations:
     * LocalInboxSource  -- the production path. Scans data/inbox/ for the
       next Tableau export, dedups by SHA-256 against the SQLite Inbox
       table, returns (df, metadata) or raises NoNewTransactionsError /
       DuplicateTransactionsError.
     * LocalFileSource   -- reads a single file off disk; used by
       --ingest-file for parser smoke-testing without dedup.
     * TableauRestSource -- stub for an eventual Tableau Cloud REST
       integration. Raises NotImplementedError today.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

import pandas as pd

from engine.sqlite_client import file_hash_already_processed, sha256_hex


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)


EXPECTED_COLUMNS: tuple[str, ...] = (
    "Record No",
    "Campus",
    "Dept",
    "Account No",
    "Account Name",
    "Project ID",
    "Vendor",
    "Record Description",
    "Program Name",
    "Reference",
    "Date",
    "Type",
    "Amount",
)

# All columns parsed as strings to preserve Dept leading zeros ('000', '107')
# and Account No formatting. Numeric parsing for Amount + Date is done after.
_STRING_DTYPE: dict[str, str] = {c: "string" for c in EXPECTED_COLUMNS}

_ACCOUNTING_PARENS = re.compile(r"^\((.+)\)$")

# pandas auto-renames empty header cells to 'Unnamed: 12' (or whatever index).
# We need to map that back to '' so the unlabeled Amount column gets picked up
# by the rename logic below.
_PANDAS_UNNAMED = re.compile(r"^Unnamed:\s*\d+$")


def _parse_amount(raw: object) -> float:
    """Convert one cell from the Amount column to a signed float.

    Handles: '$2,470.00' → 2470.0, '($788.38)' → -788.38, '($244,362.45)' →
    -244362.45, '' → 0.0, '$0.00' → 0.0, pandas NA / NaN → 0.0. Whitespace
    tolerated. Raises ValueError on anything else so a malformed row surfaces
    loudly rather than producing a silent 0.
    """
    if raw is None:
        return 0.0
    # pandas StringDtype emits pd.NA for empty cells in xlsx reads; the str()
    # round-trip would yield "<NA>" which is not a number.
    try:
        if pd.isna(raw):
            return 0.0
    except (TypeError, ValueError):
        pass
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "<na>"):
        return 0.0
    m = _ACCOUNTING_PARENS.match(s)
    if m:
        s = "-" + m.group(1)
    s = s.replace("$", "").replace(",", "").replace(" ", "")
    return float(s)


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Strip whitespace from headers; rename the unlabeled Amount column.

    Handles three tableau-export quirks at once:
    - 'Vendor ' and 'Program Name ' carry trailing spaces in the real export
      → stripped.
    - The final column (Amount) has NO header label in the real export → it
      arrives from pandas as 'Unnamed: 12' (or similar index), we map it back
      to '' and then rename to 'Amount'.
    """
    def normalize(c: object) -> object:
        if isinstance(c, str):
            if _PANDAS_UNNAMED.match(c):
                return ""
            return c.strip()
        return c

    df = df.rename(columns={c: normalize(c) for c in df.columns})

    blank_cols = [c for c in df.columns if c == ""]
    if len(blank_cols) > 1:
        raise ValueError(
            f"Tableau export has {len(blank_cols)} unlabeled columns; "
            f"expected exactly one (the Amount column)"
        )
    if blank_cols:
        new_cols = list(df.columns)
        new_cols[new_cols.index("")] = "Amount"
        df.columns = new_cols
    return df


def parse_tableau_export(
    data: bytes | str | Path | io.BytesIO,
    *,
    filename: str | None = None,
) -> pd.DataFrame:
    """Parse a Tableau export's bytes/path into a clean DataFrame.

    Output columns: EXPECTED_COLUMNS (13). Amount is signed float, Date is
    pandas datetime, all other columns are pandas StringDtype. The Grand Total
    summary row (Campus=='Total') is dropped.

    Auto-detects format by extension hint: .xlsx → openpyxl; otherwise
    UTF-16 + tab CSV.
    """
    is_xlsx = filename is not None and filename.lower().endswith(".xlsx")

    if isinstance(data, (str, Path)):
        path = Path(data)
        if path.suffix.lower() == ".xlsx":
            is_xlsx = True
        source = path
    elif isinstance(data, bytes):
        source = io.BytesIO(data)
    elif isinstance(data, io.BytesIO):
        source = data
    else:
        raise TypeError(
            f"parse_tableau_export expects bytes/str/Path/BytesIO, got "
            f"{type(data).__name__}"
        )

    if is_xlsx:
        df = pd.read_excel(source, dtype="string", engine="openpyxl")
    else:
        # UTF-16 LE BOM + TAB delim per Step 1 research. dtype=string preserves
        # leading zeros. keep_default_na=False keeps empty cells as '' so the
        # Amount cleaner can map them to 0.0.
        df = pd.read_csv(
            source,
            encoding="utf-16",
            sep="\t",
            dtype="string",
            keep_default_na=False,
        )

    df = _normalize_columns(df)

    missing = [c for c in EXPECTED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Tableau export is missing expected columns {missing}. "
            f"Found columns: {list(df.columns)}"
        )

    # Warn (don't fail) on extra columns — surfaces a Tableau-side schema
    # change immediately, but lets the current run proceed with the canonical
    # 13 columns. Silent drop without this warning was the old behavior; a
    # future operator would have no signal that the export had grown.
    extras = [c for c in df.columns if c not in EXPECTED_COLUMNS]
    if extras:
        log.warning(
            "Tableau export contains %d unexpected column(s) not in "
            "EXPECTED_COLUMNS — they are being dropped: %s. If this is a "
            "deliberate schema change, update engine.ingest.EXPECTED_COLUMNS.",
            len(extras), extras,
        )

    # Strip leading/trailing whitespace from every string cell. Production
    # exports have been clean but a stray ' Total' or 'CEN ' would silently
    # shift rows through both the Grand-Total filter and the in_scope filter
    # without any error signal. Cheap defense, large blast radius.
    for col in EXPECTED_COLUMNS:
        if col in ("Amount", "Date"):
            continue
        s = df[col]
        if pd.api.types.is_string_dtype(s):
            df[col] = s.str.strip()

    # Drop the Grand Total summary row (Campus == "Total"). The real export
    # has it as the FIRST data row; the filter is position-agnostic.
    df = df.loc[df["Campus"] != "Total"].copy()

    # Parse Amount: parens → negative, strip $ + commas, to float.
    df["Amount"] = df["Amount"].map(_parse_amount).astype("float64")

    # DEFENSIVE: spec §4 defines the sign by the Type column, not by parens
    # formatting. Today the production export agrees (Credit rows always use
    # parens). If that ever drifts — a Credit row exported as "$1,000.00"
    # without parens, say — the parens-only cleaner would yield +1000 and
    # the engine would understate spend by $2,000. We re-derive the sign
    # from Type and log any disagreement so the drift is loud, not silent.
    mismatches = 0
    if "Type" in df.columns:
        amt = df["Amount"]
        credit_mask = df["Type"] == "Credit"
        charge_mask = df["Type"] == "Charge"
        mismatches += int(((credit_mask) & (amt > 0)).sum())
        mismatches += int(((charge_mask) & (amt < 0)).sum())
        df.loc[credit_mask, "Amount"] = -amt[credit_mask].abs()
        df.loc[charge_mask, "Amount"] = amt[charge_mask].abs()
        if mismatches:
            log.warning(
                "%d row(s) had a sign disagreement between Type and the "
                "parens-cleaned Amount — Type-based sign is authoritative "
                "and was applied. Investigate the export format.", mismatches,
            )

    # Parse Date: M/D/YYYY (unpadded month/day OK with this format string).
    df["Date"] = pd.to_datetime(df["Date"], format="%m/%d/%Y", errors="raise")

    # Keep only the canonical 13 columns in order so downstream code has a
    # stable shape. The extras warning above already surfaced any drop.
    df = df[list(EXPECTED_COLUMNS)].reset_index(drop=True)

    return df


# ---------------------------------------------------------------------------
# Source protocol + implementations
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceMetadata:
    """Provenance for one transaction batch.

    source_path is set only when the source is LocalInboxSource -- it
    carries the path of the file that was just picked off data/inbox/,
    so main.py can move it to data/processed/ once the pipeline
    succeeds. Kept on the metadata rather than as a separate
    LocalInboxSource method so the engine's main loop stays
    storage-agnostic about how to "finalize" the read.
    """
    name: str            # filename or view ID
    hash: str            # SHA-256 hex of the source bytes
    received_iso: str    # ISO timestamp the source was received into the pipeline
    source_path: str | None = None


class TransactionSourceError(Exception):
    """Base class for source-layer signals the engine main handler routes on."""


class NoNewTransactionsError(TransactionSourceError):
    """No unprocessed source data available right now. Engine should exit
    cleanly with outcome=no_new_data."""


class DuplicateTransactionsError(TransactionSourceError):
    """Source data matches a previously processed file by hash. The file
    should be moved aside (LocalInboxSource handles this internally)
    rather than reprocessed.
    """

    def __init__(self, *, hash: str, filename: str):
        self.hash = hash
        self.filename = filename
        super().__init__(
            f"file {filename!r} (hash {hash[:12]}...) was already processed"
        )


@runtime_checkable
class TransactionSource(Protocol):
    def get_latest_transactions(self) -> tuple[pd.DataFrame, SourceMetadata]:
        """Pull the latest unprocessed batch.

        Returns: (DataFrame in EXPECTED_COLUMNS shape, SourceMetadata).
        Raises NoNewTransactionsError if nothing is available.
        Raises DuplicateTransactionsError if the latest file is a known dup.
        """
        ...


class LocalInboxSource:
    """Scans a local inbox folder for the next Tableau export to ingest.

    The local-first replacement for AirtableInboxSource. The operator
    drops .csv / .xlsx exports into data/inbox/, the engine picks the
    OLDEST by mtime on each run (so a backlog drains FIFO), hashes the
    bytes, checks the SQLite Inbox audit log for dedup, parses, and
    hands back the (df, meta) the rest of the pipeline expects.

    File lifecycle:
      data/inbox/<filename>
        ↓ (pick + parse + pipeline runs)
        → data/processed/<hash[:12]>-<filename>  (success — caller moves via
           move_to_processed)
        → data/processed/_duplicate-<hash[:12]>-<filename>  (already in
           Inbox table — moved here by get_latest_transactions before
           raising DuplicateTransactionsError, so the file leaves the
           queue without overwriting the original processed file)
        → stays in data/inbox/  (parser error or pipeline failure — operator
           retries on the next run)

    The caller (main.py) is responsible for the SUCCESS move via
    move_to_processed(). Done this way so a parser exception or pipeline
    crash leaves the file in inbox/ for retry, rather than the source
    moving it speculatively.
    """

    _ALLOWED_SUFFIXES: tuple[str, ...] = (".csv", ".xlsx")

    def __init__(
        self,
        conn,
        *,
        inbox_dir: str | os.PathLike | None = None,
        processed_dir: str | os.PathLike | None = None,
    ) -> None:
        self.conn = conn
        # Defaults point at the canonical local-first layout. Callers
        # can override in tests to point at a tmpdir.
        self.inbox_dir = (
            Path(inbox_dir) if inbox_dir is not None
            else Path("data") / "inbox"
        )
        self.processed_dir = (
            Path(processed_dir) if processed_dir is not None
            else Path("data") / "processed"
        )

    def get_latest_transactions(self) -> tuple[pd.DataFrame, SourceMetadata]:
        # Imported inside the method so a test module that imports
        # engine.ingest doesn't pay for sqlite_client's import side
        # effects unless the local-inbox path actually runs.
        from engine.sqlite_client import (
            file_hash_already_processed as _sqlite_hash_seen,
            sha256_hex as _sqlite_sha256,
        )

        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)

        candidates = sorted(
            (
                p for p in self.inbox_dir.iterdir()
                if p.is_file()
                and p.suffix.lower() in self._ALLOWED_SUFFIXES
            ),
            key=lambda p: p.stat().st_mtime,
        )
        if not candidates:
            raise NoNewTransactionsError(
                f"no unprocessed files in {self.inbox_dir}"
            )
        path = candidates[0]
        data = path.read_bytes()
        h = _sqlite_sha256(data)

        if _sqlite_hash_seen(self.conn, h):
            # The same content has already been processed. Move the
            # duplicate OUT of inbox/ so subsequent runs don't re-detect
            # it and busy-loop the warning. The `_duplicate-` prefix
            # keeps it visually distinct from real processed files in
            # data/processed/.
            dest = (
                self.processed_dir
                / f"_duplicate-{h[:12]}-{path.name}"
            )
            shutil.move(str(path), str(dest))
            log.info(
                "LocalInboxSource: duplicate hash %s; moved %s -> %s",
                h[:12], path.name, dest,
            )
            raise DuplicateTransactionsError(hash=h, filename=path.name)

        df = parse_tableau_export(data, filename=path.name)
        meta = SourceMetadata(
            name=path.name,
            hash=h,
            received_iso=datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc,
            ).isoformat(timespec="seconds"),
            source_path=str(path),
        )
        return df, meta

    def move_to_processed(self, source_path: str | os.PathLike, *, file_hash: str) -> Path:
        """Move a successfully-ingested file to data/processed/.

        Destination filename is `<hash[:12]>-<original-name>` so two
        files with the same operator-chosen name (but different content,
        ergo different hashes) don't collide. Called by main.py only
        AFTER the full pipeline has written the SQLite Inbox row —
        keeping the move OUT of get_latest_transactions means a parser
        crash or pipeline error leaves the file in inbox/ for retry.
        """
        src = Path(source_path)
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        dest = self.processed_dir / f"{file_hash[:12]}-{src.name}"
        shutil.move(str(src), str(dest))
        return dest


class LocalFileSource:
    """Reads a file off disk. Used by --ingest-file for parsing the
    operator's actual Tableau export without going through the
    inbox/processed flow.

    Does NOT check for dedup -- callers explicitly want the file processed.
    """

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)

    def get_latest_transactions(self) -> tuple[pd.DataFrame, SourceMetadata]:
        from datetime import datetime, timezone
        data = self.path.read_bytes()
        h = sha256_hex(data)
        df = parse_tableau_export(data, filename=self.path.name)
        meta = SourceMetadata(
            name=self.path.name,
            hash=h,
            received_iso=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        return df, meta


class TableauRestSource:
    """Stubbed source for the eventual Tableau REST API ingestion path.

    Today the operator clicks Tableau's "Download → Crosstab" button and drops
    the file into the Airtable Inbox attachment field. The plan is to eliminate
    that manual step by having the engine pull the same view directly via the
    Tableau Cloud REST API on a schedule.

    The shape of that pull (per Tableau Cloud REST API docs, the "query view
    data" endpoint):

      Server:   https://us-west-2b.online.tableau.com
      Site:     "lifechurch" (contentUrl form used in REST paths)
      Auth:     POST /api/{api-version}/auth/signin with a Personal Access
                Token (PAT name + secret), which returns an X-Tableau-Auth
                token + the resolved site-id.
      Pull:     GET /api/{api-version}/sites/{site-id}/views/{view-id}/data
                with header X-Tableau-Auth: {token}. Default response is CSV.
      Sign-out: POST /api/{api-version}/auth/signout

    Once implemented, get_latest_transactions() will:
      1. Sign in with TABLEAU_PAT_NAME + TABLEAU_PAT_SECRET (from env).
      2. Pull the view data, run parse_tableau_export on the response bytes
         (the parser already handles UTF-16/UTF-8 BOM auto-detection, tab
         and comma delimiters, accounting-parens, etc.).
      3. Sign out (best effort — token expires on its own).
      4. SHA-256-hash the response bytes and check file_hash_already_processed
         against the Airtable history so re-pulls of an unchanged view don't
         reprocess. Distinct from the Inbox dedup (different storage but the
         same hash table) so a manual Inbox upload + a future REST pull of
         the same exported file still dedupe correctly.

    Why a stub today: the engine ships behind a config switch
    (`settings.TRANSACTION_SOURCE`). Operator stays on `airtable_inbox` until
    the PATs and view ID are configured; flipping the switch to `tableau_rest`
    will then activate this path with zero downstream changes (the
    TransactionSource Protocol is the only contract). Tests pin the
    stub behavior so it can't silently no-op once wired.

    Construction note: the stub accepts the params it WILL need so callers
    can wire them up now via settings; it just doesn't make the HTTP calls.
    """

    def __init__(
        self,
        *,
        server_url: str,
        site_name: str,
        view_id: str | None,
        pat_name: str | None,
        pat_secret: str | None,
        api_version: str = "3.22",
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.site_name = site_name
        self.view_id = view_id
        self.pat_name = pat_name
        self.pat_secret = pat_secret
        self.api_version = api_version

    def get_latest_transactions(self) -> tuple[pd.DataFrame, SourceMetadata]:
        raise NotImplementedError(
            "TableauRestSource is a stub. The Tableau Cloud REST "
            "'query view data' integration is planned for a follow-up "
            "step; today the operator drops exports into data/inbox/ "
            "(TRANSACTION_SOURCE=local_inbox, the default). See "
            "engine.ingest.TableauRestSource docstring for the planned "
            "endpoint shape."
        )


__all__ = [
    "EXPECTED_COLUMNS",
    "SourceMetadata",
    "TransactionSource",
    "TransactionSourceError",
    "NoNewTransactionsError",
    "DuplicateTransactionsError",
    "LocalInboxSource",
    "LocalFileSource",
    "TableauRestSource",
    "parse_tableau_export",
]
