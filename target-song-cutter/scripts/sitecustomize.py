"""Keep every workflow child process in the background on Windows."""

from __future__ import annotations

import os
import subprocess


if os.name == "nt" and not getattr(subprocess, "_target_song_hidden_children", False):
    _original_popen_init = subprocess.Popen.__init__
    _hidden_flag = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _hidden_popen_init(self, *args, **kwargs):
        kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | _hidden_flag
        return _original_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _hidden_popen_init
    subprocess._target_song_hidden_children = True

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetErrorMode(int(kernel32.GetErrorMode()) | 0x0001 | 0x0002)
    except Exception:
        pass