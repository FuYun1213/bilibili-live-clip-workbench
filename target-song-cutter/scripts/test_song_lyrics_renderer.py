import csv
import tempfile
import unittest
from pathlib import Path

from song_lyrics_renderer import LyricLine, load_reviewed_lyrics, write_ass


class SongLyricsRendererTests(unittest.TestCase):
    def write_csv(self, reviewed="yes", corrected="正确歌词"):
        temporary = tempfile.TemporaryDirectory()
        path = Path(temporary.name) / "lyrics.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "clip", "start_seconds", "end_seconds", "recognized_text",
                "corrected_text", "reviewed",
            ])
            writer.writeheader()
            writer.writerow({
                "clip": "song", "start_seconds": "1", "end_seconds": "2",
                "recognized_text": "错误歌词", "corrected_text": corrected,
                "reviewed": reviewed,
            })
        return temporary, path

    def test_refuses_unreviewed_lyrics(self):
        temporary, path = self.write_csv(reviewed="no")
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ValueError, "Refusing to burn"):
            load_reviewed_lyrics(path)

    def test_writes_corrected_ass_with_point_one_second_delay(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "lyrics.ass"
        write_ass(path, [LyricLine("song", 1, 2, "错", "正确歌词", True)], delay=0.1)
        body = path.read_text(encoding="utf-8-sig")
        self.assertIn("0:00:01.10,0:00:02.10", body)
        self.assertIn("正确歌词", body)
        self.assertNotIn(",错\n", body)


if __name__ == "__main__":
    unittest.main()
