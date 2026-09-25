#!/usr/bin/env python3
"""Search the generated Christian theology knowledge-base bundle."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import textwrap
from pathlib import Path

import kb_common as common


ROOT = Path(__file__).resolve().parent
KB_DIR = ROOT
DEFAULT_LIMIT = 20
SNIPPET_RADIUS = 180

EVIDENCE_KEYS = {
    "primary": "core_primary_evidence",
    "extended": "extended_evidence",
    "archival": "archival_evidence",
}
BODY_ROLE = "body"
FOOTNOTE_ROLE = "footnote"

DASH_TRANSLATION = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
)


def configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass


def die(message: str, exit_code: int = 2) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(exit_code)


def norm_text(value: str | None) -> str:
    if value is None:
        return ""
    value = value.strip().strip("\"'")
    value = value.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", value).casefold()


def norm_lookup(value: str | None) -> str:
    return norm_text(value).translate(DASH_TRANSLATION)


def norm_source_path(value: str | None) -> str:
    return norm_text(value).replace("\\", "/")


def display_text(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", value.replace("\u00a0", " ")).strip()


def evidence_text(row: dict, raw_cache: dict[tuple[str, int], str] | None = None, raw: bool = False) -> str:
    if raw:
        source_id = row.get("source_id")
        page_number = row.get("page_number")
        if source_id and isinstance(page_number, int):
            return raw_page_text(source_id, page_number, raw_cache if raw_cache is not None else {})
    return row.get("reader_text") or row.get("verbatim_text") or ""


def wanted_roles(args: argparse.Namespace) -> set[str]:
    if args.role == "all":
        return {BODY_ROLE, FOOTNOTE_ROLE}
    if args.role == FOOTNOTE_ROLE:
        return {FOOTNOTE_ROLE}
    if args.include_footnotes:
        return {BODY_ROLE, FOOTNOTE_ROLE}
    return {BODY_ROLE}


def role_matches(row: dict, args: argparse.Namespace) -> bool:
    return (row.get("text_role") or BODY_ROLE) in wanted_roles(args)


def layer_matches(row: dict, args: argparse.Namespace) -> bool:
    if args.layer == "all":
        return True
    if row.get("retrieval_layer") == args.layer:
        return True
    wants_footnotes = args.include_footnotes or args.role in {FOOTNOTE_ROLE, "all"}
    return wants_footnotes and (row.get("text_role") or BODY_ROLE) == FOOTNOTE_ROLE


def raw_page_text(source_id: str, page_number: int, raw_cache: dict[tuple[str, int], str]) -> str:
    key = (source_id, page_number)
    if key in raw_cache:
        return raw_cache[key]
    path = KB_DIR / "_source_cache" / source_id / "pages.jsonl"
    if not path.exists():
        die(
            "--raw requires the optional source-audit package: "
            f"missing {path}. Obtain the audit package or omit --raw."
        )
    text = ""
    for row in common.iter_jsonl(path):
        if row.get("page_number") == page_number:
            text = row.get("original_text", "")
            break
    raw_cache[key] = text
    return text


def load_manifest() -> dict:
    manifest_path = KB_DIR / "christian_kb_manifest.json"
    manifest = common.read_json(manifest_path)
    if manifest is None:
        die(f"missing required file: {manifest_path}")
    return manifest


def manifest_paths(manifest: dict, key: str) -> list[Path]:
    return [KB_DIR / name for name in manifest.get("files", {}).get(key, [])]


def load_sources(manifest: dict) -> dict[str, dict]:
    sources: dict[str, dict] = {}
    for path in manifest_paths(manifest, "source_registry"):
        for row in common.iter_jsonl(path):
            source_id = row.get("source_id")
            if source_id:
                sources[source_id] = dict(row)

    csv_path = KB_DIR / "christian_kb_sources.csv"
    if csv_path.exists():
        with csv_path.open("r", encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                source_id = row.get("source_id")
                if source_id:
                    sources.setdefault(source_id, {}).update(row)
    return sources


def resolve_source_ids(source_arg: str | None, sources: dict[str, dict]) -> set[str] | None:
    if not source_arg:
        return None

    query = source_arg.strip().strip("\"'")
    query_norm = norm_text(query)
    query_path_norm = norm_source_path(query)

    exact_ids = {
        source_id
        for source_id in sources
        if norm_text(source_id) == query_norm
    }
    if exact_ids:
        return exact_ids

    exact_titles = {
        source_id
        for source_id, source in sources.items()
        if norm_text(source.get("display_title")) == query_norm
    }
    if exact_titles:
        return exact_titles

    exact_paths = {
        source_id
        for source_id, source in sources.items()
        if norm_source_path(source.get("pdf_file")) == query_path_norm
        or norm_text(Path(source.get("pdf_file", "")).stem) == query_norm
    }
    if exact_paths:
        return exact_paths

    partials = {
        source_id
        for source_id, source in sources.items()
        if query_norm in norm_text(source.get("display_title"))
        or query_path_norm in norm_source_path(source.get("pdf_file"))
    }
    if len(partials) == 1:
        return partials
    if len(partials) > 1:
        lines = [
            f"{source_id}: {sources[source_id].get('display_title', '')}"
            for source_id in sorted(partials)[:12]
        ]
        suffix = "" if len(partials) <= 12 else f"\n...and {len(partials) - 12} more"
        die(
            "ambiguous --source; use a source id or a more specific title.\n"
            + "\n".join(lines)
            + suffix
        )

    die(f"unknown --source: {source_arg!r}")


def requested_authors(args: argparse.Namespace) -> list[str]:
    authors = []
    if args.author:
        authors.extend(arg_values(args.author))
    if args.authors:
        authors.extend(arg_values(args.authors))
    seen = set()
    result = []
    for author in authors:
        key = norm_text(author)
        if key and key not in seen:
            seen.add(key)
            result.append(author)
    return result


def arg_values(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [item for item in value if item]


def requested_mentioned_authors(args: argparse.Namespace) -> list[str]:
    authors = []
    if getattr(args, "mentioned_author", None):
        authors.extend(arg_values(args.mentioned_author))
    if getattr(args, "mentioned_authors", None):
        authors.extend(arg_values(args.mentioned_authors))
    seen = set()
    result = []
    for author in authors:
        key = norm_text(author)
        if key and key not in seen:
            seen.add(key)
            result.append(author)
    return result


def author_matches(author: str | None, requested: list[str]) -> bool:
    if not requested:
        return True
    actual = norm_text(author)
    return any(actual == norm_text(item) for item in requested)


def mentioned_author_matches(row: dict, requested: list[str]) -> bool:
    if not requested:
        return True
    actual = {norm_text(author) for author in row.get("mentioned_authors", []) if author}
    return any(norm_text(author) in actual for author in requested)


def evidence_section_probe_text(evidence: dict) -> str:
    text = evidence_text(evidence)
    first_block = text.split("\n\n", 1)[0]
    return compact_section_probe(" ".join(
        value
        for value in [
            evidence.get("active_heading") or "",
            evidence.get("outline_path") or "",
            evidence.get("heading") or "",
            first_block[:700],
        ]
        if value
    ))


def compact_section_probe(value: str) -> str:
    return display_text(value)[:1400]


def section_matches(
    evidence: dict,
    section: str | None,
    section_alias_paths: dict[str, set[str]] | None = None,
) -> bool:
    if not section:
        return True
    query = norm_lookup(section)
    values = [
        evidence.get("active_heading"),
        evidence.get("outline_path"),
        evidence.get("heading"),
    ]
    if any(query in norm_lookup(value) for value in values if value):
        return True
    if query in norm_lookup(evidence_section_probe_text(evidence)):
        return True

    source_id = evidence.get("source_id")
    outline_path = norm_lookup(evidence.get("outline_path"))
    for alias_path in (section_alias_paths or {}).get(source_id, set()):
        if outline_path == alias_path or outline_path.startswith(alias_path + " >"):
            return True
    return False


def find_section_alias_paths(
    manifest: dict,
    layer: str,
    section: str | None,
    source_ids: set[str] | None,
    include_footnotes: bool = False,
) -> dict[str, set[str]]:
    if not section:
        return {}

    query = norm_lookup(section)
    aliases: dict[str, set[str]] = {}
    for path in selected_evidence_paths(manifest, layer, include_footnotes):
        for row in common.iter_jsonl(path):
            source_id = row.get("source_id")
            if source_ids is not None and source_id not in source_ids:
                continue

            direct_values = [
                row.get("active_heading"),
                row.get("outline_path"),
                row.get("heading"),
            ]
            if any(query in norm_lookup(value) for value in direct_values if value):
                continue
            if query not in norm_lookup(evidence_section_probe_text(row)):
                continue

            outline_path = norm_lookup(row.get("outline_path"))
            if outline_path:
                aliases.setdefault(source_id, set()).add(outline_path)
    return aliases


def section_needs_alias_paths(section: str | None) -> bool:
    if not section:
        return False
    section_key = norm_lookup(section)
    return bool(re.search(r"\b(book|volume|chapter|article|epistle|section)\s+[0-9ivxlcdm.]+\b", section_key))


def row_matches_term(row: dict, term: str | None) -> bool:
    if not term:
        return True
    return norm_lookup(row.get("lookup_form") or row.get("shared_value")) == norm_lookup(term)


def row_matches_scripture(row: dict, scripture: str | None) -> bool:
    if not scripture:
        return True
    query = norm_lookup(scripture)
    key = norm_lookup(row.get("canonical_or_unresolved_key") or row.get("shared_value"))
    if key == query:
        return True
    printed_forms = row.get("printed_forms")
    if isinstance(printed_forms, dict):
        return any(norm_lookup(form) == query for form in printed_forms)
    return False


def selected_evidence_paths(manifest: dict, layer: str, include_footnotes: bool = False) -> list[Path]:
    if layer == "all" or include_footnotes:
        paths: list[Path] = []
        for item in ("primary", "extended", "archival"):
            paths.extend(manifest_paths(manifest, EVIDENCE_KEYS[item]))
        return paths
    return manifest_paths(manifest, EVIDENCE_KEYS[layer])


def matching_concordance_locations(
    manifest: dict,
    args: argparse.Namespace,
    source_ids: set[str] | None,
) -> tuple[list[dict], str, str]:
    if args.term:
        paths = manifest_paths(manifest, "term_concordance")
        match_type = "term"
        match_value = display_text(args.term)
    else:
        paths = manifest_paths(manifest, "scripture_concordance")
        match_type = "scripture"
        match_value = display_text(args.scripture)

    locations = []
    for path in paths:
        for row in common.iter_jsonl(path):
            if source_ids is not None and row.get("source_id") not in source_ids:
                continue
            if args.term and not row_matches_term(row, args.term):
                continue
            if args.scripture and not row_matches_scripture(row, args.scripture):
                continue
            for location in row.get("evidence_locations", []):
                item = dict(location)
                item["source_id"] = row.get("source_id")
                item["concordance_occurrence_count"] = row.get("occurrence_count", 0)
                locations.append(item)
    return locations, match_type, match_value


def load_evidence_by_id(paths: list[Path], wanted_ids: set[str]) -> dict[str, dict]:
    evidence: dict[str, dict] = {}
    if not wanted_ids:
        return evidence
    missing = set(wanted_ids)
    for path in paths:
        for row in common.iter_jsonl(path):
            evidence_id = row.get("evidence_id")
            if evidence_id in missing:
                evidence[evidence_id] = row
                missing.remove(evidence_id)
                if not missing:
                    return evidence
    return evidence


def compile_match_regex(match_type: str, match_value: str) -> re.Pattern | None:
    if not match_value:
        return None
    if match_type == "term":
        pattern = re.escape(match_value)
        pattern = pattern.replace(r"\ ", r"[\s\-]+")
        return re.compile(rf"(?i)(?<![A-Za-z0-9])({pattern})(?![A-Za-z0-9])")
    return re.compile(re.escape(match_value).replace(r"\-", r"[\-\u2010-\u2014\u2212]"), re.I)


def make_snippet(text: str, regex: re.Pattern | None, char_start: int | None = None) -> str:
    text = text.replace("\u0000", "").replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""

    match = regex.search(text) if regex else None
    if match:
        start = max(0, match.start() - SNIPPET_RADIUS)
        end = min(len(text), match.end() + SNIPPET_RADIUS)
        snippet = text[start:end].strip()
        snippet = regex.sub(lambda found: f"[[{found.group(0)}]]", snippet, count=1)
    elif isinstance(char_start, int):
        start = max(0, char_start - SNIPPET_RADIUS)
        end = min(len(text), char_start + SNIPPET_RADIUS)
        snippet = text[start:end].strip()
    else:
        start = 0
        end = min(len(text), SNIPPET_RADIUS * 2)
        snippet = text[: SNIPPET_RADIUS * 2].strip()

    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


def source_title(source_id: str | None, sources: dict[str, dict]) -> str:
    return sources.get(source_id or "", {}).get("display_title", "")


def source_pdf(source_id: str | None, sources: dict[str, dict]) -> str:
    return sources.get(source_id or "", {}).get("pdf_file", "")


def result_from_evidence(
    evidence: dict,
    location: dict,
    sources: dict[str, dict],
    match_type: str,
    match_value: str,
    regex: re.Pattern | None,
    args: argparse.Namespace,
    raw_cache: dict[tuple[str, int], str],
    section_filter: str | None = None,
) -> dict:
    source_id = evidence.get("source_id") or location.get("source_id")
    heading = evidence.get("active_heading") or ""
    outline_path = evidence.get("outline_path") or ""
    char_start = location.get("first_char_start", location.get("char_start"))
    page_number = location.get("page_number") or evidence.get("page_number")
    if args.raw and source_id and isinstance(page_number, int):
        snippet_text = raw_page_text(source_id, page_number, raw_cache)
    else:
        snippet_text = evidence_text(evidence)
    return {
        "source_id": source_id,
        "source_title": source_title(source_id, sources),
        "pdf_file": source_pdf(source_id, sources),
        "author": evidence.get("work_attributed_author") or "",
        "mentioned_authors": evidence.get("mentioned_authors", []),
        "page_number": page_number,
        "page_start": evidence.get("page_start") or evidence.get("page_number") or location.get("page_number"),
        "page_end": evidence.get("page_end") or evidence.get("page_number") or location.get("page_number"),
        "evidence_id": evidence.get("evidence_id") or location.get("evidence_id"),
        "text_role": evidence.get("text_role") or BODY_ROLE,
        "text_status": evidence.get("text_status") or "",
        "occurrence_count": location.get("occurrence_count", 1),
        "occurrence_scope": "evidence",
        "heading": heading,
        "outline_path": outline_path,
        "retrieval_layer": evidence.get("retrieval_layer") or "",
        "match_type": match_type,
        "match_value": match_value,
        "section_filter": section_filter or "",
        "snippet_text_source": "raw_page_text" if args.raw else "reader_text",
        "snippet": make_snippet(snippet_text, regex, char_start if not args.raw else None),
    }


def search_concordance(
    manifest: dict,
    sources: dict[str, dict],
    args: argparse.Namespace,
    source_ids: set[str] | None,
) -> list[dict]:
    locations, match_type, match_value = matching_concordance_locations(manifest, args, source_ids)
    regex = compile_match_regex(match_type, match_value)
    evidence_ids = {item.get("evidence_id") for item in locations if item.get("evidence_id")}
    include_footnotes = args.include_footnotes or args.role in {FOOTNOTE_ROLE, "all"}
    evidence_by_id = load_evidence_by_id(selected_evidence_paths(manifest, args.layer, include_footnotes), evidence_ids)
    authors = requested_authors(args)
    mentioned_authors = requested_mentioned_authors(args)
    section_alias_paths = (
        find_section_alias_paths(manifest, args.layer, args.section, source_ids, include_footnotes)
        if section_needs_alias_paths(args.section)
        else {}
    )
    raw_cache: dict[tuple[str, int], str] = {}

    results = []
    for location in locations:
        evidence = evidence_by_id.get(location.get("evidence_id"))
        if not evidence:
            continue
        if source_ids is not None and evidence.get("source_id") not in source_ids:
            continue
        if not author_matches(evidence.get("work_attributed_author"), authors):
            continue
        if not mentioned_author_matches(evidence, mentioned_authors):
            continue
        if not section_matches(evidence, args.section, section_alias_paths):
            continue
        if not role_matches(evidence, args):
            continue
        if not layer_matches(evidence, args):
            continue
        results.append(
            result_from_evidence(
                evidence,
                location,
                sources,
                match_type,
                match_value,
                regex,
                args,
                raw_cache,
                args.section,
            )
        )
    return results


def search_crossbook(
    manifest: dict,
    sources: dict[str, dict],
    args: argparse.Namespace,
    source_ids: set[str] | None,
) -> list[dict]:
    if not args.term and not args.scripture:
        die("--crossbook requires --term or --scripture")

    if args.term:
        expected_basis = "shared_literal_term"
        match_type = "term"
        match_value = display_text(args.term)
    else:
        expected_basis = "shared_explicit_scripture_reference"
        match_type = "scripture"
        match_value = display_text(args.scripture)
    regex = compile_match_regex(match_type, match_value)
    authors = requested_authors(args)
    requested_author_keys = {norm_text(author) for author in authors}
    mentioned_authors = requested_mentioned_authors(args)
    requested_mentioned_author_keys = {norm_text(author) for author in mentioned_authors}
    raw_cache: dict[tuple[str, int], str] = {}

    results = []
    for path in manifest_paths(manifest, "crossbook_evidence"):
        for row in common.iter_jsonl(path):
            if row.get("link_basis") != expected_basis:
                continue
            if args.term and not row_matches_term(row, args.term):
                continue
            if args.scripture and not row_matches_scripture(row, args.scripture):
                continue

            source_evidence = row.get("source_evidence", [])
            row_author_keys = {
                norm_text(item.get("work_attributed_author"))
                for item in source_evidence
                if item.get("work_attributed_author")
            }
            if requested_author_keys and not requested_author_keys.issubset(row_author_keys):
                continue
            row_mentioned_author_keys = {
                norm_text(author)
                for item in source_evidence
                for author in item.get("mentioned_authors", [])
                if author
            }
            if requested_mentioned_author_keys and not requested_mentioned_author_keys.issubset(row_mentioned_author_keys):
                continue

            for item in source_evidence:
                source_id = item.get("source_id")
                if (item.get("text_role") or BODY_ROLE) not in wanted_roles(args):
                    continue
                if not layer_matches(item, args):
                    continue
                if source_ids is not None and source_id not in source_ids:
                    continue
                if authors and not author_matches(item.get("work_attributed_author"), authors):
                    continue
                if not mentioned_author_matches(item, mentioned_authors):
                    continue
                snippet_text = item.get("evidence_text", "")
                if args.raw and source_id and isinstance(item.get("page_number"), int):
                    snippet_text = raw_page_text(source_id, item["page_number"], raw_cache)
                results.append(
                    {
                        "source_id": source_id,
                        "source_title": source_title(source_id, sources),
                        "pdf_file": source_pdf(source_id, sources),
                        "author": item.get("work_attributed_author") or "",
                        "mentioned_authors": item.get("mentioned_authors", []),
                        "page_number": item.get("page_number"),
                        "evidence_id": item.get("evidence_id"),
                        "text_role": item.get("text_role") or BODY_ROLE,
                        "snippet_text_source": "raw_page_text" if args.raw else "reader_text",
                        "occurrence_count": item.get("occurrence_count", 1),
                        "occurrence_scope": "source",
                        "heading": "",
                        "outline_path": "",
                        "retrieval_layer": "crossbook",
                        "match_type": match_type,
                        "match_value": row.get("shared_value") or match_value,
                        "section_filter": args.section or "",
                        "snippet": make_snippet(snippet_text, regex),
                        "crossbook_id": row.get("crossbook_id"),
                        "crossbook_source_count": row.get("source_count"),
                        "crossbook_occurrence_count": row.get("occurrence_count"),
                    }
                )
    if args.section:
        evidence_ids = {row.get("evidence_id") for row in results if row.get("evidence_id")}
        include_footnotes = args.include_footnotes or args.role in {FOOTNOTE_ROLE, "all"}
        evidence_lookup = load_evidence_by_id(selected_evidence_paths(manifest, "all", include_footnotes), evidence_ids)
        section_alias_paths = (
            find_section_alias_paths(manifest, "all", args.section, source_ids, include_footnotes)
            if section_needs_alias_paths(args.section)
            else {}
        )
        filtered = []
        for row in results:
            evidence = evidence_lookup.get(row.get("evidence_id"), {})
            if not section_matches(evidence, args.section, section_alias_paths):
                continue
            row["heading"] = evidence.get("active_heading") or ""
            row["outline_path"] = evidence.get("outline_path") or ""
            filtered.append(row)
        results = filtered
    return results


def print_jsonl(results: list[dict], limit: int) -> None:
    for row in results[:limit]:
        print(json.dumps(row, ensure_ascii=False, sort_keys=True))


def print_readable(results: list[dict], args: argparse.Namespace, source_ids: set[str] | None) -> None:
    query_bits = []
    if args.crossbook:
        query_bits.append("crossbook")
    if args.term:
        query_bits.append(f'term="{args.term}"')
    if args.scripture:
        query_bits.append(f'scripture="{args.scripture}"')
    if args.source:
        resolved = ", ".join(sorted(source_ids or []))
        query_bits.append(f'source="{args.source}" ({resolved})')
    if args.section:
        query_bits.append(f'section="{args.section}"')
    for author in requested_authors(args):
        query_bits.append(f'author="{author}"')
    for author in requested_mentioned_authors(args):
        query_bits.append(f'mentioned_author="{author}"')
    query_bits.append(f"layer={args.layer}")
    query_bits.append(f"role={args.role}")
    if args.include_footnotes:
        query_bits.append("include_footnotes=true")
    if args.raw:
        query_bits.append("raw=true")

    displayed = min(len(results), args.limit)
    print(f"Query: {', '.join(query_bits)}")
    print(f"Matches: {len(results)}; displaying: {displayed}")
    if not results:
        return

    for index, row in enumerate(results[: args.limit], 1):
        source = row["source_id"]
        title = row["source_title"]
        author = row["author"] or "Unknown"
        page = row["page_number"]
        page_range = ""
        if row.get("page_start") and row.get("page_end") and row.get("page_start") != row.get("page_end"):
            page_range = f"-{row['page_end']}"
        evidence_id = row["evidence_id"]
        count = row["occurrence_count"]
        count_label = "source occurrences" if row.get("occurrence_scope") == "source" else "occurrences"
        print()
        print(
            f"{index}. {source} p.{page}{page_range} | {author} | "
            f"{row.get('text_role', BODY_ROLE)} | {evidence_id} | {count_label}: {count}"
        )
        if title:
            print(f"   {title}")
        if row.get("heading"):
            print(f"   Heading: {row['heading']}")
        elif row.get("outline_path"):
            print(f"   Path: {row['outline_path']}")
        if row.get("mentioned_authors"):
            print(f"   Mentioned authors: {', '.join(row['mentioned_authors'])}")
        if row.get("crossbook_id"):
            print(
                "   "
                f"Crossbook: {row['crossbook_id']} "
                f"({row.get('crossbook_source_count')} sources, "
                f"{row.get('crossbook_occurrence_count')} occurrences)"
            )
        if not args.no_snippet and row.get("snippet"):
            wrapped = textwrap.fill(row["snippet"], width=100, subsequent_indent="   ")
            print(f"   {wrapped}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search the generated Christian theology knowledge-base bundle."
    )
    parser.add_argument("--term", nargs="+", help="Registered literal term lookup form, e.g. grace.")
    parser.add_argument("--scripture", nargs="+", help='Canonical or printed Scripture reference, e.g. "Romans 5:12".')
    parser.add_argument(
        "--source",
        nargs="+",
        help=(
            "Source id, exact book title, PDF path, or unambiguous title/path substring. "
            'Quote titles with spaces, e.g. --source "Summa Theologica".'
        ),
    )
    parser.add_argument("--author", nargs="+", help='Evidence author filter, e.g. "Augustine of Hippo".')
    parser.add_argument("--mentioned-author", nargs="+", help='Mentioned/discussed author filter, e.g. "Origen".')
    parser.add_argument(
        "--section",
        nargs="+",
        help='Filter by active heading or outline path, e.g. "Chapter IX", "Book XIV", or "Article. 7".',
    )
    parser.add_argument(
        "--authors",
        nargs="+",
        help='Multiple evidence author filters, e.g. --authors "Augustine of Hippo" "Martin Luther".',
    )
    parser.add_argument(
        "--mentioned-authors",
        nargs="+",
        help='Multiple mentioned/discussed author filters, e.g. --mentioned-authors "Origen" "Ambrose of Milan".',
    )
    parser.add_argument(
        "--layer",
        choices=["primary", "extended", "archival", "all"],
        default="primary",
        help="Evidence layer to expand for non-crossbook searches. Default: primary.",
    )
    parser.add_argument(
        "--role",
        choices=[BODY_ROLE, FOOTNOTE_ROLE, "all"],
        default=BODY_ROLE,
        help="Evidence role to search/display. Default: body.",
    )
    parser.add_argument(
        "--include-footnotes",
        action="store_true",
        help="Include separately stored footnote evidence in addition to body evidence.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Show snippets from the original mixed PDF page extraction instead of clean reader text.",
    )
    parser.add_argument("--crossbook", action="store_true", help="Search cross-book evidence links.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Maximum displayed results. Default: {DEFAULT_LIMIT}.")
    parser.add_argument("--jsonl", action="store_true", help="Emit machine-readable JSON Lines.")
    parser.add_argument("--no-snippet", action="store_true", help="Omit snippets from readable output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_output()
    parser = build_parser()
    args = parser.parse_args(argv)

    for name in ("term", "scripture", "source", "author", "mentioned_author", "section"):
        value = getattr(args, name)
        if isinstance(value, list):
            setattr(args, name, " ".join(value))

    if not args.term and not args.scripture:
        die("provide --term or --scripture")
    if args.term and args.scripture:
        die("use either --term or --scripture, not both")
    if args.limit < 1:
        die("--limit must be at least 1")
    if args.raw and not (KB_DIR / "_source_cache").is_dir():
        die(
            "--raw requires the optional source-audit package under "
            f"{KB_DIR / '_source_cache'}. Obtain that package or omit --raw."
        )
    if not (KB_DIR / "christian_kb_manifest.json").exists():
        die(
            "generated KB bundle not found. Obtain the separately published "
            f"bundle and place it in {KB_DIR}."
        )

    manifest = load_manifest()
    sources = load_sources(manifest)
    source_ids = resolve_source_ids(args.source, sources)

    if args.crossbook:
        results = search_crossbook(manifest, sources, args, source_ids)
    else:
        results = search_concordance(manifest, sources, args, source_ids)

    author_order = {
        norm_text(author): index for index, author in enumerate(requested_authors(args))
    }

    def sort_key(row: dict) -> tuple:
        author_rank = 0
        if args.crossbook and author_order:
            author_rank = author_order.get(norm_text(row.get("author")), len(author_order))
        return (
            author_rank,
            row.get("source_id") or "",
            row.get("page_number") or 0,
            row.get("evidence_id") or "",
        )

    results.sort(key=sort_key)

    if args.jsonl:
        print_jsonl(results, args.limit)
    else:
        print_readable(results, args, source_ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
