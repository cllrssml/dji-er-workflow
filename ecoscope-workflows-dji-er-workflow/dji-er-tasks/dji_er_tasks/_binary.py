"""Platform-aware locator for the dji-log-parser binary."""

import os
import platform
from pathlib import Path

_BIN_DIR = Path(__file__).parent / "bin"

_BUNDLED = {
    "Linux": _BIN_DIR / "dji-log-linux-x86_64",
    "Windows": _BIN_DIR / "dji-log-win-x86_64.exe",
    "Darwin": _BIN_DIR / "dji-log-macos-universal",
}

_FALLBACK = {
    "Linux": [Path.home() / "bin" / "dji-log"],
    "Windows": [Path.home() / "bin" / "dji-log.exe"],
    "Darwin": [Path.home() / "bin" / "dji-log"],
}


def get_binary_path() -> str:
    """Return the absolute path to the dji-log-parser binary for this platform.

    Checks the bundled uas_tasks/bin/ directory first, then ~/bin/ as fallback.
    Raises RuntimeError with download instructions if nothing is found.
    """
    system = platform.system()
    candidates = []

    bundled = _BUNDLED.get(system)
    if bundled:
        candidates.append(bundled)

    candidates.extend(_FALLBACK.get(system, []))

    for path in candidates:
        p = Path(path)
        if p.exists() and os.access(p, os.X_OK):
            return str(p)

    raise RuntimeError(
        f"dji-log binary not found (platform: {system}).\n"
        f"Download v0.5.7 from: https://github.com/lvauvillier/dji-log-parser/releases\n"
        f"Extract and place in: {_BIN_DIR}\n"
        f"  Linux  : dji-log-linux-x86_64         (then chmod +x)\n"
        f"  Windows: dji-log-win-x86_64.exe\n"
        f"  macOS  : dji-log-macos-universal       (then chmod +x)\n"
        f"See {_BIN_DIR / 'README.txt'} for full instructions."
    )
