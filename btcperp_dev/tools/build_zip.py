#!/usr/bin/env python3
"""Build dist/btcperp_v<VERSION>.zip with one top-level folder `btcperp/`.

Whitelist-based: only the files the bot needs. Never includes .env, keys, data/, logs/, venv,
__pycache__, .git or anything outside btcperp/. Prints path, size and SHA256.
"""

import hashlib
import sys
import zipfile
from pathlib import Path

DEV = Path(__file__).resolve().parent.parent
SRC = DEV / "btcperp"
DIST = DEV / "dist"
TOP_FILES = ["run.py", "install.py", "requirements.txt", "VERSION", "README.md", "START_HERE.md", "API_NOTES.md",
             "CHANGELOG.md", "REVIEW_v1.1.0.md", "REVIEW_v1.2.0.md", "REVIEW_v1.3.0.md", "BACKTEST.md", ".env.example",
             "offline_sign/offline_sign.html", "config/config.yaml",
             "config/calendar.yaml", "config/calendar_history.yaml", "config/backtest_criteria.yaml"]
CODE_DIRS = ["perpbot", "tests"]
BAT_DIR = "windows"                      # Windows shortcuts: packaged with CRLF line endings
FORBIDDEN_PARTS = {".env", "data", "logs", "venv", "__pycache__", ".git", ".pytest_cache"}
FIXED_TIME = (2026, 9, 29, 0, 0, 0)


def collect() -> list[str]:
    files = list(TOP_FILES)
    for d in CODE_DIRS:
        for p in sorted((SRC / d).rglob("*.py")):
            files.append(p.relative_to(SRC).as_posix())
    for p in sorted((SRC / BAT_DIR).glob("*.bat")):
        files.append(p.relative_to(SRC).as_posix())
    for f in files:
        parts = set(Path(f).parts)
        if parts & FORBIDDEN_PARTS or f.endswith((".pyc", ".sqlite3", ".log")):
            raise SystemExit(f"refusing to package {f}")
        if not (SRC / f).is_file():
            raise SystemExit(f"missing file {f}")
    return files


def main() -> int:
    version = (SRC / "VERSION").read_text().strip()
    files = collect()
    manifest = "\n".join(sorted(files + ["MANIFEST.txt"])) + "\n"
    DIST.mkdir(exist_ok=True)
    out = DIST / f"btcperp_v{version}.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(files):
            info = zipfile.ZipInfo(f"btcperp/{f}", date_time=FIXED_TIME)
            info.external_attr = (0o755 if f in ("run.py", "install.py") else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            data = (SRC / f).read_bytes()
            if f.endswith(".bat"):
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                if any(b > 127 for b in data):
                    raise SystemExit(f"{f}: .bat files must be plain ASCII (cmd.exe code pages)")
            z.writestr(info, data)
        info = zipfile.ZipInfo("btcperp/MANIFEST.txt", date_time=FIXED_TIME)
        info.external_attr = 0o644 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(info, manifest)
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    assert all(n.startswith("btcperp/") for n in names)
    for n in names:
        if set(Path(n).parts) & FORBIDDEN_PARTS or n.endswith("/.env"):
            raise SystemExit(f"forbidden entry in zip: {n}")
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"zip: {out}\nfiles: {len(names)}\nsize: {out.stat().st_size} bytes\nsha256: {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
