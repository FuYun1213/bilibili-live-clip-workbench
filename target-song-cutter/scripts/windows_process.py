"""Windows-safe subprocess defaults for the local desktop workflow."""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any, Sequence


TRANSIENT_WINDOWS_START_FAILURES = frozenset(
    {
        0xC0000142,  # STATUS_DLL_INIT_FAILED
        0xC0000135,  # STATUS_DLL_NOT_FOUND
        0xC000007B,  # STATUS_INVALID_IMAGE_FORMAT
    }
)


def configure_noninteractive_error_mode() -> None:
    """Prevent child-process loader failures from opening modal Windows dialogs."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        current = int(kernel32.GetErrorMode())
        # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX. Children inherit this.
        kernel32.SetErrorMode(current | 0x0001 | 0x0002)
    except Exception:
        # The subprocess flags below still suppress console windows if this API is absent.
        pass


def hidden_creationflags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def hidden_subprocess_kwargs() -> dict[str, Any]:
    flags = hidden_creationflags()
    return {"creationflags": flags} if flags else {}


def configure_hidden_subprocess_defaults() -> None:
    """Hide every descendant console process, even when a helper omits flags."""
    if os.name != "nt" or getattr(subprocess, "_target_song_hidden_children", False):
        return
    original = subprocess.Popen.__init__
    hidden = hidden_creationflags()

    def hidden_popen_init(self, *args, **kwargs):
        kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | hidden
        return original(self, *args, **kwargs)

    subprocess.Popen.__init__ = hidden_popen_init
    subprocess._target_song_hidden_children = True

def unsigned_returncode(returncode: int) -> int:
    return int(returncode) & 0xFFFFFFFF


def is_transient_start_failure(returncode: int) -> bool:
    return unsigned_returncode(returncode) in TRANSIENT_WINDOWS_START_FAILURES


def run_checked_with_transient_retries(
    command: Sequence[str],
    *,
    retries: int = 4,
    initial_delay: float = 0.4,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Run a media helper and retry only Windows loader initialization failures."""
    options = dict(kwargs)
    options.pop("check", None)
    attempts = max(1, int(retries))
    for attempt in range(1, attempts + 1):
        try:
            return subprocess.run(list(command), check=True, **options)
        except subprocess.CalledProcessError as exc:
            if not is_transient_start_failure(exc.returncode) or attempt >= attempts:
                raise
            time.sleep(max(0.0, float(initial_delay)) * attempt)
    raise AssertionError("unreachable")


configure_noninteractive_error_mode()
configure_hidden_subprocess_defaults()
