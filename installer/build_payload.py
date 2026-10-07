"""Build an explicit source-only installation payload from tracked files."""

from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).resolve().parent / "payload.zip"
SCRIPTS = {
    "environment.ps1", "voice-hotkey.ps1", "voice-hotkey-desktop.ps1",
    "run-hotkey-command.py", "run-hermes-bounded.py",
    "transcribe-hotkey-recording.py", "vision-locate.py",
    "vision-locate.ps1", "local-ocr.py", "local-api.ps1",
    "start-app-and-wait.ps1", "capture-screen.ps1",
    "download-model.py", "download-sherpa-model.py",
}
TOP_LEVEL = {"pyproject.toml", "uv.lock"}
DOCS = {"docs/TECHNICAL.md", "docs/ARCHITECTURE.md", "README-release.md"}


def selected(path: str) -> bool:
    parts = Path(path).parts
    if any(segment.startswith(".") for segment in parts):
        return False
    if path in TOP_LEVEL | DOCS:
        return True
    if path == "README.md" and not (ROOT / "README-release.md").is_file():
        return True
    if path == "assets/dragon-voice-icon-head-v1.png":
        return True
    if path == "knowledge/application-aliases.json":
        return True
    if (len(parts) == 2 and parts[0] == "services" and
            parts[1].endswith(".py") and parts[1] != "echo_mcp.py"):
        return True
    return len(parts) == 2 and parts[0] == "scripts" and parts[1] in SCRIPTS


def main() -> None:
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    names = [entry.decode("utf-8") for entry in tracked.split(b"\0") if entry]
    chosen = sorted(name for name in names if selected(name))
    if not chosen or not ({"README-release.md", "README.md"} & set(chosen)):
        raise RuntimeError("Release documentation is not committed")
    manifest = []
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in chosen:
            path = ROOT / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 5_000_000:
                raise RuntimeError(f"Unexpected release item: {name}")
            data = path.read_bytes()
            destination = "README.md" if name == "README-release.md" else name
            archive.writestr(destination, data)
            manifest.append({"path": destination, "sha256": hashlib.sha256(data).hexdigest()})
        config = (ROOT / "installer" / "vision-provider.toml").read_bytes()
        archive.writestr("config/vision-provider.toml", config)
        manifest.append({"path": "config/vision-provider.toml",
                         "sha256": hashlib.sha256(config).hexdigest()})
        archive.writestr("release-manifest.json", json.dumps(manifest, indent=2))
    print(f"Payload: {OUT}; {len(manifest)} approved files")


if __name__ == "__main__":
    main()
