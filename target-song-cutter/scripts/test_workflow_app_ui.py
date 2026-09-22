from __future__ import annotations

from collections import deque
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import workflow_app


class WorkflowAppUiTests(unittest.TestCase):
    def test_copy_prompt_only_copies_fresh_output_after_generation_succeeds(self):
        cases = ("success", "failed", "start_error", "missing_output", "save_failed")
        for outcome in cases:
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                project = root / "project"
                path = project / "selection" / "selection-prompt.md"
                path.parent.mkdir(parents=True)
                path.write_text("old prompt", encoding="utf-8")
                bench = object.__new__(workflow_app.Workbench)
                bench.project_dir = Mock(return_value=str(project))
                bench.save_project = Mock(return_value=outcome != "save_failed")
                bench.core_command = lambda args: ["python", "workflow_app_core.py", *args]
                bench.command_counter = 0
                bench.active_commands = {}
                bench.pending_commands = deque()
                bench.task_processes = {}
                bench.vars = {"status": Mock()}
                bench.clipboard_clear = Mock()
                bench.clipboard_append = Mock()
                bench.append_log = Mock()
                bench.refresh_status = Mock()
                bench._sync_manual_activity_marker = Mock()
                bench._launch_ready_commands = Mock()
                bench._launch_command = lambda record: bench.active_commands.update(
                    {record["flow_key"]: record}
                )

                bench.copy_prompt()

                bench.clipboard_clear.assert_not_called()
                bench.clipboard_append.assert_not_called()
                if outcome == "save_failed":
                    self.assertFalse(bench.active_commands)
                    continue
                record = bench.active_commands["manual:flow2"]
                self.assertEqual(
                    record["command"][-3:], ["prompt", "--project", str(project)]
                )
                # A project switch while generation runs must not change the source.
                bench.project_dir.return_value = str(root / "another-project")
                if outcome == "success":
                    path.write_text("fresh prompt with current subtitle rules", encoding="utf-8")
                elif outcome == "missing_output":
                    path.unlink()
                with patch.object(workflow_app.messagebox, "showerror") as showerror:
                    bench._finish_command(
                        record["id"],
                        code=1 if outcome == "failed" else 0,
                        error="cannot start" if outcome == "start_error" else "",
                    )
                if outcome == "success":
                    bench.clipboard_clear.assert_called_once_with()
                    bench.clipboard_append.assert_called_once_with(
                        "fresh prompt with current subtitle rules"
                    )
                else:
                    bench.clipboard_clear.assert_not_called()
                    bench.clipboard_append.assert_not_called()
                if outcome == "missing_output":
                    showerror.assert_called_once()
                if outcome == "start_error":
                    showerror.assert_not_called()

    def test_flow2_description_shows_saved_model_without_claiming_ultra_for_other_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bench = object.__new__(workflow_app.Workbench)
            with patch.object(workflow_app.core, "WORKSPACE_ROOT", root):
                self.assertIn("GPT 6 / xhigh", bench.flow2_prompt_description())
                config = workflow_app.auto.default_config(root)
                config["codex_reasoning_effort"] = "high"
                workflow_app.auto.save_config(root / "workflow-auto.json", config)
                description = bench.flow2_prompt_description()
                self.assertIn("gpt-6-astra / high", description)
                self.assertNotIn("GPT 6 Ultra", description)
                (root / "workflow-auto.json").write_text("{invalid", encoding="utf-8")
                description = bench.flow2_prompt_description()
                self.assertIn("配置读取失败", description)
                self.assertNotIn("GPT 6 Ultra", description)

    def test_save_auto_config_preserves_explicit_codex_settings(self):
        cases = (
            (None, ("gpt-6-astra", "xhigh")),
            (("gpt-6-astra", "high"), ("gpt-6-astra", "high")),
            (("gpt-5.6-sol", "xhigh"), ("gpt-5.6-sol", "xhigh")),
        )
        for saved_model, expected in cases:
            with self.subTest(saved_model=saved_model), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                config_path = root / "workflow-auto.json"
                if saved_model:
                    config = workflow_app.auto.default_config(root)
                    config["codex_model"], config["codex_reasoning_effort"] = saved_model
                    config["stable_seconds"] = 1
                    workflow_app.auto.save_config(config_path, config)
                bench = object.__new__(workflow_app.Workbench)
                values = {
                    "stable_seconds": "120", "quiet_seconds": "900",
                    "watch_root": str(root / "recordings"),
                    "auto_output": str(root / "queue"),
                    "scheduled_delivery_time": "17:00", "codex_command": "",
                    "mode": next(label for label, mode in workflow_app.MODE_LABELS.items() if mode == "narrative"),
                    "model": "local:qwen3-asr-auto", "device": "cuda",
                    "compute_type": "float16",
                }
                bench.vars = {key: Mock(get=Mock(return_value=value)) for key, value in values.items()}
                bench.auto_backfill = Mock(get=Mock(return_value=False))
                bench.auto_burn = Mock(get=Mock(return_value=False))
                bench.auto_upload = Mock(get=Mock(return_value=False))
                bench.scheduled_delivery_enabled = Mock(get=Mock(return_value=True))
                with patch.object(workflow_app.core, "WORKSPACE_ROOT", root):
                    bench.save_auto_config()
                stored = workflow_app.auto.load_config(config_path)
                self.assertEqual((stored["codex_model"], stored["codex_reasoning_effort"]), expected)
                self.assertEqual(stored["stable_seconds"], 120)
                self.assertEqual(stored["_watch_root"], root / "recordings")

    def test_auto_job_drag_selection_replaces_adds_and_toggles_ranges(self):
        rows = ("a", "b", "c", "d", "e")
        self.assertEqual(
            workflow_app.auto_job_drag_selection(rows, "b", "d"),
            ("b", "c", "d"),
        )
        self.assertEqual(
            workflow_app.auto_job_drag_selection(
                rows, "b", "d", base_selection={"a", "e"}, mode="add"
            ),
            rows,
        )
        self.assertEqual(
            workflow_app.auto_job_drag_selection(
                rows, "b", "d", base_selection={"a", "c", "e"}, mode="toggle"
            ),
            ("a", "b", "d", "e"),
        )

    def test_selected_auto_job_ids_follow_table_order_and_deduplicate_detail_rows(self):
        class Tree:
            def selection(self):
                return ("job-2::detail", "job-1", "job-2")

            def get_children(self):
                return ("job-1", "job-1::detail", "job-2", "job-2::detail")

        bench = object.__new__(workflow_app.Workbench)
        bench.auto_job_tree = Tree()
        bench.auto_job_row_to_job = {
            "job-1": "job-1",
            "job-1::detail": "job-1",
            "job-2": "job-2",
            "job-2::detail": "job-2",
        }

        self.assertEqual(bench.selected_auto_job_ids(), ["job-1", "job-2"])

    def test_cut_uses_source_clock_for_exact_clip_and_full_recording(self):
        regions = [
            {"source_start": 100.0, "source_end": 110.0,
             "exact_start": 0.0, "exact_end": 10.0},
            {"source_start": 200.0, "source_end": 210.0,
             "exact_start": 10.0, "exact_end": 20.0},
        ]
        cases = [
            ("exact", 2.5, None, None, 102.5, 0),
            ("exact", 12.5, None, None, 202.5, 1),
            ("source", 202.5, None, None, 202.5, 1),
            # VLC can briefly report the old time after a paused seek.
            ("exact", 0.0, 105.0, None, 105.0, 0),
            ("exact", 0.0, None, 205.0, 205.0, 1),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "clip.mp4"
            source = Path(temporary) / "source.flv"
            metadata = workflow_app.review_workspace.review_proxy_metadata_path(video)
            metadata.parent.mkdir(parents=True)
            metadata.write_text(json.dumps({
                "streaming": True, "source": str(source), "regions": regions,
            }), encoding="utf-8")
            for media, reported, jump, starting, expected, index in cases:
                with self.subTest(media=media, reported=reported, jump=jump, starting=starting):
                    bench = object.__new__(workflow_app.Workbench)
                    bench.timeline_rendering = False
                    bench.current_review_video = video
                    bench.current_review_media = video if media == "exact" else source
                    bench.preview_player = Mock()
                    bench.preview_player.current_seconds.return_value = reported
                    bench._review_jump_source = jump
                    bench._review_jump_until = float("inf")
                    bench._review_starting_source = starting
                    bench._review_starting_until = float("inf")
                    segments = [
                        {"source_start": r["source_start"], "source_end": r["source_end"]}
                        for r in regions
                    ]
                    bench.timeline_state = {"segments": segments}
                    bench.timeline_segments = lambda: bench.timeline_state["segments"]
                    bench.timeline_undo = []
                    bench.push_timeline_undo = Mock()
                    bench.persist_timeline_state = Mock()
                    bench.draw_review_waveform = Mock()
                    bench.vars = {"review_line_status": Mock()}

                    bench.cut_timeline_at_playhead()

                    result = bench.timeline_state["segments"]
                    self.assertEqual(len(result), 3, bench.vars["review_line_status"].set.call_args)
                    self.assertEqual(result[index]["source_end"], expected)
                    self.assertEqual(result[index + 1]["source_start"], expected)
                    self.assertEqual(bench.timeline_selected_segment, index + 1)
                    self.assertEqual(
                        workflow_app.review_workspace.timeline_duration(result), 20.0
                    )
                    bench.persist_timeline_state.assert_called_once_with()

    def test_terminal_opens_separately_and_mirrors_log_without_resizing_editor(self):
        bench = object.__new__(workflow_app.Workbench)
        bench.log = Mock(get=Mock(return_value="existing log"))
        bench.console_toggle_button = Mock()
        with patch.object(workflow_app, "Toplevel") as popup_factory, patch.object(workflow_app, "ScrolledText") as viewer_factory:
            bench.toggle_console_log()
            viewer = viewer_factory.return_value
            viewer.insert.assert_called_once_with("1.0", "existing log")
            bench.log.pack.assert_not_called()
            bench.log.pack_forget.assert_not_called()
            bench.append_log("new line")
            viewer.insert.assert_called_with(workflow_app.END, "new line")
            bench.log.insert.assert_called_once_with(workflow_app.END, "new line")
            bench.toggle_console_log()
            popup_factory.return_value.destroy.assert_called_once_with()
            self.assertIsNone(bench._console_popup_text)
            bench.console_toggle_button.configure.assert_called_with(text="展开终端")

    def test_review_description_combines_clip_lead_with_creator_template(self):
        profiles = {
            "kioi": {
                "upload": {
                    "description": "通用简介\n\n个人空间：https://space.example/kioi"
                }
            }
        }
        item = {
            "creator": "kioi",
            "description": "本片保留柚雨与嘉宾的现场对话。",
        }
        value = workflow_app.composed_review_description(item, profiles)
        self.assertEqual(
            value,
            "本片保留柚雨与嘉宾的现场对话。\n\n"
            "通用简介\n\n个人空间：https://space.example/kioi",
        )
        item["description"] = value
        self.assertEqual(
            workflow_app.composed_review_description(item, profiles), value
        )

    def test_participant_parser_supports_many_guests_and_deduplicates(self):
        self.assertEqual(
            workflow_app.split_participant_names(
                "柚雨Kioi、米汀Nagisa, 克罗雅Kloa；雾深Girimi、米汀Nagisa"
            ),
            ["柚雨Kioi", "米汀Nagisa", "克罗雅Kloa", "雾深Girimi"],
        )

    def test_subtitle_search_matches_speaker_text_and_time(self):
        rows = [
            {"name": "米汀Nagisa", "start": "0:00:01.00", "end": "0:00:02.00", "text": "第一句"},
            {"name": "克罗雅Kloa", "start": "0:00:03.00", "end": "0:00:04.00", "text": "美国人能飞"},
            {"name": "雾深Girimi", "start": "0:00:05.00", "end": "0:00:06.00", "text": "第三句"},
        ]
        self.assertEqual(
            workflow_app.filtered_subtitle_rows(rows, "美国人"), [rows[1]]
        )
        self.assertEqual(
            workflow_app.filtered_subtitle_rows(rows, "Girimi"), [rows[2]]
        )
        self.assertEqual(
            workflow_app.filtered_subtitle_rows(rows, "0:00:03"), [rows[1]]
        )

    def test_speaker_choices_are_driven_by_selected_participants(self):
        class Value:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class Box:
            def configure(self, **kwargs):
                self.values = kwargs["values"]

        bench = object.__new__(workflow_app.Workbench)
        bench.review_speaker_box = Box()
        bench.profiles = {
            "kioi": {"display_name": "柚雨Kioi", "title_tag": "柚雨Kioi"},
            "nagisa": {"display_name": "米汀Nagisa", "title_tag": "米汀Nagisa"},
            "kloa": {"display_name": "克罗雅Kloa", "title_tag": "克罗雅Kloa"},
        }
        bench.speaker_profile_labels = {
            "kioi — 柚雨Kioi": "kioi",
            "nagisa — 米汀Nagisa": "nagisa",
            "kloa — 克罗雅Kloa": "kloa",
        }
        bench.profile_labels = {"kioi — 柚雨Kioi": "kioi"}
        bench.vars = {
            "review_participants": Value("柚雨Kioi、米汀Nagisa"),
            "review_speaker": Value("待确认"),
        }
        bench.review_dialogue_rows = []
        bench._current_review_creator_key = lambda: "kioi"
        bench.refresh_review_speaker_choices()
        self.assertIn("kioi — 柚雨Kioi", bench.review_speaker_box.values)
        self.assertIn("nagisa — 米汀Nagisa", bench.review_speaker_box.values)
        self.assertNotIn("kloa — 克罗雅Kloa", bench.review_speaker_box.values)

    def test_cleaned_jobs_are_archived_out_of_progress_table(self):
        state = {
            "jobs": {
                "waiting": {"id": "waiting", "status": "awaiting_delivery_review"},
                "published": {"id": "published", "status": "published"},
                "cleaned": {"id": "cleaned", "status": "cleaned_published"},
                "discarded": {"id": "discarded", "status": "cleaned_discarded"},
            }
        }
        visible = workflow_app.visible_auto_jobs(state)
        self.assertEqual(
            {job["id"] for job in visible},
            {"waiting", "published"},
        )
        self.assertIn("cleaned", state["jobs"])
        self.assertIn("discarded", state["jobs"])

    def test_monitor_detail_wraps_to_width_without_losing_text(self):
        self.assertEqual(workflow_app.split_monitor_detail("短说明", 12), ["短说明"])
        detail = "这是一个足够长的最近说明，需要在监控表格中显示全部文字和最后的原因"
        narrow = workflow_app.split_monitor_detail(detail, 18)
        wide = workflow_app.split_monitor_detail(detail, 36)
        self.assertGreater(len(narrow), len(wide))
        self.assertEqual("".join(narrow), detail)
        self.assertEqual("".join(wide), detail)

    def test_monitor_detail_has_no_line_count_cap_or_added_ellipsis(self):
        detail = "非常长的说明" * 20 + "最终错误原因"
        lines = workflow_app.split_monitor_detail(detail, 12)
        self.assertGreater(len(lines), 2)
        self.assertEqual("".join(lines), detail)
        self.assertNotIn("…", "".join(lines))

    def test_queued_jobs_have_a_distinct_row_color_tag(self):
        self.assertEqual(workflow_app.auto_job_row_tag("queued"), "queued")
        self.assertEqual(
            workflow_app.auto_job_row_tag("failed", retry_pending=True), "queued"
        )
        self.assertEqual(
            workflow_app.auto_job_row_tag("awaiting_delivery_review"), "waiting"
        )

    def test_paused_jobs_have_a_distinct_row_color_tag(self):
        self.assertEqual(workflow_app.auto_job_row_tag("paused"), "paused")

    def test_subtitle_preview_uses_unsaved_normal_and_radio_sizes(self):
        normal = workflow_app.subtitle_preview_spec(
            display_name="昼夜",
            role_color="7fa8d6",
            subtitle_fill_color="#C0C6D4",
            subtitle_outline_color="#111318",
            subtitle_outline_width="5",
            dialogue_font="Microsoft YaHei",
            dialogue_font_size="70",
            radio_subtitle_size="65",
            radio=False,
        )
        radio = workflow_app.subtitle_preview_spec(
            display_name="昼夜",
            role_color="#7FA8D6",
            dialogue_font="Microsoft YaHei",
            dialogue_font_size="70",
            radio_subtitle_size="65",
            radio=True,
        )
        self.assertEqual(normal["role_color"], "#7FA8D6")
        self.assertEqual(normal["subtitle_fill_color"], "#C0C6D4")
        self.assertEqual(normal["subtitle_outline_color"], "#111318")
        self.assertEqual(normal["outline_width"], 5.0)
        self.assertGreaterEqual(normal["canvas_outline_width"], 1)
        self.assertEqual(normal["source_size"], 70)
        self.assertEqual(radio["source_size"], 65)
        with self.assertRaises(ValueError):
            workflow_app.subtitle_preview_spec(
                display_name="昼夜",
                role_color="#7FA8D6",
                dialogue_font="Microsoft YaHei",
                dialogue_font_size="999",
                radio_subtitle_size="65",
            )

    def test_subtitle_preview_draws_profile_fill_full_outline_and_shadow(self):
        class Canvas:
            def __init__(self):
                self.calls = []

            def create_text(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        canvas = Canvas()
        workflow_app.draw_outlined_canvas_text(
            canvas, 100, 200, "关注柚雨Kioi谢谢喵",
            fill="#37C8F3", outline="#111318", outline_width=3,
            shadow_color="#000000", shadow_offset=2,
            font=("Microsoft YaHei", 36, "bold"),
        )
        self.assertGreater(len(canvas.calls), 10)
        self.assertEqual(canvas.calls[0][1]["fill"], "#000000")
        self.assertTrue(
            all(call[1]["fill"] == "#111318" for call in canvas.calls[1:-1])
        )
        self.assertEqual(canvas.calls[-1][1]["fill"], "#37C8F3")
        self.assertEqual(canvas.calls[-1][1]["text"], "关注柚雨Kioi谢谢喵")

    def test_review_playback_rate_parses_and_clamps_ui_value(self):
        class Value:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        bench = object.__new__(workflow_app.Workbench)
        bench.vars = {"review_speed": Value("1.5x")}
        self.assertEqual(bench._review_playback_rate(), 1.5)
        bench.vars["review_speed"].value = "99x"
        self.assertEqual(bench._review_playback_rate(), 2.0)
        bench.vars["review_speed"].value = "bad"
        self.assertEqual(bench._review_playback_rate(), 1.0)

    def test_play_waits_for_waveform_instead_of_indexing_empty_timeline(self):
        class Value:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        bench = object.__new__(workflow_app.Workbench)
        bench.current_review_video = Path("D:/review/clip.mp4")
        bench._review_requested_source_start = 0.0
        bench.review_waveform_data = None
        bench.timeline_segments = lambda: []
        bench.vars = {"review_line_status": Value()}
        loaded = []
        bench.load_current_waveform = lambda: loaded.append(True)
        with (
            patch.object(
                workflow_app.review_workspace,
                "supports_review_proxy",
                return_value=False,
            ),
            patch.object(workflow_app.review_workspace, "find_libvlc") as find_vlc,
            patch.object(workflow_app.messagebox, "showerror") as showerror,
        ):
            bench.play_current_embedded()

        self.assertEqual(loaded, [True])
        self.assertIn("完成后会自动播放", bench.vars["review_line_status"].value)
        find_vlc.assert_not_called()
        showerror.assert_not_called()

    def test_review_list_only_keeps_pending_and_revise_items(self):
        self.assertTrue(workflow_app.review_queue_status("pending"))
        self.assertTrue(workflow_app.review_queue_status("revise"))
        self.assertFalse(workflow_app.review_queue_status("approved"))
        self.assertFalse(workflow_app.review_queue_status("skipped"))

    def test_visible_review_statuses_adds_approved_only_when_history_is_enabled(self):
        class Value:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        bench = object.__new__(workflow_app.Workbench)
        bench.vars = {"review_show_approved": Value(False)}
        self.assertEqual({"pending", "revise"}, bench.visible_review_statuses())
        bench.vars["review_show_approved"].value = True
        self.assertEqual(
            {"pending", "revise", "approved"}, bench.visible_review_statuses()
        )

    def test_first_edit_of_approved_clip_requires_and_records_replacement(self):
        class Value:
            def __init__(self, value=""):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class Tree:
            def __init__(self):
                self.values = ["主播", "场次", "001", "审核通过", "标题"]
                self.tags = ("approved",)

            def selection(self):
                return ("row",)

            def item(self, _iid, option=None, **kwargs):
                if "values" in kwargs:
                    self.values = list(kwargs["values"])
                if "tags" in kwargs:
                    self.tags = tuple(kwargs["tags"])
                if option == "values":
                    return tuple(self.values)
                return {"values": tuple(self.values), "tags": self.tags}

        with tempfile.TemporaryDirectory() as temporary:
            review = Path(temporary)
            video = review / "001.mp4"
            video.write_bytes(b"video")
            workflow_app.review_workspace.load_decisions(review)
            workflow_app.review_workspace.set_decision(
                review, video.name, "approved"
            )
            bench = object.__new__(workflow_app.Workbench)
            bench.current_review_dir = review
            bench.current_review_video = video
            bench.vars = {"review_line_status": Value()}
            bench.review_clip_tree = Tree()
            bench.review_item_map = {"row": {"status": "approved"}}
            bench.auto_job_for_review_directory = lambda _review: {
                "id": "job-1",
                "status": "ready_to_publish",
                "manual_republish_required": True,
            }
            with patch.object(
                workflow_app.messagebox, "askyesno", return_value=True
            ) as prompt:
                self.assertTrue(
                    bench._ensure_current_approved_revision("封面或 Tag")
                )
            decision = workflow_app.review_workspace.load_decisions(
                review, create=False
            )["clips"][video.name]
            self.assertEqual("revise", decision["status"])
            self.assertTrue(decision["replacement_requested"])
            self.assertTrue(decision["replacement_was_published"])
            self.assertEqual(("revise",), bench.review_clip_tree.tags)
            prompt.assert_called_once()

    def test_clicking_subtitle_seeks_to_start_and_autoplays(self):
        class Value:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class Tree:
            def selection(self):
                return ("7",)

        bench = object.__new__(workflow_app.Workbench)
        bench.review_subtitle_tree = Tree()
        bench._review_edit_line = 7
        bench.review_dialogue_rows = [{
            "line_number": 7,
            "start": "0:00:12.34",
            "end": "0:00:14.00",
            "text": "选中的字幕",
        }]
        bench.vars = {"review_line_status": Value()}
        seeks = []
        bench.seek_timeline_seconds = (
            lambda seconds, **kwargs: seeks.append((seconds, kwargs))
        )

        bench.play_selected_subtitle()

        self.assertEqual([(12.34, {"autoplay": True})], seeks)
        self.assertIn("开始播放", bench.vars["review_line_status"].value)

    def test_autoplay_seek_resumes_an_existing_player(self):
        class Player:
            player = object()

            def __init__(self):
                self.resumed = 0

            def resume(self):
                self.resumed += 1
                return False

        bench = object.__new__(workflow_app.Workbench)
        bench.timeline_segments = lambda: [{
            "source_start": 10.0,
            "source_end": 20.0,
            "edited_start": 0.0,
            "edited_end": 10.0,
        }]
        bench.preview_player = Player()
        positioned = []
        bench._set_preview_source_time = (
            lambda seconds, **kwargs: positioned.append((seconds, kwargs))
        )
        bench.draw_review_playhead = lambda: None

        bench.seek_timeline_seconds(2.5, autoplay=True)

        self.assertEqual([(12.5, {"jump": False})], positioned)
        self.assertEqual(1, bench.preview_player.resumed)

    def test_preview_segment_opens_source_without_rebuilding_at_each_boundary(self):
        class Player:
            player = object()

            def __init__(self):
                self.play_calls = []
                self.rates = []
                self.seeks = []

            def play(self, video, window_id, **kwargs):
                self.play_calls.append((video, window_id, kwargs))

            def set_rate(self, value):
                self.rates.append(value)

            def set_time(self, value):
                self.seeks.append(value)

            def set_muted(self, _value):
                return True

            def pause(self):
                return None

        class Host:
            def winfo_id(self):
                return 77

        class Value:
            def get(self):
                return "1.0x"

        bench = object.__new__(workflow_app.Workbench)
        bench.preview_player = Player()
        bench.current_review_video = Path("D:/review/clip.mp4")
        bench.current_review_media = Path("D:/source/full.mkv")
        bench.review_player_host = Host()
        bench.vars = {"review_speed": Value()}
        bench.timeline_segments = lambda: [
            {"source_start": 10.0, "source_end": 20.0},
            {"source_start": 30.0, "source_end": 40.0},
        ]
        bench.after = lambda _delay, callback: callback()
        bench._review_starting_source = None
        bench._review_starting_until = 0.0

        bench._play_preview_segment(0, 12.5)

        _video, _window, options = bench.preview_player.play_calls[0]
        self.assertEqual(options["start_seconds"], 12.5)
        self.assertNotIn("stop_seconds", options)
        self.assertEqual(bench._preview_segment_index, 0)

    def test_streaming_review_plays_exact_clip_on_its_local_clock(self):
        class Player:
            player = object()

            def __init__(self):
                self.play_calls = []

            def play(self, video, window_id, **kwargs):
                self.play_calls.append((video, window_id, kwargs))

            def set_rate(self, _value):
                return True

            def set_time(self, _value):
                return None

            def set_muted(self, _value):
                return True

            def pause(self):
                return None

        class Host:
            def winfo_id(self):
                return 77

        class Value:
            def get(self):
                return "1.0x"

        bench = object.__new__(workflow_app.Workbench)
        bench.preview_player = Player()
        bench.current_review_video = Path("D:/review/clip.mp4")
        bench.current_review_media = Path("D:/source/full.flv")
        bench.review_player_host = Host()
        bench.vars = {"review_speed": Value()}
        bench.timeline_segments = lambda: [
            {"source_start": 100.0, "source_end": 110.0},
        ]
        bench.after = lambda _delay, _callback: None
        bench._review_starting_source = None
        bench._review_starting_until = 0.0

        exact = bench.current_review_video.resolve()
        with (
            patch.object(workflow_app.review_workspace, "has_streaming_review", return_value=True),
            patch.object(workflow_app.review_workspace, "review_playback_media", return_value=exact),
            patch.object(
                workflow_app.review_workspace,
                "source_to_review_media_seconds",
                return_value=2.5,
            ),
        ):
            bench._play_preview_segment(0, 102.5)

        media, _window, options = bench.preview_player.play_calls[0]
        self.assertEqual(media, exact)
        self.assertEqual(options["start_seconds"], 2.5)
        self.assertEqual(bench.current_review_media, exact)

    def test_preview_segment_transition_reuses_player_and_seeks_in_place(self):
        class Player:
            player = object()

            def __init__(self):
                self.play_calls = []
                self.current = 10.0
                self.mutes = []

            def play(self, *args, **kwargs):
                self.play_calls.append((args, kwargs))

            def set_time(self, value):
                self.current = float(value)

            def current_seconds(self):
                return self.current

            def set_muted(self, value):
                self.mutes.append(bool(value))
                return True

            def set_rate(self, _value):
                return True

            def is_playing(self):
                return True

            def resume(self):
                return False

            def pause(self):
                return None

        class Host:
            def winfo_id(self):
                return 77

        class Value:
            def get(self):
                return "1.0x"

        bench = object.__new__(workflow_app.Workbench)
        bench.preview_player = Player()
        bench.current_review_video = Path("D:/review/clip.mp4")
        bench.current_review_media = Path("D:/source/full.mkv")
        bench._preview_media_path = str(bench.current_review_media.resolve())
        bench.review_player_host = Host()
        bench.vars = {"review_speed": Value()}
        bench.timeline_segments = lambda: [
            {"source_start": 10.0, "source_end": 20.0},
            {"source_start": 30.0, "source_end": 40.0},
        ]
        bench.after = lambda _delay, callback: callback()
        bench._review_jump_source = None
        bench._review_jump_until = 0.0
        bench._review_starting_source = None
        bench._review_starting_until = 0.0

        bench._play_preview_segment(1, 30.0)

        self.assertEqual(bench.preview_player.play_calls, [])
        self.assertEqual(bench.preview_player.current, 30.0)
        self.assertEqual(bench.preview_player.mutes, [True, False])
        self.assertEqual(bench._preview_segment_index, 1)

    def test_playback_boundary_advances_only_to_next_kept_segment(self):
        class Player:
            player = object()

            def current_seconds(self):
                return 20.0

            def is_playing(self):
                return True

        class Value:
            def set(self, value):
                self.value = value

            def get(self):
                return "1.0x"

        bench = object.__new__(workflow_app.Workbench)
        bench.preview_player = Player()
        bench.current_review_video = None
        bench._preview_segment_index = 0
        bench._preview_segment_started_at = workflow_app.time.monotonic() - 1.0
        segments = [
            {"source_start": 10.0, "source_end": 20.0},
            {"source_start": 30.0, "source_end": 40.0},
        ]
        bench.timeline_segments = lambda: segments
        bench._preview_source_seconds = lambda: 20.0
        starts = []
        bench._play_preview_segment = (
            lambda index, source, **_kwargs: starts.append((index, source))
        )
        bench.vars = {"review_playback": Value(), "review_speed": Value()}
        bench.format_review_clock = lambda value: f"{value:.1f}"
        bench.draw_review_playhead = lambda: None
        bench._update_review_video_subtitle = lambda *_args, **_kwargs: None
        bench._keep_review_playhead_visible = lambda _value: None
        bench.winfo_exists = lambda: False

        bench._review_playback_tick()

        self.assertEqual(starts, [(1, 30.0)])

    def test_combined_button_approves_pending_clip(self):
        bench = object.__new__(workflow_app.Workbench)
        bench.current_review_dir = Path("D:/review")
        bench.current_review_video = Path("D:/review/001.mp4")
        bench._current_review_decision_row = lambda: {"status": "pending"}
        bench._current_review_source_publication = lambda: (False, None)
        statuses = []
        bench.set_current_review_decision = statuses.append

        bench.approve_or_replace_current_review()

        self.assertEqual(["approved"], statuses)

    def test_combined_button_confirms_approved_replacement_then_approves(self):
        bench = object.__new__(workflow_app.Workbench)
        bench.current_review_dir = Path("D:/review")
        bench.current_review_video = Path("D:/review/001.mp4")
        bench._current_review_decision_row = lambda: {"status": "approved"}
        bench._current_review_source_publication = lambda: (True, {"id": "job-1"})
        confirmed = []
        bench._ensure_current_approved_revision = (
            lambda action: confirmed.append(action) or True
        )
        statuses = []
        bench.set_current_review_decision = statuses.append

        bench.approve_or_replace_current_review()

        self.assertEqual(["内容"], confirmed)
        self.assertEqual(["approved"], statuses)

    def test_approved_nonpublished_clip_still_writes_as_approved(self):
        bench = object.__new__(workflow_app.Workbench)
        bench.current_review_dir = Path("D:/review")
        bench.current_review_video = Path("D:/review/001.mp4")
        bench._current_review_decision_row = lambda: {"status": "approved"}
        bench._current_review_source_publication = lambda: (False, None)
        bench._ensure_current_approved_revision = lambda _action: self.fail(
            "ordinary approved clips must not enter source replacement"
        )
        statuses = []
        bench.set_current_review_decision = statuses.append

        bench.approve_or_replace_current_review()

        self.assertEqual(["approved"], statuses)

    def test_review_primary_button_text_tracks_publication_state(self):
        class Button:
            def __init__(self):
                self.text = ""

            def configure(self, **kwargs):
                self.text = kwargs["text"]

        bench = object.__new__(workflow_app.Workbench)
        bench.review_approve_button = Button()
        decision = {"status": "pending"}
        bench._current_review_decision_row = lambda: decision
        bench._current_review_source_publication = lambda: (False, None)

        bench.refresh_review_primary_action()
        self.assertEqual("写入为通过", bench.review_approve_button.text)

        decision["status"] = "approved"
        bench._current_review_source_publication = lambda: (True, {"id": "job-1"})
        bench.refresh_review_primary_action()
        self.assertEqual(
            "重新烧录并替换源", bench.review_approve_button.text
        )

    def test_approved_history_has_explicit_green_review_status(self):
        class Value:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class Label:
            def __init__(self):
                self.foreground = ""

            def configure(self, **kwargs):
                self.foreground = kwargs["foreground"]

        bench = object.__new__(workflow_app.Workbench)
        status = Value()
        label = Label()
        bench.vars = {"review_decision_status": status}
        bench.review_decision_status_label = label

        bench.refresh_review_decision_status("approved")

        self.assertEqual("审核状态：审核通过", status.value)
        self.assertEqual("#137333", label.foreground)

    def test_open_review_folder_falls_back_to_configured_path_after_pool_clears(self):
        class Value:
            def get(self):
                return "D:/review-batch"

        bench = object.__new__(workflow_app.Workbench)
        bench.current_review_dir = None
        bench.vars = {"review_dir": Value()}
        opened = []
        bench.open_path = opened.append
        bench.open_loaded_review_folder()
        self.assertEqual(opened[0].as_posix(), "D:/review-batch")

    def test_different_manual_flows_can_overlap_but_same_flow_has_one_key(self):
        python = "python.exe"
        core = "workflow_app_core.py"
        self.assertEqual(
            workflow_app.command_flow_key([python, core, "step1"]),
            "manual:flow1",
        )
        self.assertEqual(
            workflow_app.command_flow_key([python, core, "prompt"]),
            workflow_app.command_flow_key([python, core, "import-selection"]),
        )
        self.assertNotEqual(
            workflow_app.command_flow_key([python, core, "step1"]),
            workflow_app.command_flow_key([python, core, "step3"]),
        )

    def test_explicit_automatic_queue_actions_are_manual_priority(self):
        python = "python.exe"
        auto = "workflow_auto.py"
        redo = [
            python, auto, "--config", "auto.json", "redo-job",
            "--job-id", "job-1", "--from-flow", "flow3",
        ]
        self.assertEqual(workflow_app.command_flow_key(redo), "manual:flow3")
        self.assertEqual(
            workflow_app.command_flow_key(
                [
                    python, auto, "--config", "auto.json", "retry-codex",
                    "--job-id", "job-1",
                ]
            ),
            "manual:flow2",
        )
        self.assertEqual(
            workflow_app.command_flow_key(
                [
                    python, auto, "--config", "auto.json", "continue-late-job",
                    "--job-id", "job-1",
                ]
            ),
            "manual:flow0",
        )
        self.assertEqual(
            workflow_app.command_flow_key(
                [python, auto, "--config", "auto.json", "run-queue"]
            ),
            "manual:queue-run",
        )
        for action in ("pause-job", "resume-job", "prioritize-job"):
            self.assertEqual(
                workflow_app.command_flow_key(
                    [python, auto, "--config", "auto.json", action, "--job-id", "job-1"]
                ),
                "manual:queue-control",
            )
        self.assertEqual(
            workflow_app.command_flow_key(
                [python, auto, "--config", "auto.json", "watch"]
            ),
            "automatic-queue",
        )
        self.assertEqual(
            workflow_app.command_flow_key(
                [python, auto, "--config", "auto.json", "cleanup-job", "--job-id", "job-1"]
            ),
            "automatic-queue",
        )

    def test_dispatcher_runs_two_different_flows_and_queues_same_flow(self):
        class Value:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        bench = object.__new__(workflow_app.Workbench)
        bench.command_counter = 0
        bench.active_commands = {}
        bench.pending_commands = deque()
        bench.vars = {"status": Value()}
        bench.append_log = lambda _value: None
        bench._sync_manual_activity_marker = Mock()

        def launch(record):
            bench.active_commands[record["flow_key"]] = record

        bench._launch_command = launch
        python = "python.exe"
        core = "workflow_app_core.py"
        bench.run_command([python, core, "step1"])
        bench.run_command([python, core, "step1"])
        bench.run_command([python, core, "step3"])

        self.assertEqual(
            set(bench.active_commands),
            {"manual:flow1", "manual:flow3"},
        )
        self.assertEqual(len(bench.pending_commands), 1)
        self.assertEqual(
            bench.pending_commands[0]["flow_key"],
            "manual:flow1",
        )

    def test_fully_decided_old_batch_is_derived_as_ready_for_publish(self):
        job = {
            "id": "job-1",
            "status": "awaiting_delivery_review",
            "project": "D:/project",
        }
        decisions = {
            "clips": {
                "001.mp4": {"status": "approved"},
                "002.mp4": {"status": "skipped"},
            }
        }
        with (
            patch.object(
                workflow_app.core,
                "load_project",
                return_value=(Path("D:/project/workflow-project.json"), {"active_review_dir": "D:/review"}),
            ),
            patch.object(
                workflow_app.review_workspace,
                "load_decisions",
                return_value=decisions,
            ),
        ):
            summary = workflow_app.auto_job_review_completion(job)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["approved"], 1)
        self.assertEqual(summary["unresolved"], 0)

    def test_all_rejected_old_batch_is_complete_with_zero_approved(self):
        job = {
            "id": "job-rejected",
            "status": "awaiting_delivery_review",
            "project": "D:/project",
        }
        with (
            patch.object(
                workflow_app.core,
                "load_project",
                return_value=(
                    Path("D:/project/workflow-project.json"),
                    {"active_review_dir": "D:/review"},
                ),
            ),
            patch.object(
                workflow_app.auto,
                "review_decision_summary",
                return_value={
                    "complete": True,
                    "approved": 0,
                    "unresolved": 0,
                    "review_dir": "D:/review",
                },
            ),
        ):
            summary = workflow_app.auto_job_review_completion(job)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["approved"], 0)

    def test_pending_review_display_says_flow4_has_not_started(self):
        display = workflow_app.auto_job_review_display(
            {"complete": False, "approved": 0, "unresolved": 1}
        )

        self.assertEqual(display[0], "等待人工审核（1 条未决定）")
        self.assertEqual(display[1], "等待审核")
        self.assertIn("流程4尚未启动", display[2])

    def test_completed_review_display_prompts_burn(self):
        display = workflow_app.auto_job_review_display(
            {"complete": True, "approved": 2, "unresolved": 0}
        )

        self.assertEqual(display[0], "审核已完成，待烧录发布")
        self.assertEqual(display[1], "待烧录发布")
        self.assertIn("2 条通过", display[2])
    def test_redo_flow_options_are_listed_from_flow0_through_flow5(self):
        self.assertEqual(
            list(workflow_app.REDO_FLOW_OPTIONS.values()),
            ["flow0", "flow1", "flow2", "flow3", "flow4", "flow5"],
        )
        self.assertTrue(
            next(iter(workflow_app.REDO_FLOW_OPTIONS)).startswith("流程 0")
        )


    def test_applying_or_applied_state_cannot_recreate_pending_edit_file(self):
        bench = object.__new__(workflow_app.Workbench)
        bench.current_review_video = Path("D:/review/clip.mp4")
        with patch.object(
            workflow_app.review_workspace, "save_timeline_edit"
        ) as save_edit:
            for status in ("applying", "applied"):
                bench.timeline_state = {"status": status, "segments": []}
                bench.persist_timeline_state()
        save_edit.assert_not_called()

    def test_pending_state_is_still_persisted_normally(self):
        bench = object.__new__(workflow_app.Workbench)
        bench.current_review_video = Path("D:/review/clip.mp4")
        bench.timeline_state = {"status": "clean", "segments": []}
        with patch.object(
            workflow_app.review_workspace, "save_timeline_edit"
        ) as save_edit:
            bench.persist_timeline_state()
        self.assertEqual("pending", bench.timeline_state["status"])
        save_edit.assert_called_once_with(
            bench.current_review_video, bench.timeline_state
        )

    def test_approving_during_render_queues_approval_after_saving(self):
        class Value:
            def __init__(self, value=""):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        bench = object.__new__(workflow_app.Workbench)
        bench.current_review_dir = Path("D:/review")
        bench.current_review_video = Path("D:/review/clip.mp4")
        bench.timeline_rendering = True
        bench.timeline_rendering_video_key = str(
            bench.current_review_video.resolve()
        )
        bench._flush_pending_subtitle_autosave = lambda: True
        bench.save_current_review_copy = lambda **_kwargs: True
        bench.vars = {
            "review_notes": Value("ok"),
            "review_line_status": Value(),
        }
        bench.set_current_review_decision("approved")
        self.assertEqual(
            bench._timeline_approval_request["key"],
            str(bench.current_review_video.resolve()),
        )
        self.assertIn("自动通过", bench.vars["review_line_status"].value)

    def test_approve_with_pending_edit_starts_apply_without_confirmation(self):
        class Value:
            def get(self):
                return ""

        bench = object.__new__(workflow_app.Workbench)
        bench.current_review_dir = Path("D:/review")
        bench.current_review_video = Path("D:/review/clip.mp4")
        bench.timeline_rendering = False
        bench._flush_pending_subtitle_autosave = lambda: True
        bench.save_current_review_copy = lambda **_kwargs: True
        bench.vars = {"review_notes": Value()}
        calls = []
        bench.apply_timeline_edit = lambda **kwargs: calls.append(kwargs) or True
        with (
            patch.object(workflow_app.review_workspace, "timeline_edit_path") as edit_path,
            patch.object(
                workflow_app.review_workspace,
                "_read_json",
                return_value={"status": "pending"},
            ),
        ):
            edit_path.return_value.is_file.return_value = True
            bench.set_current_review_decision("approved")
        self.assertEqual(calls, [{"confirm": False}])
        self.assertEqual(
            bench._timeline_approval_request["video_name"], "clip.mp4"
        )
    def test_auto_publish_batch_includes_pending_without_human_decision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            review = root / "review"
            project.mkdir()
            review.mkdir()
            video = review / "001-topic.mp4"
            video.write_bytes(b"video")
            workflow_app.review_workspace.load_decisions(review)
            (review / "titles-and-covers.csv").write_text(
                "video,title\n001-topic.mp4,待审但可直传\n",
                encoding="utf-8-sig",
            )
            bench = object.__new__(workflow_app.Workbench)
            with patch.object(
                workflow_app.core,
                "load_project",
                return_value=(
                    project / "workflow-project.json",
                    {"active_review_dir": str(review)},
                ),
            ):
                _directory, selected, titles, unreviewed = (
                    bench._review_batch_for_job({"project": str(project)})
                )
            self.assertEqual(["001-topic.mp4"], selected)
            self.assertEqual(["待审但可直传"], titles)
            self.assertEqual(1, unreviewed)

    def test_published_revision_uses_merged_burn_and_upload_action(self):
        bench = object.__new__(workflow_app.Workbench)
        calls = []
        bench.replace_selected_published_job = calls.append
        job = {
            "id": "job-1",
            "status": "ready_to_publish",
            "manual_republish_required": True,
        }

        bench._publish_auto_job(job)

        self.assertEqual([job], calls)

    def test_waiting_published_revision_can_start_merged_replace_action(self):
        bench = object.__new__(workflow_app.Workbench)
        bench._review_batch_for_job = lambda _job: (
            Path("D:/review"),
            ["001.mp4"],
            ["修订后的标题"],
            0,
        )
        bench.auto_command = lambda command, extra=None, **_kwargs: [
            command, *(extra or [])
        ]
        launched = []
        bench._run_selected_auto_command = (
            lambda command, **kwargs: launched.append((command, kwargs))
        )
        job = {
            "id": "job-1",
            "status": "awaiting_delivery_review",
            "manual_republish_required": True,
            "project": "D:/project",
            "publication_history": [{"bvids": ["BV1234567890"]}],
        }

        with patch.object(
            workflow_app.messagebox, "askokcancel", return_value=True
        ) as confirm:
            bench.replace_selected_published_job(job)

        self.assertIn("先按审核台当前选择重新烧录", confirm.call_args.args[1])
        self.assertEqual("replace-published-job", launched[0][0][0])
        self.assertEqual("重新烧录并替换原稿素材", launched[0][1]["action_name"])

    def test_unreviewed_upload_requires_second_explicit_confirmation(self):
        bench = object.__new__(workflow_app.Workbench)
        bench.profiles = {"awu": {"display_name": "哎小呜"}}
        bench._review_batch_for_job = lambda _job: (
            Path("D:/review"),
            ["001.mp4"],
            ["待审标题"],
            1,
        )
        bench._run_selected_auto_command = lambda *_args, **_kwargs: self.fail(
            "upload must not start when explicit no-review confirmation is declined"
        )
        job = {
            "id": "job-1",
            "creator": "awu",
            "started_at": "2026-08-24T01:00:00",
            "title": "测试直播",
            "status": "awaiting_delivery_review",
            "project": "D:/missing",
        }
        with (
            patch.object(workflow_app.messagebox, "askokcancel", return_value=True),
            patch.object(
                workflow_app.messagebox, "askyesno", return_value=False
            ) as no_review,
        ):
            bench._publish_auto_job(job)

        no_review.assert_called_once()

    def test_reviewed_upload_shows_titles_without_no_review_confirmation(self):
        bench = object.__new__(workflow_app.Workbench)
        bench.profiles = {"awu": {"display_name": "哎小呜"}}
        bench._review_batch_for_job = lambda _job: (
            Path("D:/review"),
            ["001.mp4"],
            ["已经审核的标题"],
            0,
        )
        bench.auto_command = lambda command, extra=None, **_kwargs: [
            command, *(extra or [])
        ]
        bench._after_selected_upload = lambda _job_id, _resume: False
        launched = []
        bench._run_selected_auto_command = (
            lambda command, **kwargs: launched.append((command, kwargs))
        )
        job = {
            "id": "job-1",
            "creator": "awu",
            "started_at": "2026-08-24T01:00:00",
            "title": "测试直播",
            "status": "awaiting_delivery_review",
            "project": "D:/missing",
        }
        with (
            patch.object(
                workflow_app.messagebox, "askokcancel", return_value=True
            ) as title_prompt,
            patch.object(workflow_app.messagebox, "askyesno") as no_review,
        ):
            bench._publish_auto_job(job)

        self.assertIn("已经审核的标题", title_prompt.call_args.args[1])
        no_review.assert_not_called()
        self.assertEqual("run-job", launched[0][0][0])
        self.assertIn("--allow-upload", launched[0][0])

    def test_successful_upload_does_not_prompt_for_cleanup(self):
        class Value:
            def set(self, value):
                self.value = value

        bench = object.__new__(workflow_app.Workbench)
        bench.vars = {"auto_status": Value()}
        bench.refresh_auto_status = lambda **_kwargs: None
        bench.auto_job_by_id = lambda _job_id: {
            "id": "job-1",
            "status": "published",
        }
        logs = []
        bench.append_log = logs.append
        bench._start_cleanup_job = lambda *_args, **_kwargs: self.fail(
            "cleanup must remain a separate manual action"
        )

        with (
            patch.object(workflow_app.messagebox, "askokcancel") as cleanup_prompt,
            patch.object(workflow_app.messagebox, "showinfo") as cleanup_notice,
        ):
            handled_resume = bench._after_selected_upload("job-1", True)

        self.assertFalse(handled_resume)
        cleanup_prompt.assert_not_called()
        cleanup_notice.assert_not_called()
        self.assertIn("队列继续执行", bench.vars["auto_status"].value)
        self.assertIn("未自动弹出清理确认", "".join(logs))

    def test_upload_queues_behind_monitor_without_stopping_it(self):
        class Value:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        bench = object.__new__(workflow_app.Workbench)
        monitor = object()
        bench.monitor_process = monitor
        bench._monitor_starting = False
        bench.vars = {"auto_status": Value()}
        bench.save_auto_config = lambda: None
        logs = []
        launched = []
        bench._kill_tree = lambda _process: self.fail(
            "queued upload must not stop the monitor"
        )
        bench._recover_interrupted_auto_jobs = lambda: self.fail(
            "queued upload must not recover an uninterrupted job"
        )
        bench.append_log = logs.append
        bench.refresh_auto_status = lambda **_kwargs: None
        bench.run_command = (
            lambda command, after=None, **kwargs: launched.append((command, after))
        )
        bench.after = lambda _delay, _callback: self.fail(
            "queued upload must not enter the stop-wait loop"
        )
        command = [
            "python",
            "workflow_auto.py",
            "--config",
            "workflow-auto.json",
            "run-job",
            "--job-id",
            "job-1",
            "--allow-upload",
        ]

        bench._run_selected_auto_command(
            command, action_name="烧录并上传"
        )

        self.assertIs(bench.monitor_process, monitor)
        self.assertEqual(launched[0][0], command)
        self.assertIn("已排队", bench.vars["auto_status"].value)
        self.assertTrue(any("优先队列" in value for value in logs))

    def test_manual_flow_that_coexists_does_not_wait_for_monitor_to_stop(self):
        class Value:
            def set(self, _value):
                return None

        bench = object.__new__(workflow_app.Workbench)
        monitor = object()
        bench.monitor_process = monitor
        bench._monitor_starting = False
        bench.vars = {"auto_status": Value()}
        bench.save_auto_config = lambda: None
        bench._kill_tree = lambda _process: self.fail(
            "coexisting manual flow must not stop the monitor"
        )
        bench._recover_interrupted_auto_jobs = lambda: self.fail(
            "coexisting manual flow has no interrupted job"
        )
        bench.append_log = lambda _value: None
        bench.refresh_auto_status = lambda **_kwargs: None
        launched = []
        bench.run_command = (
            lambda command, after=None, **kwargs: launched.append((command, after))
        )
        bench.after = lambda _delay, _callback: self.fail(
            "coexisting manual flow must not enter the stop-wait loop"
        )
        command = [
            "python",
            "workflow_auto.py",
            "--config",
            "workflow-auto.json",
            "quick-clip",
            "--job-id",
            "live-1",
        ]

        bench._run_selected_auto_command(
            command, action_name="立刻切片"
        )

        self.assertIs(bench.monitor_process, monitor)
        self.assertEqual(launched[0][0], command)
    def test_manual_burn_can_choose_skip_human_review(self):
        bench = object.__new__(workflow_app.Workbench)
        bench.project_dir = lambda: "D:/project"
        bench.open_delivery_folder = lambda: None
        launched = []
        bench.run_core = lambda args, after=None: launched.append((args, after))
        with patch.object(
            workflow_app.messagebox, "askyesnocancel", return_value=False
        ):
            bench.start_burn()
        self.assertEqual(
            ["step4", "--project", "D:/project", "--skip-human-review"],
            launched[0][0],
        )
    def test_manual_burn_saves_latest_cover_copy_before_launch(self):
        for saved in (True, False):
            with self.subTest(saved=saved):
                bench = object.__new__(workflow_app.Workbench)
                project = Path("D:/project")
                bench.project_dir = lambda: str(project)
                bench.current_review_dir = project / "runs" / "review"
                bench.current_review_video = bench.current_review_dir / "clip.mp4"
                bench.open_delivery_folder = Mock()
                bench.run_core = Mock()
                bench.save_current_review_copy = Mock(return_value=saved)
                with patch.object(workflow_app.messagebox, "askyesnocancel", return_value=True), patch.object(
                    workflow_app.review_workspace, "_workflow_project_root", return_value=project
                ):
                    bench.start_burn()
                bench.save_current_review_copy.assert_called_once_with(silent=True)
                self.assertEqual(bench.run_core.call_count, int(saved))

    def test_review_context_save_uses_draft_metadata(self):
        class Value:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        bench = object.__new__(workflow_app.Workbench)
        bench.review_tab = "review"
        bench.notebook = type("Notebook", (), {"select": lambda self: "review"})()
        bench.current_review_video = Path("D:/review/clip.mp4")
        bench.current_review_ass = None
        bench.vars = {"status": Value()}
        bench.append_log = lambda _value: None
        calls = []
        bench.save_current_review_copy = lambda **kwargs: calls.append(kwargs) or True
        self.assertTrue(bench._save_active_context())
        self.assertEqual(calls, [{"silent": True, "draft": True}])

    def test_inline_fuzzy_dictionary_replaces_and_saves_current_line(self):
        class Value:
            def __init__(self, value=""):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class TextEditor:
            def __init__(self):
                self.value = "今天收到钢崩了"

            def get(self, start, _end):
                if start == "sel.first":
                    raise RuntimeError("no selection")
                return self.value

            def delete(self, _start, _end):
                self.value = ""

            def insert(self, _start, value):
                self.value = value

            def edit_modified(self, _value):
                return None

        bench = object.__new__(workflow_app.Workbench)
        bench.vars = {
            "review_fuzzy_wrong": Value("钢崩"),
            "review_fuzzy_replacement": Value("（谢谢SC）"),
            "review_line_status": Value(),
        }
        bench.review_text_editor = TextEditor()
        bench._schedule_subtitle_autosave = lambda: None
        bench._flush_pending_subtitle_autosave = lambda: True
        with patch.object(
            workflow_app.glossary,
            "upsert_correction",
            return_value={"wrong": "钢崩", "replacement": "（谢谢SC）"},
        ) as upsert:
            bench.add_review_fuzzy_correction()
        upsert.assert_called_once_with("钢崩", "（谢谢SC）", status="active")
        self.assertEqual(bench.review_text_editor.value, "今天收到（谢谢SC）了")
        self.assertIn("已替换并写入", bench.vars["review_line_status"].value)

    def test_failed_subtitle_autosave_restores_the_loaded_clip_selection(self):
        class Tree:
            def __init__(self):
                self.selected = ("new",)

            def selection(self):
                return self.selected

            def selection_set(self, iid):
                self.selected = (iid,)

            def focus(self, _iid):
                return None

            def see(self, _iid):
                return None

        bench = object.__new__(workflow_app.Workbench)
        old_video = Path("D:/review/old.mp4")
        bench.current_review_video = old_video
        bench._review_edit_line = 7
        bench._flush_pending_subtitle_autosave = lambda: False
        bench.review_item_map = {
            "old": {"video": old_video},
            "new": {"video": Path("D:/review/new.mp4")},
        }
        bench.review_clip_tree = Tree()

        bench.review_clip_selected()

        self.assertEqual(("old",), bench.review_clip_tree.selection())
        self.assertEqual(old_video, bench.current_review_video)
        self.assertEqual(7, bench._review_edit_line)

    def test_stale_subtitle_line_after_empty_pool_does_not_block_new_clip(self):
        bench = object.__new__(workflow_app.Workbench)
        bench._subtitle_autosave_after = None
        bench._review_edit_line = 7
        bench._subtitle_preserve_editor_line = 7
        bench.current_review_ass = None

        self.assertTrue(bench._flush_pending_subtitle_autosave())
        self.assertIsNone(bench._review_edit_line)
        self.assertIsNone(bench._subtitle_preserve_editor_line)

    def test_missing_local_review_material_refreshes_its_batch(self):
        class Tree:
            def selection(self):
                return ("missing",)

        class Value:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        bench = object.__new__(workflow_app.Workbench)
        bench.current_review_video = None
        bench._review_edit_line = None
        bench.review_clip_tree = Tree()
        bench.review_item_map = {
            "missing": {
                "video": Path("D:/review/missing.mp4"),
                "review_dir": Path("D:/review"),
            }
        }
        bench.vars = {"review_line_status": Value()}
        bench.review_global_mode = False
        callbacks = []
        bench.after_idle = callbacks.append
        refreshed = []
        bench.load_review_directory = refreshed.append

        with patch.object(Path, "is_file", return_value=False):
            bench.review_clip_selected()
        callbacks[0]()

        self.assertEqual([Path("D:/review")], refreshed)
        self.assertIn("素材已不存在", bench.vars["review_line_status"].value)

    def test_published_stale_review_does_not_submit_another_burn(self):
        class Value:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        with tempfile.TemporaryDirectory() as temporary:
            review = Path(temporary)
            (review / "clip.mp4").write_bytes(b"video")
            bench = object.__new__(workflow_app.Workbench)
            bench.vars = {"auto_status": Value()}
            bench.auto_job_for_review_directory = lambda _review: {
                "id": "job-1", "status": "published"
            }
            bench.load_global_review_pool = lambda: None
            bench._run_selected_auto_command = lambda *_args, **_kwargs: self.fail(
                "published jobs must not be burned again"
            )
            with patch.object(
                workflow_app.review_workspace,
                "load_decisions",
                return_value={"clips": {"clip.mp4": {"status": "approved"}}},
            ):
                bench.maybe_auto_burn_review_batch(review)
        self.assertIn("已经投稿", bench.vars["auto_status"].value)

    def test_finished_review_batch_starts_burn_and_upload(self):
        class Value:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        with tempfile.TemporaryDirectory() as temporary:
            review = Path(temporary)
            (review / "clip.mp4").write_bytes(b"video")
            bench = object.__new__(workflow_app.Workbench)
            bench.vars = {"auto_status": Value()}
            bench.auto_job_for_review_directory = lambda _review: {
                "id": "job-1", "status": "awaiting_delivery_review"
            }
            bench.auto_command = lambda command, extra=None: [command, *(extra or [])]
            launches = []
            bench._run_selected_auto_command = lambda command, **kwargs: launches.append(
                (command, kwargs)
            )
            with patch.object(
                workflow_app.review_workspace,
                "load_decisions",
                return_value={"clips": {"clip.mp4": {"status": "approved"}}},
            ):
                bench.maybe_auto_burn_review_batch(review)
        self.assertEqual(
            launches[0][0],
            [
                "run-job",
                "--job-id",
                "job-1",
                "--allow-burn",
                "--allow-upload",
                "--confirm-collaboration",
            ],
        )
        self.assertEqual(
            launches[0][1]["action_name"], "人工审核完成后自动烧录并上传"
        )

    def test_all_rejected_batch_is_finalized_without_upload(self):
        class Value:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        with tempfile.TemporaryDirectory() as temporary:
            review = Path(temporary)
            bench = object.__new__(workflow_app.Workbench)
            bench.vars = {"auto_status": Value()}
            bench.auto_job_for_review_directory = lambda _review: {
                "id": "job-rejected", "status": "awaiting_delivery_review"
            }
            bench.auto_command = lambda command, extra=None: [command, *(extra or [])]
            launches = []
            bench._run_selected_auto_command = lambda command, **kwargs: launches.append(
                (command, kwargs)
            )
            with patch.object(
                workflow_app.review_workspace,
                "load_decisions",
                return_value={"clips": {"clip.mp4": {"status": "skipped"}}},
            ):
                bench.maybe_auto_burn_review_batch(review)
        self.assertEqual(
            launches[0][0],
            ["run-job", "--job-id", "job-rejected", "--allow-burn"],
        )
        self.assertNotIn("--allow-upload", launches[0][0])
        self.assertIn("不会烧录或投稿", bench.vars["auto_status"].value)


    def test_finished_review_batch_resumes_upload_after_burn(self):
        class Value:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        with tempfile.TemporaryDirectory() as temporary:
            review = Path(temporary)
            (review / "clip.mp4").write_bytes(b"video")
            bench = object.__new__(workflow_app.Workbench)
            bench.vars = {"auto_status": Value()}
            bench.auto_job_for_review_directory = lambda _review: {
                "id": "job-1", "status": "ready_to_publish"
            }
            bench.auto_command = lambda command, extra=None: [command, *(extra or [])]
            launches = []
            bench._run_selected_auto_command = lambda command, **kwargs: launches.append(
                (command, kwargs)
            )
            with patch.object(
                workflow_app.review_workspace,
                "load_decisions",
                return_value={"clips": {"clip.mp4": {"status": "approved"}}},
            ):
                bench.maybe_auto_burn_review_batch(review)
        self.assertIn("--allow-upload", launches[0][0])
        self.assertIn("烧录、媒体审计和投稿", bench.vars["auto_status"].value)

    def test_review_batch_continuation_waits_for_undo_grace_period(self):
        class Value:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        bench = object.__new__(workflow_app.Workbench)
        bench.vars = {"auto_status": Value()}
        scheduled = []
        cancelled = []

        def after(delay, callback):
            scheduled.append((delay, callback))
            return "timer-1"

        bench.after = after
        bench.after_cancel = cancelled.append
        review = Path("D:/review")
        bench._schedule_review_batch_continuation(review)

        self.assertEqual(
            scheduled[0][0], workflow_app.REVIEW_APPROVAL_UNDO_GRACE_MS
        )
        self.assertIn("8 秒内", bench.vars["auto_status"].value)
        self.assertTrue(bench._cancel_scheduled_review_continuation(review))
        self.assertEqual(cancelled, ["timer-1"])

    def test_review_undo_is_blocked_after_burn_is_ready_to_publish(self):
        bench = object.__new__(workflow_app.Workbench)
        bench._review_burn_requests = {}
        bench.auto_job_for_review_directory = lambda _review: {
            "id": "job-1",
            "status": "ready_to_publish",
        }

        started, stage = bench._review_batch_has_started(Path("D:/review"))

        self.assertTrue(started)
        self.assertEqual(stage, "ready_to_publish")

    def test_undo_last_review_approval_restores_and_reselects_clip(self):
        class Value:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        with tempfile.TemporaryDirectory() as temporary:
            review = Path(temporary)
            video = review / "clip.mp4"
            video.write_bytes(b"video")
            state = workflow_app.review_workspace.set_decision(
                review,
                video.name,
                "approved",
                record_undo=True,
            )
            entry = dict(
                state[workflow_app.review_workspace.DECISION_UNDO_HISTORY_KEY][-1]
            )
            entry["review_dir"] = str(review)
            bench = object.__new__(workflow_app.Workbench)
            bench._last_review_approval_undo = entry
            bench._review_burn_requests = {}
            bench._review_continuation_after_ids = {
                str(review.resolve()): "timer-1"
            }
            bench.after_cancel = lambda _timer: None
            bench.auto_job_for_review_directory = lambda _review: None
            bench.review_global_mode = True
            selected = []
            bench.load_global_review_pool = lambda **kwargs: selected.append(
                kwargs["select_video_path"]
            )
            bench.vars = {
                "review_line_status": Value(),
                "auto_status": Value(),
            }

            bench.undo_last_review_approval()

            decision = workflow_app.review_workspace.load_decisions(
                review, create=False
            )["clips"][video.name]
            self.assertEqual(decision["status"], "pending")
            self.assertEqual(selected, [str(video)])
            self.assertIn("恢复为待审核", bench.vars["review_line_status"].value)
            self.assertIsNone(bench._last_review_approval_undo)


class SubtitleSelectionRegressionTests(unittest.TestCase):
    class Tree:
        def __init__(self):
            self.selected = ("8",)
            self.focused = ""
            self.seen = ""

        def selection(self):
            return self.selected

        def selection_set(self, iid):
            self.selected = (str(iid),)

        def focus(self, iid):
            self.focused = str(iid)

        def see(self, iid):
            self.seen = str(iid)

    def test_failed_autosave_restores_subtitle_row_loaded_in_editor(self):
        bench = object.__new__(workflow_app.Workbench)
        bench.review_subtitle_tree = self.Tree()
        bench._review_edit_line = 7
        bench._flush_pending_subtitle_autosave = lambda: False
        bench.review_dialogue_rows = [
            {"line_number": 7},
            {"line_number": 8},
        ]

        bench.review_subtitle_selected()

        self.assertEqual(bench.review_subtitle_tree.selection(), ("7",))
        self.assertEqual(bench.review_subtitle_tree.focused, "7")
        self.assertEqual(bench.review_subtitle_tree.seen, "7")
        self.assertEqual(bench._review_edit_line, 7)

    def test_successful_autosave_reload_keeps_the_clicked_subtitle_selected(self):
        class Value:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class Editor:
            def __init__(self):
                self.value = ""

            def delete(self, _start, _end):
                self.value = ""

            def insert(self, _start, value):
                self.value = value

            def edit_modified(self, _value):
                return None

        bench = object.__new__(workflow_app.Workbench)
        bench.review_subtitle_tree = self.Tree()
        bench._review_edit_line = 7

        def flush_and_rebuild():
            bench.review_subtitle_tree.selection_set("7")
            return True

        bench._flush_pending_subtitle_autosave = flush_and_rebuild
        bench.review_dialogue_rows = [
            {
                "line_number": 8,
                "start": "0:00:12.00",
                "end": "0:00:14.00",
                "name": "",
                "text": "新选中的字幕",
            }
        ]
        bench.vars = {
            "review_start": Value(),
            "review_end": Value(),
            "review_speaker": Value(),
        }
        bench.review_text_editor = Editor()
        bench._subtitle_loading = False
        bench._speaker_label_for_name = lambda _name: "待确认"
        bench.draw_review_waveform = lambda: None
        bench.timeline_selection_kind = None

        bench.review_subtitle_selected()

        self.assertEqual(bench.review_subtitle_tree.selection(), ("8",))
        self.assertEqual(bench._review_edit_line, 8)
        self.assertEqual(bench.review_text_editor.value, "新选中的字幕")
    def test_deleted_stale_ass_line_no_longer_blocks_next_click(self):
        bench = object.__new__(workflow_app.Workbench)
        bench._subtitle_autosave_after = None
        bench._subtitle_autosave_busy = False
        bench.current_review_ass = Path("D:/review/clip.ass")
        bench._review_edit_line = 7
        bench.review_dialogue_rows = []

        self.assertTrue(bench._autosave_subtitle_line(7))
        self.assertIsNone(bench._review_edit_line)

    def test_quick_clip_command_coexists_with_monitor(self):
        command = [
            "python",
            "workflow_auto.py",
            "--config",
            "workflow-auto.json",
            "quick-clip",
            "--job-id",
            "live-1",
        ]
        self.assertEqual(workflow_app.command_flow_key(command), "manual:flow1")


class SubtitleEditorHistoryTests(unittest.TestCase):
    class Value:
        def __init__(self):
            self.value = ""

        def set(self, value):
            self.value = value

    class Tree:
        def __init__(self, selected=("5",)):
            self.selected = selected

        def selection(self):
            return self.selected

    def test_unknown_detected_speaker_remains_visible_and_editable(self):
        bench = object.__new__(workflow_app.Workbench)
        bench.profile_labels = {"awu — 哎小呜": "awu"}
        bench.profiles = {
            "awu": {
                "display_name": "哎小呜",
                "title_tag": "哎小呜",
            }
        }

        self.assertEqual(bench._speaker_label_for_name("哎小呜"), "awu — 哎小呜")
        self.assertEqual(bench._speaker_label_for_name("嘉宾甲"), "嘉宾甲")

    def test_editor_state_restores_caret_and_selection_after_reload(self):
        class Editor:
            def __init__(self):
                self.insert_at = "1.4"
                self.selection = ("1.2", "1.5")
                self.seen = ""

            def index(self, _name):
                return self.insert_at

            def tag_ranges(self, _name):
                return self.selection

            def tag_remove(self, *_args):
                self.selection = ()

            def tag_add(self, _name, start, end):
                self.selection = (str(start), str(end))

            def mark_set(self, _name, value):
                self.insert_at = str(value)

            def see(self, value):
                self.seen = str(value)

        bench = object.__new__(workflow_app.Workbench)
        bench.review_text_editor = Editor()
        state = bench._capture_subtitle_editor_state()
        bench.review_text_editor.insert_at = "1.99"
        bench.review_text_editor.selection = ()

        bench._restore_subtitle_editor_state(state)

        self.assertEqual(bench.review_text_editor.insert_at, "1.4")
        self.assertEqual(bench.review_text_editor.selection, ("1.2", "1.5"))
        self.assertEqual(bench.review_text_editor.seen, "1.4")
    def test_preserving_reload_does_not_rewrite_active_subtitle_editor(self):
        class Tree:
            def __init__(self):
                self.children = ["old"]
                self.selected = ("7",)

            def selection(self):
                return self.selected

            def get_children(self):
                return tuple(self.children)

            def delete(self, *_items):
                self.children = []

            def insert(self, _parent, _position, *, iid, values):
                self.children.append(str(iid))

            def selection_set(self, iid):
                self.selected = (str(iid),)

            def focus(self, _iid):
                return None

            def see(self, _iid):
                return None

        with tempfile.TemporaryDirectory() as temporary:
            ass = Path(temporary) / "clip.ass"
            ass.write_text("[Events]\n", encoding="utf-8-sig")
            bench = object.__new__(workflow_app.Workbench)
            bench.current_review_ass = ass
            bench.review_subtitle_tree = Tree()
            bench.review_dialogue_rows = []
            bench.refresh_review_speaker_choices = lambda: None
            bench.refresh_review_collaboration_status = lambda: None
            editor_reloads = []
            bench.review_subtitle_selected = lambda: editor_reloads.append(True)
            rows = [{
                "line_number": 7,
                "name": "",
                "start": "0:00:01.00",
                "end": "0:00:02.00",
                "text": "光标留在这里",
            }]

            with patch.object(
                workflow_app.review_workspace, "read_dialogues", return_value=rows
            ):
                bench.reload_current_ass_dialogues("7", preserve_editor=True)

        self.assertEqual(editor_reloads, [])
        self.assertEqual(bench.review_subtitle_tree.selection(), ("7",))

    def test_synthetic_same_row_selection_does_not_reload_editor(self):
        class Tree:
            def selection(self):
                return ("7",)

        bench = object.__new__(workflow_app.Workbench)
        bench.review_subtitle_tree = Tree()
        bench._review_edit_line = 7
        bench._subtitle_preserve_editor_line = 7
        bench.timeline_selection_kind = None
        draws = []
        bench.draw_review_waveform = lambda: draws.append(True)

        bench.review_subtitle_selected()

        self.assertEqual(bench._review_edit_line, 7)
        self.assertEqual(bench.timeline_selection_kind, "subtitle")
        self.assertEqual(draws, [True])

    def test_autosave_refresh_updates_tree_without_rebuilding_editor_row(self):
        class Tree:
            def __init__(self):
                self.values = {}

            def exists(self, iid):
                return iid == "7"

            def item(self, iid, *, values):
                self.values[iid] = values

        bench = object.__new__(workflow_app.Workbench)
        bench.current_review_ass = Path("D:/review/clip.ass")
        bench.review_dialogue_rows = [{"line_number": 7}]
        bench.review_subtitle_tree = Tree()
        bench.refresh_review_speaker_choices = lambda: None
        bench.refresh_review_collaboration_status = lambda: None
        bench.reload_current_ass_dialogues = lambda *_args, **_kwargs: self.fail(
            "ordinary text autosave must not rebuild the subtitle tree"
        )
        rows = [{
            "line_number": 7,
            "name": "主播",
            "start": "0:00:01.00",
            "end": "0:00:02.00",
            "text": "光标仍在原位",
        }]

        with patch.object(
            workflow_app.review_workspace, "read_dialogues", return_value=rows
        ):
            bench._refresh_subtitle_rows_after_autosave(7)

        self.assertEqual(
            bench.review_subtitle_tree.values["7"][-1], "光标仍在原位"
        )

    def test_apply_timeline_button_is_enabled_only_for_pending_edit(self):
        class Button:
            def __init__(self):
                self.options = {}

            def configure(self, **kwargs):
                self.options.update(kwargs)

        bench = object.__new__(workflow_app.Workbench)
        bench.timeline_apply_button = Button()
        bench.timeline_rendering = False
        bench.current_review_video = Path("D:/review/clip.mp4")
        bench.timeline_state = {"status": "clean"}

        bench.refresh_timeline_apply_button()
        self.assertEqual(bench.timeline_apply_button.options["state"], "disabled")

        bench.timeline_state["status"] = "pending"
        bench.refresh_timeline_apply_button()
        self.assertEqual(bench.timeline_apply_button.options["state"], "normal")

        bench.timeline_state["status"] = "applied"
        bench.refresh_timeline_apply_button()
        self.assertEqual(bench.timeline_apply_button.options["state"], "disabled")

    def test_deleted_subtitle_can_be_undone_and_redone(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "clip.mp4"
            ass = root / "clip.ass"
            video.write_bytes(b"video")
            metadata = workflow_app.review_workspace.review_proxy_metadata_path(video)
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_text('{"marker":"before"}', encoding="utf-8")
            ass.write_text(
                "[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
                "MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,恢复我\n",
                encoding="utf-8-sig",
            )
            bench = object.__new__(workflow_app.Workbench)
            bench.current_review_video = video
            bench.current_review_ass = ass
            bench.timeline_state = {
                "status": "clean",
                "source_duration": 10.0,
                "segments": [{"source_start": 0.0, "source_end": 10.0}],
            }
            key = str(video.resolve())
            bench.timeline_state_by_video = {key: bench.timeline_state}
            bench.timeline_selected_segment = None
            bench.timeline_undo = []
            bench.timeline_redo = []
            bench.timeline_rendering = False
            bench.review_subtitle_tree = self.Tree()
            bench._review_edit_line = 5
            bench.ensure_timeline_state = lambda: None
            bench.reload_current_ass_dialogues = lambda _preferred="": None
            bench.draw_review_waveform = lambda: None
            bench.vars = {"review_line_status": self.Value()}

            bench.push_timeline_undo(mark_video_edit=False)
            workflow_app.review_workspace.delete_dialogue(ass, 3)
            metadata.write_text('{"marker":"after"}', encoding="utf-8")
            with patch.object(
                workflow_app.review_workspace,
                "sync_review_proxy_ass_to_original",
                return_value=True,
            ):
                bench.undo_timeline_action()

            self.assertIn("恢复我", ass.read_text(encoding="utf-8-sig"))
            self.assertIn("before", metadata.read_text(encoding="utf-8"))
            self.assertEqual(len(bench.timeline_redo), 1)

            with patch.object(
                workflow_app.review_workspace,
                "sync_review_proxy_ass_to_original",
                return_value=True,
            ):
                bench.redo_timeline_action()

            self.assertNotIn("恢复我", ass.read_text(encoding="utf-8-sig"))
            self.assertIn("after", metadata.read_text(encoding="utf-8"))

    def test_timeline_initializes_from_stream_metadata_before_waveform(self):
        bench = object.__new__(workflow_app.Workbench)
        bench.current_review_video = Path("D:/review/clip.mp4")
        bench.review_waveform_data = None
        bench.timeline_state_by_video = {}
        bench.timeline_undo_by_video = {}
        bench.timeline_redo_by_video = {}
        bench.timeline_selected_segment = None
        with patch.object(
            workflow_app.review_workspace,
            "review_proxy_metadata",
            return_value={"source_duration": 100.0},
        ), patch.object(
            workflow_app.review_workspace,
            "load_timeline_edit",
            return_value={
                "status": "clean",
                "segments": [{"source_start": 60.0, "source_end": 70.0}],
            },
        ):
            bench.ensure_timeline_state()

        self.assertEqual(
            bench.timeline_segments()[0]["source_start"], 60.0
        )

class ReviewClipClickRegressionTests(unittest.TestCase):
    def test_mouse_click_reloads_highlighted_review_row_and_starts_preview(self):
        class Tree:
            def __init__(self):
                self.selected = ("review-1",)

            def identify_row(self, _y):
                return "review-1"

            def selection(self):
                return self.selected

            def selection_set(self, iid):
                self.selected = (iid,)

            def focus(self, _iid):
                return None

            def see(self, _iid):
                return None

        bench = object.__new__(workflow_app.Workbench)
        bench.review_clip_tree = Tree()
        video = Path("D:/review/clip.mp4")
        bench.review_item_map = {"review-1": {"video": video}}
        bench.current_review_video = None
        loaded = []
        played = []

        def load():
            loaded.append(video)
            bench.current_review_video = video

        bench.review_clip_selected = load
        bench.play_current_embedded = lambda: played.append(video)
        bench.after_idle = lambda callback: callback()
        event = type("Event", (), {"y": 12})()

        bench.review_clip_clicked(event)

        self.assertEqual(loaded, [video])
        self.assertEqual(played, [])

        bench.review_clip_clicked(event)
        self.assertEqual(played, [video])

if __name__ == "__main__":
    unittest.main()


