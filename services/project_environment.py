"""Set project-owned paths before importing third-party libraries."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for name, relative in {
    "HF_HOME": "cache/huggingface",
    "TORCH_HOME": "cache/torch",
    "MODELSCOPE_CACHE": "cache/modelscope",
    "PIP_CACHE_DIR": "cache/pip",
    "UV_CACHE_DIR": "cache/uv",
    "TEMP": "cache/temp",
    "TMP": "cache/temp",
}.items():
    target = (ROOT / relative).resolve()
    if not target.is_relative_to(ROOT):
        raise RuntimeError("Cache escaped project root")
    target.mkdir(parents=True, exist_ok=True)
    os.environ[name] = str(target)
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
