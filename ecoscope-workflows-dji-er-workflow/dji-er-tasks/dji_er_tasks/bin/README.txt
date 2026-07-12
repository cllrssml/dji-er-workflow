dji-log-parser binaries (v0.5.7)
=================================

Bundled binaries for all supported platforms. These are used by
uas_tasks/_binary.py to decrypt and parse DJI RC Pro .txt flight logs.

  dji-log-linux-x86_64       Linux x86_64
  dji-log-win-x86_64.exe     Windows x86_64
  dji-log-macos-universal    macOS universal (x86_64 + arm64)

Source: https://github.com/lvauvillier/dji-log-parser/releases/tag/v0.5.7
Licence: MIT

If the bundled binary for your platform is absent, _binary.py also checks
~/bin/dji-log (Linux/macOS) or ~/bin/dji-log.exe (Windows) as a fallback.
