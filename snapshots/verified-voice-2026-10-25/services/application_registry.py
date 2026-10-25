"""Small deterministic application registry for voice-command alias resolution."""

from __future__ import annotations

import json
from difflib import SequenceMatcher
import re
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "data" / "application-registry.sqlite3"
ALIAS_KNOWLEDGE = ROOT / "knowledge" / "application-aliases.json"
VISIBILITY_KNOWLEDGE = ROOT / "knowledge" / "application-visibility.json"

SEED_APPS = (
    ("tuba", "图吧工具箱", "launch_tuba_toolbox", "D:/图吧工具箱202601/图吧工具箱2026.exe", 1),
    ("charmap", "字符映射表", "launch_character_map", "C:/Windows/System32/charmap.exe", 0),
    ("epic", "Epic Games Launcher", "launch_epic_games_launcher",
     "C:/Program Files/Epic Games/Launcher/Portal/Binaries/Win64/EpicGamesLauncher.exe", 0),
)
SEED_ALIASES = (
    ("tuba", "图吧工具箱", "canonical"),
    ("tuba", "图板工具箱", "asr_observed"),
    ("tuba", "图霸工具箱", "asr_observed"),
    ("charmap", "字符映射表", "canonical"),
    ("epic", "Epic Games Launcher", "canonical"),
    ("epic", "Epic", "short_name"),
    ("epic", "EPSA", "user_confirmed_asr"),
)


def normalized(text: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


def primary_launch_clause(command: str) -> str:
    """Keep the first launch request separate from later conditional actions."""
    verb = re.search(r"打开|启动|运行", command)
    if verb is None:
        return command
    clause = command[verb.start():]
    followup = re.search(
        r"(?:如果|如需|假如|倘若|若是|然后|随后|接着|之后|并且|"
        r"[,，。;；]|并(?:选择|点击|双击|进入)|再(?:打开|启动|运行|选择|点击))", clause[verb.end() - verb.start():])
    if followup is not None:
        clause = clause[:verb.end() - verb.start() + followup.start()]
    return clause.strip(" \t\r\n，,。；;")


def requested_application_name(command: str) -> str:
    """Extract the named launch target without later account or click instructions."""
    clause = primary_launch_clause(command)
    quoted = re.search(r"《([^》]+)》|“([^”]+)”", clause)
    if quoted is not None:
        return (quoted.group(1) or quoted.group(2)).strip()
    clause = re.sub(r"^.*?(?:打开|启动|运行)", "", clause, count=1)
    clause = re.sub(r"^(?:一下|我电脑上的|电脑上的|本机的|桌面上的)+", "", clause)
    clause = re.sub(r"(?:这款|这个)(?:游戏|软件|应用程序|应用|程序)$", "", clause)
    return clause.strip(" \t\r\n，,。；;：:")


def _sync_alias_knowledge(database: sqlite3.Connection, knowledge_path: Path) -> None:
    if not knowledge_path.is_file():
        return
    document = json.loads(knowledge_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(
            document.get("applications"), list):
        raise ValueError("application alias knowledge format is invalid")
    for entry in document["applications"]:
        if not isinstance(entry, dict):
            raise ValueError("application alias entry is invalid")
        name, aliases, source = (entry.get("canonical_name"), entry.get("aliases"),
                                 entry.get("source"))
        if (not isinstance(name, str) or not name.strip() or len(name) > 100 or
                not isinstance(aliases, list) or not aliases or
                not isinstance(source, str) or not source.strip() or len(source) > 100):
            raise ValueError("application alias entry is invalid")
        apps = database.execute(
            "SELECT app_id FROM applications WHERE display_name = ? AND enabled = 1 AND approved = 1",
            (name,)).fetchall()
        if not apps:
            continue  # Knowledge can precede local installation or approval.
        if len(apps) != 1:
            raise ValueError(f"application name is ambiguous: {name}")
        app_id = apps[0][0]
        for alias in aliases:
            if not isinstance(alias, str) or not alias.strip() or len(alias) > 100:
                raise ValueError("application alias is invalid")
            key = normalized(alias)
            conflicts = database.execute(
                "SELECT app_id FROM aliases WHERE alias_key = ? AND approved = 1 AND app_id != ?",
                (key, app_id)).fetchall()
            if conflicts:
                raise ValueError(f"approved alias conflicts with another application: {alias}")
            database.execute(
                "INSERT OR IGNORE INTO aliases (app_id, alias, alias_key, source, approved) "
                "VALUES (?, ?, ?, ?, 1)", (app_id, alias, key, source))


def _sync_visibility(database: sqlite3.Connection, visibility_path: Path) -> None:
    if not visibility_path.is_file():
        return
    document = json.loads(visibility_path.read_text(encoding="utf-8"))
    hidden = document.get("hidden_canonical_names") if isinstance(document, dict) else None
    if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(hidden, list) or any(
            not isinstance(name, str) or not name.strip() for name in hidden):
        raise ValueError("application visibility knowledge format is invalid")
    for name in hidden:
        database.execute(
            "UPDATE applications SET visible = 0 WHERE display_name = ?", (name,))


def initialize(path: Path = DATABASE, knowledge_path: Path = ALIAS_KNOWLEDGE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as database:
      with database:
        database.execute("PRAGMA foreign_keys = ON")
        database.executescript("""
            CREATE TABLE IF NOT EXISTS applications (
                app_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                executable_path TEXT NOT NULL,
                requires_admin INTEGER NOT NULL CHECK (requires_admin IN (0, 1)),
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                approved INTEGER NOT NULL DEFAULT 0 CHECK (approved IN (0, 1)),
                approval_source TEXT NOT NULL DEFAULT 'unreviewed'
            );
            CREATE TABLE IF NOT EXISTS aliases (
                app_id TEXT NOT NULL REFERENCES applications(app_id),
                alias TEXT NOT NULL,
                alias_key TEXT NOT NULL,
                source TEXT NOT NULL,
                approved INTEGER NOT NULL DEFAULT 1 CHECK (approved IN (0, 1)),
                PRIMARY KEY (app_id, alias_key)
            );
            CREATE INDEX IF NOT EXISTS aliases_key_idx ON aliases(alias_key);
            CREATE TABLE IF NOT EXISTS alias_review (
                review_id INTEGER PRIMARY KEY,
                app_id TEXT NOT NULL REFERENCES applications(app_id),
                alias TEXT NOT NULL,
                alias_key TEXT NOT NULL,
                source TEXT NOT NULL,
                conflict_app_ids TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'approved', 'rejected')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS alias_review_pending_idx
                ON alias_review(state, review_id);
        """)
        _ensure_column(database, "applications", "approved",
                       "INTEGER NOT NULL DEFAULT 0 CHECK (approved IN (0, 1))")
        _ensure_column(database, "applications", "approval_source",
                       "TEXT NOT NULL DEFAULT 'unreviewed'")
        _ensure_column(database, "applications", "visible",
                       "INTEGER NOT NULL DEFAULT 1 CHECK (visible IN (0, 1))")
        _ensure_column(database, "aliases", "approved",
                       "INTEGER NOT NULL DEFAULT 1 CHECK (approved IN (0, 1))")
        database.executemany(
            "INSERT OR IGNORE INTO applications "
            "(app_id, display_name, tool_name, executable_path, requires_admin, "
            "approved, approval_source) VALUES (?, ?, ?, ?, ?, 1, 'user_tested_allowlist')",
            SEED_APPS)
        database.executemany(
            "UPDATE applications SET approved = 1, approval_source = 'user_tested_allowlist' "
            "WHERE app_id = ? AND executable_path = ?",
            ((app_id, executable) for app_id, _, _, executable, _ in SEED_APPS))
        database.executemany(
            "INSERT OR IGNORE INTO aliases (app_id, alias, alias_key, source, approved) "
            "VALUES (?, ?, ?, ?, 1)",
            ((app_id, alias, normalized(alias), source)
             for app_id, alias, source in SEED_ALIASES))
        _sync_alias_knowledge(database, knowledge_path)
        _sync_visibility(database, VISIBILITY_KNOWLEDGE)
        database.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS aliases_fts USING fts5(
                alias_key, alias, app_id UNINDEXED, tokenize='trigram'
            )
        """)
        database.execute("DELETE FROM aliases_fts")
        database.execute("""
            INSERT INTO aliases_fts(alias_key, alias, app_id)
            SELECT l.alias_key, l.alias, l.app_id FROM aliases AS l
            JOIN applications AS a ON a.app_id = l.app_id
            WHERE l.approved = 1 AND a.approved = 1 AND a.enabled = 1
              AND a.visible = 1
        """)


def _ensure_column(database: sqlite3.Connection, table: str,
                   column: str, declaration: str) -> None:
    columns = {row[1] for row in database.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        database.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def get_application(app_id: str, path: Path = DATABASE) -> dict | None:
    with closing(sqlite3.connect(path)) as database:
        database.row_factory = sqlite3.Row
        row = database.execute("""
            SELECT app_id, display_name, tool_name, executable_path, requires_admin,
                   approval_source
            FROM applications WHERE app_id = ? AND enabled = 1 AND approved = 1
        """, (app_id,)).fetchone()
        return dict(row) if row else None


def get_application_by_tool(tool_name: str, path: Path = DATABASE) -> dict | None:
    """Resolve an exposed launch tool to its approved registry entry."""
    with closing(sqlite3.connect(path)) as database:
        database.row_factory = sqlite3.Row
        row = database.execute("""
            SELECT app_id, display_name, tool_name, executable_path, requires_admin,
                   approval_source
            FROM applications
            WHERE tool_name = ? AND enabled = 1 AND approved = 1
        """, (tool_name,)).fetchone()
        return dict(row) if row else None


def approved_applications(path: Path = DATABASE) -> list[dict]:
    """Return launchable applications; the registry is the authority."""
    with closing(sqlite3.connect(path)) as database:
        database.row_factory = sqlite3.Row
        rows = database.execute("""
            SELECT app_id, display_name, tool_name, executable_path, requires_admin,
                   approval_source
            FROM applications WHERE enabled = 1 AND approved = 1
            ORDER BY app_id
        """).fetchall()
        return [dict(row) for row in rows]


def visible_applications(path: Path = DATABASE) -> list[dict]:
    """List user-facing launch options; hidden apps remain explicitly callable."""
    with closing(sqlite3.connect(path)) as database:
        database.row_factory = sqlite3.Row
        rows = database.execute("""
            SELECT app_id, display_name, tool_name, executable_path, requires_admin,
                   approval_source
            FROM applications
            WHERE enabled = 1 AND approved = 1 AND visible = 1
            ORDER BY app_id
        """).fetchall()
        return [dict(row) for row in rows]


def propose_alias(app_id: str, alias: str, source: str,
                  path: Path = DATABASE) -> dict:
    key = normalized(alias)
    if not key or len(alias) > 100 or not source or len(source) > 100:
        raise ValueError("alias proposal is invalid")
    with closing(sqlite3.connect(path)) as database:
        database.row_factory = sqlite3.Row
        with database:
            app = database.execute(
                "SELECT app_id FROM applications WHERE app_id = ? AND enabled = 1",
                (app_id,)).fetchone()
            if not app:
                raise ValueError("application is not registered")
            conflicts = [row[0] for row in database.execute(
                "SELECT app_id FROM aliases WHERE alias_key = ? AND approved = 1 "
                "AND app_id != ? ORDER BY app_id", (key, app_id))]
            database.execute("""
                INSERT INTO alias_review(app_id, alias, alias_key, source, conflict_app_ids)
                VALUES (?, ?, ?, ?, ?)
            """, (app_id, alias, key, source, ",".join(conflicts)))
            review_id = database.execute("SELECT last_insert_rowid()").fetchone()[0]
    return {"status": "pending_conflict_review" if conflicts else "pending_review",
            "review_id": review_id, "conflict_app_ids": conflicts}


def review_alias(review_id: int, approve: bool, path: Path = DATABASE) -> dict:
    with closing(sqlite3.connect(path)) as database:
        database.row_factory = sqlite3.Row
        with database:
            proposal = database.execute(
                "SELECT * FROM alias_review WHERE review_id = ? AND state = 'pending'",
                (review_id,)).fetchone()
            if not proposal:
                raise ValueError("pending alias review was not found")
            state = "approved" if approve else "rejected"
            database.execute("""
                UPDATE alias_review SET state = ?, reviewed_at = CURRENT_TIMESTAMP
                WHERE review_id = ?
            """, (state, review_id))
            if approve:
                database.execute("""
                    INSERT OR IGNORE INTO aliases(app_id, alias, alias_key, source, approved)
                    SELECT app_id, alias, alias_key, source, 1 FROM alias_review
                    WHERE review_id = ?
                """, (review_id,))
                database.execute("DELETE FROM aliases_fts")
                database.execute("""
                    INSERT INTO aliases_fts(alias_key, alias, app_id)
                    SELECT l.alias_key, l.alias, l.app_id FROM aliases AS l
                    JOIN applications AS a ON a.app_id = l.app_id
                    WHERE l.approved = 1 AND a.approved = 1 AND a.enabled = 1
                      AND a.visible = 1
                """)
    return {"status": state, "review_id": review_id,
            "conflict_app_ids": proposal["conflict_app_ids"].split(",")
            if proposal["conflict_app_ids"] else []}


def pending_alias_reviews(path: Path = DATABASE) -> list[dict]:
    with closing(sqlite3.connect(path)) as database:
        database.row_factory = sqlite3.Row
        rows = database.execute("""
            SELECT review_id, app_id, alias, source, conflict_app_ids, created_at
            FROM alias_review WHERE state = 'pending' ORDER BY review_id
        """).fetchall()
        return [{**dict(row),
                 "conflict_app_ids": row["conflict_app_ids"].split(",")
                 if row["conflict_app_ids"] else []}
                for row in rows]


def resolve(command: str, path: Path = DATABASE) -> dict:
    if not isinstance(command, str) or not 1 <= len(command.strip()) <= 500:
        raise ValueError("command must contain 1 to 500 characters")
    verb = re.search(r"打开|启动|运行", command)
    intent_prefix = normalized(command[:verb.end()]) if verb else normalized(command)
    key = normalized(primary_launch_clause(command))
    if any(phrase in intent_prefix for phrase in ("不要打开", "别打开", "不准打开", "禁止打开",
                                         "不要启动", "别启动", "不用打开", "不需要打开",
                                         "取消打开", "不要运行", "别运行")):
        return {"status": "blocked_negation"}
    if not any(verb in key for verb in ("打开", "启动", "运行")):
        return {"status": "unsupported_action"}
    with closing(sqlite3.connect(path)) as database:
        database.row_factory = sqlite3.Row
        # ponytail: linear alias matching suits this small registered set; add
        # Chinese FTS5 trigram and then CPU embeddings when the registry grows.
        rows = database.execute("""
            SELECT a.app_id, a.display_name, a.tool_name, l.alias, l.alias_key
            FROM aliases AS l JOIN applications AS a ON a.app_id = l.app_id
            WHERE a.enabled = 1 AND a.approved = 1 AND l.approved = 1
        """).fetchall()
    spans = [(row, match.start(), match.end())
             for row in rows if row["alias_key"]
             for match in re.finditer(re.escape(row["alias_key"]), key)]
    # A short alias inside a longer matched name must not hijack that name.
    # Separate names joined by "和" remain separate matches and stay ambiguous.
    matches = [row for row, start, end in spans
               if not any(other_start <= start and end <= other_end and
                          other_end - other_start > end - start
                          for _other, other_start, other_end in spans)]
    by_app: dict[str, list[sqlite3.Row]] = {}
    for row in matches:
        by_app.setdefault(row["app_id"], []).append(row)
    if not by_app:
        return {"status": "no_match"}
    if len(by_app) > 1:
        return {"status": "ambiguous", "candidates": sorted(by_app)}
    app_id, app_matches = next(iter(by_app.items()))
    first = app_matches[0]
    return {"status": "confirmed_alias", "app_id": app_id,
            "display_name": first["display_name"], "tool_name": first["tool_name"],
            "matched_aliases": sorted({row["alias"] for row in app_matches})}


def lexical_candidates(command: str, top_k: int = 3,
                       path: Path = DATABASE) -> dict:
    if not 1 <= top_k <= 10:
        raise ValueError("top_k must be between 1 and 10")
    key = normalized(requested_application_name(command))
    for phrase in ("请帮我", "麻烦帮我", "请", "麻烦", "帮我", "打开", "启动", "运行", "一下"):
        key = key.replace(normalized(phrase), "")
    grams = sorted({key[index:index + 3] for index in range(max(0, len(key) - 2))})
    if not grams:
        return {"status": "no_lexical_candidate", "candidates": []}
    match = " OR ".join('"' + gram.replace('"', '""') + '"' for gram in grams)
    with closing(sqlite3.connect(path)) as database:
        database.row_factory = sqlite3.Row
        try:
            rows = database.execute("""
            SELECT a.app_id, a.display_name, a.tool_name,
                       l.alias, bm25(aliases_fts) AS rank
                FROM aliases_fts JOIN aliases AS l ON l.alias_key = aliases_fts.alias_key
                JOIN applications AS a ON a.app_id = l.app_id
                WHERE aliases_fts MATCH ? AND l.approved = 1
                  AND a.approved = 1 AND a.enabled = 1 AND a.visible = 1
                ORDER BY rank LIMIT 100
            """, (match,)).fetchall()
        except sqlite3.OperationalError as error:
            raise RuntimeError("SQLite FTS5 lexical search failed") from error
    candidates = []
    seen = set()
    for row in rows:
        if row["app_id"] in seen:
            continue
        seen.add(row["app_id"])
        candidates.append({"app_id": row["app_id"],
                           "display_name": row["display_name"],
                           "tool_name": row["tool_name"],
                           "matched_alias": row["alias"],
                           "rank": row["rank"],
                           "source": "sqlite_fts5_trigram"})
        if len(candidates) >= top_k:
            break
    return {"status": "candidate" if candidates else "no_lexical_candidate",
            "candidates": candidates, "executes": False}


def ranked_candidates(command: str, top_k: int = 5,
                      path: Path = DATABASE) -> dict:
    """Rank approved local names for review, never for automatic execution."""
    if not 1 <= top_k <= 5:
        raise ValueError("top_k must be between 1 and 5")
    requested = requested_application_name(command)
    key = normalized(requested)
    if len(key) < 2:
        return {"status": "no_candidate", "requested_name": requested,
                "candidates": [], "executes": False}
    with closing(sqlite3.connect(path)) as database:
        database.row_factory = sqlite3.Row
        rows = database.execute("""
            SELECT a.app_id, a.display_name, a.tool_name, l.alias, l.alias_key
            FROM aliases AS l JOIN applications AS a ON a.app_id = l.app_id
            WHERE a.enabled = 1 AND a.approved = 1 AND a.visible = 1
              AND l.approved = 1
        """).fetchall()
    by_app: dict[str, dict] = {}
    for row in rows:
        alias_key = row["alias_key"]
        similarity = SequenceMatcher(None, key, alias_key).ratio()
        current = by_app.get(row["app_id"])
        if current is None or similarity > current["score"]:
            by_app[row["app_id"]] = {
                "app_id": row["app_id"], "display_name": row["display_name"],
                "tool_name": row["tool_name"], "matched_alias": row["alias"],
                "score": round(similarity, 4), "source": "local_alias",
                "installed_entry": True,
            }
    candidates = sorted(by_app.values(),
                        key=lambda item: (-item["score"], item["display_name"]))[:top_k]
    return {"status": "candidate" if candidates else "no_candidate",
            "requested_name": requested, "candidates": candidates,
            "executes": False}


def resolve_exact_name(name: str, path: Path = DATABASE) -> dict:
    """Resolve a manually entered name only when it exactly names one local app."""
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ValueError("application name must contain 1 to 100 characters")
    key = normalized(name)
    with closing(sqlite3.connect(path)) as database:
        database.row_factory = sqlite3.Row
        rows = database.execute("""
            SELECT a.app_id, a.display_name, a.tool_name, l.alias
            FROM aliases AS l JOIN applications AS a ON a.app_id = l.app_id
            WHERE l.alias_key = ? AND l.approved = 1
              AND a.enabled = 1 AND a.approved = 1
        """, (key,)).fetchall()
    apps = {row["app_id"]: row for row in rows}
    if not apps:
        return {"status": "no_match"}
    if len(apps) != 1:
        return {"status": "ambiguous", "candidates": sorted(apps)}
    row = next(iter(apps.values()))
    return {"status": "confirmed_alias", "app_id": row["app_id"],
            "display_name": row["display_name"], "tool_name": row["tool_name"],
            "matched_aliases": [row["alias"]]}


if __name__ == "__main__":
    initialize()
    print(DATABASE)
