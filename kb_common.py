import csv
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[1]
OUT_DIR = WORKSPACE / "knowledge_base"
CACHE_DIR = OUT_DIR / "_source_cache"
RAW_DOCUMENTS_DIR = WORKSPACE / "raw_documents"
AUTHOR_REGISTRY_PATH = OUT_DIR / "author_registry.json"


def compact_whitespace(value):
    return re.sub(r"\s+", " ", value or "").strip()


def sha256_hex(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def portable_path(path, workspace=WORKSPACE):
    return Path(path).resolve().relative_to(Path(workspace).resolve()).as_posix()


def json_dumps(row, pretty=False):
    if pretty:
        return json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True)
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def write_json(path, row):
    Path(path).write_text(
        json_dumps(row, pretty=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_jsonl(path, rows):
    with Path(path).open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json_dumps(row) + "\n")


def iter_jsonl(path):
    path = Path(path)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if line:
                yield json.loads(line)


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def context_excerpt(text, start, end, radius=220):
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return text[left:right], left, right


def word_count(text):
    return len(re.findall(r"[^\W_]+(?:['’][^\W_]+)?", text or "", re.UNICODE))


def csv_ready(row):
    ready = {}
    for key, value in row.items():
        if isinstance(value, (dict, list)):
            ready[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            ready[key] = value
    return ready


def write_csv(path, rows, fieldnames):
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(csv_ready(row))


def load_author_registry(path=AUTHOR_REGISTRY_PATH):
    if not path.exists():
        raise FileNotFoundError(f"Missing author registry: {path}")
    rows = json.loads(path.read_text(encoding="utf-8"))
    aliases = {}
    folder_hints = []
    for index, row in enumerate(rows, start=1):
        canonical = row.get("canonical_author", "").strip()
        if not canonical:
            raise ValueError(f"Author registry row {index} has no canonical_author")
        author_aliases = [str(value).strip() for value in row.get("aliases", []) if str(value).strip()]
        if not author_aliases:
            raise ValueError(f"Author registry row {index} has no aliases")
        aliases[canonical] = author_aliases
        for hint in row.get("folder_hints", []):
            hint = str(hint).strip()
            if hint:
                folder_hints.append((hint.casefold(), canonical))
    folder_hints.sort(key=lambda item: len(item[0]), reverse=True)
    return aliases, folder_hints


AUTHOR_ALIASES, AUTHOR_FOLDER_HINTS = load_author_registry()


BIBLE_BOOKS = [
    ("Genesis", ["Genesis", "Gen"]),
    ("Exodus", ["Exodus", "Exod", "Ex"]),
    ("Leviticus", ["Leviticus", "Lev"]),
    ("Numbers", ["Numbers", "Num"]),
    ("Deuteronomy", ["Deuteronomy", "Deut"]),
    ("Joshua", ["Joshua", "Josh"]),
    ("Judges", ["Judges", "Judg"]),
    ("Ruth", ["Ruth"]),
    ("1 Samuel", ["1 Samuel", "I Samuel", "1 Sam", "I Sam"]),
    ("2 Samuel", ["2 Samuel", "II Samuel", "2 Sam", "II Sam"]),
    ("1 Kings", ["1 Kings", "I Kings", "1 Kgs", "I Kgs"]),
    ("2 Kings", ["2 Kings", "II Kings", "2 Kgs", "II Kgs"]),
    ("1 Chronicles", ["1 Chronicles", "I Chronicles", "1 Chron", "I Chron"]),
    ("2 Chronicles", ["2 Chronicles", "II Chronicles", "2 Chron", "II Chron"]),
    ("Ezra", ["Ezra"]),
    ("Nehemiah", ["Nehemiah", "Neh"]),
    ("Esther", ["Esther", "Esth"]),
    ("Job", ["Job"]),
    ("Psalms", ["Psalm", "Psalms", "Ps", "Psa"]),
    ("Proverbs", ["Proverbs", "Prov"]),
    ("Ecclesiastes", ["Ecclesiastes", "Eccl"]),
    ("Song of Songs", ["Song of Songs", "Song", "Canticles"]),
    ("Isaiah", ["Isaiah", "Isa"]),
    ("Jeremiah", ["Jeremiah", "Jer"]),
    ("Lamentations", ["Lamentations", "Lam"]),
    ("Ezekiel", ["Ezekiel", "Ezek"]),
    ("Daniel", ["Daniel", "Dan"]),
    ("Hosea", ["Hosea", "Hos"]),
    ("Joel", ["Joel"]),
    ("Amos", ["Amos"]),
    ("Obadiah", ["Obadiah", "Obad"]),
    ("Jonah", ["Jonah"]),
    ("Micah", ["Micah", "Mic"]),
    ("Nahum", ["Nahum", "Nah"]),
    ("Habakkuk", ["Habakkuk", "Hab"]),
    ("Zephaniah", ["Zephaniah", "Zeph"]),
    ("Haggai", ["Haggai", "Hag"]),
    ("Zechariah", ["Zechariah", "Zech"]),
    ("Malachi", ["Malachi", "Mal"]),
    ("Matthew", ["Matthew", "Matt", "Mt"]),
    ("Mark", ["Mark", "Mk"]),
    ("Luke", ["Luke", "Lk"]),
    ("John", ["John", "Jn"]),
    ("Acts", ["Acts"]),
    ("Romans", ["Romans", "Rom"]),
    ("1 Corinthians", ["1 Corinthians", "I Corinthians", "1 Cor", "I Cor"]),
    ("2 Corinthians", ["2 Corinthians", "II Corinthians", "2 Cor", "II Cor"]),
    ("Galatians", ["Galatians", "Gal"]),
    ("Ephesians", ["Ephesians", "Eph"]),
    ("Philippians", ["Philippians", "Phil"]),
    ("Colossians", ["Colossians", "Col"]),
    ("1 Thessalonians", ["1 Thessalonians", "I Thessalonians", "1 Thess", "I Thess"]),
    ("2 Thessalonians", ["2 Thessalonians", "II Thessalonians", "2 Thess", "II Thess"]),
    ("1 Timothy", ["1 Timothy", "I Timothy", "1 Tim", "I Tim"]),
    ("2 Timothy", ["2 Timothy", "II Timothy", "2 Tim", "II Tim"]),
    ("Titus", ["Titus"]),
    ("Philemon", ["Philemon", "Philem"]),
    ("Hebrews", ["Hebrews", "Heb"]),
    ("James", ["James", "Jas"]),
    ("1 Peter", ["1 Peter", "I Peter", "1 Pet", "I Pet"]),
    ("2 Peter", ["2 Peter", "II Peter", "2 Pet", "II Pet"]),
    ("1 John", ["1 John", "I John", "1 Jn", "I Jn"]),
    ("2 John", ["2 John", "II John", "2 Jn", "II Jn"]),
    ("3 John", ["3 John", "III John", "3 Jn", "III Jn"]),
    ("Jude", ["Jude"]),
    ("Revelation", ["Revelation", "Rev", "Apocalypse"]),
]


BOOK_CHAPTER_MAX = {
    "Genesis": 50, "Exodus": 40, "Leviticus": 27, "Numbers": 36, "Deuteronomy": 34,
    "Joshua": 24, "Judges": 21, "Ruth": 4, "1 Samuel": 31, "2 Samuel": 24,
    "1 Kings": 22, "2 Kings": 25, "1 Chronicles": 29, "2 Chronicles": 36,
    "Ezra": 10, "Nehemiah": 13, "Esther": 10, "Job": 42, "Psalms": 150,
    "Proverbs": 31, "Ecclesiastes": 12, "Song of Songs": 8, "Isaiah": 66,
    "Jeremiah": 52, "Lamentations": 5, "Ezekiel": 48, "Daniel": 12, "Hosea": 14,
    "Joel": 3, "Amos": 9, "Obadiah": 1, "Jonah": 4, "Micah": 7, "Nahum": 3,
    "Habakkuk": 3, "Zephaniah": 3, "Haggai": 2, "Zechariah": 14, "Malachi": 4,
    "Matthew": 28, "Mark": 16, "Luke": 24, "John": 21, "Acts": 28, "Romans": 16,
    "1 Corinthians": 16, "2 Corinthians": 13, "Galatians": 6, "Ephesians": 6,
    "Philippians": 4, "Colossians": 4, "1 Thessalonians": 5, "2 Thessalonians": 3,
    "1 Timothy": 6, "2 Timothy": 4, "Titus": 3, "Philemon": 1, "Hebrews": 13,
    "James": 5, "1 Peter": 5, "2 Peter": 3, "1 John": 5, "2 John": 1,
    "3 John": 1, "Jude": 1, "Revelation": 22,
}
BOOK_VERSE_MAX = defaultdict(lambda: 90, {"Psalms": 176})

BOOK_ALIAS_TO_CANON = {}
for canon, aliases in BIBLE_BOOKS:
    for alias in aliases:
        BOOK_ALIAS_TO_CANON[alias.lower().replace(".", "")] = canon

BOOK_PATTERN = "|".join(
    sorted(
        (re.escape(alias) for _, aliases in BIBLE_BOOKS for alias in aliases),
        key=len,
        reverse=True,
    )
)
SCRIPTURE_RE = re.compile(
    rf"\b({BOOK_PATTERN})\.?\s+([0-9]{{1,3}}|[ivxlcdmIVXLCDM]{{1,8}})[.:]\s*([0-9]{{1,3}})(?:\s*[-–]\s*([0-9]{{1,3}}))?",
    re.IGNORECASE,
)


def ascii_clean(value):
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.replace("\x00", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def slug(value, max_len=80):
    value = ascii_clean(value).lower()
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return (value[:max_len].rstrip("_") or "unit")


def raw_relative_parts(path):
    try:
        return path.resolve().relative_to(RAW_DOCUMENTS_DIR.resolve()).parts
    except ValueError:
        return path.resolve().relative_to(WORKSPACE.resolve()).parts


def raw_folder_text(path):
    parts = raw_relative_parts(path)
    folders = parts[:-1] or (path.parent.name,)
    return " / ".join(folders).lower()


def source_id_for(path):
    folder = raw_folder_text(path)
    stem = path.stem.lower()
    if "ante-nicene" in folder:
        return f"ANF_{stem[-2:]}"
    if "nicene and post-nicene" in folder:
        if stem.startswith("npnf1"):
            return f"NPNF1_{stem[-2:]}"
        return f"NPNF2_{stem[-2:]}"
    if "martin luther" in folder:
        return f"LUTHER_{slug(stem).upper()}"
    if "john calvin" in folder:
        return f"CALVIN_{slug(stem).upper()}"
    if "thomas aquinas" in folder:
        return f"AQUINAS_{slug(stem).upper()}"
    parts = raw_relative_parts(path)
    fallback_parts = (*parts[:-1], path.stem)
    return slug(" ".join(fallback_parts)).upper()


def infer_collection(path):
    relative = raw_relative_parts(path)
    return relative[0] if len(relative) > 1 else path.parent.name


def folder_author(path):
    folder = raw_folder_text(path)
    for hint, author in AUTHOR_FOLDER_HINTS:
        if hint in folder:
            return author
    return ""


def _author_folder_key(value):
    """Normalize a folder name for exact canonical/alias matching."""
    return re.sub(r"[^a-z0-9]+", "", compact_whitespace(value).casefold())


def author_from_folder(path):
    """Resolve a non-anthology first-level raw_documents folder to one author.

    The two Fathers collections intentionally contain many works and are not
    author folders, so they return an empty result for downstream evidence
    discovery to handle.
    """
    parts = raw_relative_parts(path)
    if not parts:
        return ""
    root_folder = parts[0]
    if _author_folder_key(root_folder) in {
        "antenicenefathers",
        "niceneandpostnicenefathers",
    }:
        return ""
    folder_key = _author_folder_key(root_folder)
    matches = []
    for canonical, aliases in AUTHOR_ALIASES.items():
        names = [canonical, *aliases]
        if any(_author_folder_key(name) == folder_key for name in names):
            matches.append(canonical)
    matches = sorted(set(matches), key=str.casefold)
    if len(matches) > 1:
        raise ValueError(f"Raw author folder resolves ambiguously: {root_folder!r} -> {matches}")
    return matches[0] if matches else ""


def roman_to_int(value):
    if value.isdigit():
        return int(value)
    total = 0
    previous = 0
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    for char in reversed(value.lower()):
        current = values.get(char, 0)
        if current < previous:
            total -= current
        else:
            total += current
            previous = current
    return total


def valid_scripture_ref(book, chapter, verse_start, verse_end=None):
    if not book or chapter < 1 or chapter > BOOK_CHAPTER_MAX.get(book, 0):
        return False
    if verse_start < 1 or verse_start > BOOK_VERSE_MAX[book]:
        return False
    if verse_end is not None and (verse_end < verse_start or verse_end > BOOK_VERSE_MAX[book]):
        return False
    return True


def pdf_paths():
    if not RAW_DOCUMENTS_DIR.exists():
        return []
    return sorted(
        (path for path in RAW_DOCUMENTS_DIR.rglob("*.pdf") if path.is_file()),
        key=lambda path: path.relative_to(RAW_DOCUMENTS_DIR).as_posix().casefold(),
    )
