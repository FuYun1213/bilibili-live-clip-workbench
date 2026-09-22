import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import local_publish


class LocalDeliveryCoverTests(unittest.TestCase):
    def test_shipped_profiles_pass_full_renderer_validation(self) -> None:
        self.assertEqual(
            local_publish.validate_profiles(local_publish.load_profiles()),
            [],
        )

    def test_content_tags_extend_profile_and_campaign_tags(self) -> None:
        self.assertEqual(
            local_publish.split_tags(
                "反转,直播趣事",
                ["花礼Harei", "芙娅之魂"],
            ),
            ["花礼Harei", "芙娅之魂", "反转", "直播趣事"],
        )

    def test_prepare_infers_fuya_tag_from_title(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            delivery = Path(temporary)
            video = delivery / "001-topic.mp4"
            video.write_bytes(b"test-video")
            copy = delivery / "titles-and-covers.csv"
            copy.write_text(
                "clip_id,video,title,tags\n"
                "001,001-topic.mp4,今晚一起玩游戏,回火测试\n",
                encoding="utf-8",
            )
            output = delivery / "publishing-manifest.json"
            args = argparse.Namespace(
                copy=str(copy), clips_dir=str(delivery), creator="harei",
                output=str(output), force=True,
            )
            self.assertEqual(local_publish.prepare(args), 0)
            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertIn("芙娅之魂", manifest["items"][0]["tags"])

    def test_archived_profile_does_not_block_active_cover_validation(self) -> None:
        profiles = {
            "viridis": {
                "display_name": "小松绿Viridis",
                "title_tag": "小松绿Viridis",
                "room_id": "1727071052",
                "role_color": "#8EB056",
                "subtitle_fill_color": "#4DD66E",
                "subtitle_outline_color": "#111318",
                "subtitle_outline_width": 5.0,
                "hotwords": [],
                "upload": {"tid": None},
                "dialogue_font": "Microsoft YaHei",
                "dialogue_font_size": 70,
                "radio_subtitle_size": 65,
            },
            "old_profile": {
                "archived": True,
                "role_color": "#8EB056",
            },
        }

        self.assertEqual(local_publish.validate_profiles(profiles), [])

    def test_active_profiles_still_require_unique_role_colors(self) -> None:
        base = {
            "display_name": "人物",
            "title_tag": "人物",
            "room_id": "1",
            "role_color": "#8EB056",
            "subtitle_fill_color": "#4DD66E",
            "subtitle_outline_color": "#111318",
            "subtitle_outline_width": 5.0,
            "hotwords": [],
            "upload": {"tid": None},
            "dialogue_font": "Microsoft YaHei",
            "dialogue_font_size": 70,
            "radio_subtitle_size": 65,
        }
        profiles = {
            "viridis": dict(base),
            "another": {**base, "room_id": "2"},
        }

        errors = local_publish.validate_profiles(profiles)

        self.assertIn("another: duplicate role_color with viridis", errors)
        self.assertIn(
            "another: duplicate subtitle_fill_color with viridis", errors
        )

    def test_active_subtitle_fill_colors_must_not_be_nearly_identical(self) -> None:
        base = {
            "display_name": "人物",
            "title_tag": "人物",
            "room_id": "1",
            "role_color": "#8EB056",
            "subtitle_fill_color": "#4DD66E",
            "subtitle_outline_color": "#111318",
            "subtitle_outline_width": 5.0,
            "hotwords": [],
            "upload": {"tid": None},
            "dialogue_font": "Microsoft YaHei",
            "dialogue_font_size": 70,
            "radio_subtitle_size": 65,
        }
        profiles = {
            "viridis": dict(base),
            "near_green": {
                **base,
                "room_id": "2",
                "role_color": "#123456",
                "subtitle_fill_color": "#50D870",
            },
        }
        errors = local_publish.validate_profiles(profiles)
        self.assertTrue(
            any("subtitle_fill_color is too close to viridis" in value for value in errors)
        )

    def test_find_video_resolves_explicit_filename_inside_clips_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clips = root / "clips"
            copy_dir = root / "review"
            clips.mkdir()
            copy_dir.mkdir()
            video = clips / "001-topic.mp4"
            video.write_bytes(b"test-video")

            resolved = local_publish.find_video(
                clips, "001", video.name, copy_dir
            )

            self.assertEqual(resolved, video.resolve())

    def test_render_local_places_cover_beside_video_without_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            delivery = Path(temporary) / "delivery"
            delivery.mkdir()
            video = delivery / "001-topic.mp4"
            video.write_bytes(b"test-video")
            copy = delivery / "titles-and-covers.csv"
            copy.write_text(
                "clip_id,title,cover_mode,cover_text_primary,cover_text_secondary\n"
                "001,Title,quote-impact,Primary,Secondary\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                copy=str(copy), clips_dir=str(delivery), creator="viridis", force=False
            )

            def fake_render(_item, _profile, output: Path, **_kwargs) -> None:
                output.write_bytes(b"test-cover")

            with (
                mock.patch.object(local_publish, "load_profiles", return_value={"viridis": {}}),
                mock.patch.object(local_publish, "validate_profiles", return_value=[]),
                mock.patch.object(local_publish, "extract_reference"),
                mock.patch.object(local_publish, "render_cover", side_effect=fake_render),
                mock.patch.object(local_publish, "item_errors", return_value=[]),
            ):
                result = local_publish.render_local(args)

            self.assertEqual(result, 0)
            self.assertTrue((delivery / "001-topic-cover.jpg").is_file())
            self.assertFalse((delivery / "covers").exists())
            self.assertFalse((delivery / "publishing-manifest.json").exists())

    def test_render_local_uses_reviewed_cover_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            delivery = Path(temporary) / "delivery"
            delivery.mkdir()
            (delivery / "001-topic.mp4").write_bytes(b"test-video")
            copy = delivery / "titles-and-covers.csv"
            copy.write_text(
                "clip_id,title,cover_mode,cover_text_primary,cover_time_seconds\n"
                "001,Title,quote-impact,Primary,12.75\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                copy=str(copy), clips_dir=str(delivery), creator="viridis", force=False
            )

            def fake_render(_item, _profile, output: Path, **_kwargs) -> None:
                output.write_bytes(b"test-cover")

            with (
                mock.patch.object(local_publish, "load_profiles", return_value={"viridis": {}}),
                mock.patch.object(local_publish, "validate_profiles", return_value=[]),
                mock.patch.object(local_publish, "extract_reference") as extractor,
                mock.patch.object(local_publish, "render_cover", side_effect=fake_render),
                mock.patch.object(local_publish, "item_errors", return_value=[]),
            ):
                self.assertEqual(local_publish.render_local(args), 0)

            self.assertEqual(extractor.call_count, 1)
            self.assertEqual(extractor.call_args.args[2], 12.75)

    def test_cover_time_rejects_negative_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite non-negative"):
            local_publish.optional_nonnegative_float("-0.1", "cover_time_seconds")
    def test_frame_extraction_retries_windows_dll_initialization_failure(self) -> None:
        transient = local_publish.subprocess.CalledProcessError(0xC0000142, ["ffmpeg"])
        with (
            mock.patch.object(
                local_publish.subprocess,
                "run",
                side_effect=[transient, None],
            ) as runner,
            mock.patch.object(local_publish.time, "sleep") as sleeper,
        ):
            local_publish.run_frame_extraction(["ffmpeg", "-version"])

        self.assertEqual(runner.call_count, 2)
        sleeper.assert_called_once_with(0.4)

    def test_frame_extraction_does_not_retry_normal_ffmpeg_failure(self) -> None:
        failure = local_publish.subprocess.CalledProcessError(1, ["ffmpeg"])
        with (
            mock.patch.object(local_publish.subprocess, "run", side_effect=failure) as runner,
            mock.patch.object(local_publish.time, "sleep") as sleeper,
            self.assertRaises(local_publish.subprocess.CalledProcessError),
        ):
            local_publish.run_frame_extraction(["ffmpeg", "-version"])

        self.assertEqual(runner.call_count, 1)
        sleeper.assert_not_called()
    def test_render_local_reuses_existing_cover(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            delivery = Path(temporary)
            (delivery / "001-topic.mp4").write_bytes(b"test-video")
            (delivery / "001-topic-cover.jpg").write_bytes(b"existing-cover")
            copy = delivery / "titles-and-covers.csv"
            copy.write_text("clip_id,title,cover_mode,cover_text_primary\n001,Title,quote-impact,Primary\n", encoding="utf-8")
            args = argparse.Namespace(copy=str(copy), clips_dir=str(delivery), creator="viridis", force=False)
            with (
                mock.patch.object(local_publish, "load_profiles", return_value={"viridis": {}}),
                mock.patch.object(local_publish, "validate_profiles", return_value=[]),
                mock.patch.object(local_publish, "render_cover") as renderer,
                mock.patch.object(local_publish, "item_errors", return_value=[]),
            ):
                self.assertEqual(local_publish.render_local(args), 0)
                renderer.assert_not_called()

    def test_yuchu_narrative_ignores_legacy_fixed_background(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            video = folder / "001-topic.mp4"
            video.write_bytes(b"test-video")
            frame = folder / "frame.png"
            Image.new("RGB", (1920, 1080), (240, 20, 20)).save(frame)
            output = folder / "cover.jpg"
            item = {
                "clip_id": "001",
                "video": str(video),
                "title": "Title",
                "cover_mode": "quote-impact",
                "cover_source_region": "full",
                "cover_text_primary": "Primary",
                "cover_text_secondary": "",
                "reference_image": str(frame),
                "evidence_image": "",
            }
            profile = dict(local_publish.load_profiles()["yuchu"])
            profile["narrative_cover"] = {"asset": "covers/yuchu-base.png"}

            local_publish.render_cover(item, profile, output)

            self.assertEqual(Path(item["reference_image"]).resolve(), frame.resolve())
            with Image.open(output) as rendered:
                red, green, blue = rendered.getpixel((10, 10))
            self.assertGreater(red, 200)
            self.assertLess(green, 60)
            self.assertLess(blue, 60)

    def test_yuchu_song_uses_designated_background(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            video = folder / "001-song.mp4"
            video.write_bytes(b"test-video")
            frame = folder / "frame.png"
            Image.new("RGB", (1920, 1080), (240, 20, 20)).save(frame)
            output = folder / "cover.jpg"
            item = {
                "clip_id": "001",
                "video": str(video),
                "title": "Song",
                "cover_mode": "song",
                "cover_source_region": "full",
                "cover_text_primary": "Song",
                "cover_text_secondary": "",
                "reference_image": str(frame),
                "evidence_image": "",
            }
            profile = local_publish.load_profiles()["yuchu"]

            local_publish.render_cover(item, profile, output)

            expected = local_publish.SKILL_ROOT / "assets" / "covers" / "yuchu-base.png"
            self.assertEqual(Path(item["reference_image"]).resolve(), expected.resolve())
            self.assertTrue(output.is_file())

    def test_radio_cover_layout_moves_copy_to_right_panel(self):
        normal_x, normal_width, normal_top, normal_bottom = local_publish.narrative_cover_layout("auto")
        radio_x, radio_width, radio_top, radio_bottom = local_publish.narrative_cover_layout("radio-left")
        self.assertGreater(radio_x, normal_x)
        self.assertLess(radio_width, normal_width)
        self.assertEqual(radio_top, normal_top)
        self.assertEqual(radio_bottom, normal_bottom)
        self.assertGreaterEqual(normal_x - normal_width / 2, 240)
        self.assertLessEqual(normal_x + normal_width / 2, 1680)

    def test_narrative_cover_copy_requires_two_explicit_regions(self) -> None:
        item = {
            "title": "完整标题",
            "cover_mode": "quote-impact",
            "cover_source_region": "auto",
            "cover_text_primary": "认真猜名言",
            "cover_text_secondary": "答案全说反🤓",
            "video": "missing.mp4",
            "cover": "missing.jpg",
        }
        errors = local_publish.item_errors(item, upload_ready=False)
        self.assertFalse(any("3–22" in error for error in errors))
        item["cover_text_primary"] = "一" * 23
        errors = local_publish.item_errors(item, upload_ready=False)
        self.assertIn("普通切片封面上区应为 3–22 个有效文字", errors)
    def test_verified_guest_image_is_resolved_and_composited_below_text(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            guest = root / "guest.png"
            image = Image.new("RGBA", (60, 100), (0, 0, 0, 0))
            for x in range(10, 50):
                for y in range(5, 95):
                    image.putpixel((x, y), (255, 30, 40, 255))
            image.save(guest)
            profiles = {
                "host": {"display_name": "主播"},
                "guest": {
                    "display_name": "嘉宾",
                    "cover_character_image": str(guest),
                },
            }
            row = {
                "collaboration_type": "collaboration",
                "participants": "主播,嘉宾",
                "participant_profile_keys": "host,guest",
                "guest_dialogue_verified": "1",
            }
            paths, missing = local_publish.resolve_guest_character_images(
                row, profiles, "host", root
            )
            self.assertEqual(paths, [guest.resolve()])
            self.assertEqual(missing, [])
            canvas = Image.new("RGBA", local_publish.CANVAS, (10, 20, 30, 255))
            used = local_publish.composite_guest_characters(canvas, paths)
            self.assertEqual(used, [str(guest.resolve())])
            self.assertGreater(
                sum(
                    1 for pixel in canvas.get_flattened_data()
                    if pixel[0] > 200 and pixel[1] < 80
                ),
                100,
            )
            row["guest_dialogue_verified"] = "0"
            self.assertEqual(
                local_publish.resolve_guest_character_images(row, profiles, "host", root),
                ([], []),
            )
if __name__ == "__main__":
    unittest.main()

