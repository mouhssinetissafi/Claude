"""Phase 5: footage inventory for the script writer.

Summarizes usable scenes, strongest scenes, recurring subjects, brands, total
usable duration, rejections and near-duplicate groups. Near-duplicates are
found with token overlap on the vision descriptions (cheap, local).
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from autoeditor.config import Config
from autoeditor.footage_only.scenes import Scene
from autoeditor.pipeline.job import JobPaths, write_json

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {"a", "an", "the", "of", "in", "on", "with", "and", "to", "at", "is", "for", "from", "by", "or", "as"}


def _tokens(analysis: dict[str, Any]) -> set[str]:
    words: list[str] = []
    for key in ("subjects", "objects", "product_or_brand", "colors"):
        for item in analysis.get(key, []) or []:
            words.extend(_TOKEN.findall(str(item).lower()))
    for key in ("environment", "shot_type", "camera_motion", "mood"):
        words.extend(_TOKEN.findall(str(analysis.get(key, "")).lower()))
    return {w for w in words if w not in _STOP and len(w) > 2}


def similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def combined_score(analysis: dict[str, Any]) -> float:
    return round(0.6 * float(analysis.get("visual_interest_score", 0)) + 0.4 * float(analysis.get("quality_score", 0)), 2)


def build_inventory(scenes: list[Scene], cfg: Config, *, similarity_threshold: float = 0.6) -> dict[str, Any]:
    usable = [s for s in scenes if s.usable and s.analysis]
    rejected = [
        {"scene_id": s.scene_id, "source_file": s.source_file, "reason": s.rejected_reason or "no analysis"} for s in scenes if not (s.usable and s.analysis)
    ]

    similar_map: dict[int, list[int]] = {s.scene_id: [] for s in usable}
    for i, a in enumerate(usable):
        for b in usable[i + 1 :]:
            assert a.analysis is not None and b.analysis is not None
            same_source_adjacent = a.source_file == b.source_file and abs(a.end_time - b.start_time) < 0.01
            if similarity(a.analysis, b.analysis) >= similarity_threshold or same_source_adjacent:
                similar_map[a.scene_id].append(b.scene_id)
                similar_map[b.scene_id].append(a.scene_id)

    # Union-find style grouping of near-duplicates so one look cannot dominate.
    group_of: dict[int, int] = {}
    groups: list[list[int]] = []
    for s in usable:
        if s.scene_id in group_of:
            continue
        stack = [s.scene_id]
        group: list[int] = []
        while stack:
            sid = stack.pop()
            if sid in group_of:
                continue
            group_of[sid] = len(groups)
            group.append(sid)
            stack.extend(similar_map.get(sid, []))
        groups.append(sorted(group))

    scene_rows: list[dict[str, Any]] = []
    subjects: Counter[str] = Counter()
    brands: Counter[str] = Counter()
    topics: Counter[str] = Counter()
    for s in usable:
        an: dict[str, Any] = s.analysis or {}
        subjects.update(str(x).lower() for x in an.get("subjects", []))
        brands.update(str(x) for x in an.get("product_or_brand", []))
        topics.update(str(x).lower() for x in an.get("possible_topics", []))
        scene_rows.append(
            {
                "scene_id": s.scene_id,
                "source_file": s.source_file,
                "start_time": s.start_time,
                "end_time": s.end_time,
                "duration": s.duration,
                "description": an.get("description", ""),
                "subjects": an.get("subjects", []),
                "objects": an.get("objects", []),
                "environment": an.get("environment", ""),
                "shot_type": an.get("shot_type", ""),
                "camera_motion": an.get("camera_motion", ""),
                "subject_motion": an.get("subject_motion", ""),
                "mood": an.get("mood", ""),
                "text_content": an.get("text_content", ""),
                "product_or_brand": an.get("product_or_brand", []),
                "quality_score": an.get("quality_score", 0),
                "visual_interest_score": an.get("visual_interest_score", 0),
                "score": combined_score(an),
                "similar_to": sorted(similar_map.get(s.scene_id, [])),
                "duplicate_group": group_of.get(s.scene_id),
            }
        )

    # Strongest scenes: best score per duplicate group, then by score, diversified by source.
    best_per_group: dict[int, dict[str, Any]] = {}
    for row in scene_rows:
        g = row["duplicate_group"]
        if g not in best_per_group or row["score"] > best_per_group[g]["score"]:
            best_per_group[g] = row
    ranked = sorted(best_per_group.values(), key=lambda r: (-r["score"], r["scene_id"]))
    strongest: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for row in ranked:
        if row["source_file"] in seen_sources and len(strongest) < 3:
            continue
        seen_sources.add(row["source_file"])
        strongest.append({"scene_id": row["scene_id"], "score": row["score"], "description": row["description"]})
        if len(strongest) >= 5:
            break
    if len(strongest) < min(5, len(ranked)):
        for row in ranked:
            if all(x["scene_id"] != row["scene_id"] for x in strongest):
                strongest.append({"scene_id": row["scene_id"], "score": row["score"], "description": row["description"]})
            if len(strongest) >= 5:
                break

    per_source: dict[str, float] = {}
    for s in usable:
        per_source[s.source_file] = round(per_source.get(s.source_file, 0.0) + s.duration, 3)
    total = round(sum(s.duration for s in usable), 3)
    return {
        "version": 1,
        "total_usable_seconds": total,
        "usable_scene_count": len(usable),
        "rejected_scene_count": len(rejected),
        "per_source_seconds": per_source,
        "usable_scenes": scene_rows,
        "strongest_scenes": strongest,
        "recurring_subjects": [{"subject": k, "count": v} for k, v in subjects.most_common(8) if v > 1],
        "products_or_brands": [{"name": k, "count": v} for k, v in brands.most_common(8)],
        "possible_topics": [k for k, _ in topics.most_common(8)],
        "duplicate_groups": [g for g in groups if len(g) > 1],
        "rejected_scenes": rejected,
        "notes": [
            "scene_ids in duplicate_groups look nearly identical; do not let one group dominate the video",
            f"target video length is {cfg.get('script.target_min_seconds')}-{cfg.get('script.target_max_seconds')} seconds",
        ],
    }


def write_inventory(inventory: dict[str, Any], paths: JobPaths) -> None:
    write_json(paths.inventory_json, inventory)
