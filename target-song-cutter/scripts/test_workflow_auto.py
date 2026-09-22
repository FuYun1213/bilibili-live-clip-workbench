from __future__ import annotations

import csv
import json
import os
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import workflow_auto as app


class DiskSpaceGuardTests(unittest.TestCase):
    def test_low_space_blocks_before_media_work(self):
        usage = app.shutil._ntuple_diskusage(
            10 * 1024**3,
            9 * 1024**3,
            1 * 1024**3,
        )
        with patch.object(app.shutil, "disk_usage", return_value=usage):
            with self.assertRaisesRegex(app.AutomationError, "磁盘空间不足"):
                app.ensure_heavy_job_disk_space(Path("D:/queue"))

    def test_sufficient_space_is_reported(self):
        usage = app.shutil._ntuple_diskusage(
            200 * 1024**3,
            50 * 1024**3,
            150 * 1024**3,
        )
        with patch.object(app.shutil, "disk_usage", return_value=usage):
            self.assertEqual(
                app.ensure_heavy_job_disk_space(Path("D:/queue")),
                150 * 1024**3,
            )


class CampaignScopeTests(unittest.TestCase):
    def test_campaign_match_requires_activity_date_and_live_title(self):
        profile = app.core.load_profiles()["harei"]
        matched = app.matching_creator_campaign(
            profile,
            {
                "title": "芙娅之魂回火测试联动",
                "started_at": "2026-08-28T19:00:00+08:00",
            },
        )
        self.assertIsNotNone(matched)
        self.assertEqual(matched["tags"], ["芙娅之魂"])
        self.assertEqual(
            matched["selection_prompt"],
            "references/campaign-prompts/fuya-soul-reforging-game.md",
        )
        self.assertIsNone(
            app.matching_creator_campaign(
                profile,
                {
                    "title": "普通杂谈",
                    "started_at": "2026-08-28T19:00:00+08:00",
                },
            )
        )

    def test_campaign_match_accepts_later_grouped_segment_title(self):
        profile = app.core.load_profiles()["yuchu"]
        matched = app.matching_creator_campaign(
            profile,
            {
                "title": "突击打半小时游戏！",
                "started_at": "2026-08-28T21:57:06+08:00",
                "segments": [
                    {
                        "path": "录制-1727074031-20260828-220619-来玩玩芙娅之魂！.flv"
                    }
                ],
            },
        )
        self.assertIsNotNone(matched)
        self.assertEqual(matched["key"], "fuya_soul_reforging_2026")
        self.assertIsNone(
            app.matching_creator_campaign(
                profile,
                {
                    "title": "芙娅之魂回火测试联动",
                    "started_at": "2026-09-11T19:00:00+08:00",
                },
            )
        )

    def test_campaign_match_accepts_play_game_title(self):
        profile = app.core.load_profiles()["hazel"]
        matched = app.matching_creator_campaign(
            profile,
            {
                "title": "今晚和大家一起玩游戏",
                "started_at": "2026-08-29T19:00:00+08:00",
            },
        )
        self.assertIsNotNone(matched)
        self.assertEqual(matched["key"], "fuya_soul_reforging_2026")
        self.assertEqual(matched["tags"], ["芙娅之魂"])

    def test_new_creators_normal_streams_remain_eligible_without_campaign_tags(self):
        profiles = app.core.load_profiles()
        for creator in ("harei", "hazel", "youyi"):
            with self.subTest(creator=creator):
                self.assertFalse(profiles[creator]["campaign_only"])
                job = {
                    "creator": creator,
                    "title": "普通杂谈",
                    "started_at": "2026-08-28T19:00:00+08:00",
                }
                self.assertTrue(
                    app.prepare_job_campaign_scope({}, app.empty_state(), job)
                )
                self.assertNotIn("campaign", job)

    def test_yuchu_normal_stream_remains_eligible_without_campaign_tags(self):
        job = {
            "creator": "yuchu",
            "title": "普通杂谈",
            "started_at": "2026-08-28T19:00:00+08:00",
        }
        self.assertTrue(app.prepare_job_campaign_scope({}, app.empty_state(), job))
        self.assertNotIn("campaign", job)

    def test_job_speaker_count_overrides_global_asr_default(self):
        config = {"asr": {"model": "local:qwen3-asr-auto", "speaker_count": 2}}
        self.assertEqual(
            app.effective_asr_config(config, {"speaker_count": 4})["speaker_count"],
            4,
        )

    def test_all_campaign_creators_share_game_selection_prompt(self):
        profiles = app.core.load_profiles()
        expected = "references/campaign-prompts/fuya-soul-reforging-game.md"
        for creator in ("harei", "hazel", "youyi", "yuchu"):
            with self.subTest(creator=creator):
                campaign = profiles[creator]["campaigns"][0]
                self.assertEqual(campaign["selection_prompt"], expected)
                self.assertNotIn("芙娅之魂", profiles[creator]["upload"]["tags"])

    def test_matched_campaign_prompt_is_persisted_to_project(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mp4"
            source.write_bytes(b"media")
            project = root / "project"
            app.core.init_project(
                project, source, None,
                "harei", "narrative", "large-v3-turbo", "cpu", "int8",
            )
            job = {
                "creator": "harei",
                "title": "芙娅之魂回火测试联动",
                "started_at": "2026-08-28T19:00:00+08:00",
            }
            self.assertTrue(app.prepare_job_campaign_scope({}, app.empty_state(), job))
            app.sync_job_campaign_to_project(project, job)
            _, state = app.core.load_project(project)
            self.assertEqual(
                state["config"]["campaign_selection_prompt"],
                "references/campaign-prompts/fuya-soul-reforging-game.md",
            )


class RecordingGroupingTests(unittest.TestCase):
    def test_parse_bililive_name(self):
        path = Path("录制-1727076670-20260819-091125-664-小粉螈记得进来报备一下.flv")
        part = app.parse_recording_path(path, duration=12.5, signature="x")
        self.assertEqual(part.room_id, "1727076670")
        self.assertEqual(part.started_at.utcoffset().total_seconds(), 8 * 3600)
        self.assertEqual(part.started_at.strftime("%Y-%m-%d %H:%M:%S.%f"), "2026-08-19 09:11:25.664000")
        self.assertEqual(part.title, "小粉螈记得进来报备一下")

    def test_groups_reconnects_and_splits_long_gap(self):
        names = [
            ("录制-1-20260819-090000-000-同一场.flv", 120.0),
            ("录制-1-20260819-090205-000-同一场.flv", 300.0),
            ("录制-1-20260819-120000-000-同一场.flv", 60.0),
        ]
        parts = [app.parse_recording_path(Path(name), duration, name) for name, duration in names]
        sessions = app.group_recordings(parts, session_gap_seconds=300)
        self.assertEqual([len(item.parts) for item in sessions], [2, 1])

    def test_title_change_does_not_split_same_room_session(self):
        names = [
            ("录制-1-20260819-090000-000-标题一.flv", 120.0),
            ("录制-1-20260819-090205-000-标题二.flv", 300.0),
        ]
        parts = [app.parse_recording_path(Path(name), duration, name) for name, duration in names]
        sessions = app.group_recordings(parts, session_gap_seconds=300)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(sessions[0].parts), 2)

    def test_discover_media_skips_consolidated_recording_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ordinary = root / "主播" / "录制-1-20260819-090000-000-标题.flv"
            consolidated = (
                ordinary.parent
                / app.CONSOLIDATED_RECORDING_DIRNAME
                / "录制-1-20260819-090000-000-标题-拼接版.mkv"
            )
            ordinary.parent.mkdir(parents=True)
            consolidated.parent.mkdir(parents=True)
            ordinary.write_bytes(b"ordinary")
            consolidated.write_bytes(b"consolidated")
            self.assertEqual(app.discover_media(root), [ordinary.resolve()])

    def test_first_scan_baselines_then_registers_new_same_session_parts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            watch = root / "1727076670-枝堇Sumire"
            watch.mkdir()
            first = watch / "录制-1727076670-20260819-090000-000-同一场.flv"
            first.write_bytes(b"old")
            old_time = time.time() - 1000
            os.utime(first, (old_time, old_time))
            config = app.default_config(root)
            config.update(
                {
                    "_watch_root": watch,
                    "_output_root": root / "out",
                    "_state_file": root / "state.json",
                    "stable_seconds": 0,
                    "session_quiet_seconds": 0,
                    "session_gap_seconds": 300,
                    "mode": "narrative",
                }
            )
            state = app.empty_state()
            self.assertEqual(app.scan_state(config, state, clock=time.time(), probe=lambda _: 60), [])
            second = watch / "录制-1727076670-20260819-090105-000-同一场.flv"
            second.write_bytes(b"new")
            os.utime(second, (old_time, old_time))
            jobs = app.scan_state(config, state, clock=time.time(), probe=lambda _: 60)
            self.assertEqual(len(jobs), 1)
            job = state["jobs"][jobs[0]]
            self.assertEqual(job["creator"], "sumire")
            self.assertEqual(len(job["segments"]), 2)

    def test_active_reconnect_fragment_restarts_grace_before_registering_session(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            watch = root / "1727076670-枝堇Sumire"
            watch.mkdir()
            first = watch / "录制-1727076670-20260819-090000-000-同一场.flv"
            reconnect = watch / "录制-1727076670-20260819-090105-000-同一场.flv"
            first.write_bytes(b"old")
            reconnect.write_bytes(b"still-growing")
            clock = time.time()
            old_time = clock - 1000
            os.utime(first, (old_time, old_time))
            os.utime(reconnect, (clock - 5, clock - 5))
            config = app.default_config(root)
            config.update(
                {
                    "_watch_root": watch,
                    "_output_root": root / "out",
                    "_state_file": root / "state.json",
                    "stable_seconds": 120,
                    "session_quiet_seconds": 900,
                    "session_gap_seconds": 300,
                    "backfill_existing": True,
                    "mode": "narrative",
                }
            )
            state = app.empty_state()

            self.assertEqual(
                app.scan_state(config, state, clock=clock, probe=lambda _: 60), []
            )
            self.assertEqual(len(state["reconnect_waits"]), 1)
            wait = next(iter(state["reconnect_waits"].values()))
            self.assertEqual(Path(wait["latest_path"]), reconnect.resolve())
            self.assertEqual(wait["segment_count"], 2)
            self.assertGreaterEqual(wait["remaining_seconds"], 895)

            os.utime(reconnect, (old_time, old_time))
            jobs = app.scan_state(config, state, clock=clock, probe=lambda _: 60)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(len(state["jobs"][jobs[0]]["segments"]), 2)
            self.assertEqual(state["reconnect_waits"], {})

    def test_new_title_in_same_room_still_blocks_previous_fragment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            watch = root / "1727076670-枝堇Sumire"
            watch.mkdir()
            first = watch / "录制-1727076670-20260819-090000-000-标题一.flv"
            reconnect = watch / "录制-1727076670-20260819-090105-000-标题二.flv"
            first.write_bytes(b"old")
            reconnect.write_bytes(b"active")
            clock = time.time()
            os.utime(first, (clock - 1000, clock - 1000))
            os.utime(reconnect, (clock - 5, clock - 5))
            config = app.default_config(root)
            config.update(
                {
                    "_watch_root": watch,
                    "_output_root": root / "out",
                    "_state_file": root / "state.json",
                    "stable_seconds": 120,
                    "session_quiet_seconds": 900,
                    "backfill_existing": True,
                    "mode": "narrative",
                }
            )
            state = app.empty_state()

            self.assertEqual(
                app.scan_state(config, state, clock=clock, probe=lambda _: 60), []
            )
            self.assertEqual(len(state["reconnect_waits"]), 1)

    def test_batch_range_registers_only_intersecting_session_and_binds_xml(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            watch = root / "1727076670-枝堇Sumire"
            watch.mkdir()
            outside = watch / "录制-1727076670-20260818-090000-000-旧场.flv"
            inside = watch / "录制-1727076670-20260819-090000-000-目标场.flv"
            xml = inside.with_suffix(".xml")
            outside.write_bytes(b"old")
            inside.write_bytes(b"target")
            xml.write_text("<i></i>", encoding="utf-8")
            old_time = time.time() - 1000
            for path in (outside, inside, xml):
                os.utime(path, (old_time, old_time))
            config = app.default_config(root)
            config.update(
                {
                    "_watch_root": root,
                    "_output_root": root / "out",
                    "_state_file": root / "state.json",
                    "stable_seconds": 0,
                    "session_quiet_seconds": 0,
                    "mode": "narrative",
                }
            )
            state = app.empty_state()
            recorder_time = app.parse_recording_path(inside, 60, "x").started_at
            local_day = recorder_time.astimezone().strftime("%Y-%m-%d")
            start, end = app.parse_batch_range(local_day, local_day)
            report = app.register_range_jobs(
                config, state, start, end, clock=time.time(), probe=lambda _: 60
            )
            self.assertEqual(report["matched_sessions"], 1)
            self.assertEqual(len(report["registered"]), 1)
            self.assertEqual(report["skipped_invalid"], 0)
            job = state["jobs"][report["job_ids"][0]]
            self.assertEqual(Path(job["segments"][0]["path"]), inside.resolve())
            self.assertEqual(Path(job["segments"][0]["xml"]), xml.resolve())
            self.assertIn(str(outside.resolve()), state["known"])

    def test_date_only_batch_end_includes_the_whole_day(self):
        start, end = app.parse_batch_range("2026-08-19", "2026-08-19")
        self.assertEqual(start, datetime(2026, 8, 19, 0, 0).astimezone())
        self.assertEqual(
            end, datetime(2026, 8, 19, 23, 59, 59, 999999).astimezone()
        )

    def test_recorder_utc8_time_is_displayed_in_machine_timezone(self):
        expected = datetime(
            2026, 8, 19, 9, 0, tzinfo=app.timezone_for_offset(8)
        ).astimezone()
        shown = app.format_recording_time_local("2026-08-19T09:00:00", 8)
        self.assertEqual(shown, expected.strftime("%Y-%m-%d %H:%M"))

    def test_timezone_metadata_does_not_change_legacy_session_id(self):
        part = app.parse_recording_path(
            Path("录制-1-20260819-090000-000-同一场.flv"), 60, "x"
        )
        session = app.group_recordings([part], 300)[0]
        self.assertIn("2026-08-19T09:00:00", session.key)
        self.assertNotIn("+08:00", session.key)


class Flow0PreflightTests(unittest.TestCase):
    def test_ffprobe_retries_windows_dll_init_failure_without_console(self):
        calls: list[dict] = []
        results = iter(
            (
                type("Result", (), {"returncode": -1073741502, "stdout": "", "stderr": ""})(),
                type("Result", (), {"returncode": 0, "stdout": "12.5\n", "stderr": ""})(),
            )
        )
        original_run = app.subprocess.run
        original_sleep = app.time.sleep

        def fake_run(_command, **kwargs):
            calls.append(kwargs)
            return next(results)

        try:
            app.subprocess.run = fake_run
            app.time.sleep = lambda _seconds: None
            duration = app.ffprobe_duration(Path("recording.flv"))
        finally:
            app.subprocess.run = original_run
            app.time.sleep = original_sleep

        self.assertEqual(duration, 12.5)
        self.assertEqual(len(calls), 2)
        if os.name == "nt":
            self.assertEqual(
                calls[0].get("creationflags"),
                getattr(app.subprocess, "CREATE_NO_WINDOW", 0),
            )

    def test_ffprobe_runtime_failure_is_not_cached_as_bad_media(self):
        with tempfile.TemporaryDirectory() as temp:
            media = Path(temp) / "recording.flv"
            media.write_bytes(b"media")
            state = app.empty_state()
            signatures = {str(media.resolve()): app.file_signature(media)}

            def failed_probe(_path: Path) -> float:
                raise app.ProbeRuntimeError("0xC0000142")

            with self.assertRaises(app.ProbeRuntimeError):
                app.validated_media_parts(state, [media], signatures, failed_probe)
            self.assertEqual(state.get("invalid_media", {}), {})
            self.assertNotIn(str(media.resolve()), state["known"])
    def test_discover_media_includes_flv_and_mp4(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            flv = root / "part.flv"
            mp4 = root / "part.mp4"
            ignored = root / "notes.txt"
            flv.write_bytes(b"flv")
            mp4.write_bytes(b"mp4")
            ignored.write_text("ignore", encoding="utf-8")
            discovered = app.discover_media(root)
            self.assertEqual(set(discovered), {flv.resolve(), mp4.resolve()})

    def test_bad_flv_does_not_block_valid_flv_and_mp4_session(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            watch = root / "1727071052-小松绿Viridis"
            watch.mkdir()
            good_flv = watch / "录制-1727071052-20260819-090000-000-同一场.flv"
            bad_flv = watch / "录制-1727071052-20260819-090105-000-同一场.flv"
            good_mp4 = watch / "录制-1727071052-20260819-090210-000-同一场.mp4"
            good_flv.write_bytes(b"good-flv")
            bad_flv.write_bytes(b"broken-flv-shell")
            good_mp4.write_bytes(b"good-mp4")
            old_time = time.time() - 1000
            for path in (good_flv, bad_flv, good_mp4):
                os.utime(path, (old_time, old_time))
            config = app.default_config(root)
            config.update(
                {
                    "_watch_root": root,
                    "_output_root": root / "out",
                    "_state_file": root / "state.json",
                    "stable_seconds": 0,
                    "session_quiet_seconds": 0,
                    "session_gap_seconds": 300,
                    "backfill_existing": True,
                    "mode": "narrative",
                }
            )
            state = app.empty_state()

            def probe(path: Path) -> float:
                if path.resolve() == bad_flv.resolve():
                    raise app.AutomationError("ffprobe 无法读取素材时长")
                return 60.0

            jobs = app.scan_state(config, state, clock=time.time(), probe=probe)
            self.assertEqual(len(jobs), 1)
            job = state["jobs"][jobs[0]]
            accepted = {Path(item["path"]) for item in job["segments"]}
            self.assertEqual(accepted, {good_flv.resolve(), good_mp4.resolve()})
            self.assertEqual(len(job["invalid_segments"]), 1)
            self.assertEqual(
                Path(job["invalid_segments"][0]["path"]), bad_flv.resolve()
            )
            self.assertIn(str(bad_flv.resolve()), state["known"])
            self.assertIn(str(bad_flv.resolve()), state["invalid_media"])

    def test_late_segments_create_visible_continuation_with_only_new_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {
                "_output_root": root / "projects",
                "_state_file": root / "state.json",
                "mode": "narrative",
            }
            original = {"path": str(root / "part-1.flv"), "duration": 60}
            later_a = {"path": str(root / "part-2.flv"), "duration": 120}
            later_b = {"path": str(root / "part-3.flv"), "duration": 180}
            job = {
                "id": "session-1",
                "status": "late_segment",
                "late_segment_previous_status": "published",
                "creator": "chilly",
                "mode": "narrative",
                "project": str(root / "published-project"),
                "segments": [original],
                "late_segments": [original, later_a, later_b],
            }
            state = app.empty_state()
            state["jobs"][job["id"]] = job

            continuation = app.create_late_segment_continuation(config, state, job)

            self.assertEqual(continuation["status"], "queued")
            self.assertEqual(continuation["continuation_of"], "session-1")
            self.assertEqual(
                [Path(item["path"]).name for item in continuation["segments"]],
                ["part-2.flv", "part-3.flv"],
            )
            self.assertEqual(job["status"], "published")
            self.assertEqual(len(job["segments"]), 1)
            self.assertEqual(len(job["accounted_segments"]), 3)
            self.assertNotIn("late_segments", job)
            self.assertTrue(config["_state_file"].is_file())

    def test_late_segment_continuation_rejects_manifest_without_new_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = {"path": str(root / "part-1.flv"), "duration": 60}
            job = {
                "id": "session-2",
                "status": "late_segment",
                "late_segment_previous_status": "published",
                "segments": [original],
                "late_segments": [dict(original)],
            }
            state = app.empty_state()
            state["jobs"][job["id"]] = job
            with self.assertRaisesRegex(app.AutomationError, "没有新的独立文件"):
                app.create_late_segment_continuation(
                    {
                        "_output_root": root / "projects",
                        "_state_file": root / "state.json",
                    },
                    state,
                    job,
                )

    def test_continue_late_job_cli_is_available(self):
        args = app.build_parser().parse_args(
            [
                "--config",
                "config.json",
                "continue-late-job",
                "--job-id",
                "session-3",
            ]
        )
        self.assertEqual(args.command, "continue-late-job")
        self.assertEqual(args.job_id, "session-3")

    def test_flow0_digest_ignores_observation_timestamps(self):
        base = {
            "segments": [{"path": "good.flv", "signature": "1:2", "duration": 10}],
            "invalid_segments": [
                {
                    "path": "bad.flv",
                    "signature": "3:4",
                    "reason": "ffprobe failed",
                    "observed_at": "first",
                    "last_seen_at": "first",
                }
            ],
        }
        later = json.loads(json.dumps(base))
        later["invalid_segments"][0]["last_seen_at"] = "later"
        self.assertEqual(
            app.flow0_manifest_digest(base), app.flow0_manifest_digest(later)
        )
    def test_materialize_mixed_formats_writes_flow0_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first.flv"
            second = root / "second.mp4"
            bad = root / "broken.flv"
            first.write_bytes(b"flv")
            second.write_bytes(b"mp4")
            bad.write_bytes(b"bad")
            project = root / "project"
            job = {
                "project": str(project),
                "segments": [
                    {
                        "path": str(first),
                        "xml": "",
                        "signature": "first",
                        "duration": 10.0,
                        "started_at": "2026-08-19T09:00:00",
                    },
                    {
                        "path": str(second),
                        "xml": "",
                        "signature": "second",
                        "duration": 20.0,
                        "started_at": "2026-08-19T09:00:10",
                    },
                ],
                "invalid_segments": [
                    {
                        "path": str(bad),
                        "extension": ".flv",
                        "reason": "ffprobe failed",
                    }
                ],
            }
            original_run = app._run_media_command
            original_probe = app.ffprobe_duration
            try:
                app._run_media_command = (
                    lambda _project, _stage, command: Path(command[-1]).write_bytes(
                        b"merged"
                    )
                )
                app.ffprobe_duration = lambda _path: 30.0
                source, xml = app.materialize_session({}, job)
            finally:
                app._run_media_command = original_run
                app.ffprobe_duration = original_probe

            self.assertTrue(source.is_file())
            self.assertIsNone(xml)
            report = json.loads(
                (project / "continuity" / "preflight-report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report["flow"], "flow0")
            self.assertEqual(report["accepted_formats"], {".flv": 1, ".mp4": 1})
            self.assertEqual(report["accepted_count"], 2)
            self.assertEqual(report["skipped_invalid_count"], 1)
            self.assertEqual(report["action"], "merged-stream-copy")

    def test_materialize_can_replace_split_files_with_verified_consolidated_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            watch = root / "录播"
            stream = watch / "1-测试主播"
            stream.mkdir(parents=True)
            first = stream / "录制-1-20260819-090000-000-标题一.flv"
            second = stream / "录制-1-20260819-090010-000-标题二.flv"
            first_xml = first.with_suffix(".xml")
            second_xml = second.with_suffix(".xml")
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            first_xml.write_text("<i><d p='1,1,25,1,0,0,1,0'>一</d></i>", encoding="utf-8")
            second_xml.write_text("<i><d p='1,1,25,1,0,0,2,0'>二</d></i>", encoding="utf-8")
            project = root / "project"
            job = {
                "project": str(project),
                "segments": [
                    {"path": str(first), "xml": str(first_xml), "duration": 10.0},
                    {"path": str(second), "xml": str(second_xml), "duration": 20.0},
                ],
                "invalid_segments": [],
            }
            config = {
                "_watch_root": watch,
                "replace_split_files_after_flow0": True,
            }
            original_run = app._run_media_command
            original_probe = app.ffprobe_duration
            try:
                app._run_media_command = (
                    lambda _project, _stage, command: Path(command[-1]).write_bytes(b"merged")
                )
                app.ffprobe_duration = lambda _path: 30.0
                source, xml = app.materialize_session(config, job)
            finally:
                app._run_media_command = original_run
                app.ffprobe_duration = original_probe

            self.assertTrue(source.is_file())
            self.assertEqual(source.parent.name, app.CONSOLIDATED_RECORDING_DIRNAME)
            self.assertTrue(xml and xml.is_file())
            for path in (first, second, first_xml, second_xml):
                self.assertFalse(path.exists())
            self.assertEqual(app.discover_media(watch), [])
            self.assertEqual(len(job["flow0_replacement"]["deleted"]), 4)
            manifest = json.loads(
                (project / "continuity" / "segments.json").read_text(encoding="utf-8")
            )
            self.assertEqual(Path(manifest["continuous_source"]), source)

    def test_short_stream_copy_retries_with_transcode(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first.flv"
            second = root / "second.mp4"
            first.write_bytes(b"flv")
            second.write_bytes(b"mp4")
            project = root / "project"
            job = {
                "project": str(project),
                "segments": [
                    {"path": str(first), "xml": "", "duration": 10.0},
                    {"path": str(second), "xml": "", "duration": 20.0},
                ],
                "invalid_segments": [],
            }
            stages: list[str] = []
            durations = iter((5.0, 30.0))
            original_run = app._run_media_command
            original_probe = app.ffprobe_duration

            def fake_run(_project, stage, command):
                stages.append(stage)
                Path(command[-1]).write_bytes(b"output")

            try:
                app._run_media_command = fake_run
                app.ffprobe_duration = lambda _path: next(durations)
                source, _xml = app.materialize_session({}, job)
            finally:
                app._run_media_command = original_run
                app.ffprobe_duration = original_probe

            self.assertTrue(source.is_file())
            self.assertEqual(
                stages,
                ["flow0-continuity-copy", "flow0-continuity-transcode"],
            )
            report = json.loads(
                (project / "continuity" / "preflight-report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report["action"], "merged-transcode")

class XmlAndCodexContractTests(unittest.TestCase):
    def test_selection_schema_does_not_hardcode_candidate_count(self) -> None:
        schema = app.selection_schema()
        clips = schema["properties"]["选片"]
        self.assertNotIn("maxItems", clips)
        item = clips["items"]
        self.assertIn("多人类型", item["required"])
        self.assertEqual(item["properties"]["嘉宾台词已核实"]["const"], False)
    def _xml(self, path: Path, *, danmaku: float | None = None, sc: float | None = None) -> None:
        root = ET.Element("i")
        ET.SubElement(root, "BililiveRecorderRecordInfo", {"roomid": "1"})
        if danmaku is not None:
            node = ET.SubElement(root, "d", {"p": f"{danmaku},1,25,16777215,0,0,user,0"})
            node.text = "弹幕"
        if sc is not None:
            node = ET.SubElement(root, "sc", {"ts": str(sc), "price": "30"})
            node.text = "SC"
        ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)

    def test_companion_xml_requires_one_nonempty_match(self):
        with tempfile.TemporaryDirectory() as temp:
            media = Path(temp) / "录制-1-20260819-090000-000-测试.flv"
            xml = media.with_suffix(".xml")
            media.write_bytes(b"media")
            xml.write_bytes(b"")
            self.assertIsNone(app.companion_xml(media))
            xml.write_text("<i></i>", encoding="utf-8")
            self.assertEqual(app.companion_xml(media), xml.resolve())

    def test_xml_offsets_include_a_segment_without_xml(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, third = root / "1.xml", root / "3.xml"
            self._xml(first, danmaku=1)
            self._xml(third, danmaku=2, sc=3)
            output = root / "merged.xml"
            app.merge_bililive_xml(
                [
                    {"xml": str(first), "duration": 5},
                    {"xml": "", "duration": 10},
                    {"xml": str(third), "duration": 4},
                ],
                output,
            )
            merged = ET.parse(output).getroot()
            d_times = [float(node.attrib["p"].split(",")[0]) for node in merged.findall("d")]
            sc_times = [float(node.attrib["ts"]) for node in merged.findall("sc")]
            self.assertEqual(d_times, [1.0, 17.0])
            self.assertEqual(sc_times, [18.0])

    def test_codex_command_is_ephemeral_read_only_and_structured(self):
        command = app.codex_selection_command(
            ["codex"], "选片", Path("schema.json"), Path("selection.json")
        )
        self.assertIn("--ephemeral", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertIn("--output-schema", command)
        self.assertIn("-o", command)
        self.assertEqual(command[-1], "-")
        self.assertNotIn("选片", command)
        config = app.default_config()
        self.assertEqual(config["codex_model"], "gpt-6-astra")
        self.assertEqual(config["codex_reasoning_effort"], "xhigh")
        self.assertEqual(command[command.index("--model") + 1], config["codex_model"])
        self.assertEqual(
            command[command.index("--config") + 1],
            f'model_reasoning_effort="{config["codex_reasoning_effort"]}"',
        )

    def test_codex_schema_wraps_selection_but_public_payload_stays_array(self):
        schema = app.selection_schema()
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["required"], ["选片"])
        expected = [{"标题": "题", "副标题": "副", "时间戳": []}]
        self.assertEqual(app.unwrap_codex_selection({"选片": expected}), expected)

    def test_reuses_repairable_historical_selection_without_codex(self):
        with tempfile.TemporaryDirectory() as temp:
            selection = Path(temp)
            previous = selection / "selection.codex.invalid-2.json"
            app.core.atomic_json(
                previous,
                {
                    "选片": [
                        {
                            "标题": "听见耳洞话题后先心动又顾虑，最终决定用耳夹代替打孔",
                            "副标题": "耳洞冲动｜改用耳夹",
                            "时间戳": [{"开始": "00:00:00", "结束": "00:00:40"}],
                        }
                    ]
                },
            )
            output = selection / "selection.codex.json"
            state = {"config": {"creator": "chilly", "mode": "narrative"}}
            recovered = app.recover_previous_selection(selection, output, state)
            self.assertEqual(recovered, output)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(payload[0]["标题"].startswith("听见耳洞话题"))
            self.assertTrue((selection / "selection-recovery.json").is_file())

    def test_reuses_manual_retry_archive_without_codex(self):
        with tempfile.TemporaryDirectory() as temp:
            selection = Path(temp)
            previous = (
                selection
                / "manual-retries"
                / "20260821-010203"
                / "selection.codex.invalid-1.json"
            )
            previous.parent.mkdir(parents=True)
            app.core.atomic_json(
                previous,
                {
                    "选片": [
                        {
                            "标题": "VR和PSP同时掉水里救谁？我得等他们救我，因为我只会悬浮",
                            "副标题": "谁也救不了｜等他们救我",
                            "时间戳": [{"开始": "00:00:00", "结束": "00:00:40"}],
                        }
                    ]
                },
            )
            output = selection / "selection.codex.json"
            state = {"config": {"creator": "awu", "mode": "narrative"}}
            recovered = app.recover_previous_selection(selection, output, state)
            self.assertEqual(recovered, output)
            self.assertTrue(output.is_file())
            receipt = json.loads(
                (selection / "selection-recovery.json").read_text(encoding="utf-8")
            )
            self.assertIn("manual-retries", receipt["source"])

    def test_selection_quality_accepts_creator_tag_without_rewriting_body(self):
        state = {"config": {"creator": "chilly", "mode": "narrative"}}
        payload = [
            {
                "标题": "听见耳洞话题后先心动又顾虑，最终决定用耳夹代替打孔",
                "副标题": "耳洞冲动｜改用耳夹",
                "时间戳": [{"开始": "00:00:00", "结束": "00:00:40"}],
            },
            {
                "标题": "发现文件丢失后短暂当机，最后改为先处理简单头像",
                "副标题": "文件丢失｜先画头像",
                "时间戳": [{"开始": "00:01:00", "结束": "00:01:40"}],
            },
        ]
        app.validate_selection_payload(payload, state)
        self.assertEqual(app.repair_selection_titles(payload, state), payload)

    def test_selection_prevalidation_uses_real_title_and_tag_gates(self):
        state = {"config": {"creator": "viridis", "mode": "narrative"}}
        original = app.core.load_profiles
        app.core.load_profiles = lambda: {
            "viridis": {
                "title_tag": "小松绿Viridis",
                "upload": {"tags": ["VirtuaReal", "小松绿Viridis"]},
            }
        }
        try:
            app.validate_selection_payload(
                [
                    {
                        "标题": "小松绿锐评直播间怪话，结果被弹幕用原话当场拆穿",
                        "副标题": "锐评怪话｜当场拆穿",
                        "时间戳": [{"开始": "00:00:10", "结束": "00:00:40"}],
                    }
                ],
                state,
            )
        finally:
            app.core.load_profiles = original


    def test_incomplete_title_is_routed_to_review_without_dropping_clip(self):
        warnings = app.validate_selection_payload(
            [
                {
                    "标题": "筑基大能",
                    "副标题": "刚成为筑基大能｜却被读音当场卡住",
                    "时间戳": [{"开始": "00:00:00", "结束": "00:00:40"}],
                }
            ],
            {"config": {"creator": "sumire", "mode": "narrative"}},
        )
        self.assertTrue(any("切片保留并送人工审核" in item for item in warnings))

    def test_local_boundary_repair_preserves_callback_and_merges_chronological_short_gap(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            transcript = project / "analysis" / "transcript" / "transcript.filtered.csv"
            transcript.parent.mkdir(parents=True)
            with transcript.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=("start_seconds", "end_seconds", "text")
                )
                writer.writeheader()
                for start in range(0, 60, 10):
                    writer.writerow({
                        "start_seconds": start,
                        "end_seconds": start + 10,
                        "text": f"完整转写句{start}",
                    })
            source = project / "continuity" / "continuous-source.mkv"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"media")
            state = {"config": {
                "creator": "viridis",
                "mode": "narrative",
                "source": str(source),
            }}
            payload = [{
                "标题": "小松绿先提出计划又被现场变化打断，最后改成完整方案",
                "副标题": "计划变化｜完整收尾",
                "时间戳": [
                    {"开始秒": 40, "结束秒": 60},
                    {"开始秒": 5, "结束秒": 25},
                ],
            }]
            repaired = app.repair_selection_boundaries(payload, state)
            items = app.core.normalize_selection(repaired, "viridis", "narrative")
            self.assertEqual(items[0]["timestamps"], [
                {"start_seconds": 40.0, "end_seconds": 60.0},
                {"start_seconds": 0.0, "end_seconds": 30.0},
            ])
            self.assertEqual(items[0]["narrative_structure"]["roles"], ["cold_open", "setup"])
            self.assertEqual(app.narrative_boundary_issues(items[0], []), [])
            payload[0]["时间戳"].reverse()
            chronological = app.repair_selection_boundaries(payload, state)
            items = app.core.normalize_selection(chronological, "viridis", "narrative")
            self.assertEqual(items[0]["timestamps"], [{"start_seconds": 0.0, "end_seconds": 60.0}])
            self.assertEqual(items[0]["narrative_structure"]["roles"], ["body"])

    def test_boundary_gate_reports_mid_sentence_start_and_end(self):
        item = {
            "clip_id": "001",
            "timestamps": [{"start_seconds": 5.0, "end_seconds": 25.0}],
        }
        rows = [
            {"start_seconds": "0", "end_seconds": "10", "text": "第一句"},
            {"start_seconds": "20", "end_seconds": "30", "text": "最后一句"},
        ]
        issues = app.narrative_boundary_issues(item, rows)
        self.assertTrue(any("开头切在转写句中间" in issue for issue in issues))
        self.assertTrue(any("结尾切在转写句中间" in issue for issue in issues))
    def test_grounding_gate_rejects_unrelated_title_and_delivery_codes(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            transcript = project / "analysis" / "transcript" / "transcript.filtered.csv"
            transcript.parent.mkdir(parents=True)
            with transcript.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=("start_seconds", "end_seconds", "text"),
                )
                writer.writeheader()
                writer.writerows(
                    [
                        {
                            "start_seconds": 0,
                            "end_seconds": 40,
                            "text": "小松绿说不要只看一个管人，多看几个人才能排满生活",
                        },
                        {
                            "start_seconds": 40,
                            "end_seconds": 80,
                            "text": "外卖优惠口令是663645，我终于背下来了",
                        },
                    ]
                )
            source = project / "continuity" / "continuous-source.mkv"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"media")
            state = {
                "config": {
                    "creator": "viridis",
                    "mode": "narrative",
                    "source": str(source),
                }
            }
            warnings = app.validate_selection_payload(
                [
                    {
                        "标题": "小松绿解释皮椅必须垫坐垫，最后说露出皮肤会粘",
                        "副标题": "皮椅会粘腿｜越解释越不对",
                        "时间戳": [{"开始": "00:00:00", "结束": "00:00:40"}],
                    }
                ],
                state,
            )
            self.assertTrue(
                any("标题与所选正文缺少直接证据" in item for item in warnings)
            )
            issues = app.selection_grounding_issues(
                app.core.normalize_selection(
                    [
                        {
                            "标题": "小松绿终于背下外卖优惠口令，结果数字让观众记住了",
                            "副标题": "背出口令｜神秘数字",
                            "时间戳": [{"开始": "00:00:40", "结束": "00:01:20"}],
                        }
                    ],
                    "viridis",
                    "narrative",
                ),
                state,
            )
            self.assertTrue(any("硬排除内容" in issue for issue in issues))
            with self.assertRaisesRegex(app.SelectionQualityError, "硬排除内容"):
                app.validate_selection_payload(
                    [
                        {
                            "标题": "小松绿终于背下外卖优惠口令，结果数字让观众记住了",
                            "副标题": "背出口令｜神秘数字",
                            "时间戳": [{"开始": "00:00:40", "结束": "00:01:20"}],
                        }
                    ],
                    state,
                )

class ProgressStateTests(unittest.TestCase):
    def test_legacy_waiting_job_gets_derived_progress(self):
        snapshot = app.job_progress_snapshot(
            {
                "status": "ready_to_publish",
                "project": "",
                "detail": "预览完成",
            }
        )
        self.assertEqual(snapshot["progress_percent"], 90)
        self.assertEqual(snapshot["current_stage"], "等待投稿授权")

    def test_persisted_progress_never_moves_backwards(self):
        with tempfile.TemporaryDirectory() as temp:
            state = app.empty_state()
            job = {
                "id": "job",
                "status": "running",
                "project": str(Path(temp) / "project"),
                "progress_percent": 60,
                "current_stage": "流程3完成",
            }
            state["jobs"]["job"] = job
            config = {"_state_file": Path(temp) / "state.json"}
            app._set_job_progress(config, state, job, 20, "流程1完成")
            self.assertEqual(job["progress_percent"], 60)
            self.assertEqual(job["current_stage"], "流程3完成")
            app._set_job_progress(config, state, job, 70, "流程4进行中")
            self.assertEqual(job["progress_percent"], 70)
            self.assertEqual(job["current_stage"], "流程4进行中")
            saved = json.loads(config["_state_file"].read_text(encoding="utf-8"))
            self.assertEqual(saved["jobs"]["job"]["progress_percent"], 70)


class ExplicitCodexRetryTests(unittest.TestCase):
    def _prepare_retry(self, root):
        project = root / "project"
        selection = project / "selection"
        selection.mkdir(parents=True)
        old = [{
            "标题": "听见耳洞话题后先心动又顾虑，最终决定用耳夹代替打孔",
            "副标题": "耳洞冲动｜改用耳夹",
            "时间戳": [{"开始": "00:00:00", "结束": "00:00:40"}],
        }]
        project_state = {
            "schema_version": app.core.SCHEMA_VERSION,
            "config": {"creator": "chilly", "mode": "narrative"},
            "steps": {"flow1": {"status": "completed"}},
        }
        state_path = project / app.core.PROJECT_FILENAME
        app.core.atomic_json(state_path, project_state)
        app.core.atomic_json(selection / "selection.codex.json", {"选片": old})
        app.core.atomic_json(selection / "selection.codex.invalid-1.json", {"选片": old})
        config = {**app.default_config(), "_state_file": root / "queue.json"}
        queue = app.empty_state()
        job = {
            "id": "retry", "status": "awaiting_selection_review",
            "project": str(project),
        }
        queue["jobs"][job["id"]] = job
        archive = app.prepare_codex_retry(config, queue, job)
        state_path, project_state = app.core.load_project(project)
        # Prove that the archived old result could be recovered by the old path.
        proof = selection / "recoverability-proof.json"
        self.assertEqual(app.recover_previous_selection(selection, proof, project_state), proof)
        proof.unlink()
        (selection / "selection-recovery.json").unlink()
        prompt = selection / "selection-prompt.md"
        prompt.write_text("new prompt", encoding="utf-8")
        (selection / "selection-context.csv").write_text(
            "start_seconds,end_seconds,text\n0,40,耳夹\n", encoding="utf-8"
        )
        app.core.atomic_json(selection / "selection-context-stats.json", {"selected_rows": 1})
        return config, state_path, project_state, prompt, archive

    def test_explicit_retry_calls_model_despite_repairable_archive_then_recovers_current(self):
        with tempfile.TemporaryDirectory() as temp:
            config, state_path, state, prompt, archive = self._prepare_retry(Path(temp))
            output = prompt.parent / "selection.codex.json"
            fresh = [{
                "标题": "发现文件丢失后短暂当机，最后改为先处理简单头像",
                "副标题": "文件丢失｜先画头像",
                "时间戳": [{"开始": "00:01:00", "结束": "00:01:40"}],
            }]

            def invoke_stub(_project, _label, command, **_kwargs):
                persisted = app.core.load_project(state_path)[1]["codex_retry_request"]
                self.assertTrue(persisted["invoked_at"])
                self.assertEqual(persisted["model"], "gpt-6-astra")
                self.assertEqual(persisted["reasoning_effort"], "xhigh")
                app.core.atomic_json(output, {"选片": fresh})

            with (
                patch.object(app.core, "build_selection_prompt", return_value=(prompt, None, None)),
                patch.object(app, "resolve_codex_command", return_value=["codex"]),
                patch.object(app.core, "run_external", side_effect=invoke_stub) as run,
            ):
                self.assertEqual(app.invoke_codex_selection(config, state_path, state), output)
                self.assertEqual(json.loads(output.read_text(encoding="utf-8")), fresh)
                reloaded = app.core.load_project(state_path)[1]
                self.assertEqual(app.invoke_codex_selection(config, state_path, reloaded), output)
                self.assertEqual(run.call_count, 1)
            self.assertTrue((archive / "selection.codex.json").is_file())
            self.assertFalse((prompt.parent / "selection-recovery.json").exists())

    def test_failed_explicit_retry_never_restores_old_archive_or_repeats_call(self):
        for failure in ("external", "gate"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                config, state_path, state, prompt, archive = self._prepare_retry(Path(temp))
                output = prompt.parent / "selection.codex.json"

                def invoke_stub(_project, _label, _command, **_kwargs):
                    if failure == "external":
                        raise RuntimeError("stub external failure")
                    app.core.atomic_json(output, {"选片": [{
                        "标题": "文件丢失后说 don't worry，决定先处理简单头像",
                        "副标题": "文件丢失｜先画头像",
                        "时间戳": [{"开始": "00:01:00", "结束": "00:01:40"}],
                    }]})

                with (
                    patch.object(app.core, "build_selection_prompt", return_value=(prompt, None, None)),
                    patch.object(app, "resolve_codex_command", return_value=["codex"]),
                    patch.object(app.core, "run_external", side_effect=invoke_stub) as run,
                ):
                    with self.assertRaises(app.SelectionNeedsReview):
                        app.invoke_codex_selection(config, state_path, state)
                    reloaded = app.core.load_project(state_path)[1]
                    with self.assertRaisesRegex(app.SelectionNeedsReview, "已发起过一次"):
                        app.invoke_codex_selection(config, state_path, reloaded)
                    self.assertEqual(run.call_count, 1)
                self.assertTrue((archive / "selection.codex.json").is_file())
                self.assertFalse((prompt.parent / "selection-recovery.json").exists())


class JobActionTests(unittest.TestCase):
    def test_failed_job_click_request_is_durable_and_requeues_without_losing_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {
                "_output_root": root / "projects",
                "_state_file": root / "queue.json",
            }
            state = app.empty_state()
            state["jobs"]["job-flow3"] = {
                "id": "job-flow3",
                "status": "failed",
                "attempts": 7,
                "progress_percent": 50,
                "current_stage": "执行失败",
                "detail": "flow3-cover-render 失败",
            }
            app.save_state(config, state)

            request = app.request_failed_job_retry(config, "job-flow3")

            self.assertTrue(request.is_file())
            self.assertTrue(app.retry_request_pending(config, "job-flow3"))
            loaded = app.load_state(config)
            applied = app.apply_retry_requests(config, loaded)
            self.assertEqual(applied, ["job-flow3"])
            self.assertFalse(request.exists())
            job = loaded["jobs"]["job-flow3"]
            self.assertEqual(job["status"], "queued")
            self.assertEqual(job["attempts"], 0)
            self.assertEqual(job["progress_percent"], 50)
            self.assertEqual(job["manual_error_retry_count"], 1)

    def test_failed_codex_job_cannot_bypass_explicit_retry_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {
                "_output_root": root / "projects",
                "_state_file": root / "queue.json",
            }
            state = app.empty_state()
            state["jobs"]["job-flow2"] = {
                "id": "job-flow2",
                "status": "failed",
                "progress_percent": 30,
                "current_stage": "流程2失败",
                "detail": "Codex 输出无效",
            }
            app.save_state(config, state)

            with self.assertRaisesRegex(app.AutomationError, "重新调用 Codex"):
                app.request_failed_job_retry(config, "job-flow2")

    def test_selected_cleanup_plan_supports_mixed_jobs_and_deduplicates_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            watch = root / "recordings"
            output = root / "projects"
            watch.mkdir()
            output.mkdir()
            shared = watch / "shared.flv"
            shared.write_bytes(b"shared")
            jobs = {}
            for job_id, status in (("job-1", "published"), ("job-2", "cleanup_failed")):
                project = output / job_id
                project.mkdir()
                job = {
                    "id": job_id,
                    "status": status,
                    "project": str(project),
                    "segments": [{"path": str(shared)}],
                }
                if status == "cleanup_failed":
                    job["cleanup"] = {"reason": "published"}
                jobs[job_id] = job
            jobs["queued"] = {
                "id": "queued", "status": "queued",
                "project": str(output / "queued"), "segments": [],
            }
            state = {"jobs": jobs}
            selected = [jobs["job-2"], jobs["job-1"], jobs["queued"]]
            self.assertEqual(app.cleanup_reason_for_job(jobs["job-1"]), "published")
            self.assertEqual(app.cleanup_reason_for_job(jobs["job-2"]), "published")
            self.assertEqual(app.cleanup_reason_for_job(jobs["queued"]), "discard")
            plan = app.cleanup_jobs_plan(
                {"_watch_root": watch, "_output_root": output}, selected
            )
            self.assertEqual(plan["job_count"], 3)
            self.assertEqual(plan["existing_count"], 3)
            self.assertEqual(
                app.cleanup_jobs_confirmation(selected),
                app.cleanup_jobs_confirmation(list(reversed(selected))),
            )
            args = app.build_parser().parse_args([
                "--config", str(root / "config.json"), "cleanup-jobs",
                "--job-id", "job-1", "--job-id", "job-2",
                "--confirm", "token",
            ])
            self.assertEqual(args.job_id, ["job-1", "job-2"])

    def test_selected_batch_continues_after_one_job_fails(self):
        state = {
            "jobs": {
                "first": {"id": "first", "status": "ready_to_publish"},
                "second": {"id": "second", "status": "ready_to_publish"},
            }
        }
        calls = []

        def run_one(_config, _state, job, **_kwargs):
            calls.append(job["id"])
            if job["id"] == "first":
                raise app.AutomationError("first failed")

        with patch.object(app, "run_selected_job", side_effect=run_one):
            report = app.run_selected_jobs(
                {}, state, ["first", "second"],
                allow_burn=True, allow_upload=True,
            )

        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(report["completed"], ["second"])
        self.assertEqual(len(report["failures"]), 1)
        self.assertIn("first failed", report["failures"][0])

    def test_mixed_selected_cleanup_preserves_published_and_discarded_results(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            watch = root / "recordings"
            output = root / "projects"
            watch.mkdir()
            output.mkdir()
            config = {
                "_watch_root": watch.resolve(),
                "_output_root": output.resolve(),
                "_state_file": output / "state.json",
            }
            state = app.empty_state()
            jobs = []
            for job_id, status in (("published", "published"), ("draft", "queued")):
                project = output / job_id
                project.mkdir()
                recording = watch / f"{job_id}.flv"
                recording.write_bytes(job_id.encode("utf-8"))
                job = {
                    "id": job_id,
                    "status": status,
                    "project": str(project),
                    "segments": [{"path": str(recording)}],
                }
                state["jobs"][job_id] = job
                jobs.append(job)
            app.save_state(config, state)

            report = app.cleanup_selected_jobs(
                config,
                state,
                ["published", "draft"],
                confirmation=app.cleanup_jobs_confirmation(jobs),
            )

            self.assertEqual(report["completed"], ["published", "draft"])
            self.assertEqual(report["failures"], [])
            self.assertEqual(state["jobs"]["published"]["status"], "cleaned_published")
            self.assertEqual(state["jobs"]["draft"]["status"], "cleaned_discarded")
            args = app.build_parser().parse_args([
                "--config", str(root / "config.json"), "run-jobs",
                "--job-id", "job-1", "--job-id", "job-2",
                "--allow-burn", "--allow-upload",
            ])
            self.assertEqual(args.job_id, ["job-1", "job-2"])
    def test_published_cleanup_deletes_only_manifest_owned_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            watch = root / "recordings"
            output = root / "projects"
            project = output / "job-1"
            watch.mkdir()
            project.mkdir(parents=True)
            recording = watch / "session.flv"
            danmaku = watch / "session.xml"
            unrelated = watch / "other.flv"
            recording.write_bytes(b"video")
            danmaku.write_text("<i />", encoding="utf-8")
            unrelated.write_bytes(b"keep")
            (project / "generated.mp4").write_bytes(b"clip")
            state_path = output / "workflow-auto-state.json"
            config = {
                "_watch_root": watch.resolve(),
                "_output_root": output.resolve(),
                "_state_file": state_path,
            }
            job = {
                "id": "job-1",
                "status": "published",
                "project": str(project),
                "segments": [
                    {"path": str(recording), "xml": str(danmaku)}
                ],
            }
            state = app.empty_state()
            state["jobs"]["job-1"] = job
            state["known"][str(recording.resolve())] = "signature"

            plan = app.cleanup_job_plan(config, job)
            self.assertEqual(plan["existing_count"], 3)
            with self.assertRaises(app.AutomationError):
                app.cleanup_job_files(
                    config,
                    state,
                    job,
                    reason="published",
                    confirmation="wrong",
                )
            self.assertTrue(recording.exists())

            audit = app.cleanup_job_files(
                config,
                state,
                job,
                reason="published",
                confirmation=app.cleanup_confirmation("job-1"),
            )
            self.assertEqual(len(audit["deleted"]), 3)
            self.assertFalse(recording.exists())
            self.assertFalse(danmaku.exists())
            self.assertFalse(project.exists())
            self.assertTrue(unrelated.exists())
            self.assertEqual(job["status"], "cleaned_published")
            self.assertNotIn(str(recording.resolve()), state["known"])

    def test_running_status_can_be_discarded_after_cleanup_owns_queue_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            watch = root / "recordings"
            output = root / "projects"
            project = output / "job-running"
            watch.mkdir()
            project.mkdir(parents=True)
            recording = watch / "running.flv"
            recording.write_bytes(b"video")
            config = {
                "_watch_root": watch.resolve(),
                "_output_root": output.resolve(),
                "_state_file": output / "workflow-auto-state.json",
            }
            job = {
                "id": "job-running",
                "status": "running",
                "project": str(project),
                "segments": [{"path": str(recording)}],
            }
            state = app.empty_state()
            state["jobs"][job["id"]] = job
            app.cleanup_job_files(
                config,
                state,
                job,
                reason="discard",
                confirmation=app.cleanup_confirmation(job["id"]),
            )
            self.assertEqual(job["status"], "cleaned_discarded")
            self.assertFalse(recording.exists())
            self.assertFalse(project.exists())
    def test_cleanup_rejects_project_equal_to_output_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            watch = root / "recordings"
            output = root / "projects"
            watch.mkdir()
            output.mkdir()
            config = {"_watch_root": watch, "_output_root": output}
            job = {"id": "bad", "project": str(output), "segments": []}
            with self.assertRaises(app.AutomationError):
                app.cleanup_job_plan(config, job)
            nested_project = watch / "generated-project"
            nested_project.mkdir()
            overlap_config = {"_watch_root": watch, "_output_root": watch}
            overlap_job = {
                "id": "overlap", "project": str(nested_project), "segments": []
            }
            with self.assertRaises(app.AutomationError):
                app.cleanup_job_plan(overlap_config, overlap_job)

    def test_manual_codex_retry_archives_old_json_and_keeps_flow1(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "projects" / "job-2"
            selection = project / "selection"
            selection.mkdir(parents=True)
            codex_output = selection / "selection.codex.json"
            canonical = selection / "selection.json"
            invalid = selection / "selection.codex.invalid-1.json"
            for path in (codex_output, canonical, invalid):
                path.write_text("[]", encoding="utf-8")
            project_state = {
                "schema_version": app.core.SCHEMA_VERSION,
                "config": {"creator": "sumire", "mode": "narrative"},
                "steps": {
                    "flow1": {"status": "completed"},
                    "flow2": {"status": "failed"},
                    "flow3": {"status": "failed"},
                },
                "approvals": {"editorial": {"digest": "old"}},
                "selection_file": str(canonical),
                "active_run_dir": str(project / "runs" / "old"),
            }
            app.core.atomic_json(project / app.core.PROJECT_FILENAME, project_state)
            queue_state_path = root / "queue.json"
            config = {"_state_file": queue_state_path}
            job = {
                "id": "job-2",
                "status": "awaiting_selection_review",
                "project": str(project),
                "progress_percent": 30,
                "detail": "旧输出未通过选片门禁",
            }
            state = app.empty_state()
            state["jobs"]["job-2"] = job
            state["codex_circuit"] = {"retry_at": time.time() + 1000}

            archive = app.prepare_codex_retry(config, state, job)
            self.assertIsNotNone(archive)
            self.assertFalse(codex_output.exists())
            self.assertFalse(canonical.exists())
            self.assertFalse(invalid.exists())
            self.assertTrue((archive / "selection.codex.json").is_file())
            _, saved_project = app.core.load_project(project)
            self.assertIn("flow1", saved_project["steps"])
            self.assertNotIn("flow2", saved_project["steps"])
            self.assertNotIn("flow3", saved_project["steps"])
            self.assertNotIn("selection_file", saved_project)
            self.assertEqual(saved_project["approvals"], {})
            self.assertNotIn("codex_circuit", state)
            self.assertEqual(job["status"], "queued")
            self.assertEqual(job["manual_codex_retry_count"], 1)
            self.assertTrue(saved_project["codex_retry_request"]["requested_at"])
            self.assertNotIn("invoked_at", saved_project["codex_retry_request"])

    def test_redo_from_each_flow_archives_only_affected_artifacts(self):
        flows = app.REDO_FLOW_ORDER
        progress = {
            "flow0": 0, "flow1": 10, "flow2": 25,
            "flow3": 45, "flow4": 65, "flow5": 82,
        }
        for from_flow in flows:
            with self.subTest(from_flow=from_flow), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                project = root / "projects" / "job-redo"
                continuity = project / "continuity"
                analysis = project / "analysis"
                selection = project / "selection"
                active_run = project / "runs" / "prepare-old"
                delivery_root = project / "deliveries" / "delivery-old"
                delivery = delivery_root / "files"
                for directory in (continuity, analysis, selection, active_run, delivery):
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / "old.txt").write_text("old", encoding="utf-8")
                review = active_run / "export" / "clips"
                review.mkdir(parents=True, exist_ok=True)
                (review / "review.txt").write_text("review", encoding="utf-8")
                selection_file = selection / "selection.json"
                selection_file.write_text("[]", encoding="utf-8")
                project_state = {
                    "schema_version": app.core.SCHEMA_VERSION,
                    "created_at": app.now_iso(),
                    "config": {"creator": "sumire", "mode": "narrative"},
                    "steps": {
                        "flow0": {"status": "completed"},
                        **{
                            name: {"status": "completed"}
                            for name in flows
                        },
                    },
                    "approvals": {
                        "editorial": {"digest": "editorial"},
                        "delivery": {"digest": "delivery"},
                        "publish": {"digest": "publish"},
                    },
                    "selection_file": str(selection_file),
                    "active_run_dir": str(active_run),
                    "active_review_dir": str(active_run / "export" / "clips"),
                    "active_burn_source_dir": str(active_run / "burn-source"),
                    "delivery_dir": str(delivery),
                    "flow0_manifest_digest": "old-flow0-digest",
                    "codex_retry_request": {"invoked_at": "previous-call"},
                    "delivery_digest": "delivery-digest",
                    "publish_preview_digest": "preview-digest",
                }
                app.core.atomic_json(
                    project / app.core.PROJECT_FILENAME, project_state
                )
                config = {"_state_file": root / "queue.json"}
                state = app.empty_state()
                job = {
                    "id": "job-redo",
                    "status": "awaiting_delivery_review",
                    "project": str(project),
                    "progress_percent": 90,
                }
                state["jobs"][job["id"]] = job

                archive = app.prepare_job_redo(
                    config, state, job, from_flow
                )
                flow_index = flows.index(from_flow)
                _, saved = app.core.load_project(project)

                self.assertEqual(
                    set(saved["steps"]),
                    set(flows[:flow_index]),
                )
                self.assertEqual(continuity.exists(), flow_index > 0)
                self.assertEqual(analysis.exists(), flow_index > 1)
                self.assertEqual(selection.exists(), flow_index > 2)
                self.assertEqual(active_run.exists(), flow_index > 3)
                self.assertEqual(delivery_root.exists(), flow_index > 4)
                self.assertEqual(
                    set(saved["approvals"]),
                    {"editorial", "delivery"} if flow_index == 5
                    else {"editorial"} if flow_index >= 3
                    else set(),
                )
                self.assertEqual("flow0_manifest_digest" in saved, flow_index > 0)
                self.assertEqual("codex_retry_request" in saved, flow_index > 2)
                self.assertEqual(job["status"], "queued")
                self.assertEqual(job["progress_percent"], progress[from_flow])
                self.assertTrue(archive.is_dir() or from_flow == "flow5")
                self.assertEqual(saved["redo_history"][-1]["from_flow"], from_flow)

    def test_published_job_can_redo_and_preserves_publication_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "projects" / "published-redo"
            selection = project / "selection"
            review = project / "runs" / "review" / "export" / "clips"
            delivery = project / "deliveries" / "delivery-1" / "files"
            for directory in (selection, review, delivery):
                directory.mkdir(parents=True, exist_ok=True)
            selection_file = selection / "selection.json"
            selection_file.write_text("[]", encoding="utf-8")
            (delivery / "publish-receipts.json").write_text(
                json.dumps(
                    {
                        "items": {
                            "001": {
                                "title": "旧稿",
                                "bvid": "BV1234567890",
                            }
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            project_state = {
                "schema_version": app.core.SCHEMA_VERSION,
                "created_at": app.now_iso(),
                "config": {"creator": "chilly", "mode": "narrative"},
                "steps": {
                    name: {
                        "status": "published" if name == "flow5" else "completed"
                    }
                    for name in app.REDO_FLOW_ORDER
                },
                "approvals": {"publish": {"digest": "old-publish"}},
                "selection_file": str(selection_file),
                "active_review_dir": str(review),
                "delivery_dir": str(delivery),
                "published_at": "2026-08-23T01:02:03-07:00",
            }
            app.core.atomic_json(
                project / app.core.PROJECT_FILENAME, project_state
            )
            config = {"_state_file": root / "queue.json"}
            state = app.empty_state()
            job = {
                "id": "published-redo",
                "status": "published",
                "creator": "chilly",
                "project": str(project),
                "progress_percent": 100,
            }
            state["jobs"][job["id"]] = job

            app.prepare_job_redo(config, state, job, "flow5")
            _, saved = app.core.load_project(project)

            self.assertEqual(job["status"], "queued")
            self.assertTrue(job["manual_republish_required"])
            self.assertEqual(
                job["publication_history"][-1]["bvids"], ["BV1234567890"]
            )
            self.assertNotIn("flow5", saved["steps"])
            self.assertNotIn("published_at", saved)
            self.assertEqual(
                saved["publication_history"][-1]["published_at"],
                "2026-08-23T01:02:03-07:00",
            )
            receipts = saved["publication_history"][-1]["publish_receipts"]
            self.assertEqual(receipts["items"]["001"]["bvid"], "BV1234567890")

    def test_redo_runner_burns_only_when_starting_at_flow4_or_flow5(self):
        state = app.empty_state()
        job = {"id": "redo", "status": "queued"}
        with patch.object(
            app, "prepare_job_redo", return_value=Path("archive")
        ), patch.object(app, "process_job") as process:
            app.redo_job_from_flow({}, state, job, "flow3")
            self.assertFalse(process.call_args.kwargs["allow_burn"])
            self.assertFalse(process.call_args.kwargs["allow_upload"])
            process.reset_mock()
            app.redo_job_from_flow({}, state, job, "flow4")
            self.assertTrue(process.call_args.kwargs["allow_burn"])
            self.assertFalse(process.call_args.kwargs["allow_upload"])

    def test_pause_resume_and_prioritize_queue_job(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {"_state_file": root / "queue.json"}
            state = app.empty_state()
            first = {
                "id": "first",
                "status": "queued",
                "creator": "sumire",
                "created_at": "2026-08-23T01:00:00-07:00",
            }
            target = {
                "id": "target",
                "status": "queued",
                "creator": "chilly",
                "created_at": "2026-08-23T02:00:00-07:00",
            }
            state["jobs"] = {"first": first, "target": target}

            app.prioritize_queue_job(config, state, target)
            self.assertEqual(
                app.runnable_jobs(state, allow_burn=False, allow_upload=False)[0]["id"],
                "target",
            )
            app.pause_queue_job(config, state, target)
            self.assertEqual(target["status"], "paused")
            self.assertEqual(
                [item["id"] for item in app.runnable_jobs(
                    state, allow_burn=False, allow_upload=False
                )],
                ["first"],
            )
            app.resume_queue_job(config, state, target)
            self.assertEqual(target["status"], "queued")
            self.assertEqual(
                app.runnable_jobs(state, allow_burn=False, allow_upload=False)[0]["id"],
                "target",
            )

    def test_published_revision_never_auto_republishes(self):
        job = {
            "id": "revision",
            "status": "ready_to_publish",
            "creator": "chilly",
            "manual_republish_required": True,
        }
        self.assertFalse(
            app.job_is_runnable(
                job, allow_burn=True, allow_upload=True, current_time=0
            )
        )
        job.pop("manual_republish_required")
        self.assertTrue(
            app.job_is_runnable(
                job, allow_burn=True, allow_upload=True, current_time=0
            )
        )

    def test_replace_published_job_burns_waiting_revision_before_replacing(self):
        job = {
            "id": "revision",
            "status": "awaiting_delivery_review",
            "manual_republish_required": True,
            "project": "D:/project",
        }
        state = app.empty_state()
        project_state = {"active_review_dir": "", "revision_clip_names": []}

        def finish_burn(_config, _state, target, **kwargs):
            self.assertTrue(kwargs["allow_burn"])
            self.assertFalse(kwargs["allow_upload"])
            target["status"] = "ready_to_publish"

        with (
            patch.object(app, "process_job", side_effect=finish_burn) as burn,
            patch.object(
                app.core,
                "load_project",
                return_value=(Path("D:/project/workflow-project.json"), project_state),
            ),
            patch.object(
                app.core, "step5_replace_preview", return_value={}
            ) as preview,
            patch.object(app.core, "step5_replace_upload") as upload,
            patch.object(app.core, "save_project"),
            patch.object(app, "_set_job_progress"),
        ):
            app.replace_published_job({}, state, job)

        burn.assert_called_once()
        preview.assert_called_once()
        upload.assert_called_once()
        self.assertEqual("published", job["status"])
        self.assertNotIn("manual_republish_required", job)

    def test_run_pending_queue_consumes_existing_jobs_without_scanning(self):
        state = app.empty_state()
        state["jobs"] = {
            "first": {
                "id": "first",
                "status": "queued",
                "creator": "chilly",
                "created_at": "2026-08-24T01:00:00-07:00",
            },
            "second": {
                "id": "second",
                "status": "queued",
                "creator": "awu",
                "created_at": "2026-08-24T02:00:00-07:00",
            },
        }

        def finish(_config, _state, job, **_kwargs):
            job["status"] = "awaiting_delivery_review"

        with (
            patch.object(app, "load_state", return_value=state),
            patch.object(app, "apply_retry_requests", return_value=[]),
            patch.object(app, "save_state"),
            patch.object(app, "scan_state") as scan,
            patch.object(app, "process_job", side_effect=finish) as process,
        ):
            _saved, processed = app.run_pending_queue(
                {"_state_file": Path("queue.json")},
                allow_burn=False,
                allow_upload=False,
            )

        self.assertEqual(["first", "second"], processed)
        self.assertEqual(2, process.call_count)
        scan.assert_not_called()

    def test_run_queue_cli_is_available_without_a_job_id(self):
        args = app.build_parser().parse_args(
            ["--config", "config.json", "run-queue"]
        )
        self.assertEqual("run-queue", args.command)
        self.assertFalse(args.allow_burn)
        self.assertFalse(args.allow_upload)

    def test_queue_control_cli_actions_are_available(self):
        parser = app.build_parser()
        for action in ("pause-job", "resume-job", "prioritize-job"):
            args = parser.parse_args(
                ["--config", "config.json", action, "--job-id", "job"]
            )
            self.assertEqual(args.command, action)
            self.assertEqual(args.job_id, "job")

    def test_redo_job_cli_accepts_all_flow_choices(self):
        parser = app.build_parser()
        for from_flow in app.REDO_FLOW_ORDER:
            args = parser.parse_args(
                [
                    "--config",
                    "config.json",
                    "redo-job",
                    "--job-id",
                    "job",
                    "--from-flow",
                    from_flow,
                ]
            )
            self.assertEqual(args.command, "redo-job")
            self.assertEqual(args.from_flow, from_flow)

class ReviewAndLockTests(unittest.TestCase):
    def test_gate_upgrade_rechecks_saved_invalid_selection_once(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            selection = project / "selection"
            selection.mkdir(parents=True)
            (selection / "selection.codex.invalid-1.json").write_text(
                "{}", encoding="utf-8"
            )
            job = {
                "status": "awaiting_selection_review",
                "creator": "yuchu",
                "project": str(project),
                "selection_gate_version": app.SELECTION_GATE_VERSION - 1,
            }
            self.assertTrue(
                app.job_is_runnable(
                    job, allow_burn=False, allow_upload=False, current_time=0
                )
            )
            job["selection_gate_version"] = app.SELECTION_GATE_VERSION
            self.assertFalse(
                app.job_is_runnable(
                    job, allow_burn=False, allow_upload=False, current_time=0
                )
            )

    def test_status_read_keeps_live_job_running_but_startup_can_recover_it(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "state.json"
            state = app.empty_state()
            state["jobs"]["job"] = {
                "status": "running",
                "resume_status": "failed",
                "detail": "正在转写",
            }
            app.core.atomic_json(state_path, state)
            config = {"_state_file": state_path}
            self.assertEqual(app.load_state(config)["jobs"]["job"]["status"], "running")
            recovered = app.load_state(config, recover_running=True)
            self.assertEqual(recovered["jobs"]["job"]["status"], "failed")

    def test_machine_publish_ignores_vad_review_flag_but_requires_qa_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / "run"
            clips = root / "clips"
            qa = run / "clip-local-asr" / "subtitle-vad-qa.csv"
            qa.parent.mkdir(parents=True)
            clips.mkdir()
            (clips / "001.mp4").write_bytes(b"video")
            (clips / "001.ass").write_text("ass", encoding="utf-8")
            fields = ["needs_review", "reviewed", "decision"]
            with qa.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow({"needs_review": "no", "reviewed": "not-required", "decision": ""})
            state = {
                "config": {"mode": "narrative"},
                "active_run_dir": str(run),
                "active_review_dir": str(clips),
            }
            app.unattended_review_ready(state)
            with qa.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow({"needs_review": "yes", "reviewed": "no", "decision": ""})
            app.unattended_review_ready(state)
            qa.unlink()
            with self.assertRaisesRegex(app.AutomationError, "缺少字幕 VAD QA"):
                app.unattended_review_ready(state)

    def test_imported_review_package_restores_flow3_without_reprocessing(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            clips = project / "runs" / "import-review" / "export" / "clips"
            clips.mkdir(parents=True)
            video = clips / "001-topic.mp4"
            video.write_bytes(b"video")
            video.with_suffix(".ass").write_text("[Events]\n", encoding="utf-8")
            video.with_name("001-topic-cover.jpg").write_bytes(b"cover")
            with (clips / "titles-and-covers.csv").open(
                "w", encoding="utf-8-sig", newline=""
            ) as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["clip_id", "video", "title"]
                )
                writer.writeheader()
                writer.writerow(
                    {"clip_id": "001", "video": video.name, "title": "topic"}
                )
            (clips / "review-decisions.json").write_text("{}", encoding="utf-8")
            transcript = project / "analysis" / "transcript"
            transcript.mkdir(parents=True)
            (transcript / "transcript.csv").write_text(
                "start_seconds,end_seconds,text\n0,1,test\n", encoding="utf-8"
            )
            selection = project / "selection" / "selection.json"
            selection.parent.mkdir(parents=True)
            selection.write_text("[]", encoding="utf-8")
            state = {
                "schema_version": app.core.SCHEMA_VERSION,
                "created_at": app.now_iso(),
                "steps": {"flow3": {"status": "failed"}},
                "approvals": {},
                "config": {"creator": "sumire", "mode": "narrative"},
                "selection_file": str(selection),
                "imported_review_package": {"clip_count": 1},
            }
            app.core.atomic_json(
                project / app.core.PROJECT_FILENAME, state
            )
            self.assertTrue(app.recover_imported_review_package(project, state))
            restored = app.core.load_project(project)[1]
            self.assertEqual(restored["steps"]["flow3"]["status"], "completed")
            self.assertEqual(Path(restored["active_review_dir"]), clips)

    def test_manual_activity_pauses_only_monitor_owned_automatic_work(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {
                "_output_root": root,
                "_state_file": root / "state.json",
                "_automatic_monitor_cycle": True,
            }
            marker = app.manual_activity_path(config)
            app.core.atomic_json(
                marker,
                {
                    "version": 1,
                    "pid": os.getpid(),
                    "flows": ["manual:flow3"],
                },
            )
            state = app.empty_state()
            job = {"id": "auto-yield", "status": "running"}
            state["jobs"][job["id"]] = job

            self.assertTrue(app.manual_work_active(config))
            self.assertTrue(
                app.defer_automatic_job_for_manual_work(config, state, job)
            )
            self.assertEqual(job["status"], "queued")
            self.assertIn("手动流程优先", job["current_stage"])

            config.pop("_automatic_monitor_cycle")
            job["status"] = "running"
            self.assertFalse(
                app.defer_automatic_job_for_manual_work(config, state, job)
            )
            self.assertEqual(job["status"], "running")

    def test_stale_manual_activity_is_removed_and_watcher_lock_is_independent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {
                "_output_root": root,
                "_state_file": root / "state.json",
            }
            marker = app.manual_activity_path(config)
            app.core.atomic_json(
                marker,
                {"version": 1, "pid": -1, "flows": ["manual:flow1"]},
            )
            self.assertFalse(app.manual_work_active(config))
            self.assertFalse(marker.exists())

            watcher = app.watcher_lock_target(config)
            with app.queue_lock(watcher):
                with app.queue_lock(config["_state_file"]):
                    self.assertNotEqual(watcher, config["_state_file"])

    def test_stale_lock_is_recovered(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state.json"
            lock = state.with_name(state.name + ".lock")
            lock.write_text("-1", encoding="ascii")
            with app.queue_lock(state):
                self.assertTrue(lock.is_file())
            self.assertFalse(lock.exists())

    def test_explicit_command_owns_independent_manual_priority_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {
                "_output_root": root,
                "_state_file": root / "state.json",
            }
            marker = app.manual_command_activity_path(config)
            with app.manual_priority_marker(
                config, enabled=True, action="redo-job"
            ):
                self.assertTrue(marker.exists())
                self.assertTrue(app.manual_work_active(config))
            self.assertFalse(marker.exists())
            self.assertFalse(app.manual_work_active(config))

    def test_manual_lock_waits_for_current_queue_writer(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state.json"
            entered = threading.Event()

            def current_writer():
                with app.queue_lock(state):
                    entered.set()
                    time.sleep(0.15)

            worker = threading.Thread(target=current_writer)
            worker.start()
            self.assertTrue(entered.wait(timeout=1.0))
            started = time.monotonic()
            with app.queue_lock(
                state, wait_seconds=1.0, poll_seconds=0.01
            ):
                self.assertGreaterEqual(time.monotonic() - started, 0.1)
            worker.join(timeout=1.0)
            self.assertFalse(worker.is_alive())

    def test_mixed_schema_and_unwrap_keep_two_public_groups(self):
        schema = app.selection_schema("mixed")
        self.assertEqual(set(schema["required"]), {"普通切片", "歌切"})
        item_schema = schema["properties"]["普通切片"]["items"]
        self.assertEqual(
            set(item_schema["required"]),
            {
                "标题", "副标题", "时间戳", "标签", "标签依据", "vr_topic",
                "多人类型", "参与者", "说话人依据", "嘉宾台词已核实",
            },
        )
        self.assertEqual(item_schema["properties"]["标签"]["minItems"], 5)
        payload = {"普通切片": [], "歌切": []}
        self.assertEqual(app.unwrap_codex_selection(payload, "mixed"), payload)
        self.assertTrue(app.selection_is_empty(payload, "mixed"))
        payload["歌切"].append(
            {
                "标题": "现场演唱",
                "副标题": "夏天的风",
                "时间戳": [{"开始": "00:00:00", "结束": "00:00:20"}],
            }
        )
        self.assertFalse(app.selection_is_empty(payload, "mixed"))

    def test_mixed_quality_gate_checks_narrative_but_allows_song_title_shape(self):
        payload = {
            "普通切片": [
                {
                    "标题": "枝堇认真立下保证后被自己下一句话当场拆穿",
                    "副标题": "认真保证｜当场拆穿",
                    "时间戳": [{"开始": "00:01:00", "结束": "00:01:30"}],
                }
            ],
            "歌切": [
                {
                    "标题": "夏天的风现场演唱",
                    "副标题": "夏天的风",
                    "时间戳": [{"开始": "00:05:00", "结束": "00:05:20"}],
                }
            ],
        }
        app.validate_selection_payload(
            payload, {"config": {"creator": "sumire", "mode": "mixed"}}
        )

    def test_selected_reviewed_job_can_burn_and_upload_in_one_authorized_run(self):
        state = app.empty_state()
        job = {"id": "reviewed", "status": "awaiting_delivery_review"}

        def complete(_config, _state, target, *, allow_burn, allow_upload):
            self.assertTrue(allow_burn)
            self.assertTrue(allow_upload)
            target["status"] = "published"

        with patch.object(app, "process_job", side_effect=complete) as process:
            app.run_selected_job(
                {}, state, job, allow_burn=True, allow_upload=True
            )
        process.assert_called_once()

        failed = {"id": "failed", "status": "failed"}
        with self.assertRaisesRegex(app.AutomationError, "当前状态不允许"):
            app.run_selected_job(
                {}, state, failed, allow_burn=True, allow_upload=True
            )

    def test_all_rejected_review_finishes_without_burn_or_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            review = root / "review"
            review.mkdir()
            video = review / "001-topic.mp4"
            video.write_bytes(b"video")
            app.core.review_workspace.set_decision(review, video.name, "skipped")
            config = {"_state_file": root / "queue.json"}
            state = app.empty_state()
            job = {
                "id": "all-rejected",
                "status": "awaiting_delivery_review",
                "progress_percent": 65,
            }
            state["jobs"][job["id"]] = job
            project_state = {"active_review_dir": str(review)}

            finished = app.finalize_empty_completed_review(
                config, state, job, project_state
            )

            self.assertTrue(finished)
            self.assertEqual(job["status"], "no_candidates")
            self.assertEqual(job["progress_percent"], 100)
            self.assertIn("没有通过稿", job["current_stage"])
            self.assertTrue(video.is_file(), "finishing review must not delete project files")

    def test_run_selected_accepts_all_rejected_terminal_outcome(self):
        state = app.empty_state()
        job = {"id": "all-rejected", "status": "awaiting_delivery_review"}

        def finish(_config, _state, target, **_kwargs):
            target["status"] = "no_candidates"

        with patch.object(app, "process_job", side_effect=finish):
            app.run_selected_job(
                {}, state, job, allow_burn=True, allow_upload=False
            )
        self.assertEqual(job["status"], "no_candidates")

    def test_flow4_failure_stays_failed_without_burn_authorization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {"_state_file": root / "queue.json"}
            state = app.empty_state()
            job = {
                "id": "audit-failed",
                "status": "running",
                "progress_percent": 72,
            }
            state["jobs"][job["id"]] = job
            project_state = {
                "steps": {
                    "flow4": {
                        "status": "failed",
                        "detail": "cue 27 has invalid media timing",
                    }
                }
            }

            held = app.preserve_flow4_failure(
                config, state, job, project_state, allow_burn=False
            )

            self.assertTrue(held)
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["current_stage"], "执行失败")
            self.assertIn("cue 27 has invalid media timing", job["detail"])
            self.assertFalse(
                app.preserve_flow4_failure(
                    config, state, job, project_state, allow_burn=True
                )
            )

    def test_human_reviewed_flow4_failure_can_be_explicitly_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            review = project / "review"
            review.mkdir(parents=True)
            video = review / "001-topic.mp4"
            video.write_bytes(b"video")
            app.core.review_workspace.set_decision(review, video.name, "approved")
            app.core.atomic_json(
                project / app.core.PROJECT_FILENAME,
                {
                    "schema_version": app.core.SCHEMA_VERSION,
                    "created_at": app.now_iso(),
                    "config": {},
                    "steps": {
                        "flow3": {"status": "completed"},
                        "flow4": {
                            "status": "failed",
                            "detail": "cue 27 has invalid media timing",
                        },
                    },
                    "approvals": {"delivery": {"source": "human"}},
                    "active_review_dir": str(review),
                },
            )
            job = {
                "id": "flow4-retry",
                "status": "failed",
                "project": str(project),
            }
            self.assertTrue(app.resumable_delivery_failure(job))

            def complete(_config, _state, target, *, allow_burn, allow_upload):
                self.assertTrue(allow_burn)
                self.assertTrue(allow_upload)
                target["status"] = "published"

            with patch.object(app, "process_job", side_effect=complete):
                app.run_selected_job(
                    {}, app.empty_state(), job, allow_burn=True, allow_upload=True
                )
            self.assertEqual(job["status"], "published")

    def test_delivery_uses_human_rules_only_after_all_decisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            review = Path(temporary) / "review"
            review.mkdir()
            video = review / "001-topic.mp4"
            video.write_bytes(b"video")
            video.with_suffix(".ass").write_text("[Events]\n", encoding="utf-8-sig")
            state = {"active_review_dir": str(review)}

            app.core.review_workspace.set_decision(
                review, video.name, "pending"
            )
            self.assertEqual(
                app.delivery_approval_source(state), "automated-qa"
            )

            app.core.review_workspace.set_decision(
                review, video.name, "approved"
            )
            self.assertEqual(app.delivery_approval_source(state), "human")
    def test_only_fully_reviewed_flow5_execute_failure_is_resumable(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            review = project / "review"
            delivery = project / "delivery"
            review.mkdir(parents=True)
            delivery.mkdir()
            video = review / "001-topic.mp4"
            video.write_bytes(b"video")
            app.core.review_workspace.set_decision(review, video.name, "approved")
            (delivery / "titles-and-covers.csv").write_text(
                "clip_id,video,title\n001,001-topic.mp4,topic\n", encoding="utf-8-sig"
            )
            app.core.atomic_json(
                project / app.core.PROJECT_FILENAME,
                {
                    "schema_version": app.core.SCHEMA_VERSION,
                    "created_at": app.now_iso(),
                    "config": {},
                    "steps": {"flow4": {"status": "completed"}},
                    "approvals": {},
                    "active_review_dir": str(review),
                    "delivery_dir": str(delivery),
                },
            )
            job = {
                "id": "publish-retry",
                "status": "failed",
                "project": str(project),
                "detail": "flow5-publish-execute 失败，退出码 4294967295",
            }
            self.assertTrue(app.resumable_publish_failure(job))

            def complete(_config, _state, target):
                target["status"] = "published"

            with patch.object(app, "retry_publish_job", side_effect=complete), patch.object(app, "process_job") as process:
                app.run_selected_job({}, app.empty_state(), job, allow_burn=True, allow_upload=True)
            process.assert_not_called()
            self.assertEqual(job["status"], "published")
    def test_unattended_publish_accepts_pending_vad_review_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "run"
            review = root / "review"
            qa_dir = run / "clip-local-asr"
            qa_dir.mkdir(parents=True)
            review.mkdir()
            video = review / "001-topic.mp4"
            video.write_bytes(b"video")
            video.with_suffix(".ass").write_text("[Events]\n", encoding="utf-8-sig")
            (review / "titles-and-covers.csv").write_text(
                "video,content_type\n001-topic.mp4,narrative\n",
                encoding="utf-8-sig",
            )
            (qa_dir / "subtitle-vad-qa.csv").write_text(
                "clip,needs_review,reviewed\n001-topic.mp4,yes,no\n",
                encoding="utf-8-sig",
            )
            app.core.review_workspace.load_decisions(review)
            app.unattended_review_ready(
                {
                    "active_run_dir": str(run),
                    "active_review_dir": str(review),
                    "config": {"mode": "narrative"},
                }
            )

class LiveAuthorizationTests(unittest.TestCase):
    def test_default_monitor_permissions_are_safe_and_persistable(self):
        config = app.default_config(Path("D:/workspace"))
        self.assertFalse(config["allow_burn"])
        self.assertFalse(config["allow_upload"])
        self.assertTrue(config["scheduled_delivery_enabled"])
        self.assertEqual(config["scheduled_delivery_time"], "17:00")
        config["allow_burn"] = True
        self.assertEqual(app.watch_permissions(config), (True, False))

    def test_scheduled_permissions_open_at_local_delivery_time(self):
        config = {
            "scheduled_delivery_enabled": True,
            "scheduled_delivery_time": "17:00",
        }
        self.assertEqual(
            app.scheduled_watch_permissions(
                config,
                allow_burn=True,
                allow_upload=True,
                current=datetime(2026, 8, 31, 16, 59),
            ),
            (False, False),
        )
        self.assertEqual(
            app.scheduled_watch_permissions(
                config,
                allow_burn=True,
                allow_upload=True,
                current=datetime(2026, 8, 31, 17, 0),
            ),
            (True, True),
        )

    def test_disabled_schedule_leaves_manual_monitor_permissions_unchanged(self):
        config = {
            "scheduled_delivery_enabled": False,
            "scheduled_delivery_time": "17:00",
        }
        self.assertEqual(
            app.scheduled_watch_permissions(
                config,
                allow_burn=True,
                allow_upload=False,
                current=datetime(2026, 8, 31, 9, 0),
            ),
            (True, False),
        )

    def test_schedule_time_validation_rejects_invalid_clock(self):
        self.assertEqual(app.normalize_daily_time("7:05"), "07:05")
        with self.assertRaisesRegex(app.AutomationError, "HH:MM"):
            app.normalize_daily_time("5pm")
        with self.assertRaisesRegex(app.AutomationError, "有效范围"):
            app.normalize_daily_time("24:00")

    def test_upload_permission_always_implies_burn(self):
        self.assertEqual(
            app.watch_permissions({"allow_burn": False, "allow_upload": True}),
            (True, True),
        )

    def test_live_config_can_revoke_permissions_without_restarting_monitor(self):
        enabled = {"allow_burn": True, "allow_upload": True}
        disabled = {"allow_burn": False, "allow_upload": False}
        self.assertEqual(app.watch_permissions(enabled), (True, True))
        self.assertEqual(app.watch_permissions(disabled), (False, False))
    def test_collaboration_summary_holds_automatic_upload_until_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            review = Path(temporary) / "review"
            review.mkdir()
            with (review / "titles-and-covers.csv").open(
                "w", encoding="utf-8-sig", newline=""
            ) as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "video", "collaboration_type", "participants",
                        "guest_dialogue_verified",
                    ],
                )
                writer.writeheader()
                writer.writerow({
                    "video": "001-multi.mp4",
                    "collaboration_type": "collaboration",
                    "participants": "主播,嘉宾",
                    "guest_dialogue_verified": "0",
                })
            summary = app.collaboration_review_summary(
                {"active_review_dir": str(review)}
            )
            self.assertTrue(summary["required"])
            self.assertEqual(summary["clips"], ["001-multi.mp4"])
            self.assertEqual(summary["unverified"], ["001-multi.mp4"])

            state = app.empty_state()
            job = {
                "id": "multi",
                "status": "ready_to_publish",
                "collaboration_review_required": True,
            }
            with self.assertRaisesRegex(app.AutomationError, "多人素材投稿需要明确确认"):
                app.run_selected_job(
                    {}, state, job, allow_burn=True, allow_upload=True
                )

            def complete(_config, _state, target, *, allow_burn, allow_upload):
                self.assertTrue(allow_burn)
                self.assertTrue(allow_upload)
                self.assertTrue(target["collaboration_upload_confirmed"])
                target["status"] = "published"

            with patch.object(app, "process_job", side_effect=complete):
                app.run_selected_job(
                    {}, state, job, allow_burn=True, allow_upload=True,
                    confirm_collaboration=True,
                )

    def test_collaboration_summary_ignores_skipped_multi_person_clip(self):
        with tempfile.TemporaryDirectory() as temporary:
            review = Path(temporary) / "review"
            review.mkdir()
            with (review / "titles-and-covers.csv").open(
                "w", encoding="utf-8-sig", newline=""
            ) as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "video", "collaboration_type", "participants",
                        "guest_dialogue_verified",
                    ],
                )
                writer.writeheader()
                writer.writerow({
                    "video": "001-multi.mp4",
                    "collaboration_type": "collaboration",
                    "participants": "主播,嘉宾",
                    "guest_dialogue_verified": "0",
                })
                writer.writerow({
                    "video": "002-single.mp4",
                    "collaboration_type": "single",
                    "participants": "主播",
                    "guest_dialogue_verified": "1",
                })
            (review / "review-decisions.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "clips": {
                            "001-multi.mp4": {"status": "skipped"},
                            "002-single.mp4": {"status": "approved"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            summary = app.collaboration_review_summary(
                {"active_review_dir": str(review)}
            )
            self.assertFalse(summary["required"])
            self.assertEqual(summary["clips"], [])
            self.assertEqual(summary["unverified"], [])

    def test_run_job_cli_has_explicit_collaboration_confirmation(self):
        args = app.build_parser().parse_args([
            "--config", "workflow-auto.json", "run-job", "--job-id", "multi",
            "--allow-upload", "--confirm-collaboration",
        ])
        self.assertTrue(args.confirm_collaboration)

    def test_live_control_inheritance_does_not_remove_review_job(self):
        target = {
            "id": "completed-session",
            "status": "queued",
            "live_activity_key": "room-day",
        }
        state = {
            "jobs": {
                "live-row": {
                    "id": "live-row",
                    "status": app.LIVE_JOB_STATUS,
                    "live_activity_key": "room-day",
                    "queue_priority": -5,
                },
                "review-job": {
                    "id": "review-job",
                    "status": "awaiting_delivery_review",
                    "live_activity_key": "room-day",
                    "queue_priority": 99,
                },
                target["id"]: target,
            }
        }
        app.inherit_live_queue_controls(state, "room-day", target)
        self.assertNotIn("live-row", state["jobs"])
        self.assertIn("review-job", state["jobs"])
        self.assertEqual(target["queue_priority"], -5)

class LiveQuickClipTests(unittest.TestCase):
    def _config(self, root: Path, watch: Path) -> dict:
        config = app.default_config(root)
        config.update(
            {
                "_watch_root": watch,
                "_output_root": root / "out",
                "_state_file": root / "state.json",
                "stable_seconds": 120,
                "session_quiet_seconds": 900,
                "session_gap_seconds": 300,
                "backfill_existing": True,
                "mode": "narrative",
            }
        )
        return config

    def test_active_recording_is_visible_but_not_runnable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            watch = root / "1727076670-枝堇Sumire"
            watch.mkdir()
            media = watch / "录制-1727076670-20260824-090000-000-直播中.flv"
            media.write_bytes(b"growing")
            clock = time.time()
            os.utime(media, (clock - 2, clock - 2))
            config = self._config(root, watch)
            state = app.empty_state()

            self.assertEqual(app.scan_state(config, state, clock=clock), [])

            self.assertEqual(len(state["jobs"]), 1)
            live = next(iter(state["jobs"].values()))
            self.assertEqual(live["status"], app.LIVE_JOB_STATUS)
            self.assertEqual(live["creator"], "sumire")
            self.assertFalse(
                app.job_is_runnable(
                    live, allow_burn=True, allow_upload=True, current_time=clock
                )
            )

    def test_live_pause_and_priority_transfer_to_completed_session(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            watch = root / "1727076670-枝堇Sumire"
            watch.mkdir()
            media = watch / "录制-1727076670-20260824-090000-000-直播中.flv"
            media.write_bytes(b"growing")
            clock = time.time()
            os.utime(media, (clock - 2, clock - 2))
            config = self._config(root, watch)
            state = app.empty_state()
            app.scan_state(config, state, clock=clock)
            live = next(iter(state["jobs"].values()))
            app.pause_queue_job(config, state, live)
            app.prioritize_queue_job(config, state, live)

            old = clock - 1000
            os.utime(media, (old, old))
            registered = app.scan_state(
                config, state, clock=clock, probe=lambda _path: 180.0
            )

            self.assertEqual(len(registered), 1)
            self.assertEqual(len(state["jobs"]), 1)
            completed = state["jobs"][registered[0]]
            self.assertEqual(completed["status"], "paused")
            self.assertEqual(completed["paused_from_status"], "queued")
            self.assertLess(completed["queue_priority"], 0)

    def test_quick_clip_cli_is_available(self):
        args = app.build_parser().parse_args(
            ["--config", "workflow-auto.json", "quick-clip", "--job-id", "live-1"]
        )
        self.assertEqual(args.command, "quick-clip")
        self.assertEqual(args.job_id, "live-1")

if __name__ == "__main__":
    unittest.main()
