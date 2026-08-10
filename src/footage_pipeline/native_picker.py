"""Native macOS folder picker.

A browser cannot give us a real absolute filesystem path (``<input
webkitdirectory>`` deliberately hides it), and this pipeline needs the actual
POSIX path of the card/volume being backed up. So the *backend* opens the
system dialog with ``osascript`` and returns the path it produced.

Because the dialog is drawn by the machine running the server, the server is
expected to run on the user's Mac (which it does — this is a local app).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: The dialog is modal and waits on a human, so the timeout is generous.
PICKER_TIMEOUT_SECONDS = 600


class NativePickerUnavailable(RuntimeError):
    """Raised when no native folder dialog can be shown on this machine."""


class NativePickerFailed(RuntimeError):
    """Raised when the dialog was shown but did not produce a usable path."""


@dataclass
class PickResult:
    """Outcome of a folder pick. ``path`` is ``None`` when the user cancelled."""

    path: str | None
    cancelled: bool = False


def is_available() -> bool:
    """True when a native folder dialog can be shown (macOS with osascript)."""
    return sys.platform == "darwin" and Path("/usr/bin/osascript").exists()


def _build_script(prompt: str, default_path: str | None) -> str:
    # AppleScript string literals escape backslash and double-quote only.
    def quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    clause = f"choose folder with prompt {quote(prompt)}"
    if default_path:
        candidate = Path(default_path).expanduser()
        if candidate.is_dir():
            clause += f" default location POSIX file {quote(str(candidate))}"
    return f"POSIX path of ({clause})"


def choose_folder(
    prompt: str = "Choose a folder",
    default_path: str | None = None,
) -> PickResult:
    """Open the macOS "choose folder" dialog and return the absolute path.

    Args:
        prompt: Text shown at the top of the dialog.
        default_path: Folder the dialog opens at, when it still exists.

    Returns:
        A :class:`PickResult`; ``cancelled`` is True when the user dismissed the
        dialog.

    Raises:
        NativePickerUnavailable: Not running on macOS, or ``osascript`` missing.
        NativePickerFailed: The dialog errored, timed out, or returned nothing.
    """
    if not is_available():
        raise NativePickerUnavailable(
            "The native folder picker needs macOS. Run this app on the Mac that "
            "has the drives attached, or type the path manually."
        )

    try:
        completed = subprocess.run(
            ["/usr/bin/osascript", "-e", _build_script(prompt, default_path)],
            capture_output=True,
            text=True,
            timeout=PICKER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise NativePickerFailed("The folder dialog timed out waiting for a choice.") from exc

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        # osascript reports a dismissed dialog as error -128.
        if "-128" in stderr or "User canceled" in stderr or "user canceled" in stderr:
            return PickResult(path=None, cancelled=True)
        raise NativePickerFailed(stderr or "The folder dialog failed.")

    path = (completed.stdout or "").strip()
    if not path:
        return PickResult(path=None, cancelled=True)

    # "POSIX path of" yields a trailing slash for folders; keep "/" itself.
    if len(path) > 1:
        path = path.rstrip("/")
    return PickResult(path=path)
