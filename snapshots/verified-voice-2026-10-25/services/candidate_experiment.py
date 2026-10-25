"""Candidate countdown with ranked selection and the older literal experiment."""
from __future__ import annotations

import math
import time
import tomllib
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "config" / "voice-runtime.toml"


def load_settings(path=CONFIG):
    defaults = {"enabled": False, "countdown_seconds": 60,
                "minimum_similarity": None, "minimum_margin": None,
                "mode": "preview", "thresholds_calibrated": False}
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream).get("candidate_auto", {})
    except (OSError, ValueError):
        return defaults
    if not isinstance(data, dict):
        return defaults
    if type(data.get("enabled")) is bool:
        defaults["enabled"] = data["enabled"]
    if data.get("mode") in ("preview", "execute", "ranked"):
        defaults["mode"] = data["mode"]
    if type(data.get("thresholds_calibrated")) is bool:
        defaults["thresholds_calibrated"] = data["thresholds_calibrated"]
    seconds = data.get("countdown_seconds")
    if type(seconds) is int and 1 <= seconds <= 180:
        defaults["countdown_seconds"] = seconds
    for key in ("minimum_similarity", "minimum_margin"):
        value = data.get(key)
        if type(value) in (float, int) and math.isfinite(value) and 0 < value <= 1:
            defaults[key] = value
    return defaults


def nomination(items, settings, *, task_state="needs_confirmation"):
    if not settings["enabled"] or task_state != "needs_confirmation":
        return {"eligible": False, "reason": "disabled_or_invalid_state"}
    if any(item.get("auto_select_allowed") is False for item in items if isinstance(item, dict)):
        return {"eligible": False, "reason": "ambiguous_name_or_weak_correction"}
    if settings.get("mode") == "ranked":
        # Preserve the resolver's combined ranking, including semantic evidence;
        # do not sort it again by a different source's literal similarity.
        top = items[0] if isinstance(items, list) and items else None
        if (not isinstance(top, dict) or top.get("local_approved") is not True
                or top.get("relevance_checked") is not True or not top.get("relevance_basis")
                or not isinstance(top.get("app_id"), str) or not top["app_id"]
                or not isinstance(top.get("display_name"), str) or not top["display_name"].strip()):
            return {"eligible": False, "reason": "no_approved_ranked_candidate"}
        scores = [top.get(key) for key in ("fusion_rank_score", "name_similarity", "score")]
        if not any(type(value) in (int, float) and math.isfinite(value) and value > 0 for value in scores):
            return {"eligible": False, "reason": "no_positive_retrieval_evidence"}
        return {"eligible": True, "app_id": top["app_id"], "display_name": top["display_name"],
                "reason": "first_displayed_ranked_candidate", "preview_only": False, "dispatch": False}
    aliases = [item for item in items if item.get("local_approved") is True
               and item.get("approved_alias") is True
               and type(item.get("name_similarity")) in (int, float)
               and math.isfinite(item["name_similarity"]) and 0 <= item["name_similarity"] <= 1]
    aliases.sort(key=lambda item: -item["name_similarity"])
    if not aliases:
        return {"eligible": False, "reason": "no_approved_alias_evidence"}
    top = aliases[0]
    margin = top["name_similarity"] - (aliases[1]["name_similarity"] if len(aliases) > 1 else 0)
    result = {"eligible": False, "app_id": top["app_id"], "display_name": top["display_name"],
              "name_similarity": top["name_similarity"], "margin": margin,
              "preview_only": settings.get("mode") != "execute" or settings.get("thresholds_calibrated") is not True,
              "dispatch": False}
    if margin <= 0:
        return {**result, "reason": "tied_names"}
    minimum, gap = settings["minimum_similarity"], settings["minimum_margin"]
    if minimum is None or gap is None:
        return {**result, "reason": "thresholds_uncalibrated"}
    eligible = top["name_similarity"] >= minimum and margin >= gap
    return {**result, "eligible": eligible,
            "reason": "literal_thresholds_met" if eligible else "insufficient_literal_evidence"}


def validate_experimental_choice(items, selected_app_id, settings=None):
    settings = load_settings() if settings is None else settings
    if settings.get("mode") != "execute" or settings.get("thresholds_calibrated") is not True:
        raise ValueError("candidate execution experiment is not enabled and calibrated")
    # The request comes from the session opt-in switch. Backend configuration
    # separately gates execution and recomputes the literal evidence.
    decision = nomination(items, {**settings, "enabled": True})
    if not decision.get("eligible") or decision.get("app_id") != selected_app_id:
        raise ValueError("candidate does not meet the local literal evidence gates")
    return decision


def validate_countdown_choice(items, selected_app_id, settings=None):
    settings = load_settings() if settings is None else settings
    if settings.get("enabled") is not True or settings.get("mode") != "ranked":
        raise ValueError("ranked candidate countdown is disabled")
    decision = nomination(items, settings)
    if not decision.get("eligible") or decision.get("app_id") != selected_app_id:
        raise ValueError("automatic selection must match the first approved ranked candidate")
    return decision


class CandidatePreview:
    def __init__(self, settings=None, *, clock=time.monotonic):
        self.settings = load_settings() if settings is None else settings.copy()
        self.clock, self.expires, self.decision = clock, None, None

    def cancel(self):
        self.expires = None

    def set_enabled(self, enabled):
        self.settings["enabled"] = bool(enabled)
        self.cancel()

    def arm(self, items):
        self.cancel()
        self.decision = nomination(items, self.settings)
        if self.settings["enabled"] and (self.settings.get("mode") != "ranked" or self.decision.get("eligible")):
            self.expires = self.clock() + self.settings["countdown_seconds"]
        return self.decision

    def poll(self):
        if self.expires is None:
            return None
        remaining = self.expires - self.clock()
        if remaining > 0:
            return {"remaining": math.ceil(remaining)}
        self.cancel()
        dispatch = (self.settings.get("enabled") is True
                    and (self.settings.get("mode") == "ranked" or
                         (self.settings.get("mode") == "execute" and self.settings.get("thresholds_calibrated") is True))
                    and self.decision.get("eligible") is True)
        return {"decision": self.decision, "dispatch": dispatch}
