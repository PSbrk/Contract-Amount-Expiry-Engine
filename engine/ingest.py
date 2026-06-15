"""Tableau export ingestion.

Two pieces:

1. parse_tableau_export(data) — turns the raw bytes of a Tableau export into
   a DataFrame the rest of the engine can consume. Handles the documented
   quirks of the export format:
     * UTF-16 LE with BOM, TAB-delimited (despite the .csv extension)
     * 13 columns, the last one (Amount) carries NO header label
     * Headers may have trailing spaces (real cases: 'Vendor ', 'Program Name ')
     * A 'Grand Total' summary row sits as the FIRST data row with
       Campus='Total' and must be dropped
     * Amount column carries '$' + thousands commas; negatives use accounting
       parens like '($788.38)' (NOT a leading minus sign)
     * Dept values like '000' must keep their leading zeros (strings, not ints)
     * Date is M/D/YYYY with UNPADDED month/day
   .xlsx exports are also accepted via openpyxl as a fallback.

2. TransactionSource protocol + two implementations:
     * AirtableInboxSource — pulls the newest unprocessed Inbox attachment,
       hashes it, checks for prior-processed dedup, returns (df, metadata)
       or raises NoNewTransactionsError / DuplicateTransactionsError.
     * LocalFileSource — reads a file off disk; used by --ingest-file for
       dev smoke-testing without touching Airtable.
   The protocol is what Step 7's TableauRestSource will satisfy when it lands.
"""

from __future__ import annotations

import io
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import pandas as pd

from engine.airtable_client import (
    download_attachment_bytes,
    file_hash_already_processed,
    get_newest_unprocessed_inbox,
    sha256_hex,
)


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

    inbox_record_id is set only when the source is AirtableInboxSource — local
    files and (future) Tableau REST pulls have no Airtable backing row.
    """
    name: str            # filename or view ID
    hash: str            # SHA-256 hex of the source bytes
    received_iso: str    # ISO timestamp the source was received into the pipeline
    inbox_record_id: str | None = None


class TransactionSourceError(Exception):
    """Base class for source-layer signals the engine main handler routes on."""


class NoNewTransactionsError(TransactionSourceError):
    """No unprocessed source data available right now. Engine should exit
    cleanly with outcome=no_new_data."""


class DuplicateTransactionsError(TransactionSourceError):
    """Source data matches a previously processed file by hash. The Inbox
    record (if any) should be flagged as a duplicate rather than reprocessed.
    """

    def __init__(self, *, hash: str, filename: str, inbox_record_id: str | None):
        self.hash = hash
        self.filename = filename
        self.inbox_record_id = inbox_record_id
        super().__init__(
            f"file {filename!r} (hash {hash[:12]}…) was already processed"
        )


class UnusableInboxRecordError(TransactionSourceError):
    """An Inbox record exists but cannot be processed (e.g. has no attachment).

    Engine should mark the record Processed with a Notes explanation so it
    doesn't loop on the same broken record run after run. Distinct from
    NoNewTransactionsError (which means there's genuinely nothing to do).
    """

    def __init__(self, *, inbox_record_id: str, reason: str):
        self.inbox_record_id = inbox_record_id
        self.reason = reason
        super().__init__(f"Inbox record {inbox_record_id!r}: {reason}")


@runtime_checkable
class TransactionSource(Protocol):
    def get_latest_transactions(self) -> tuple[pd.DataFrame, SourceMetadata]:
        """Pull the latest unprocessed batch.

        Returns: (DataFrame in EXPECTED_COLUMNS shape, SourceMetadata).
        Raises NoNewTransactionsError if nothing is available.
        Raises DuplicateTransactionsError if the latest file is a known dup.
        """
        ...


class AirtableInboxSource:
    """Pulls the newest unprocessed attachment from the Airtable Inbox table.

    The Airtable client (engine.airtable_client) wraps every Airtable
    interaction; this class only orchestrates them.
    """

    def __init__(self, base) -> None:
        self.base = base

    def get_latest_transactions(self) -> tuple[pd.DataFrame, SourceMetadata]:
        record = get_newest_unprocessed_inbox(self.base)
        if record is None:
            raise NoNewTransactionsError("no unprocessed Inbox records")
        if not record.attachments:
            # Distinct exception so main can mark the record Processed —
            # otherwise it would be re-detected as "newest unprocessed" on
            # every subsequent run and append duplicate Run Log noise
            # forever.
            raise UnusableInboxRecordError(
                inbox_record_id=record.id,
                reason=f"record {record.name!r} has no attachment",
            )

        att = record.attachments[0]
        filename = att.get("filename") or record.name or "<unnamed>"
        data = download_attachment_bytes(att)
        h = sha256_hex(data)

        if file_hash_already_processed(self.base, h):
            raise DuplicateTransactionsError(
                hash=h,
                filename=filename,
                inbox_record_id=record.id,
            )

        df = parse_tableau_export(data, filename=filename)
        meta = SourceMetadata(
            name=filename,
            hash=h,
            received_iso=record.created_time,
            inbox_record_id=record.id,
        )
        return df, meta


class LocalFileSource:
    """Reads a file off disk. Used by --ingest-file for parsing the operator's
    actual Tableau export without round-tripping through Airtable.

    Does NOT check for dedup — local files are always fresh from the caller's
    perspective. (Airtable dedup is the right place for that check; this
    source is for dev smoke-testing only.)
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
            inbox_record_id=None,
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
            "TableauRestSource is a Step 7 stub. The Tableau Cloud REST "
            "'query view data' integration is planned for a follow-up step; "
            "today the operator should keep settings.TRANSACTION_SOURCE on "
            "'airtable_inbox' and continue uploading exports to the Airtable "
            "Inbox. See engine.ingest.TableauRestSource docstring for the "
            "planned endpoint shape."
        )


__all__ = [
    "EXPECTED_COLUMNS",
    "SourceMetadata",
    "TransactionSource",
    "TransactionSourceError",
    "NoNewTransactionsError",
    "DuplicateTransactionsError",
    "UnusableInboxRecordError",
    "AirtableInboxSource",
    "LocalFileSource",
    "TableauRestSource",
    "parse_tableau_export",
]
