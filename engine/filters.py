"""Filter enforcement — applies ACCOUNTS_IN_SCOPE and DEPTS_IN_SCOPE.

Spec §4 is explicit that the engine MUST enforce these filters and not trust
the Tableau export to have done it. A pre-filtered export is fine but not
required; this module is the single source of truth for in-scope-ness.

A pre-filtered export still works: the intersection of an already-filtered
set with the same filter is a no-op.
"""

from __future__ import annotations

import pandas as pd

from config import settings


def is_in_scope_mask(df: pd.DataFrame) -> pd.Series:
    """Boolean mask: True for rows that pass BOTH Account No AND Dept filters."""
    return (
        df["Account No"].isin(settings.ACCOUNTS_IN_SCOPE)
        & df["Dept"].isin(settings.DEPTS_IN_SCOPE)
    )


def in_scope(df: pd.DataFrame) -> pd.DataFrame:
    """Rows that pass both filters. Defensive .copy() so downstream mutations
    do not trigger a SettingWithCopyWarning."""
    return df.loc[is_in_scope_mask(df)].copy()


def out_of_scope(df: pd.DataFrame) -> pd.DataFrame:
    """Rows that do NOT pass both filters. Surfaced for Run Log totals so the
    operator can sanity-check that the filter didn't drop something
    unexpected (e.g. an unfamiliar new Account No)."""
    return df.loc[~is_in_scope_mask(df)].copy()


def signed_sum(df: pd.DataFrame) -> float:
    """Net signed sum of the Amount column. Credit rows are already negative
    after parse_tableau_export's parens cleanup, so this is just a sum."""
    return float(df["Amount"].sum())


__all__ = [
    "is_in_scope_mask",
    "in_scope",
    "out_of_scope",
    "signed_sum",
]
