"""
Tests for the one-time dedupe of existing wisdom items.
Written first (red), then dedupe.py implemented (green).

Run: cd <repo-root> && python3 -m pytest scripts/test_dedupe.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dedupe  # noqa: E402

CONFIG = {"reinforcement_boost": 0.1, "max_confidence": 0.98}


def item(id_, content, conf=0.5, count=1, first="2026-05-01", last="2026-05-01"):
    return {"id": id_, "content": content, "confidence": conf, "reinforcement_count": count,
            "first_seen": first, "last_reinforced": last}


# --- clustering ----------------------------------------------------------------

def test_clusters_are_transitive():
    clusters = dedupe.cluster(["a", "b", "c", "d"], [("a", "b"), ("b", "c")])
    assert sorted(sorted(c) for c in clusters) == [["a", "b", "c"], ["d"]]


def test_cluster_ignores_unknown_ids():
    clusters = dedupe.cluster(["a", "b"], [("a", "zzz")])
    assert sorted(sorted(c) for c in clusters) == [["a"], ["b"]]


# --- merging a cluster ---------------------------------------------------------

def test_merge_cluster_sums_evidence_and_keeps_best_wording():
    items = [
        item("f001", "Prefers terse answers", conf=0.40, count=1, first="2026-05-01", last="2026-05-01"),
        item("f007", "Wants short replies without padding", conf=0.50, count=2, first="2026-06-01", last="2026-09-01"),
        item("f020", "Likes concise responses", conf=0.30, count=1, first="2026-04-01", last="2026-07-01"),
    ]
    merged = dedupe.merge_cluster(items, CONFIG)
    assert merged["id"] == "f007"                       # highest confidence is representative
    assert merged["content"] == "Wants short replies without padding"
    assert merged["reinforcement_count"] == 4
    assert merged["first_seen"] == "2026-04-01"
    assert merged["last_reinforced"] == "2026-09-01"
    assert abs(merged["confidence"] - 0.70) < 1e-9      # 0.50 + 2 × 0.1 boost
    assert sorted(merged["merged_ids"]) == ["f001", "f020"]


def test_merge_cluster_caps_confidence():
    items = [item(f"f{i:03d}", f"same {i}", conf=0.9) for i in range(5)]
    assert dedupe.merge_cluster(items, CONFIG)["confidence"] == 0.98


def test_singleton_cluster_is_unchanged():
    one = item("f001", "only one")
    assert dedupe.merge_cluster([one], CONFIG) == one


# --- applying to a category ----------------------------------------------------

def test_dedupe_category_uses_adjudicated_pairs_only():
    items = [item("f001", "Prefers terse answers"), item("f002", "Wants short replies"),
             item("f003", "Uses macOS")]
    result = dedupe.dedupe_category(items, pairs=[("f001", "f002")], config=CONFIG)
    ids = sorted(i["id"] for i in result)
    assert len(result) == 2 and "f003" in ids


def test_find_duplicate_pairs_validates_ids_and_survives_bad_output():
    class Client:
        def __init__(self, out):
            self.out = out

        def call(self, system, user_msg):
            assert "DEDUPE_ADJUDICATION" in system
            return self.out

    items = [item("f001", "Prefers terse short answers"), item("f002", "Wants terse short replies"),
             item("f003", "Uses macOS laptop")]
    good = Client(json.dumps({"duplicates": {"f001": ["f002", "f999"]}}))
    assert dedupe.find_duplicate_pairs(good, "user_facts", items) == [("f001", "f002")]
    assert dedupe.find_duplicate_pairs(Client("garbage"), "user_facts", items) == []
