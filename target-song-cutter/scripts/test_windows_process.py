from __future__ import annotations

import subprocess
import unittest
from unittest import mock

import windows_process


class WindowsProcessRetryTests(unittest.TestCase):
    def test_retries_transient_loader_failure_then_returns_success(self) -> None:
        transient = subprocess.CalledProcessError(0xC0000142, ["ffprobe"])
        success = subprocess.CompletedProcess(["ffprobe"], 0, stdout="12.5\n")
        with (
            mock.patch.object(
                windows_process.subprocess,
                "run",
                side_effect=[transient, success],
            ) as runner,
            mock.patch.object(windows_process.time, "sleep") as sleeper,
        ):
            result = windows_process.run_checked_with_transient_retries(
                ["ffprobe", "-version"], capture_output=True, text=True
            )
        self.assertIs(result, success)
        self.assertEqual(runner.call_count, 2)
        sleeper.assert_called_once_with(0.4)

    def test_does_not_retry_normal_media_failure(self) -> None:
        failure = subprocess.CalledProcessError(1, ["ffprobe"])
        with (
            mock.patch.object(
                windows_process.subprocess,
                "run",
                side_effect=failure,
            ) as runner,
            mock.patch.object(windows_process.time, "sleep") as sleeper,
            self.assertRaises(subprocess.CalledProcessError),
        ):
            windows_process.run_checked_with_transient_retries(["ffprobe"])
        self.assertEqual(runner.call_count, 1)
        sleeper.assert_not_called()


if __name__ == "__main__":
    unittest.main()
