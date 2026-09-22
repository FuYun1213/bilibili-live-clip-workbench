import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bililive_recorder_status as recorder
import workflow_auto as auto


class BililiveRecorderStatusTests(unittest.TestCase):
    def test_only_reads_status_for_the_recorder_managed_watch_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = root / "local"
            managed = root / "recordings"
            pointer = local / "BililiveRecorder" / "path.json"
            pointer.parent.mkdir(parents=True)
            managed.mkdir()
            pointer.write_text(
                json.dumps({"Path": str(managed)}, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"LOCALAPPDATA": str(local)}):
                self.assertTrue(recorder.manages_watch_root(managed))
                self.assertFalse(recorder.manages_watch_root(root / "other"))

    def test_room_map_keeps_offline_rooms_for_authoritative_stop_detection(self):
        rooms = recorder.live_room_map({
            "available": True,
            "rooms": [
                {"room_id": "1", "is_live": True},
                {"room_id": "2", "is_live": False},
            ],
        })
        self.assertEqual(set(rooms), {"1", "2"})


class RecorderStatusQueueIntegrationTests(unittest.TestCase):
    def _config(self, root: Path, watch: Path) -> dict:
        config = auto.default_config(root)
        config.update({
            "_watch_root": watch,
            "_output_root": root / "out",
            "_state_file": root / "state.json",
            "stable_seconds": 120,
            "session_quiet_seconds": 900,
            "backfill_existing": True,
            "mode": "narrative",
        })
        return config

    def test_live_room_is_visible_before_first_media_segment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            watch = root / "recordings"
            watch.mkdir()
            state = auto.empty_state()
            snapshot = {
                "available": True,
                "source": "mikufans-wpf-ui",
                "checked_at": "2026-08-26T00:00:00+00:00",
                "rooms": [{
                    "room_id": "1727076670",
                    "name": "枝堇Sumire",
                    "title": "直播标题",
                    "status": "Streaming",
                    "bitrate": "3.20 Mbps",
                    "is_live": True,
                }],
            }

            auto.scan_state(
                self._config(root, watch),
                state,
                recorder_snapshot=snapshot,
            )

            self.assertEqual(len(state["jobs"]), 1)
            job = next(iter(state["jobs"].values()))
            self.assertEqual(job["status"], auto.LIVE_JOB_STATUS)
            self.assertEqual(job["current_stage"], "录播姬检测到直播，等待素材")
            self.assertEqual(job["segments"], [])
            self.assertEqual(job["recorder_status_source"], "mikufans-wpf-ui")

    def test_recorder_offline_state_overrides_recent_final_file_activity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            watch = root / "recordings"
            watch.mkdir()
            media = watch / "录制-1727076670-20260826-090000-000-刚结束.flv"
            media.write_bytes(b"recent-final-write")
            clock = time.time()
            os.utime(media, (clock - 2, clock - 2))
            state = auto.empty_state()
            snapshot = {
                "available": True,
                "source": "mikufans-wpf-ui",
                "checked_at": "2026-08-26T00:00:00+00:00",
                "rooms": [{
                    "room_id": "1727076670",
                    "status": "Monitoring",
                    "bitrate": "0.00 Mbps",
                    "is_live": False,
                }],
            }

            auto.scan_state(
                self._config(root, watch),
                state,
                clock=clock,
                recorder_snapshot=snapshot,
            )

            self.assertEqual(state["jobs"], {})
            self.assertTrue(state["recorder_status"]["available"])

    def test_live_file_joins_the_same_recorder_declared_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            watch = root / "recordings"
            watch.mkdir()
            media = watch / "录制-1727076670-20260826-090000-000-直播中.flv"
            media.write_bytes(b"growing")
            clock = time.time()
            os.utime(media, (clock - 2, clock - 2))
            state = auto.empty_state()
            snapshot = {
                "available": True,
                "source": "mikufans-wpf-ui",
                "checked_at": "2026-08-26T00:00:00+00:00",
                "rooms": [{
                    "room_id": "1727076670",
                    "status": "Recording",
                    "bitrate": "4.00 Mbps",
                    "is_live": True,
                }],
            }

            auto.scan_state(
                self._config(root, watch),
                state,
                clock=clock,
                recorder_snapshot=snapshot,
            )

            self.assertEqual(list(state["jobs"]), ["live-1727076670-recorder"])
            job = state["jobs"]["live-1727076670-recorder"]
            self.assertEqual(len(job["segments"]), 1)
            self.assertEqual(job["current_stage"], "正在直播，可快速切片")


if __name__ == "__main__":
    unittest.main()
