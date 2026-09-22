import os
import tempfile
import time
import unittest
from pathlib import Path

import workflow_auto as auto


class RecorderFinalizingTests(unittest.TestCase):
    def test_offline_transition_keeps_queue_controls_until_media_is_stable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            watch = root / "recordings"
            watch.mkdir()
            media = watch / "录制-1727076670-20260826-090000-000-直播.flv"
            media.write_bytes(b"growing")
            clock = time.time()
            os.utime(media, (clock - 2, clock - 2))
            config = auto.default_config(root)
            config.update({
                "_watch_root": watch,
                "_output_root": root / "out",
                "_state_file": root / "state.json",
                "stable_seconds": 120,
                "session_quiet_seconds": 180,
                "backfill_existing": True,
                "mode": "narrative",
            })
            live = {
                "available": True,
                "checked_at": "live",
                "rooms": [{
                    "room_id": "1727076670",
                    "status": "Recording",
                    "bitrate": "3.00 Mbps",
                    "is_live": True,
                }],
            }
            offline = {
                "available": True,
                "checked_at": "offline",
                "rooms": [{
                    "room_id": "1727076670",
                    "status": "Monitoring",
                    "bitrate": "0.00 Mbps",
                    "is_live": False,
                }],
            }
            state = auto.empty_state()

            auto.scan_state(config, state, clock=clock, recorder_snapshot=live)
            job = state["jobs"]["live-1727076670-recorder"]
            auto.prioritize_queue_job(config, state, job)

            auto.scan_state(
                config,
                state,
                clock=clock + 10,
                recorder_snapshot=offline,
            )
            closing = state["jobs"]["live-1727076670-recorder"]
            self.assertEqual(closing["status"], auto.LIVE_FINALIZING_STATUS)
            self.assertEqual(closing["current_stage"], "直播已结束，等待录播封口")

            registered = auto.scan_state(
                config,
                state,
                clock=clock + 300,
                recorder_snapshot=offline,
                probe=lambda _path: 180.0,
            )
            self.assertEqual(len(registered), 1)
            self.assertNotIn("live-1727076670-recorder", state["jobs"])
            completed = state["jobs"][registered[0]]
            self.assertEqual(completed["status"], "queued")
            self.assertLess(completed["queue_priority"], 0)


if __name__ == "__main__":
    unittest.main()
