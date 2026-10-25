"""One name lookup for typed/recognized applications and installed toolbox entries."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import ctypes
import struct
from contextlib import closing
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

from application_registry import DATABASE, get_application, normalized
from tuba_tool_catalog import entries


def file_details(path):
    try:
        stat = Path(path).stat()
        return _file_details(str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return {}


@lru_cache(maxsize=512)
def _file_details(path, _mtime, _size):
    """Read Windows version resources; never execute the inspected program."""
    if not hasattr(ctypes, "WinDLL"):
        return {}
    from ctypes import wintypes
    api = ctypes.WinDLL("version")
    api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
    api.VerQueryValueW.argtypes = [wintypes.LPCVOID, wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.UINT)]
    size = api.GetFileVersionInfoSizeW(path, None)
    if not size:
        return {}
    buffer = ctypes.create_string_buffer(size)
    if not api.GetFileVersionInfoW(path, 0, size, buffer):
        return {}
    pointer, length = wintypes.LPVOID(), wintypes.UINT()
    if not api.VerQueryValueW(buffer, r"\VarFileInfo\Translation", ctypes.byref(pointer), ctypes.byref(length)) or length.value < 4:
        return {}
    language, codepage = struct.unpack("<HH", ctypes.string_at(pointer, 4))
    result = {}
    for key, field in (("description", "FileDescription"), ("product_version", "ProductVersion")):
        block = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\{field}"
        if api.VerQueryValueW(buffer, block, ctypes.byref(pointer), ctypes.byref(length)) and length.value:
            result[key] = ctypes.wstring_at(pointer, length.value).split("\0", 1)[0]
    return result


def toolbox_targets():
    """Expose real launch/content entries, never inventory scripts or guessed paths."""
    targets = []
    for item in entries():
        path = Path(item["path"])
        if path.suffix.casefold() not in {".exe", ".jpg", ".jpeg", ".png"}:
            continue
        if re.search(r"(?i)^(?:unins|uninstall|setup|install|update)|(?:helper|crashreport)", path.stem):
            continue
        identity = "tuba-entry-" + hashlib.sha256(item["relative_path"].casefold().encode()).hexdigest()[:24]
        targets.append({"app_id": identity, "display_name": item["name"],
                        "canonical_name": item["name"], "tool_name": "toolbox_open",
                        "executable_path": item["path"], "entry_path": item["path"],
                        "parent_name": "图吧工具箱", "category": item["category"],
                        "folder": item["folder"], "target_kind": "toolbox",
                        "kind": item["kind"],
                        "aliases": [item["name"]], "visible": True,
                        "source": "installed_toolbox_catalog"})
    graphical = {}
    for item in targets:
        if item["kind"] == "launcher" and item["canonical_name"].casefold().endswith("_gui"):
            metadata = file_details(item["entry_path"])
            if "gui" in metadata.get("description", "").casefold():
                family = item["canonical_name"][:-4]
                item.update(metadata, display_name=family, canonical_name=family, entry_role="graphical")
                item["aliases"].append(family)
                graphical[(Path(item["entry_path"]).parent, normalized(family))] = True
    for item in targets:
        family_key = (Path(item["entry_path"]).parent, normalized(item["canonical_name"]))
        if item.get("entry_role") != "graphical" and family_key in graphical:
            metadata = file_details(item["entry_path"])
            if re.search(r"\bcli\b", metadata.get("description", ""), re.I):
                canonical = item["canonical_name"] + " CLI"
                item.update(metadata, display_name=canonical, canonical_name=canonical,
                            aliases=[canonical], entry_role="command_line")
    return targets


def get_target(app_id, registry_db=DATABASE):
    if isinstance(app_id, str) and app_id.startswith("tuba-entry-"):
        return next((item for item in toolbox_targets() if item["app_id"] == app_id), None)
    return get_application(app_id, registry_db)


def _targets(registry_db):
    # Read-only: a typo in a registry path must not create an empty database.
    uri = Path(registry_db).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as database:
        database.row_factory = sqlite3.Row
        apps = [dict(row) for row in database.execute("""SELECT app_id, display_name,
            tool_name, executable_path, visible FROM applications
            WHERE enabled=1 AND approved=1 ORDER BY app_id""")]
        aliases = {}
        for app_id, alias in database.execute("SELECT app_id, alias FROM aliases WHERE approved=1"):
            aliases.setdefault(app_id, []).append(alias)
    for app in apps:
        app.update(canonical_name=app["display_name"], target_kind="application",
                   aliases=[app["display_name"], *aliases.get(app["app_id"], [])])
    unavailable = []
    try:
        apps.extend(toolbox_targets())
    except (OSError, ValueError, KeyError):
        unavailable.append("installed_toolbox_catalog")
    return apps, unavailable


def literal_query(name, literal=False):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ValueError("application name must contain 1 to 100 characters")
    insist = re.match(r"^(?:名字就是|名称就是|就是叫|就叫)\s*", name.strip())
    if insist:
        name, literal = name.strip()[insist.end():], True
    if not normalized(name):
        raise ValueError("application name is empty")
    return name.strip(), bool(literal)


def _candidate(item, score, alias, exact):
    return {key: value for key, value in item.items() if key != "aliases"} | file_details(item["executable_path"]) | {
        "matched_alias": alias, "score": score, "name_similarity": score,
        "fusion_rank_score": score, "local_approved": True, "executes": False,
        "sources": ["installed_toolbox_catalog" if item["target_kind"] == "toolbox" else "exact_alias" if exact else "local_alias"],
        "relevance_checked": True,
        "relevance_basis": {"kind": "registered_name" if exact else "catalog_name_similarity", "matched_alias": alias},
        "auto_select_allowed": True}


def search_name(name, registry_db=DATABASE, *, literal=False, parent=None):
    original = name
    name, literal = literal_query(name, literal)
    key = normalized(name)
    targets, unavailable = _targets(registry_db)
    if parent is not None:
        if parent != "tuba":
            raise ValueError("Unknown toolbox scope")
        targets = [item for item in targets if item.get("target_kind") == "toolbox"]
    exact, near = [], []
    # ponytail: one linear pass over the small existing registry/catalog. Reuse
    # its index if the inventory grows; no second embedding/model service.
    for item in targets:
        aliases = item["aliases"]
        matched = next((alias for alias in aliases if normalized(alias) == key), None)
        if matched is not None:
            exact.append(_candidate(item, 1.0, matched, True))
            continue
        if literal or not item.get("visible", True) or len(key) < 3:
            continue
        scores = [(SequenceMatcher(None, key, normalized(alias)).ratio(), alias) for alias in aliases
                  if len(normalized(alias)) >= 3
                  and re.findall(r"\d+", key) == re.findall(r"\d+", normalized(alias))]
        if scores:
            score, alias = max(scores)
            if score >= .72:
                near.append(_candidate(item, round(score, 4), alias, False))
    exact.sort(key=lambda item: (item["display_name"].casefold(), item.get("entry_path", ""), item["app_id"]))
    near.sort(key=lambda item: (-item["score"], item["display_name"].casefold(), item["app_id"]))
    candidates = exact
    corrected = None
    reliable = bool(exact)
    if not exact and near:
        top_name = normalized(near[0]["canonical_name"])
        competing = next((item["score"] for item in near if normalized(item["canonical_name"]) != top_name), 0)
        reliable = near[0]["score"] >= .74 and near[0]["score"] - competing >= .12
        if reliable:
            corrected = near[0]["canonical_name"]
            candidates = [item for item in near if normalized(item["canonical_name"]) == top_name]
        else:
            candidates = near[:5]
    same_name = len(exact) > 1 or (len(candidates) > 1 and len({normalized(item["canonical_name"]) for item in candidates}) == 1)
    for item in candidates:
        item["auto_select_allowed"] = reliable and not same_name
    confirmed = bool(exact) and len(exact) == 1
    result = {"status": "confirmed_alias" if confirmed else "ambiguous" if candidates else "no_match",
              "requested_name": name, "original_name": original, "corrected_name": corrected,
              "literal_name": literal, "candidates": candidates, "executes": False,
              "parent_scope": parent,
              "same_name_entries": same_name, "complete_matches": bool(exact) or same_name,
              "searched_sources": ["application_registry", "approved_aliases", "installed_toolbox_catalog"],
              "unavailable_sources": unavailable, "web_lookup_status": "not_needed",
              "correction_basis": "installed_name_similarity" if corrected else None}
    if confirmed:
        item = exact[0]
        result.update(app_id=item["app_id"], display_name=item["display_name"],
                      matched_aliases=[name, item["matched_alias"]], target_kind=item["target_kind"],
                      resolution_source="unified_exact_name")
    return result
