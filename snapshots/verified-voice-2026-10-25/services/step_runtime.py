"""Shared monotonic cutoffs and ownership-safe subprocess cleanup."""
from __future__ import annotations

import math
import inspect
import os
import subprocess
import time
import threading
from functools import wraps

MAX_STEP_SECONDS = 180


class StepTimeout(TimeoutError):
    pass


class Deadline:
    def __init__(self, name, seconds=MAX_STEP_SECONDS, *, clock=time.monotonic,
                 started=None, expires=None):
        if not math.isfinite(seconds) or not 0 < seconds <= MAX_STEP_SECONDS:
            raise ValueError("step budget must be within 180 seconds")
        self.name, self.clock = name, clock
        self.started = clock() if started is None else started
        self.expires = min(self.started + seconds, expires) if expires is not None else self.started + seconds
        if not all(math.isfinite(v) for v in (self.started, self.expires)):
            raise ValueError("invalid step deadline")

    @classmethod
    def inherited(cls, name, seconds=MAX_STEP_SECONDS):
        value = os.environ.get("LOCALAI_STEP_EXPIRES")
        return cls(name, seconds, expires=float(value) if value else None)

    def remaining(self, cap=MAX_STEP_SECONDS):
        remaining = self.expires - self.clock()
        if remaining <= 0:
            raise StepTimeout(f"{self.name} exceeded its step deadline")
        return min(remaining, cap, MAX_STEP_SECONDS)

    def environment(self, base=None):
        env = dict(os.environ if base is None else base)
        env["LOCALAI_STEP_EXPIRES"] = str(self.expires)
        return env

    def report(self):
        return {"step": self.name, "elapsed_seconds": round(self.clock() - self.started, 3),
                "budget_seconds": round(self.expires - self.started, 3)}


def stop_owned(process):
    """Only accept the Popen handle created by this caller, never process names."""
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def failure_code(error):
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
        return "deadline_exceeded"
    if isinstance(error, (ImportError, ModuleNotFoundError)):
        return "dependency_missing"
    if isinstance(error, FileNotFoundError):
        return "model_or_runtime_missing"
    if isinstance(error, ValueError):
        return "invalid_input"
    if type(error).__name__ == "ResourceError":
        return "resource_unavailable"
    message = str(error).casefold()
    if "out of memory" in message or "cuda error: memory" in message:
        return "out_of_memory"
    return "worker_failed"


def call_with_deadline(function, *args, deadline, **kwargs):
    """Pass the shared budget to production adapters; tiny injected stubs may omit it."""
    parameters = inspect.signature(function).parameters
    if "deadline" in parameters or any(p.kind == p.VAR_KEYWORD for p in parameters.values()):
        kwargs["deadline"] = deadline
    deadline.remaining()
    result = function(*args, **kwargs)
    deadline.remaining()
    return result


def hard_bounded_step(name, seconds=MAX_STEP_SECONDS):
    """A native model call may ignore Python cancellation; its own worker exits."""
    def decorate(function):
        @wraps(function)
        def bounded(*args, **kwargs):
            deadline = Deadline(name, seconds)
            def cutoff():
                # No prompt, credentials or generated text goes into diagnostics.
                import json
                try:
                    print("LOCALAI_STEP_CUTOFF=" + json.dumps(deadline.report()), flush=True)
                finally:
                    os._exit(124)
            timer = threading.Timer(deadline.remaining(), cutoff)
            timer.daemon = True
            timer.start()
            try:
                result = function(*args, **kwargs)
                deadline.remaining()
                return result
            finally:
                timer.cancel()
        return bounded
    return decorate


class StepSupervisor:
    """A process watchdog advances only when its caller supplies verified evidence."""
    def __init__(self, name, *, on_timeout=None):
        self.on_timeout = on_timeout
        self.timer = None
        self.deadline = Deadline(name)
        self.completed_steps = set()
        self.generation = 0
        self._arm()

    def _arm(self):
        self.generation += 1
        generation, deadline = self.generation, self.deadline
        def cutoff():
            if generation != self.generation:
                return
            report = deadline.report()
            try:
                if self.on_timeout:
                    self.on_timeout(report)
            finally:
                os._exit(124)
        self.timer = threading.Timer(self.deadline.remaining(), cutoff)
        self.timer.daemon = True
        self.timer.start()

    def verified_step(self, name):
        self.deadline.remaining()
        if name in self.completed_steps:
            return False
        self.completed_steps.add(name)
        self.generation += 1
        self.timer.cancel()
        self.deadline = Deadline(name)
        self._arm()
        return True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.generation += 1
        self.timer.cancel()
