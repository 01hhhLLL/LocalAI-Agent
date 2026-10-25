"""Shared ModelScope vision API client and Windows Credential Manager access."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
try:
    from .step_runtime import Deadline
except ImportError:
    from step_runtime import Deadline


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "vision-provider.toml"


class VisionQuotaError(RuntimeError):
    """The provider reported a quota or request-rate limit."""


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _Credential(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("target_name", ctypes.c_wchar_p),
        ("comment", ctypes.c_wchar_p),
        ("last_written", _FileTime),
        ("credential_blob_size", ctypes.c_uint32),
        ("credential_blob", ctypes.POINTER(ctypes.c_ubyte)),
        ("persist", ctypes.c_uint32),
        ("attribute_count", ctypes.c_uint32),
        ("attributes", ctypes.c_void_p),
        ("target_alias", ctypes.c_wchar_p),
        ("user_name", ctypes.c_wchar_p),
    ]


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("rb") as stream:
        return tomllib.load(stream)


def credential_target(config: dict[str, Any], slot: int) -> str:
    if slot not in (1, 2):
        raise ValueError("Token slot must be 1 or 2")
    field = "credential_target_primary" if slot == 1 else "credential_target_secondary"
    return str(config["provider"][field])


def store_token(token: str, slot: int = 1) -> None:
    """Store a token as a generic credential for this Windows logon."""
    if os.name != "nt":
        raise RuntimeError("Windows Credential Manager is only available on Windows")
    token = token.strip()
    if not token or any(ord(ch) < 32 for ch in token):
        raise ValueError("Token is empty or invalid")

    config = load_config()
    blob = token.encode("utf-8")
    if len(blob) > 2560:  # Windows CRED_MAX_CREDENTIAL_BLOB_SIZE
        raise ValueError("Token exceeds the Windows Credential Manager size limit")
    blob_buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    target = credential_target(config, slot)
    user_name = "ModelScope"
    credential = _Credential()
    credential.type = 1  # CRED_TYPE_GENERIC
    credential.target_name = target
    credential.user_name = user_name
    credential.credential_blob_size = len(blob)
    credential.credential_blob = ctypes.cast(blob_buffer, ctypes.POINTER(ctypes.c_ubyte))
    credential.persist = 2  # CRED_PERSIST_LOCAL_MACHINE

    advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    advapi.CredWriteW.argtypes = [ctypes.POINTER(_Credential), ctypes.c_uint32]
    advapi.CredWriteW.restype = ctypes.c_int
    if not advapi.CredWriteW(ctypes.byref(credential), 0):
        raise OSError(ctypes.get_last_error(), "Windows Credential Manager could not save the token")


def _read_credential(target: str) -> str | None:
    if os.name != "nt":
        return None
    advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    advapi.CredReadW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.POINTER(_Credential)),
    ]
    advapi.CredReadW.restype = ctypes.c_int
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    advapi.CredFree.restype = None
    pointer = ctypes.POINTER(_Credential)()
    if not advapi.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        error_code = ctypes.get_last_error()
        if error_code == 1168:  # ERROR_NOT_FOUND
            return None
        raise OSError(error_code, "Windows Credential Manager could not read the token")
    try:
        credential = pointer.contents
        raw = ctypes.string_at(credential.credential_blob, credential.credential_blob_size)
        return raw.decode("utf-8")
    finally:
        advapi.CredFree(pointer)


def get_token(slot: int = 1) -> str:
    if os.environ.get("LOCALAI_DISABLE_VISION_API") == "1":
        raise RuntimeError("vision_api_disabled_for_this_run")
    env_name = "MODELSCOPE_API_KEY" if slot == 1 else "MODELSCOPE_API_KEY_2"
    from_environment = os.environ.get(env_name, "").strip()
    if from_environment:
        return from_environment
    token = _read_credential(credential_target(load_config(), slot))
    if not token:
        raise RuntimeError(
            f"ModelScope visual token {slot} is not configured. "
            f"Run scripts/vision-locate.ps1 --save-token --slot {slot} locally."
        )
    return token


def _endpoint(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    if (parsed.scheme != "https" or parsed.hostname != "api-inference.modelscope.cn"
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Vision API must use the official HTTPS ModelScope inference host")
    return base_url.rstrip("/") + "/chat/completions"


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [item.get("text", "") for item in content if isinstance(item, dict)]
        return "".join(part for part in parts if isinstance(part, str)).strip()
    return ""


def ask_vision(prompt: str, image: bytes, mime_type: str = "image/png", slot: int = 1, *, deadline=None) -> str:
    """Send one image and prompt through the project's shared vision provider."""
    if os.environ.get("LOCALAI_DISABLE_VISION_API") == "1":
        raise RuntimeError("vision_api_disabled_for_this_run")
    if not prompt.strip() or len(prompt) > 12000:
        raise ValueError("Prompt must contain 1 to 12000 characters")
    if not image or len(image) > 24 * 1024 * 1024:
        raise ValueError("Image must be non-empty and at most 24 MiB")
    if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise ValueError("Unsupported image type")

    config = load_config()
    deadline = deadline or Deadline.inherited("vision_request")
    provider = config["provider"]
    request_settings = config["request"]
    image_url = f"data:{mime_type};base64,{base64.b64encode(image).decode('ascii')}"
    body = {
        "model": provider["model"],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        }],
        "stream": False,
        "temperature": request_settings["temperature"],
        "top_p": request_settings["top_p"],
        "top_k": request_settings["top_k"],
        "presence_penalty": request_settings["presence_penalty"],
        "max_tokens": request_settings["max_tokens"],
        "chat_template_kwargs": {"enable_thinking": request_settings["enable_thinking"]},
    }
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        _endpoint(str(provider["base_url"])),
        data=encoded,
        headers={
            "Authorization": f"Bearer {get_token(slot)}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=deadline.remaining(int(provider["timeout_seconds"]))) as response:
            result = json.loads(response.read().decode("utf-8"))
        deadline.remaining()
    except urllib.error.HTTPError as error:
        try:
            error_body = error.read(8192).decode("utf-8", errors="ignore")
        except OSError:
            error_body = ""
        if error.code == 429 or re.search(
            r"quota|rate.?limit|daily.?limit|exceed|too.many.requests|额度|频率|限额|流控",
            error_body,
            re.I,
        ):
            raise VisionQuotaError(
                f"ModelScope returned HTTP {error.code} (rate or quota limit). "
                "Check whether today's quota is exhausted; if so, add a second visual token."
            ) from None
        if error.code == 401:
            raise RuntimeError("ModelScope rejected the token (HTTP 401); replace the saved token") from None
        if error.code == 403:
            raise RuntimeError("ModelScope denied this model or account (HTTP 403)") from None
        raise RuntimeError(f"ModelScope vision request failed with HTTP {error.code}") from None
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", None)
        if isinstance(reason, TimeoutError):
            raise TimeoutError("vision request deadline exceeded") from None
        message = type(reason).__name__ if reason is not None else "network error"
        raise RuntimeError(f"Could not reach ModelScope vision API ({message})") from None

    api_error = result.get("error") if isinstance(result, dict) else None
    if api_error:
        message = api_error.get("message", "") if isinstance(api_error, dict) else str(api_error)
        if re.search(r"quota|rate.?limit|daily.?limit|exceed|额度|频率", message, re.I):
            raise VisionQuotaError(
                "ModelScope reported a rate or quota limit. Check today's quota; "
                "if it is exhausted, add a second visual token."
            )
        raise RuntimeError("ModelScope returned an API error")

    try:
        text = _content_text(result["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError):
        text = ""
    if not text:
        raise RuntimeError("ModelScope returned no vision response text")
    return text
