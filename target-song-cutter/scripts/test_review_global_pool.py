from __future__ import annotations

import json
import tempfile
import unittest
from array import array
from pathlib import Path
from unittest import mock

import review_workspace


class GlobalReviewPoolTests(unittest.TestCase):
    def test_discovers_only_requested_statuses_with_creator_and_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workflow-projects"
            project = root / "session-a"
            review = project / "runs" / "prepare-a" / "export" / "clips"
            review.mkdir(parents=True)
            (project / "workflow-project.json").write_text(
                json.dumps({"config": {"creator": "sumire"}}), encoding="utf-8"
            )
            continuity = project / "continuity"
            continuity.mkdir()
            (continuity / "segments.json").write_text(
                json.dumps(
                    {
                        "segments": [
                            {
                                "path": "录制-1727076670-20260819-090003-593-测试直播.flv",
                                "started_at": "2026-08-19T09:00:03",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            pending = review / "001_pending.mp4"
            approved = review / "002_approved.mp4"
            pending.write_bytes(b"pending")
            approved.write_bytes(b"approved")
            (review / "titles-and-covers.csv").write_text(
                "video,title\n001_pending.mp4,待审标题\n002_approved.mp4,已审标题\n",
                encoding="utf-8-sig",
            )
            (review / review_workspace.DECISIONS_FILENAME).write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "clips": {
                            pending.name: {"status": "pending", "notes": ""},
                            approved.name: {"status": "approved", "notes": ""},
                        },
                    }
                ),
                encoding="utf-8",
            )

            items = review_workspace.discover_review_items(
                root, statuses={"pending", "revise"}
            )

            self.assertEqual([pending], [item["video"] for item in items])
            self.assertEqual("sumire", items[0]["creator"])
            self.assertEqual("2026-08-19 09:00｜测试直播", items[0]["session"])
            self.assertEqual("待审标题", items[0]["title"])

    def test_published_projects_are_excluded_even_if_decisions_stay_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workflow-projects"
            project = root / "published-session"
            review = project / "runs" / "prepare-a" / "export" / "clips"
            review.mkdir(parents=True)
            (project / "workflow-project.json").write_text(
                json.dumps(
                    {
                        "config": {"creator": "sumire"},
                        "published_at": "2026-08-22T03:03:52-07:00",
                        "steps": {"flow5": {"status": "published"}},
                    }
                ),
                encoding="utf-8",
            )
            video = review / "001_pending.mp4"
            video.write_bytes(b"published")
            (review / review_workspace.DECISIONS_FILENAME).write_text(
                json.dumps(
                    {"schema_version": 1, "clips": {video.name: {"status": "pending"}}}
                ),
                encoding="utf-8",
            )

            items = review_workspace.discover_review_items(
                root, statuses={"pending", "revise"}
            )

            self.assertEqual([], items)
            history_items = review_workspace.discover_review_items(
                root,
                statuses={"pending"},
                include_published_projects=True,
            )
            self.assertEqual([], history_items)
            review_workspace.set_decision(review, video.name, "approved")
            approved_history = review_workspace.discover_review_items(
                root,
                statuses={"approved"},
                include_published_projects=True,
            )
            self.assertEqual(
                [video], [item["video"] for item in approved_history]
            )

    def test_only_active_review_directory_is_discovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workflow-projects"
            project = root / "session-a"
            active = project / "runs" / "active" / "export" / "clips"
            stale = project / "redo-archives" / "old" / "export" / "clips"
            active.mkdir(parents=True)
            stale.mkdir(parents=True)
            (project / "workflow-project.json").write_text(
                json.dumps({
                    "config": {"creator": "sumire"},
                    "active_review_dir": str(active),
                }),
                encoding="utf-8",
            )
            active_video = active / "001_active.mp4"
            stale_video = stale / "001_stale.mp4"
            active_video.write_bytes(b"active")
            stale_video.write_bytes(b"stale")
            review_workspace.load_decisions(active)
            review_workspace.load_decisions(stale)

            items = review_workspace.discover_review_items(
                root, statuses={"pending", "revise"}
            )

            self.assertEqual(
                [active_video], [item["video"] for item in items]
            )

    def test_flow3_output_suppresses_stale_pending_copy_without_active_dir(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workflow-projects"
            project = root / "session-a"
            stale = project / "runs" / "prepare-old" / "export" / "clips"
            current = project / "runs" / "prepare-new" / "export" / "clips"
            stale.mkdir(parents=True)
            current.mkdir(parents=True)
            video_name = "001_same-title.mp4"
            stale_video = stale / video_name
            current_video = current / video_name
            stale_video.write_bytes(b"old")
            current_video.write_bytes(b"new")
            review_workspace.load_decisions(stale)
            review_workspace.load_decisions(current)
            review_workspace.set_decision(current, video_name, "approved")
            (project / "workflow-project.json").write_text(
                json.dumps(
                    {
                        "config": {"creator": "kioi"},
                        "steps": {
                            "flow3": {
                                "status": "completed",
                                "outputs": [
                                    str(current),
                                    str(current / review_workspace.DECISIONS_FILENAME),
                                ],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            pending = review_workspace.discover_review_items(
                root, statuses={"pending", "revise"}
            )
            history = review_workspace.discover_review_items(
                root,
                statuses={"approved"},
                include_published_projects=True,
            )

            self.assertEqual([], pending)
            self.assertEqual([current_video], [item["video"] for item in history])

    def test_published_review_changes_detects_cover_and_tag_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            review = project / "runs" / "prepare-a" / "export" / "clips"
            delivery = project / "deliveries" / "delivery-a" / "files"
            review.mkdir(parents=True)
            delivery.mkdir(parents=True)
            (project / "workflow-project.json").write_text(
                json.dumps({
                    "published_at": "2026-08-22T03:03:52-07:00",
                    "active_review_dir": str(review),
                    "delivery_dir": str(delivery),
                    "steps": {"flow5": {"status": "published"}},
                }),
                encoding="utf-8",
            )
            for folder in (review, delivery):
                (folder / "titles-and-covers.csv").write_text(
                    "video,title,tags,tag_evidence,cover_text_primary,cover_text_secondary\n"
                    "001.mp4,标题,旧Tag,依据,封面上,封面下\n",
                    encoding="utf-8-sig",
                )
                (folder / "001-cover.jpg").write_bytes(b"same-cover")

            self.assertEqual(
                [], review_workspace.published_review_changes(review, "001.mp4")
            )
            (review / "titles-and-covers.csv").write_text(
                "video,title,tags,tag_evidence,cover_text_primary,cover_text_secondary\n"
                "001.mp4,标题,新Tag,新依据,封面上,封面下\n",
                encoding="utf-8-sig",
            )
            (review / "001-cover.jpg").write_bytes(b"new-cover")
            changes = review_workspace.published_review_changes(
                review, "001.mp4"
            )
            self.assertIn("Tag", changes)
            self.assertIn("封面", changes)

    def test_published_delivery_metadata_is_not_invalidated_by_stale_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            review = project / "runs" / "prepare-a" / "export" / "clips"
            delivery = project / "deliveries" / "delivery-a" / "files"
            review.mkdir(parents=True)
            delivery.mkdir(parents=True)
            state_path = project / "workflow-project.json"
            state_path.write_text(
                json.dumps(
                    {
                        "published_at": "2026-08-22T03:03:52-07:00",
                        "delivery_dir": str(delivery),
                        "steps": {"flow5": {"status": "published"}},
                    }
                ),
                encoding="utf-8",
            )
            copy_path = delivery / "titles-and-covers.csv"
            copy_path.write_text(
                "video,tags,tag_evidence\n001.mp4,原标签,原依据\n",
                encoding="utf-8-sig",
            )

            result = review_workspace.sync_review_tags_to_delivery(
                review,
                "001.mp4",
                tags="新标签",
                tag_evidence="新依据",
            )

            self.assertIsNone(result)
            self.assertIn("原标签", copy_path.read_text(encoding="utf-8-sig"))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual("published", state["steps"]["flow5"]["status"])

    def test_waveform_peaks_downsamples_pcm(self):
        pcm = array("h", [0, 1000, -2000, 4000, -8000, 16000, -32000, 0])
        completed = mock.Mock(returncode=0, stdout=pcm.tobytes(), stderr=b"")
        with mock.patch.object(review_workspace.subprocess, "run", return_value=completed):
            result = review_workspace.waveform_peaks(
                Path("clip.mp4"), Path("ffmpeg.exe"), columns=8, sample_rate=1000
            )

        self.assertEqual(8, len(result["peaks"]))
        self.assertAlmostEqual(0.008, result["duration"])
        self.assertAlmostEqual(1.0, max(result["peaks"]))
        self.assertEqual(0.0, result["peaks"][0])


if __name__ == "__main__":
    unittest.main()
