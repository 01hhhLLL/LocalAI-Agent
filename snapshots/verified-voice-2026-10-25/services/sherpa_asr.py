"""CPU-only streaming Chinese ASR using the pinned sherpa-onnx Zipformer model."""
import importlib.util
import os
import sys
from pathlib import Path

import project_environment

_DLL_SEARCH_HANDLES = []


def _prioritize_project_dlls():
    if sys.platform != "win32":
        return
    for package in ("onnxruntime", "sherpa_onnx"):
        spec = importlib.util.find_spec(package)
        if spec and spec.origin:
            base = Path(spec.origin).parent
            for directory in (base / "capi", base / "lib", base):
                if directory.is_dir():
                    resolved = str(directory.resolve())
                    _DLL_SEARCH_HANDLES.append(os.add_dll_directory(resolved))
                    os.environ["PATH"] = resolved + os.pathsep + os.environ["PATH"]


_prioritize_project_dlls()


def create_recognizer(num_threads: int = 1):
    # Load the environment-pinned ONNX Runtime first; Windows otherwise may
    # resolve an older onnxruntime.dll from System32 ahead of the wheel copy.
    import onnxruntime  # noqa: F401
    import sherpa_onnx

    root = project_environment.ROOT / "models/model-sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30/source"
    required = ("tokens.txt", "encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx")
    missing = [name for name in required if not (root / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Sherpa model files are missing: {', '.join(missing)}")
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=str(root / "tokens.txt"), encoder=str(root / "encoder.int8.onnx"),
        decoder=str(root / "decoder.onnx"), joiner=str(root / "joiner.int8.onnx"),
        num_threads=num_threads, sample_rate=16000, feature_dim=80,
        enable_endpoint_detection=True, provider="cpu")


def decode_samples(recognizer, samples, sample_rate: int = 16000, chunk_seconds: float = 0.1):
    """Decode mono float32 samples in streaming chunks; return final endpoint results."""
    import numpy as np

    if sample_rate != 16000:
        raise ValueError("Sherpa model requires 16000 Hz audio")
    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    if not len(audio):
        raise ValueError("Audio is empty")
    stream = recognizer.create_stream()
    chunk = int(sample_rate * chunk_seconds)
    results = []

    def drain():
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        if recognizer.is_endpoint(stream):
            result = recognizer.get_result(stream).strip()
            if result:
                results.append(result)
            recognizer.reset(stream)

    for start in range(0, len(audio), chunk):
        stream.accept_waveform(sample_rate, audio[start:start + chunk])
        drain()
    stream.input_finished()
    drain()
    final = recognizer.get_result(stream).strip()
    if final and (not results or final != results[-1]):
        results.append(final)
    return results
