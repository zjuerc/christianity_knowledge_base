#!/usr/bin/env python3
"""Add independently versioned, observed pagination to an EXISTING KB.

Never invokes ingestion, evidence chunking, or term indexing. PyMuPDF is only
needed for the pagination scan; reading the resulting JSONL needs stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from collections import Counter
from itertools import groupby
from pathlib import Path

SCHEMA_VERSION = "1.0.0"
EXTRACTOR_VERSION = "1.0.0"
FILENAME = "christian_kb_page_locations.jsonl"
DISPLAY_LABELS = {
    "pdf_page_number": {
        "singular": "PDF file page", "plural": "PDF file pages",
        "description": "The 1-based page position used to navigate the PDF file, including covers and front matter.",
    },
    "pdf_edition_page_number": {
        "singular": "Header/footer page", "plural": "Header/footer pages",
        "description": "The page number printed in the header or footer of the PDF page.",
    },
    "original_book_page_numbers": {
        "singular": "Printed book reference", "plural": "Printed book references",
        "description": "The original printed book page number shown in the PDF margin; it may not pinpoint every quoted word.",
    },
}
LABEL = re.compile(r"(?:[0-9]{1,6}|[ivxlcdmIVXLCDM]{1,12})\Z")


def json_line(row):
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def read_rows(path):
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def margin_lines(page):
    """Inspect embedded text geometry; retain only visible margin observations."""
    import fitz
    height, width = page.rect.height, page.rect.width
    # No image bytes, OCR, body-text output, or changes to the PDF/evidence.
    data = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)
    found = []
    for block in data["blocks"]:
        for line in block.get("lines", []):
            x0, top, x1, bottom = line["bbox"]
            if x0 < 0 or top < 0 or x1 > width + .1 or bottom > height + .1:
                continue
            if not (top < height * .12 or bottom > height * .82 or x0 > width * .86):
                continue
            text = " ".join(span["text"].strip() for span in line["spans"]).strip()
            if not LABEL.fullmatch(text):
                continue
            found.append({"label": text, "bbox": [round(v, 2) for v in line["bbox"]],
                          "font_size": round(max(span["size"] for span in line["spans"]), 2)})
    return found


def ccel_footer(line, width, height):
    x0, top, x1, bottom = line["bbox"]
    return (abs(x1 / width - 504 / 612) < .004
            and abs(top / height - 692.5 / 792) < .006
            and 10.5 <= line["font_size"] <= 11.5)


def detect_profile(document):
    samples = sorted({min(i, len(document) - 1) for i in (1, 4, 10, 20, 40, 100, len(document) // 2)})
    hits = 0
    for index in samples:
        page = document[index]
        if any(ccel_footer(line, page.rect.width, page.rect.height) for line in margin_lines(page)):
            hits += 1
    return "ccel" if hits >= 2 else "unrecognized"


def classify_page(source_id, page_number, pdf_hash, width, height, lines, profile, has_text=True):
    footer, original, uncertain, excluded = [], [], [], []
    for line in sorted(lines, key=lambda item: (item["bbox"][1], item["bbox"][0])):
        x0, top, x1, bottom = line["bbox"]
        # The old evidence classifier's 12%/82% bands include body headings and
        # footnotes. Those numerals are not pagination candidates.
        if not (top < height * .09 or top > height * .86 or x0 > width * .86):
            continue
        observed = {**line, "method": "embedded_text_margin_coordinates"}
        if profile == "ccel" and ccel_footer(line, width, height):
            footer.append({**observed, "system": "pdf_edition", "verification_status": "verified"})
        elif (profile == "ccel" and .86 < x0 / width < .92
              and 5.5 <= line["font_size"] <= 6.5 and .085 < top / height < .87):
            original.append({**observed, "system": "original_book_marker", "verification_status": "verified"})
        elif profile == "ccel" and x1 / width < .84 and (top / height < .12 or line["font_size"] < 10):
            # Verified CCEL page-template zones: TOC entries/running headings
            # above the body and left-aligned footnote identifiers below it.
            excluded.append({**observed, "system": "not_pagination", "verification_status": "excluded",
                             "reason": "ccel_header_toc_or_footnote_number_outside_pagination_zones"})
        else:
            uncertain.append({**observed, "system": "unresolved", "verification_status": "unresolved"})
    labels = list(dict.fromkeys(item["label"] for item in footer))
    if len(labels) > 1:
        uncertain.extend({**item, "system": "pdf_edition", "verification_status": "unresolved"} for item in footer)
        footer, labels = [], []
    # Unknown layouts are intentionally not guessed from a standalone numeral.
    missing = "unavailable" if not has_text or profile != "ccel" else "not_marked"
    unresolved_footer = any(item["system"] in ("pdf_edition", "unresolved") for item in uncertain)
    return {
        "source_id": source_id, "source_format": "pdf", "pdf_page_number": page_number,
        "pdf_sha256": pdf_hash, "page_width": round(width, 2), "page_height": round(height, 2),
        "pdf_edition_page_number": labels[0] if labels else None,
        "pdf_edition_status": "verified" if labels else ("unresolved" if unresolved_footer else missing),
        "original_book_page_numbers": list(dict.fromkeys(item["label"] for item in original)),
        "original_book_status": "verified" if original else ("unresolved" if uncertain else missing),
        "observations": footer + original + uncertain + excluded,
    }


def active_sources(kb_dir, manifest):
    return [row for name in manifest["files"]["source_registry"]
            for row in read_rows(Path(kb_dir) / name) if row.get("registry_status") == "active"]


def extension_info(path, sources):
    counts = Counter()
    pages = 0
    for row in read_rows(path):
        pages += 1
        counts["pdf_edition_" + row["pdf_edition_status"]] += 1
        counts["original_book_" + row["original_book_status"]] += 1
        if any(o["verification_status"] == "unresolved" for o in row["observations"]):
            counts["pages_with_unresolved_candidates"] += 1
    return {"schema_version": SCHEMA_VERSION, "extractor_version": EXTRACTOR_VERSION,
            "display_labels": DISPLAY_LABELS,
            "sha256": sha256_file(path), "page_count": pages, "coverage": dict(sorted(counts.items())),
            "source_fingerprints": {r["source_id"]: r["pdf_sha256"] for r in sources},
            "policy": "observed_numbers_only; no_offsets; no_ocr; original_markers_are_page_level"}


def refresh_labels(kb_dir):
    """Change only label descriptions; never inspect PDFs or rewrite JSONL."""
    kb_dir = Path(kb_dir)
    path = kb_dir / "christian_kb_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    extension = manifest.get("pagination")
    if not extension:
        raise ValueError("Existing pagination extension required for a label-only refresh")
    if sha256_file(kb_dir / FILENAME) != extension.get("sha256"):
        raise ValueError("Pagination digest mismatch; no label metadata changed")
    if extension.get("display_labels") == DISPLAY_LABELS:
        print("Pagination labels already current; no KB changes.")
        return False
    backup = path.with_suffix(".json.before-labels")
    if not backup.exists():
        shutil.copy2(path, backup)
    extension["display_labels"] = DISPLAY_LABELS
    write_json(path, manifest)
    coverage_path = kb_dir / "pagination_coverage.json"
    if coverage_path.exists():
        coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
        if coverage.get("sha256") == extension["sha256"]:
            coverage["display_labels"] = DISPLAY_LABELS
            write_json(coverage_path, coverage)
    print("Updated KB label descriptions; pagination JSONL and evidence unchanged.")
    return True


def refresh(kb_dir, pdf_root=None):
    import fitz
    kb_dir = Path(kb_dir).resolve()
    pdf_root = Path(pdf_root).resolve() if pdf_root else kb_dir.parent / "raw_documents"
    manifest_path = kb_dir / "christian_kb_manifest.json"
    if not manifest_path.exists():
        raise ValueError("Existing KB manifest required. Restore the released bundle; do not rebuild.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sources = sorted(active_sources(kb_dir, manifest), key=lambda row: row["source_id"])
    cache = kb_dir / "_pagination_cache"
    cache.mkdir(exist_ok=True)
    # A released bundle is sufficient for reuse: no private source cache needed.
    published = kb_dir / FILENAME
    previous = manifest.get("pagination", {})
    if (published.exists() and previous.get("extractor_version") == EXTRACTOR_VERSION
            and previous.get("sha256") == sha256_file(published)):
        fingerprints = {s["source_id"]: s["pdf_sha256"] for s in sources}
        for source_id, group in groupby(read_rows(published), key=lambda row: row["source_id"]):
            rows_path = cache / (source_id + ".jsonl")
            meta_path = cache / (source_id + ".json")
            if rows_path.exists() and meta_path.exists():
                continue
            if fingerprints.get(source_id) != previous.get("source_fingerprints", {}).get(source_id):
                continue
            temporary = rows_path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                for row in group:
                    if row["pdf_sha256"] != fingerprints[source_id]:
                        raise ValueError("Published pagination fingerprint mismatch")
                    stream.write(json_line(row))
            os.replace(temporary, rows_path)
            write_json(meta_path, {"signature": {"pdf_sha256": fingerprints[source_id],
                                                 "extractor_version": EXTRACTOR_VERSION},
                                   "sha256": sha256_file(rows_path), "profile": "published_metadata"})
    for index, source in enumerate(sources, 1):
        source_id = source["source_id"]
        relative = Path(source["pdf_file"])
        if relative.parts[0] == "raw_documents":
            relative = Path(*relative.parts[1:])
        pdf_path = pdf_root / relative
        actual_hash = sha256_file(pdf_path)
        if actual_hash != source["pdf_sha256"]:
            raise ValueError(f"PDF fingerprint mismatch: {source_id}; no changes published")
        rows_path, meta_path = cache / (source_id + ".jsonl"), cache / (source_id + ".json")
        signature = {"pdf_sha256": actual_hash, "extractor_version": EXTRACTOR_VERSION}
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        if (meta.get("signature") == signature and rows_path.exists()
                and meta.get("sha256") == sha256_file(rows_path)):
            print(f"[{index}/{len(sources)}] reused pagination {source_id}", flush=True)
            continue
        temporary = rows_path.with_suffix(".tmp")
        with fitz.open(pdf_path) as doc, temporary.open("w", encoding="utf-8", newline="\n") as stream:
            profile = detect_profile(doc)
            for number, page in enumerate(doc, 1):
                lines = margin_lines(page)
                # A numeric-free scanned page cannot establish that no number is printed.
                has_text = bool(lines) or bool(page.get_text("words", clip=fitz.Rect(0, 0, page.rect.width, page.rect.height * .15)))
                row = classify_page(source_id, number, actual_hash, page.rect.width, page.rect.height,
                                    lines, profile, has_text=bool(lines) or has_text)
                stream.write(json_line(row))
        os.replace(temporary, rows_path)
        write_json(meta_path, {"signature": signature, "sha256": sha256_file(rows_path), "profile": profile})
        print(f"[{index}/{len(sources)}] scanned margins {source_id} ({profile})", flush=True)
    target = kb_dir / FILENAME
    temporary = target.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        for source in sources:
            with (cache / (source["source_id"] + ".jsonl")).open("rb") as src:
                shutil.copyfileobj(src, stream)
    extension = extension_info(temporary, sources)
    changed = manifest.get("pagination") != extension or not target.exists() or sha256_file(target) != extension["sha256"]
    if changed:
        if not manifest_path.with_suffix(".json.before-pagination").exists():
            shutil.copy2(manifest_path, manifest_path.with_suffix(".json.before-pagination"))
        os.replace(temporary, target)
        manifest["pagination"] = extension
        manifest["files"]["page_locations"] = [FILENAME]
        write_json(manifest_path, manifest)
    else:
        temporary.unlink()
    write_json(kb_dir / "pagination_coverage.json", extension)
    print(json.dumps({"changed": changed, **extension["coverage"], "pages": extension["page_count"]}), flush=True)
    return extension


def preserve_extension(kb_dir, manifest):
    """Keep only fingerprint-matching records when a future build changes sources."""
    kb_dir = Path(kb_dir)
    path = kb_dir / FILENAME
    if not path.exists():
        return
    sources = active_sources(kb_dir, manifest)
    fingerprints = {s["source_id"]: s["pdf_sha256"] for s in sources}
    temporary = path.with_suffix(".tmp")
    removed = 0
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        for row in read_rows(path):
            if fingerprints.get(row["source_id"]) == row["pdf_sha256"]:
                stream.write(json_line(row))
            else:
                removed += 1
    if removed:
        os.replace(temporary, path)
    else:
        temporary.unlink()
    # Only sources actually represented in the file are covered by this extension.
    present = {row["source_id"] for row in read_rows(path)}
    manifest["pagination"] = extension_info(path, [s for s in sources if s["source_id"] in present])
    manifest["files"]["page_locations"] = [FILENAME]


def validate_extension(kb_dir, manifest):
    extension = manifest.get("pagination")
    if not extension:
        return []
    errors = []
    path = Path(kb_dir) / FILENAME
    try:
        if extension.get("schema_version") != SCHEMA_VERSION:
            errors.append("pagination_schema_version_mismatch")
        if "display_labels" in extension and extension["display_labels"] != DISPLAY_LABELS:
            errors.append("pagination_display_labels_mismatch")
        if manifest.get("files", {}).get("page_locations") != [FILENAME]:
            errors.append("pagination_file_registration_mismatch")
        if sha256_file(path) != extension.get("sha256"):
            errors.append("pagination_digest_mismatch")
        fingerprints = {s["source_id"]: s["pdf_sha256"] for s in active_sources(kb_dir, manifest)}
        seen = set()
        for row in read_rows(path):
            key = row["source_id"], row["pdf_page_number"]
            if key in seen or not isinstance(key[1], int) or key[1] < 1:
                errors.append("pagination_duplicate_or_invalid_page")
            seen.add(key)
            if (fingerprints.get(key[0]) != row["pdf_sha256"]
                    or extension.get("source_fingerprints", {}).get(key[0]) != row["pdf_sha256"]):
                errors.append("pagination_source_fingerprint_mismatch")
            for field in ("pdf_edition_status", "original_book_status"):
                if row.get(field) not in {"verified", "not_marked", "unavailable", "unresolved"}:
                    errors.append("pagination_invalid_verification_status")
        if len(seen) != extension.get("page_count"):
            errors.append("pagination_count_mismatch")
    except (OSError, ValueError, KeyError, TypeError):
        errors.append("pagination_unreadable_or_invalid")
    return sorted(set(errors))


def evidence_pages(row):
    if row.get("source_format", "pdf") != "pdf":
        return []
    spans = row.get("source_spans") or []
    pages = list(dict.fromkeys(s["page_number"] for s in spans if isinstance(s.get("page_number"), int)))
    if not pages:
        start = row.get("page_start") or row.get("page_number")
        end = row.get("page_end") or start
        if isinstance(start, int) and isinstance(end, int) and 0 < start <= end:
            pages = list(range(start, end + 1))
    return pages


def attach_page_locations(kb_dir, manifest, rows):
    needed = {(row["source_id"], p) for row in rows for p in evidence_pages(row)}
    found = {}
    extension = manifest.get("pagination", {})
    for name in manifest.get("files", {}).get("page_locations", []):
        for item in read_rows(Path(kb_dir) / name):
            key = item["source_id"], item["pdf_page_number"]
            if key in needed and extension.get("source_fingerprints", {}).get(key[0]) == item["pdf_sha256"]:
                found[key] = item
    for row in rows:
        source = row["source_id"]
        row["page_locations"] = [found.get((source, p), {
            "source_id": source, "source_format": "pdf", "pdf_page_number": p,
            "pdf_edition_page_number": None, "pdf_edition_status": "unavailable",
            "original_book_page_numbers": [], "original_book_status": "unavailable", "observations": [],
        }) for p in evidence_pages(row)]


def compact_labels(labels):
    labels = [str(label) for label in labels]
    if len(labels) > 1 and all(label.isdecimal() and str(int(label)) == label for label in labels):
        numbers = list(map(int, labels))
        if numbers == list(range(numbers[0], numbers[0] + len(numbers))):
            return f"{labels[0]}–{labels[-1]}"
    return ", ".join(labels)


def format_page_locations(row):
    # Kept dependency-free for the standalone KB CLI. The desktop consumer has
    # the same public JSON contract and tests compare both formatters.
    locations = row.get("page_locations") or []
    pages = [item["pdf_page_number"] for item in locations] or evidence_pages(row)
    page_label = DISPLAY_LABELS["pdf_page_number"]["plural" if len(pages) > 1 else "singular"]
    parts = [f"{page_label}: {compact_labels(pages) or 'unavailable'}"]
    for key, status_key in [
        ("pdf_edition_page_number", "pdf_edition_status"),
        ("original_book_page_numbers", "original_book_status"),
    ]:
        values, statuses = [], []
        for item in locations:
            statuses.append(item.get(status_key, "unavailable"))
            if item.get(status_key) != "verified":
                continue
            raw = item.get(key)
            values.extend(raw if isinstance(raw, list) else ([raw] if raw else []))
        label = DISPLAY_LABELS[key]["plural" if max(len(values), len(locations)) > 1 else "singular"]
        if values:
            text = compact_labels(values)
            if any(s != "verified" for s in statuses):
                text = ", ".join(map(str, values)) + " (incomplete)"
            parts.append(f"{label}: {text}")
        else:
            status = "unresolved" if "unresolved" in statuses else (
                "not marked" if statuses and all(s == "not_marked" for s in statuses) else "unavailable")
            parts.append(f"{label}: {status}")
    return " · ".join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--pdf-root", type=Path)
    parser.add_argument("--refresh-labels", action="store_true", help="Update label descriptions only; no PDF scan or JSONL rewrite.")
    args = parser.parse_args()
    try:
        if args.refresh_labels:
            refresh_labels(args.kb_dir)
        else:
            refresh(args.kb_dir, args.pdf_root)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Pagination update stopped: {exc}\n")


if __name__ == "__main__":
    main()
