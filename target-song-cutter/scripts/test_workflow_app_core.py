#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

import workflow_app_core as app


class WorkflowAppCoreTests(unittest.TestCase):
    def test_only_successfully_recoverable_flow2_policy_errors_are_deferred(self):
        line = (
            "ERROR codex_core::tools::router: exec_command rejected: "
            "blocked by policy"
        )
        self.assertTrue(
            app.transient_flow2_router_error("flow2-codex-selection-1", line)
        )
        self.assertFalse(app.transient_flow2_router_error("flow1-transcription", line))
        self.assertFalse(
            app.transient_flow2_router_error(
                "flow2-codex-selection-1", "ERROR: model request failed"
            )
        )

    def test_parse_clock_accepts_seconds_and_common_clocks(self):
        self.assertEqual(app.parse_clock(12.5), 12.5)
        self.assertEqual(app.parse_clock("01:02.500"), 62.5)
        self.assertEqual(app.parse_clock("01:02:03,250"), 3723.25)

    def test_script_commands_force_utf8_for_chinese_hotwords(self):
        command = app.script_command("content_slicer.py", "枝堇,小粉螈")
        self.assertEqual(command[1:3], ["-X", "utf8"])
        self.assertEqual(command[-1], "枝堇,小粉螈")

    def test_external_process_round_trips_chinese_output_as_utf8(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app.run_external(
                root,
                "utf8-smoke",
                [sys.executable, "-c", "print('枝堇，小粉螈')"],
            )
            log = next((root / "logs").glob("*-utf8-smoke.log"))
            self.assertIn("枝堇，小粉螈", log.read_text(encoding="utf-8"))

    def test_failed_stage_includes_redacted_actual_error(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(app.WorkflowError) as caught:
                app.run_external(
                    Path(temp), "flow3-authoritative-subtitles",
                    [sys.executable, "-c", "import sys; print('Error: missing word timing cookie=secret'); sys.exit(2)"],
                )
            self.assertIn("missing word timing", str(caught.exception))
            self.assertNotIn("secret", str(caught.exception))
            self.assertIn("日志", str(caught.exception))

    def test_external_process_accepts_utf8_stdin_without_command_line_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app.run_external(
                root,
                "stdin-smoke",
                [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
                stdin_text="羽啾的芙娅之魂选片 Prompt",
            )
            log = next((root / "logs").glob("*-stdin-smoke.log"))
            self.assertIn("羽啾的芙娅之魂选片 Prompt", log.read_text(encoding="utf-8"))

    def test_normalize_selection_adds_creator_and_keeps_non_contiguous_order(self):
        payload = [
            {
                "标题": "枝堇一本正经立人设后被自己当场戳穿",
                "副标题": "最速崩塌｜当场自爆",
                "时间戳": [
                    {"开始": "00:02:00", "结束": "00:02:12"},
                    {"开始": "00:00:30", "结束": "00:01:00"},
                ],
            }
        ]
        items = app.normalize_selection(payload, "sumire", "narrative")
        self.assertTrue(items[0]["title"].startswith("【枝堇Sumire】"))
        self.assertEqual(items[0]["cover_text_primary"], "最速崩塌")
        self.assertEqual(items[0]["timestamps"][1]["start_seconds"], 30.0)
        canonical = app.canonical_selection(items)
        self.assertEqual(
            set(canonical[0]),
            {
                "标题", "副标题", "时间戳", "多人类型", "参与者",
                "说话人依据", "嘉宾台词已核实", "叙事结构",
            },
        )

    def test_collaboration_metadata_resolves_profiles_but_stays_unverified(self):
        payload = [{
            "标题": "枝堇与哎小呜连麦讨论新企划",
            "副标题": "两人讨论企划｜现场互相回应",
            "时间戳": [{"开始": "00:01:00", "结束": "00:01:40"}],
            "多人类型": "多人连麦",
            "参与者": ["枝堇", "哎小呜"],
            "说话人依据": "01:10 两人互相回应",
            "嘉宾台词已核实": True,
        }]
        item = app.normalize_selection(payload, "sumire", "narrative")[0]
        self.assertEqual(item["collaboration_type"], "collaboration")
        self.assertEqual(item["participant_profile_keys"][:2], ["sumire", "awu"])
        self.assertIn("哎小呜", item["participants"])
        self.assertFalse(item["guest_dialogue_verified"])
        self.assertIn("枝堇", app.collaboration_description(item))
        self.assertIn("哎小呜", app.collaboration_description(item))

    def test_append_manual_selection_preserves_existing_review_until_regenerated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            source.write_bytes(b"media")
            state_path = app.init_project(
                root / "project", source, None,
                "kioi", "narrative", "large-v3-turbo", "cpu", "int8",
                speaker_count=4,
            )
            _, initialized = app.load_project(state_path)
            self.assertEqual(initialized["config"]["asr"]["speaker_count"], 4)
            first = app.normalize_selection(
                [{
                    "标题": "柚雨先发现了一条旧素材",
                    "副标题": "发现旧素材｜回看才想起来",
                    "时间戳": [[10, 40]],
                }],
                "kioi",
                "narrative",
            )
            selection = state_path.parent / "selection" / "selection.json"
            app.atomic_json(selection, app.canonical_selection(first, "narrative"))
            _, state = app.load_project(state_path)
            state["selection_file"] = str(selection)
            state["active_review_dir"] = str(root / "old-review")
            app.save_project(state_path, state)

            target, item = app.append_manual_selection(
                state_path.parent,
                title="柚雨听到美国人能飞，世界观当场碎了",
                subtitle="美国人能飞｜世界观碎了😅",
                timestamps=[{"开始秒": 9092.74, "结束秒": 9135.36}],
                participants=["柚雨Kioi", "米汀Nagisa", "克罗雅Kloa", "雾深Girimi"],
                collaboration_type="collaboration",
            )

            payload = json.loads(target.read_text(encoding="utf-8-sig"))
            self.assertEqual(len(payload), 2)
            self.assertEqual(payload[-1]["参与者"][1:], [
                "米汀Nagisa", "克罗雅Kloa", "雾深Girimi",
            ])
            self.assertEqual(item["clip_id"], "002")
            _, updated = app.load_project(state_path)
            self.assertEqual(updated["active_review_dir"], str(root / "old-review"))
            self.assertEqual(updated["steps"]["flow3"]["status"], "pending")

    def test_normalize_selection_rejects_short_narrative(self):
        with self.assertRaisesRegex(app.WorkflowError, "30–300"):
            app.normalize_selection(
                [{"标题": "完整事件", "副标题": "完整故事", "时间戳": [[1, 10]]}],
                "sumire",
                "narrative",
            )

    def test_narrative_cover_copy_requires_explicit_phrase_separators(self):
        primary, secondary = app.split_cover_copy(
            "本音被嫌弃｜当场被骗", "narrative"
        )
        self.assertEqual(primary, "本音被嫌弃")
        self.assertEqual(secondary, "当场被骗")
        with self.assertRaisesRegex(app.WorkflowError, "2–3 句"):
            app.split_cover_copy("本音被嫌弃，当场被骗", "narrative")

    def test_narrative_cover_copy_preserves_manual_wrap_inside_regions(self):
        primary, secondary = app.split_cover_copy(
            "十二点路灯全关\n四周一个人都没有｜枝堇抱着凳子走\n求弹幕陪她说话😅",
            "narrative",
        )
        self.assertEqual(primary, "十二点路灯全关\n四周一个人都没有")
        self.assertEqual(secondary, "枝堇抱着凳子走\n求弹幕陪她说话😅")

    def test_narrative_cover_copy_accepts_two_lines_and_one_emoji(self):
        primary, secondary = app.split_cover_copy(
            "念完怪台词｜自己先破防😅", "narrative"
        )
        self.assertEqual(primary, "念完怪台词")
        self.assertEqual(secondary, "自己先破防😅")
        with self.assertRaisesRegex(app.WorkflowError, "最多使用 1 个"):
            app.split_cover_copy("这句话笑了😁｜她也笑了🤣", "narrative")
        with self.assertRaisesRegex(app.WorkflowError, "只能使用这些 emoji"):
            app.split_cover_copy("这句话用了｜错误表情😀", "narrative")

    def test_compact_context_expands_and_merges_story_neighborhoods(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_csv = root / "transcript.csv"
            with source_csv.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["start_seconds", "end_seconds", "text"]
                )
                writer.writeheader()
                for start in range(0, 601, 10):
                    writer.writerow({
                        "start_seconds": start,
                        "end_seconds": start + 5,
                        "text": f"普通内容{start}",
                    })
            engagement = root / "engagement"
            engagement.mkdir()
            with (engagement / "engagement-hotspots.csv").open(
                "w", encoding="utf-8-sig", newline=""
            ) as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["cause_search_start", "payoff_search_end"],
                )
                writer.writeheader()
                writer.writerow({"cause_search_start": 300, "payoff_search_end": 330})
                writer.writerow({"cause_search_start": 320, "payoff_search_end": 350})
            context_csv = root / "selection-context.csv"
            stats = app.build_compact_selection_context(
                source_csv,
                engagement,
                context_csv,
                mode="narrative",
                max_context_seconds=400,
                max_source_seconds=600,
            )
            self.assertEqual(
                stats["context_padding_seconds"], {"before": 120, "after": 180}
            )
            self.assertEqual(len(stats["time_ranges"]), 1)
            self.assertLessEqual(stats["time_ranges"][0]["start_seconds"], 180)
            self.assertGreaterEqual(stats["time_ranges"][0]["end_seconds"], 530)
            self.assertLessEqual(stats["selected_unique_seconds"], 400)

    def test_prompt_and_template_are_file_backed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mp4"
            source.write_bytes(b"media")
            state_path = app.init_project(
                root / "project", source, None,
                "sumire", "narrative", "large-v3-turbo", "cpu", "int8",
            )
            _, initial_state = app.load_project(state_path)
            self.assertNotIn("audio", initial_state["config"])
            initial_state["config"]["audio"] = str(root / "obsolete-override.wav")
            app.atomic_json(state_path, initial_state)
            _, migrated_state = app.load_project(state_path)
            self.assertNotIn("audio", migrated_state["config"])
            transcript = state_path.parent / "analysis" / "transcript"
            transcript.mkdir(parents=True)
            with (transcript / "transcript.filtered.csv").open(
                "w", encoding="utf-8-sig", newline=""
            ) as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["start_seconds", "end_seconds", "text"]
                )
                writer.writeheader()
                writer.writerow({"start_seconds": "1", "end_seconds": "2", "text": "测试"})
            _, state = app.load_project(state_path)
            prompt, context, template = app.build_selection_prompt(state_path, state)
            prompt_text = prompt.read_text(encoding="utf-8")
            self.assertIn("唯一允许的输出格式", prompt_text)
            self.assertIn("先判断值不值得切", prompt_text)
            self.assertIn("把短爆点扩成完整叙事", prompt_text)
            self.assertIn("沿同一话题向前找", prompt_text)
            self.assertIn("数量由本场有效内容自然决定", prompt_text)
            self.assertIn("不强制出现“结果／最后／却”", prompt_text)
            self.assertIn("标题只概括已经确认完整的故事岛", prompt_text)
            self.assertIn("具体对象的问题", prompt_text)
            self.assertIn("前至少 120 秒和后至少 180 秒", prompt_text)
            self.assertIn(
                "审核台可继续沿整场源视频精剪", prompt_text
            )
            self.assertIn("在输出 JSON 前先在内部做三遍处理", prompt_text)
            self.assertIn("一律只写“（谢谢SC）”", prompt_text)
            self.assertIn("起因 → 变化／升级 → 结果或反应", prompt_text)
            self.assertIn("不能只留在审核代理的上下文里", prompt_text)
            self.assertIn("首句必须让陌生观众知道代词和话题指什么", prompt_text)
            self.assertIn("倒叙与跨场回调", prompt_text)
            self.assertIn("后段冷开场", prompt_text)
            self.assertIn("时间戳数组必须按最终成片播放顺序", prompt_text)
            self.assertIn("旧场视频不在当前 selection 文件夹", prompt_text)
            self.assertIn("封面文案就是输出字段“副标题”", prompt_text)
            self.assertNotIn("不要把爆点前置", prompt_text)
            self.assertNotIn("3–5 分候选", prompt_text)
            self.assertTrue(context.is_file())
            payload = json.loads(template.read_text(encoding="utf-8"))
            self.assertEqual(
                set(payload[0]),
                {
                    "标题", "副标题", "时间戳", "标签", "标签依据", "vr_topic",
                    "多人类型", "参与者", "说话人依据", "嘉宾台词已核实", "叙事结构",
                },
            )
            self.assertEqual(len(payload[0]["标签"]), 5)
            self.assertIn("Tag 生成门禁", prompt_text)
            self.assertIn("程序会在发布时自动加入固定标签", prompt_text)
            self.assertIn("封面的上区和下区文案", prompt_text)
            self.assertIn("无方框描边文字", prompt_text)
            self.assertIn("😋 表示可爱或美味", prompt_text)
            self.assertIn("没有本人素材就不贴图", prompt_text)
            self.assertNotIn("程序会把这些词块", prompt_text)
            self.assertIn("中文简称“枝堇”", prompt_text)
            self.assertIn("不得用“她”“他”“TA”", prompt_text)
            self.assertIn("枝堇说要露出肚皮", prompt_text)
            self.assertNotIn("芙娅之魂回火测试｜游戏联动选片专属规则", prompt_text)

            state["config"].update({
                "campaign_name": "芙娅之魂回火测试",
                "campaign_selection_prompt": (
                    "references/campaign-prompts/fuya-soul-reforging-game.md"
                ),
            })
            campaign_prompt, _, _ = app.build_selection_prompt(state_path, state)
            campaign_prompt_text = campaign_prompt.read_text(encoding="utf-8")
            self.assertIn(
                "本场活动专属选片规则：芙娅之魂回火测试",
                campaign_prompt_text,
            )
            self.assertIn("以游戏事件链为最小单位", campaign_prompt_text)
            self.assertIn(
                "目标／规则／计划 → 尝试或操作 → 局势变化 → 结果与反应",
                campaign_prompt_text,
            )
            self.assertIn("活动名和主播完整 ID", campaign_prompt_text)

    def test_model_authored_tags_round_trip_into_canonical_selection(self):
        payload = [{
            "标题": "刘备文学网站名单当场说漏嘴",
            "副标题": "嘴上不看｜名单全会",
            "时间戳": [{"开始": "00:01:00", "结束": "00:01:40"}],
            "标签": ["刘备文学", "老福特", "绿色网站", "粉色网站", "网络文学"],
            "标签依据": "刘备文学、老福特=01:10直接提及；其余标签=01:00-01:40网站话题",
        }]
        items = app.normalize_selection(payload, "sumire", "narrative")
        self.assertEqual(items[0]["publish_tags"], payload[0]["标签"])
        self.assertEqual(app.derive_tags(items[0], "narrative"), payload[0]["标签"])
        canonical = app.canonical_selection(items, "narrative")
        self.assertEqual(canonical[0]["标签"], payload[0]["标签"])
        self.assertEqual(canonical[0]["标签依据"], payload[0]["标签依据"])

    def test_boundary_audit_writer_creates_new_run_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "runs" / "prepare-new" / "narrative-boundary-expansion.csv"
            app.write_narrative_boundary_audit(
                path,
                [{
                    "clip_id": "001",
                    "pre_added_seconds": 20.0,
                    "post_added_seconds": 30.0,
                    "before_seconds": 60.0,
                    "review_draft_seconds": 110.0,
                }],
            )
            self.assertTrue(path.is_file())

    def test_narrative_review_ranges_add_story_handles_without_touching_song(self):
        with tempfile.TemporaryDirectory() as temp:
            transcript = Path(temp) / "transcript.csv"
            with transcript.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["start_seconds", "end_seconds", "text"]
                )
                writer.writeheader()
                writer.writerow(
                    {"start_seconds": 0, "end_seconds": 500, "text": "整场"}
                )
            items = [
                {
                    "clip_id": "001",
                    "content_type": "narrative",
                    "timestamps": [{"start_seconds": 100.0, "end_seconds": 160.0}],
                },
                {
                    "clip_id": "002",
                    "content_type": "song",
                    "timestamps": [{"start_seconds": 200.0, "end_seconds": 230.0}],
                },
            ]
            expanded, audit = app.expand_narrative_review_ranges(items, transcript)
            self.assertEqual(expanded[0]["timestamps"][0]["start_seconds"], 80.0)
            self.assertEqual(expanded[0]["timestamps"][0]["end_seconds"], 190.0)
            self.assertEqual(expanded[1]["timestamps"], items[1]["timestamps"])
            self.assertEqual(audit[0]["pre_added_seconds"], 20.0)
            self.assertEqual(audit[0]["post_added_seconds"], 30.0)
            self.assertEqual(items[0]["timestamps"][0]["start_seconds"], 100.0)

    def test_narrative_review_expansion_never_exceeds_three_hundred_seconds(self):
        with tempfile.TemporaryDirectory() as temp:
            transcript = Path(temp) / "transcript.csv"
            with transcript.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["start_seconds", "end_seconds", "text"]
                )
                writer.writeheader()
                writer.writerow(
                    {"start_seconds": 0, "end_seconds": 500, "text": "整场"}
                )
            items = [{
                "clip_id": "001",
                "content_type": "narrative",
                "timestamps": [{"start_seconds": 5.0, "end_seconds": 290.0}],
            }]
            expanded, _audit = app.expand_narrative_review_ranges(items, transcript)
            span = expanded[0]["timestamps"][0]
            self.assertLessEqual(span["end_seconds"] - span["start_seconds"], 300.0)
    def test_gift_acknowledgements_are_split_out_of_edit_ranges(self):
        with tempfile.TemporaryDirectory() as temp:
            gift = Path(temp) / "gift-acknowledgements.csv"
            with gift.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["start_seconds", "end_seconds", "text"]
                )
                writer.writeheader()
                writer.writerow(
                    {"start_seconds": "6", "end_seconds": "8", "text": "谢谢灯牌"}
                )
                writer.writerow(
                    {"start_seconds": "16", "end_seconds": "19", "text": "谢谢礼物"}
                )
            items = [
                {
                    "clip_id": "001",
                    "timestamps": [{"start_seconds": 0.0, "end_seconds": 25.0}],
                }
            ]
            trimmed, audit = app.trim_items_around_gift_acknowledgements(
                items, gift
            )
            ranges = trimmed[0]["timestamps"]
            self.assertEqual(len(ranges), 3)
            self.assertLessEqual(ranges[0]["end_seconds"], 6.0)
            self.assertGreaterEqual(ranges[1]["start_seconds"], 8.0)
            self.assertLessEqual(ranges[1]["end_seconds"], 16.0)
            self.assertGreaterEqual(ranges[2]["start_seconds"], 19.0)
            self.assertEqual(audit[0]["clip_id"], "001")
            self.assertGreater(audit[0]["removed_seconds"], 5.0)

    def test_plan_and_copy_csv_match_existing_script_contracts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            items = app.normalize_selection(
                [
                    {
                        "标题": "主播尝试强行解释后被弹幕当场拆穿",
                        "副标题": "强行解释｜当场拆穿",
                        "时间戳": [{"开始秒": 10, "结束秒": 40}],
                    }
                ],
                "viridis",
                "narrative",
            )
            plan = root / "edit-plan.csv"
            app.write_edit_plan(plan, items)
            with plan.open("r", encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(set(app.PLAN_FIELDS), set(row))
            self.assertEqual(row["keep"], "1")

            slices = root / "slices.csv"
            with slices.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["file", "slice_id"])
                writer.writeheader()
                writer.writerow({"file": "001_test.mp4", "slice_id": "001"})
            copy = root / "titles-and-covers.csv"
            app.write_copy_csv(
                copy,
                items,
                slices,
                "narrative",
                campaign_tags=["芙娅之魂"],
            )
            with copy.open("r", encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(set(app.COPY_FIELDS), set(row))
            self.assertEqual(row["campaign_tags"], "芙娅之魂")
            self.assertEqual(len(row["tags"].split(",")), 5)
            self.assertTrue(row["tag_evidence"])

    def test_clip_title_keyword_infers_fuya_campaign_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            items = app.normalize_selection(
                [
                    {
                        "标题": "主播说今晚一起玩游戏",
                        "副标题": "今晚一起玩｜开心立刻开局",
                        "时间戳": [{"开始秒": 10, "结束秒": 40}],
                    }
                ],
                "hazel",
                "narrative",
            )
            slices = root / "slices.csv"
            with slices.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["file", "slice_id"])
                writer.writeheader()
                writer.writerow({"file": "001_test.mp4", "slice_id": "001"})
            copy = root / "titles-and-covers.csv"
            app.write_copy_csv(copy, items, slices, "narrative")
            with copy.open("r", encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["campaign_tags"], "芙娅之魂")

    def test_expected_confirmation_is_bound_to_delivery_count(self):
        with tempfile.TemporaryDirectory() as temp:
            delivery = Path(temp)
            with (delivery / "titles-and-covers.csv").open(
                "w", encoding="utf-8-sig", newline=""
            ) as handle:
                writer = csv.DictWriter(handle, fieldnames=["clip_id"])
                writer.writeheader()
                for index in range(1, 8):
                    writer.writerow({"clip_id": f"{index:03d}"})
            self.assertEqual(
                app.expected_confirmation({"delivery_dir": str(delivery)}),
                "发布 7 条",
            )


    def test_mixed_selection_keeps_public_three_field_groups_and_internal_types(self):
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
                    "标题": "枝堇现场演唱夏天的风",
                    "副标题": "夏天的风",
                    "时间戳": [{"开始": "00:05:00", "结束": "00:05:20"}],
                }
            ],
        }
        items = app.normalize_selection(payload, "sumire", "mixed")
        self.assertEqual([item["content_type"] for item in items], ["narrative", "song"])
        self.assertEqual(items[1]["cover_text_secondary"], "")
        canonical = app.canonical_selection(items, "mixed")
        self.assertEqual(set(canonical), {"普通切片", "歌切"})
        expected = {
            "标题", "副标题", "时间戳", "多人类型", "参与者",
            "说话人依据", "嘉宾台词已核实", "叙事结构",
        }
        self.assertEqual(set(canonical["普通切片"][0]), expected)
        self.assertEqual(set(canonical["歌切"][0]), expected)

    def test_mixed_prompt_is_one_call_two_group_template(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "radio.mp4"
            source.write_bytes(b"media")
            state_path = app.init_project(
                root / "project", source, None,
                "sumire", "mixed", "large-v3-turbo", "cpu", "int8",
            )
            transcript = state_path.parent / "analysis" / "transcript"
            transcript.mkdir(parents=True)
            with (transcript / "transcript.filtered.csv").open(
                "w", encoding="utf-8-sig", newline=""
            ) as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["start_seconds", "end_seconds", "text"]
                )
                writer.writeheader()
                writer.writerow({"start_seconds": "1", "end_seconds": "2", "text": "测试"})
            _, state = app.load_project(state_path)
            prompt, _, template = app.build_selection_prompt(state_path, state)
            prompt_text = prompt.read_text(encoding="utf-8")
            self.assertIn("混合切片（普通切片 + 歌切）", prompt_text)
            self.assertIn("不预设每类或总计条数", prompt_text)
            payload = json.loads(template.read_text(encoding="utf-8"))
            self.assertEqual(set(payload), {"普通切片", "歌切"})

    def test_radio_layout_detects_explicit_radio_filename_without_probe(self):
        self.assertTrue(app.should_use_radio_layout(Path("主播电台回.mp4")))
        self.assertTrue(app.should_use_radio_layout(Path("late-night-radio.flv")))

    def test_radio_song_review_uses_placeholder_not_raw_asr_lyrics(self):
        with tempfile.TemporaryDirectory() as temp:
            ass = Path(temp) / "song.ass"
            ass.write_text(
                "[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:01.00,0:00:03.00,Radio,Singer,0,0,0,,错误识别歌词\n",
                encoding="utf-8",
            )
            app.write_radio_song_review_placeholder(ass, "Radio", "枝堇")
            result = ass.read_text(encoding="utf-8-sig")
            self.assertNotIn("错误识别歌词", result)
            self.assertIn("歌词待校对", result)
            self.assertIn("流程4生成正式字幕", result)

    def test_machine_publish_lyrics_fall_back_to_recognized_text(self):
        rows = app.machine_publish_lyric_rows(
            [
                {
                    "clip": "001",
                    "recognized_text": "机器识别歌词",
                    "corrected_text": "",
                    "reviewed": "no",
                },
                {
                    "clip": "001",
                    "recognized_text": "旧识别",
                    "corrected_text": "人工已改歌词",
                    "reviewed": "yes",
                },
            ]
        )
        self.assertEqual("机器识别歌词", rows[0]["corrected_text"])
        self.assertEqual("yes", rows[0]["reviewed"])
        self.assertEqual("人工已改歌词", rows[1]["corrected_text"])

    def test_step4_cli_accepts_skip_human_review(self):
        args = app.build_parser().parse_args(
            ["step4", "--project", "D:/project", "--skip-human-review"]
        )
        self.assertTrue(args.skip_human_review)
        self.assertFalse(args.confirm_reviewed)
    def test_flow3_requires_passed_full_session_transcript_before_export(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            state_path = project / "workflow-project.json"
            state = {"config": {"mode": "narrative"}}
            root = project / "analysis" / "transcript"
            root.mkdir(parents=True)
            (root / "transcript.filtered.csv").write_text(
                "start_seconds,end_seconds,text\n0,1,测试\n",
                encoding="utf-8-sig",
            )
            with self.assertRaisesRegex(app.WorkflowError, "重做流程1"):
                app.authoritative_transcript_files(state_path, state)

            app.atomic_json(root / "transcript.json", {"authoritative": True})
            app.atomic_json(root / "speech-activity.json", {"regions": []})
            app.atomic_json(
                root / "transcript-completeness.json",
                {
                    "status": "FAIL",
                    "authoritative": True,
                    "unresolved_window_count": 2,
                },
            )
            with self.assertRaisesRegex(app.WorkflowError, "不能在切片阶段补录"):
                app.authoritative_transcript_files(state_path, state)
if __name__ == "__main__":
    unittest.main()

