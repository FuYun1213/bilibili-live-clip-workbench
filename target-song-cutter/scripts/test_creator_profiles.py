#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import creator_profiles
import transcribe_review_clips
import workflow_app_core as core


class CreatorProfileTests(unittest.TestCase):
    def profile_file(self, root: Path) -> Path:
        path = root / "creator-profiles.json"
        path.write_text(
            json.dumps({"schema_version": 1, "profiles": {}}, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def test_manual_profile_saves_subtitle_template_and_disables_new_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.profile_file(Path(temp))
            key = creator_profiles.upsert_profile(
                key="new_person",
                display_name="新人物",
                room_id="123456",
                role_color="#F9A699",
                dialogue_font="SimSun",
                dialogue_font_size=76,
                radio_subtitle_size=62,
                profiles_path=path,
            )
            profile = json.loads(path.read_text(encoding="utf-8"))["profiles"][key]
            self.assertEqual(profile["role_color"], "#F9A699")
            self.assertEqual(profile["dialogue_font"], "SimSun")
            self.assertEqual(profile["dialogue_font_size"], 76)
            self.assertEqual(profile["radio_subtitle_size"], 62)
            self.assertFalse(profile["upload"]["enabled"])

    def test_unknown_room_auto_creates_one_reusable_draft(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self.profile_file(root)
            recording = root / "998877-哎小呜" / "录制-998877-20260820-010203-000-电台.flv"
            first = creator_profiles.ensure_auto_profile(
                "998877", [recording], profiles_path=path
            )
            second = creator_profiles.ensure_auto_profile(
                "998877", [recording], profiles_path=path
            )
            profile = json.loads(path.read_text(encoding="utf-8"))["profiles"][first]
            self.assertEqual(first, second)
            self.assertEqual(profile["display_name"], "哎小呜")
            self.assertEqual(profile["room_id"], "998877")
            self.assertTrue(profile["auto_created"])
            self.assertFalse(profile["upload"]["enabled"])

    def test_archive_hides_profile_and_prevents_automatic_recreation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self.profile_file(root)
            creator_profiles.upsert_profile(
                key="keep_person",
                display_name="保留人物",
                room_id="111111",
                role_color="#AABBCC",
                profiles_path=path,
            )
            creator_profiles.upsert_profile(
                key="remove_person",
                display_name="移除人物",
                room_id="222222",
                role_color="#F9A699",
                profiles_path=path,
            )
            result = creator_profiles.archive_profile(
                "remove_person", profiles_path=path
            )
            self.assertEqual(result["display_name"], "移除人物")
            profile = json.loads(path.read_text(encoding="utf-8"))["profiles"][
                "remove_person"
            ]
            self.assertTrue(profile["archived"])
            self.assertFalse(profile["upload"]["enabled"])
            recording = root / "222222-移除人物" / "recording.flv"
            self.assertEqual(
                creator_profiles.ensure_auto_profile(
                    "222222", [recording], profiles_path=path
                ),
                "",
            )
            restored = creator_profiles.upsert_profile(
                key="remove_person",
                display_name="移除人物",
                room_id="222222",
                role_color="#F9A699",
                profiles_path=path,
            )
            self.assertEqual(restored, "remove_person")
            restored_profile = json.loads(path.read_text(encoding="utf-8"))[
                "profiles"
            ][restored]
            self.assertNotIn("archived", restored_profile)

    def test_cannot_archive_last_active_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.profile_file(Path(temp))
            creator_profiles.upsert_profile(
                key="only_person",
                display_name="唯一人物",
                room_id="333333",
                role_color="#AABBCC",
                profiles_path=path,
            )
            with self.assertRaisesRegex(ValueError, "最后一个人物"):
                creator_profiles.archive_profile("only_person", profiles_path=path)

    def test_aixiaowu_profile_uses_requested_color(self):
        profile = core.load_profiles()["aixiaowu"]
        self.assertEqual(profile["display_name"], "哎小呜")
        self.assertEqual(profile["role_color"], "#F9A699")

    def test_luka_and_generic_guest_are_subtitle_only_profiles(self):
        profiles = core.load_profiles()
        expected = {
            "luka": ("鹿卡上学不迟到", "#70E000"),
            "generic_guest": ("通用嘉宾", "#D4A373"),
        }
        for key, (display_name, fill_color) in expected.items():
            with self.subTest(key=key):
                profile = profiles[key]
                self.assertEqual(profile["display_name"], display_name)
                self.assertTrue(profile["guest_only"])
                self.assertEqual(profile["room_id"], "")
                self.assertEqual(profile["subtitle_fill_color"], fill_color)
                self.assertEqual(profile["subtitle_outline_color"], "#111318")
                self.assertFalse(profile["upload"]["enabled"])
                self.assertEqual(profile["upload"]["tags"], [])

    def test_hazel_uses_neutral_gray_subtitles(self):
        profile = core.load_profiles()["hazel"]
        self.assertEqual(profile["display_name"], "灰泽满Hazel")
        self.assertEqual(profile["role_color"], "#707780")
        self.assertEqual(profile["subtitle_fill_color"], "#A0A0A0")
        self.assertEqual(profile["subtitle_outline_color"], "#111318")

    def test_profile_customizes_active_ass_style(self):
        template = (
            "[V4+ Styles]\n"
            "Style: Regular,OldFont,70,&H00FFFFFF,&H00FFFFFF,&H00000000,"
            "&H00000000,-1,0,0,0,100,100,0,0,1,3,2,2,10,10,10,1\n"
        )
        result = transcribe_review_clips.customize_ass_template(
            template,
            "Regular",
            font="SimSun",
            font_size=76,
            color="#F9A699",
        )
        self.assertIn("Style: Regular,SimSun,76", result)
        self.assertIn("&H0099A6F9", result)

    def test_profile_customizes_fill_dark_outline_and_width(self):
        template = (
            "[V4+ Styles]\n"
            "Style: Regular,OldFont,70,&H00FFFFFF,&H00FFFFFF,&H00000000,"
            "&H00000000,-1,0,0,0,100,100,0,0,1,3,2,2,10,10,10,1\n"
        )
        result = transcribe_review_clips.customize_ass_template(
            template,
            "Regular",
            primary_color="#37C8F3",
            outline_color="#111318",
            outline_width=5.0,
        )
        self.assertIn("&H00F3C837,&H00F3C837,&H00181311", result)
        self.assertIn(",1,5,2.5,2,", result)

    def test_vertical_and_square_sources_default_to_radio(self):
        original = core.media_dimensions
        try:
            core.media_dimensions = lambda _path: (1080, 1920)
            self.assertTrue(core.should_use_radio_layout(Path("mobile.mp4")))
            core.media_dimensions = lambda _path: (1080, 1080)
            self.assertTrue(core.should_use_radio_layout(Path("square.mp4")))
            core.media_dimensions = lambda _path: (1920, 1080)
            self.assertFalse(core.should_use_radio_layout(Path("landscape.mp4")))
        finally:
            core.media_dimensions = original

    def test_auto_profile_cannot_publish_until_completed(self):
        original = core.load_profiles
        core.load_profiles = lambda: {
            "draft_person": {
                "display_name": "测试草稿人物",
                "upload": {"enabled": False},
            }
        }
        try:
            state = {"config": {"creator": "draft_person"}}
            with self.assertRaisesRegex(core.WorkflowError, "新建人物草稿"):
                core.ensure_profile_publish_enabled(state)
        finally:
            core.load_profiles = original


    def test_profile_editor_can_complete_and_enable_upload_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.profile_file(Path(temp))
            key = creator_profiles.upsert_profile(
                key="publish_person",
                display_name="投稿人物",
                room_id="445566",
                role_color="#AABBCC",
                upload_enabled=True,
                upload_description="投稿简介",
                upload_tags="投稿人物,直播切片,测试",
                narrative_collection_title="投稿人物切片",
                narrative_season_id="",
                narrative_section_id="",
                song_collection_title="投稿人物歌",
                profiles_path=path,
            )
            upload = json.loads(path.read_text(encoding="utf-8"))["profiles"][
                key
            ]["upload"]
            self.assertTrue(upload["enabled"])
            self.assertEqual(["投稿人物", "直播切片", "测试"], upload["tags"])
            self.assertEqual("投稿简介", upload["description"])
            self.assertEqual(
                "投稿人物切片", upload["collections"]["narrative"]["title"]
            )
            self.assertNotIn("season_id", upload["collections"]["narrative"])

    def test_profile_cannot_enable_upload_with_incomplete_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.profile_file(Path(temp))
            with self.assertRaisesRegex(ValueError, "投稿简介"):
                creator_profiles.upsert_profile(
                    key="incomplete_person",
                    display_name="不完整人物",
                    role_color="#AABBCC",
                    upload_enabled=True,
                    upload_description="",
                    upload_tags="不完整人物",
                    narrative_collection_title="不完整人物切片",
                    profiles_path=path,
                )
    def test_character_image_is_validated_and_copied_into_profile_assets(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self.profile_file(root)
            source = root / "guest.png"
            image = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
            for x in range(8, 24):
                for y in range(6, 30):
                    image.putpixel((x, y), (255, 80, 90, 255))
            image.save(source)
            key = creator_profiles.upsert_profile(
                key="cover_guest",
                display_name="封面嘉宾",
                role_color="#AABBCC",
                cover_character_image=str(source),
                profiles_path=path,
            )
            profile = json.loads(path.read_text(encoding="utf-8"))["profiles"][key]
            managed = root / profile["cover_character_image"]
            self.assertEqual(profile["cover_character_image"], "creator-images/cover_guest.png")
            self.assertTrue(managed.is_file())
if __name__ == "__main__":
    unittest.main()
