import unittest

from target_song_cutter import (
    Segment,
    Window,
    expand_to_music_boundaries,
    filter_windows_by_music,
    merge_windows,
    split_long_segments,
    srt_timestamp,
)


class MergeWindowsTests(unittest.TestCase):
    def test_merges_nearby_hits_and_adds_padding(self):
        windows = [
            Window(20, 28, .71, -20), Window(30, 38, .75, -22),
            Window(50, 58, .80, -18), Window(150, 158, .40, -20),
        ]
        result = merge_windows(windows, .65, -45, 15, 10, 20, 480, 200)
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0].start, result[0].end), (10, 68))

    def test_rejects_quiet_and_short_groups(self):
        windows = [Window(10, 18, .90, -60), Window(30, 38, .90, -20)]
        result = merge_windows(windows, .65, -45, 15, 5, 30, 480, 100)
        self.assertEqual(result, [])

    def test_splits_very_long_continuous_runs(self):
        windows = [Window(i, i + 8, .75, -20) for i in range(0, 700, 10)]
        result = merge_windows(windows, .65, -45, 3, 0, 60, 300, 800)
        self.assertGreaterEqual(len(result), 2)
        self.assertTrue(all(segment.end - segment.start <= 300 for segment in result))

    def test_expands_to_quiet_music_boundaries(self):
        levels = [-55] * 10 + [-20] * 180 + [-55] * 10
        cores = [Segment(50, 120, .72, .80, 20)]
        result = expand_to_music_boundaries(cores, levels, 1, 4, 12, 180, 2, 200, 480)
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0].start, result[0].end), (8, 192))

    def test_merges_vocal_cores_from_same_song(self):
        levels = [-55] * 10 + [-20] * 180 + [-55] * 10
        cores = [
            Segment(40, 70, .70, .78, 10),
            Segment(110, 140, .74, .82, 12),
        ]
        result = expand_to_music_boundaries(cores, levels, 1, 4, 12, 180, 2, 200, 480)
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0].start, result[0].end), (8, 192))

    def test_srt_timestamp(self):
        self.assertEqual(srt_timestamp(3661.234), "01:01:01,234")

    def test_singing_gate_rejects_speech_only_window(self):
        windows = [
            Window(0, 8, .8, -20),
            Window(10, 18, .8, -20),
        ]
        levels = [-60] * 10 + [-25] * 10
        result, threshold = filter_windows_by_music(windows, levels, 1, minimum_db=-40)
        self.assertEqual(result, [windows[1]])
        self.assertEqual(threshold, -40)

    def test_splits_long_merged_song_at_internal_valley(self):
        levels = [-20] * 240 + [-55] * 10 + [-20] * 250
        segment = Segment(0, 500, .75, .85, 100)
        result = split_long_segments([segment], levels, 1, 420, 90, 5)
        self.assertEqual(len(result), 2)
        self.assertTrue(235 <= result[0].end <= 250)
        self.assertEqual(result[0].end, result[1].start)


if __name__ == "__main__":
    unittest.main()
