"""Resolve tool names from the existing installation inventory, without guessed UI paths."""
from __future__ import annotations
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/tuba-tool-paths.json"
CONTENT_SUFFIXES = {".jpg", ".jpeg", ".png", ".html", ".htm", ".pdf"}
LAUNCH_SUFFIXES = {".exe", ".lnk", ".url", ".bat", ".cmd"}


def normalized(value):
    return re.sub(r"[^a-z0-9\u3400-\u9fff]", "", value.casefold())


def entries(catalog_path=CATALOG):
    catalog = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    install = Path(catalog["install_root"]).resolve(strict=True)
    result = []
    for item in catalog["files"]:
        relative = Path(item["path"])
        if (relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 4
                or relative.parts[0] != "tools"):
            continue
        path = (install / relative).resolve()
        if (not path.is_relative_to(install) or not path.is_file() or path.is_symlink()
                or path.suffix.casefold() not in CONTENT_SUFFIXES | LAUNCH_SUFFIXES):
            continue
        result.append({"name": path.stem, "folder": relative.parts[2],
                       "category": relative.parts[1], "relative_path": relative.as_posix(),
                       "path": str(path), "kind": "content" if path.suffix.casefold() in CONTENT_SUFFIXES else "launcher",
                       "ui_path_verified": False, "source": "installed_file_inventory"})
    return result


def lookup(name, catalog_path=CATALOG):
    key = normalized(name)
    if not key:
        raise ValueError("Tool name is empty")
    items = entries(catalog_path)
    exact = [item for item in items if normalized(item["name"]) == key]
    if not exact:
        exact = [item for item in items if normalized(item["folder"]) == key]
    return {"status": "matched" if len(exact) == 1 else "ambiguous" if exact else "not_found",
            "requested_name": name, "candidates": exact, "total_matches": len(exact)}


def document():
    items = entries()
    lines = ["# 图吧工具箱工具入口与内容路径", "",
             "来源：本机安装目录与已有文件清单。文件目录路径已经逐项检查存在；界面分类、点击路径和每项启动结果未逐一核验。", "",
             "程序通过同一清单查找名称，唯一命中才使用；多个入口保留歧义，不猜选。REA 的分析结果单独留证，不冒充目录扫描的来源。", "",
             "| 目录分类 | 工具文件夹 | 名称 | 类型 | 实际路径 |", "|---|---|---|---|---|"]
    for item in items:
        values = [item[key].replace("|", "\\|") for key in ("category", "folder", "name", "kind", "path")]
        lines.append("| " + " | ".join(values) + " |")
    return {"entries": items, "markdown": "\n".join(lines) + "\n"}
