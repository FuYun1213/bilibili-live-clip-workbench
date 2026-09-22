import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from burn_ass_subtitles import prepare_fontsdir, validate_ass_styles


class AssStyleGateTests(unittest.TestCase):
    def test_rejects_undefined_dialogue_style(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.ass"
            path.write_text(
                "[V4+ Styles]\n"
                "Style: Viridis,Arial,65,&H00FFFFFF\n"
                "[Events]\n"
                "Dialogue: 0,0:00:00.00,0:00:01.00,CharacterRadio,Speaker,0,0,0,,Text\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "CharacterRadio"):
                validate_ass_styles(path)

    def test_accepts_defined_dialogue_style(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "good.ass"
            path.write_text(
                "[V4+ Styles]\n"
                "Style: CharacterRadio,Arial,65,&H00FFFFFF\n"
                "[Events]\n"
                "Dialogue: 0,0:00:00.00,0:00:01.00,CharacterRadio,Speaker,0,0,0,,Text\n",
                encoding="utf-8",
            )
            validate_ass_styles(path)


    def test_nested_font_directories_are_flattened_for_libass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fonts"
            nested = root / "smiley"
            nested.mkdir(parents=True)
            (nested / "SmileySans.otf").write_bytes(b"font")
            flattened, workspace = prepare_fontsdir(root)
            try:
                self.assertIsNotNone(workspace)
                self.assertTrue((flattened / "SmileySans.otf").is_file())
            finally:
                if workspace is not None:
                    workspace.cleanup()

if __name__ == "__main__":
    unittest.main()
