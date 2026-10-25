"""Merge ranked evidence without comparing unrelated score scales."""
from __future__ import annotations

import math
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from application_registry import (get_application, lexical_candidates, normalized, ranked_candidates,
                                  requested_application_name, visible_applications)
from step_runtime import Deadline, call_with_deadline
from application_lookup import search_name


def _near_name(wanted, alias):
    left, right = normalized(wanted), normalized(alias)
    if not left or not right:
        return None
    if left == right:
        return {"kind": "registered_name", "matched_alias": alias}
    if min(len(left), len(right)) < 3 or re.findall(r"\d+", left) != re.findall(r"\d+", right):
        return None
    # ponytail: at most two spelling edits qualify a literal suggestion; broad
    # paraphrases require the existing independently checked name-evidence path.
    budget = min(2, (min(len(left), len(right)) + 3) // 4)
    if abs(len(left) - len(right)) > budget:
        return None
    row = list(range(len(right) + 1))
    for index, char in enumerate(left, 1):
        current = [index]
        for offset, other in enumerate(right, 1):
            current.append(min(current[-1] + 1, row[offset] + 1, row[offset - 1] + (char != other)))
        row = current
    if row[-1] <= budget:
        return {"kind": "bounded_name_edit", "matched_alias": alias, "edit_distance": row[-1]}
    return None


def qualify_candidates(target, candidates, registry_db):
    """Ranking orders recall; it cannot establish that two app names match."""
    if not candidates:
        return []
    names = {}
    if any("web_name_evidence" not in item.get("sources", []) for item in candidates):
        with closing(sqlite3.connect(Path(registry_db).resolve().as_uri() + "?mode=ro", uri=True)) as database:
            for app_id, name in database.execute("""SELECT a.app_id, l.alias FROM applications a
                    JOIN aliases l ON l.app_id=a.app_id WHERE a.enabled=1 AND a.approved=1
                    AND a.visible=1 AND l.approved=1 UNION SELECT app_id,display_name FROM applications
                    WHERE enabled=1 AND approved=1 AND visible=1"""):
                names.setdefault(app_id, []).append(name)
    qualified = []
    catalog_matches = None
    for item in candidates:
        if item["app_id"].startswith("tuba-entry-"):
            if catalog_matches is None:
                catalog_matches = {entry["app_id"]: entry for entry in search_name(target, registry_db)["candidates"]}
            current = catalog_matches.get(item["app_id"])
            if current is not None:
                qualified.append(current)
            continue
        aliases = names.get(item["app_id"], [])
        basis = next((match for alias in aliases if (match := _near_name(target, alias))), None)
        if basis is None and "web_name_evidence" in item.get("sources", []):
            # This source is added only after the existing local proposal plus
            # program-controlled name-evidence lookup succeeds.
            basis = {"kind": "checked_name_evidence"}
        if basis is not None:
            qualified.append({**item, "relevance_checked": True, "relevance_basis": basis})
    return qualified


def merge_candidates(applications, sources, limit=5):
    approved = {app["app_id"]: app for app in applications}
    combined = {}
    for source, rows in sources:
        seen = set()
        for rank, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                continue
            app_id = row.get("app_id")
            if app_id not in approved or app_id in seen:
                continue
            score = row.get("score")
            if score is not None and (not isinstance(score, (int, float)) or not math.isfinite(score)):
                continue
            if source == "local_alias" and (score is None or score <= 0):
                continue
            seen.add(app_id)
            app = approved[app_id]
            current = combined.setdefault(app_id, {
                "app_id": app_id, "display_name": app["display_name"], "tool_name": app["tool_name"],
                "sources": [], "evidence": [], "fusion_rank_score": 0.0,
                "local_approved": True, "executes": False})
            evidence = {"source": source, "source_rank": rank}
            if score is not None:
                evidence["raw_score"] = score
            if "rank" in row:
                evidence["raw_fts_rank"] = row["rank"]
            if "matched_alias" in row:
                evidence["matched_alias"] = row["matched_alias"]
            current["sources"].append(source)
            current["evidence"].append(evidence)
            # ponytail: reciprocal ranks merge the existing small recall lists;
            # calibrate retrieval quality on corrected utterances before weighting sources.
            current["fusion_rank_score"] += 1 / (60 + rank)
            if source == "local_alias":
                current.update(name_similarity=score, score=score,
                               matched_alias=row.get("matched_alias"), approved_alias=True)
    result = sorted(combined.values(), key=lambda item: (
        "exact_alias" not in item["sources"], -item["fusion_rank_score"], item["display_name"]))
    return result[:limit]


def review_candidates(text, *, resolution, registry_db, task_id, journal_db,
                      semantic_search, web_lookup, model_ready, free_vram, cancelled):
    deadline = Deadline("application_name_resolution")
    if "name_lookup" in resolution:
        return {**resolution["name_lookup"], "name_lookup": resolution["name_lookup"]}
    target = requested_application_name(text)
    visible = visible_applications(registry_db)
    if resolution["status"] == "ambiguous":
        # Explicit multi-target ambiguity uses the same manual panel as fuzzy names.
        rows = [{"app_id": app_id} for app_id in resolution["candidates"]]
        candidates = [{**item, "relevance_checked": True, "relevance_basis": {"kind": "explicit_multi_target"}}
                      for item in merge_candidates(visible, [("exact_alias", rows)])]
        return {"requested_name": target, "candidates": candidates,
                "ambiguity": "multiple_applications", "web_lookup_status": "not_needed",
                "executes": False}
    name_command = "打开" + target
    ranked = ranked_candidates(name_command, path=registry_db)
    lexical = lexical_candidates(name_command, path=registry_db)
    sources = [("local_alias", ranked["candidates"]),
               ("sqlite_fts5_trigram", lexical["candidates"])]
    plausible = qualify_candidates(target, ranked["candidates"], registry_db)
    scores = [item["score"] for item in plausible]
    strong_local = bool(scores) and scores[0] >= 0.85 and (
        len(scores) < 2 or scores[0] - scores[1] >= 0.1)
    if not strong_local:
        deadline.remaining()
        semantic = call_with_deadline(semantic_search, name_command, task_id, journal_db,
                                      deadline=deadline)
        deadline.remaining()
        if semantic.get("status") in {"semantic_timeout", "deadline_exceeded"}:
            raise TimeoutError("name retrieval timed out; route abandoned")
        if semantic.get("status") == "cancelled" or cancelled():
            return {"cancelled": True}
        sources.append(("bge_faiss_cpu", semantic.get("candidates", [])))
    # This is a literal-name routing heuristic, never a cross-model probability.
    local_evidence = bool(plausible)
    web_status = "not_needed" if local_evidence else "model_unavailable"
    if not local_evidence and visible and target and call_with_deadline(model_ready, deadline=deadline):
        deadline.remaining()
        try:
            # ponytail: bound local-model context with the existing recall
            # lists. Broader recall should reuse this index, not a second RAG DB.
            recalled = merge_candidates(visible, sources, limit=16)
            ids = {item["app_id"] for item in recalled or ranked["candidates"]}
            approved_shortlist = [app for app in visible if app["app_id"] in ids][:16]
            evidence = call_with_deadline(web_lookup, name_command, approved_shortlist,
                                          cancelled=cancelled, deadline=deadline)
        except TimeoutError:
            raise
        except (OSError, RuntimeError, ValueError):
            evidence = {"status": "search_unavailable"}
        deadline.remaining()
        web_status = evidence.get("status", "search_unavailable")
        if web_status in {"timeout", "deadline_exceeded"}:
            raise TimeoutError("name evidence search timed out; route abandoned")
        if web_status == "resolved" and get_application(evidence.get("app_id"), registry_db):
            sources.append(("web_name_evidence", [{"app_id": evidence["app_id"]}]))
    candidates = qualify_candidates(target, merge_candidates(visible, sources, limit=16), registry_db)[:5]
    return {"requested_name": target, "candidates": candidates,
            "web_lookup_status": web_status, "executes": False,
            "cancelled": cancelled(), "ranking": "reciprocal_source_ranks_not_probability",
            "relevance_policy": "registered_names_or_checked_name_evidence"}
