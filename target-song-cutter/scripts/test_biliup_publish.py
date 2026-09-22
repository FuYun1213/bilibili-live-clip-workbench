import csv
import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

import biliup_publish as publish


class BiliupPublishTests(unittest.TestCase):
    def make_delivery(self, root: Path, title: str = "测试标题") -> tuple[Path, Path]:
        delivery = root / "delivery"
        delivery.mkdir()
        video = delivery / "001-test.mp4"
        video.write_bytes(b"")
        video.with_suffix(".ass").write_text("[Script Info]\n", encoding="utf-8")
        video.with_name(f"{video.stem}-cover.jpg").write_bytes(b"jpeg")
        copy_path = delivery / "titles-and-covers.csv"
        with copy_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["clip_id", "video", "title", "tags", "tag_evidence", "vr_topic"])
            writer.writeheader()
            writer.writerow({"clip_id": "001", "video": video.name, "title": title, "tags": "测试主题,反转,直播趣事", "tag_evidence": "测试素材中的主题、反转和趣事", "vr_topic": ""})
        return delivery, copy_path

    def test_default_cookie_uses_local_app_data(self):
        path = publish.default_cookie_path({"LOCALAPPDATA": r"C:\Users\tester\AppData\Local"})
        self.assertIn("target-song-cutter", str(path))
        self.assertNotIn("OneDrive", str(path))

    def test_cookie_location_rejects_onedrive(self):
        with self.assertRaisesRegex(ValueError, "OneDrive"):
            publish.validate_cookie_location(Path(r"C:\Users\tester\OneDrive\cookies.json"))

    def test_description_formula_appends_fixed_creator_block(self):
        fixed = "主播固定简介\n\n个人空间：https://space.bilibili.com/1"
        self.assertEqual(
            publish.compose_description(fixed, "本条视频导语", "命令行导语"),
            f"本条视频导语\n\n{fixed}",
        )
        self.assertEqual(
            publish.compose_description(fixed, "", "命令行导语"),
            f"命令行导语\n\n{fixed}",
        )
        self.assertEqual(publish.compose_description(fixed), fixed)

    def test_description_formula_does_not_duplicate_fixed_block(self):
        fixed = "主播固定简介\n\n直播间：https://live.bilibili.com/1"
        complete = f"本条视频导语\n\n{fixed}"
        self.assertEqual(publish.compose_description(fixed, complete), complete)

    def test_import_cookie_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.json"
            destination = root / "private" / "cookies.json"
            payload = json.loads(json.dumps(publish.COOKIE_TEMPLATE))
            payload["cookie_info"]["cookies"][0]["value"] = "sess"
            payload["cookie_info"]["cookies"][1]["value"] = "csrf"
            source.write_text(json.dumps(payload), encoding="utf-8")
            result = publish.import_cookie_file(source, destination)
            self.assertEqual(result, destination.resolve())
            self.assertEqual(publish.cookie_json_status(result), "ready")

    def test_cookie_template_is_valid_but_not_ready(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "private" / "cookies.json"
            publish.create_cookie_template(destination)
            self.assertEqual(publish.cookie_json_status(destination), "template")
            publish.validate_cookie_json(destination, require_ready=False)
            with self.assertRaisesRegex(ValueError, "incomplete"):
                publish.validate_cookie_json(destination)

    def test_qr_login_credentials_are_saved_in_biliup_format(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "private" / "cookies.json"
            result = publish.save_qr_login_credentials(
                destination,
                {
                    "SESSDATA": "session-value",
                    "bili_jct": "csrf-value",
                    "DedeUserID": "12345",
                    "extra_cookie": "kept",
                },
                refresh_token="refresh-value",
            )
            payload = json.loads(result.read_text(encoding="utf-8"))
            values = {
                row["name"]: row["value"]
                for row in payload["cookie_info"]["cookies"]
            }
            self.assertEqual("ready", publish.cookie_json_status(result))
            self.assertEqual("session-value", values["SESSDATA"])
            self.assertEqual("kept", values["extra_cookie"])
            self.assertEqual(12345, payload["token_info"]["mid"])
            self.assertEqual("refresh-value", payload["token_info"]["refresh_token"])

    def test_qr_login_credentials_require_session_and_csrf(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "private" / "cookies.json"
            with self.assertRaisesRegex(ValueError, "bili_jct"):
                publish.save_qr_login_credentials(
                    destination,
                    {"SESSDATA": "session-value"},
                )
            self.assertFalse(destination.exists())

    def test_force_template_never_replaces_ready_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "private" / "cookies.json"
            payload = json.loads(json.dumps(publish.COOKIE_TEMPLATE))
            payload["cookie_info"]["cookies"][0]["value"] = "sess"
            payload["cookie_info"]["cookies"][1]["value"] = "csrf"
            destination.parent.mkdir(parents=True)
            destination.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "ready"):
                publish.create_cookie_template(destination, force=True)
            self.assertEqual(publish.cookie_json_status(destination), "ready")

    def test_load_items_requires_same_folder_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            delivery, copy_path = self.make_delivery(Path(temp))
            items = publish.load_upload_items(
                delivery=delivery,
                copy_path=copy_path,
                creator="viridis",
                tid=138,
                copyright_value=1,
                source="",
                no_reprint=1,
                default_tags="测试,切片",
                default_description="简介",
            )
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["video"].name, "001-test.mp4")
            self.assertEqual(items[0]["cover"].name, "001-test-cover.jpg")
            self.assertEqual(items[0]["tags"][0], "VirtuaReal")
            self.assertIn("测试主题", items[0]["tags"])
            self.assertIn("反转", items[0]["tags"])
            self.assertTrue(items[0]["description"].startswith("简介\n\n"))
            self.assertIn("1891335475", items[0]["description"])

    def test_campaign_tag_and_full_creator_id_reach_final_upload_tags(self):
        with tempfile.TemporaryDirectory() as temp:
            delivery, copy_path = self.make_delivery(Path(temp))
            video = delivery / "001-test.mp4"
            with copy_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "clip_id", "video", "title", "tags", "campaign_tags",
                        "tag_evidence", "vr_topic",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "clip_id": "001",
                        "video": video.name,
                        "title": "活动切片",
                        "tags": "回火测试,直播趣事",
                        "campaign_tags": "芙娅之魂",
                        "tag_evidence": "活动直播中的回火测试内容",
                        "vr_topic": "",
                    }
                )
            profiles = publish.load_profiles()
            profile = json.loads(json.dumps(profiles["harei"]))
            profile["upload"]["tags"] = [
                tag for tag in profile["upload"]["tags"]
                if tag != "芙娅之魂"
            ]
            with patch.object(publish, "load_profiles", return_value={"harei": profile}):
                item = publish.load_upload_items(
                    delivery=delivery,
                    copy_path=copy_path,
                    creator="harei",
                    tid=138,
                    copyright_value=1,
                    source="",
                    no_reprint=1,
                    default_tags="",
                    default_description="",
                )[0]
            self.assertIn("花礼Harei", item["tags"])
            self.assertIn("芙娅之魂", item["tags"])
            self.assertLess(
                item["tags"].index("花礼Harei"),
                item["tags"].index("芙娅之魂"),
            )

    def test_title_keyword_infers_fuya_tag_without_campaign_column(self):
        with tempfile.TemporaryDirectory() as temp:
            delivery, copy_path = self.make_delivery(
                Path(temp), title="主播今晚一起玩游戏"
            )
            item = publish.load_upload_items(
                delivery=delivery,
                copy_path=copy_path,
                creator="harei",
                tid=138,
                copyright_value=1,
                source="",
                no_reprint=1,
                default_tags="",
                default_description="",
            )[0]
            self.assertIn("芙娅之魂", item["tags"])

    def test_song_row_uses_music_tid_and_creator_description(self):
        with tempfile.TemporaryDirectory() as temp:
            delivery, copy_path = self.make_delivery(Path(temp))
            video = delivery / "001-test.mp4"
            ass = video.with_suffix(".ass")
            gate = video.with_name(f"{video.stem}.song-timing-gate.json")
            gate.write_text(json.dumps({
                "schema": "target-song-cutter.song-timing-gate.v1",
                "status": "PASS",
                "clip": "001",
                "video": {"name": video.name, "sha256": publish.sha256_file(video)},
                "ass": {"name": ass.name, "sha256": publish.sha256_file(ass)},
            }), encoding="utf-8")
            with copy_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["clip_id", "video", "title", "content_type", "tags", "tag_evidence"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "clip_id": "001",
                        "video": video.name,
                        "title": "song",
                        "content_type": "song",
                        "tags": "歌曲名,现场演唱,中文翻唱",
                        "tag_evidence": "成片歌曲和现场演唱内容",
                    }
                )
            item = publish.load_upload_items(
                delivery=delivery,
                copy_path=copy_path,
                creator="kioi",
                tid=27,
                song_tid=130,
                copyright_value=1,
                source="",
                no_reprint=1,
                default_tags="",
                default_description="",
            )[0]
            self.assertEqual(item["tid"], 130)
            self.assertEqual(item["tags"][0], "VirtuaReal")
            self.assertIn("歌切", item["tags"])
            self.assertIn("1096820127", item["description"])

    def test_each_creator_profile_has_distinct_description_and_links(self):
        expected = {
            "kioi": ("1096820127", "1791260756"),
            "sumire": ("1150976664", "1727076670"),
            "viridis": ("1891335475", "1727071052"),
            "yuchu": ("2138961136", "1727074031"),
            "komichi": ("1512246445", "1700301235"),
            "chilly": ("3706962250303984", "1774689965"),
        }
        profiles = publish.load_profiles()
        descriptions = []
        virtuareal_profiles = {"kioi", "sumire", "viridis", "yuchu"}
        for creator, identifiers in expected.items():
            upload = profiles[creator]["upload"]
            if creator in virtuareal_profiles:
                self.assertEqual(upload["tags"][0], "VirtuaReal")
            self.assertIn(identifiers[0], upload["description"])
            self.assertIn(identifiers[1], upload["description"])
            descriptions.append(upload["description"])
        self.assertEqual(len(descriptions), len(set(descriptions)))

    def test_all_creator_profiles_default_to_content_needs_no_label(self):
        profiles = publish.load_profiles()
        for creator, profile in profiles.items():
            with self.subTest(creator=creator):
                self.assertFalse(profile["upload"].get("no_reprint", False))

    def test_reposted_content_requires_source(self):
        with tempfile.TemporaryDirectory() as temp:
            delivery, copy_path = self.make_delivery(Path(temp))
            with self.assertRaisesRegex(ValueError, "--source"):
                publish.load_upload_items(
                    delivery=delivery,
                    copy_path=copy_path,
                    creator="viridis",
                    tid=138,
                    copyright_value=2,
                    source="",
                    no_reprint=1,
                    default_tags="测试",
                    default_description="",
                )

    def test_each_command_is_one_independent_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            delivery, copy_path = self.make_delivery(Path(temp))
            item = publish.load_upload_items(
                delivery=delivery,
                copy_path=copy_path,
                creator="viridis",
                tid=138,
                copyright_value=1,
                source="",
                no_reprint=1,
                default_tags="测试",
                default_description="",
            )[0]
            command = publish.build_upload_command(
                "biliup", Path(temp) / "cookies.json", item, limit=3
            )
            self.assertEqual(command[1], "-u")
            self.assertLess(command.index("-u"), command.index("upload"))
            self.assertEqual(command.count("upload"), 1)
            self.assertEqual(command[command.index("--submit") + 1], "web")
            self.assertEqual(command[command.index("--limit") + 1], "3")
            self.assertEqual(command[command.index("--title") + 1], "测试标题")
    def test_narrative_collection_is_attached_to_upload_command(self):
        with tempfile.TemporaryDirectory() as temp:
            delivery, copy_path = self.make_delivery(Path(temp))
            item = publish.load_upload_items(
                delivery=delivery,
                copy_path=copy_path,
                creator="viridis",
                tid=27,
                copyright_value=1,
                source="",
                no_reprint=1,
                default_tags="测试",
                default_description="",
            )[0]
            self.assertEqual(item["collection"], "小松绿切片")
            self.assertEqual(item["season_id"], 8813006)
            self.assertEqual(item["section_id"], 9824320)
            command = publish.build_upload_command(
                "biliup", Path(temp) / "cookies.json", item, limit=3
            )
            payload = json.loads(command[command.index("--extra-fields") + 1])
            self.assertEqual(payload, {"season_id": 8813006})
            bcut = publish.build_upload_command(
                "biliup",
                Path(temp) / "cookies.json",
                item,
                limit=3,
                submit="b-cut-android",
            )
            self.assertEqual(bcut[bcut.index("--submit") + 1], "b-cut-android")

    def test_default_collection_titles_use_only_chinese_creator_name(self):
        profile = {"display_name": "哎小呜Awu"}
        self.assertEqual(
            publish.default_collection_title(profile, "narrative"), "哎小呜切片"
        )
        self.assertEqual(publish.default_collection_title(profile, "song"), "哎小呜歌")

    def test_resolve_collection_by_exact_title_returns_section_route(self):
        rows = [{
            "season": {"id": 123, "title": "哎小呜切片"},
            "sections": {"sections": [
                {"id": 789, "seasonId": 123, "title": "正片", "order": 1}
            ]},
        }]
        with patch.object(publish, "list_creator_collections", return_value=rows):
            route = publish.resolve_collection_by_title("哎小呜切片", "private-cookie")
        self.assertEqual(route, {
            "title": "哎小呜切片", "season_id": 123, "section_id": 789
        })

    def test_prepare_new_creator_collections_creates_narrative_first(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profiles_path = root / "creator-profiles.json"
            profiles_path.write_text(json.dumps({
                "schema_version": 1,
                "profiles": {
                    "new_person": {
                        "display_name": "新人物New",
                        "upload": {"enabled": True, "collections": {}},
                    }
                },
            }, ensure_ascii=False), encoding="utf-8")
            cover = root / "cover.jpg"
            cover.write_bytes(b"jpeg")
            items = [{
                "clip_id": "S01",
                "cover": cover,
                "collection_kind": "song",
                "collection_explicit": False,
                "collection": "新人物歌",
                "season_id": None,
                "section_id": None,
            }]
            created = [
                {"title": "新人物切片", "season_id": 1001, "section_id": 2001},
                {"title": "新人物歌", "season_id": 1002, "section_id": 2002},
            ]
            with patch.object(
                publish, "create_or_resolve_collection", side_effect=created
            ) as create:
                routes = publish.prepare_creator_collections(
                    "new_person",
                    items,
                    root / "web-cookies.json",
                    profiles_path=profiles_path,
                )
            self.assertEqual(create.call_args_list[0].args[1], "新人物切片")
            self.assertEqual(create.call_args_list[1].args[1], "新人物歌")
            self.assertEqual(routes["narrative"]["section_id"], 2001)
            self.assertEqual(items[0]["section_id"], 2002)
            saved = json.loads(profiles_path.read_text(encoding="utf-8"))[
                "profiles"
            ]["new_person"]["upload"]["collections"]
            self.assertEqual(saved["narrative"]["season_id"], 1001)
            self.assertEqual(saved["song"]["section_id"], 2002)

    def test_execute_requires_verified_collection_id(self):
        with self.assertRaisesRegex(ValueError, "missing collection"):
            publish.require_collection_routing([
                {"clip_id": "KIO-S01", "collection": "柚雨歌", "season_id": None}
            ])

    def test_all_upload_collections_have_verified_section_ids(self):
        profiles = publish.load_profiles()
        for creator in ("kioi", "sumire", "viridis", "yuchu"):
            collections = profiles[creator]["upload"]["collections"]
            for kind in ("narrative", "song"):
                self.assertGreater(collections[kind]["season_id"], 0)
                self.assertGreater(collections[kind]["section_id"], 0)

    def test_extract_bvid_from_upload_output(self):
        output = "上传成功: {'code': 0, 'data': {'bvid': 'BV1G2b26bE4P'}}"
        self.assertEqual(publish.extract_bvid(output), "BV1G2b26bE4P")
        with self.assertRaisesRegex(RuntimeError, "no BV id"):
            publish.extract_bvid("上传成功但没有稿件编号")

    def test_execute_skips_clip_with_success_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "010.mp4"
            video.write_bytes(b"video")
            item = {
                "clip_id": "010",
                "title": "已经成功的稿件",
                "video": str(video),
                "collection": "枝堇切片",
                "section_id": 123,
            }
            (root / "publish-receipts.json").write_text(
                json.dumps({
                    "schema": "target-song-cutter.publish-receipts.v1",
                    "items": {"010": {"title": item["title"], "bvid": "BV1receipt"}},
                }),
                encoding="utf-8",
            )
            runner = Mock()
            with patch.object(publish, "ensure_collection_membership") as ensure, patch.object(
                publish, "find_existing_bvid_by_title"
            ) as find_existing, patch("builtins.print"):
                result = publish.execute_uploads(
                    "biliup",
                    root / "cookies.json",
                    [item],
                    limit=3,
                    line="auto",
                    cooldown=0,
                    collection_cookie_path=root / "web-cookies.json",
                    run=runner,
                )
            self.assertEqual(result, 0)
            runner.assert_not_called()
            find_existing.assert_not_called()
            ensure.assert_called_once_with(
                root / "web-cookies.json", 123, "BV1receipt", item["title"]
            )

    def test_flow5_throttle_persists_and_waits_remaining_ten_minutes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "flow5-throttle.json"
            for index in range(5):
                publish.record_flow5_upload_success(
                    path,
                    clip_id=f"{index + 1:03d}",
                    bvid=f"BV{index + 1}",
                    now=lambda value=1000 + index * 8: value,
                )
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["completed_in_window"], 5)
            self.assertEqual(saved["cooldown_until"], 1032 + 600)

            clock = {"value": 1332}
            waits = []

            def sleep(seconds):
                waits.append(seconds)
                clock["value"] += seconds

            state = publish.wait_for_flow5_upload_slot(
                path,
                sleep=sleep,
                now=lambda: clock["value"],
            )

            self.assertEqual(waits, [300])
            self.assertEqual(state["completed_in_window"], 0)
            self.assertEqual(state["cooldown_until"], 0)

    def test_execute_uploads_sixth_item_only_after_ten_minute_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            throttle_path = root / "shared-flow5-throttle.json"
            items = []
            for index in range(6):
                video = root / f"{index + 1:03d}.mp4"
                video.write_bytes(b"video")
                items.append(
                    {
                        "clip_id": f"{index + 1:03d}",
                        "title": f"稿件 {index + 1}",
                        "video": str(video),
                        "collection": "测试合集",
                        "section_id": 123,
                    }
                )
            clock = {"value": 1000}
            waits = []
            upload_times = []

            def sleep(seconds):
                waits.append(seconds)
                clock["value"] += seconds

            def run(*_args, **_kwargs):
                upload_times.append(clock["value"])
                return SimpleNamespace(returncode=0, stdout="ok", stderr="")

            bvids = [f"BV1TEST{i:05d}" for i in range(6)]
            with patch.object(
                publish, "find_existing_bvid_by_title", return_value=None
            ), patch.object(
                publish, "build_upload_command", return_value=["biliup"]
            ), patch.object(
                publish, "extract_bvid", side_effect=bvids
            ), patch.object(
                publish, "ensure_collection_membership"
            ), patch("builtins.print"):
                result = publish.execute_uploads(
                    "biliup",
                    root / "cookies.json",
                    items,
                    limit=3,
                    line="auto",
                    cooldown=0,
                    collection_cookie_path=root / "web-cookies.json",
                    run=run,
                    flow5_throttle_path=throttle_path,
                    sleep=sleep,
                    now=lambda: clock["value"],
                )

            self.assertEqual(result, 0)
            self.assertEqual(len(upload_times), 6)
            self.assertEqual(upload_times[5] - upload_times[4], 600)
            self.assertIn(592, waits)
            throttle = json.loads(throttle_path.read_text(encoding="utf-8"))
            self.assertEqual(throttle["completed_in_window"], 1)
            self.assertEqual(throttle["cooldown_until"], 0)

    def test_explicit_collection_title_without_ids_is_resolved_automatically(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profiles_path = root / "creator-profiles.json"
            profiles_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "profiles": {
                            "person": {
                                "display_name": "人物",
                                "upload": {
                                    "enabled": True,
                                    "collections": {
                                        "narrative": {
                                            "title": "默认切片",
                                            "season_id": 1,
                                            "section_id": 2,
                                        }
                                    },
                                },
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            cover = root / "cover.jpg"
            cover.write_bytes(b"jpeg")
            items = [
                {
                    "clip_id": "001",
                    "cover": cover,
                    "collection_kind": "narrative",
                    "collection_explicit": True,
                    "collection": "单条指定合集",
                    "season_id": None,
                    "section_id": None,
                }
            ]
            with patch.object(
                publish,
                "create_or_resolve_collection",
                return_value={
                    "title": "单条指定合集",
                    "season_id": 123,
                    "section_id": 456,
                },
            ) as resolve:
                publish.prepare_creator_collections(
                    "person",
                    items,
                    root / "web-cookies.json",
                    profiles_path=profiles_path,
                )
            resolve.assert_called_once()
            self.assertEqual(123, items[0]["season_id"])
            self.assertEqual(456, items[0]["section_id"])

    def test_preview_accepts_more_than_legacy_pacing_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            delivery = Path(temporary)
            items = [
                {"clip_id": f"{index:03d}", "title": f"标题 {index}"}
                for index in range(1, 8)
            ]
            args = SimpleNamespace(
                delivery=str(delivery),
                copy=None,
                creator="sumire",
                tid=27,
                copyright=1,
                source="",
                no_reprint=0,
                tags="",
                description="",
                only=None,
                song_tid=130,
                max_items=5,
                execute=False,
            )
            with (
                patch.object(publish, "load_upload_items", return_value=items),
                patch.object(publish, "public_plan", return_value=items),
                patch("builtins.print"),
            ):
                self.assertEqual(publish.command_upload(args), 0)
if __name__ == "__main__":
    unittest.main()
