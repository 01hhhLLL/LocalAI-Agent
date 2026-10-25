"""Advisory resource estimates and OS-owned serial model leases."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_VERSION = "voice-sequential-v3-no-resource-gates"
# Retained for the standalone planning tool, never enforced by live loaders.
VRAM_RESERVE_MIB = RAM_RESERVE_MIB = 2048
TEXT_CONTEXT_TOKENS = 4096
MAX_OUTPUT_TOKENS = 512
PROFILES = {
    "qwen3-8b": {"vram_budget_mib": 8192, "ram_budget_mib": 5120,
                  "ram_load_reserve_mib": 512, "commit_budget_mib": 15360,
                  "max_context_tokens": TEXT_CONTEXT_TOKENS, "max_output_tokens": MAX_OUTPUT_TOKENS},
    "qwen3-asr-1.7b": {"vram_budget_mib": 6656, "ram_budget_mib": 5120,
                       "ram_load_reserve_mib": 512, "commit_budget_mib": 15360,
                       "max_audio_seconds": 180, "max_output_tokens": MAX_OUTPUT_TOKENS},
    "bge-small-zh": {"vram_budget_mib": 0, "ram_budget_mib": 1024, "max_query_characters": 80},
    "rapidocr": {"vram_budget_mib": 0, "ram_budget_mib": 1024, "max_image_side": 4096},
}
for profile in PROFILES.values():
    profile.update(load_peak_mib=None, resident_mib=None, maximum_inference_peak_mib=None,
                   measured_resources={stage: {"ram_mib": None, "vram_mib": None}
                                       for stage in ("load_peak", "resident", "maximum_inference_peak")},
                   peak_evidence="pending_maximum_workload_measurement")

PROFILES["qwen3-asr-1.7b"]["observed_resources"] = {
    "source": "docs/voice-resource-live-fix-20261015.md",
    "audio_seconds": 180, "fixture": "repeated_saved_recording",
    "load_peak_rss_mib": 5110, "resident_rss_mib": 1126,
    "inference_peak_cuda_allocated_mib": 5235,
    "inference_peak_cuda_reserved_mib": 6398,
    "universal_maximum_verified": False,
}
PROFILES["qwen3-8b"]["observed_resources"] = {
    "source": "docs/voice-resource-live-fix-20261015.md",
    "input_tokens": 3578, "output_tokens": 512,
    "load_peak_rss_mib": 4810, "resident_rss_mib": 970,
    "inference_peak_cuda_allocated_mib": 7307,
    "inference_peak_cuda_reserved_mib": 7506,
    "universal_maximum_verified": False,
}


class ResourceError(RuntimeError):
    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report


def probe_resources():
    from model_preflight import probe_hardware
    hardware = probe_hardware()
    gpu = next((g for g in hardware["gpus"] if g["index"] == 0), {})
    resources = {"gpu_free_mib": gpu.get("free_mib"), "ram_free_mib": hardware.get("ram_free_mib"),
                 "commit_free_mib": hardware.get("commit_free_mib")}
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ("peak_ws", "ws", "peak_pool", "pool",
                    "peak_nonpool", "nonpool", "pagefile", "peak_pagefile", "private")]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        function = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        function.restype = wintypes.BOOL
        if function(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            resources["process_commit_mib"] = counters.private // 1024**2
    return resources


def admission(model, resources):
    profile = PROFILES[model]
    reasons = []
    for resource, budget, reserve in (("ram_free_mib", "ram_budget_mib", profile.get("ram_load_reserve_mib", RAM_RESERVE_MIB)),
                                       ("gpu_free_mib", "vram_budget_mib", VRAM_RESERVE_MIB)):
        if budget == "vram_budget_mib" and not profile[budget]:
            continue
        available = resources.get(resource)
        if available is None or available < profile[budget] + reserve:
            reasons.append(resource + "_insufficient_or_unknown")
    if profile.get("commit_budget_mib") is not None:
        # The text service imports its runtime before admission. Its already
        # committed pages are part of the measured whole-process peak, not a
        # second allocation. Only this owning process may receive that credit.
        commit_required = max(0, profile["commit_budget_mib"] - resources.get("process_commit_mib", 0)) + RAM_RESERVE_MIB
        available = resources.get("commit_free_mib")
        if available is None or available < commit_required:
            reasons.append("commit_free_mib_insufficient_or_unknown")
    else:
        commit_required = 0
    return {"admitted": not reasons, "reasons": reasons, "model": model,
            "resources": dict(resources),
            "required_free_ram_mib": profile["ram_budget_mib"] + profile.get("ram_load_reserve_mib", RAM_RESERVE_MIB),
            "required_free_commit_mib": commit_required,
            "required_free_vram_mib": profile["vram_budget_mib"] + VRAM_RESERVE_MIB,
            "profile": profile.copy(), "policy_version": POLICY_VERSION,
            "maximum_workload_verified": False}


def require_admission(model, probe=probe_resources):
    """Compatibility entry point: live fixed-budget admission is disabled."""
    PROFILES[model]
    return {"admitted": True, "reasons": [], "model": model,
            "resource_checks_enabled": False, "policy_version": POLICY_VERSION}


def require_headroom(model, probe=probe_resources, *, phase="resident"):
    """Compatibility entry point: no resource sampling or threshold stopping."""
    PROFILES[model]
    return {"resource_checks_enabled": False, "phase": phase}


class FileLease:
    """Nonblocking file-byte lease; the OS releases it on exit, including crashes."""
    def __init__(self, name, directory):
        self.stream = None
        directory.mkdir(parents=True, exist_ok=True)
        stream = (directory / (name + ".lock")).open("a+b")
        try:
            if stream.seek(0, 2) == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise ResourceError("owned_operation_already_running") from None
        self.stream = stream

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class ModelLease(FileLease):
    def __init__(self, model, directory=ROOT / "runtime" / "model-ownership"):
        self.model = model
        # ponytail: GPU models serialize on one device. A future measured
        # co-resident plan requires reservations rather than another lock name.
        super().__init__("gpu0" if PROFILES[model]["vram_budget_mib"] else model, directory)
        self.admission_lease = None
        try:
            self.admission_lease = FileLease("load-admission", directory)
        except Exception:
            super().close()
            raise

    def loaded(self):
        if self.admission_lease is not None:
            self.admission_lease.close()
            self.admission_lease = None

    def close(self):
        self.loaded()
        super().close()
