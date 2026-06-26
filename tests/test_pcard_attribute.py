"""Option A: attribute blank-vendor P-Card spend to a contract by description.

The operator clicks "Attribute to X" on the P-Card tab → a blank-vendor,
pattern-bearing Learned Mapping is stored. On the next ingest the engine
STAMPS X's vendor name onto matching blank-vendor rows BEFORE attribution, so
they split into their own clean vendor group and attribute normally; the
unrelated blank-vendor noise (Amazon, Lowes) stays on the P-Card tab.

Layers:
  1. load_pcard_links — reads only blank-vendor + pattern LMs (the links).
  2. stamp_pcard_links — pure: stamps Vendor on matching blank rows only.
  3. stamp → attribute — the stamped row attributes to the contract; siblings
     stay unmatched (NOT dragged to ambiguous).
  4. UI — "Attribute to X" writes the link (opex only; CapEx refused); unlink
     removes it.
"""
import sqlite3

import pandas as pd
import pytest

from engine import campus_map
from engine.asana_contracts import Contract
from engine.attribution import attribute, stamp_pcard_links
from engine.name_match import distinctive_tokens, name_in_description, match_unlinked
from engine.sqlite_client import (
    ensure_schema,
    load_pcard_links,
    upsert_needs_tagging_group,
)
from engine.ui import create_app


# --------------------------------------------------------------------------
# helpers / fixtures
# --------------------------------------------------------------------------

def _lm_row(conn, *, key, campus, dept, acct, vendor, name, gid, pattern):
    conn.execute(
        '''INSERT INTO "Learned Mappings"
             ("Key","Campus","Dept","Account No","Vendor","Contract Name",
              "Contract Gid","Description Pattern","Learned At","Notes")
           VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (key, campus, dept, acct, vendor, name, gid, pattern, "2026-06-26", ""),
    )
    conn.commit()


def _contract(name, *, gid, acc="63040", capex_id=None):
    return Contract(
        gid=gid, name=name, campus_options=frozenset({"HNV"}),
        contract_amount=10000.0, target_start=None, due_on=None,
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant", dept="000", acc=acc, capex_id=capex_id,
    )


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    yield c
    c.close()


@pytest.fixture
def client(conn):
    app = create_app(conn=conn)
    app.testing = True
    return app.test_client()


# --------------------------------------------------------------------------
# 1. load_pcard_links — only blank-vendor + pattern rows are links
# --------------------------------------------------------------------------

def test_load_pcard_links_filters(conn):
    _lm_row(conn, key="HNV|000|63040|", campus="HNV", dept="000", acct="63040",
            vendor="", name="Empire Today LLC", gid="g1", pattern="empire today")
    _lm_row(conn, key="HNV|000|63080|", campus="HNV", dept="000", acct="63080",
            vendor="", name="No Pattern", gid="g2", pattern="")          # skipped
    _lm_row(conn, key="HNV|000|63090|Acme", campus="HNV", dept="000", acct="63090",
            vendor="Acme", name="Acme Inc", gid="g3", pattern="")        # vendor LM

    links = load_pcard_links(conn)
    assert len(links) == 1
    assert links[0] == {"campus": "HNV", "dept": "000", "account_no": "63040",
                        "gid": "g1", "name": "Empire Today LLC", "pattern": "empire today"}


# --------------------------------------------------------------------------
# 2. stamp_pcard_links — pure, blank-vendor + pattern-match only
# --------------------------------------------------------------------------

def _df():
    return pd.DataFrame({
        "Record No": ["R1", "R2", "R3"],
        "Campus": ["HNV", "HNV", "OKC"],
        "Dept": ["000", "000", "000"],
        "Account No": ["63040", "63040", "63040"],
        "Vendor": ["", "", ""],
        "Record Description": [
            "Deposit HNV office carpet, EMPIRE TODAY HS",   # match
            "Amazon supplies misc",                          # blank, no match
            "EMPIRE TODAY other campus",                     # right desc, wrong campus
        ],
        "Amount": [4278.26, 50.0, 99.0],
        "Date": [pd.Timestamp("2026-06-01")] * 3,
    })


def test_stamp_only_matching_blank_rows():
    df = _df()
    links = [{"campus": "HNV", "dept": "000", "account_no": "63040",
              "name": "Empire Today LLC", "pattern": "empire today"}]
    n = stamp_pcard_links(df, links)
    assert n == 1
    assert df.loc[0, "Vendor"] == "Empire Today LLC"     # matched
    assert df.loc[1, "Vendor"] == ""                     # blank, no desc match
    assert df.loc[2, "Vendor"] == ""                     # wrong campus, untouched


def test_stamp_never_overwrites_a_real_vendor():
    df = _df()
    df.loc[0, "Vendor"] = "SOME REAL VENDOR"
    links = [{"campus": "HNV", "dept": "000", "account_no": "63040",
              "name": "Empire Today LLC", "pattern": "empire today"}]
    assert stamp_pcard_links(df, links) == 0
    assert df.loc[0, "Vendor"] == "SOME REAL VENDOR"


# --------------------------------------------------------------------------
# 3. stamp → attribute: the matched row attributes; siblings stay unmatched
# --------------------------------------------------------------------------

def test_stamped_row_attributes_and_siblings_stay_unmatched():
    df = _df().iloc[:2].copy()        # Empire match + Amazon noise, same group
    links = [{"campus": "HNV", "dept": "000", "account_no": "63040",
              "name": "Empire Today LLC", "pattern": "empire today"}]
    stamp_pcard_links(df, links)
    contracts = [_contract("Empire Today LLC", gid="g1")]

    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})

    attributed = run.auto + run.learned
    assert len(attributed) == 1
    assert attributed[0].contract_gid == "g1"
    # The Amazon row stayed a clean blank-vendor unmatched group (NOT ambiguous).
    assert any(r.status == "unmatched" for r in run.unmatched)


def test_gid_pin_disambiguates_same_name_after_stamp():
    """Two contracts share a name (e.g. an active + an archived 'Summit Fire').
    Stamping the name alone would go ambiguous; main.py also injects a
    gid-pinned LM on the stamped key so it resolves to the operator's pick."""
    df = _df().iloc[:1].copy()        # the row whose desc names "empire today"
    c_pick = _contract("Acme Co", gid="g_pick")
    c_other = _contract("Acme Co", gid="g_other")
    stamp_pcard_links(df, [{"campus": "HNV", "dept": "000", "account_no": "63040",
                            "name": "Acme Co", "pattern": "empire today"}])
    # mirror the main.py wiring: a gid-pinned plain LM on the stamped key
    learned = {("HNV", "000", "63040", "Acme Co"): [("Acme Co", "g_pick", None)]}

    run = attribute(df, [c_pick, c_other], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings=learned)
    attributed = run.auto + run.learned
    assert len(attributed) == 1 and attributed[0].contract_gid == "g_pick"


# --------------------------------------------------------------------------
# 4. UI: attribute (opex), reject CapEx, unlink
# --------------------------------------------------------------------------

def _seed_pcard_row(conn):
    rec = upsert_needs_tagging_group(
        conn, group_key="HNV|000|63040|", campus="HNV", dept="000",
        account_no="63040", vendor="",
        sample_description="Deposit HNV carpet, EMPIRE TODAY HS",
        amount=4278.26, candidate_names=[], created_at_iso_date="2026-06-26",
    )
    return rec["id"]


def _patch_asana(monkeypatch, contracts):
    from engine import asana_client, asana_contracts
    monkeypatch.setattr(asana_client, "get_api_client", lambda *a, **k: None)
    monkeypatch.setattr(asana_contracts, "load_open_contracts", lambda *a, **k: contracts)


def test_attribute_route_writes_link(client, conn, monkeypatch):
    rid = _seed_pcard_row(conn)
    _patch_asana(monkeypatch, [_contract("Empire Today LLC", gid="g1")])

    resp = client.post(f"/p-card-spend/{rid}/attribute", data={"gid": "g1"})
    assert resp.status_code == 302

    links = load_pcard_links(conn)
    assert len(links) == 1
    assert links[0]["gid"] == "g1" and links[0]["pattern"]      # non-empty stem
    assert links[0]["campus"] == "HNV" and links[0]["account_no"] == "63040"


def test_attribute_route_refuses_capex_target(client, conn, monkeypatch):
    rid = _seed_pcard_row(conn)
    _patch_asana(monkeypatch, [_contract("Empire Today LLC", gid="g1", acc="63015")])

    client.post(f"/p-card-spend/{rid}/attribute", data={"gid": "g1"},
                follow_redirects=True)
    assert load_pcard_links(conn) == []          # CapEx target → no link written


def test_pcard_tag_stripped_so_schendel_matches():
    """The '(pcard)' operator tag must not become a required match token."""
    toks = distinctive_tokens("Schendel Pest Services (pcard)")
    assert toks == {"schendel", "pest"}          # not {'pcard', 'pest', 'schendel'}
    assert name_in_description(
        toks, "Campus Pest Control-02/2025, WWP*SCHENDEL PEST, Lunsford, 02/14/2025")


def test_match_unlinked_surfaces_cross_campus():
    """A '(pcard)' contract filed under NKC must still match a WWK charge — as
    a cross-campus hit (the link's gid-pin attributes regardless of campus)."""
    pool = [("Schendel Pest Services (pcard)", "gS", {"NKC"})]
    confident, cross = match_unlinked(
        ["WWP*SCHENDEL PEST campus pest control"], {"WWK"}, pool)
    assert confident == []
    assert cross == [("Schendel Pest Services (pcard)", "gS")]


def test_pcard_page_renders_cross_campus_attribute_button(client, conn, monkeypatch):
    upsert_needs_tagging_group(
        conn, group_key="WWK|000|63080|", campus="WWK", dept="000",
        account_no="63080", vendor="",
        sample_description="Campus Pest Control, WWP*SCHENDEL PEST, Lunsford, John, 02/14/2025",
        amount=763.10, candidate_names=[], created_at_iso_date="2026-06-26",
        distinct_descriptions=[("Campus Pest Control, WWP*SCHENDEL PEST, Lunsford, John, 02/14/2025", 1, 763.10, "", "")],
    )
    # _contract campus is HNV (≠ WWK) → cross-campus; opex acct → linkable.
    _patch_asana(monkeypatch, [_contract("Schendel Pest Services (pcard)", gid="gS")])
    body = client.get("/p-card-spend").get_data(as_text=True)
    assert "Attribute to Schendel Pest Services (pcard)" in body
    assert "different campus" in body


def test_open_tab_hides_net_zero_groups(client, conn, monkeypatch):
    _patch_asana(monkeypatch, [])
    upsert_needs_tagging_group(             # fully-reversed → net $0
        conn, group_key="ZZZ|000|63080|", campus="ZZZ", dept="000",
        account_no="63080", vendor="",
        sample_description="Reversed thing here, AMAZON, Doe, John, 01/01/2025",
        amount=0.0, candidate_names=[], created_at_iso_date="2026-06-26")
    upsert_needs_tagging_group(             # real net spend
        conn, group_key="YYY|000|63080|", campus="YYY", dept="000",
        account_no="63080", vendor="",
        sample_description="Real net spend here, AMAZON, Doe, John, 01/01/2025",
        amount=500.0, candidate_names=[], created_at_iso_date="2026-06-26")

    open_body = client.get("/p-card-spend").get_data(as_text=True)
    assert "Real net spend here" in open_body
    assert "Reversed thing here" not in open_body      # net-$0 hidden from Open
    assert "fully-reversed" in open_body               # transparency note

    all_body = client.get("/p-card-spend?show=all").get_data(as_text=True)
    assert "Reversed thing here" in all_body           # still auditable under All


def test_link_by_description_links_blank_vendor_line_item(client, conn):
    """The P-card line-item picker links a blank-vendor description to a
    SPECIFIC contract (gid), even when the auto-matcher can't (Gallivan)."""
    conn.execute(
        'INSERT INTO "Dashboard" ("Contract","Asana Task GID","Campus Set",'
        '"Contract Reason Text") VALUES (?,?,?,?)',
        ("Gallivan Corporation dba Applied Mulch Soil", "gGAL", "ALB", "Snow Removal"))
    conn.commit()
    rid = upsert_needs_tagging_group(
        conn, group_key="ALB|000|63080|", campus="ALB", dept="000",
        account_no="63080", vendor="", sample_description="Gallivan Snow Contract",
        amount=11737.50, candidate_names=[], created_at_iso_date="2026-06-26",
        distinct_descriptions=[("Gallivan Snow Contract", 1, 11737.50, "", "")])["id"]

    label = "Gallivan Corporation dba Applied Mulch Soil — ALB — Snow Removal"
    resp = client.post(f"/p-card-spend/{rid}/link-by-description",
                       data={"description": "Gallivan Snow Contract", "contract": label})
    assert resp.status_code == 302

    links = load_pcard_links(conn)
    assert len(links) == 1
    assert links[0]["gid"] == "gGAL"          # resolved to the SPECIFIC contract
    assert "gallivan" in links[0]["pattern"]  # pattern = the line-item stem


def test_unlink_route_removes_link(client, conn, monkeypatch):
    rid = _seed_pcard_row(conn)
    _patch_asana(monkeypatch, [_contract("Empire Today LLC", gid="g1")])
    client.post(f"/p-card-spend/{rid}/attribute", data={"gid": "g1"})
    lm_id = conn.execute('SELECT id FROM "Learned Mappings"').fetchone()["id"]

    client.post(f"/p-card-spend/unlink/{lm_id}", follow_redirects=True)
    assert load_pcard_links(conn) == []
