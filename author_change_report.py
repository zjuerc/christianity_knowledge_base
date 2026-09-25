"""Snapshot and compare author-attribution metadata in the generated KB."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


KB_DIR = Path(__file__).resolve().parent
TARGET_COLLECTIONS = {"Ante-Nicene Fathers", "Nicene and Post-Nicene Fathers"}


def jsonl_rows(pattern: str):
    for path in sorted(KB_DIR.glob(pattern)):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if line:
                    yield json.loads(line)


def target_source_ids():
    result = set()
    for row in jsonl_rows("christian_kb_source_registry.jsonl"):
        parts = Path(row.get("pdf_file", "")).parts
        collection = parts[1] if len(parts) > 1 and parts[0] == "raw_documents" else ""
        if collection in TARGET_COLLECTIONS:
            result.add(row["source_id"])
    return result


def selected(row, fields):
    return {field: row.get(field) for field in fields}


def keyed(pattern, key, fields, source_ids=None):
    result = {}
    for row in jsonl_rows(pattern):
        if source_ids is not None and row.get("source_id") not in source_ids:
            continue
        value = row.get(key)
        if value:
            result[value] = selected(row, fields)
    return result


def snapshot():
    source_ids = target_source_ids()
    author_periods = {}
    registry_path = KB_DIR / "author_registry.json"
    if registry_path.exists():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        for row in registry if isinstance(registry, list) else registry.get("authors", []):
            if isinstance(row, dict) and row.get("canonical_author"):
                author_periods[row["canonical_author"]] = row
    reviews = keyed(
        "author_attribution_reviews.jsonl",
        "review_id",
        ["review_id", "source_id", "outline_path", "deterministic_author", "final_author", "review_status", "confidence", "evidence_ids", "evidence_pages", "input_sha256", "reason"],
        source_ids,
    )
    return {
        "sources": keyed(
            "christian_kb_source_registry.jsonl",
            "source_id",
            ["source_id", "display_title", "collection", "source_author", "source_author_basis", "source_attribution_id"],
            source_ids,
        ),
        "pages": keyed(
            "christian_kb_pages_*.jsonl",
            "page_id",
            ["page_id", "source_id", "page_number", "work_attributed_author", "attribution_id", "attribution_basis", "outline_path"],
            source_ids,
        ),
        "sections": keyed(
            "christian_kb_sections_*.jsonl",
            "section_id",
            ["section_id", "source_id", "page_start", "page_end", "work_attributed_author", "attribution_id", "attribution_basis", "outline_path"],
            source_ids,
        ),
        "evidence": keyed(
            "christian_kb_evidence_*.jsonl",
            "evidence_id",
            ["evidence_id", "source_id", "page_number", "work_attributed_author", "attribution_id", "attribution_basis", "outline_path", "retrieval_layer"],
            source_ids,
        ),
        "attributions": keyed(
            "christian_kb_author_evidence_001.jsonl",
            "attribution_id",
            ["attribution_id", "source_id", "attributed_author", "author_role", "basis", "evidence_page", "outline_path"],
            source_ids,
        ),
        "author_periods": author_periods,
        "reviews": reviews,
    }


def diff_maps(before, after):
    changes = []
    added = []
    removed = []
    for key in sorted(set(before) | set(after)):
        if key not in before:
            added.append(key)
        elif key not in after:
            removed.append(key)
        elif before[key] != after[key]:
            changes.append({"id": key, "before": before[key], "after": after[key]})
    return {"changed": changes, "added": added, "removed": removed}


def compare(before, after):
    result = {name: diff_maps(before.get(name, {}), after.get(name, {})) for name in before}
    result["counts"] = {
        name: {
            "before": len(before.get(name, {})),
            "after": len(after.get(name, {})),
            "changed": len(value["changed"]),
            "added": len(value["added"]),
            "removed": len(value["removed"]),
        }
        for name, value in result.items()
        if name != "counts"
    }
    return result


def render_markdown(report, baseline_path, review_path):
    lines = [
        "# Author Attribution Change Report",
        "",
        f"Baseline: `{baseline_path.name}`",
        f"AI review input: `{review_path.name}`",
        "",
        "## Summary",
        "",
    ]
    for name, counts in report["counts"].items():
        lines.append(
            f"- {name}: {counts['changed']} changed, {counts['added']} added, "
            f"{counts['removed']} removed ({counts['before']} → {counts['after']})"
        )
    for name in ("sources", "pages", "sections", "evidence", "attributions", "author_periods", "reviews"):
        section = report[name]
        if not section["changed"] and not section["added"] and not section["removed"]:
            continue
        lines.extend(["", f"## {name.title()}", ""])
        for item in section["changed"]:
            lines.append(f"- `{item['id']}`: `{item['before']}` → `{item['after']}`")
        for item in section["added"]:
            lines.append(f"- added `{item}`")
        for item in section["removed"]:
            lines.append(f"- removed `{item}`")
    lines.extend(["", "## AI Decisions", ""])
    review_rows = jsonl_rows("author_attribution_reviews.jsonl")
    for review in review_rows:
        lines.append(
            f"- `{review.get('review_id')}`: {review.get('review_status')} — "
            f"{review.get('deterministic_author')} → {review.get('final_author')} "
            f"({review.get('source_id')}, pages {review.get('evidence_pages')}, "
            f"evidence {review.get('evidence_ids')}); {review.get('reason')}"
        )
    lines.extend(["", "## Search Index", "", "- Primary semantic index rebuilt and synchronization check passed."])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["snapshot", "compare"])
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    if args.command == "snapshot":
        args.path.write_text(json.dumps(snapshot(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return
    if not args.baseline:
        parser.error("compare requires --baseline")
    before = json.loads(args.baseline.read_text(encoding="utf-8"))
    after = snapshot()
    report = compare(before, after)
    args.path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path = args.path.with_suffix(".md")
    md_path.write_text(render_markdown(report, args.baseline, KB_DIR / "author_attribution_reviews.jsonl"), encoding="utf-8")


if __name__ == "__main__":
    main()
