"""Topic registry: one canonical Title Case name per idea, with aliases merged deterministically."""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import replace

from ..models import Claim
from ..paths import VaultPaths

_ARTICLES = {"a", "an", "the"}
_STOPWORDS = _ARTICLES | {"of", "for", "to", "in", "on", "and", "with", "your", "my", "about"}
_SMALL_TITLE_WORDS = {"and", "of", "for", "to", "in", "on", "the", "a", "an", "vs", "with", "or", "at", "by"}
_BAD_TITLE_CHARS = re.compile(r'[\[\]#^|\\/:?*<>"{}]')
MAX_TOPIC_CHARS = 60
DEFAULT_TOPIC = "General"

# canonical title -> phrases that mean the same thing (fitness / nutrition basics). Edit freely.
SYNONYM_GROUPS: dict[str, list[str]] = {
    "Protein Intake": ["Daily Protein", "Protein Requirements", "Protein Needs", "Protein Target", "Protein Consumption",
                       "How Much Protein", "Protein Amount", "Protein Per Day", "Dietary Protein"],
    "Muscle Growth": ["Hypertrophy", "Muscle Building", "Building Muscle", "Muscle Gain", "Muscle Hypertrophy",
                      "Gaining Muscle"],
    "Fat Loss": ["Weight Loss", "Losing Fat", "Losing Weight", "Fat Burning", "Body Fat Loss"],
    "Calorie Deficit": ["Caloric Deficit", "Energy Deficit", "Cutting Calories"],
    "Calorie Surplus": ["Caloric Surplus", "Energy Surplus", "Bulking"],
    "Calorie Intake": ["Caloric Intake", "Daily Calories", "Calorie Target", "Calorie Needs", "Calorie Requirements"],
    "Carbohydrate Intake": ["Carb Intake", "Carbs", "Carbohydrates", "Daily Carbs", "Carb Consumption"],
    "Fat Intake": ["Dietary Fat", "Dietary Fats", "Daily Fat"],
    "Training Volume": ["Weekly Volume", "Workout Volume", "Sets Per Week", "Weekly Sets", "Volume Per Muscle"],
    "Training Frequency": ["Workout Frequency", "Weekly Frequency", "Frequency Per Muscle", "Training Split Frequency"],
    "Training to Failure": ["Proximity to Failure", "Muscle Failure", "Training Failure", "Going to Failure"],
    "Progressive Overload": ["Progressive Resistance", "Overload Principle"],
    "Rep Range": ["Repetition Range", "Rep Ranges", "Reps Per Set"],
    "Rest Periods": ["Rest Time", "Rest Between Sets", "Rest Intervals", "Rest Duration"],
    "Warm Up": ["Warmup", "Warming Up", "Warm Up Sets", "Warm Up Routine"],
    "Deload": ["Deload Week", "Deloading", "Deload Weeks"],
    "Creatine": ["Creatine Supplementation", "Creatine Monohydrate", "Creatine Supplement", "Taking Creatine"],
    "Sleep": ["Sleep Quality", "Sleep Duration", "Better Sleep", "Sleep Optimization", "Sleep Hygiene"],
    "Hydration": ["Water Intake", "Daily Water", "Fluid Intake", "Drinking Water"],
    "Meal Timing": ["Nutrient Timing", "Protein Timing", "Meal Frequency"],
    "Intermittent Fasting": ["Fasting", "Time Restricted Eating"],
    "Cardio": ["Cardiovascular Exercise", "Aerobic Exercise", "Aerobic Training", "Cardio Training"],
    "Exercise Form": ["Lifting Form", "Proper Form", "Exercise Technique", "Lifting Technique", "Form And Technique"],
    "Injury Prevention": ["Preventing Injuries", "Avoiding Injury", "Injury Risk"],
    "Supplements": ["Dietary Supplements", "Supplement Stack", "Supplementation"],
}


def _stem(word: str) -> str:
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "shes", "ches", "xes", "zes")):
        return word[:-2]
    if word.endswith(("ss", "us", "is")):
        return word
    if word.endswith("s"):
        return word[:-1]
    if word.endswith("ie") and len(word) > 4:
        return word[:-2] + "y"
    return word


def topic_key(topic: str) -> str:
    """Order-insensitive comparison key: casefold, no punctuation, no articles/filler, singular words."""
    words = re.sub(r"[^\w\s]|_", " ", topic.casefold().replace("&", " and ").replace("'", "")).split()
    stems = sorted(_stem(w) for w in words if w not in _STOPWORDS)
    return " ".join(stems) or " ".join(sorted(words))


def title_case(text: str) -> str:
    out = []
    for i, word in enumerate(re.sub(r"\s+", " ", text.strip()).split(" ")):
        if len(word) > 1 and word.isupper():
            out.append(word)  # acronyms: HIIT, RPE
        elif i > 0 and word.lower() in _SMALL_TITLE_WORDS:
            out.append(word.lower())
        else:
            out.append("-".join(p[:1].upper() + p[1:].lower() for p in word.split("-")))
    return " ".join(out)


def clean_topic(topic: str | None) -> str:
    t = _BAD_TITLE_CHARS.sub(" ", str(topic or ""))
    t = re.sub(r"\s+", " ", t).strip(" .,-")
    return title_case(t)[:MAX_TOPIC_CHARS].strip() or DEFAULT_TOPIC


_SYNONYM_LOOKUP: dict[str, str] = {}
for _canon, _phrases in SYNONYM_GROUPS.items():
    for _p in [_canon, *_phrases]:
        _SYNONYM_LOOKUP.setdefault(topic_key(_p), _canon)


class TopicRegistry:
    """Persisted in paths.topics_json as {canonical: {"aliases": [...], "claims": [ids]}} (atomic writes)."""

    def __init__(self, paths: VaultPaths):
        self.paths = paths
        self._lock = threading.RLock()
        self._topics: dict[str, dict] = {}
        self._key_to_canon: dict[str, str] = {}
        self._load()

    # ---- persistence ----------------------------------------------------
    def _load(self) -> None:
        p = self.paths.topics_json
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for canon, entry in data.items():
            self._topics[canon] = {"aliases": list(entry.get("aliases", [])), "claims": list(entry.get("claims", []))}
        for canon, entry in self._topics.items():
            self._key_to_canon.setdefault(topic_key(canon), canon)
            for a in entry["aliases"]:
                self._key_to_canon.setdefault(topic_key(a), canon)

    def save(self) -> None:
        with self._lock:
            p = self.paths.topics_json
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(self._topics, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            os.replace(tmp, p)

    # ---- API ------------------------------------------------------------
    def canonical(self, topic: str) -> str:
        """Canonical name for `topic` (alias, plural, word-order and built-in synonym merges). New topics register."""
        raw = clean_topic(topic)
        key = topic_key(raw)
        with self._lock:
            canon = self._key_to_canon.get(key)
            if canon is None:
                syn = _SYNONYM_LOOKUP.get(key)
                canon = (self._key_to_canon.get(topic_key(syn)) or self._register(syn)) if syn else self._register(raw)
                self._key_to_canon[key] = canon
            if raw != canon and raw not in self._topics[canon]["aliases"]:
                self._topics[canon]["aliases"].append(raw)
                self._topics[canon]["aliases"].sort()
            return canon

    def _register(self, canonical: str) -> str:
        self._topics.setdefault(canonical, {"aliases": [], "claims": []})
        self._key_to_canon.setdefault(topic_key(canonical), canonical)
        return canonical

    def add_claims(self, claims: list[Claim]) -> dict[str, list[Claim]]:
        """Re-label claims with their canonical topic, record their ids, persist. Returns canonical -> claims."""
        grouped: dict[str, list[Claim]] = {}
        with self._lock:
            for c in claims:
                canon = self.canonical(c.topic)
                grouped.setdefault(canon, []).append(c if c.topic == canon else replace(c, topic=canon))
                ids = self._topics[canon]["claims"]
                if c.id not in ids:
                    ids.append(c.id)
            self.save()
        return grouped

    def merge(self, groups: dict[str, list[str]]) -> dict[str, str]:
        """Fold member topics into a canonical one (members become aliases). Returns old name -> canonical."""
        moved: dict[str, str] = {}
        with self._lock:
            for canon, members in groups.items():
                canon = clean_topic(canon)
                target = self._topics.setdefault(canon, {"aliases": [], "claims": []})
                for m in members:
                    if m == canon or m not in self._topics:
                        continue
                    entry = self._topics.pop(m)
                    target["aliases"] = sorted(set(target["aliases"]) | set(entry["aliases"]) | {m})
                    target["claims"] += [i for i in entry["claims"] if i not in target["claims"]]
                    moved[m] = canon
                    for a in entry["aliases"]:
                        moved[a] = canon
                for name in [canon, *target["aliases"]]:
                    self._key_to_canon[topic_key(name)] = canon
            self.save()
        return moved

    def all_topics(self) -> list[str]:
        with self._lock:
            return sorted(self._topics)

    def aliases(self, topic: str) -> list[str]:
        with self._lock:
            canon = self._key_to_canon.get(topic_key(clean_topic(topic)))
            return list(self._topics[canon]["aliases"]) if canon else []

    def claim_ids(self, topic: str) -> list[str]:
        with self._lock:
            return list(self._topics.get(topic, {}).get("claims", []))
