"""Pixel proof for a located target; uses the already installed OCR image runtime."""
from __future__ import annotations
import base64
import hashlib
import io
import json
import math
import sys
import time


def capture_geometry(capture):
    fields = ("screen_left", "screen_top", "screen_width", "screen_height", "image_width", "image_height")
    if any(type(capture.get(k)) is not int for k in fields):
        raise ValueError("invalid screenshot geometry")
    left, top, width, height, iw, ih = [capture[k] for k in fields]
    if not (0 < width <= 65536 and 0 < height <= 65536 and 0 < iw <= 4096 and 0 < ih <= 4096):
        raise ValueError("screenshot size is outside the supported budget")
    return [left, top, width, height, iw, ih]


def desktop_center(located, capture):
    left, top, width, height, iw, ih = capture_geometry(capture)
    center = located.get("center")
    if (not isinstance(center, (list, tuple)) or len(center) != 2
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in center)
            or not 0 <= center[0] < iw or not 0 <= center[1] < ih):
        raise ValueError("target center is outside the screenshot")
    return (round(left + center[0] * width / iw), round(top + center[1] * height / ih))


def pixel_digest(capture, bbox):
    from PIL import Image
    capture_geometry(capture)
    encoded = capture.get("image_base64")
    if not isinstance(encoded, str) or len(encoded) > 34 * 1024 * 1024:
        raise ValueError("invalid screenshot data")
    image = base64.b64decode(encoded, validate=True)
    with Image.open(io.BytesIO(image)) as source:
        if source.size != (capture["image_width"], capture["image_height"]):
            raise ValueError("screenshot size and pixels disagree")
        return hashlib.sha256(source.convert("RGB").crop(bbox).tobytes()).hexdigest()


def make_proof(capture, located, *, expires, app_id=None, window_rect=None):
    geometry = capture_geometry(capture)
    bbox = located.get("bbox")
    if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4
            or any(type(v) not in (float, int) or not math.isfinite(v) for v in bbox)):
        raise ValueError("target has no bounded pixel region")
    x1, y1, x2, y2 = bbox
    if not 0 <= x1 < x2 <= geometry[4] or not 0 <= y1 < y2 <= geometry[5]:
        raise ValueError("target region is outside the screenshot")
    cx, cy = located["center"]
    if not x1 <= cx <= x2 or not y1 <= cy <= y2:
        raise ValueError("target center and region disagree")
    crop = [max(0, math.floor(x1) - 6), max(0, math.floor(y1) - 6),
            min(geometry[4], math.ceil(x2) + 6), min(geometry[5], math.ceil(y2) + 6)]
    proof = {"geometry": geometry, "crop_bbox": crop, "pixel_sha256": pixel_digest(capture, crop),
            "desktop_center": list(desktop_center(located, capture)), "app_id": app_id,
            "window_handle": capture.get("foreground_window_handle"), "window_rect": window_rect,
            "expires_monotonic": expires, "created_monotonic": time.monotonic()}
    if (located.get("locator") == "local_ocr" and isinstance(located.get("matched_text"), str)
            and 1 <= len(located["matched_text"]) <= 80
            and type(located.get("confidence")) in (int, float) and .9 <= located["confidence"] <= 1):
        proof["ocr_target"] = {"text": located["matched_text"], "bbox": list(bbox),
                               "score": located["confidence"]}
    return proof


def _verify_geometry(proof, capture, now):
    expires = proof.get("expires_monotonic")
    if type(expires) not in (float, int) or not math.isfinite(expires) or now >= expires:
        raise TimeoutError("pointer proof deadline expired")
    if capture_geometry(capture) != proof.get("geometry"):
        raise RuntimeError("desktop geometry changed after location")
    if capture.get("foreground_window_handle") != proof.get("window_handle"):
        raise RuntimeError("foreground window changed after location")
    if proof.get("window_rect") is not None and capture.get("foreground_window_rect") != proof["window_rect"]:
        raise RuntimeError("approved window moved after location")
    crop = proof.get("crop_bbox")
    iw, ih = proof["geometry"][4:]
    if (not isinstance(crop, list) or len(crop) != 4 or any(type(v) is not int for v in crop)
            or not 0 <= crop[0] < crop[2] <= iw or not 0 <= crop[1] < crop[3] <= ih):
        raise ValueError("invalid pointer proof region")
    return crop


def verify_proof(proof, capture, *, now=None):
    crop = _verify_geometry(proof, capture, time.monotonic() if now is None else now)
    # ponytail: the default remains pixel-exact. A separately scoped OCR profile
    # may re-observe dynamic navigation text without relaxing this comparison.
    if pixel_digest(capture, crop) != proof.get("pixel_sha256"):
        raise RuntimeError("target pixels changed after location")
    return {"fresh": True}


FOLIA_NAVIGATION = frozenset({"电台", "歌单", "专辑", "本地"})


def folia_ocr_refresh_allowed(proof, registered):
    from pathlib import Path
    target = proof.get("ocr_target") or {}
    return (isinstance(target, dict) and isinstance(target.get("text"), str)
            and registered.get("app_id") == proof.get("app_id")
            and registered.get("display_name", "").casefold() == "folia"
            and Path(registered.get("executable_path", "")).name.casefold() == "folia.exe"
            and target.get("text") in FOLIA_NAVIGATION)


def verify_ocr_proof(proof, capture, lines, *, now=None):
    """Revalidate one OCR navigation label despite an animated background."""
    _verify_geometry(proof, capture, time.monotonic() if now is None else now)
    target, rect = proof.get("ocr_target"), proof.get("window_rect")
    if (not isinstance(target, dict) or not isinstance(target.get("text"), str)
            or target["text"] not in FOLIA_NAVIGATION
            or type(target.get("score")) not in (int, float) or not .9 <= target["score"] <= 1
            or not isinstance(lines, list) or len(lines) > 1000
            or not isinstance(rect, list) or len(rect) != 4):
        raise ValueError("invalid OCR navigation proof")
    original = target.get("bbox")
    if (not isinstance(original, list) or len(original) != 4
            or any(type(v) not in (float, int) or not math.isfinite(v) for v in original)
            or not 0 <= original[0] < original[2] <= capture["image_width"]
            or not 0 <= original[1] < original[3] <= capture["image_height"]):
        raise ValueError("invalid original OCR bounds")
    matches, anchors = [], set()
    for row in lines:
        if not isinstance(row, dict):
            continue
        box, score, text = row.get("bbox"), row.get("score"), row.get("text")
        if (not isinstance(text, str) or type(score) not in (int, float) or not .9 <= score <= 1
                or not isinstance(box, list) or len(box) != 4
                or any(type(v) not in (float, int) or not math.isfinite(v) for v in box)
                or not 0 <= box[0] < box[2] <= capture["image_width"]
                or not 0 <= box[1] < box[3] <= capture["image_height"]):
            continue
        point = desktop_center({"center": [(box[0]+box[2])/2, (box[1]+box[3])/2]}, capture)
        if not (rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]):
            continue
        text = text.strip()
        if text in FOLIA_NAVIGATION:
            anchors.add(text)
        if text == target["text"]:
            matches.append(box)
    # ponytail: this profile only covers four harmless Folia navigation labels.
    # At most two source pixels of OCR-box jitter; moved controls or duplicate
    # labels require fresh planning, not a relaxed coordinate/image threshold.
    if (len(anchors) < 2 or len(matches) != 1
            or max(abs(a-b) for a, b in zip(original, matches[0])) > 2):
        raise RuntimeError("fresh OCR target missing, ambiguous or moved")
    left, top, width, height, iw, ih = proof["geometry"]
    px, py = proof["desktop_center"]
    cx, cy = (px-left)*iw/width, (py-top)*ih/height
    if not matches[0][0] <= cx <= matches[0][2] or not matches[0][1] <= cy <= matches[0][3]:
        raise RuntimeError("original click point left the fresh OCR text bounds")
    return {"fresh": True, "method": "fresh_local_ocr", "target": target["text"],
            "fresh_bbox": matches[0], "anchor_count": len(anchors),
            "box_delta_pixels": max(abs(a-b) for a, b in zip(original, matches[0]))}


def main():
    payload = json.loads(sys.stdin.buffer.read(70 * 1024 * 1024))
    if payload.get("mode") == "make":
        result = make_proof(payload["capture"], payload["located"], expires=payload["expires"],
                             app_id=payload.get("app_id"), window_rect=payload.get("window_rect"))
    elif payload.get("mode") == "verify":
        result = verify_proof(payload["proof"], payload["capture"])
    elif payload.get("mode") == "verify_ocr":
        result = verify_ocr_proof(payload["proof"], payload["capture"], payload["lines"])
    else:
        raise ValueError("invalid pointer proof operation")
    print(json.dumps(result, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
