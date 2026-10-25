"""Run project-local CPU OCR over image bytes on stdin, returning text boxes only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
from model_resources import ModelLease, require_admission, require_headroom
from step_runtime import hard_bounded_step
MODELS = ROOT / "runtime" / "ocr-models"
REQUIRED_MODELS = (
    "PP-OCRv6_det_small.onnx",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "PP-OCRv6_rec_small.onnx",
)


@hard_bounded_step("local_ocr")
def main() -> int:
    with ModelLease("rapidocr"):
        require_admission("rapidocr")
        return _recognize_owned()


def _recognize_owned() -> int:
    if any(not (MODELS / name).is_file() for name in REQUIRED_MODELS):
        raise RuntimeError("project-local OCR models are missing")
    image = sys.stdin.buffer.read(32_000_001)
    if not image or len(image) > 32_000_000:
        raise ValueError("OCR image is empty or too large")
    from rapidocr import RapidOCR
    from PIL import Image
    import io
    with Image.open(io.BytesIO(image)) as source:
        if max(source.size) > 4096:
            raise ValueError("OCR image exceeds the 4096-pixel side budget")

    engine = RapidOCR(params={
        "Global.model_root_dir": str(MODELS),
        "Global.log_level": "critical",
        "EngineConfig.onnxruntime.intra_op_num_threads": 2,
    })
    result = engine(image)
    require_headroom("rapidocr")
    boxes = result.boxes if result.boxes is not None else []
    lines = [
        {"text": str(text), "score": round(float(score), 4),
         "bbox": [round(float(box[:, 0].min()), 2), round(float(box[:, 1].min()), 2),
                  round(float(box[:, 0].max()), 2), round(float(box[:, 1].max()), 2)]}
        for text, score, box in zip(result.txts or [], result.scores or [], boxes)
    ]
    sys.stdout.buffer.write(json.dumps({"lines": lines}, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
