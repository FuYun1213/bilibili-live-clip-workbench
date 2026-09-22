#!/usr/bin/env python3
"""Regression tests for live-song onset and repeated-chorus alignment."""

from __future__ import annotations

import unittest

from retime_reviewed_lyrics_from_asr import (
    best_line_anchor,
    select_monotonic_reliable,
    visible,
)


def acoustic(text: str, start: float, step: float = 0.22) -> list[dict]:
    return [
        {"char": char.casefold(), "start": start + index * step, "end": start + (index + 1) * step}
        for index, char in enumerate(visible(text))
    ]


class LiveTimingRegressionTest(unittest.TestCase):
    def test_repeated_easy_line_uses_matching_first_lyric_unit(self) -> None:
        raw = (
            acoustic("心情很easy很easyye", 68.88)
            + acoustic("梦很easy很easyye", 72.78)
        )
        anchor = best_line_anchor(
            visible("梦很easy很easyye"), 72.828, 76.82, raw, 10.0
        )
        self.assertTrue(anchor["onset_matched"])
        self.assertAlmostEqual(72.78, 72.828 + float(anchor["offset"]), places=2)

    def test_monotonic_chain_cannot_reuse_one_chorus_occurrence(self) -> None:
        rows = [
            {"start_seconds": "68.8"},
            {"start_seconds": "72.8"},
            {"start_seconds": "76.8"},
        ]
        anchors = [
            {"offset": 0.08, "coverage": 1.0, "mad": 0.02, "score": 3.0, "onset_matched": True},
            {"offset": -3.92, "coverage": 0.95, "mad": 0.03, "score": 2.9, "onset_matched": True},
            {"offset": 0.02, "coverage": 1.0, "mad": 0.02, "score": 3.0, "onset_matched": True},
        ]
        selected = select_monotonic_reliable(rows, anchors, 0.30, 1.20)
        self.assertEqual([0, 2], selected)


if __name__ == "__main__":
    unittest.main()
