"""Download a fixed public model revision into the project only."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import project_environment
import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4
from huggingface_hub import snapshot_download

revision = "b968826d9c46dd6066d109eabc6255188de91218"
target = project_environment.ROOT / "models/model-qwen3-8b/source"
snapshot_download("Qwen/Qwen3-8B", revision=revision, local_dir=target,
                  token=False, max_workers=2,
                  allow_patterns=["*.json", "*.safetensors", "*.txt", "*.jinja", "*.model"])
checksums = {}
for path in sorted(target.iterdir()):
    if path.is_file():
        with path.open("rb") as source:
            checksums[path.name] = hashlib.file_digest(source, "sha256").hexdigest()
record = {"repo": "Qwen/Qwen3-8B", "revision": revision,
          "downloaded_at": datetime.now(timezone.utc).isoformat(), "sha256": checksums}
report = project_environment.ROOT / "tests/results" / f"model-assets-{uuid4().hex}.json"
with report.open("x", encoding="utf-8") as stream:
    json.dump(record, stream, indent=2)
print(f"Model assets ready; {len(checksums)} files hashed. Report: {report}", flush=True)
