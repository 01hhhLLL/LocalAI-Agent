"""Read-only startup planning. Estimates are admission budgets, not load proofs."""
from __future__ import annotations

import csv
import ctypes
import itertools
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from model_resources import PROFILES, VRAM_RESERVE_MIB, RAM_RESERVE_MIB

ROOT = Path(__file__).resolve().parents[1]
GPU_RESERVE_MIB = VRAM_RESERVE_MIB
# ponytail: one GPU only, matching today's cuda:0 loaders. Multi-GPU placement
# needs a real placement planner; never sum memory from unrelated GPUs.
# Conservative profiles include runtime/KV/activation overhead for current
# bounded workloads, informed by docs/quantization-comparison-*-four-way.md
# and docs/asr-model-comparison-2026-10-01.md. Full-workload peaks remain unmeasured.
# Longer context/audio and additional requests require measured recalibration.
CATALOG = {
    "qwen3-8b": {
        "path": "models/model-qwen3-8b/source", "runtime": "fast",
        "packages": ["torch", "transformers", "accelerate"],
        "options": {"nf4": (8192, PROFILES["qwen3-8b"]["ram_budget_mib"], 6.0)},
    },
    "qwen3-asr-1.7b": {
        "path": "models/asr-comparison/qwen3-asr-1.7b/source",
        "runtime": "asr-compare-qwen", "packages": ["torch", "qwen_asr"],
        "options": {"bf16": (PROFILES["qwen3-asr-1.7b"]["vram_budget_mib"],
                              PROFILES["qwen3-asr-1.7b"]["ram_budget_mib"], 8.0)},
    },
    "bge-small-zh": {
        "path": "models/model-bge-small-zh-v1.5", "runtime": "retrieval",
        "packages": ["torch", "transformers", "faiss"],
        "options": {"cpu-fp32": (0, 1024, 0.0)},
    },
    "rapidocr": {
        "path": "runtime/ocr-models", "runtime": "ocr",
        "packages": ["rapidocr", "onnxruntime"],
        "options": {"cpu-fp32": (0, 1024, 0.0)},
    },
}


def _run(command):
    return subprocess.run(command, capture_output=True, text=True, check=True,
                          timeout=25, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def probe_hardware():
    result = {"observed_at": datetime.now(timezone.utc).isoformat(),
              "cpu_cores": os.cpu_count(), "ram_free_mib": None,
              "ram_total_mib": None, "commit_free_mib": None,
              "commit_limit_mib": None, "gpus": [], "errors": []}
    try:
        if os.name != "nt":
            raise OSError("RAM probe currently supports Windows only")
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in
                ("total", "available", "page_total", "page_available", "virtual_total",
                 "virtual_available", "extended")]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("GlobalMemoryStatusEx failed")
        result.update(ram_total_mib=status.total // 1024**2,
                      ram_free_mib=status.available // 1024**2,
                      commit_free_mib=status.page_available // 1024**2,
                      commit_limit_mib=status.page_total // 1024**2)
    except OSError:
        result["errors"].append("ram_probe_failed")
    try:
        output = _run(["nvidia-smi", "--query-gpu=index,name,memory.total,memory.free,compute_cap",
                       "--format=csv,noheader,nounits"]).stdout
        for row in csv.reader(output.splitlines()):
            index, name, total, free, capability = (item.strip() for item in row)
            result["gpus"].append({"index": int(index), "name": name,
                                   "total_mib": int(total), "free_mib": int(free),
                                   "capability": float(capability)})
    except (OSError, ValueError, subprocess.SubprocessError):
        result["errors"].append("gpu_probe_failed")
    return result


def probe_installation(root=ROOT):
    installed, runtimes = {}, {}
    code = ("import importlib.util,json\n"
            "packages={p:importlib.util.find_spec(p) is not None for p in "
            "['torch','transformers','accelerate','bitsandbytes','qwen_asr','faiss','rapidocr','onnxruntime']}\n"
            "cuda=False\nif packages['torch']:\n import torch\n cuda=torch.cuda.is_available()\n"
            "print(json.dumps({'cuda':cuda,'packages':packages}))")
    for model, spec in CATALOG.items():
        folder = root / spec["path"]
        weights = list(folder.glob("*.safetensors")) + list(folder.glob("*.pt"))
        index = folder / "model.safetensors.index.json"
        complete = bool(weights)
        if index.is_file():
            try:
                names = set(json.loads(index.read_text(encoding="utf-8"))["weight_map"].values())
                complete = bool(names) and all(isinstance(name, str) and
                    Path(name).name == name and (folder / name).is_file() for name in names)
            except (OSError, KeyError, TypeError, ValueError):
                complete = False
        configured = (folder / "config.json").is_file() or (folder / "configuration.json").is_file()
        installed[model] = {"weights_present": complete and configured}
        if model == "rapidocr":
            installed[model]["weights_present"] = all((folder / name).is_file() for name in (
                "PP-OCRv6_det_small.onnx", "ch_ppocr_mobile_v2.0_cls_mobile.onnx", "PP-OCRv6_rec_small.onnx"))
        if model == "qwen3-8b":
            fp8 = root / "models/model-qwen3-8b-fp8/source"
            try:
                names = set(json.loads((fp8 / "model.safetensors.index.json").read_text(
                    encoding="utf-8"))["weight_map"].values())
                installed[model]["fp8_present"] = bool(names) and all(
                    isinstance(name, str) and Path(name).name == name and
                    (fp8 / name).is_file() for name in names) and (fp8 / "config.json").is_file()
            except (OSError, KeyError, TypeError, ValueError):
                installed[model]["fp8_present"] = False
        runtime = spec["runtime"]
        if runtime not in runtimes:
            python = root / "runtime" / runtime / (".pixi/envs/default/python.exe"
                if runtime == "retrieval" else "Scripts/python.exe")
            try:
                runtimes[runtime] = json.loads(_run([str(python), "-B", "-c", code]).stdout.strip())
            except (OSError, ValueError, subprocess.SubprocessError):
                runtimes[runtime] = {"cuda": False, "packages": {}, "error": "runtime_probe_failed"}
        installed[model].update(runtimes[runtime])
    return installed


def evaluate(hardware, installed):
    gpu = next((g for g in hardware.get("gpus", []) if g["index"] == 0), None)
    rows = {}
    for model, spec in CATALOG.items():
        info = installed.get(model, {})
        options = {}
        for quant, (vram, ram, capability) in spec["options"].items():
            reasons = []
            if not info.get("fp8_present" if quant == "fp8" else "weights_present"):
                reasons.append("model_files_missing_or_incomplete")
            packages = spec["packages"] + (["bitsandbytes"] if quant in {"nf4", "fp4", "int8"} else [])
            if any(not info.get("packages", {}).get(p) for p in packages):
                reasons.append("runtime_dependencies_missing")
            if vram:
                if not gpu or not info.get("cuda"):
                    reasons.append("cuda_unavailable")
                elif gpu["capability"] < capability:
                    reasons.append("gpu_architecture_unsupported")
                if gpu and gpu["free_mib"] < vram + GPU_RESERVE_MIB:
                    reasons.append("insufficient_free_vram")
            available = hardware.get("ram_free_mib")
            if available is None:
                reasons.append("ram_unknown")
            elif available < ram + PROFILES[model].get("ram_load_reserve_mib", RAM_RESERVE_MIB):
                reasons.append("insufficient_free_ram")
            if PROFILES[model].get("commit_budget_mib") is not None:
                commit = hardware.get("commit_free_mib")
                if commit is None or commit < PROFILES[model]["commit_budget_mib"] + RAM_RESERVE_MIB:
                    reasons.append("insufficient_or_unknown_commit")
            options[quant] = {"estimated_fit": not reasons, "reasons": reasons,
                              "vram_mib": vram, "ram_mib": ram,
                              "ram_load_reserve_mib": PROFILES[model].get("ram_load_reserve_mib", RAM_RESERVE_MIB),
                              "peak_profile": PROFILES[model],
                              "evidence": "conservative_budget_not_a_load_test"}
        rows[model] = options
    return {"hardware": hardware, "models": rows, "gpu_reserve_mib": GPU_RESERVE_MIB,
            "ram_reserve_mib": RAM_RESERVE_MIB,
            "limitations": ["gpu_0_only", "no_cpu_offload", "bounded_single_request",
                            "unlisted_quantization_requires_adapter_and_validation",
                            "selection_not_connected_to_existing_loaders",
                            "vision_api_is_remote_not_a_local_model",
                            "browser_embedded_models_are_external_unmeasured_workloads",
                            "maximum_workload_peaks_pending_measurement"]}


def check_selection(report, selection, mode):
    if mode not in {"sequential", "simultaneous"} or not isinstance(selection, dict) or not selection:
        raise ValueError("Choose a mode and at least one model")
    if len(selection) > len(CATALOG):
        raise ValueError("Too many models")
    options = []
    for model, quant in selection.items():
        if model not in report["models"] or quant not in report["models"][model]:
            raise ValueError("Unknown model or unsupported quantization")
        option = report["models"][model][quant]
        if not option["estimated_fit"]:
            raise ValueError(f"{model}/{quant}: {','.join(option['reasons'])}")
        options.append(option)
    aggregate = sum if mode == "simultaneous" else max
    vram = aggregate(o["vram_mib"] for o in options)
    ram = aggregate(o["ram_mib"] for o in options)
    gpu = next((g for g in report["hardware"].get("gpus", []) if g["index"] == 0), None)
    if vram and (not gpu or vram + GPU_RESERVE_MIB > gpu["free_mib"]):
        raise ValueError("Combined models exceed free GPU memory; select sequential or fewer models")
    ram_reserve = max(o.get("ram_load_reserve_mib", RAM_RESERVE_MIB) for o in options)
    if ram + ram_reserve > report["hardware"]["ram_free_mib"]:
        raise ValueError("Combined models exceed free RAM; select sequential or fewer models")
    return {"mode": mode, "models": dict(selection), "vram_budget_mib": vram,
            "ram_budget_mib": ram, "status": "estimated_fit_requires_runtime_recheck"}


def candidate_plans(report, models):
    if not models or len(models) != len(set(models)) or any(m not in CATALOG for m in models):
        raise ValueError("Select unique registered models")
    choices = [[q for q, option in report["models"][m].items() if option["estimated_fit"]]
               for m in models]
    plans = []
    for combination in itertools.product(*choices):
        selection = dict(zip(models, combination))
        for mode in ("sequential", "simultaneous"):
            try:
                plans.append(check_selection(report, selection, mode))
            except ValueError:
                pass
    return plans
