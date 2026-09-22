import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import review_workspace
from review_workspace import (
    active_dialogue_text,
    discard_review_clip,
    bind_ass_media,
    find_dialogue_overlaps,
    force_single_line_ass,
    insert_dialogue,
    load_decisions,
    parse_ass_time,
    read_dialogues,
    selected_video_names,
    set_decision,
    sync_review_tags_to_delivery,
    update_dialogue,
    update_review_copy,
    review_copy_row,
    resolve_dialogue_overlaps,
)


ASS = """[Script Info]
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize
Style: Main,Arial,70

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.00,Main,主播,0,0,0,,hello world\\N下一行
"""


class ReviewWorkspaceTests(unittest.TestCase):
    def test_active_dialogue_text_uses_clip_time_without_boundary_overlap(self):
        rows = [
            {
                "start": "0:00:01.00",
                "end": "0:00:03.00",
                "text": "第一句",
            },
            {
                "start": "0:00:03.00",
                "end": "0:00:05.00",
                "text": "第二句",
            },
        ]
        self.assertEqual(active_dialogue_text(rows, 0.5), "")
        self.assertEqual(active_dialogue_text(rows, 1.5), "第一句")
        self.assertEqual(active_dialogue_text(rows, 3.0), "第二句")
        self.assertEqual(active_dialogue_text(rows, 5.0), "")

    def test_decisions_require_approval_and_skip_without_deleting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("001.mp4", "002.mp4"):
                (root / name).write_bytes(b"video")
            state = load_decisions(root)
            self.assertEqual(set(state["clips"]), {"001.mp4", "002.mp4"})
            with self.assertRaisesRegex(ValueError, "待审核"):
                selected_video_names(root, require_human_decisions=True)
            set_decision(root, "001.mp4", "approved")
            set_decision(root, "002.mp4", "skipped", "故事不完整")
            selected, state = selected_video_names(
                root, require_human_decisions=True
            )
            self.assertEqual(selected, ["001.mp4"])
            self.assertTrue((root / "002.mp4").is_file())
            self.assertEqual(state["clips"]["002.mp4"]["notes"], "故事不完整")

    def test_recorded_approval_can_restore_exact_previous_decision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "001.mp4"
            video.write_bytes(b"video")
            set_decision(root, video.name, "revise", "标题还没改完")
            approved = set_decision(
                root,
                video.name,
                "approved",
                "误点通过",
                record_undo=True,
            )
            entry = approved[review_workspace.DECISION_UNDO_HISTORY_KEY][-1]
            latest = review_workspace.latest_undoable_approval(root)
            self.assertEqual(latest["action_id"], entry["action_id"])

            result = review_workspace.undo_approval(
                root,
                video_name=video.name,
                action_id=entry["action_id"],
            )

            self.assertEqual(result["status"], "revise")
            restored = load_decisions(root, create=False)["clips"][video.name]
            self.assertEqual(restored["status"], "revise")
            self.assertEqual(restored["notes"], "标题还没改完")
            self.assertIsNone(review_workspace.latest_undoable_approval(root))

    def test_approval_undo_is_rejected_after_a_later_decision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "001.mp4"
            video.write_bytes(b"video")
            approved = set_decision(
                root, video.name, "approved", record_undo=True
            )
            action_id = approved[review_workspace.DECISION_UNDO_HISTORY_KEY][-1][
                "action_id"
            ]
            set_decision(root, video.name, "revise", "后续已经开始修改")

            with self.assertRaisesRegex(ValueError, "后续变化"):
                review_workspace.undo_approval(
                    root, video_name=video.name, action_id=action_id
                )

    def test_title_and_subtitle_update_preserves_publish_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "titles-and-covers.csv"
            fields = [
                "clip_id", "video", "title", "content_type",
                "cover_text_primary", "cover_text_secondary", "description", "tags",
            ]
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow({
                    "clip_id": "001", "video": "001.mp4", "title": "旧标题",
                    "content_type": "narrative", "cover_text_primary": "旧主",
                    "cover_text_secondary": "旧副", "description": "保留简介", "tags": "VirtuaReal,测试",
                })
            update_review_copy(
                root, "001.mp4", title="新标题",
                cover_text_primary="新主", cover_text_secondary="新副",
                tags="刘备文学,皇叔.得意", tag_evidence="20:24-21:40原文依据",
                description="单条投稿简介", collection="皇叔合集",
                season_id="123", section_id="456",
                collaboration_type="collaboration",
                participants="主播,嘉宾",
                participant_profile_keys="host,guest",
                speaker_evidence="01:10 两人互相回应",
                guest_dialogue_verified="1",
                cover_mode="evidence-reaction",
                cover_source_region="full",
                cover_time_seconds="12.500",
                reference_image="D:/covers/custom.png",
            )
            row = review_copy_row(root, "001.mp4")
            self.assertEqual(row["title"], "新标题")
            self.assertEqual(row["cover_text_primary"], "新主")
            self.assertEqual(row["cover_text_secondary"], "新副")
            self.assertEqual(row["description"], "单条投稿简介")
            self.assertEqual(row["collection"], "皇叔合集")
            self.assertEqual(row["season_id"], "123")
            self.assertEqual(row["section_id"], "456")
            self.assertEqual(row["tags"], "刘备文学,皇叔.得意")
            self.assertEqual(row["tag_evidence"], "20:24-21:40原文依据")
            self.assertEqual(row["collaboration_type"], "collaboration")
            self.assertEqual(row["participants"], "主播,嘉宾")
            self.assertEqual(row["participant_profile_keys"], "host,guest")
            self.assertEqual(row["guest_dialogue_verified"], "1")
            self.assertEqual(row["cover_mode"], "evidence-reaction")
            self.assertEqual(row["cover_source_region"], "full")
            self.assertEqual(row["cover_time_seconds"], "12.500")
            self.assertEqual(row["reference_image"], "D:/covers/custom.png")

    def test_embedded_vlc_player_remembers_and_applies_playback_rate(self):
        import ctypes

        class Lib:
            def __init__(self):
                self.calls = []

            def libvlc_media_player_set_rate(self, player, value):
                self.calls.append((player, value.value))
                return 0

        player = object.__new__(review_workspace.EmbeddedVlcPlayer)
        player.ctypes = ctypes
        player.lib = Lib()
        player.player = 123
        player.playback_rate = 1.0
        self.assertTrue(player.set_rate(1.5))
        self.assertEqual(player.playback_rate, 1.5)
        self.assertAlmostEqual(player.lib.calls[-1][1], 1.5)

    def test_streaming_review_uses_exact_clip_and_maps_source_clock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "review" / "001.mp4"
            source = root / "source.flv"
            video.parent.mkdir()
            video.write_bytes(b"exact")
            source.write_bytes(b"source")
            metadata_path = review_workspace.review_proxy_metadata_path(video)
            metadata_path.parent.mkdir()
            metadata_path.write_text(
                json.dumps(
                    {
                        "streaming": True,
                        "source": str(source),
                        "source_duration": 300.0,
                        "regions": [
                            {
                                "source_start": 100.0,
                                "source_end": 110.0,
                                "exact_start": 0.0,
                                "exact_end": 10.0,
                                "proxy_start": 0.0,
                                "proxy_end": 10.0,
                            },
                            {
                                "source_start": 200.0,
                                "source_end": 210.0,
                                "exact_start": 10.0,
                                "exact_end": 20.0,
                                "proxy_start": 10.0,
                                "proxy_end": 20.0,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(review_workspace.review_media_path(video), video.resolve())
            self.assertEqual(
                review_workspace.review_playback_media(video, 102.0, 108.0),
                video.resolve(),
            )
            self.assertEqual(
                review_workspace.review_playback_media(video, 90.0, 95.0),
                source.resolve(),
            )
            self.assertEqual(
                review_workspace.source_to_review_media_seconds(
                    video, video, 202.5
                ),
                12.5,
            )
            self.assertEqual(
                review_workspace.review_media_to_source_seconds(
                    video, video, 12.5
                ),
                202.5,
            )
            self.assertEqual(
                review_workspace.review_media_to_source_seconds(video, video, 10.0),
                200.0,
            )
            self.assertEqual(
                review_workspace.source_to_review_media_seconds(
                    video, source, 202.5
                ),
                202.5,
            )

    def test_nonstreaming_exact_clip_keeps_its_local_clock(self):
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "001.mp4"
            video.write_bytes(b"exact")
            self.assertEqual(
                review_workspace.source_to_review_media_seconds(
                    video, video, 12.5
                ),
                12.5,
            )

    def test_timeline_render_filter_trims_audio_and_video_to_same_duration(self):
        segments = [
            {"source_start": 10.0, "source_end": 12.5},
            {"source_start": 20.0, "source_end": 21.25},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "edited.mp4"

            def fake_run(command, **_kwargs):
                destination.write_bytes(b"ok")
                self.assertIn("-filter_complex", command)
                graph = command[command.index("-filter_complex") + 1]
                self.assertIn("trim=start=0:duration=2.500000", graph)
                self.assertIn("atrim=start=0:duration=2.500000", graph)
                self.assertIn("trim=start=0:duration=1.250000", graph)
                self.assertIn("atrim=start=0:duration=1.250000", graph)
                self.assertIn("aresample=async=1:first_pts=0", graph)
                self.assertIn("-avoid_negative_ts", command)

                class Result:
                    returncode = 0
                    stderr = b""

                return Result()

            with patch.object(review_workspace.subprocess, "run", fake_run):
                review_workspace.render_timeline_edit(
                    Path("source.mkv"), destination, segments, Path("ffmpeg")
                )
    def test_manual_tags_sync_to_existing_delivery_and_invalidate_preview(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            review = project / "runs" / "import-review" / "export" / "clips"
            delivery = project / "deliveries" / "delivery-1" / "files"
            review.mkdir(parents=True)
            delivery.mkdir(parents=True)
            fields = [
                "clip_id", "video", "title", "cover_text_primary",
                "cover_text_secondary", "tags", "tag_evidence",
            ]
            for folder, tags in ((review, "旧审核"), (delivery, "旧交付")):
                with (folder / "titles-and-covers.csv").open(
                    "w", encoding="utf-8-sig", newline=""
                ) as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    writer.writerow({
                        "clip_id": "001", "video": "001.mp4", "title": "标题",
                        "cover_text_primary": "主文案", "cover_text_secondary": "副文案",
                        "tags": tags, "tag_evidence": "旧依据",
                    })
            (project / "workflow-project.json").write_text(
                json.dumps({
                    "delivery_dir": str(delivery),
                    "publish_preview_digest": "stale",
                    "steps": {"flow5": {"status": "completed"}},
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            result = sync_review_tags_to_delivery(
                review, "001.mp4", tags="刘备文学,皇叔.得意",
                tag_evidence="20:24-21:40原文依据",
                description="交付简介", collection="交付合集",
                season_id="321", section_id="654",
            )
            self.assertEqual(result, delivery / "titles-and-covers.csv")
            row = review_copy_row(delivery, "001.mp4")
            self.assertEqual(row["tags"], "刘备文学,皇叔.得意")
            self.assertEqual(row["description"], "交付简介")
            self.assertEqual(row["collection"], "交付合集")
            self.assertEqual(row["season_id"], "321")
            self.assertEqual(row["section_id"], "654")
            state = json.loads((project / "workflow-project.json").read_text(encoding="utf-8"))
            self.assertNotIn("publish_preview_digest", state)
            self.assertEqual(state["steps"]["flow5"]["status"], "pending")

    def test_insert_dialogue_uses_existing_style_and_chronological_position(self):
        with tempfile.TemporaryDirectory() as temporary:
            ass = Path(temporary) / "001.ass"
            value = ASS.replace(
                "Dialogue: 0,0:00:01.00,0:00:02.00,Main,主播,0,0,0,,hello world\\N下一行",
                "Dialogue: 0,0:00:03.00,0:00:04.00,Main,主播,0,0,0,,后一句",
            )
            ass.write_text(value, encoding="utf-8-sig")
            line_number = insert_dialogue(
                ass, start_seconds=1.0, end_seconds=2.5, text="新增 一行"
            )
            rows = read_dialogues(ass)
            self.assertEqual([row["text"] for row in rows], ["新增 一行", "后一句"])
            self.assertEqual(rows[0]["style"], "Main")
            self.assertEqual(rows[0]["name"], "主播")
            self.assertEqual(rows[0]["line_number"], line_number)

    def test_delete_dialogue_removes_exact_row_and_returns_nearest(self):
        with tempfile.TemporaryDirectory() as temporary:
            ass = Path(temporary) / "delete.ass"
            value = ASS.replace(
                "Dialogue: 0,0:00:01.00,0:00:02.00,Main,主播,0,0,0,,hello world\\N下一行",
                "\n".join(
                    [
                        "Dialogue: 0,0:00:01.00,0:00:02.00,Main,主播,0,0,0,,第一句",
                        "Dialogue: 0,0:00:03.00,0:00:04.00,Main,主播,0,0,0,,第二句",
                    ]
                ),
            )
            ass.write_text(value, encoding="utf-8-sig")
            first = read_dialogues(ass)[0]
            preferred = review_workspace.delete_dialogue(ass, first["line_number"])
            rows = read_dialogues(ass)
            self.assertEqual([row["text"] for row in rows], ["第二句"])
            self.assertEqual(preferred, rows[0]["line_number"])

    def test_existing_ass_fuzzy_paid_message_is_retroactively_normalized(self):
        with tempfile.TemporaryDirectory() as temporary:
            ass = Path(temporary) / "old-sc.ass"
            ass.write_text(
                ASS.replace("hello world\\N下一行", "谢谢老板的刚镚，正文保留"),
                encoding="utf-8-sig",
            )
            self.assertEqual(review_workspace.normalize_paid_messages_ass(ass), 1)
            self.assertEqual(read_dialogues(ass)[0]["text"], "（谢谢SC），正文保留")
    def test_existing_ass_uses_active_correction_dictionary(self):
        with tempfile.TemporaryDirectory() as temporary:
            ass = Path(temporary) / "old-asr.ass"
            ass.write_text(
                ASS.replace(
                    "Dialogue: 0,0:00:01.00,0:00:02.00,Main,主播,0,0,0,,hello world\\N下一行",
                    "\n".join([
                        "Dialogue: 0,0:00:01.00,0:00:02.00,Main,主播,0,0,0,,波罗芬和小粉元提到KPR",
                        "Dialogue: 0,0:00:03.00,0:00:04.00,Main,嘉宾,0,0,0,,波罗芬也被说成小粉元",
                    ]),
                ),
                encoding="utf-8-sig",
            )
            self.assertEqual(review_workspace.normalize_paid_messages_ass(ass), 2)
            self.assertEqual(
                [row["text"] for row in read_dialogues(ass)],
                ["布洛芬和小粉螈提到KPL", "布洛芬也被说成小粉螈"],
            )

    def test_subtitle_time_snaps_to_red_playhead_by_pixel_distance(self):
        value, snapped = review_workspace.snap_subtitle_time(10.08, 10.0, 100.0)
        self.assertTrue(snapped)
        self.assertEqual(value, 10.0)
        value, snapped = review_workspace.snap_subtitle_time(10.2, 10.0, 100.0)
        self.assertFalse(snapped)
        self.assertEqual(value, 10.2)

    def test_source_gap_maps_to_edited_boundary_without_resetting_to_zero(self):
        segments = [
            {"source_start": 10.0, "source_end": 20.0},
            {"source_start": 30.0, "source_end": 40.0},
        ]
        edited, index = review_workspace.source_to_edited_nearest(segments, 25.0)
        self.assertEqual((edited, index), (10.0, 0))
        edited, index = review_workspace.source_to_edited_nearest(segments, 45.0)
        self.assertEqual((edited, index), (20.0, 1))
    def test_single_line_editor_removes_explicit_breaks(self):
        with tempfile.TemporaryDirectory() as temporary:
            ass = Path(temporary) / "001.ass"
            ass.write_text(ASS, encoding="utf-8-sig")
            self.assertEqual(force_single_line_ass(ass), 2)
            value = ass.read_text(encoding="utf-8-sig")
            self.assertIn("WrapStyle: 2", value)
            self.assertIn("hello world 下一行", value)
            self.assertNotIn(r"\N", value)
            row = read_dialogues(ass)[0]
            update_dialogue(
                ass,
                row["line_number"],
                start="0:00:01.20",
                end="0:00:02.40",
                text="NFY cannot break",
            )
            updated = read_dialogues(ass)[0]
            self.assertEqual(updated["start"], "0:00:01.20")
            self.assertEqual(updated["end"], "0:00:02.40")
            self.assertEqual(updated["text"], "NFY cannot break")

    def test_overlap_repair_shortens_previous_cue_and_keeps_both_texts(self):
        with tempfile.TemporaryDirectory() as temporary:
            ass = Path(temporary) / "overlap.ass"
            value = ASS.replace(
                "Dialogue: 0,0:00:01.00,0:00:02.00,Main,主播,0,0,0,,hello world\\N下一行",
                "\n".join(
                    [
                        "Dialogue: 0,0:00:01.00,0:00:03.00,Main,主播,0,0,0,,前一句",
                        "Dialogue: 0,0:00:02.50,0:00:04.00,Main,主播,0,0,0,,后一句",
                    ]
                ),
            )
            ass.write_text(value, encoding="utf-8-sig")
            self.assertEqual(len(find_dialogue_overlaps(ass)), 1)
            self.assertEqual(resolve_dialogue_overlaps(ass), 1)
            self.assertEqual(find_dialogue_overlaps(ass), [])
            rows = read_dialogues(ass)
            self.assertEqual([row["text"] for row in rows], ["前一句", "后一句"])
            self.assertLessEqual(
                parse_ass_time(rows[0]["end"]),
                parse_ass_time(rows[1]["start"]),
            )

    def test_overlap_repair_merges_duplicate_cues(self):
        with tempfile.TemporaryDirectory() as temporary:
            ass = Path(temporary) / "duplicate.ass"
            value = ASS.replace(
                "Dialogue: 0,0:00:01.00,0:00:02.00,Main,主播,0,0,0,,hello world\\N下一行",
                "\n".join(
                    [
                        "Dialogue: 0,0:00:01.00,0:00:03.00,Main,主播,0,0,0,,同一句",
                        "Dialogue: 0,0:00:02.00,0:00:04.00,Main,主播,0,0,0,,同一句",
                    ]
                ),
            )
            ass.write_text(value, encoding="utf-8-sig")
            self.assertEqual(resolve_dialogue_overlaps(ass), 1)
            rows = read_dialogues(ass)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["end"], "0:00:04.00")

    def test_aegisub_media_binding_uses_absolute_review_video(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ass = root / "clip.ass"
            video = root / "clip.mp4"
            ass.write_text(ASS, encoding="utf-8-sig")
            video.write_bytes(b"video")
            bind_ass_media(ass, video)
            value = ass.read_text(encoding="utf-8-sig")
            self.assertIn(f"Audio File: {video.resolve()}", value)
            self.assertIn(f"Video File: {video.resolve()}", value)
    def test_discard_review_clip_removes_only_exact_generated_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.flv"
            source.write_bytes(b"source")
            review = root / "review"
            review.mkdir()
            video = review / "001-topic.mp4"
            other = review / "002-keep.mp4"
            for path in (video, other):
                path.write_bytes(b"video")
            video.with_suffix(".ass").write_text("ass", encoding="utf-8")
            video.with_name(f"{video.stem}-cover.jpg").write_bytes(b"cover")
            proxy = review_workspace.review_proxy_video_path(video)
            proxy.parent.mkdir()
            proxy.write_bytes(b"proxy")
            review_workspace.review_proxy_ass_path(video).write_text("ass", encoding="utf-8")
            review_workspace.review_proxy_metadata_path(video).write_text("{}", encoding="utf-8")
            with (review / "titles-and-covers.csv").open(
                "w", encoding="utf-8-sig", newline=""
            ) as handle:
                writer = csv.DictWriter(handle, fieldnames=["clip_id", "video", "title"])
                writer.writeheader()
                writer.writerow({"clip_id": "001", "video": video.name, "title": "删除"})
                writer.writerow({"clip_id": "002", "video": other.name, "title": "保留"})

            def remove_file(path: Path) -> None:
                path.unlink()

            with patch.object(review_workspace, "_trash_path", side_effect=remove_file):
                removed = discard_review_clip(review, video.name)

            self.assertGreaterEqual(len(removed), 6)
            self.assertFalse(video.exists())
            self.assertFalse(video.with_suffix(".ass").exists())
            self.assertFalse(video.with_name(f"{video.stem}-cover.jpg").exists())
            self.assertTrue(other.is_file())
            self.assertTrue(source.is_file())
            with (review / "titles-and-covers.csv").open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["video"] for row in rows], [other.name])

    def test_waveform_disk_cache_avoids_redecoding_unchanged_media(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "clip.mp4"
            video.write_bytes(b"video-v1")
            cache = root / "cache"
            with patch.object(
                review_workspace,
                "waveform_peaks",
                return_value={"duration": 4.0, "peaks": [0.25, 0.75]},
            ) as decode:
                first = review_workspace.cached_waveform_peaks(
                    video, Path("ffmpeg"), cache_dir=cache, columns=2
                )
                second = review_workspace.cached_waveform_peaks(
                    video, Path("ffmpeg"), cache_dir=cache, columns=2
                )
                video.write_bytes(b"video-version-2")
                third = review_workspace.cached_waveform_peaks(
                    video, Path("ffmpeg"), cache_dir=cache, columns=2
                )
            self.assertEqual(first, second)
            self.assertEqual(first, third)
            self.assertEqual(decode.call_count, 2)

    def test_pending_clips_are_publishable_when_human_review_is_optional(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "001.mp4").write_bytes(b"video")
            load_decisions(root)
            selected, state = selected_video_names(
                root, require_human_decisions=False
            )
            self.assertEqual(["001.mp4"], selected)
            self.assertEqual("pending", state["clips"]["001.mp4"]["status"])
    def test_approved_revision_marks_revise_and_clears_after_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "001.mp4"
            video.write_bytes(b"video")
            load_decisions(root)
            set_decision(root, video.name, "approved", "ok")

            started = review_workspace.begin_approved_revision(
                root,
                video.name,
                action="封面、Tag",
                job_id="job-1",
                published=True,
            )
            row = started["clips"][video.name]
            self.assertEqual("revise", row["status"])
            self.assertTrue(row["replacement_requested"])
            self.assertEqual("封面、Tag", row["replacement_action"])
            self.assertEqual("job-1", row["replacement_job_id"])

            set_decision(root, video.name, "approved", "revised")
            completed = review_workspace.complete_approved_revisions(
                root, [video.name]
            )
            row = completed["clips"][video.name]
            self.assertEqual("approved", row["status"])
            self.assertFalse(row["replacement_requested"])
            self.assertTrue(row["replacement_completed_at"])

    def test_speaker_style_and_dialogue_name_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            ass = Path(temporary) / "multi.ass"
            ass.write_text(
                "[Script Info]\n[V4+ Styles]\n"
                "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
                "Style: Main,Arial,70,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,2,2,10,10,10,1\n"
                "[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:01.00,0:00:02.00,Main,主播,0,0,0,,第一句\n",
                encoding="utf-8-sig",
            )
            style = review_workspace.ensure_speaker_style(
                ass,
                "guest_one",
                {
                    "display_name": "嘉宾一",
                    "dialogue_font": "SimSun",
                    "dialogue_font_size": 76,
                    "role_color": "#F9A699",
                    "subtitle_fill_color": "#FF5AA5",
                    "subtitle_outline_color": "#111318",
                    "subtitle_outline_width": 5.0,
                },
                base_style="Main",
            )
            row = read_dialogues(ass)[0]
            update_dialogue(
                ass,
                row["line_number"],
                start=row["start"],
                end=row["end"],
                text=row["text"],
                style=style,
                name="嘉宾一",
            )
            value = ass.read_text(encoding="utf-8-sig")
            self.assertIn("Style: Speaker_guest_one,SimSun,76", value)
            self.assertIn("&H00A55AFF", value)
            self.assertIn("&H00181311", value)
            updated = read_dialogues(ass)[0]
            self.assertEqual(updated["style"], "Speaker_guest_one")
            self.assertEqual(updated["name"], "嘉宾一")

    def test_refresh_ass_palette_updates_host_and_guest_styles(self):
        with tempfile.TemporaryDirectory() as temporary:
            ass = Path(temporary) / "palette.ass"
            ass.write_text(
                "[V4+ Styles]\n"
                "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
                "Style: Regular,Arial,70,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,2,2,10,10,10,1\n"
                "Style: Speaker_guest,Arial,70,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,2,2,10,10,10,1\n"
                "[Events]\n"
                "Dialogue: 0,0:00:01.00,0:00:02.00,Regular,主播,0,0,0,,第一句\n"
                "Dialogue: 0,0:00:02.00,0:00:03.00,Speaker_guest,嘉宾,0,0,0,,第二句\n",
                encoding="utf-8-sig",
            )
            profiles = {
                "host": {
                    "display_name": "主播",
                    "dialogue_font": "SimSun",
                    "dialogue_font_size": 85,
                    "subtitle_fill_color": "#37C8F3",
                    "subtitle_outline_color": "#111318",
                    "subtitle_outline_width": 5.0,
                },
                "guest": {
                    "display_name": "嘉宾",
                    "dialogue_font": "Microsoft YaHei",
                    "dialogue_font_size": 80,
                    "subtitle_fill_color": "#FF5AA5",
                    "subtitle_outline_color": "#111318",
                    "subtitle_outline_width": 5.0,
                },
            }
            changed = review_workspace.refresh_ass_subtitle_palette(
                ass, "host", profiles
            )
            value = ass.read_text(encoding="utf-8-sig")
            self.assertEqual(changed, 2)
            self.assertIn("Style: Regular,SimSun,85,&H00F3C837", value)
            self.assertIn("Style: Speaker_guest,Microsoft YaHei,80,&H00A55AFF", value)
            self.assertEqual(value.count("&H00181311"), 2)
if __name__ == "__main__":
    unittest.main()
