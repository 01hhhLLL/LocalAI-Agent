"""Notice newly installed app names without approving or launching them."""

from __future__ import annotations

import os
import re
import sqlite3
import time
from html import escape
from contextlib import closing
from pathlib import Path

from application_registry import DATABASE, normalized

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "docs" / "application-alias-review-20261009.md"
SKIP = re.compile(r"卸载|删除|修复|更新|安装|uninstall|remove|repair|update|setup|installer", re.I)


def installed_names(max_seconds: float = 8) -> set[str]:
    """Read Windows install labels and Start Menu shortcut names, not targets."""
    deadline = time.monotonic() + max_seconds
    names: set[str] = set()
    for base in (os.environ.get("APPDATA"), os.environ.get("PROGRAMDATA")):
        if not base:
            continue
        folder = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        if not folder.is_dir():
            continue
        for shortcut in folder.rglob("*.lnk"):
            if time.monotonic() >= deadline:
                raise TimeoutError("Start Menu discovery exceeded its time limit")
            name = shortcut.stem.strip()
            if 2 <= len(name) <= 100 and not SKIP.search(name):
                names.add(name)
    try:
        import winreg
    except ImportError:
        return names
    subkey = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                root = winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | view)
            except OSError:
                continue
            with root:
                for index in range(winreg.QueryInfoKey(root)[0]):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("installed-app discovery exceeded its time limit")
                    try:
                        child = winreg.OpenKey(root, winreg.EnumKey(root, index))
                        with child:
                            name = winreg.QueryValueEx(child, "DisplayName")[0]
                    except OSError:
                        continue
                    if isinstance(name, str):
                        name = name.strip()
                        if 2 <= len(name) <= 100 and not SKIP.search(name):
                            names.add(name)
    return names


def record_inventory(names: set[str], database_path: Path = DATABASE,
                     review_path: Path = REVIEW) -> dict:
    """First run sets a baseline; later additions appear as unapproved rows."""
    if not names:
        return {"status": "no_names", "new_pending": []}
    by_key = {normalized(name): name for name in sorted(names) if normalized(name)}
    with closing(sqlite3.connect(database_path, timeout=10)) as database:
        database.execute("CREATE TABLE IF NOT EXISTS discovery_seen "
                         "(name_key TEXT PRIMARY KEY, display_name TEXT NOT NULL)")
        database.execute("CREATE TABLE IF NOT EXISTS discovery_meta "
                         "(name TEXT PRIMARY KEY, value TEXT NOT NULL)")
        baseline = database.execute(
            "SELECT value FROM discovery_meta WHERE name = 'baseline_done'").fetchone()
        seen = {row[0] for row in database.execute("SELECT name_key FROM discovery_seen")}
        approved = {row[0] for row in database.execute("""
            SELECT l.alias_key FROM aliases AS l JOIN applications AS a ON a.app_id = l.app_id
            WHERE a.enabled = 1 AND a.approved = 1 AND l.approved = 1
        """)}
        pending = [] if baseline is None else [
            by_key[key] for key in sorted(by_key) if key not in seen and key not in approved]
        if pending and not review_path.is_file():
            raise FileNotFoundError("alias review document is missing")
        if pending:
            existing = review_path.read_text(encoding="utf-8")
            with review_path.open("a", encoding="utf-8") as output:
                if "## 新发现、待核对的程序" not in existing:
                    output.write("\n## 新发现、待核对的程序\n\n"
                                 "这些仅来自安装名称或开始菜单；尚未核验启动入口，不能自动执行。\n\n")
                for name in pending:
                    marker = f"<!-- discovered:{normalized(name)} -->"
                    if marker not in existing:
                        label = escape(name, quote=False).replace("[", "\\[").replace("]", "\\]")
                        output.write(f"- {label}：待核对；你想补的叫法：  {marker}\n")
        with database:
            database.executemany(
                "INSERT OR IGNORE INTO discovery_seen(name_key, display_name) VALUES (?, ?)",
                by_key.items())
            database.execute("INSERT OR IGNORE INTO discovery_meta(name, value) "
                             "VALUES ('baseline_done', '1')")
    return {"status": "baseline" if baseline is None else "updated",
            "new_pending": pending}


def refresh() -> dict:
    return record_inventory(installed_names())
