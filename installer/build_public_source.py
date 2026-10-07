"""Create a fresh, reviewable GitHub export without local history or test data."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from build_payload import ROOT, selected

EXTRA = {
    "installer/LocalAIAgent.Setup.csproj",
    "installer/Program.cs",
    "installer/build-release.ps1",
    "installer/build_payload.py",
    "installer/build_public_source.py",
    "installer/vision-provider.toml",
}
PUBLIC_IGNORE = """dist/\ninstaller/payload.zip\ninstaller/bin/\ninstaller/obj/\nconfig/\ncache/\ndata/\nlogs/\nmodels/\npackages/\nruntime/\ntests/\n*.wav\n*.flac\n*.mp4\n*.mkv\n*.sqlite3\n*.db\n.env*\n*token*\n*secret*\n*private*key*\n*credential*\n"""


def main() -> None:
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    names = [part.decode("utf-8") for part in tracked.split(b"\0") if part]
    chosen = sorted(name for name in names if selected(name) or name in EXTRA)
    short = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                    cwd=ROOT, text=True).strip()
    target = ROOT / "dist" / f"public-source-{short}"
    if target.exists():
        raise RuntimeError(f"Export already exists: {target}")
    target.mkdir(parents=True)
    for name in chosen:
        source = ROOT / name
        if not source.is_file() or source.is_symlink() or source.stat().st_size > 5_000_000:
            raise RuntimeError(f"Unexpected public source file: {name}")
        relative = "README.md" if name == "README-release.md" else name
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    (target / ".gitignore").write_text(PUBLIC_IGNORE, encoding="utf-8")
    (target / "release-source-manifest.json").write_text(
        json.dumps({"files": chosen}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Public export: {target}; {len(chosen)} reviewed source files")


if __name__ == "__main__":
    main()
