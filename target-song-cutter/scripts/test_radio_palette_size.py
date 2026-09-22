import tempfile
import unittest
from pathlib import Path
import review_workspace as rw


class RadioPaletteSizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ass = Path(self.tmp.name) / "test.ass"
        self.profile = {"display_name": "羽啾chu2u", "dialogue_font": "Microsoft YaHei",
                        "dialogue_font_size": 85, "subtitle_fill_color": "#FFAA3D",
                        "subtitle_outline_color": "#111318"}

    def write(self, radio=True, title=True):
        self.ass.write_text(
            "[Script Info]\n" + ("Title: Vertical livestream radio layout\n" if radio and title else "")
            + "[V4+ Styles]\n"
            + ("Style: YuchuRadio,Arial,80,&H00FFFFFF,&H00FFFFFF,&H00000000,&H50000000,-1,0,0,0,100,100,0,0,1,3,2,5,720,80,0,1\n"
               if radio else "Style: Regular,Arial,65,&H00FFFFFF,&H00FFFFFF,&H00000000,&H50000000,-1,0,0,0,100,100,0,0,1,3,2,2,10,10,10,1\n")
            + "[Events]\nDialogue: 0,0:00:00.10,0:00:01.00," + ("YuchuRadio" if radio else "Regular")
            + ",羽啾chu2u,0,0,0,,已经人工校对的正文\n", encoding="utf-8-sig")

    def test_refresh_radio_migrates_wrong_size_and_is_idempotent(self):
        self.write()
        self.assertEqual(rw.refresh_ass_subtitle_palette(self.ass, "yuchu", {"yuchu": self.profile}), 1)
        self.assertIn("Style: YuchuRadio,Microsoft YaHei,65,", self.ass.read_text(encoding="utf-8-sig"))
        self.assertIn("已经人工校对的正文", self.ass.read_text(encoding="utf-8-sig"))
        self.assertEqual(rw.refresh_ass_subtitle_palette(self.ass, "yuchu", {"yuchu": self.profile}), 0)

    def test_radio_geometry_survives_header_editor_changes(self):
        self.write(title=False)
        rw.refresh_ass_subtitle_palette(self.ass, "yuchu", {"yuchu": self.profile})
        self.assertIn("Style: YuchuRadio,Microsoft YaHei,65,", self.ass.read_text(encoding="utf-8-sig"))

    def test_creating_and_refreshing_radio_speaker_clone_keeps_panel_size(self):
        self.write()
        rw.ensure_speaker_style(self.ass, "yuchu", self.profile, base_style="YuchuRadio")
        rw.refresh_ass_subtitle_palette(self.ass, "yuchu", {"yuchu": self.profile})
        self.assertIn("Style: Speaker_yuchu,Microsoft YaHei,65,", self.ass.read_text(encoding="utf-8-sig"))

    def test_ordinary_subtitle_still_updates_to_profile_size(self):
        self.write(radio=False)
        rw.refresh_ass_subtitle_palette(self.ass, "yuchu", {"yuchu": self.profile})
        rw.ensure_speaker_style(self.ass, "yuchu", self.profile, base_style="Regular")
        text = self.ass.read_text(encoding="utf-8-sig")
        self.assertIn("Style: Regular,Microsoft YaHei,85,", text)
        self.assertIn("Style: Speaker_yuchu,Microsoft YaHei,85,", text)


if __name__ == "__main__":
    unittest.main()
