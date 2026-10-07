"""Download the pinned CPU Zipformer model into the project-owned model tree."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import project_environment

from huggingface_hub import snapshot_download


REPO = "csukuangfj/sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30"
REVISION = "ad658fa0201659a09ea3c176129a191c77ecae8f"
DESTINATION = project_environment.ROOT / "models/model-sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30/source"


def main():
    path = snapshot_download(
        repo_id=REPO, revision=REVISION, local_dir=DESTINATION,
        allow_patterns=["README.md", "tokens.txt", "encoder.int8.onnx",
                        "decoder.onnx", "joiner.int8.onnx", "test_wavs/*.wav"],
    )
    required = ("tokens.txt", "encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx")
    missing = [name for name in required if not (Path(path) / name).is_file()]
    if missing:
        raise RuntimeError(f"Model download is incomplete: {missing}")
    print(f"Model ready: {path}")


if __name__ == "__main__":
    main()
