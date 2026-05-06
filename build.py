from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ENTRYPOINT = ROOT / "main.py"
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
APP_NAME = "RPGodotOptimizerToolkit"


def main() -> int:
    if not ENTRYPOINT.exists():
        print(f"Missing entrypoint: {ENTRYPOINT}", file=sys.stderr)
        return 1

    pyinstaller = shutil.which("pyinstaller")
    if pyinstaller is None:
        print(
            "PyInstaller is not installed or not on PATH. "
            "Install it with `py -m pip install pyinstaller`.",
            file=sys.stderr,
        )
        return 1

    DIST_DIR.mkdir(exist_ok=True)
    BUILD_DIR.mkdir(exist_ok=True)

    cmd = [
        pyinstaller,
        "--noconfirm",
        "--clean",
        "--name",
        APP_NAME,
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR),
        "--specpath",
        str(ROOT),
        str(ENTRYPOINT),
    ]

    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
