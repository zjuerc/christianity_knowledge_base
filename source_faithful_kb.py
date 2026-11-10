import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pdfplumber
from pypdf import PdfReader

import kb_common as legacy
import page_locations


WORKSPACE = legacy.WORKSPACE
OUT_DIR = legacy.OUT_DIR
CACHE_DIR = legacy.CACHE_DIR
SCHEMA_VERSION = "3.2.0"
BUILDER_VERSION = "2026-09-13.author-role-separation"
TERM_REGISTRY_SCHEMA_VERSION = "1.0.0"
TERM_REGISTRY_FILENAME = "christian_kb_term_registry.jsonl"
TERM_REGISTRY_SOURCE_PATH = Path(__file__).resolve().parent / TERM_REGISTRY_FILENAME
TERM_REQUESTS_FILENAME = "term_requests.json"
TERM_REQUESTS_PATH = Path(__file__).resolve().parent / TERM_REQUESTS_FILENAME
AUTHOR_REVIEW_FILENAME = "author_attribution_reviews.jsonl"
AUTHOR_REVIEW_PATH = OUT_DIR / AUTHOR_REVIEW_FILENAME
MAX_SPLIT_BYTES = 20_000_000
MAX_EVIDENCE_CHARS = 6000

TEXT_ROLE_BODY = "body"
TEXT_ROLE_FOOTNOTE = "footnote"
TEXT_ROLE_FOOTNOTE_MARKER = "footnote_marker"
TEXT_ROLE_RUNNING_HEADER = "running_header"
TEXT_ROLE_PRINTED_PAGE_NUMBER = "printed_page_number"
TEXT_ROLE_OFFPAGE_HIDDEN = "offpage_hidden"
TEXT_ROLE_STRUCTURAL_AUXILIARY = "structural_auxiliary"
TEXT_ROLES = {
    TEXT_ROLE_BODY,
    TEXT_ROLE_FOOTNOTE,
    TEXT_ROLE_FOOTNOTE_MARKER,
    TEXT_ROLE_RUNNING_HEADER,
    TEXT_ROLE_PRINTED_PAGE_NUMBER,
    TEXT_ROLE_OFFPAGE_HIDDEN,
    TEXT_ROLE_STRUCTURAL_AUXILIARY,
}

REFERENCE_MARKERS = (
    "cover",
    "about this book",
    "title page",
    "table of contents",
    "contents",
    "index",
    "indices",
    "bibliography",
    "works consulted",
    "credits",
    "comparative table",
)
EDITORIAL_MARKERS = (
    "editor",
    "translator",
    "introductory",
    "introduction",
    "prolegomena",
    "biographical",
    "chronological",
    "literature",
    "preface",
    "foreword",
    "notice",
)
ARGUMENT_PART_MARKERS = {
    "reply to objection": "reply_to_objection",
    "on the contrary": "on_the_contrary",
    "i answer that": "respondeo",
    "objection": "objection",
}


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


compact_whitespace = legacy.compact_whitespace
sha256_hex = legacy.sha256_hex
sha256_file = legacy.sha256_file
portable_path = legacy.portable_path
json_dumps = legacy.json_dumps
write_json = legacy.write_json
write_jsonl = legacy.write_jsonl
iter_jsonl = legacy.iter_jsonl
read_json = legacy.read_json
context_excerpt = legacy.context_excerpt
word_count = legacy.word_count


def normalize_extracted_text(value):
    value = (value or "").replace("\x00", "")
    return value.replace("\r\n", "\n").replace("\r", "\n")


def safe_extract_page(page):
    try:
        return page.extract_text() or ""
    except Exception as exc:
        return "", f"pypdf_error:{type(exc).__name__}"


def page_has_images(page):
    try:
        resources = page.get("/Resources") or {}
        xobjects = resources.get("/XObject") or {}
        for value in xobjects.values():
            obj = value.get_object()
            if obj.get("/Subtype") == "/Image":
                return True
    except Exception:
        return False
    return False


def median(values):
    values = sorted(value for value in values if value is not None)
    if not values:
        return 0
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2


LAYOUT_DASH_TRANSLATION = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
)
FOOTNOTE_SYMBOL_MARKERS = "*†‡§¶"
FOOTNOTE_MARKER_RE = re.compile(
    rf"(?:\d{{1,5}}|[{re.escape(FOOTNOTE_SYMBOL_MARKERS)}]+|[a-z])",
    re.IGNORECASE,
)


def layout_color_key(value):
    if value is None:
        return "none"
    if isinstance(value, (list, tuple)):
        return "(" + ",".join(f"{float(item):.4g}" for item in value if isinstance(item, (int, float))) + ")"
    return str(value)


def layout_color_is_blackish(value):
    if value is None:
        return True
    if isinstance(value, (int, float)):
        return abs(float(value)) < 0.02
    if isinstance(value, (list, tuple)):
        numeric = [float(item) for item in value if isinstance(item, (int, float))]
        return bool(numeric) and max(abs(item) for item in numeric) < 0.02
    return False


def layout_line_signature(text):
    value = compact_whitespace(text).casefold().translate(LAYOUT_DASH_TRANSLATION)
    value = value.replace("…", "...")
    value = re.sub(r"\s+\d{1,4}$", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" .:-")


def layout_line_rows(pdf_page):
    words = pdf_page.extract_words(
        extra_attrs=["size", "fontname", "non_stroking_color"],
        keep_blank_chars=False,
        use_text_flow=False,
    )
    words = [word for word in words if compact_whitespace(word.get("text", ""))]
    rows = []
    for word in sorted(words, key=lambda item: (item.get("top", 0), item.get("x0", 0))):
        center = (float(word.get("top", 0)) + float(word.get("bottom", 0))) / 2
        row = None
        for candidate in rows:
            if abs(center - candidate["center"]) <= 2.2:
                row = candidate
                break
        if row is None:
            row = {"center": center, "words": []}
            rows.append(row)
        row["words"].append(word)
        row["center"] = sum(
            (float(item.get("top", 0)) + float(item.get("bottom", 0))) / 2
            for item in row["words"]
        ) / len(row["words"])

    line_rows = []
    for line_number, row in enumerate(sorted(rows, key=lambda item: item["center"]), start=1):
        line_words = sorted(row["words"], key=lambda item: item.get("x0", 0))
        sizes = [float(word.get("size") or 0) for word in line_words]
        font_names = sorted({str(word.get("fontname") or "") for word in line_words if word.get("fontname")})
        color_values = sorted({layout_color_key(word.get("non_stroking_color")) for word in line_words})
        x0 = min(word.get("x0", 0) for word in line_words)
        x1 = max(word.get("x1", 0) for word in line_words)
        top = min(word.get("top", 0) for word in line_words)
        bottom = max(word.get("bottom", 0) for word in line_words)
        text = compact_whitespace(" ".join(word.get("text", "") for word in line_words))
        line_rows.append(
            {
                "line_number": line_number,
                "text": text,
                "layout_signature": layout_line_signature(text),
                "x0": round(x0, 2),
                "x1": round(x1, 2),
                "top": round(top, 2),
                "bottom": round(bottom, 2),
                "center": round((top + bottom) / 2, 2),
                "font_size_min": round(min(sizes), 2) if sizes else 0,
                "font_size_max": round(max(sizes), 2) if sizes else 0,
                "font_size_median": round(median(sizes), 2),
                "font_names": font_names,
                "color_values": color_values,
                "has_colored_text": any(
                    not layout_color_is_blackish(word.get("non_stroking_color"))
                    for word in line_words
                ),
                "word_count": len(line_words),
            }
        )
    return [row for row in line_rows if row["text"]]


def layout_line_offpage(line, page_width):
    return (
        line["x1"] < -1
        or line["x0"] > page_width + 1
        or line["x0"] < -8
        or line["x1"] > page_width + 8
    )


def layout_line_in_margin(line, page_height):
    return line["top"] < page_height * 0.12 or line["bottom"] > page_height * 0.90


def layout_body_font_size(lines, width, page_height):
    counts = Counter()
    for line in lines:
        if layout_line_offpage(line, width):
            continue
        if line["font_size_median"] <= 0:
            continue
        if is_printed_page_number_line(line, page_height):
            continue
        if line["word_count"] <= 2 and layout_line_in_margin(line, page_height):
            continue
        counts[round(line["font_size_median"], 1)] += max(1, line["word_count"])
    if not counts:
        return 0
    return counts.most_common(1)[0][0]


def is_printed_page_number_line(line, page_height):
    text = compact_whitespace(line.get("text", ""))
    if line.get("word_count", 0) > 2:
        return False
    numeric = bool(re.fullmatch(r"\d{1,4}", text))
    roman = bool(re.fullmatch(r"[ivxlcdm]{1,8}", text.casefold()))
    return (numeric or roman) and (
        line["top"] < page_height * 0.12
        or line["bottom"] > page_height * 0.82
    )


def footnote_marker_from_start(text):
    text = compact_whitespace(text)
    match = re.match(
        rf"^(?:\[({FOOTNOTE_MARKER_RE.pattern})\]|({FOOTNOTE_MARKER_RE.pattern})(?:[\].):]?))(?:\s+|$)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return ""
    return compact_whitespace(match.group(1) or match.group(2)).casefold().strip("[]).:")


def footnote_marker_values(text):
    text = compact_whitespace(text).casefold()
    if not text:
        return []
    if not re.fullmatch(
        rf"(?:\[?{FOOTNOTE_MARKER_RE.pattern}\]?[\].):]?)(?:\s+\[?{FOOTNOTE_MARKER_RE.pattern}\]?[\].):]?)*",
        text,
        re.IGNORECASE,
    ):
        return []
    return [item.strip("[]).:") for item in FOOTNOTE_MARKER_RE.findall(text)]


def is_marker_only_line(text):
    return bool(footnote_marker_values(text))


def line_starts_like_footnote(text):
    return bool(footnote_marker_from_start(text))


def structural_page_reason(lines, page_width, page_height):
    visible = [
        line
        for line in sorted(lines, key=lambda item: (item["top"], item["x0"]))
        if line.get("text") and not layout_line_offpage(line, page_width)
    ]
    if not visible:
        return ""
    first_lines = [layout_line_signature(line["text"]) for line in visible[:12]]
    first_text = " ".join(first_lines)
    metadata_markers = ("author(s):", "publisher:", "ccel subjects:", "print basis:")
    if any(marker in first_text for marker in metadata_markers):
        return "front_matter_metadata_page"
    structural_titles = {
        "contents",
        "table of contents",
        "index",
        "general index",
        "scripture index",
        "index of scripture references",
        "index of subjects",
        "index of authors",
        "index of texts",
    }
    for signature in first_lines[:8]:
        if signature in structural_titles:
            return f'structural_title:"{signature}"'
        if (
            signature.startswith("index of ")
            or signature.endswith(" index")
            or signature.startswith("table of ")
        ):
            return f'structural_title:"{signature}"'
    return ""


def build_layout_context(plumber_pdf):
    context = {
        "layout_pages": {},
        "layout_errors": {},
        "repeated_margin_signatures": set(),
        "structural_page_reasons": {},
    }
    margin_pages = defaultdict(set)
    for page_number, pdf_page in enumerate(plumber_pdf.pages, start=1):
        try:
            lines = layout_line_rows(pdf_page)
        except Exception as exc:
            context["layout_errors"][page_number] = f"pdfplumber_layout_error:{type(exc).__name__}"
            continue
        page_width = float(pdf_page.width)
        page_height = float(pdf_page.height)
        context["layout_pages"][page_number] = lines
        structural_reason = structural_page_reason(lines, page_width, page_height)
        if structural_reason:
            context["structural_page_reasons"][page_number] = structural_reason
        for line in lines:
            if layout_line_offpage(line, page_width):
                continue
            if not layout_line_in_margin(line, page_height):
                continue
            if is_printed_page_number_line(line, page_height):
                continue
            signature = line.get("layout_signature") or layout_line_signature(line.get("text", ""))
            if len(signature) < 8:
                continue
            margin_pages[signature].add(page_number)
    context["repeated_margin_signatures"] = {
        signature
        for signature, page_numbers in margin_pages.items()
        if len(page_numbers) >= 2
    }
    return context


def repeated_margin_line(line, page_height, repeated_margin_signatures):
    signature = line.get("layout_signature") or layout_line_signature(line.get("text", ""))
    return bool(signature and signature in repeated_margin_signatures and layout_line_in_margin(line, page_height))


def detect_footnote_start(lines, page_width, page_height, body_size, repeated_margin_signatures):
    candidate_tops = []
    lower_small_lines = []
    for line in sorted(lines, key=lambda item: (item["top"], item["x0"])):
        if layout_line_offpage(line, page_width):
            continue
        if is_printed_page_number_line(line, page_height):
            continue
        if repeated_margin_line(line, page_height, repeated_margin_signatures):
            continue
        smaller_than_body = bool(body_size and line["font_size_median"] <= body_size - 0.6)
        if line["top"] >= page_height * 0.58 and smaller_than_body:
            lower_small_lines.append(line)
        if line["top"] < page_height * 0.45:
            continue
        if smaller_than_body and line_starts_like_footnote(line["text"]) and not is_marker_only_line(line["text"]):
            candidate_tops.append(line["top"])
    if candidate_tops:
        return min(candidate_tops)
    if len(lower_small_lines) >= 2:
        return min(line["top"] for line in lower_small_lines)
    return None


def note_markers_for_page(lines, page_width, page_height, footnote_start):
    if footnote_start is None:
        return set()
    markers = set()
    for line in lines:
        if layout_line_offpage(line, page_width):
            continue
        if line["top"] < footnote_start - 1:
            continue
        marker = footnote_marker_from_start(line["text"])
        if marker:
            markers.add(marker)
    return markers


def linked_inline_marker_line(line, body_lines, body_size, note_markers, footnote_start):
    markers = footnote_marker_values(line.get("text", ""))
    if not markers or not note_markers:
        return False
    if not all(marker in note_markers for marker in markers):
        return False
    if footnote_start is not None and line["top"] >= footnote_start - 1:
        return False
    smaller_than_body = bool(body_size and line["font_size_max"] <= body_size - 0.5)
    if not smaller_than_body:
        return False
    for body_line in body_lines:
        vertical_distance = abs(line["center"] - body_line["center"])
        horizontally_near = body_line["x0"] - 12 <= line["x0"] <= body_line["x1"] + 12
        if vertical_distance <= max(12, body_size * 1.25) and horizontally_near:
            return True
    return False


def isolated_margin_marker_line(line, body_lines, page_width, body_size, footnote_start):
    markers = footnote_marker_values(line.get("text", ""))
    if not markers or not body_lines:
        return False
    if footnote_start is not None and line["top"] >= footnote_start - 1:
        return False
    if not body_size or line["font_size_max"] > body_size - 1.5:
        return False
    flow_lines = [candidate for candidate in body_lines if candidate.get("word_count", 0) >= 5]
    if not flow_lines:
        return False
    body_x0 = min(candidate["x0"] for candidate in flow_lines)
    body_x1 = max(candidate["x1"] for candidate in flow_lines)
    side_margin = line["x1"] < body_x0 - 10 or line["x0"] > body_x1 + 10
    near_page_edge = line["x0"] < page_width * 0.16 or line["x1"] > page_width * 0.84
    if not side_margin and not near_page_edge:
        return False
    for body_line in flow_lines:
        if abs(line["center"] - body_line["center"]) <= max(12, body_size * 1.25):
            return True
    return False


def classify_layout_lines(
    lines,
    page_width,
    page_height,
    repeated_margin_signatures=None,
    structural_reason="",
):
    repeated_margin_signatures = repeated_margin_signatures or set()
    body_size = layout_body_font_size(lines, page_width, page_height)
    footnote_start = detect_footnote_start(
        lines,
        page_width,
        page_height,
        body_size,
        repeated_margin_signatures,
    )
    note_markers = note_markers_for_page(lines, page_width, page_height, footnote_start)
    body_lines = [
        line
        for line in lines
        if not layout_line_offpage(line, page_width)
        and body_size
        and abs(line["font_size_median"] - body_size) <= 0.4
        and not is_printed_page_number_line(line, page_height)
    ]

    classified = []
    footnotes_started = False
    for line in sorted(lines, key=lambda item: (item["top"], item["x0"])):
        role = TEXT_ROLE_BODY
        reasons = []
        confidence = 0.82
        smaller_than_body = bool(body_size and line["font_size_median"] <= body_size - 0.6)

        if layout_line_offpage(line, page_width):
            role = TEXT_ROLE_OFFPAGE_HIDDEN
            reasons.append("line_bbox_outside_page_bounds")
            confidence = 0.99
        elif is_printed_page_number_line(line, page_height):
            role = TEXT_ROLE_PRINTED_PAGE_NUMBER
            reasons.append("standalone_numeric_or_roman_margin_line")
            confidence = 0.95
        elif structural_reason:
            role = TEXT_ROLE_STRUCTURAL_AUXILIARY
            reasons.append(structural_reason)
            confidence = 0.9
        elif repeated_margin_line(line, page_height, repeated_margin_signatures):
            role = TEXT_ROLE_RUNNING_HEADER
            reasons.append("repeated_margin_signature_across_pages")
            confidence = 0.9
        elif (
            footnote_start is not None
            and line["top"] >= footnote_start - 1
            and (smaller_than_body or footnotes_started or line_starts_like_footnote(line["text"]))
        ):
            role = TEXT_ROLE_FOOTNOTE
            reasons.append("lower_page_footnote_block")
            if smaller_than_body:
                reasons.append("font_smaller_than_body")
            if line_starts_like_footnote(line["text"]):
                reasons.append("starts_with_note_marker")
            confidence = 0.88
            footnotes_started = True
        elif linked_inline_marker_line(line, body_lines, body_size, note_markers, footnote_start):
            role = TEXT_ROLE_FOOTNOTE_MARKER
            reasons.append("small_raised_marker_linked_to_same_page_footnote")
            confidence = 0.93
        elif isolated_margin_marker_line(line, body_lines, page_width, body_size, footnote_start):
            role = TEXT_ROLE_FOOTNOTE_MARKER
            reasons.append("small_isolated_marker_outside_body_flow")
            confidence = 0.9
        else:
            reasons.append("body_default_no_positive_auxiliary_evidence")
            if line.get("has_colored_text"):
                reasons.append("color_retained_because_color_alone_is_not_auxiliary")
            if smaller_than_body:
                reasons.append("smaller_text_retained_without_footnote_or_marker_context")

        row = dict(line)
        row["text_role"] = role
        row["classification_confidence"] = round(confidence, 2)
        row["classification_reasons"] = reasons
        classified.append(row)
    return classified


def join_body_lines(lines):
    operations = set()
    output = ""
    previous = None
    for line in lines:
        text = compact_whitespace(line.get("text", ""))
        if not text:
            continue
        if not output:
            output = text
            previous = line
            continue

        gap = max(0, line["top"] - previous["bottom"]) if previous else 0
        previous_height = max(1, previous["bottom"] - previous["top"]) if previous else 1
        paragraph_break = gap > max(12, previous_height * 1.6)

        if output.endswith("-") and text[:1].islower():
            output = output[:-1] + text
            operations.add("repair_line_break_hyphenation")
        else:
            output += ("\n\n" if paragraph_break else " ") + text
            operations.add("repair_line_wrapping")
        previous = line
    return output.strip(), sorted(operations)


def join_footnote_lines(lines):
    output = ""
    operations = set()
    for line in lines:
        text = compact_whitespace(line.get("text", ""))
        if not text:
            continue
        if not output:
            output = text
        elif re.match(r"^\d{1,5}\b", text):
            output += "\n" + text
            operations.add("preserve_footnote_line_breaks")
        else:
            output += " " + text
            operations.add("repair_line_wrapping")
    return output.strip(), sorted(operations)


def join_auxiliary_lines(lines):
    return "\n".join(compact_whitespace(line.get("text", "")) for line in lines if line.get("text")).strip()


def page_text_from_blocks(blocks, role):
    parts = [block["text"] for block in blocks if block["text_role"] == role and block.get("text")]
    if role == TEXT_ROLE_FOOTNOTE:
        return "\n".join(parts).strip()
    return "\n\n".join(parts).strip()


def parse_footnote_refs(footnote_text):
    refs = []
    if not footnote_text:
        return refs
    marker_pattern = rf"(?:\d{{1,5}}|[{re.escape(FOOTNOTE_SYMBOL_MARKERS)}]+|\[[a-z]\]|[a-z][.)])"
    pattern = re.compile(rf"(?ms)^({marker_pattern})\s+(.+?)(?=\n{marker_pattern}\s+|\Z)")
    for match in pattern.finditer(footnote_text):
        refs.append(
            {
                "marker": match.group(1).strip("[]).:").casefold(),
                "note_text": compact_whitespace(match.group(2)),
            }
        )
    return refs


def layout_blocks_for_page(source_id, page_id, page_number, pdf_page=None, lines=None, layout_context=None):
    try:
        if lines is None:
            lines = layout_line_rows(pdf_page)
        layout_context = layout_context or {}
        lines = classify_layout_lines(
            lines,
            float(pdf_page.width),
            float(pdf_page.height),
            layout_context.get("repeated_margin_signatures", set()),
            layout_context.get("structural_page_reasons", {}).get(page_number, ""),
        )
    except Exception as exc:
        return [], [f"pdfplumber_layout_error:{type(exc).__name__}"]

    blocks = []
    current_role = None
    current_lines = []
    page_has_inline_markers = any(line["text_role"] == TEXT_ROLE_FOOTNOTE_MARKER for line in lines)

    def flush():
        if not current_lines:
            return
        role = current_role
        if role == TEXT_ROLE_BODY:
            text, operations = join_body_lines(current_lines)
        elif role == TEXT_ROLE_FOOTNOTE:
            text, operations = join_footnote_lines(current_lines)
        else:
            text = join_auxiliary_lines(current_lines)
            operations = []
        if role != TEXT_ROLE_BODY:
            operations = sorted(set(operations + [f"classify_as_{role}"]))
        if role == TEXT_ROLE_BODY:
            body_operations = ["classify_as_body"]
            if page_has_inline_markers:
                body_operations.append("exclude_linked_inline_footnote_markers_from_reader_text")
            operations = sorted(set(operations + body_operations))
        confidence_values = [
            line.get("classification_confidence")
            for line in current_lines
            if isinstance(line.get("classification_confidence"), (int, float))
        ]
        classification_reasons = sorted(
            {
                reason
                for line in current_lines
                for reason in line.get("classification_reasons", [])
            }
        )
        blocks.append(
            {
                "block_number": len(blocks) + 1,
                "block_id": f"{source_id}_PAGE_{page_number:05d}_BLOCK_{len(blocks) + 1:02d}",
                "source_id": source_id,
                "page_id": page_id,
                "page_number": page_number,
                "text_role": role,
                "text": text,
                "line_start": current_lines[0]["line_number"],
                "line_end": current_lines[-1]["line_number"],
                "bbox": {
                    "x0": min(line["x0"] for line in current_lines),
                    "top": min(line["top"] for line in current_lines),
                    "x1": max(line["x1"] for line in current_lines),
                    "bottom": max(line["bottom"] for line in current_lines),
                },
                "font_size_min": min(line["font_size_min"] for line in current_lines),
                "font_size_max": max(line["font_size_max"] for line in current_lines),
                "font_size_median": round(median(line["font_size_median"] for line in current_lines), 2),
                "font_names": sorted(
                    {
                        font_name
                        for line in current_lines
                        for font_name in line.get("font_names", [])
                    }
                ),
                "color_values": sorted(
                    {
                        color_value
                        for line in current_lines
                        for color_value in line.get("color_values", [])
                    }
                ),
                "has_colored_text": any(line.get("has_colored_text") for line in current_lines),
                "classification_confidence": round(min(confidence_values), 2) if confidence_values else None,
                "classification_reasons": classification_reasons,
                "cleaning_operations": operations,
            }
        )

    for line in lines:
        role = line["text_role"]
        if current_role is None:
            current_role = role
            current_lines = [line]
        elif role == current_role:
            current_lines.append(line)
        else:
            flush()
            current_role = role
            current_lines = [line]
    flush()
    return blocks, []


def content_role_for_heading(title, path=""):
    text = compact_whitespace(f"{path} > {title}").casefold()
    for marker, role in ARGUMENT_PART_MARKERS.items():
        if marker in text:
            return role, f'explicit_heading_contains:"{marker}"'
    if any(marker in text for marker in REFERENCE_MARKERS):
        marker = next(marker for marker in REFERENCE_MARKERS if marker in text)
        return "reference_material", f'explicit_heading_contains:"{marker}"'
    if any(marker in text for marker in EDITORIAL_MARKERS):
        marker = next(marker for marker in EDITORIAL_MARKERS if marker in text)
        return "editorial_material", f'explicit_heading_contains:"{marker}"'
    if text.strip(" >"):
        return "unmarked_body_text", "no_explicit_nonprimary_marker"
    return "unresolved", "no_explicit_heading"


def mechanical_term_form(value):
    value = normalize_extracted_text(value).casefold()
    value = value.replace("’", "'").replace("‘", "'")
    value = re.sub(r"[^\w']+", " ", value, flags=re.UNICODE)
    return compact_whitespace(value)


def term_registration_id_for(lookup_form):
    return f"TERM_REG_{sha256_hex(lookup_form)[:16]}"


def canonical_term_registry_rows(rows):
    return sorted(rows, key=lambda row: (row.get("lookup_form", ""), row.get("term_registration_id", "")))


def term_registry_sha256(rows):
    digest = hashlib.sha256()
    for row in canonical_term_registry_rows(rows):
        digest.update((json_dumps(row) + "\n").encode("utf-8"))
    return digest.hexdigest()


def literal_term_pattern(value, capture_name=None, overlapping=False):
    escaped = re.escape(value)
    body = f"(?P<{capture_name}>{escaped})" if capture_name else escaped
    bounded = rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])"
    if overlapping:
        bounded = f"(?={bounded})"
    return re.compile(bounded, re.IGNORECASE)


def configure_term_registry(rows):
    global REGISTERED_TERM_ROWS
    global REGISTERED_TERM_LOOKUPS
    global LITERAL_TERMS
    global TERM_LOOKUP_FOR_PRINTED
    global TERM_ALTERNATION
    global TERM_RE
    global TERM_REGISTRY_SHA256

    canonical_rows = canonical_term_registry_rows(rows)
    lookup_seen = set()
    printed_to_lookup = {}
    literal_terms = []
    for row in canonical_rows:
        lookup = row.get("lookup_form", "")
        if not lookup or lookup in lookup_seen:
            raise ValueError(f"Invalid or duplicate term-registry lookup form: {lookup!r}")
        lookup_seen.add(lookup)
        if row.get("term_registration_id") != term_registration_id_for(lookup):
            raise ValueError(f"Invalid term-registration ID for {lookup!r}")
        if row.get("normalization_rule") != "case_punctuation_and_spacing_only":
            raise ValueError(f"Invalid term normalization rule for {lookup!r}")
        if row.get("registration_basis") != "literal_printed_form_attested_in_supplied_pdf":
            raise ValueError(f"Invalid term registration basis for {lookup!r}")
        if row.get("semantic_definition") is not None or row.get("semantic_relations") is not None:
            raise ValueError(f"Term registry contains semantic content for {lookup!r}")
        forms = row.get("registered_forms") or []
        if not forms:
            raise ValueError(f"Term registry has no source-attested form for {lookup!r}")
        form_seen = set()
        for form in forms:
            printed = form.get("printed_form", "")
            form_key = printed.casefold()
            if not printed or form_key in form_seen:
                raise ValueError(f"Invalid or duplicate printed form for {lookup!r}")
            if mechanical_term_form(printed) != lookup:
                raise ValueError(f"Printed form does not mechanically normalize to {lookup!r}: {printed!r}")
            if form.get("matching_rule") != "literal_case_insensitive_with_alphanumeric_boundaries":
                raise ValueError(f"Invalid matching rule for {lookup!r}: {printed!r}")
            if not isinstance(form.get("attestation"), dict):
                raise ValueError(f"Missing source attestation for {lookup!r}: {printed!r}")
            previous_lookup = printed_to_lookup.get(form_key)
            if previous_lookup and previous_lookup != lookup:
                raise ValueError(f"Printed form registered to multiple lookup forms: {printed!r}")
            printed_to_lookup[form_key] = lookup
            form_seen.add(form_key)
            literal_terms.append(printed)

    LITERAL_TERMS = sorted(set(literal_terms), key=lambda value: (-len(value), value.casefold(), value))
    TERM_LOOKUP_FOR_PRINTED = printed_to_lookup
    TERM_ALTERNATION = "|".join(re.escape(term) for term in LITERAL_TERMS)
    TERM_RE = (
        re.compile(
            rf"(?=(?<![A-Za-z0-9])(?P<term>{TERM_ALTERNATION})(?![A-Za-z0-9]))",
            re.IGNORECASE,
        )
        if TERM_ALTERNATION
        else re.compile(r"(?!)")
    )
    REGISTERED_TERM_ROWS = canonical_rows
    REGISTERED_TERM_LOOKUPS = lookup_seen
    TERM_REGISTRY_SHA256 = term_registry_sha256(canonical_rows)


configure_term_registry(list(iter_jsonl(TERM_REGISTRY_SOURCE_PATH)))
ABSOLUTE_PRIVATE_PATH_RE = re.compile(
    r"(?:"
    r"(?<![A-Za-z])[A-Za-z]:[/\\](?:[^/\\\r\n]+[/\\])+"
    r"|/(?:Users|home)/[^/\s]+"
    r")",
    re.IGNORECASE,
)


@dataclass
class OutlineEntry:
    order: int
    level: int
    title: str
    page_start: int
    page_end: int
    outline_path: str
    content_role: str
    content_role_basis: str
    work_attributed_author: str
    attribution_id: str
    attribution_basis: str
    mentioned_authors: list[str]


def flatten_outline(items, reader, level=0, rows=None):
    if rows is None:
        rows = []
    for item in items or []:
        if isinstance(item, list):
            flatten_outline(item, reader, level + 1, rows)
            continue
        try:
            title = normalize_extracted_text(item.title)
        except Exception:
            title = normalize_extracted_text(str(item))
        try:
            page = reader.get_destination_page_number(item) + 1
        except Exception:
            page = 1
        rows.append(
            {
                "order": len(rows) + 1,
                "level": level,
                "title": compact_whitespace(title),
                "page_start": max(1, page),
            }
        )
    return rows


def add_outline_paths(rows):
    stack = []
    for row in rows:
        level = row["level"]
        stack = stack[:level]
        while len(stack) < level:
            stack.append("")
        stack.append(row["title"])
        row["outline_path"] = " > ".join(part for part in stack if part)
    return rows


def aliases_for_author(author):
    aliases = set(legacy.AUTHOR_ALIASES.get(author, []))
    aliases.add(author)
    words = author.split()
    if len(words) >= 2:
        aliases.add(f"{words[-1]}, {' '.join(words[:-1])}")
    return sorted((compact_whitespace(a) for a in aliases if a), key=len, reverse=True)


def author_mentions(text):
    results = []
    for author in legacy.AUTHOR_ALIASES:
        for alias in aliases_for_author(author):
            match = re.search(rf"(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])", text, re.IGNORECASE)
            if match:
                results.append((author, match.start(), match.end()))
                break
    return sorted(results, key=lambda item: item[1])


def unique_author_mentions(text):
    return sorted({item[0] for item in author_mentions(text)})


def remove_author_mentions(authors, work_author):
    work_key = compact_whitespace(work_author).casefold()
    return [
        author
        for author in sorted(set(authors), key=str.casefold)
        if author and author != "Unknown" and author.casefold() != work_key
    ]


def explicit_author_from_heading(title):
    clean = compact_whitespace(title)
    mentions = author_mentions(clean)
    authors = sorted({item[0] for item in mentions})
    if len(authors) != 1:
        return ""
    author = authors[0]
    low = clean.casefold().strip(" .:")
    exact_alias = any(low == alias.casefold().strip(" .:") for alias in aliases_for_author(author))
    alias_patterns = [
        re.escape(alias.casefold().strip(" .:"))
        for alias in aliases_for_author(author)
        if alias
    ]
    alias_pattern = "(?:" + "|".join(alias_patterns) + ")"
    byline = bool(re.search(rf"\bby\s+(?:the\s+)?(?:st\.?\s+)?{alias_pattern}\b", low))
    work_genre = (
        r"(?:works|writings|treatises|letters|epistles|homilies|sermons|"
        r"commentaries|fragments|selections|discourses|orations|panegyrics|"
        r"doctrinal treatises|moral treatises)"
    )
    singular_work_genre = r"(?:letter|epistle|homily|sermon|commentary|fragment|discourse|oration|panegyric|treatise)"
    author_prefix = r"(?:(?:st\.?|saint|blessed|our\s+holy\s+father|same)\s+)*"
    title_prefix = r"^(?:(?:the|an?|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|canonical)\s+)*"
    structural = bool(
        re.search(rf"\b{work_genre}\s+(?:of|from|by)\s+(?:st\.?\s+)?{alias_pattern}\b", low)
        or re.search(rf"\bfrom\s+the\s+{work_genre}\s+of\s+(?:st\.?\s+)?{alias_pattern}\b", low)
        or re.search(rf"{title_prefix}{singular_work_genre}\s+(?:of|from|by)\s+{author_prefix}{alias_pattern}\b", low)
    )
    all_caps = clean.isupper() and len(clean) <= 90
    return author if exact_alias or byline or structural or all_caps else ""


def find_author_evidence(author, page_rows, preferred_pages=None):
    preferred_pages = set(preferred_pages or [])
    scored = []
    for page in page_rows[:60]:
        text = page["original_text"]
        for alias in aliases_for_author(author):
            for match in re.finditer(rf"(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])", text, re.IGNORECASE):
                before = text[max(0, match.start() - 80):match.start()].casefold()
                score = 0
                if page["page_number"] in preferred_pages:
                    score += 8
                if re.search(r"\b(by|author\(s\):|written(?: in latin)? by)\b", before):
                    score += 12
                if page["page_number"] <= 15:
                    score += 4
                excerpt, left, right = context_excerpt(text, match.start(), match.end(), 150)
                scored.append((score, page["page_number"], match.start(), excerpt, left, right))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    score, page_number, start, excerpt, left, right = scored[0]
    if score < 4:
        return None
    return {
        "evidence_text": excerpt,
        "evidence_page": page_number,
        "evidence_char_start": left,
        "evidence_char_end": right,
        "evidence_status": "explicit_printed_name",
    }


def title_page_numbers(raw_outline):
    return [
        row["page_start"]
        for row in raw_outline
        if "title page" in row["title"].casefold()
    ]


def source_author_attribution(path, reader, raw_outline, page_rows):
    preferred = title_page_numbers(raw_outline)
    folder_candidate = legacy.author_from_folder(path)
    if folder_candidate:
        evidence = find_author_evidence(folder_candidate, page_rows, preferred)
        return folder_candidate, "folder_author_name", evidence

    title_material = compact_whitespace(getattr(reader.metadata, "title", "") or "")
    for page_number in preferred[:2]:
        if 1 <= page_number <= len(page_rows):
            title_material += " " + page_rows[page_number - 1]["original_text"]
    mentioned = sorted({item[0] for item in author_mentions(title_material)})
    if len(mentioned) == 1:
        evidence = find_author_evidence(mentioned[0], page_rows, preferred)
        if evidence:
            return mentioned[0], "explicit_source_title_scope", evidence
    return "Unknown", "no_single_explicit_source_author", None


def load_author_attribution_reviews(path=AUTHOR_REVIEW_PATH):
    reviews = {}
    if not path.exists():
        return reviews
    for row in legacy.iter_jsonl(path):
        required = ("review_id", "source_id", "outline_path", "review_status", "final_author")
        missing = [key for key in required if not row.get(key)]
        if missing:
            raise ValueError(f"Author review {row.get('review_id', '<unknown>')} is missing {missing}")
        key = (row["source_id"], row["outline_path"])
        if key in reviews:
            raise ValueError(f"Duplicate author review target: {key}")
        if row["final_author"] != "Unknown" and row["final_author"] not in legacy.AUTHOR_ALIASES:
            raise ValueError(f"Author review names unregistered author: {row['final_author']}")
        reviews[key] = row
    return reviews


AUTHOR_ATTRIBUTION_REVIEWS = load_author_attribution_reviews()


def build_outline_entries(
    source_id,
    raw_outline,
    page_count,
    source_author,
    source_attr_id,
    source_author_basis,
):
    rows = add_outline_paths(raw_outline)
    author_scopes = {}
    attribution_counter = 0
    attribution_rows = []
    prepared = []

    for row in rows:
        level = row["level"]
        author_scopes = {key: value for key, value in author_scopes.items() if key < level}
        heading_author = explicit_author_from_heading(row["title"])
        heading_mentions = unique_author_mentions(row["title"])
        review = AUTHOR_ATTRIBUTION_REVIEWS.get((source_id, row["outline_path"]))
        review_basis = None
        if review and review.get("review_status") == "accepted":
            expected = review.get("deterministic_author")
            if expected and heading_author and expected != heading_author:
                raise ValueError(
                    f"Author review {review['review_id']} expected {expected!r}, got {heading_author!r}"
                )
            heading_author = None
            if review.get("suppress_mentioned_authors", False):
                heading_mentions = []
            if review.get("final_author") != "Unknown":
                author_scopes[level] = (
                    review["final_author"],
                    review.get("target_attribution_id", ""),
                    row["title"],
                    row["page_start"],
                )
                review_basis = review.get("target_basis", "ai_reviewed_attribution")
        if heading_author:
            attribution_counter += 1
            attribution_id = f"{source_id}_ATTR_{attribution_counter:05d}"
            author_scopes[level] = (heading_author, attribution_id, row["title"], row["page_start"])
            attribution_rows.append(
                {
                    "attribution_id": attribution_id,
                    "source_id": source_id,
                    "attributed_author": heading_author,
                    "author_role": "work_attribution",
                    "basis": "explicit_outline_heading_scope",
                    "evidence_text": row["title"],
                    "evidence_page": row["page_start"],
                    "outline_path": row["outline_path"],
                    "review_status": "mechanically_verified_exact_heading",
                }
            )
        else:
            for mentioned_author in heading_mentions:
                attribution_counter += 1
                attribution_id = f"{source_id}_ATTR_{attribution_counter:05d}"
                attribution_rows.append(
                    {
                        "attribution_id": attribution_id,
                        "source_id": source_id,
                        "attributed_author": mentioned_author,
                        "author_role": "mentioned_author",
                        "basis": "explicit_outline_heading_reference",
                        "evidence_text": row["title"],
                        "evidence_page": row["page_start"],
                        "outline_path": row["outline_path"],
                        "review_status": "mechanically_verified_exact_heading",
                    }
                )
        if author_scopes:
            deepest = max(author_scopes)
            author, attr_id, _, _ = author_scopes[deepest]
            basis = review_basis or "explicit_outline_heading_scope"
        elif source_author != "Unknown":
            author = source_author
            attr_id = source_attr_id
            basis = source_author_basis
        else:
            author = "Unknown"
            attr_id = ""
            basis = "unknown"
        role, role_basis = content_role_for_heading(row["title"], row["outline_path"])
        prepared.append(
            {
                **row,
                "author": author,
                "attribution_id": attr_id,
                "attribution_basis": basis,
                "mentioned_authors": remove_author_mentions(heading_mentions, author),
                "content_role": role,
                "content_role_basis": role_basis,
            }
        )

    for index, row in enumerate(prepared):
        end = page_count
        for later in prepared[index + 1:]:
            if later["page_start"] > row["page_start"]:
                end = later["page_start"] - 1
                break
        row["page_end"] = max(row["page_start"], min(end, page_count))

    entries = [
        OutlineEntry(
            order=row["order"],
            level=row["level"],
            title=row["title"],
            page_start=row["page_start"],
            page_end=row["page_end"],
            outline_path=row["outline_path"],
            content_role=row["content_role"],
            content_role_basis=row["content_role_basis"],
            work_attributed_author=row["author"],
            attribution_id=row["attribution_id"],
            attribution_basis=row["attribution_basis"],
            mentioned_authors=row["mentioned_authors"],
        )
        for row in prepared
    ]
    return entries, attribution_rows


def active_outline_entries(entries, page_count):
    ordered = sorted(entries, key=lambda row: (row.page_start, row.order))
    active = None
    index = 0
    result = {}
    for page_number in range(1, page_count + 1):
        while index < len(ordered) and ordered[index].page_start <= page_number:
            active = ordered[index]
            index += 1
        result[page_number] = active
    return result


def evidence_chunks(text):
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + MAX_EVIDENCE_CHARS)
        if end < len(text):
            lower_bound = start + int(MAX_EVIDENCE_CHARS * 0.65)
            candidates = [text.rfind("\n", lower_bound, end), text.rfind(" ", lower_bound, end)]
            boundary = max(candidates)
            if boundary > start:
                end = boundary + 1
        chunks.append((start, end, text[start:end]))
        start = end
    return chunks


def evidence_layer(author, content_role, text_role=TEXT_ROLE_BODY):
    if text_role != TEXT_ROLE_BODY:
        return "archival"
    if content_role in {"reference_material", "editorial_material"}:
        return "archival"
    if author == "Unknown" or content_role == "unresolved":
        return "extended"
    return "primary"


def evidence_id_for_offset(evidence_rows, page_number, offset):
    candidates = evidence_rows.get(page_number, [])
    for row in candidates:
        if row["char_start"] <= offset < row["char_end"]:
            return row["evidence_id"]
    return candidates[-1]["evidence_id"] if candidates else ""


def evidence_text(row):
    return row.get("reader_text") or row.get("verbatim_text") or ""


def page_for_evidence_offset(evidence, offset):
    for span in evidence.get("source_spans") or []:
        start = span.get("reader_char_start")
        end = span.get("reader_char_end")
        if isinstance(start, int) and isinstance(end, int) and start <= offset < end:
            return span.get("page_number") or evidence.get("page_number")
    return evidence.get("page_number")


def source_span_for_segment(segment, reader_start, reader_end, page_start=0):
    return {
        "page_id": segment["page_id"],
        "page_number": segment["page_number"],
        "text_role": segment["text_role"],
        "block_ids": segment.get("block_ids", []),
        "line_start": segment.get("line_start"),
        "line_end": segment.get("line_end"),
        "reader_char_start": reader_start,
        "reader_char_end": reader_end,
        "page_reader_char_start": page_start,
        "page_reader_char_end": page_start + max(0, reader_end - reader_start),
        "raw_page_text_sha256": segment.get("raw_page_text_sha256"),
    }


def evidence_row_from_parts(
    source_id,
    chunk_number,
    group,
    text,
    spans,
    footnote_refs,
    cleaning_operations,
    classification_reasons=None,
    classification_confidence=None,
):
    first_page = spans[0]["page_number"] if spans else group["page_number"]
    page_end = spans[-1]["page_number"] if spans else first_page
    text_role = group["text_role"]
    role_index = group["role_index"]
    evidence_id = f"{source_id}_EVID_{first_page:05d}_{text_role.upper()}_{role_index:02d}"
    raw_hashes = [span.get("raw_page_text_sha256") for span in spans if span.get("raw_page_text_sha256")]
    printed_page_numbers = group.get("printed_page_numbers") or []
    mentioned_authors = remove_author_mentions(
        list(group.get("mentioned_authors") or []) + unique_author_mentions(text),
        group["work_attributed_author"],
    )
    return {
        "evidence_id": evidence_id,
        "source_id": source_id,
        "page_id": group["page_id"],
        "page_number": first_page,
        "page_start": first_page,
        "page_end": page_end,
        "chunk_number": chunk_number,
        "char_start": 0,
        "char_end": len(text),
        "verbatim_text": text,
        "reader_text": text,
        "text_role": text_role,
        "source_spans": spans,
        "footnote_refs": footnote_refs,
        "printed_page_number": printed_page_numbers[0] if printed_page_numbers else None,
        "printed_page_numbers": printed_page_numbers,
        "raw_page_text_sha256": raw_hashes[0] if raw_hashes else None,
        "raw_page_text_sha256s": raw_hashes,
        "cleaning_operations": sorted(set(cleaning_operations)),
        "classification_confidence": classification_confidence,
        "classification_reasons": sorted(set(classification_reasons or [])),
        "text_status": (
            "reader_clean_pdfplumber_body_text"
            if text_role == TEXT_ROLE_BODY
            else "reader_clean_pdfplumber_footnote_text"
        ),
        "work_attributed_author": group["work_attributed_author"],
        "mentioned_authors": mentioned_authors,
        "attribution_id": group["attribution_id"],
        "attribution_basis": group["attribution_basis"],
        "speaker_claim": None,
        "active_heading": group["active_heading"],
        "outline_path": group["outline_path"],
        "content_role": group["content_role"],
        "content_role_basis": group["content_role_basis"],
        "retrieval_layer": evidence_layer(group["work_attributed_author"], group["content_role"], text_role),
        "interpretive_summary": None,
    }


def split_segment_evidence(source_id, chunk_number_start, role_counter, segment):
    rows = []
    for start, end, chunk_text in evidence_chunks(segment["text"]):
        if not chunk_text.strip():
            continue
        role_counter[(segment["page_number"], segment["text_role"])] += 1
        group = {
            **segment,
            "role_index": role_counter[(segment["page_number"], segment["text_role"])],
            "printed_page_numbers": [segment.get("printed_page_number")] if segment.get("printed_page_number") else [],
        }
        rows.append(
            evidence_row_from_parts(
                source_id,
                chunk_number_start + len(rows),
                group,
                chunk_text,
                [source_span_for_segment(segment, 0, len(chunk_text), start)],
                segment.get("footnote_refs", []),
                segment.get("cleaning_operations", []),
                segment.get("classification_reasons", []),
                segment.get("classification_confidence"),
            )
        )
    return rows


def segment_group_key(segment):
    if segment["text_role"] != TEXT_ROLE_BODY:
        return None
    return (
        segment["text_role"],
        segment["work_attributed_author"],
        segment["attribution_id"],
        segment["attribution_basis"],
        tuple(segment.get("mentioned_authors") or []),
        segment["active_heading"],
        segment["outline_path"],
        segment["content_role"],
        segment["content_role_basis"],
    )


def build_clean_evidence_rows(source_id, segments):
    evidence = []
    role_counter = defaultdict(int)
    current = None

    def flush_current():
        nonlocal current
        if not current or not current["text"].strip():
            current = None
            return
        first_page = current["spans"][0]["page_number"]
        text_role = current["group"]["text_role"]
        role_counter[(first_page, text_role)] += 1
        group = {
            **current["group"],
            "role_index": role_counter[(first_page, text_role)],
            "printed_page_numbers": current["printed_page_numbers"],
        }
        evidence.append(
            evidence_row_from_parts(
                source_id,
                len(evidence) + 1,
                group,
                current["text"],
                current["spans"],
                current["footnote_refs"],
                current["cleaning_operations"],
                current["classification_reasons"],
                (
                    round(min(current["classification_confidences"]), 2)
                    if current["classification_confidences"]
                    else None
                ),
            )
        )
        current = None

    for segment in segments:
        if not segment["text"].strip():
            continue
        key = segment_group_key(segment)
        if key is None or len(segment["text"]) > MAX_EVIDENCE_CHARS:
            flush_current()
            evidence.extend(split_segment_evidence(source_id, len(evidence) + 1, role_counter, segment))
            continue
        if current and (current["key"] != key or len(current["text"]) + len(segment["text"]) + 2 > MAX_EVIDENCE_CHARS):
            flush_current()
        if not current:
            current = {
                "key": key,
                "group": segment,
                "text": segment["text"],
                "spans": [source_span_for_segment(segment, 0, len(segment["text"]))],
                "footnote_refs": list(segment.get("footnote_refs", [])),
                "cleaning_operations": list(segment.get("cleaning_operations", [])),
                "classification_reasons": list(segment.get("classification_reasons", [])),
                "classification_confidences": (
                    [segment["classification_confidence"]]
                    if isinstance(segment.get("classification_confidence"), (int, float))
                    else []
                ),
                "printed_page_numbers": [segment.get("printed_page_number")] if segment.get("printed_page_number") else [],
            }
            continue
        separator = "\n\n"
        start = len(current["text"]) + len(separator)
        current["text"] += separator + segment["text"]
        current["spans"].append(source_span_for_segment(segment, start, start + len(segment["text"])))
        current["footnote_refs"].extend(segment.get("footnote_refs", []))
        current["cleaning_operations"].extend(segment.get("cleaning_operations", []))
        current["classification_reasons"].extend(segment.get("classification_reasons", []))
        if isinstance(segment.get("classification_confidence"), (int, float)):
            current["classification_confidences"].append(segment["classification_confidence"])
        if segment.get("printed_page_number"):
            current["printed_page_numbers"].append(segment["printed_page_number"])

    flush_current()
    return evidence


def extract_term_rows(source_id, evidence_rows):
    rows = []
    for evidence in evidence_rows:
        text = evidence_text(evidence)
        if not text:
            continue
        grouped = defaultdict(list)
        for match in TERM_RE.finditer(text):
            printed = match.group("term")
            start = match.start("term")
            end = match.end("term")
            lookup = mechanical_term_form(printed)
            grouped[lookup].append((start, end, printed, page_for_evidence_offset(evidence, start)))
        for lookup, matches in sorted(grouped.items()):
            first_start, first_end, _, first_page = matches[0]
            excerpt, excerpt_start, excerpt_end = context_excerpt(text, first_start, first_end)
            row_id = sha256_hex(f"{source_id}|{evidence['evidence_id']}|{lookup}")[:16]
            rows.append(
                {
                    "term_occurrence_id": f"TERM_OCC_{row_id}",
                    "source_id": source_id,
                    "page_number": first_page,
                    "evidence_id": evidence["evidence_id"],
                    "text_role": evidence.get("text_role", TEXT_ROLE_BODY),
                    "printed_term": matches[0][2],
                    "lookup_form": lookup,
                    "normalization_rule": "case_punctuation_and_spacing_only",
                    "occurrence_count": len(matches),
                    "matches": [
                        {
                            "char_start": start,
                            "char_end": end,
                            "printed_text": printed,
                            "page_number": page_number,
                        }
                        for start, end, printed, page_number in matches
                    ],
                    "evidence_text": excerpt,
                    "evidence_char_start": excerpt_start,
                    "evidence_char_end": excerpt_end,
                    "semantic_assertion": None,
                }
            )
    return rows


def extract_scripture_rows(source_id, evidence_rows):
    rows = []
    for evidence in evidence_rows:
        text = evidence_text(evidence)
        if not text:
            continue
        for match in legacy.SCRIPTURE_RE.finditer(text):
            printed = text[match.start():match.end()]
            alias = match.group(1).casefold().replace(".", "")
            book = legacy.BOOK_ALIAS_TO_CANON.get(alias, "")
            chapter = legacy.roman_to_int(match.group(2))
            verse_start = int(match.group(3))
            verse_end = int(match.group(4)) if match.group(4) else None
            valid = bool(chapter and legacy.valid_scripture_ref(book, chapter, verse_start, verse_end))
            canonical = ""
            if valid:
                canonical = f"{book} {chapter}:{verse_start}"
                if verse_end is not None:
                    canonical += f"-{verse_end}"
            excerpt, excerpt_start, excerpt_end = context_excerpt(text, match.start(), match.end())
            page_number = page_for_evidence_offset(evidence, match.start())
            row_id = sha256_hex(f"{source_id}|{evidence['evidence_id']}|{match.start()}|{printed}")[:16]
            rows.append(
                {
                    "scripture_occurrence_id": f"SCRIPTURE_OCC_{row_id}",
                    "source_id": source_id,
                    "page_number": page_number,
                    "evidence_id": evidence["evidence_id"],
                    "text_role": evidence.get("text_role", TEXT_ROLE_BODY),
                    "printed_reference": printed,
                    "canonical_reference": canonical or None,
                    "normalization_status": "book_chapter_and_plausible_verse_validated" if valid else "unresolved",
                    "reference_type": "explicit_printed_reference",
                    "char_start": match.start(),
                    "char_end": match.end(),
                    "evidence_text": excerpt,
                    "evidence_char_start": excerpt_start,
                    "evidence_char_end": excerpt_end,
                    "allusion_inference": None,
                }
            )
    return rows


def source_title(reader, path):
    metadata_title = compact_whitespace(getattr(reader.metadata, "title", "") or "")
    if metadata_title:
        return metadata_title, "supplied_pdf_embedded_metadata"
    return path.stem, "portable_filename_catalog_hint"


def package_paths(source_id):
    base = CACHE_DIR / source_id
    return {
        "base": base,
        "metadata": base / "metadata.json",
        "source": base / "source.json",
        "pages": base / "pages.jsonl",
        "page_blocks": base / "page_blocks.jsonl",
        "sections": base / "sections.jsonl",
        "evidence": base / "evidence.jsonl",
        "terms": base / "term_occurrences.jsonl",
        "scripture": base / "scripture_occurrences.jsonl",
        "attributions": base / "attributions.jsonl",
        "validation": base / "validation.json",
    }


def package_reusable(source_id, signature):
    paths = package_paths(source_id)
    metadata = read_json(paths["metadata"], {})
    validation = read_json(paths["validation"], {})
    required = [
        paths[key]
        for key in (
            "source",
            "pages",
            "page_blocks",
            "sections",
            "evidence",
            "terms",
            "scripture",
            "attributions",
        )
    ]
    return (
        metadata.get("builder_version") == BUILDER_VERSION
        and metadata.get("schema_version") == SCHEMA_VERSION
        and metadata.get("term_registry_sha256") == TERM_REGISTRY_SHA256
        and metadata.get("signature") == signature
        and validation.get("status") == "pass"
        and validation.get("term_registry_sha256") == TERM_REGISTRY_SHA256
        and all(path.exists() for path in required)
    )


def validate_package(package):
    errors = []
    warnings = []
    source = package["source"]
    pages = package["pages"]
    page_blocks = package.get("page_blocks", [])
    evidence = package["evidence"]
    terms = package["terms"]
    scripture = package["scripture"]
    attributions = package["attributions"]

    page_map = {row["page_number"]: row for row in pages}
    block_ids = set()
    blocks_by_page = defaultdict(list)
    for block in page_blocks:
        block_id = block.get("block_id")
        if not block_id or block_id in block_ids:
            errors.append(f"duplicate_or_empty_page_block:{block_id}")
        block_ids.add(block_id)
        if block.get("text_role") not in TEXT_ROLES:
            errors.append(f"invalid_page_block_role:{block_id}")
        page = page_map.get(block.get("page_number"))
        if not page:
            errors.append(f"missing_page_for_block:{block_id}")
        else:
            blocks_by_page[block["page_number"]].append(block)

    evidence_ids = set()
    evidence_by_id = {}
    for row in evidence:
        evidence_id = row.get("evidence_id")
        if not evidence_id or evidence_id in evidence_ids:
            errors.append(f"duplicate_or_empty_evidence:{evidence_id}")
        evidence_ids.add(evidence_id)
        evidence_by_id[evidence_id] = row
        text = evidence_text(row)
        if row.get("reader_text") != text:
            errors.append(f"reader_text_mismatch:{evidence_id}")
        if row.get("verbatim_text") != text:
            errors.append(f"verbatim_text_mismatch:{evidence_id}")
        if row.get("char_start") != 0 or row.get("char_end") != len(text):
            errors.append(f"evidence_clean_offsets_invalid:{evidence_id}")
        if row.get("text_role") not in {TEXT_ROLE_BODY, TEXT_ROLE_FOOTNOTE}:
            errors.append(f"evidence_invalid_text_role:{evidence_id}")
        page = page_map.get(row.get("page_number"))
        if not page:
            errors.append(f"missing_page_for_evidence:{evidence_id}")
            continue
        spans = row.get("source_spans") or []
        if not spans:
            errors.append(f"evidence_missing_source_spans:{evidence_id}")
        for span in spans:
            span_page = page_map.get(span.get("page_number"))
            if not span_page:
                errors.append(f"evidence_span_missing_page:{evidence_id}")
            if span.get("text_role") != row.get("text_role"):
                errors.append(f"evidence_span_role_mismatch:{evidence_id}")
            start = span.get("reader_char_start")
            end = span.get("reader_char_end")
            if not all(isinstance(value, int) for value in (start, end)) or not (0 <= start <= end <= len(text)):
                errors.append(f"evidence_span_offsets_invalid:{evidence_id}")
            if span_page and span.get("raw_page_text_sha256") != span_page.get("original_text_sha256"):
                errors.append(f"evidence_span_raw_hash_mismatch:{evidence_id}")

    for page in pages:
        role_counts = page.get("layout_role_counts") or {}
        if any(role not in TEXT_ROLES for role in role_counts):
            errors.append(f"invalid_page_role_count:{page['page_id']}")
        if page.get("original_text", "").strip() and not blocks_by_page.get(page["page_number"]):
            errors.append(f"text_page_without_layout_blocks:page_{page['page_number']}")
        if page.get("layout_unclassified_blocks", 0):
            errors.append(f"unclassified_layout_blocks:page_{page['page_number']}")

    if not REGISTERED_TERM_ROWS:
        errors.append("term_registry_empty")
    for row in terms:
        evidence_row = evidence_by_id.get(row["evidence_id"])
        if not evidence_row:
            errors.append(f"term_missing_evidence:{row['term_occurrence_id']}")
        if row.get("lookup_form") not in REGISTERED_TERM_LOOKUPS:
            errors.append(f"term_not_registered:{row['term_occurrence_id']}")
        if row.get("normalization_rule") != "case_punctuation_and_spacing_only":
            errors.append(f"term_normalization_rule_invalid:{row['term_occurrence_id']}")
        if row.get("semantic_assertion") is not None:
            errors.append(f"term_contains_semantic_assertion:{row['term_occurrence_id']}")
        if evidence_row:
            text = evidence_text(evidence_row)
            for match in row["matches"]:
                printed = match["printed_text"]
                if text[match["char_start"]:match["char_end"]] != printed:
                    errors.append(f"term_not_exact:{row['term_occurrence_id']}")
                    break
                if mechanical_term_form(printed) != row.get("lookup_form"):
                    errors.append(f"term_lookup_not_mechanical:{row['term_occurrence_id']}")
                    break
                if TERM_LOOKUP_FOR_PRINTED.get(printed.casefold()) != row.get("lookup_form"):
                    errors.append(f"term_form_not_registered:{row['term_occurrence_id']}")
                    break

    for row in scripture:
        evidence_row = evidence_by_id.get(row["evidence_id"])
        if not evidence_row:
            errors.append(f"scripture_missing_evidence:{row['scripture_occurrence_id']}")
        if evidence_row and evidence_text(evidence_row)[row["char_start"]:row["char_end"]] != row["printed_reference"]:
            errors.append(f"scripture_not_exact:{row['scripture_occurrence_id']}")

    for row in attributions:
        page = page_map.get(row.get("evidence_page", 0))
        if (
            page
            and row["evidence_text"] not in page["original_text"]
            and row["basis"] not in {"explicit_outline_heading_scope", "explicit_outline_heading_reference"}
        ):
            errors.append(f"attribution_not_exact:{row['attribution_id']}")

    quality_counts = Counter(row["extraction_quality_status"] for row in pages)
    warnings.extend(
        f"{status}:{count}"
        for status, count in sorted(quality_counts.items())
        if status != "extracted"
    )
    if len(pages) != source["page_count"]:
        errors.append("page_count_mismatch")

    return {
        "source_id": source["source_id"],
        "schema_version": SCHEMA_VERSION,
        "builder_version": BUILDER_VERSION,
        "term_registry_sha256": TERM_REGISTRY_SHA256,
        "validated_at": now_iso(),
        "status": "pass" if not errors else "fail",
        "errors": errors,
        "warnings": warnings,
        "counts": {
            "pages": len(pages),
            "page_blocks": len(page_blocks),
            "sections": len(package["sections"]),
            "evidence": len(evidence),
            "term_occurrence_rows": len(terms),
            "scripture_occurrences": len(scripture),
            "attributions": len(attributions),
        },
    }


def process_pdf(path, pdf_sha256=None):
    source_id = legacy.source_id_for(path)
    reader = PdfReader(path)
    raw_outline = flatten_outline(reader.outline, reader)
    page_rows = []
    page_blocks = []
    extraction_errors = {}
    layout_errors = {}
    try:
        plumber_pdf = pdfplumber.open(path)
        layout_context = build_layout_context(plumber_pdf)
        layout_errors.update(layout_context.get("layout_errors", {}))
    except Exception as exc:
        plumber_pdf = None
        layout_context = {}
        layout_errors[0] = f"pdfplumber_open_error:{type(exc).__name__}"
    for page_number, page in enumerate(reader.pages, start=1):
        extracted = safe_extract_page(page)
        if isinstance(extracted, tuple):
            original_text, error = extracted
            extraction_errors[page_number] = error
        else:
            original_text = extracted
        cleaned_text = normalize_extracted_text(original_text)
        flags = []
        replacement_count = original_text.count("\ufffd") + original_text.count("�")
        if replacement_count:
            flags.append(f"replacement_characters:{replacement_count}")
        if page_number in extraction_errors:
            flags.append(extraction_errors[page_number])
        page_id = f"{source_id}_PAGE_{page_number:05d}"
        if plumber_pdf and page_number <= len(plumber_pdf.pages):
            blocks, block_errors = layout_blocks_for_page(
                source_id,
                page_id,
                page_number,
                plumber_pdf.pages[page_number - 1],
                (layout_context.get("layout_pages") or {}).get(page_number),
                layout_context,
            )
            if block_errors:
                layout_errors[page_number] = ";".join(block_errors)
        else:
            blocks = []
            layout_errors[page_number] = layout_errors.get(0, "pdfplumber_page_missing")
        if layout_errors.get(page_number) and cleaned_text.strip():
            blocks = [
                {
                    "block_number": 1,
                    "block_id": f"{source_id}_PAGE_{page_number:05d}_BLOCK_01",
                    "source_id": source_id,
                    "page_id": page_id,
                    "page_number": page_number,
                    "text_role": TEXT_ROLE_BODY,
                    "text": cleaned_text,
                    "line_start": None,
                    "line_end": None,
                    "bbox": None,
                    "font_size_min": None,
                    "font_size_max": None,
                    "font_size_median": None,
                    "font_names": [],
                    "color_values": [],
                    "has_colored_text": False,
                    "classification_confidence": None,
                    "classification_reasons": ["fallback_after_layout_error"],
                    "cleaning_operations": ["fallback_pypdf_text_after_layout_error"],
                }
            ]
        if layout_errors.get(page_number):
            flags.append(layout_errors[page_number])
        page_blocks.extend(blocks)
        role_counts = Counter(block["text_role"] for block in blocks)
        reader_text = page_text_from_blocks(blocks, TEXT_ROLE_BODY)
        footnote_text = page_text_from_blocks(blocks, TEXT_ROLE_FOOTNOTE)
        auxiliary_text = "\n".join(
            block["text"]
            for block in blocks
            if block.get("text_role") not in {TEXT_ROLE_BODY, TEXT_ROLE_FOOTNOTE}
            and block.get("text")
        )
        printed_page_number = next(
            (
                block["text"]
                for block in blocks
                if block.get("text_role") == TEXT_ROLE_PRINTED_PAGE_NUMBER and block.get("text")
            ),
            None,
        )
        block_cleaning_operations = sorted(
            {
                operation
                for block in blocks
                for operation in block.get("cleaning_operations", [])
            }
        )
        has_images = page_has_images(page)
        if not original_text.strip():
            status = "no_text_with_images" if has_images else "no_extracted_text"
        elif flags:
            status = "review_recommended"
        else:
            status = "extracted"
        page_rows.append(
            {
                "page_id": page_id,
                "source_id": source_id,
                "page_number": page_number,
                "extraction_method": "pypdf_embedded_text+pdfplumber_layout",
                "extraction_quality_status": status,
                "ocr_used": False,
                "has_images": has_images,
                "original_text": original_text,
                "cleaned_text": cleaned_text,
                "reader_text": reader_text,
                "footnote_text": footnote_text,
                "auxiliary_text_sha256": sha256_hex(auxiliary_text),
                "printed_page_number": printed_page_number,
                "layout_role_counts": dict(sorted(role_counts.items())),
                "layout_block_count": len(blocks),
                "layout_unclassified_blocks": 0,
                "cleaning_operations": (
                    ["remove_null_bytes", "normalize_line_endings"]
                    if cleaned_text != original_text
                    else []
                )
                + block_cleaning_operations,
                "original_text_sha256": sha256_hex(original_text),
                "cleaned_text_sha256": sha256_hex(cleaned_text),
                "reader_text_sha256": sha256_hex(reader_text),
                "footnote_text_sha256": sha256_hex(footnote_text),
                "character_count": len(original_text),
                "word_count": word_count(original_text),
                "reader_word_count": word_count(reader_text),
                "footnote_word_count": word_count(footnote_text),
                "review_flags": flags,
            }
        )
    if plumber_pdf:
        plumber_pdf.close()

    source_author, source_author_basis, source_author_evidence = source_author_attribution(path, reader, raw_outline, page_rows)
    attributions = []
    source_attr_id = ""
    if source_author_evidence:
        source_attr_id = f"{source_id}_ATTR_00000"
        attributions.append(
            {
                "attribution_id": source_attr_id,
                "source_id": source_id,
                "attributed_author": source_author,
                "author_role": "work_attribution",
                "basis": source_author_basis,
                **source_author_evidence,
                "outline_path": "",
                "review_status": "mechanically_verified_exact_text",
            }
        )

    outline_entries, outline_attributions = build_outline_entries(
        source_id,
        raw_outline,
        len(page_rows),
        source_author,
        source_attr_id,
        source_author_basis,
    )
    attributions.extend(outline_attributions)
    active_by_page = active_outline_entries(outline_entries, len(page_rows))

    segments = []
    blocks_by_page = defaultdict(list)
    for block in page_blocks:
        blocks_by_page[block["page_number"]].append(block)
    for page in page_rows:
        active = active_by_page[page["page_number"]]
        if active:
            author = active.work_attributed_author
            attribution_id = active.attribution_id
            attribution_basis = active.attribution_basis
            heading = active.title
            outline_path = active.outline_path
            role = active.content_role
            role_basis = active.content_role_basis
            mentioned_authors = active.mentioned_authors
        else:
            author = source_author
            attribution_id = source_attr_id
            attribution_basis = source_author_basis if source_author != "Unknown" else "unknown"
            heading = ""
            outline_path = ""
            role, role_basis = content_role_for_heading("", "")
            mentioned_authors = []
        page.update(
            {
                "work_attributed_author": author,
                "mentioned_authors": mentioned_authors,
                "attribution_id": attribution_id,
                "attribution_basis": attribution_basis,
                "active_heading": heading,
                "outline_path": outline_path,
                "content_role": role,
                "content_role_basis": role_basis,
            }
        )
        page_footnote_refs = parse_footnote_refs(page.get("footnote_text", ""))
        page["footnote_refs"] = page_footnote_refs
        for text_role, text_field in (
            (TEXT_ROLE_BODY, "reader_text"),
            (TEXT_ROLE_FOOTNOTE, "footnote_text"),
        ):
            text = page.get(text_field, "")
            if not text.strip():
                continue
            role_blocks = [
                block for block in blocks_by_page.get(page["page_number"], [])
                if block.get("text_role") == text_role
            ]
            segments.append(
                {
                    "source_id": source_id,
                    "page_id": page["page_id"],
                    "page_number": page["page_number"],
                    "text_role": text_role,
                    "text": text,
                    "block_ids": [block["block_id"] for block in role_blocks],
                    "line_start": min((block["line_start"] for block in role_blocks if block.get("line_start") is not None), default=None),
                    "line_end": max((block["line_end"] for block in role_blocks if block.get("line_end") is not None), default=None),
                    "raw_page_text_sha256": page["original_text_sha256"],
                    "printed_page_number": page.get("printed_page_number"),
                    "footnote_refs": page_footnote_refs if text_role == TEXT_ROLE_BODY else [],
                    "cleaning_operations": [
                        operation
                        for block in role_blocks
                        for operation in block.get("cleaning_operations", [])
                    ],
                    "classification_confidence": (
                        round(
                            min(
                                block["classification_confidence"]
                                for block in role_blocks
                                if isinstance(block.get("classification_confidence"), (int, float))
                            ),
                            2,
                        )
                        if any(isinstance(block.get("classification_confidence"), (int, float)) for block in role_blocks)
                        else None
                    ),
                    "classification_reasons": sorted(
                        {
                            reason
                            for block in role_blocks
                            for reason in block.get("classification_reasons", [])
                        }
                    ),
                    "work_attributed_author": author,
                    "mentioned_authors": mentioned_authors,
                    "attribution_id": attribution_id,
                    "attribution_basis": attribution_basis,
                    "active_heading": heading,
                    "outline_path": outline_path,
                    "content_role": role,
                    "content_role_basis": role_basis,
                }
            )

    evidence = build_clean_evidence_rows(source_id, segments)

    sections = [
        {
            "section_id": f"{source_id}_SEC_{entry.order:05d}",
            "source_id": source_id,
            "title": entry.title,
            "title_basis": "supplied_pdf_outline",
            "outline_path": entry.outline_path,
            "outline_level": entry.level,
            "page_start": entry.page_start,
            "page_end": entry.page_end,
            "work_attributed_author": entry.work_attributed_author,
            "mentioned_authors": entry.mentioned_authors,
            "attribution_id": entry.attribution_id,
            "attribution_basis": entry.attribution_basis,
            "content_role": entry.content_role,
            "content_role_basis": entry.content_role_basis,
            "interpretive_summary": None,
        }
        for entry in outline_entries
    ]
    terms = extract_term_rows(source_id, evidence)
    scripture = extract_scripture_rows(source_id, evidence)
    title, title_basis = source_title(reader, path)
    quality_counts = Counter(row["extraction_quality_status"] for row in page_rows)
    authors = sorted({row["work_attributed_author"] for row in evidence if row["work_attributed_author"] != "Unknown"})
    mentioned_authors = sorted(
        {
            author
            for row in evidence
            for author in row.get("mentioned_authors", [])
            if author and author != "Unknown"
        },
        key=str.casefold,
    )
    warning_pages = [row["page_number"] for row in page_rows if row["extraction_quality_status"] != "extracted"]
    first_text_page = next((row["page_number"] for row in page_rows if row["original_text"].strip()), 1)
    visual_samples = sorted(set((title_page_numbers(raw_outline)[:1] or [first_text_page]) + [first_text_page] + warning_pages[:1]))
    source = {
        "source_id": source_id,
        "schema_version": SCHEMA_VERSION,
        "collection": legacy.infer_collection(path),
        "pdf_file": portable_path(path),
        "display_title": title,
        "title_basis": title_basis,
        "catalog_folder_hint": path.parent.name,
        "source_author": source_author,
        "source_author_basis": source_author_basis,
        "source_attribution_id": source_attr_id,
        "authors_with_explicit_attribution": authors,
        "authors_with_explicit_reference": mentioned_authors,
        "page_count": len(page_rows),
        "pdf_sha256": pdf_sha256 or sha256_file(path),
        "size_bytes": path.stat().st_size,
        "extraction_engine": "pypdf+pdfplumber",
        "extracted_text_pages": sum(1 for row in page_rows if row["original_text"].strip()),
        "extracted_word_count": sum(row["word_count"] for row in page_rows),
        "extracted_character_count": sum(row["character_count"] for row in page_rows),
        "reader_text_pages": sum(1 for row in page_rows if row["reader_text"].strip()),
        "reader_word_count": sum(row["reader_word_count"] for row in page_rows),
        "footnote_word_count": sum(row["footnote_word_count"] for row in page_rows),
        "extraction_quality_counts": dict(sorted(quality_counts.items())),
        "visual_sample_pages": visual_samples,
        "external_knowledge_used": False,
        "interpretive_content_included": False,
    }
    package = {
        "source": source,
        "pages": page_rows,
        "page_blocks": page_blocks,
        "sections": sections,
        "evidence": evidence,
        "terms": terms,
        "scripture": scripture,
        "attributions": attributions,
    }
    package["validation"] = validate_package(package)
    return package


def save_package(package, signature):
    source_id = package["source"]["source_id"]
    paths = package_paths(source_id)
    paths["base"].mkdir(parents=True, exist_ok=True)
    package["source"]["schema_version"] = SCHEMA_VERSION
    write_json(paths["source"], package["source"])
    compact_pages = []
    for page in package["pages"]:
        page = dict(page)
        if page.get("cleaned_text") == page.get("original_text"):
            page.pop("cleaned_text", None)
            page["cleaned_text_same_as_original"] = True
        compact_pages.append(page)
    compact_evidence = []
    for row in package["evidence"]:
        row = dict(row)
        text = row.get("verbatim_text") or row.get("reader_text", "")
        if text:
            row["verbatim_text_sha256"] = sha256_hex(text)
        if row.get("reader_text") == row.get("verbatim_text"):
            row.pop("verbatim_text", None)
        compact_evidence.append(row)
    compact_terms = []
    for row in package["terms"]:
        row = dict(row)
        row.pop("evidence_text", None)
        compact_terms.append(row)
    compact_scripture = []
    for row in package["scripture"]:
        row = dict(row)
        row.pop("evidence_text", None)
        compact_scripture.append(row)
    write_jsonl(paths["pages"], compact_pages)
    write_jsonl(paths["page_blocks"], package.get("page_blocks", []))
    write_jsonl(paths["sections"], package["sections"])
    write_jsonl(paths["evidence"], compact_evidence)
    write_jsonl(paths["terms"], compact_terms)
    write_jsonl(paths["scripture"], compact_scripture)
    write_jsonl(paths["attributions"], package["attributions"])
    write_json(paths["validation"], package["validation"])
    metadata = {
        "source_id": source_id,
        "schema_version": SCHEMA_VERSION,
        "builder_version": BUILDER_VERSION,
        "term_registry_sha256": TERM_REGISTRY_SHA256,
        "signature": signature,
        "package_status": package["validation"]["status"],
        "counts": package["validation"]["counts"],
    }
    write_json(paths["metadata"], metadata)
    for legacy_name in ("units.jsonl", "author_work_inventory.jsonl"):
        legacy_path = paths["base"] / legacy_name
        if legacy_path.exists():
            legacy_path.unlink()


def load_package(source_id, include_text=True):
    paths = package_paths(source_id)
    page_rows = list(iter_jsonl(paths["pages"]))
    page_map = {row["page_number"]: row for row in page_rows}
    for row in page_rows:
        if row.get("cleaned_text_same_as_original") and "cleaned_text" not in row:
            row["cleaned_text"] = row.get("original_text", "")
    evidence_rows = list(iter_jsonl(paths["evidence"]))
    evidence_map = {row["evidence_id"]: row for row in evidence_rows}
    term_rows = list(iter_jsonl(paths["terms"]))
    scripture_rows = list(iter_jsonl(paths["scripture"]))
    if include_text:
        for row in evidence_rows:
            if "verbatim_text" not in row:
                if row.get("reader_text") is not None:
                    row["verbatim_text"] = row.get("reader_text", "")
                else:
                    page_text = page_map[row["page_number"]].get("original_text", "")
                    row["verbatim_text"] = page_text[row["char_start"]:row["char_end"]]
            if "reader_text" not in row:
                row["reader_text"] = row.get("verbatim_text", "")
        for row in term_rows:
            if "evidence_text" not in row and row.get("matches"):
                source_text = evidence_text(evidence_map.get(row["evidence_id"], {}))
                first = row["matches"][0]
                excerpt, start, end = context_excerpt(
                    source_text, first["char_start"], first["char_end"]
                )
                row["evidence_text"] = excerpt
                row["evidence_char_start"] = start
                row["evidence_char_end"] = end
        for row in scripture_rows:
            if "evidence_text" not in row:
                source_text = evidence_text(evidence_map.get(row["evidence_id"], {}))
                excerpt, start, end = context_excerpt(
                    source_text, row["char_start"], row["char_end"]
                )
                row["evidence_text"] = excerpt
                row["evidence_char_start"] = start
                row["evidence_char_end"] = end
    if not include_text:
        for row in page_rows:
            row.pop("original_text", None)
            row.pop("cleaned_text", None)
    return {
        "source": read_json(paths["source"], {}),
        "pages": page_rows,
        "page_blocks": list(iter_jsonl(paths["page_blocks"])),
        "sections": list(iter_jsonl(paths["sections"])),
        "evidence": evidence_rows,
        "terms": term_rows,
        "scripture": scripture_rows,
        "attributions": list(iter_jsonl(paths["attributions"])),
        "validation": read_json(paths["validation"], {}),
    }


def registry_form_row(printed_form, source, page, evidence_id, char_start, char_end):
    return {
        "printed_form": printed_form,
        "matching_rule": "literal_case_insensitive_with_alphanumeric_boundaries",
        "attestation": {
            "source_id": source["source_id"],
            "pdf_file": source["pdf_file"],
            "page_number": page["page_number"],
            "evidence_id": evidence_id,
            "char_start": char_start,
            "char_end": char_end,
            "page_text_sha256": page.get("original_text_sha256")
            or sha256_hex(page.get("original_text", "")),
        },
    }


def registry_row(lookup_form, registered_forms):
    return {
        "term_registration_id": term_registration_id_for(lookup_form),
        "lookup_form": lookup_form,
        "normalization_rule": "case_punctuation_and_spacing_only",
        "registration_basis": "literal_printed_form_attested_in_supplied_pdf",
        "registered_forms": sorted(
            registered_forms,
            key=lambda row: (row["printed_form"].casefold(), row["printed_form"]),
        ),
        "semantic_definition": None,
        "semantic_relations": None,
    }


def derive_term_registry_from_packages(active_source_ids=None, compare_existing_catalog=False):
    active_source_ids = active_source_ids or [
        legacy.source_id_for(path) for path in legacy.pdf_paths()
    ]
    forms_by_lookup = defaultdict(dict)
    for source_id in active_source_ids:
        paths = package_paths(source_id)
        source = read_json(paths["source"], {})
        pages = {
            row["page_number"]: row
            for row in iter_jsonl(paths["pages"])
        }
        evidence_by_id = {}
        for evidence in iter_jsonl(paths["evidence"]):
            if "verbatim_text" not in evidence:
                evidence["verbatim_text"] = evidence.get("reader_text", "")
            evidence_by_id[evidence["evidence_id"]] = evidence
        if not source or not pages:
            raise RuntimeError(f"Cannot derive term registry from incomplete package {source_id}")
        for term_row in iter_jsonl(paths["terms"]):
            lookup = term_row["lookup_form"]
            evidence = evidence_by_id.get(term_row["evidence_id"])
            page = pages.get(term_row["page_number"])
            if not page:
                raise RuntimeError(
                    f"Cannot attest {lookup!r}: page {term_row['page_number']} is missing in {source_id}"
                )
            if not evidence:
                raise RuntimeError(
                    f"Cannot attest {lookup!r}: evidence {term_row['evidence_id']} is missing in {source_id}"
                )
            source_text = evidence_text(evidence)
            for match in term_row.get("matches", []):
                start, end = match["char_start"], match["char_end"]
                printed = match["printed_text"]
                if source_text[start:end] != printed:
                    raise RuntimeError(
                        f"Cannot attest {lookup!r}: stored evidence text is not exact in {source_id} {term_row['evidence_id']}"
                    )
                if mechanical_term_form(printed) != lookup:
                    raise RuntimeError(
                        f"Cannot attest {lookup!r}: printed form normalizes differently: {printed!r}"
                    )
                match_page = pages.get(match.get("page_number") or term_row["page_number"]) or page
                form_key = printed.casefold()
                forms_by_lookup[lookup].setdefault(
                    form_key,
                    registry_form_row(
                        printed,
                        source,
                        match_page,
                        term_row["evidence_id"],
                        start,
                        end,
                    ),
                )

    rows = [
        registry_row(lookup, list(forms.values()))
        for lookup, forms in sorted(forms_by_lookup.items())
    ]
    catalog_path = OUT_DIR / "christian_kb_term_catalog.jsonl"
    if compare_existing_catalog and catalog_path.exists():
        catalog_terms = {row["lookup_form"] for row in iter_jsonl(catalog_path)}
        derived_terms = {row["lookup_form"] for row in rows}
        if catalog_terms != derived_terms:
            missing = sorted(catalog_terms - derived_terms)
            extra = sorted(derived_terms - catalog_terms)
            raise RuntimeError(
                "Term-registry migration does not match the current catalog: "
                f"missing={missing[:10]}, extra={extra[:10]}"
            )
    return rows


def initialize_term_registry_from_packages():
    rows = derive_term_registry_from_packages()
    write_jsonl(TERM_REGISTRY_SOURCE_PATH, rows)
    configure_term_registry(rows)
    return {
        "registry_schema_version": TERM_REGISTRY_SCHEMA_VERSION,
        "term_registry_sha256": TERM_REGISTRY_SHA256,
        "migration_basis": "existing_validated_literal_term_occurrence_rows",
        "registered_terms": len(REGISTERED_TERM_ROWS),
        "registered_forms": sum(
            len(row["registered_forms"]) for row in REGISTERED_TERM_ROWS
        ),
        "knowledge_occurrences_changed": False,
        "semantic_content_added": False,
    }


def refresh_term_registry_from_clean_packages(active_source_ids):
    before_hash = TERM_REGISTRY_SHA256
    rows = derive_term_registry_from_packages(active_source_ids)
    configure_term_registry(rows)
    write_jsonl(TERM_REGISTRY_SOURCE_PATH, REGISTERED_TERM_ROWS)

    revalidated = 0
    for source_id in active_source_ids:
        paths = package_paths(source_id)
        metadata = read_json(paths["metadata"], {})
        package = load_package(source_id)
        package["validation"] = validate_package(package)
        if package["validation"]["status"] != "pass":
            raise RuntimeError(
                f"Package validation failed after clean registry refresh for {source_id}: "
                + "; ".join(package["validation"]["errors"][:5])
            )
        save_package(package, metadata.get("signature", {}))
        revalidated += 1
        del package

    return {
        "before_term_registry_sha256": before_hash,
        "term_registry_sha256": TERM_REGISTRY_SHA256,
        "registered_terms": len(REGISTERED_TERM_ROWS),
        "registered_forms": sum(
            len(row["registered_forms"]) for row in REGISTERED_TERM_ROWS
        ),
        "source_packages_revalidated": revalidated,
        "attestation_basis": "reader_clean_evidence_text",
    }


TERM_ATTESTATION_TOKEN_RE = re.compile(r"[A-Za-z0-9']+")


def has_literal_term_boundaries(text, start, end):
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return not (before and before.isascii() and before.isalnum()) and not (
        after and after.isascii() and after.isalnum()
    )


def find_source_attestations(literal_terms, active_source_ids=None):
    candidates = []
    seen = set()
    for value in literal_terms:
        candidate = normalize_extracted_text(value).strip()
        if not candidate or not mechanical_term_form(candidate):
            raise ValueError("A registered term must contain source-searchable text")
        key = candidate.casefold()
        if key not in seen:
            candidates.append(candidate)
            seen.add(key)
    candidate_buckets = defaultdict(list)
    for candidate in candidates:
        lowered = candidate.lower()
        tokens = TERM_ATTESTATION_TOKEN_RE.findall(lowered)
        if not tokens:
            raise ValueError("A registered term must contain source-searchable text")
        candidate_buckets[tokens[0]].append((candidate, lowered))
    found = {}
    active_source_ids = active_source_ids or [
        legacy.source_id_for(path) for path in legacy.pdf_paths()
    ]
    for source_id in active_source_ids:
        if len(found) == len(candidates):
            break
        paths = package_paths(source_id)
        source = read_json(paths["source"], {})
        pages = {row["page_number"]: row for row in iter_jsonl(paths["pages"])}
        for evidence in iter_jsonl(paths["evidence"]):
            if len(found) == len(candidates):
                break
            text = evidence_text(evidence)
            lowered_text = text.lower()
            possible = []
            possible_seen = set()
            for token in set(TERM_ATTESTATION_TOKEN_RE.findall(lowered_text)):
                for item in candidate_buckets.get(token, []):
                    if item[0] not in possible_seen:
                        possible.append(item)
                        possible_seen.add(item[0])
            for candidate, lowered_candidate in possible:
                if candidate in found:
                    continue
                start = lowered_text.find(lowered_candidate)
                while start != -1:
                    end = start + len(lowered_candidate)
                    if has_literal_term_boundaries(lowered_text, start, end):
                        break
                    start = lowered_text.find(lowered_candidate, start + 1)
                if start == -1:
                    continue
                printed = text[start:end]
                page_number = page_for_evidence_offset(evidence, start)
                page = pages.get(page_number) or {"page_number": page_number, "original_text_sha256": evidence.get("raw_page_text_sha256")}
                found[candidate] = registry_form_row(
                    printed,
                    source,
                    page,
                    evidence["evidence_id"],
                    start,
                    end,
                )
    missing = [candidate for candidate in candidates if candidate not in found]
    if missing:
        raise ValueError(
            "Literal term registration rejected because no exact supplied-PDF attestation was found: "
            + ", ".join(repr(value) for value in missing)
        )
    return found


def register_source_attested_terms(literal_terms):
    if not REGISTERED_TERM_ROWS:
        raise RuntimeError(
            "Cannot add a term to an empty registry; run --initialize-term-registry first"
        )
    before_terms = len(REGISTERED_TERM_ROWS)
    before_forms = sum(
        len(row["registered_forms"]) for row in REGISTERED_TERM_ROWS
    )
    before_hash = TERM_REGISTRY_SHA256
    attestations = find_source_attestations(literal_terms)
    rows_by_lookup = {
        row["lookup_form"]: {
            **row,
            "registered_forms": list(row["registered_forms"]),
        }
        for row in REGISTERED_TERM_ROWS
    }
    added = []
    unchanged = []
    for candidate, form in attestations.items():
        lookup = mechanical_term_form(form["printed_form"])
        row = rows_by_lookup.setdefault(lookup, registry_row(lookup, []))
        existing_keys = {
            item["printed_form"].casefold() for item in row["registered_forms"]
        }
        if form["printed_form"].casefold() in existing_keys:
            unchanged.append(candidate)
            continue
        row["registered_forms"].append(form)
        row["registered_forms"].sort(
            key=lambda item: (item["printed_form"].casefold(), item["printed_form"])
        )
        added.append(candidate)
    rows = canonical_term_registry_rows(rows_by_lookup.values())
    configure_term_registry(rows)
    if added:
        write_jsonl(TERM_REGISTRY_SOURCE_PATH, REGISTERED_TERM_ROWS)
    return {
        "added": added,
        "unchanged": unchanged,
        "before_registered_terms": before_terms,
        "before_registered_forms": before_forms,
        "before_term_registry_sha256": before_hash,
        "term_registry_sha256": TERM_REGISTRY_SHA256,
        "registered_terms": len(REGISTERED_TERM_ROWS),
        "registered_forms": sum(
            len(row["registered_forms"]) for row in REGISTERED_TERM_ROWS
        ),
    }


def read_term_requests(path):
    request_path = Path(path)
    data = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Term requests file must be a JSON list of strings")
    terms = []
    for index, value in enumerate(data, start=1):
        if not isinstance(value, str):
            raise ValueError(f"Term requests row {index} is not a string")
        term = value.strip()
        if term:
            terms.append(term)
    if not terms:
        raise ValueError("Term requests file does not contain any non-empty terms")
    return terms


class SplitJsonlWriter:
    def __init__(self, prefix):
        self.prefix = prefix
        self.index = 0
        self.stream = None
        self.current_bytes = 0
        self.files = []
        for path in OUT_DIR.glob(f"{prefix}_*.jsonl"):
            path.unlink()

    def _open_next(self):
        if self.stream:
            self.stream.close()
        self.index += 1
        path = OUT_DIR / f"{self.prefix}_{self.index:03d}.jsonl"
        self.stream = path.open("w", encoding="utf-8", newline="\n")
        self.files.append(path.name)
        self.current_bytes = 0

    def write(self, row):
        line = json_dumps(row) + "\n"
        encoded_size = len(line.encode("utf-8"))
        if self.stream is None or (self.current_bytes and self.current_bytes + encoded_size > MAX_SPLIT_BYTES):
            self._open_next()
        self.stream.write(line)
        self.current_bytes += encoded_size

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        return self.files


def legacy_snapshot():
    manifest = read_json(OUT_DIR / "christian_kb_manifest.json", {})
    source_stats = defaultdict(dict)
    for path in sorted(OUT_DIR.glob("christian_kb_units_*.jsonl")):
        for row in iter_jsonl(path):
            stats = source_stats[row.get("source_id", "Unknown")]
            stats["legacy_units"] = stats.get("legacy_units", 0) + 1
            if row.get("claim_atoms"):
                stats["rows_with_claim_atoms"] = stats.get("rows_with_claim_atoms", 0) + 1
            if row.get("topic_ids"):
                stats["rows_with_topic_ids"] = stats.get("rows_with_topic_ids", 0) + 1
    if CACHE_DIR.exists():
        for package_dir in sorted(path for path in CACHE_DIR.iterdir() if path.is_dir()):
            metadata = read_json(package_dir / "metadata.json", {})
            validation = read_json(package_dir / "validation.json", {})
            if validation.get("status") != "pass":
                continue
            counts = validation.get("counts", {})
            stats = source_stats[package_dir.name]
            stats.update(
                {
                    "prior_package_builder": metadata.get("builder_version", "Unknown"),
                    "prior_pages": counts.get("pages", 0),
                    "prior_evidence": counts.get("evidence", 0),
                    "prior_term_occurrence_rows": counts.get("term_occurrence_rows", 0),
                    "prior_scripture_occurrences": counts.get("scripture_occurrences", 0),
                    "prior_attributions": counts.get("attributions", 0),
                }
            )
    return {
        "captured_at": now_iso(),
        "manifest": manifest,
        "source_stats": {key: dict(value) for key, value in sorted(source_stats.items())},
    }


def existing_evidence_scopes():
    scopes = []
    for path in sorted(OUT_DIR.glob("christian_kb_evidence_*_*.jsonl")):
        for row in iter_jsonl(path):
            evidence_id = row.get("evidence_id")
            source_id = row.get("source_id")
            if not evidence_id or not source_id:
                continue
            page_start = row.get("page_start") or row.get("page_number")
            page_end = row.get("page_end") or row.get("page_number")
            if not isinstance(page_start, int) or not isinstance(page_end, int):
                continue
            scopes.append(
                {
                    "old_id": evidence_id,
                    "source_id": source_id,
                    "page_start": page_start,
                    "page_end": page_end,
                    "text_role": row.get("text_role"),
                }
            )
    return scopes


def old_redirect_rows(page_to_evidence, prior_evidence_scopes=None):
    existing = list(iter_jsonl(OUT_DIR / "christian_kb_id_redirects.jsonl"))
    seen = {row["old_id"] for row in existing}
    rows = list(existing)
    for scope in prior_evidence_scopes or []:
        old_id = scope["old_id"]
        if old_id in seen:
            continue
        replacements = []
        for page_number in range(scope["page_start"], scope["page_end"] + 1):
            replacements.extend(page_to_evidence.get((scope["source_id"], page_number), []))
        replacements = sorted(set(replacements))
        if not replacements or old_id in replacements:
            continue
        rows.append(
            {
                "old_id": old_id,
                "old_record_type": "previous_evidence_chunk",
                "replacement_evidence_ids": replacements,
                "redirect_basis": "same_source_and_overlapping_pdf_page_range_after_clean_rebuild",
                "exact_semantic_equivalence": False,
                "manual_verification": (
                    f"Open source {scope['source_id']} PDF pages "
                    f"{scope['page_start']}-{scope['page_end']} and compare the previous raw-page evidence "
                    "with the replacement clean evidence chunks."
                ),
            }
        )
        seen.add(old_id)
    for path in sorted(OUT_DIR.glob("christian_kb_units_*.jsonl")):
        for unit in iter_jsonl(path):
            old_id = unit.get("unit_id")
            if not old_id or old_id in seen:
                continue
            source_id = unit.get("source_id", "")
            start, end = unit.get("page_refs", [0, -1])
            replacements = []
            for page_number in range(int(start), int(end) + 1):
                replacements.extend(page_to_evidence.get((source_id, page_number), []))
            rows.append(
                {
                    "old_id": old_id,
                    "old_record_type": "legacy_inferred_knowledge_unit",
                    "replacement_evidence_ids": replacements,
                    "redirect_basis": "same_source_and_overlapping_pdf_page_range",
                    "exact_semantic_equivalence": False,
                    "manual_verification": f"Open the supplied PDF pages {start}-{end} and compare them with the replacement evidence chunks.",
                }
            )
            seen.add(old_id)
    return sorted(rows, key=lambda row: row["old_id"])


def source_signature(path):
    return {"pdf_file": portable_path(path), "size_bytes": path.stat().st_size, "pdf_sha256": sha256_file(path)}


def audit_paths(run_date):
    audit_dir = OUT_DIR / run_date
    audit_dir.mkdir(parents=True, exist_ok=True)
    return {
        "dir": audit_dir,
        "baseline": audit_dir / f"source_faithful_baseline_{run_date}.json",
        "log": audit_dir / f"source_faithful_rebuild_log_{run_date}.jsonl",
        "report": audit_dir / f"source_faithful_rebuild_report_{run_date}.md",
        "summary": audit_dir / f"source_faithful_rebuild_summary_{run_date}.json",
        "validation": audit_dir / f"source_faithful_validation_{run_date}.json",
        "visual_samples": audit_dir / f"source_faithful_visual_samples_{run_date}.jsonl",
    }


def start_audit(paths, baseline):
    write_json(paths["baseline"], baseline)
    write_jsonl(paths["log"], [])
    write_jsonl(paths["visual_samples"], [])
    paths["report"].write_text(
        "\n".join(
            [
                "# Source-Faithful Theology Knowledge Base Rebuild",
                "",
                f"Run date: {paths['dir'].name}",
                f"Schema: `{SCHEMA_VERSION}`",
                f"Builder: `{BUILDER_VERSION}`",
                "",
                "Each PDF was processed, validated, compared, and logged before the next PDF began. Cross-book keys were updated incrementally from exact literal terms and explicit printed Scripture references. No generated theological conclusion is stored.",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )


def append_audit_entry(paths, entry):
    with paths["log"].open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json_dumps(entry) + "\n")
    with paths["visual_samples"].open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json_dumps({"source_id": entry["source_id"], "pdf_file": entry["pdf_file"], "pages": entry["visual_sample_pages"]}) + "\n")
    sample = entry.get("manual_sample", {})
    before = entry["before"]
    before_lines = ["Before:"]
    if before.get("prior_package_builder"):
        before_lines.extend(
            [
                f"- Prior source-faithful package: `{before['prior_package_builder']}`",
                f"- Pages: {before.get('prior_pages', 0)}; evidence chunks: {before.get('prior_evidence', 0)}; literal-term rows: {before.get('prior_term_occurrence_rows', 0)}; Scripture occurrences: {before.get('prior_scripture_occurrences', 0)}",
                f"- Explicit attribution rows: {before.get('prior_attributions', 0)}",
            ]
        )
    if before.get("legacy_units"):
        before_lines.extend(
            [
                f"- Legacy units: {before.get('legacy_units', 0)}",
                f"- Rows carrying inferred topic tags: {before.get('rows_with_topic_ids', 0)}",
                f"- Rows carrying selected claim atoms: {before.get('rows_with_claim_atoms', 0)}",
            ]
        )
    if len(before_lines) == 1:
        before_lines.append("- No prior validated package or legacy rows were present for this source.")
    if before.get("legacy_units"):
        first_change = "- Replaced inferred topics, selected claims, author positions, and synthesized cross-book comparisons with exact evidence and mechanical concordances."
    else:
        first_change = "- Reread the PDF and rebuilt its exact evidence, literal-term, Scripture, and attribution records against the prior source-faithful baseline."
    lines = [
        f"## {entry['source_id']} - {entry['display_title']}",
        "",
        f"PDF: `{entry['pdf_file']}`",
        f"Package status: `{entry['package_status']}`",
        "",
        *before_lines,
        "",
        "Changed:",
        first_change,
        "- Preserved every character returned by the PDF extractor in contiguous evidence chunks.",
        "- Accepted authorship only from an exact printed byline or explicit PDF outline heading; unresolved attribution remains `Unknown`.",
        "",
        "Changed To:",
        f"- Pages: {entry['after']['pages']}; evidence chunks: {entry['after']['evidence']}; literal-term rows: {entry['after']['term_occurrence_rows']}; Scripture occurrences: {entry['after']['scripture_occurrences']}",
        f"- Explicit attribution rows: {entry['after']['attributions']}; extraction warnings: {entry['warning_count']}",
        f"- Newly connected cross-book term keys: {entry['incremental_crossbook_term_links']}; Scripture keys: {entry['incremental_crossbook_scripture_links']}",
        "",
        "Why:",
        "- Exact source evidence is reproducible and does not require accepting a generated theological inference.",
        "- Independent packages make future additions and affected global rebuilds deterministic.",
        "",
        "How To Manually Verify:",
        f"- Open `{entry['pdf_file']}` at PDF page {sample.get('page_number', 1)}.",
        f"- Find `{sample.get('evidence_id', '')}` in `_source_cache/{entry['source_id']}/evidence.jsonl`.",
        "- Compare `reader_text` with the visible PDF body text, then inspect `page_blocks.jsonl` for footnotes, headers, printed page numbers, and hidden/off-page text.",
        f"- Suggested rendered sample pages: {', '.join(str(value) for value in entry['visual_sample_pages']) or 'none'}.",
        "",
    ]
    with paths["report"].open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines) + "\n")


def source_csv(rows):
    fields = [
        "source_id", "collection", "pdf_file", "display_title", "title_basis",
        "source_author", "source_author_basis", "page_count", "pdf_sha256",
        "size_bytes", "extraction_engine", "extracted_text_pages", "extracted_word_count",
        "extracted_character_count", "reader_text_pages", "reader_word_count",
        "footnote_word_count", "external_knowledge_used", "interpretive_content_included",
    ]
    with (OUT_DIR / "christian_kb_sources.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def cleanup_deprecated_outputs():
    exact_names = [
        "christian_kb_author_work_inventory.jsonl",
        "christian_kb_concepts.jsonl",
        "christian_kb_scripture_index.jsonl",
        "christian_kb_sections.jsonl",
    ]
    for name in exact_names:
        path = OUT_DIR / name
        if path.exists():
            path.unlink()
    for prefix in ("christian_kb_units", "christian_kb_author_positions", "christian_kb_crossbook_matrix"):
        for path in OUT_DIR.glob(f"{prefix}_*.jsonl"):
            path.unlink()


def assemble_bundle(active_source_ids, previous_registry=None):
    if not REGISTERED_TERM_ROWS:
        raise RuntimeError(
            f"Term registry is empty. Run --initialize-term-registry before building."
        )
    previous_registry = previous_registry or []
    prior_evidence_scopes = existing_evidence_scopes()
    source_rows = []
    package_manifest_rows = []
    page_to_evidence = defaultdict(list)
    term_aggregate = defaultdict(lambda: {"occurrences": 0, "evidence_rows": 0, "sources": {}})
    scripture_aggregate = defaultdict(lambda: {"occurrences": 0, "sources": {}})
    counts = Counter()

    page_writer = SplitJsonlWriter("christian_kb_pages")
    page_block_writer = SplitJsonlWriter("christian_kb_page_blocks")
    section_writer = SplitJsonlWriter("christian_kb_sections")
    evidence_writers = {
        "primary": SplitJsonlWriter("christian_kb_evidence_primary"),
        "extended": SplitJsonlWriter("christian_kb_evidence_extended"),
        "archival": SplitJsonlWriter("christian_kb_evidence_archival"),
    }
    term_writer = SplitJsonlWriter("christian_kb_term_concordance")
    scripture_writer = SplitJsonlWriter("christian_kb_scripture_concordance")
    author_writer = SplitJsonlWriter("christian_kb_author_evidence")

    for source_id in active_source_ids:
        paths = package_paths(source_id)
        source = read_json(paths["source"], {})
        validation = read_json(paths["validation"], {})
        if validation.get("status") != "pass":
            raise RuntimeError(f"Source package {source_id} is not valid")
        if validation.get("term_registry_sha256") != TERM_REGISTRY_SHA256:
            raise RuntimeError(
                f"Source package {source_id} was not indexed with the active term registry"
            )
        source_rows.append(source)
        package_manifest_rows.append(
            {
                "source_id": source_id,
                "pdf_file": source["pdf_file"],
                "pdf_sha256": source["pdf_sha256"],
                "schema_version": SCHEMA_VERSION,
                "builder_version": BUILDER_VERSION,
                "term_registry_sha256": TERM_REGISTRY_SHA256,
                "package_status": "validated",
                **validation["counts"],
            }
        )
        package_pages = list(iter_jsonl(paths["pages"]))
        raw_page_text = {
            row["page_number"]: row.get("original_text", "")
            for row in package_pages
        }
        evidence_text_by_id = {}
        evidence_metadata = {}
        for row in iter_jsonl(paths["evidence"]):
            if "verbatim_text" not in row:
                row["verbatim_text"] = row.get("reader_text", "")
            if "reader_text" not in row:
                row["reader_text"] = row.get("verbatim_text", "")
            evidence_writers[row["retrieval_layer"]].write(row)
            counts[f"evidence_{row['retrieval_layer']}"] += 1
            span_pages = {
                span.get("page_number")
                for span in row.get("source_spans", [])
                if span.get("page_number")
            } or {row["page_number"]}
            for page_number in span_pages:
                page_to_evidence[(source_id, page_number)].append(row["evidence_id"])
            evidence_text_by_id[row["evidence_id"]] = evidence_text(row)
            evidence_metadata[row["evidence_id"]] = {
                "author": row["work_attributed_author"],
                "mentioned_authors": row.get("mentioned_authors", []),
                "page": row["page_number"],
                "text_role": row.get("text_role", TEXT_ROLE_BODY),
                "retrieval_layer": row.get("retrieval_layer", ""),
            }
        for row in iter_jsonl(paths["page_blocks"]):
            page_block_writer.write(row)
            counts["page_blocks"] += 1
        for page in package_pages:
            page.pop("original_text", None)
            page.pop("cleaned_text", None)
            page.pop("reader_text", None)
            page.pop("footnote_text", None)
            page.pop("footnote_refs", None)
            page["evidence_ids"] = page_to_evidence[(source_id, page["page_number"])]
            page_writer.write(page)
            counts["pages"] += 1
        for row in iter_jsonl(paths["sections"]):
            section_writer.write(row)
            counts["sections"] += 1
        source_term_rows = {}
        for row in iter_jsonl(paths["terms"]):
            counts["term_occurrence_rows"] += 1
            first_match = row["matches"][0]
            text = evidence_text_by_id.get(row["evidence_id"], "")
            excerpt, _, _ = context_excerpt(
                text, first_match["char_start"], first_match["char_end"]
            )
            source_index = source_term_rows.setdefault(
                row["lookup_form"],
                {
                    "term_source_concordance_id": (
                        "TERM_SOURCE_"
                        + sha256_hex(f"{row['lookup_form']}|{source_id}")[:16]
                    ),
                    "lookup_form": row["lookup_form"],
                    "source_id": source_id,
                    "normalization_rule": "case_punctuation_and_spacing_only",
                    "occurrence_count": 0,
                    "evidence_locations": [],
                    "semantic_assertion": None,
                },
            )
            source_index["occurrence_count"] += row["occurrence_count"]
            source_index["evidence_locations"].append(
                {
                    "evidence_id": row["evidence_id"],
                    "page_number": row["page_number"],
                    "occurrence_count": row["occurrence_count"],
                    "first_char_start": first_match["char_start"],
                    "text_role": row.get("text_role", evidence_metadata.get(row["evidence_id"], {}).get("text_role", TEXT_ROLE_BODY)),
                    "retrieval_layer": evidence_metadata.get(row["evidence_id"], {}).get("retrieval_layer", ""),
                }
            )
            aggregate = term_aggregate[row["lookup_form"]]
            aggregate["occurrences"] += row["occurrence_count"]
            aggregate["evidence_rows"] += 1
            source_record = aggregate["sources"].setdefault(
                source_id,
                {
                    "source_id": source_id,
                    "work_attributed_author": evidence_metadata.get(row["evidence_id"], {}).get("author", "Unknown"),
                    "mentioned_authors": evidence_metadata.get(row["evidence_id"], {}).get("mentioned_authors", []),
                    "evidence_id": row["evidence_id"],
                    "page_number": row["page_number"],
                    "text_role": row.get("text_role", evidence_metadata.get(row["evidence_id"], {}).get("text_role", TEXT_ROLE_BODY)),
                    "retrieval_layer": evidence_metadata.get(row["evidence_id"], {}).get("retrieval_layer", ""),
                    "evidence_text": excerpt,
                    "occurrence_count": 0,
                },
            )
            source_record["occurrence_count"] += row["occurrence_count"]
        for lookup in sorted(source_term_rows):
            term_writer.write(source_term_rows[lookup])
            counts["term_concordance_rows"] += 1

        source_scripture_rows = {}
        for row in iter_jsonl(paths["scripture"]):
            counts["scripture_occurrences"] += 1
            key = row["canonical_reference"] or f"UNRESOLVED:{row['printed_reference'].casefold()}"
            text = evidence_text_by_id.get(row["evidence_id"], "")
            excerpt, _, _ = context_excerpt(text, row["char_start"], row["char_end"])
            source_index = source_scripture_rows.setdefault(
                key,
                {
                    "scripture_source_concordance_id": (
                        "SCRIPTURE_SOURCE_" + sha256_hex(f"{key}|{source_id}")[:16]
                    ),
                    "canonical_or_unresolved_key": key,
                    "source_id": source_id,
                    "occurrence_count": 0,
                    "printed_forms": Counter(),
                    "evidence_locations": [],
                    "allusion_inference": None,
                },
            )
            source_index["occurrence_count"] += 1
            source_index["printed_forms"][row["printed_reference"]] += 1
            source_index["evidence_locations"].append(
                {
                    "scripture_occurrence_id": row["scripture_occurrence_id"],
                    "evidence_id": row["evidence_id"],
                    "page_number": row["page_number"],
                    "char_start": row["char_start"],
                    "char_end": row["char_end"],
                    "text_role": row.get("text_role", evidence_metadata.get(row["evidence_id"], {}).get("text_role", TEXT_ROLE_BODY)),
                    "retrieval_layer": evidence_metadata.get(row["evidence_id"], {}).get("retrieval_layer", ""),
                }
            )
            aggregate = scripture_aggregate[key]
            aggregate["occurrences"] += 1
            source_record = aggregate["sources"].setdefault(
                source_id,
                {
                    "source_id": source_id,
                    "work_attributed_author": evidence_metadata.get(row["evidence_id"], {}).get("author", "Unknown"),
                    "mentioned_authors": evidence_metadata.get(row["evidence_id"], {}).get("mentioned_authors", []),
                    "evidence_id": row["evidence_id"],
                    "page_number": row["page_number"],
                    "text_role": row.get("text_role", evidence_metadata.get(row["evidence_id"], {}).get("text_role", TEXT_ROLE_BODY)),
                    "retrieval_layer": evidence_metadata.get(row["evidence_id"], {}).get("retrieval_layer", ""),
                    "printed_reference": row["printed_reference"],
                    "canonical_reference": row["canonical_reference"],
                    "evidence_text": excerpt,
                    "occurrence_count": 0,
                },
            )
            source_record["occurrence_count"] += 1
        for key in sorted(source_scripture_rows):
            source_index = source_scripture_rows[key]
            source_index["printed_forms"] = dict(
                sorted(source_index["printed_forms"].items())
            )
            scripture_writer.write(source_index)
            counts["scripture_concordance_rows"] += 1
        for row in iter_jsonl(paths["attributions"]):
            author_writer.write(row)
            counts["author_evidence_rows"] += 1

    files = {
        "pages": page_writer.close(),
        "page_blocks": page_block_writer.close(),
        "sections": section_writer.close(),
        "evidence_primary": evidence_writers["primary"].close(),
        "evidence_extended": evidence_writers["extended"].close(),
        "evidence_archival": evidence_writers["archival"].close(),
        "term_concordance": term_writer.close(),
        "scripture_concordance": scripture_writer.close(),
        "author_evidence": author_writer.close(),
    }

    term_catalog = []
    for lookup, aggregate in sorted(term_aggregate.items()):
        term_catalog.append(
            {
                "lookup_form": lookup,
                "normalization_rule": "case_punctuation_and_spacing_only",
                "selection_basis": "literal_printed_form_attested_in_supplied_pdf",
                "occurrence_count": aggregate["occurrences"],
                "evidence_row_count": aggregate["evidence_rows"],
                "source_count": len(aggregate["sources"]),
                "semantic_definition": None,
            }
        )
    write_jsonl(OUT_DIR / TERM_REGISTRY_FILENAME, REGISTERED_TERM_ROWS)
    write_jsonl(OUT_DIR / "christian_kb_term_catalog.jsonl", term_catalog)
    counts["literal_terms"] = len(term_catalog)
    counts["registered_literal_terms"] = len(REGISTERED_TERM_ROWS)
    counts["registered_literal_forms"] = sum(
        len(row["registered_forms"]) for row in REGISTERED_TERM_ROWS
    )

    crossbook_writer = SplitJsonlWriter("christian_kb_crossbook_evidence")
    crossbook_hash = hashlib.sha256()
    for lookup, aggregate in sorted(term_aggregate.items()):
        if len(aggregate["sources"]) < 2:
            continue
        row = {
            "crossbook_id": f"CROSS_TERM_{legacy.slug(lookup).upper()}",
            "link_basis": "shared_literal_term",
            "shared_value": lookup,
            "source_count": len(aggregate["sources"]),
            "occurrence_count": aggregate["occurrences"],
            "source_evidence": [aggregate["sources"][key] for key in sorted(aggregate["sources"])],
            "interpretive_conclusion": None,
        }
        crossbook_writer.write(row)
        crossbook_hash.update((json_dumps(row) + "\n").encode("utf-8"))
        counts["crossbook_term_rows"] += 1
    for reference, aggregate in sorted(scripture_aggregate.items()):
        if len(aggregate["sources"]) < 2:
            continue
        row = {
            "crossbook_id": f"CROSS_SCRIPTURE_{sha256_hex(reference)[:16]}",
            "link_basis": "shared_explicit_scripture_reference",
            "shared_value": reference,
            "source_count": len(aggregate["sources"]),
            "occurrence_count": aggregate["occurrences"],
            "source_evidence": [aggregate["sources"][key] for key in sorted(aggregate["sources"])],
            "interpretive_conclusion": None,
        }
        crossbook_writer.write(row)
        crossbook_hash.update((json_dumps(row) + "\n").encode("utf-8"))
        counts["crossbook_scripture_rows"] += 1
    files["crossbook_evidence"] = crossbook_writer.close()

    redirects = old_redirect_rows(page_to_evidence, prior_evidence_scopes)
    write_jsonl(OUT_DIR / "christian_kb_id_redirects.jsonl", redirects)
    counts["id_redirects"] = len(redirects)

    source_rows.sort(key=lambda row: row["source_id"])
    package_manifest_rows.sort(key=lambda row: row["source_id"])
    source_csv(source_rows)
    write_jsonl(OUT_DIR / "christian_kb_source_packages_manifest.jsonl", package_manifest_rows)

    previous_by_id = {row["source_id"]: row for row in previous_registry}
    registry = []
    for source in source_rows:
        registry.append(
            {
                "source_id": source["source_id"],
                "pdf_file": source["pdf_file"],
                "pdf_sha256": source["pdf_sha256"],
                "display_title": source["display_title"],
                "registry_status": "active",
                "schema_version": SCHEMA_VERSION,
                "last_verified_date": date.today().isoformat(),
            }
        )
    active_set = {row["source_id"] for row in registry}
    for source_id, old in sorted(previous_by_id.items()):
        if source_id not in active_set:
            registry.append({**old, "registry_status": "retired"})
    write_jsonl(OUT_DIR / "christian_kb_source_registry.jsonl", registry)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "builder_version": BUILDER_VERSION,
        "term_registry_schema_version": TERM_REGISTRY_SCHEMA_VERSION,
        "term_registry_sha256": TERM_REGISTRY_SHA256,
        "generated_at": now_iso(),
        "workspace": ".",
        "source_policy": {
            "supplied_pdfs_only": True,
            "external_knowledge_used": False,
            "generated_theological_conclusions": False,
            "unknown_preferred_over_inference": True,
            "query_routing_is_not_stored_theology": True,
            "term_registration_requires_exact_source_attestation": True,
            "all_registered_terms_use_one_schema": True,
        },
        "counts": {
            "sources": len(source_rows),
            **dict(sorted(counts.items())),
            "crossbook_rows": counts["crossbook_term_rows"] + counts["crossbook_scripture_rows"],
        },
        "files": {
            "core_primary_evidence": files["evidence_primary"],
            "extended_evidence": files["evidence_extended"],
            "archival_evidence": files["evidence_archival"],
            **{key: value for key, value in files.items() if not key.startswith("evidence_")},
            "source_registry": ["christian_kb_source_registry.jsonl"],
            "source_packages_manifest": ["christian_kb_source_packages_manifest.jsonl"],
            "term_registry": [TERM_REGISTRY_FILENAME],
            "term_catalog": ["christian_kb_term_catalog.jsonl"],
            "id_redirects": ["christian_kb_id_redirects.jsonl"],
            "sources": ["christian_kb_sources.csv"],
        },
        "crossbook_canonical_sha256": crossbook_hash.hexdigest(),
        "deprecated_inferred_outputs_removed": [
            "claim_atoms",
            "topic_ids",
            "author_positions",
            "related_concepts",
            "crossbook_matrix",
            "logic_shorthand",
        ],
    }
    page_locations.preserve_extension(OUT_DIR, manifest)
    write_json(OUT_DIR / "christian_kb_manifest.json", manifest)
    # Independently versioned metadata: unchanged sources reuse existing records;
    # only new/changed PDFs have their pagination inspected. No cache invalidation.
    page_locations.refresh(OUT_DIR)
    manifest = read_json(OUT_DIR / "christian_kb_manifest.json", {})
    cleanup_deprecated_outputs()
    return manifest


def write_manifest_markdown(manifest):
    counts = manifest["counts"]
    text = f"""# Christian Theology Knowledge Base Manifest

Schema: `{manifest['schema_version']}`
Builder: `{manifest['builder_version']}`
Term registry schema: `{manifest['term_registry_schema_version']}`
Term registry SHA-256: `{manifest['term_registry_sha256']}`

## Authority Boundary

This knowledge base is derived only from the supplied PDF files. It stores reader-clean evidence, role-classified PDF page blocks, raw page hashes, explicit printed work attribution, mentioned-author references, literal term occurrences, explicit printed Scripture references, and mechanical cross-book concordances. It contains no generated theological conclusion, inferred author position, inferred doctrinal agreement, or external enrichment.

`Unknown` and `unresolved` are intentional whenever the supplied PDF does not provide explicit evidence.

## Retrieval Without Stored Inference

A concept question is routed to registered literal source language and exact evidence locations. Every term uses the same registration schema and is accepted only when an exact printed form is attested by a supplied PDF page, clean evidence ID, character range, and raw page-text hash. No doctrine, topic, or vocabulary item has a privileged registration or validation path.

The registry supplies no generated definition, synonym relationship, doctrinal classification, or conclusion. Query-time term selection is a search action, not content in this knowledge base. Every answer still has to be supported by the cited reader-clean evidence, with raw page extraction available for audit.

## Recommended Upload

For the highest-precision bundle, upload:

1. `christian_kb_manifest.md`
2. `christian_kb_sources.csv`
3. `christian_kb_source_registry.jsonl`
4. `christian_kb_term_registry.jsonl`
5. `christian_kb_term_catalog.jsonl`
6. `christian_kb_term_concordance_*.jsonl`
7. `christian_kb_author_evidence_*.jsonl`
8. `christian_kb_scripture_concordance_*.jsonl`
9. `christian_kb_crossbook_evidence_*.jsonl`
10. `christian_kb_evidence_primary_*.jsonl`

Add `christian_kb_evidence_extended_*.jsonl` for unresolved attribution, `christian_kb_evidence_archival_*.jsonl` for editorial/reference/footnote material, and `christian_kb_page_blocks_*.jsonl` when you want layout-role audit records. Page metadata and source packages are mainly for manual verification and rebuilding.

## Counts

- Sources: {counts.get('sources', 0)}
- Pages: {counts.get('pages', 0)}
- Page layout blocks: {counts.get('page_blocks', 0)}
- Sections: {counts.get('sections', 0)}
- Primary evidence chunks: {counts.get('evidence_primary', 0)}
- Extended evidence chunks: {counts.get('evidence_extended', 0)}
- Archival evidence chunks: {counts.get('evidence_archival', 0)}
- Literal-term occurrence rows: {counts.get('term_occurrence_rows', 0)}
- Compact literal-term concordance rows: {counts.get('term_concordance_rows', 0)}
- Registered literal lookup forms: {counts.get('registered_literal_terms', 0)}
- Source-attested matcher forms: {counts.get('registered_literal_forms', 0)}
- Explicit Scripture occurrences: {counts.get('scripture_occurrences', 0)}
- Compact Scripture concordance rows: {counts.get('scripture_concordance_rows', 0)}
- Explicit attribution evidence rows: {counts.get('author_evidence_rows', 0)}
- Non-interpretive cross-book rows: {counts.get('crossbook_rows', 0)}
- Legacy ID redirects: {counts.get('id_redirects', 0)}

## Interpretation Rule

Cross-book rows mean only that supplied passages share a literal term or an explicit printed Scripture reference. They do not assert that the authors agree, disagree, use a term identically, or influenced one another.

`work_attributed_author` identifies an enclosing work scope only when supported by a printed byline, source title, or PDF outline heading. It does not assert that the named person wrote every editorial note, preface, footnote, or reference entry in that scope. Evidence rows therefore keep `speaker_claim` null, and footnotes/editorial/reference material are placed in the archival layer unless explicitly requested.
"""
    (OUT_DIR / "christian_kb_manifest.md").write_text(text, encoding="utf-8", newline="\n")


def nested_string_fields(value, field_name=None):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from nested_string_fields(item, key)
    elif isinstance(value, list):
        for item in value:
            yield from nested_string_fields(item, field_name)
    elif isinstance(value, str):
        yield field_name, value


def nested_field_names(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from nested_field_names(item)
    elif isinstance(value, list):
        for item in value:
            yield from nested_field_names(item)


def generated_file_records(path):
    if path.suffix == ".jsonl":
        yield from iter_jsonl(path)
    elif path.suffix == ".json":
        yield read_json(path, {})
    elif path.suffix == ".csv":
        with path.open("r", encoding="utf-8", newline="") as stream:
            yield from csv.DictReader(stream)
    else:
        yield {"document_text": path.read_text(encoding="utf-8", errors="ignore")}


def validate_global_bundle(manifest, active_source_ids):
    errors = []
    errors.extend(page_locations.validate_extension(OUT_DIR, manifest))
    source_ids = set(active_source_ids)
    all_supplied_source_ids = {
        legacy.source_id_for(path) for path in legacy.pdf_paths()
    }
    full_bundle = source_ids == all_supplied_source_ids
    evidence_ids = set()
    page_ids = set()
    registry_path = OUT_DIR / TERM_REGISTRY_FILENAME
    term_registry_rows = list(iter_jsonl(registry_path))
    registry_hash = term_registry_sha256(term_registry_rows)
    registry_terms = set()
    registered_form_keys = {}
    attestation_requests = defaultdict(list)
    attestation_page_keys = set()
    registered_form_count = 0
    active_attestation_count = 0
    registry_row_fields = {
        "term_registration_id",
        "lookup_form",
        "normalization_rule",
        "registration_basis",
        "registered_forms",
        "semantic_definition",
        "semantic_relations",
    }
    registry_form_fields = {"printed_form", "matching_rule", "attestation"}
    attestation_fields = {
        "source_id",
        "pdf_file",
        "page_number",
        "evidence_id",
        "char_start",
        "char_end",
        "page_text_sha256",
    }
    for row in term_registry_rows:
        lookup = row.get("lookup_form", "")
        if set(row) != registry_row_fields:
            errors.append(f"term_registry_nonuniform_row_schema:{lookup}")
        if not lookup or lookup in registry_terms:
            errors.append(f"term_registry_duplicate_or_empty_lookup:{lookup}")
        registry_terms.add(lookup)
        if row.get("term_registration_id") != term_registration_id_for(lookup):
            errors.append(f"term_registry_invalid_id:{lookup}")
        if row.get("normalization_rule") != "case_punctuation_and_spacing_only":
            errors.append(f"term_registry_invalid_normalization:{lookup}")
        if row.get("registration_basis") != "literal_printed_form_attested_in_supplied_pdf":
            errors.append(f"term_registry_invalid_basis:{lookup}")
        if row.get("semantic_definition") is not None or row.get("semantic_relations") is not None:
            errors.append(f"term_registry_contains_semantic_content:{lookup}")
        forms = row.get("registered_forms") or []
        if not forms:
            errors.append(f"term_registry_missing_forms:{lookup}")
        local_form_keys = set()
        for form in forms:
            registered_form_count += 1
            printed = form.get("printed_form", "")
            form_key = printed.casefold()
            if set(form) != registry_form_fields:
                errors.append(f"term_registry_nonuniform_form_schema:{lookup}:{printed}")
            if not printed or form_key in local_form_keys:
                errors.append(f"term_registry_duplicate_or_empty_form:{lookup}:{printed}")
            local_form_keys.add(form_key)
            prior_lookup = registered_form_keys.get(form_key)
            if prior_lookup and prior_lookup != lookup:
                errors.append(f"term_registry_form_has_multiple_lookups:{printed}")
            registered_form_keys[form_key] = lookup
            if mechanical_term_form(printed) != lookup:
                errors.append(f"term_registry_form_not_mechanical:{lookup}:{printed}")
            if form.get("matching_rule") != "literal_case_insensitive_with_alphanumeric_boundaries":
                errors.append(f"term_registry_invalid_matching_rule:{lookup}:{printed}")
            attestation = form.get("attestation") or {}
            if set(attestation) != attestation_fields:
                errors.append(f"term_registry_nonuniform_attestation_schema:{lookup}:{printed}")
            if attestation.get("source_id") in source_ids:
                active_attestation_count += 1
                attestation_requests[attestation.get("evidence_id", "")].append(
                    (lookup, printed, attestation)
                )
                attestation_page_keys.add(
                    (attestation.get("source_id"), attestation.get("page_number"))
                )
            elif full_bundle:
                errors.append(
                    f"term_registry_attestation_inactive_source:{lookup}:{printed}"
                )

    if not term_registry_rows:
        errors.append("term_registry_empty")
    if manifest.get("term_registry_schema_version") != TERM_REGISTRY_SCHEMA_VERSION:
        errors.append("term_registry_schema_version_mismatch")
    if manifest.get("term_registry_sha256") != registry_hash:
        errors.append("term_registry_manifest_hash_mismatch")
    if registry_hash != TERM_REGISTRY_SHA256:
        errors.append("term_registry_runtime_hash_mismatch")

    attestation_evidence = {}
    for path in sorted(OUT_DIR.glob("christian_kb_evidence_*_*.jsonl")):
        for row in iter_jsonl(path):
            evidence_ids.add(row["evidence_id"])
            if row["evidence_id"] in attestation_requests:
                attestation_evidence[row["evidence_id"]] = row
            if row.get("interpretive_summary") is not None or row.get("speaker_claim") is not None:
                errors.append(f"interpretive_evidence_field:{row['evidence_id']}")
    attestation_page_hashes = {}
    for path in sorted(OUT_DIR.glob("christian_kb_pages_*.jsonl")):
        for row in iter_jsonl(path):
            page_ids.add(row["page_id"])
            page_key = (row.get("source_id"), row.get("page_number"))
            if page_key in attestation_page_keys:
                attestation_page_hashes[page_key] = row.get("original_text_sha256")
            if "original_text" in row or "cleaned_text" in row:
                errors.append(f"unexpected_duplicate_page_text:{row['page_id']}")

    source_registry = {
        row["source_id"]: row
        for row in iter_jsonl(OUT_DIR / "christian_kb_source_registry.jsonl")
        if row.get("registry_status") == "active"
    }
    for evidence_id, requests in attestation_requests.items():
        evidence = attestation_evidence.get(evidence_id)
        for lookup, printed, attestation in requests:
            error_key = f"{lookup}:{attestation.get('source_id')}:{attestation.get('page_number')}"
            source = source_registry.get(attestation.get("source_id"))
            if not source or source.get("pdf_file") != attestation.get("pdf_file"):
                errors.append(f"term_registry_attestation_source_mismatch:{error_key}")
            if not evidence:
                errors.append(f"term_registry_attestation_missing_evidence:{error_key}")
                continue
            evidence_pages = {
                span.get("page_number")
                for span in evidence.get("source_spans", [])
                if span.get("page_number")
            } or {evidence.get("page_number")}
            if evidence.get("source_id") != attestation.get("source_id") or attestation.get("page_number") not in evidence_pages:
                errors.append(f"term_registry_attestation_evidence_scope_mismatch:{error_key}")
                continue
            start = attestation.get("char_start")
            end = attestation.get("char_end")
            evidence_start = evidence.get("char_start")
            evidence_end = evidence.get("char_end")
            if not all(isinstance(value, int) for value in (start, end, evidence_start, evidence_end)):
                errors.append(f"term_registry_attestation_invalid_offsets:{error_key}")
                continue
            if not (evidence_start <= start < end <= evidence_end):
                errors.append(f"term_registry_attestation_outside_evidence:{error_key}")
                continue
            relative_start = start - evidence_start
            relative_end = end - evidence_start
            if evidence.get("verbatim_text", "")[relative_start:relative_end] != printed:
                errors.append(f"term_registry_attestation_text_mismatch:{error_key}")
            page_key = (attestation.get("source_id"), attestation.get("page_number"))
            if attestation_page_hashes.get(page_key) != attestation.get("page_text_sha256"):
                errors.append(f"term_registry_attestation_page_hash_mismatch:{error_key}")

    for prefix, id_field in (
        ("christian_kb_term_concordance", "term_source_concordance_id"),
        ("christian_kb_scripture_concordance", "scripture_source_concordance_id"),
    ):
        for path in sorted(OUT_DIR.glob(f"{prefix}_*.jsonl")):
            for row in iter_jsonl(path):
                if row["source_id"] not in source_ids:
                    errors.append(f"unknown_source:{row[id_field]}")
                if prefix == "christian_kb_term_concordance":
                    if row.get("lookup_form") not in registry_terms:
                        errors.append(f"term_concordance_not_registered:{row[id_field]}")
                    if row.get("semantic_assertion") is not None:
                        errors.append(f"term_concordance_contains_semantic_assertion:{row[id_field]}")
                for location in row["evidence_locations"]:
                    if location["evidence_id"] not in evidence_ids:
                        errors.append(f"unknown_evidence:{row[id_field]}")
                        break
    cross_hash = hashlib.sha256()
    cross_rows = 0
    for path in sorted(OUT_DIR.glob("christian_kb_crossbook_evidence_*.jsonl")):
        for row in iter_jsonl(path):
            cross_rows += 1
            if row.get("interpretive_conclusion") is not None:
                errors.append(f"interpretive_crossbook_field:{row['crossbook_id']}")
            for item in row["source_evidence"]:
                if item["evidence_id"] not in evidence_ids:
                    errors.append(f"crossbook_unknown_evidence:{row['crossbook_id']}")
            cross_hash.update((json_dumps(row) + "\n").encode("utf-8"))
    if cross_hash.hexdigest() != manifest["crossbook_canonical_sha256"]:
        errors.append("crossbook_hash_mismatch")
    if cross_rows != manifest["counts"]["crossbook_rows"]:
        errors.append("crossbook_count_mismatch")
    term_catalog_rows = list(iter_jsonl(OUT_DIR / "christian_kb_term_catalog.jsonl"))
    if any(row.get("semantic_definition") is not None for row in term_catalog_rows):
        errors.append("term_catalog_contains_semantic_definition")
    catalog_terms = {row.get("lookup_form") for row in term_catalog_rows}
    if not catalog_terms.issubset(registry_terms):
        for lookup in sorted(catalog_terms - registry_terms):
            errors.append(f"term_catalog_lookup_not_registered:{lookup}")
    if full_bundle and catalog_terms != registry_terms:
        for lookup in sorted(registry_terms - catalog_terms):
            errors.append(f"registered_term_missing_from_full_catalog:{lookup}")
    if manifest.get("counts", {}).get("registered_literal_terms") != len(term_registry_rows):
        errors.append("term_registry_count_mismatch")
    if manifest.get("counts", {}).get("registered_literal_forms") != registered_form_count:
        errors.append("term_registry_form_count_mismatch")

    forbidden = ("claim_atoms", "topic_ids", "logic_shorthand", "related_concepts")
    for path in OUT_DIR.glob("christian_kb_*"):
        if path.is_file() and path.suffix in {".json", ".jsonl", ".csv", ".md"}:
            absolute_path_found = False
            deprecated_fields = set()
            for record in generated_file_records(path):
                if path.name != "christian_kb_manifest.json":
                    deprecated_fields.update(
                        set(forbidden).intersection(nested_field_names(record))
                    )
                for field_name, value in nested_string_fields(record):
                    if ABSOLUTE_PRIVATE_PATH_RE.search(value):
                        absolute_path_found = True
            if absolute_path_found:
                errors.append(f"absolute_path:{path.name}")
            for field in sorted(deprecated_fields):
                errors.append(f"deprecated_field:{field}:{path.name}")
    return {
        "validated_at": now_iso(),
        "schema_version": SCHEMA_VERSION,
        "builder_version": BUILDER_VERSION,
        "status": "pass" if not errors else "fail",
        "errors": errors,
        "checks": {
            "active_sources": len(source_ids),
            "evidence_ids": len(evidence_ids),
            "page_ids": len(page_ids),
            "crossbook_rows": cross_rows,
            "crossbook_hash": cross_hash.hexdigest(),
            "registered_literal_terms": len(term_registry_rows),
            "registered_literal_forms": registered_form_count,
            "active_registry_attestations_checked": active_attestation_count,
            "full_supplied_library": full_bundle,
            "deprecated_inferred_fields_absent": not any("deprecated_field:" in error for error in errors),
            "literal_term_semantics_absent": not any(
                "semantic" in error and "term_" in error for error in errors
            ),
            "term_registry_matches_catalog": not any(
                "term_catalog_lookup_not_registered:" in error
                or "registered_term_missing_from_full_catalog:" in error
                for error in errors
            ),
            "all_active_registered_forms_source_attested": not any(
                "term_registry_attestation_" in error for error in errors
            ),
            "uniform_term_registration_schema": not any(
                "term_registry_nonuniform_" in error for error in errors
            ),
            "portable_paths_only": not any("absolute_path:" in error for error in errors),
        },
    }


def build(force_rescan=False, validate_only=False):
    run_date = date.today().isoformat()
    paths = audit_paths(run_date)
    pdf_paths = legacy.pdf_paths()
    active_source_ids = [legacy.source_id_for(path) for path in pdf_paths]
    if validate_only:
        manifest = read_json(OUT_DIR / "christian_kb_manifest.json", {})
        validation = validate_global_bundle(manifest, active_source_ids)
        write_json(paths["validation"], validation)
        if validation["status"] != "pass":
            raise RuntimeError("Global validation failed: " + "; ".join(validation["errors"][:10]))
        return manifest

    baseline = legacy_snapshot()
    previous_registry = list(iter_jsonl(OUT_DIR / "christian_kb_source_registry.jsonl"))
    start_audit(paths, baseline)
    legacy_stats = baseline.get("source_stats", {})
    term_seen = defaultdict(set)
    scripture_seen = defaultdict(set)
    processed = 0
    reused = 0
    audit_entries = []

    for index, path in enumerate(pdf_paths, start=1):
        source_id = legacy.source_id_for(path)
        print(f"[{index}/{len(pdf_paths)}] source-faithful verification {source_id}", flush=True)
        signature = source_signature(path)
        if not force_rescan and package_reusable(source_id, signature):
            package = load_package(source_id)
            reused += 1
            package_status = "reused_validated_package"
        else:
            package = process_pdf(path, signature["pdf_sha256"])
            if package["validation"]["status"] != "pass":
                raise RuntimeError(f"Package validation failed for {source_id}: {package['validation']['errors'][:5]}")
            save_package(package, signature)
            processed += 1
            package_status = "reread_and_validated"

        source_terms = {row["lookup_form"] for row in package["terms"]}
        source_scripture = {
            row["canonical_reference"]
            for row in package["scripture"]
            if row["canonical_reference"]
        }
        term_links = sum(len(term_seen[key]) for key in source_terms)
        scripture_links = sum(len(scripture_seen[key]) for key in source_scripture)
        for key in source_terms:
            term_seen[key].add(source_id)
        for key in source_scripture:
            scripture_seen[key].add(source_id)

        sample_evidence = next((row for row in package["evidence"] if row["verbatim_text"].strip()), {})
        entry = {
            "source_id": source_id,
            "pdf_file": package["source"]["pdf_file"],
            "display_title": package["source"]["display_title"],
            "package_status": package_status,
            "before": legacy_stats.get(source_id, {}),
            "after": package["validation"]["counts"],
            "warning_count": len(package["validation"]["warnings"]),
            "incremental_crossbook_term_links": term_links,
            "incremental_crossbook_scripture_links": scripture_links,
            "visual_sample_pages": package["source"]["visual_sample_pages"],
            "manual_sample": {
                "evidence_id": sample_evidence.get("evidence_id", ""),
                "page_number": sample_evidence.get("page_number", 1),
            },
        }
        append_audit_entry(paths, entry)
        audit_entries.append(entry)
        del package

    registry_refresh = refresh_term_registry_from_clean_packages(active_source_ids)
    manifest = assemble_bundle(active_source_ids, previous_registry)
    write_manifest_markdown(manifest)
    validation = validate_global_bundle(manifest, active_source_ids)
    write_json(paths["validation"], validation)
    if validation["status"] != "pass":
        raise RuntimeError("Global validation failed: " + "; ".join(validation["errors"][:10]))

    summary = {
        "run_date": run_date,
        "schema_version": SCHEMA_VERSION,
        "builder_version": BUILDER_VERSION,
        "sources_verified_book_by_book": len(active_source_ids),
        "sources_reread": processed,
        "sources_reused": reused,
        "term_registry_refresh": registry_refresh,
        "incremental_crossbook_term_links_checked": sum(row["incremental_crossbook_term_links"] for row in audit_entries),
        "incremental_crossbook_scripture_links_checked": sum(row["incremental_crossbook_scripture_links"] for row in audit_entries),
        "manifest_counts": manifest["counts"],
        "global_validation": validation,
    }
    write_json(paths["summary"], summary)
    with paths["report"].open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("\n## Final Result\n\n")
        stream.write(f"- PDFs verified book by book: {len(active_source_ids)}\n")
        stream.write(f"- PDFs reread: {processed}; validated packages reused: {reused}\n")
        stream.write(f"- Clean term registry SHA-256: `{registry_refresh['term_registry_sha256']}`\n")
        stream.write(f"- Primary evidence chunks: {manifest['counts'].get('evidence_primary', 0)}\n")
        stream.write(f"- Extended evidence chunks: {manifest['counts'].get('evidence_extended', 0)}\n")
        stream.write(f"- Archival evidence chunks: {manifest['counts'].get('evidence_archival', 0)}\n")
        stream.write(f"- Cross-book evidence rows: {manifest['counts'].get('crossbook_rows', 0)}\n")
        stream.write(f"- Global validation: `{validation['status']}`\n")
        stream.write("- Deprecated inferred bundle files were removed after ID redirects were generated.\n")
    return manifest


def outline_rows_from_sections(sections):
    rows = []
    for index, section in enumerate(sections, start=1):
        match = re.search(r"_SEC_(\d+)$", section.get("section_id", ""))
        rows.append(
            {
                "order": int(match.group(1)) if match else index,
                "level": int(section.get("outline_level") or 1),
                "title": section.get("title", ""),
                "page_start": int(section.get("page_start") or 1),
            }
        )
    return sorted(rows, key=lambda row: (row["order"], row["page_start"]))


def source_attribution_record(package):
    source_attr_id = package.get("source", {}).get("source_attribution_id", "")
    if not source_attr_id:
        return None
    for row in package.get("attributions", []):
        if row.get("attribution_id") == source_attr_id:
            record = dict(row)
            record["author_role"] = "work_attribution"
            return record
    source = package.get("source", {})
    if source.get("source_author") and source.get("source_author") != "Unknown":
        return {
            "attribution_id": source_attr_id,
            "source_id": source.get("source_id", ""),
            "attributed_author": source["source_author"],
            "author_role": "work_attribution",
            "basis": source.get("source_author_basis", "explicit_source_title_scope"),
            "evidence_text": "",
            "evidence_page": None,
            "outline_path": "",
            "review_status": "mechanically_verified_exact_text",
        }
    return None


def active_metadata_for_page(active, source):
    if active:
        return {
            "work_attributed_author": active.work_attributed_author,
            "mentioned_authors": active.mentioned_authors,
            "attribution_id": active.attribution_id,
            "attribution_basis": active.attribution_basis,
            "active_heading": active.title,
            "outline_path": active.outline_path,
            "content_role": active.content_role,
            "content_role_basis": active.content_role_basis,
        }
    role, role_basis = content_role_for_heading("", "")
    source_author = source.get("source_author", "Unknown")
    return {
        "work_attributed_author": source_author,
        "mentioned_authors": [],
        "attribution_id": source.get("source_attribution_id", ""),
        "attribution_basis": source.get("source_author_basis", "unknown") if source_author != "Unknown" else "unknown",
        "active_heading": "",
        "outline_path": "",
        "content_role": role,
        "content_role_basis": role_basis,
    }


def refresh_package_attributions(package):
    source = package["source"]
    source_id = source["source_id"]
    page_count = len(package["pages"])
    outline_entries, outline_attributions = build_outline_entries(
        source_id,
        outline_rows_from_sections(package["sections"]),
        page_count,
        source.get("source_author", "Unknown"),
        source.get("source_attribution_id", ""),
        source.get("source_author_basis", "unknown"),
    )
    source_record = source_attribution_record(package)
    package["attributions"] = ([source_record] if source_record else []) + outline_attributions
    active_by_page = active_outline_entries(outline_entries, page_count)

    for page in package["pages"]:
        metadata = active_metadata_for_page(active_by_page.get(page["page_number"]), source)
        page.update(metadata)

    for row in package["evidence"]:
        active = active_by_page.get(row["page_number"])
        metadata = active_metadata_for_page(active, source)
        row.update({key: value for key, value in metadata.items() if key != "mentioned_authors"})
        row["mentioned_authors"] = remove_author_mentions(
            list(metadata["mentioned_authors"]) + unique_author_mentions(evidence_text(row)),
            row["work_attributed_author"],
        )
        row["retrieval_layer"] = evidence_layer(
            row["work_attributed_author"],
            row.get("content_role", "body"),
            row.get("text_role", TEXT_ROLE_BODY),
        )

    package["sections"] = [
        {
            "section_id": f"{source_id}_SEC_{entry.order:05d}",
            "source_id": source_id,
            "title": entry.title,
            "title_basis": "supplied_pdf_outline",
            "outline_path": entry.outline_path,
            "outline_level": entry.level,
            "page_start": entry.page_start,
            "page_end": entry.page_end,
            "work_attributed_author": entry.work_attributed_author,
            "mentioned_authors": entry.mentioned_authors,
            "attribution_id": entry.attribution_id,
            "attribution_basis": entry.attribution_basis,
            "content_role": entry.content_role,
            "content_role_basis": entry.content_role_basis,
            "interpretive_summary": None,
        }
        for entry in outline_entries
    ]
    source["authors_with_explicit_attribution"] = sorted(
        {
            row.get("work_attributed_author")
            for row in package["evidence"]
            if row.get("work_attributed_author") and row.get("work_attributed_author") != "Unknown"
        }
    )
    source["authors_with_explicit_reference"] = sorted(
        {
            author
            for row in package["evidence"]
            for author in row.get("mentioned_authors", [])
            if author and author != "Unknown"
        },
        key=str.casefold,
    )
    package["validation"] = validate_package(package)
    return package


def refresh_attribution_packages_and_reassemble(selected_source_ids=None):
    run_date = date.today().isoformat()
    paths = audit_paths(run_date)
    pdf_paths = legacy.pdf_paths()
    active_source_ids = [legacy.source_id_for(path) for path in pdf_paths]
    selected_source_ids = set(selected_source_ids or active_source_ids)
    unknown_source_ids = sorted(selected_source_ids - set(active_source_ids))
    if unknown_source_ids:
        raise RuntimeError("Unknown source id(s): " + ", ".join(unknown_source_ids))
    previous_registry = list(iter_jsonl(OUT_DIR / "christian_kb_source_registry.jsonl"))

    selected = [source_id for source_id in active_source_ids if source_id in selected_source_ids]
    for index, source_id in enumerate(selected, start=1):
        print(f"[{index}/{len(selected)}] refreshing attribution metadata {source_id}", flush=True)
        metadata = read_json(package_paths(source_id)["metadata"], {})
        package = refresh_package_attributions(load_package(source_id))
        if package["validation"]["status"] != "pass":
            raise RuntimeError(
                f"Package validation failed during attribution refresh for {source_id}: "
                + "; ".join(package["validation"]["errors"][:5])
            )
        save_package(package, metadata.get("signature", {}))
        del package

    manifest = assemble_bundle(active_source_ids, previous_registry)
    write_manifest_markdown(manifest)
    validation = validate_global_bundle(manifest, active_source_ids)
    write_json(paths["validation"], validation)
    if validation["status"] != "pass":
        raise RuntimeError(
            "Global validation failed after attribution refresh: "
            + "; ".join(validation["errors"][:10])
        )

    summary = read_json(paths["summary"], {})
    summary.update(
        {
            "schema_version": SCHEMA_VERSION,
            "builder_version": BUILDER_VERSION,
            "attribution_refresh": {
                "status": "complete",
                "sources_refreshed_from_validated_cache": len(selected),
                "selected_source_ids": selected,
                "pdfs_reread": 0,
            },
            "manifest_counts": manifest["counts"],
            "global_validation": validation,
        }
    )
    write_json(paths["summary"], summary)
    with paths["report"].open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("\n## Attribution Metadata Refresh\n\n")
        stream.write(f"- Refreshed attribution metadata for {len(selected)} cached source packages.\n")
        stream.write("- Work/source authors remain separate from mentioned or discussed authors.\n")
        stream.write("- No PDF text was reread and no evidence text was changed.\n")
        stream.write(f"- Global validation: `{validation['status']}`.\n")
    return manifest


def compact_existing_packages_and_reassemble(registry_migration=None):
    run_date = date.today().isoformat()
    paths = audit_paths(run_date)
    pdf_paths = legacy.pdf_paths()
    active_source_ids = [legacy.source_id_for(path) for path in pdf_paths]
    previous_registry = list(iter_jsonl(OUT_DIR / "christian_kb_source_registry.jsonl"))

    for index, source_id in enumerate(active_source_ids, start=1):
        print(f"[{index}/{len(active_source_ids)}] compacting validated package {source_id}", flush=True)
        metadata = read_json(package_paths(source_id)["metadata"], {})
        package = load_package(source_id)
        package["validation"] = validate_package(package)
        if package["validation"]["status"] != "pass":
            raise RuntimeError(
                f"Package validation failed during compaction for {source_id}: "
                + "; ".join(package["validation"]["errors"][:5])
            )
        save_package(package, metadata.get("signature", {}))
        del package

    manifest = assemble_bundle(active_source_ids, previous_registry)
    write_manifest_markdown(manifest)
    validation = validate_global_bundle(manifest, active_source_ids)
    write_json(paths["validation"], validation)
    if validation["status"] != "pass":
        raise RuntimeError(
            "Global validation failed after compaction: "
            + "; ".join(validation["errors"][:10])
        )

    summary = read_json(paths["summary"], {})
    summary.update(
        {
            "builder_version": BUILDER_VERSION,
            "schema_version": SCHEMA_VERSION,
            "term_registry": {
                "schema_version": TERM_REGISTRY_SCHEMA_VERSION,
                "sha256": TERM_REGISTRY_SHA256,
                "registered_terms": len(REGISTERED_TERM_ROWS),
                "registered_forms": sum(
                    len(row["registered_forms"]) for row in REGISTERED_TERM_ROWS
                ),
                "migration": registry_migration,
            },
            "representation_compaction": {
                "status": "complete",
                "source_text_removed": False,
                "evidence_coverage_changed": False,
                "index_context_deduplicated": True,
            },
            "manifest_counts": manifest["counts"],
            "global_validation": validation,
        }
    )
    write_json(paths["summary"], summary)
    with paths["report"].open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("\n## Representation Compaction\n\n")
        stream.write(f"- Final builder: `{BUILDER_VERSION}`\n")
        stream.write("- Repeated term/Scripture context was replaced with evidence IDs and offsets.\n")
        stream.write("- No source text, evidence span, occurrence, attribution, or cross-book key was removed.\n")
        stream.write(f"- Compact global validation: `{validation['status']}`.\n")
        stream.write(f"- Compact term-concordance rows: {manifest['counts'].get('term_concordance_rows', 0)}.\n")
        stream.write(f"- Compact Scripture-concordance rows: {manifest['counts'].get('scripture_concordance_rows', 0)}.\n")
        if registry_migration:
            stream.write("\n## Generic Source-Derived Term Registry\n\n")
            stream.write(
                f"- Migrated {registry_migration['registered_terms']} literal lookup forms and "
                f"{registry_migration['registered_forms']} punctuation-sensitive registered forms into one schema.\n"
            )
            stream.write("- Every registered form has an exact supplied-PDF source, page, evidence ID, character range, and page-text hash.\n")
            stream.write("- No term has a doctrine-specific registration path or validation rule.\n")
            stream.write("- Semantic definitions and semantic relations remain null.\n")
    return manifest


def reindex_term_packages_and_reassemble(registration_result=None):
    run_date = date.today().isoformat()
    paths = audit_paths(run_date)
    pdf_paths = legacy.pdf_paths()
    active_source_ids = [legacy.source_id_for(path) for path in pdf_paths]
    previous_source_registry = list(
        iter_jsonl(OUT_DIR / "christian_kb_source_registry.jsonl")
    )
    before_manifest = read_json(OUT_DIR / "christian_kb_manifest.json", {})

    for index, source_id in enumerate(active_source_ids, start=1):
        print(
            f"[{index}/{len(active_source_ids)}] reindexing registered literal terms {source_id}",
            flush=True,
        )
        metadata = read_json(package_paths(source_id)["metadata"], {})
        package = load_package(source_id)
        package["terms"] = extract_term_rows(source_id, package["evidence"])
        package["validation"] = validate_package(package)
        if package["validation"]["status"] != "pass":
            raise RuntimeError(
                f"Package validation failed during term reindexing for {source_id}: "
                + "; ".join(package["validation"]["errors"][:5])
            )
        save_package(package, metadata.get("signature", {}))
        del package

    manifest = assemble_bundle(active_source_ids, previous_source_registry)
    write_manifest_markdown(manifest)
    validation = validate_global_bundle(manifest, active_source_ids)
    write_json(paths["validation"], validation)
    if validation["status"] != "pass":
        raise RuntimeError(
            "Global validation failed after term reindexing: "
            + "; ".join(validation["errors"][:10])
        )

    summary = read_json(paths["summary"], {})
    summary.update(
        {
            "schema_version": SCHEMA_VERSION,
            "builder_version": BUILDER_VERSION,
            "term_registry_update": registration_result,
            "term_reindex": {
                "status": "complete",
                "sources_reindexed_from_validated_page_text": len(active_source_ids),
                "pdfs_reread": 0,
                "before_manifest_counts": before_manifest.get("counts", {}),
                "after_manifest_counts": manifest["counts"],
            },
            "global_validation": validation,
        }
    )
    write_json(paths["summary"], summary)
    with paths["report"].open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("\n## Generic Literal-Term Registration Update\n\n")
        if registration_result:
            stream.write(f"- Added exact registered forms: {registration_result.get('added', [])}.\n")
            stream.write(f"- Already registered forms: {registration_result.get('unchanged', [])}.\n")
        stream.write(f"- Active registry: {len(REGISTERED_TERM_ROWS)} lookup forms; SHA-256 `{TERM_REGISTRY_SHA256}`.\n")
        stream.write(f"- Reindexed all {len(active_source_ids)} source packages from validated cached page text.\n")
        stream.write("- The operation added no semantic definition, relation, doctrinal category, or external content.\n")
        stream.write(f"- Global validation: `{validation['status']}`.\n")
    return manifest


def cli():
    parser = argparse.ArgumentParser(description="Build a source-faithful theology knowledge base from supplied PDFs.")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--force-rescan", action="store_true", help="Reread every PDF instead of reusing validated source packages.")
    actions.add_argument("--validate-only", action="store_true", help="Validate the current generated bundle without rebuilding it.")
    actions.add_argument(
        "--compact-existing",
        action="store_true",
        help="Compact validated source packages and rebuild indexes without rereading PDFs.",
    )
    actions.add_argument(
        "--initialize-term-registry",
        action="store_true",
        help="Migrate all currently indexed, source-attested literal forms into the generic term registry.",
    )
    actions.add_argument(
        "--register-term",
        action="append",
        default=[],
        metavar="LITERAL_TEXT",
        help="Register exact literal text only after finding a supplied-PDF attestation; may be repeated.",
    )
    actions.add_argument(
        "--register-terms-file",
        metavar="PATH",
        help=f"Register exact literal terms listed in a JSON string array; defaults are tracked in {TERM_REQUESTS_FILENAME}.",
    )
    actions.add_argument(
        "--reindex-terms",
        action="store_true",
        help="Rebuild term occurrences from validated cached page text using the active registry.",
    )
    actions.add_argument(
        "--refresh-attributions",
        action="store_true",
        help="Rebuild author attribution and mentioned-author metadata from validated cached source packages.",
    )
    parser.add_argument(
        "--refresh-attribution-source",
        action="append",
        default=[],
        metavar="SOURCE_ID",
        help="Source id to refresh with --refresh-attributions; may be repeated. Defaults to all active sources.",
    )
    args = parser.parse_args()
    if args.initialize_term_registry:
        migration = initialize_term_registry_from_packages()
        compact_existing_packages_and_reassemble(registry_migration=migration)
        print(json_dumps(migration, pretty=True))
    elif args.register_term:
        result = register_source_attested_terms(args.register_term)
        if result["added"]:
            reindex_term_packages_and_reassemble(registration_result=result)
        else:
            build(validate_only=True)
        print(json_dumps(result, pretty=True))
    elif args.register_terms_file:
        result = register_source_attested_terms(read_term_requests(args.register_terms_file))
        if result["added"]:
            reindex_term_packages_and_reassemble(registration_result=result)
        else:
            build(validate_only=True)
        print(json_dumps(result, pretty=True))
    elif args.reindex_terms:
        reindex_term_packages_and_reassemble()
    elif args.refresh_attributions:
        refresh_attribution_packages_and_reassemble(args.refresh_attribution_source or None)
    elif args.compact_existing:
        compact_existing_packages_and_reassemble()
    else:
        build(force_rescan=args.force_rescan, validate_only=args.validate_only)


if __name__ == "__main__":
    cli()
