"""Tests for the calibration base-rate store and probability blender."""
from __future__ import annotations

from pathlib import Path

from pm.calibration.base_rates import BaseRate, first_match, load
from pm.calibration.model import blend
from pm.calibration.sources import ExternalProb


def test_base_rate_regex_match():
    r = BaseRate(name="r1", category="finance",
                 question_pattern="Fed (holds|keeps).*rate",
                 p=0.6)
    assert r.matches("Will the Fed holds rates in March?", "finance")
    assert not r.matches("Bitcoin above $100k?", "crypto")


def test_base_rate_category_filter():
    r = BaseRate(name="r1", category="finance",
                 question_pattern="rate",
                 p=0.5)
    assert r.matches("Fed cuts rate", "finance")
    assert not r.matches("Fed cuts rate", "politics")


def test_first_match_returns_first():
    rates = [
        BaseRate(name="generic", category=None,
                 question_pattern="rate", p=0.5),
        BaseRate(name="specific", category="finance",
                 question_pattern="Fed.*rate", p=0.6),
    ]
    m = first_match(rates, "Will the Fed cut the rate?", "finance")
    assert m is not None and m.name == "generic"  # generic listed first


def test_load_existing_yaml(tmp_path: Path):
    p = tmp_path / "rates.yaml"
    p.write_text(
        "base_rates:\n"
        "  - name: test_rate\n"
        "    category: politics\n"
        "    question_pattern: 'incumbent.*wins'\n"
        "    p: 0.65\n"
        "    n_samples: 18\n"
        "    source: 'test'\n",
        encoding="utf-8")
    rates = load(p)
    assert len(rates) == 1
    assert rates[0].p == 0.65
    assert rates[0].matches("Will the incumbent wins reelection?", "politics")


def test_load_missing_file_returns_empty(tmp_path: Path):
    assert load(tmp_path / "does_not_exist.yaml") == []


def test_load_skips_malformed_entries(tmp_path: Path):
    p = tmp_path / "rates.yaml"
    p.write_text(
        "base_rates:\n"
        "  - name: good\n"
        "    question_pattern: 'a'\n"
        "    p: 0.5\n"
        "  - name: bad_no_pattern\n"
        "    p: 0.5\n", encoding="utf-8")
    rates = load(p)
    assert len(rates) == 1
    assert rates[0].name == "good"


def test_blend_internal_only():
    r = BaseRate(name="x", category=None, question_pattern="a",
                 p=0.30, n_samples=60)
    m = blend(r, [])
    assert m is not None
    assert abs(m.p - 0.30) < 1e-3
    assert m.sources[0].startswith("internal:")


def test_blend_pure_external():
    ext = ExternalProb(source="metaculus", p=0.70, weight=1.0, as_of=0.0)
    m = blend(None, [ext])
    assert m is not None
    assert abs(m.p - 0.70) < 1e-3


def test_blend_combined_logit_average():
    r = BaseRate(name="x", category=None, question_pattern="a",
                 p=0.30, n_samples=60)  # weight 1.0
    ext = ExternalProb(source="metaculus", p=0.70, weight=1.0, as_of=0.0)
    m = blend(r, [ext])
    assert m is not None
    # Logit-average of equal weights at 0.3 and 0.7 -> sigmoid(0) = 0.5
    assert abs(m.p - 0.50) < 1e-3


def test_blend_returns_none_with_no_inputs():
    assert blend(None, []) is None
    assert blend(None, None) is None


def test_blend_low_sample_internal_still_contributes_as_prior():
    # n_samples=0 should give a small fixed weight (informed prior),
    # not zero
    r = BaseRate(name="x", category=None, question_pattern="a",
                 p=0.30, n_samples=0)
    m = blend(r, [])
    assert m is not None
    assert abs(m.p - 0.30) < 1e-3
    assert m.sources[0].startswith("internal_prior:")
