"""SND-vendorless drop rule. Run: python -m pytest engine/test_snd_filter.py
or just: python engine/test_snd_filter.py"""
import pandas as pd
from engine.filters import snd_no_vendor_mask


def test_snd_no_vendor_mask():
    df = pd.DataFrame({
        "Vendor":    ["",            "",                     "Acme",         "",       ""],
        "Reference": ["SND 080426",  "Reversed - SND 080426","SND whatever", "snd 99", "no tag"],
    })
    got = list(snd_no_vendor_mask(df))
    # drop: blank vendor + case-sensitive SND present.  keep: has vendor,
    # lowercase 'snd', or no SND at all.
    assert got == [True, True, False, False, False], got


if __name__ == "__main__":
    test_snd_no_vendor_mask()
    print("ok")
