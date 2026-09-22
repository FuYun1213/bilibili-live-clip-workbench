import unittest

from transcribe_review_clips import load_creator_hotwords


class ReviewClipHotwordTests(unittest.TestCase):
    def test_sumire_scoped_terms(self):
        terms = load_creator_hotwords("sumire")
        self.assertIn("小粉螈", terms)
        self.assertIn("得意", terms)
        self.assertNotIn("昼你鸭", terms)

    def test_chilly_scoped_fan_badge(self):
        terms = load_creator_hotwords("chilly")
        self.assertIn("昼你鸭", terms)
        self.assertNotIn("宇宙猫", terms)

    def test_viridis_scoped_stream_term(self):
        self.assertIn("孙女", load_creator_hotwords("viridis"))


if __name__ == "__main__":
    unittest.main()

