import unittest

import workflow_app


class PlaybackRateRetryTests(unittest.TestCase):
    def test_two_x_rate_is_reapplied_after_vlc_becomes_ready(self):
        class Value:
            def __init__(self, value=""):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class Player:
            def __init__(self):
                self.rates = []

            def set_rate(self, value):
                self.rates.append(value)
                return True

        bench = object.__new__(workflow_app.Workbench)
        player = Player()
        callbacks = []
        bench.preview_player = player
        bench.vars = {
            "review_speed": Value("2.0x"),
            "review_line_status": Value(),
        }
        bench.after = lambda delay, callback: callbacks.append((delay, callback))

        bench.set_review_playback_rate()
        self.assertEqual(player.rates, [2.0])
        self.assertEqual([delay for delay, _callback in callbacks], [180, 600])
        for _delay, callback in callbacks:
            callback()
        self.assertEqual(player.rates, [2.0, 2.0, 2.0])
        self.assertIn("2x", bench.vars["review_line_status"].get())


if __name__ == "__main__":
    unittest.main()
