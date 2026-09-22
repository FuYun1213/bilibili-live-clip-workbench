"""Regressions for four approved selections failing at Flow3 / 50 percent."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narrative_edit import align_authoritative_edit_ranges
import workflow_app_core as core
from test_workflow_app_integration import write_flow1_artifacts


def row(start, end, text="一句话", **extra):
    return dict(start_seconds=start, end_seconds=end, text=text, **extra)


def item(start, end, **extra):
    return dict(clip_id="001", timestamps=[row(start, end, "")], **extra)


class SubtitleBoundaryPreflightTests(unittest.TestCase):
    def test_real_incident_edges_snap_inward_and_map_without_losing_full_rows(self):
        cases = [
            (459.31, 463.86, 463.57, 465.15),
            (8024.6, 8073.17, 8072.97, 8074.18),
            (1421.63, 1452.5, 1452.22, 1455.67),
            (1730.97, 1744.11, 1743.81, 1744.67),
            (6361.32, 6377.6, 6377.35, 6382.33),
        ]
        for start, end, next_start, next_end in cases:
            with self.subTest(start=start):
                items = [item(start, end)]
                before = copy.deepcopy(items)
                fixed, audit = align_authoritative_edit_ranges(
                    items, [row(start, next_start - 0.1), row(next_start, next_end)]
                )
                self.assertEqual(fixed[0]["timestamps"][0]["end_seconds"], next_start)
                self.assertEqual(len(audit), 1)
                self.assertEqual(items, before)

    def test_leading_edge_snaps_forward_without_extending_selection(self):
        fixed, _ = align_authoritative_edit_ranges(
            [item(11.8, 15)], [row(10, 12), row(12.1, 14.8)]
        )
        self.assertEqual(fixed[0]["timestamps"][0]["start_seconds"], 12)

    def test_substantial_partial_sentence_still_requires_repair(self):
        for start, end in [(100, 114), (105, 135)]:
            with self.subTest(start=start):
                with self.assertRaisesRegex(ValueError, "尚未导出视频"):
                    align_authoritative_edit_ranges([item(start, end)], [row(100, 130)])

    def test_small_but_majority_of_short_sentence_is_not_discarded(self):
        with self.assertRaisesRegex(ValueError, "尚未导出视频"):
            align_authoritative_edit_ranges([item(10, 11.3)], [row(10, 10.9), row(11, 11.4)])

    def test_matching_word_times_allow_partial_text_without_snapping(self):
        source = row(10, 12, "甲乙", words=[
            dict(start_seconds=10, end_seconds=11, word="甲"),
            dict(start_seconds=11, end_seconds=12, word="乙"),
        ])
        items = [item(10, 11)]
        fixed, audit = align_authoritative_edit_ranges(items, [source])
        self.assertEqual(fixed, items)
        self.assertEqual(audit, [])

    def test_native_captions_do_not_force_unneeded_trim(self):
        items = [item(10, 12)]
        fixed, audit = align_authoritative_edit_ranges(items, [
            row(10, 11), row(11.8, 15, render_policy="native-captions")
        ])
        self.assertEqual(fixed, items)
        self.assertEqual(audit, [])

    def test_song_ranges_are_validated_but_never_automatically_trimmed(self):
        with self.assertRaisesRegex(ValueError, "尚未导出视频"):
            align_authoritative_edit_ranges(
                [item(10, 12.2, content_type="song")], [row(10, 11.9), row(12, 15)]
            )

    def test_overlapping_rows_cannot_chain_past_edge_budget(self):
        with self.assertRaisesRegex(ValueError, "尚未导出视频"):
            align_authoritative_edit_ranges([item(10, 12.3)], [
                row(10, 11.4), row(11.6, 12.5), row(11.9, 12.7), row(12.1, 13)
            ])

    def test_nonchronological_editorial_order_and_roles_are_preserved(self):
        items = [dict(clip_id="001", timestamps=[row(30, 32.2), row(10, 12)],
                      narrative_structure=dict(type="callback", roles=["cold_open", "body"], reason="先给结果"))]
        fixed, _ = align_authoritative_edit_ranges(items, [row(30, 31.9), row(32, 34), row(10, 12)])
        self.assertEqual([x["start_seconds"] for x in fixed[0]["timestamps"]], [30, 10])
        self.assertEqual(fixed[0]["narrative_structure"]["roles"], ["cold_open", "body"])

    def test_flow2_requires_alignment_before_marking_editorial_approved(self):
        from authoritative_alignment import AlignmentError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            state_path = core.init_project(root / "project", source, None,
                "sumire", "narrative", "large-v3-turbo", "cpu", "int8")
            write_flow1_artifacts(state_path.parent)
            selection = root / "selection.json"
            core.atomic_json(selection, [{"标题": "枝堇解释人设后被自己的原话拆穿", "副标题": "认真解释｜当场拆穿",
                                          "时间戳": [{"开始秒": 10, "结束秒": 40}]}])
            _, state = core.load_project(state_path)
            with patch("content_slicer.load_authoritative_segments", return_value=[row(0, 500)]), patch("authoritative_alignment.run_aligner", side_effect=AlignmentError("fixture unavailable")), patch.object(core, "run_external") as external:
                with self.assertRaisesRegex(core.SubtitleTimingNeedsReview, "fixture unavailable"):
                    core.import_selection(state_path, state, selection)
                external.assert_not_called()
                self.assertNotIn("editorial", state.get("approvals", {}))
                self.assertFalse((state_path.parent / "selection/selection.json").exists())


if __name__ == "__main__":
    unittest.main()
