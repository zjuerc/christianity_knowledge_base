#!/usr/bin/env python3
"""Term discovery and anchor utilities for the Christian KB."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import kb_common as legacy
import source_faithful_kb as kb

ANCHOR_SCHEMA_VERSION = "1.0.0"
DEFAULT_ANCHOR_PATH = kb.OUT_DIR / "term_discovery_anchors.json"
DEFAULT_REVIEW_PATH = kb.OUT_DIR / "term_discovery_anchor_review.csv"
USER_AGENT = "ChristianKBTermDiscovery/1.0 (+local research workflow)"

SOURCE_CONFIGS = [
    {
        "source_id": "biblegateway_topical_index",
        "label": "BibleGateway Dictionary of Bible Themes table of contents",
        "url": "https://www.biblegateway.com/resources/dictionary-of-bible-themes/toc",
        "extractor": "biblegateway_themes",
        "fetch_mode": "all",
        "fetch_urls": [
            "https://www.biblegateway.com/resources/dictionary-of-bible-themes/toc",
            "https://www.biblegateway.com/resources/dictionary-of-bible-themes/1000-God",
            "https://www.biblegateway.com/resources/dictionary-of-bible-themes/2000-Jesus-Christ",
            "https://www.biblegateway.com/resources/dictionary-of-bible-themes/3000-Holy-Spirit",
            "https://www.biblegateway.com/resources/dictionary-of-bible-themes/4000-Creation",
            "https://www.biblegateway.com/resources/dictionary-of-bible-themes/5000-Humanity",
            "https://www.biblegateway.com/resources/dictionary-of-bible-themes/6000-Sin-salvation",
            "https://www.biblegateway.com/resources/dictionary-of-bible-themes/7000-Gods-people",
            "https://www.biblegateway.com/resources/dictionary-of-bible-themes/8000-life-believer",
            "https://www.biblegateway.com/resources/dictionary-of-bible-themes/9000-Last-things",
        ],
    },
    {
        "source_id": "torrey_new_topical_textbook",
        "label": "Torrey's New Topical Textbook topical contents",
        "url": "https://ccel.org/ccel/torrey/ttt.toc.html",
        "extractor": "torrey",
        "fetch_urls": [
            "https://abv.hristianski.net/plib/CCEL/T/TORREY/TTT/INDEX.HTM",
            "https://ccel.org/ccel/torrey/ttt.toc.html",
        ],
    },
    {
        "source_id": "new_advent_catholic_encyclopedia",
        "label": "New Advent Catholic Encyclopedia A-Z index",
        "url": "https://www.newadvent.org/cathen/03712a.htm",
        "extractor": "new_advent",
        "fetch_urls": [
            f"https://www.newadvent.org/cathen/{letter}-ce.htm"
            for letter in "abcdefghijklmnopqrstuvwxyz"
        ],
    },
]

ROMAN_NUMERAL_RE = re.compile(r"^[ivxlcdm]+$", re.IGNORECASE)
CHAPTER_PREFIX_RE = re.compile(
    r"^\s*chapter\s+(?:[ivxlcdm]+|\d+)\.?\s*(?:\|\s*)?", re.IGNORECASE
)
LEADING_NUMBER_RE = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s+")
TAG_RE = re.compile(r"<[^>]+>")

ORDINAL_WORDS = {
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
    "ninth", "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth",
    "fifteenth", "sixteenth", "seventeenth", "eighteenth", "nineteenth",
    "twentieth", "final",
}

GENERIC_REJECT_TERMS = {
    "about", "abstract", "academy", "article", "articles", "bibliography",
    "book", "books", "chapter", "chapters", "commentary", "contents",
    "definition", "description", "editor", "feature", "features", "history",
    "index", "introduction", "letter", "letters", "method", "methods", "note",
    "notes", "page", "part", "parts", "preface", "question", "section",
    "sections", "source", "study", "title", "translator", "treatise", "volume",
    "whether", "concerning", "things", "thing", "one", "two", "three", "four",
    "five", "six", "seven", "eight", "nine", "ten",
}

STRUCTURAL_PHRASES = {
    "table of contents", "title page", "index of scripture references",
    "please help support", "more information", "search", "home", "library",
    "copyright", "further reading", "contents", "introduction",
}

BIOGRAPHY_MARKERS = {
    "abbot", "abbess", "archbishop", "biographical", "biography", "bishop",
    "born", "cardinal", "carmelite", "composer", "convert to", "died",
    "dominican", "founded", "founder", "friar", "jesuit", "martyr",
    "missionary", "monk", "nun", "pope", "priest", "reigned", "saint",
    "theologian", "widow",
}

GEOGRAPHY_MARKERS = {
    "abbey", "archdiocese", "city", "county", "diocese", "province", "see",
    "suffragan", "titular", "university", "geography", "kingdom of belgium",
    "kingdom of prussia", "kingdom of rumania", "located", "peninsular",
    "river", "territory",
}

DOCTRINE_KEYWORDS = {
    "absolution", "acquittal", "adoption", "adoptionism", "after life",
    "afterlife", "agnosticism", "angel", "angels", "antinomianism",
    "apologetics", "apostasy", "arminianism", "ascension", "atheism",
    "atonement", "beatific vision",
    "blessing", "calling", "calvinism", "christ", "christianity",
    "christology", "condemnation", "creation", "creator", "creed", "death",
    "decrees of god", "deism", "deity", "demon", "demons", "devil",
    "divine attributes", "divine nature", "divinity", "doctrine", "dogma",
    "dogmatic", "dualism", "election", "eschatology", "eternal life",
    "eternity", "expiation", "faith", "fall", "fall of man",
    "fallen angels", "final judgment", "final judgement", "foreknowledge",
    "forgiveness", "free will", "glory", "god", "goodness", "grace",
    "heaven", "hell", "holiness", "holy spirit", "immortality",
    "immutability", "incarnation", "judgement", "judgements", "judgment",
    "judgments", "justification", "knowledge of god", "last things", "love",
    "mercy", "miracle", "miracles", "omnipotence", "omnipresence",
    "omniscience", "original sin", "pardon", "paradise", "parousia",
    "person of christ", "pietism", "pneumatology", "predestination",
    "providence", "punishment", "purgatory", "reason", "reconciliation",
    "redemption", "regeneration", "remission", "repentance", "reprobation",
    "resurrection", "revelation", "righteousness", "salvation",
    "sanctification", "satan", "satisfaction", "sheol", "soul",
    "soteriology", "spirit", "spirits", "spiritual death",
    "spirituality of god", "theism", "theodicy", "theological anthropology",
    "theology", "thomism", "transubstantiation", "trinity", "truth",
    "unbelief", "unity of god", "will of god", "wrath",
}

BIBLICAL_THEOLOGY_KEYWORDS = {
    "abba", "abaddon", "abraham's bosom", "altar", "altar of burnt offering",
    "altar of incense", "altars", "angel of the lord", "antichrist",
    "apocrypha", "apostles", "ark", "ark of the covenant", "avenger of blood",
    "blood", "blood of christ", "book of life", "book of the law",
    "burning bush", "burnt offering", "cherubim", "circumcision",
    "cloud of glory", "command", "commandment", "commandments", "commands",
    "covenant", "covenants", "day of atonement", "drink offering", "ephod",
    "feast", "feasts", "feasts and festivals", "fellowship offering",
    "firstborn", "firstfruits", "gentiles", "gospel", "heave offering",
    "high priest", "holy of holies", "incense", "jews and gentiles",
    "jubilee", "kingdom of god", "kinsman redeemer", "lamb of god", "law",
    "law of god", "manna", "meat offerings", "mercy seat", "messiah",
    "mosaic law", "nazarites", "offering", "offerings", "oracles",
    "ordinances", "parables", "paschal lamb", "passover", "pentecost",
    "priesthood", "prophecy", "prophet", "prophets", "sabbath", "sacrifice",
    "sacrifices", "sanctuary", "scapegoat", "scripture", "scriptures",
    "seraphim", "sermon on the mount", "shepherd", "shewbread", "son of god",
    "tabernacle", "temple", "tithe", "tithes", "torah", "tree of life",
    "trespass offering", "types", "urim and thummim", "vision", "visions",
    "word of god",
}

SPIRITUAL_PRACTICE_KEYWORDS = {
    "abandonment", "abstinence", "adoration", "alms", "almsgiving", "amen",
    "anointing", "ascetic", "asceticism", "asking", "assurance",
    "backsliding", "baptism", "benediction", "benedictions", "blessed",
    "blessing", "calling", "charity", "comfort", "commitment", "communion",
    "confession", "consecration", "contemplation", "contrition", "conversion",
    "dedication", "devotion", "discipleship", "doxology", "edification",
    "fasting", "fellowship", "foot washing", "forgiveness", "hospitality",
    "intercession", "joy", "kneeling", "liberality", "liturgy", "meditation",
    "obedience", "praise", "penance", "perseverance", "prayer", "repentance",
    "resignation", "sacrament", "sacraments", "self denial",
    "self discipline", "self examination", "servanthood", "simplicity",
    "sincerity", "spiritual", "steadfastness", "thanksgiving", "temptation",
    "trust", "vows", "waiting", "watchfulness", "worship", "zeal",
}

ETHICS_KEYWORDS = {
    "abuse", "addiction", "adultery", "affection", "affections", "affliction",
    "afflictions", "ambition", "anger", "appetite", "arrogance", "avarice",
    "bitterness", "blamelessness", "blasphemy", "boasting", "boldness",
    "bribery", "carelessness", "chastity", "compassion", "conceit",
    "conscience", "contempt", "contentment", "control", "corruption",
    "courage", "coveting", "covetousness", "cowardice", "crime", "deceit",
    "defilement", "despair", "diligence", "divination", "divorce",
    "drunkenness", "envy", "evil", "exploitation", "extortion",
    "faithfulness", "faithlessness", "false witness", "favouritism", "fear",
    "flattery", "folly", "forgiveness of injuries", "fortitude", "gluttony",
    "good works", "hatred", "homicide", "hope", "humility", "hypocrisy",
    "hypocrites", "idleness", "idolatry", "indifference", "ingratitude",
    "injustice", "insults", "intolerance", "irreverence", "jealousy",
    "judging others", "justice", "kidnapping", "kindness", "love", "lying",
    "malice", "meekness", "mercy", "moral", "morality", "murder",
    "murmuring", "obedience", "oppression", "patience", "perjury",
    "persecution", "poverty", "pride", "procrastination", "prudence",
    "purity", "revenge", "reviling", "scoffing", "self control",
    "self deception", "self indulgence", "self respect", "selfishness",
    "sensitivity", "sexuality", "shame", "slander", "sloth", "sobriety",
    "strife", "swearing falsely", "temperance", "theft", "trustworthiness",
    "unfaithfulness", "unforgiveness", "ungodliness", "unkindness",
    "unselfishness", "uprightness", "usury", "vanity", "vice", "vices",
    "violence", "virtue", "virtues", "vulgarity",
}

CHURCH_KEYWORDS = {
    "absolution", "agape", "anabaptists", "anaphora", "anathema",
    "apostolic", "apostolic succession", "baptism", "bishop", "bishops",
    "canon law", "canonization", "catechism", "catechumen", "catholic",
    "church", "churching", "communion", "confirmation", "council", "deacon",
    "deacons", "diaconate", "ecclesiology", "eucharist", "excommunication",
    "heresies", "heresy", "hierarchy", "holy orders", "indulgence",
    "indulgences", "liturgy", "mass", "minister", "ministers",
    "monasticism", "ordination", "orthodoxy", "overseers", "penance",
    "presbyterianism", "priest", "priesthood", "priests", "protestantism",
    "rite", "rites", "sacrament", "sacraments", "schism", "synagogues",
    "synod", "transubstantiation", "vestments",
}

CLASS_KEYWORDS = {
    "doctrine": DOCTRINE_KEYWORDS,
    "biblical_theology": BIBLICAL_THEOLOGY_KEYWORDS,
    "spiritual_practice": SPIRITUAL_PRACTICE_KEYWORDS,
    "ethics": ETHICS_KEYWORDS,
    "church_life": CHURCH_KEYWORDS,
}

CORE_ANCHORS = {
    "atonement", "baptism", "christ", "christology", "church", "creation",
    "election", "faith", "god", "grace", "holy spirit", "incarnation",
    "justification", "providence", "redemption", "regeneration", "repentance",
    "resurrection", "revelation", "salvation", "sanctification", "sin",
    "soteriology", "trinity",
}

SINGLE_WORD_CLASS_KEYWORDS = {
    keyword
    for keywords in CLASS_KEYWORDS.values()
    for keyword in keywords
    if " " not in keyword
}

EXPLICIT_SINGLE_WORD_KEEP = CORE_ANCHORS | SINGLE_WORD_CLASS_KEYWORDS | {
    "abaddon", "abandonment", "absolution", "abstinence", "adoration",
    "adoption", "adultery", "advent", "agape", "almsgiving", "ambition", "amen",
    "angelus", "angels", "anger", "antichrist", "apostasy", "apostles",
    "asceticism", "assurance", "backsliding", "blasphemy", "charity",
    "chastity", "communion", "confession", "conscience", "contemplation",
    "contrition", "conversion", "covenant", "devotion", "envy", "evil",
    "fasting", "gospel", "heresy", "hope", "humility", "idolatry", "liturgy",
    "love", "martyrdom", "mercy", "morality", "obedience", "penance",
    "prayer", "sabbath", "sacrament", "sacraments", "scripture", "temperance",
    "temptation", "virtue", "virtues", "worship",
}

ALTAR_TOPIC_ALLOWLIST = {
    "altar", "altars", "altar in liturgy", "altars in scripture",
    "altar of burnt offering", "altar of incense", "altar of repose",
}

LITURGICAL_OBJECT_NOISE = {
    "bell", "breadbox", "breadboxes", "breads", "candle", "candles",
    "candlestick", "candlesticks", "canopy", "cards", "carpets", "cloths",
    "crucifix", "curtain", "frontal", "lamp", "lanterns", "ledge", "linens",
    "protector", "rail", "side", "steps", "stole", "tomb", "vase", "vessels",
    "wine", "spoons",
}

SACRED_OBJECT_NOISE = {
    "breadbox", "breadboxes", "candlestick", "candlesticks", "carpets",
    "curtain", "lanterns", "ledge", "protector", "spoons", "stole", "vase",
}

INSTITUTIONAL_NOISE_WORDS = {
    "administrator", "advocates", "association", "brothers", "camera", "club",
    "college", "confraternity", "congregation", "congregations", "curia",
    "executor", "expeditors", "extension", "institute", "knights", "legion",
    "majesty", "mission", "prefect", "prefecture", "preacher", "property",
    "regiminis", "relief", "servitutis", "society", "sisters", "tribunal",
    "tribunals", "university", "vicariate", "visit", "visitation", "visitors",
}

SITE_NAVIGATION_TERMS = {
    "disable scripture popups", "scripture engagement", "statement of faith",
    "privacy policy", "terms of use",
}


@dataclass
class ExtractedCandidate:
    source_id: str
    source_label: str
    source_url: str
    raw_label: str
    description: str = ""


class VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self.skip_depth += 1
        if tag in {"br", "p", "li", "tr", "h1", "h2", "h3", "h4", "h5"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1
        if tag in {"p", "li", "tr", "h1", "h2", "h3", "h4", "h5"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)

    def lines(self):
        text = "".join(self.parts)
        rows = []
        for line in text.splitlines():
            line = compact(line)
            if line:
                rows.append(line)
        return rows


class LinkTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._href = None
        self._text = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self.skip_depth += 1
        if tag == "a" and not self.skip_depth:
            self._href = dict(attrs).get("href", "")
            self._text = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1
        if tag == "a" and self._href is not None:
            label = compact("".join(self._text))
            if label:
                self.links.append((label, self._href))
            self._href = None
            self._text = []

    def handle_data(self, data):
        if self._href is not None and not self.skip_depth:
            self._text.append(data)


def compact(value):
    value = unescape(value or "").replace("\xa0", " ")
    value = value.replace("\u2019", "'").replace("\u2018", "'")
    value = value.replace("\u201c", '"').replace("\u201d", '"')
    value = value.replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", value).strip()


def strip_tags(value):
    return compact(TAG_RE.sub(" ", value or ""))


def visible_lines(html):
    parser = VisibleTextParser()
    parser.feed(html)
    return parser.lines()


def link_texts(html):
    parser = LinkTextParser()
    parser.feed(html)
    return parser.links


def fetch_url(url, timeout=30):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def fetch_first(config, source_status):
    urls = config.get("fetch_urls") or [config["url"]]
    errors = []
    for url in urls:
        try:
            html = fetch_url(url)
            source_status["fetch_url"] = url
            source_status["fetch_status"] = "ok"
            return [(url, html)]
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            errors.append(f"{url}: {exc}")
    source_status["fetch_status"] = "failed"
    source_status["fetch_errors"] = errors
    return []


def fetch_all(config, source_status):
    rows = []
    errors = []
    for url in config.get("fetch_urls") or [config["url"]]:
        try:
            rows.append((url, fetch_url(url)))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            errors.append(f"{url}: {exc}")
    source_status["fetched_pages"] = len(rows)
    source_status["fetch_status"] = "ok" if rows else "failed"
    if errors:
        source_status["fetch_errors"] = errors
    return rows


def normalize_inverted_label(label):
    label = compact(label).strip(" .;:")
    if "," not in label:
        return label
    head, tail = [part.strip(" .;:") for part in label.split(",", 1)]
    tail_norm = tail.casefold()
    if tail_norm in {"the", "a", "an"}:
        return head
    if tail_norm.startswith(("and ", "of ", "under ", "in ", "with ", "for ", "to ", "from ")):
        return f"{head} {tail}"
    if tail and len(tail.split()) <= 3:
        return f"{tail} {head}"
    return f"{head} {tail}"


def display_anchor_for_label(label):
    label = compact(label)
    label = CHAPTER_PREFIX_RE.sub("", label)
    label = LEADING_NUMBER_RE.sub("", label)
    label = re.sub(r"\s*\|\s*", " ", label)
    label = re.sub(r"\s+-\s+.*$", "", label)
    label = normalize_inverted_label(label)
    label = re.sub(r"^\s*(the|a|an)\s+", "", label, flags=re.IGNORECASE)
    label = re.sub(r"\s*\((?:see|cf\.?).*?\)\s*$", "", label, flags=re.IGNORECASE)
    label = label.replace("/", " ")
    return compact(label).strip(" .;:-")


def normalized_anchor(label):
    return kb.mechanical_term_form(display_anchor_for_label(label))


def normalized_phrase_pattern(phrase):
    phrase = kb.mechanical_term_form(phrase)
    if not phrase:
        return None
    return re.compile(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])")


def phrase_in_text(phrase, text):
    normalized = kb.mechanical_term_form(text)
    pattern = normalized_phrase_pattern(phrase)
    if pattern is None:
        return False
    return pattern.search(normalized) is not None


CLASS_KEYWORD_PATTERNS = {
    class_name: [
        (keyword, normalized_phrase_pattern(keyword))
        for keyword in keywords
    ]
    for class_name, keywords in CLASS_KEYWORDS.items()
}


def author_terms():
    terms = set()
    for canonical, aliases in legacy.AUTHOR_ALIASES.items():
        terms.add(kb.mechanical_term_form(canonical))
        terms.update(kb.mechanical_term_form(alias) for alias in aliases)
    return {term for term in terms if term}


def bible_book_terms():
    terms = set()
    for canon, aliases in legacy.BIBLE_BOOKS:
        terms.add(kb.mechanical_term_form(canon))
        terms.update(kb.mechanical_term_form(alias) for alias in aliases)
    return {term for term in terms if term}


AUTHOR_TERMS = author_terms()
BIBLE_BOOK_TERMS = bible_book_terms()


def classify_candidate(normalized, description):
    text = f"{normalized} {kb.mechanical_term_form(description)}"
    matches_by_class = {}
    for class_name, patterns in CLASS_KEYWORD_PATTERNS.items():
        matches = sorted(
            keyword for keyword, pattern in patterns
            if pattern is not None and pattern.search(text)
        )
        if matches:
            matches_by_class[class_name] = matches
    if not matches_by_class:
        return "", []
    priority = ["doctrine", "biblical_theology", "spiritual_practice", "church_life", "ethics"]
    for class_name in priority:
        if class_name in matches_by_class:
            return class_name, matches_by_class[class_name]
    class_name = sorted(matches_by_class)[0]
    return class_name, matches_by_class[class_name]


def repeated_token_phrase(normalized):
    parts = normalized.split()
    return len(parts) > 1 and len(set(parts)) == 1


def looks_like_person_or_place(raw_label, normalized, description):
    raw = raw_label.casefold()
    desc = description.casefold()
    if normalized in CORE_ANCHORS:
        return False
    if re.search(r"\b(?:b\.|d\.)\s", desc):
        return True
    if re.search(r"\b(?:born|died|reigned)\s*(?:c\.)?\s*\d{3,4}\b", desc):
        return True
    if re.search(r"\(\s*\d{3,4}\s*[-–]", desc):
        return True
    if any(marker in raw for marker in {" pope", "pope ", " saint", "saint ", " blessed", "cardinal"}):
        return True
    if any(marker in raw for marker in {" of st.", " of st ", " of saint ", "blessed ", "saint-", " of the mother of god"}):
        return True
    if any(marker in desc for marker in BIOGRAPHY_MARKERS):
        if not any(phrase_in_text(keyword, normalized) for keyword in (
            "apostles", "priesthood", "martyrdom", "communion of saints", "saints"
        )):
            return True
    if any(marker in desc for marker in GEOGRAPHY_MARKERS):
        if not any(phrase_in_text(keyword, normalized) for keyword in (
            "church", "council", "liturgy", "altar", "sacrament"
        )):
            return True
    if "," in raw_label and len(normalized.split()) >= 2:
        if not any(phrase_in_text(keyword, normalized) for keyword in (
            "grace", "sin", "baptism", "atonement", "church", "liturgy",
            "scripture", "sacrament", "trinity", "christ", "god", "heresy",
        )):
            return True
    return False


def looks_like_liturgical_object_noise(normalized):
    parts = normalized.split()
    if not parts:
        return False
    if normalized in ALTAR_TOPIC_ALLOWLIST:
        return False
    if any(part in SACRED_OBJECT_NOISE for part in parts):
        return True
    if "altar" in parts and any(part in LITURGICAL_OBJECT_NOISE for part in parts):
        return True
    return False


def looks_like_institutional_noise(normalized):
    parts = normalized.split()
    return any(part in INSTITUTIONAL_NOISE_WORDS for part in parts)


def looks_like_site_navigation(normalized):
    return normalized in SITE_NAVIGATION_TERMS or any(
        normalized.startswith(prefix)
        for prefix in ("disable ", "please ", "login", "search ", "font ")
    )


def evaluate_candidate(raw_label, description="", source_id=""):
    display = display_anchor_for_label(raw_label)
    normalized = kb.mechanical_term_form(display)
    description = compact(description)
    reasons = []
    label_class, label_keywords = classify_candidate(normalized, "")
    if not normalized:
        reasons.append("empty_after_normalization")
    parts = normalized.split()
    if len(parts) > 8:
        reasons.append("too_long")
    if normalized in STRUCTURAL_PHRASES:
        reasons.append("structural_phrase")
    if looks_like_site_navigation(normalized):
        reasons.append("site_navigation_entry")
    if normalized in GENERIC_REJECT_TERMS or normalized in ORDINAL_WORDS:
        reasons.append("generic_or_ordinal")
    if len(parts) == 1 and (normalized in BIBLE_BOOK_TERMS or normalized in AUTHOR_TERMS):
        reasons.append("single_bible_book_or_author")
    if len(parts) == 1 and ROMAN_NUMERAL_RE.match(normalized):
        reasons.append("roman_numeral")
    if repeated_token_phrase(normalized):
        reasons.append("repeated_token_phrase")
    if looks_like_liturgical_object_noise(normalized):
        reasons.append("liturgical_object_not_topic")
    if looks_like_institutional_noise(normalized):
        reasons.append("institutional_or_administrative_entry")
    if looks_like_person_or_place(raw_label, normalized, description):
        reasons.append("person_or_place_entry")

    anchor_class, matched_keywords = classify_candidate(normalized, description)
    if not anchor_class:
        reasons.append("no_strict_theology_signal")

    if len(parts) == 1 and normalized not in CORE_ANCHORS:
        if not matched_keywords or normalized in GENERIC_REJECT_TERMS:
            reasons.append("weak_single_word")
    if (
        source_id == "new_advent_catholic_encyclopedia"
        and len(parts) == 1
        and normalized not in EXPLICIT_SINGLE_WORD_KEEP
        and not normalized.endswith(("ism", "ology"))
    ):
        reasons.append("unapproved_single_word_index_entry")
    if (
        source_id == "new_advent_catholic_encyclopedia"
        and not label_keywords
        and normalized not in EXPLICIT_SINGLE_WORD_KEEP
        and not normalized.endswith(("ism", "ology"))
    ):
        reasons.append("new_advent_signal_only_in_description")

    status = "active" if not reasons else "rejected"
    weight = 0
    if status == "active":
        weight = 2
        if normalized in CORE_ANCHORS or anchor_class == "doctrine":
            weight = 3
        elif len(parts) == 1:
            weight = 1
        if source_id in {"saet_christian_theology_toc", "boyce_systematic_theology_toc"}:
            weight = max(weight, 2)

    return {
        "anchor": display.casefold(),
        "normalized_form": normalized,
        "anchor_class": anchor_class,
        "matched_keywords": matched_keywords,
        "weight": weight,
        "status": status,
        "rejection_reasons": sorted(set(reasons)),
    }


def extracted(source, url, raw_label, description=""):
    return ExtractedCandidate(
        source_id=source["source_id"],
        source_label=source["label"],
        source_url=url,
        raw_label=raw_label,
        description=description,
    )


def extract_torrey(source, url, html):
    candidates = []
    bad_prefixes = {
        "contents", "title page", "copyright", "preface", "download", "search",
        "navigation", "topical textbook",
    }
    seen = set()
    for label, _href in link_texts(html):
        normalized = normalized_anchor(label)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if any(normalized.startswith(prefix) for prefix in bad_prefixes):
            continue
        candidates.append(extracted(source, url, label))
    if len(candidates) < 50:
        for line in visible_lines(html):
            normalized = normalized_anchor(line)
            if not normalized or normalized in seen:
                continue
            if any(normalized.startswith(prefix) for prefix in bad_prefixes):
                continue
            if 1 <= len(normalized.split()) <= 8:
                seen.add(normalized)
                candidates.append(extracted(source, url, line))
    return candidates


def extract_generic_links(source, url, html):
    candidates = []
    for label, _href in link_texts(html):
        if 1 <= len(normalized_anchor(label).split()) <= 8:
            candidates.append(extracted(source, url, label))
    return candidates


def extract_biblegateway_themes(source, url, html):
    candidates = []
    seen = set()
    for label, href in link_texts(html):
        if "/resources/dictionary-of-bible-themes/" not in href:
            continue
        if "/toc" in href or "amazon.com" in href:
            continue
        if not re.match(r"^\s*\d{4}\s+\S", label):
            continue
        label = normalize_biblegateway_theme_label(label)
        normalized = normalized_anchor(label)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(extracted(source, url, label))
    return candidates


def normalize_biblegateway_theme_label(label):
    label = compact(label)
    match = re.match(r"^(\d{4})\s+(.+)$", label)
    if not match:
        return label
    number, title = match.groups()
    title = compact(title).strip(" .;:")
    if "," not in title:
        return f"{number} {title}"
    head, tail = [part.strip(" .;:") for part in title.split(",", 1)]
    tail_norm = tail.casefold()
    if tail_norm in {"the", "a", "an"}:
        return f"{number} {head}"
    article_match = re.match(r"^(the|a|an)\s+(.+)$", tail, flags=re.IGNORECASE)
    if article_match:
        article, rest = article_match.groups()
        return f"{number} {article} {rest} of {head}"
    if tail_norm.startswith("as "):
        return f"{number} {head} {tail}"
    if tail_norm.endswith(" of"):
        return f"{number} {tail} {head}"
    return f"{number} {title}"


def extract_new_advent(source, url, html):
    candidates = []
    for line in visible_lines(html):
        if " - " not in line:
            continue
        label, description = line.split(" - ", 1)
        label = compact(label)
        description = compact(description)
        if not label or len(label) > 120:
            continue
        candidates.append(extracted(source, url, label, description))
    return candidates


EXTRACTORS = {
    "torrey": extract_torrey,
    "generic_links": extract_generic_links,
    "biblegateway_themes": extract_biblegateway_themes,
    "new_advent": extract_new_advent,
}


def extract_source(config):
    source_status = {
        "source_id": config["source_id"],
        "label": config["label"],
        "url": config["url"],
        "extractor": config["extractor"],
    }
    pages = fetch_all(config, source_status) if config.get("fetch_mode") == "all" or config["extractor"] == "new_advent" else fetch_first(config, source_status)
    extractor = EXTRACTORS[config["extractor"]]
    candidates = []
    for url, html in pages:
        extracted_rows = extractor(config, url, html)
        candidates.extend(extracted_rows)
    source_status["raw_candidates"] = len(candidates)
    return candidates, source_status


def merge_review_rows(review_rows):
    merged = {}
    labels = defaultdict(set)
    for row in review_rows:
        if row["status"] != "active":
            continue
        key = row["normalized_form"]
        current = merged.setdefault(
            key,
            {
                "anchor": row["anchor"],
                "normalized_form": key,
                "source_ids": set(),
                "source_labels": set(),
                "anchor_class": row["anchor_class"],
                "weight": int(row["weight"]),
                "status": "active",
            },
        )
        current["source_ids"].add(row["source_id"])
        labels[key].add(row["source_label"])
        current["source_labels"].add(row["raw_label"])
        current["weight"] = max(current["weight"], int(row["weight"]))
        if current["anchor_class"] != "doctrine" and row["anchor_class"] == "doctrine":
            current["anchor_class"] = "doctrine"

    anchors = []
    for key, row in sorted(merged.items()):
        source_ids = sorted(row["source_ids"])
        weight = row["weight"]
        if len(source_ids) > 1:
            weight = max(weight, 3)
        anchors.append(
            {
                "anchor": row["anchor"],
                "normalized_form": key,
                "source_ids": source_ids,
                "source_labels": sorted(row["source_labels"]),
                "anchor_class": row["anchor_class"],
                "weight": weight,
                "status": "active",
                "notes": "Ranking anchor only; does not assert a concept or register a term.",
            }
        )
    return anchors


def anchor_build_csv_ready(row):
    ready = {}
    for key, value in row.items():
        if isinstance(value, list):
            ready[key] = kb.json_dumps(value)
        else:
            ready[key] = value
    return ready


def write_review_csv(path, rows):
    fieldnames = [
        "status", "anchor", "normalized_form", "anchor_class", "weight",
        "source_id", "raw_label", "description", "matched_keywords",
        "rejection_reasons",
    ]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(anchor_build_csv_ready({key: row.get(key, "") for key in fieldnames}))


def build_anchor_files(anchor_path=DEFAULT_ANCHOR_PATH, review_path=DEFAULT_REVIEW_PATH):
    review_rows = []
    sources = []
    for config in SOURCE_CONFIGS:
        print(f"extracting external anchors: {config['source_id']}", flush=True)
        candidates, status = extract_source(config)
        sources.append(status)
        for candidate in candidates:
            evaluation = evaluate_candidate(
                candidate.raw_label,
                description=candidate.description,
                source_id=candidate.source_id,
            )
            review_rows.append(
                {
                    **evaluation,
                    "source_id": candidate.source_id,
                    "source_label": candidate.source_label,
                    "source_url": candidate.source_url,
                    "raw_label": candidate.raw_label,
                    "description": candidate.description,
                }
            )

    review_rows.sort(
        key=lambda row: (
            row["status"] != "active",
            row["anchor_class"],
            row["normalized_form"],
            row["source_id"],
        )
    )
    anchors = merge_review_rows(review_rows)
    document = {
        "schema_version": ANCHOR_SCHEMA_VERSION,
        "generated_at": date.today().isoformat(),
        "purpose": (
            "External reference anchors used only to rank literal term-discovery "
            "candidates found in supplied PDFs."
        ),
        "rules": {
            "anchors_do_not_register_terms": True,
            "anchors_do_not_create_kb_facts": True,
            "candidate_must_appear_literally_in_supplied_pdf": True,
            "external_sources_are_ranking_provenance_only": True,
        },
        "sources": sources,
        "counts": {
            "active_anchors": len(anchors),
            "review_rows": len(review_rows),
            "rejected_review_rows": sum(1 for row in review_rows if row["status"] != "active"),
        },
        "anchors": anchors,
    }
    kb.write_json(Path(anchor_path), document)
    write_review_csv(Path(review_path), review_rows)
    return document


ANCHOR_ATTESTATION_SCHEMA_VERSION = "1.0.0"
DEFAULT_ANCHOR_PATH = kb.OUT_DIR / "term_discovery_anchors.json"
DEFAULT_CSV_PATH = kb.OUT_DIR / "attested_anchor_terms.csv"
DEFAULT_JSON_PATH = kb.OUT_DIR / "attested_anchor_terms.json"


@dataclass
class AnchorAttestationStats:
    anchor: str
    normalized_form: str
    anchor_class: str = ""
    weight: int = 0
    source_ids: list = field(default_factory=list)
    source_labels: list = field(default_factory=list)
    occurrence_count: int = 0
    sources: set = field(default_factory=set)
    evidence_ids: set = field(default_factory=set)
    layer_counts: Counter = field(default_factory=Counter)
    samples: list = field(default_factory=list)

    def add(self, source_id, source, evidence, text, printed_form, start, end, max_samples):
        self.occurrence_count += 1
        self.sources.add(source_id)
        self.evidence_ids.add(evidence["evidence_id"])
        self.layer_counts[evidence.get("retrieval_layer", "")] += 1
        if len(self.samples) < max_samples:
            excerpt, _, _ = kb.context_excerpt(text, start, end)
            self.samples.append(
                {
                    "source_id": source_id,
                    "collection": source.get("collection", "Unknown"),
                    "display_title": source.get("display_title", ""),
                    "source_author": source.get("source_author", "Unknown"),
                    "page_number": evidence.get("page_number"),
                    "evidence_id": evidence["evidence_id"],
                    "retrieval_layer": evidence.get("retrieval_layer", ""),
                    "text_role": evidence.get("text_role", kb.TEXT_ROLE_BODY),
                    "printed_form": printed_form,
                    "char_start": start,
                    "char_end": end,
                    "context": re.sub(r"\s+", " ", excerpt).strip(),
                }
            )

    def as_report_row(self, registered_terms, requested_terms):
        return {
            "anchor": self.anchor,
            "normalized_form": self.normalized_form,
            "anchor_class": self.anchor_class,
            "weight": self.weight,
            "occurrence_count": self.occurrence_count,
            "source_count": len(self.sources),
            "evidence_count": len(self.evidence_ids),
            "already_registered": self.normalized_form in registered_terms,
            "in_term_requests": self.normalized_form in requested_terms,
            "external_source_ids": self.source_ids,
            "external_source_labels": self.source_labels,
            "sample_source_ids": sorted(self.sources)[:10],
            "sample_evidence_ids": sorted(self.evidence_ids)[:10],
            "sample_attestations": self.samples,
            "layer_counts": dict(sorted(self.layer_counts.items())),
        }


def attest_csv_ready(row):
    ready = {}
    for key, value in row.items():
        if isinstance(value, (dict, list)):
            ready[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            ready[key] = value
    return ready


def attest_write_csv(path, rows, fieldnames):
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(attest_csv_ready(row))


def load_attestation_anchor_rows(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    anchors = []
    seen = set()
    for row in data.get("anchors", []):
        if row.get("status") != "active":
            continue
        anchor = str(row.get("anchor", "")).strip()
        normalized = kb.mechanical_term_form(row.get("normalized_form") or anchor)
        if not anchor or not normalized or normalized in seen:
            continue
        seen.add(normalized)
        anchors.append(
            {
                "anchor": anchor,
                "normalized_form": normalized,
                "anchor_class": row.get("anchor_class", ""),
                "weight": int(row.get("weight") or 0),
                "source_ids": list(row.get("source_ids") or []),
                "source_labels": list(row.get("source_labels") or []),
            }
        )
    anchors.sort(key=lambda row: (-len(row["anchor"]), row["anchor"]))
    return anchors


def attest_requested_lookup_terms(path=kb.TERM_REQUESTS_PATH):
    if not Path(path).exists():
        return set()
    return {
        kb.mechanical_term_form(term)
        for term in kb.read_term_requests(path)
        if kb.mechanical_term_form(term)
    }


def compile_anchor_pattern(anchors):
    literal_forms = sorted(
        {row["anchor"] for row in anchors if row.get("anchor")},
        key=lambda value: (-len(value), value.casefold()),
    )
    if not literal_forms:
        return None
    alternation = "|".join(re.escape(anchor) for anchor in literal_forms)
    return re.compile(
        rf"(?=(?<![A-Za-z0-9])(?P<anchor>{alternation})(?![A-Za-z0-9]))",
        re.IGNORECASE,
    )


def anchor_matches(pattern, text):
    if not pattern:
        return
    for match in pattern.finditer(text or ""):
        printed = match.group("anchor")
        yield printed, match.start("anchor"), match.end("anchor")


def attest_active_source_ids(selected_source_ids=None):
    discovered = [kb.legacy.source_id_for(path) for path in kb.legacy.pdf_paths()]
    if selected_source_ids:
        selected = set(selected_source_ids)
        discovered = [source_id for source_id in discovered if source_id in selected]
        missing = selected - set(discovered)
        if missing:
            raise ValueError("Unknown or inactive source_id(s): " + ", ".join(sorted(missing)))
    return discovered


def attest_anchors(
    anchor_path=DEFAULT_ANCHOR_PATH,
    output_csv=DEFAULT_CSV_PATH,
    output_json=DEFAULT_JSON_PATH,
    selected_source_ids=None,
    max_samples=5,
):
    anchors = load_attestation_anchor_rows(anchor_path)
    anchor_by_printed = {row["anchor"].casefold(): row for row in anchors}
    pattern = compile_anchor_pattern(anchors)
    source_ids = attest_active_source_ids(selected_source_ids)
    registered_terms = {row["lookup_form"] for row in kb.REGISTERED_TERM_ROWS}
    requested_terms = attest_requested_lookup_terms()
    stats_by_lookup = {}

    for source_index, source_id in enumerate(source_ids, start=1):
        print(f"[{source_index}/{len(source_ids)}] attesting discovery anchors {source_id}", flush=True)
        package = kb.load_package(source_id, include_text=True)
        validation = package["validation"]
        if validation.get("status") != "pass":
            raise RuntimeError(f"Source package {source_id} is not valid")
        source = package["source"]
        for evidence in package["evidence"]:
            text = kb.evidence_text(evidence)
            if not text.strip():
                continue
            for printed_form, start, end in anchor_matches(pattern, text):
                anchor_row = anchor_by_printed.get(printed_form.casefold())
                if not anchor_row:
                    continue
                lookup = anchor_row["normalized_form"]
                stats = stats_by_lookup.setdefault(
                    lookup,
                    AnchorAttestationStats(
                        anchor=anchor_row["anchor"],
                        normalized_form=lookup,
                        anchor_class=anchor_row.get("anchor_class", ""),
                        weight=anchor_row.get("weight", 0),
                        source_ids=anchor_row.get("source_ids", []),
                        source_labels=anchor_row.get("source_labels", []),
                    ),
                )
                stats.add(
                    source_id=source_id,
                    source=source,
                    evidence=evidence,
                    text=text,
                    printed_form=printed_form,
                    start=start,
                    end=end,
                    max_samples=max_samples,
                )

    rows = [
        stats.as_report_row(registered_terms, requested_terms)
        for stats in stats_by_lookup.values()
    ]
    rows.sort(
        key=lambda row: (
            row["already_registered"],
            -row["weight"],
            -row["source_count"],
            -row["occurrence_count"],
            row["anchor"],
        )
    )

    summary = {
        "schema_version": ANCHOR_ATTESTATION_SCHEMA_VERSION,
        "generated_at": date.today().isoformat(),
        "anchor_file": str(Path(anchor_path)),
        "source_count": len(source_ids),
        "active_anchor_count": len(anchors),
        "attested_anchor_count": len(rows),
        "new_attested_anchor_count": sum(1 for row in rows if not row["already_registered"]),
        "registered_attested_anchor_count": sum(1 for row in rows if row["already_registered"]),
        "requested_attested_anchor_count": sum(1 for row in rows if row["in_term_requests"]),
        "rules": {
            "match_basis": "literal_case_insensitive_with_alphanumeric_boundaries",
            "scans_validated_clean_evidence_text": True,
            "anchors_do_not_register_terms": True,
            "output_contains_attested_anchors_only": True,
        },
        "report_files": {
            "csv": Path(output_csv).name,
            "json": Path(output_json).name,
        },
        "terms": rows,
    }

    kb.write_json(Path(output_json), summary)
    fieldnames = [
        "anchor", "normalized_form", "anchor_class", "weight",
        "occurrence_count", "source_count", "evidence_count",
        "already_registered", "in_term_requests",
        "external_source_ids", "external_source_labels",
        "sample_source_ids", "sample_evidence_ids", "sample_attestations",
        "layer_counts",
    ]
    attest_write_csv(output_csv, rows, fieldnames)
    return summary


DEFAULT_LAYERS = ("primary", "extended")
VALID_LAYERS = {"primary", "extended", "archival"}
VALID_CANDIDATE_SCOPES = {"headings", "all"}
TOKEN_RE = re.compile(r"[^\W\d_]+(?:['\u2019][^\W\d_]+)?", re.UNICODE)
DEFAULT_ANCHOR_PATH = kb.OUT_DIR / "term_discovery_anchors.json"
ANCHOR_DISCOVERY_LAYERS = {"primary", "extended"}

STOPWORDS = {
    "a", "about", "above", "after", "again", "against", "all", "also", "am", "an",
    "and", "any", "are", "as", "at", "be", "because", "been", "before", "being",
    "below", "between", "both", "but", "by", "can", "chapter", "chapters", "could",
    "did", "do", "does", "doing", "down", "during", "each", "few", "for", "from",
    "further", "had", "has", "have", "having", "he", "her", "here", "hers", "him",
    "himself", "his", "how", "i", "if", "in", "into", "is", "it", "its", "itself",
    "me", "more", "most", "my", "no", "nor", "not", "now", "of", "off", "on",
    "once", "only", "or", "other", "our", "ours", "out", "over", "own", "page",
    "pages", "part", "parts", "same", "section", "sections", "shall", "she", "so",
    "some", "such", "than", "that", "the", "their", "theirs", "them", "themselves",
    "then", "there", "these", "they", "this", "those", "through", "to", "too",
    "under", "until", "up", "upon", "us", "very", "was", "we", "were", "what",
    "when", "where", "which", "while", "who", "whom", "why", "will", "with",
    "within", "without", "would", "ye", "you", "your",
}

STRUCTURAL_WORDS = {
    "article", "articles", "book", "books", "contents", "cover", "editor",
    "footnote", "index", "introduction", "letter", "letters", "note", "notes",
    "preface", "reply", "title", "translator", "volume",
}

GENERIC_DISCOURSE_WORDS = {
    "another", "answer", "answers", "argument", "asked", "brief", "case",
    "certain", "come", "continued", "day", "discourse", "former", "first",
    "general", "good", "great", "however", "latter", "life", "many", "matter",
    "nature", "one", "order", "particular", "passage", "question", "second",
    "several", "show", "shown", "statement", "subject", "third", "thing",
    "things", "treatise", "whether", "work", "works",
}

ORDINAL_WORDS = {
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
    "eighth", "ninth", "tenth", "xi", "xii", "xiii", "xiv", "xv", "xvi",
    "xvii", "xviii", "xix", "xx", "i", "ii", "iii", "iv", "v", "vi", "vii",
    "viii", "ix", "x",
}

BARE_NAME_WORDS = {
    "abraham", "adam", "ambrose", "athanasius", "augustine", "basil", "calvin",
    "chrysostom", "clement", "david", "eusebius", "gregory", "ignatius",
    "isaiah", "jeremiah", "jesus", "john", "joseph", "justin", "luther",
    "mark", "matthew", "moses", "origen", "paul", "peter", "solomon",
    "tertullian", "thomas",
}

TOPICAL_ANCHOR_CLASSES = {
    "biblical_theology", "doctrine", "ethics", "sacrament", "spiritual_practice",
}


@dataclass
class CoverageStats:
    evidence_total: int = 0
    evidence_matched: int = 0
    chars_total: int = 0
    chars_matched: int = 0
    words_total: int = 0
    words_matched: int = 0
    term_occurrences: int = 0
    pages_total: set = field(default_factory=set)
    pages_matched: set = field(default_factory=set)
    distinct_terms: set = field(default_factory=set)

    def add_evidence(self, page_key, chars, words, hits):
        self.evidence_total += 1
        self.chars_total += chars
        self.words_total += words
        self.pages_total.add(page_key)
        if hits:
            self.evidence_matched += 1
            self.chars_matched += chars
            self.words_matched += words
            self.pages_matched.add(page_key)
            self.term_occurrences += len(hits)
            self.distinct_terms.update(hits)

    def as_row(self):
        return {
            "evidence_total": self.evidence_total,
            "evidence_matched": self.evidence_matched,
            "evidence_coverage_pct": percent(self.evidence_matched, self.evidence_total),
            "page_total": len(self.pages_total),
            "page_matched": len(self.pages_matched),
            "page_coverage_pct": percent(len(self.pages_matched), len(self.pages_total)),
            "word_total": self.words_total,
            "word_matched": self.words_matched,
            "word_coverage_pct": percent(self.words_matched, self.words_total),
            "char_total": self.chars_total,
            "char_matched": self.chars_matched,
            "char_coverage_pct": percent(self.chars_matched, self.chars_total),
            "term_occurrences": self.term_occurrences,
            "distinct_registered_terms": len(self.distinct_terms),
        }


@dataclass
class CandidateStats:
    occurrence_count: int = 0
    heading_hits: int = 0
    sources: set = field(default_factory=set)
    evidence_ids: set = field(default_factory=set)
    layer_counts: Counter = field(default_factory=Counter)
    samples: list = field(default_factory=list)

    def add(self, source_id, evidence_id, layer, text, start, end, heading=False):
        self.occurrence_count += 1
        self.sources.add(source_id)
        if evidence_id:
            self.evidence_ids.add(evidence_id)
        self.layer_counts[layer] += 1
        if heading:
            self.heading_hits += 1
        if len(self.samples) < 3:
            self.samples.append(
                {
                    "source_id": source_id,
                    "evidence_id": evidence_id,
                    "context": context_for(text, start, end),
                    "from_heading": heading,
                }
            )


@dataclass
class DiscoveryAnchor:
    anchor: str
    normalized_form: str
    anchor_class: str
    weight: int = 1
    source_ids: list = field(default_factory=list)
    source_labels: list = field(default_factory=list)
    token_tuple: tuple = field(default_factory=tuple)


@dataclass
class TopicCandidateStats:
    occurrence_count: int = 0
    heading_hits: int = 0
    body_hits: int = 0
    uncovered_evidence_ids: set = field(default_factory=set)
    weakly_covered_evidence_ids: set = field(default_factory=set)
    sources: set = field(default_factory=set)
    evidence_ids: set = field(default_factory=set)
    layer_counts: Counter = field(default_factory=Counter)
    samples: list = field(default_factory=list)
    anchor_match_types: set = field(default_factory=set)
    matched_anchors: set = field(default_factory=set)
    anchor_classes: set = field(default_factory=set)
    anchor_source_ids: set = field(default_factory=set)
    anchor_weight: int = 0

    def add(
        self,
        source_id,
        evidence_id,
        layer,
        text,
        start,
        end,
        coverage_class,
        anchor,
        match_type,
        heading=False,
    ):
        self.occurrence_count += 1
        self.sources.add(source_id)
        if evidence_id:
            self.evidence_ids.add(evidence_id)
            if coverage_class == "uncovered":
                self.uncovered_evidence_ids.add(evidence_id)
            elif coverage_class == "weakly_covered":
                self.weakly_covered_evidence_ids.add(evidence_id)
        self.layer_counts[layer] += 1
        if heading:
            self.heading_hits += 1
        else:
            self.body_hits += 1
        self.anchor_match_types.add(match_type)
        if anchor:
            self.matched_anchors.add(anchor.normalized_form)
            self.anchor_classes.add(anchor.anchor_class)
            self.anchor_source_ids.update(anchor.source_ids)
            self.anchor_weight = max(self.anchor_weight, int(anchor.weight or 1))
        if len(self.samples) < 3:
            self.samples.append(
                {
                    "source_id": source_id,
                    "evidence_id": evidence_id,
                    "context": context_for(text, start, end),
                    "from_heading": heading,
                    "coverage_class": coverage_class,
                }
            )


def percent(numerator, denominator):
    if not denominator:
        return 0.0
    return round((numerator / denominator) * 100, 4)


def parse_layers(value):
    layers = tuple(part.strip().casefold() for part in value.split(",") if part.strip())
    invalid = sorted(set(layers) - VALID_LAYERS)
    if invalid:
        raise ValueError("Invalid retrieval layer(s): " + ", ".join(invalid))
    if not layers:
        raise ValueError("At least one retrieval layer is required")
    return layers


def normalize_token(value):
    return value.replace("\u2019", "'").casefold().strip("'")


def token_spans(text):
    tokens = []
    for match in TOKEN_RE.finditer(text or ""):
        token = normalize_token(match.group(0))
        if token:
            tokens.append((token, match.start(), match.end()))
    return tokens


def word_count(text):
    return len(token_spans(text))


def candidate_is_well_formed(candidate):
    parts = candidate.split()
    if not parts:
        return False
    if any(part in STRUCTURAL_WORDS for part in parts):
        return False
    if len(parts) == 1:
        return len(parts[0]) >= 3 and parts[0] not in STOPWORDS
    if parts[0] in STOPWORDS or parts[-1] in STOPWORDS:
        return False
    meaningful = [part for part in parts if part not in STOPWORDS]
    return bool(meaningful) and sum(len(part) for part in meaningful) >= 4


def is_generic_single_word(word):
    return (
        word in STOPWORDS
        or word in STRUCTURAL_WORDS
        or word in GENERIC_DISCOURSE_WORDS
        or word in ORDINAL_WORDS
        or word in BARE_NAME_WORDS
        or len(word) < 3
    )


def candidate_is_reportable(candidate, registered_terms, requested_terms):
    normalized = kb.mechanical_term_form(candidate)
    return (
        bool(normalized)
        and normalized not in registered_terms
        and normalized not in requested_terms
        and candidate_is_well_formed(normalized)
    )


def candidate_ngrams(text, max_gram=4):
    spans = token_spans(text)
    for size in range(1, max_gram + 1):
        for index in range(0, len(spans) - size + 1):
            window = spans[index:index + size]
            candidate = " ".join(token for token, _, _ in window)
            normalized = kb.mechanical_term_form(candidate)
            if not candidate_is_well_formed(normalized):
                continue
            yield normalized, window[0][1], window[-1][2]


def registered_term_hits(text):
    return [
        kb.mechanical_term_form(match.group("term"))
        for match in kb.TERM_RE.finditer(text or "")
    ]


def context_for(text, start, end, radius=120):
    excerpt, _, _ = kb.context_excerpt(text, start, end, radius=radius)
    return re.sub(r"\s+", " ", excerpt).strip()


def coverage_active_source_ids(selected_source_ids=None):
    discovered = [kb.legacy.source_id_for(path) for path in kb.legacy.pdf_paths()]
    if selected_source_ids:
        selected = set(selected_source_ids)
        discovered = [source_id for source_id in discovered if source_id in selected]
        missing = selected - set(discovered)
        if missing:
            raise ValueError("Unknown or inactive source_id(s): " + ", ".join(sorted(missing)))
    return discovered


def coverage_requested_lookup_terms(path=kb.TERM_REQUESTS_PATH):
    if not Path(path).exists():
        return set()
    return {
        kb.mechanical_term_form(term)
        for term in kb.read_term_requests(path)
        if kb.mechanical_term_form(term)
    }


def load_coverage_active_anchors(path=DEFAULT_ANCHOR_PATH):
    data = kb.read_json(Path(path), default={}) or {}
    anchors = {}
    for row in data.get("anchors", []):
        if row.get("status") != "active":
            continue
        normalized = kb.mechanical_term_form(row.get("normalized_form") or row.get("anchor") or "")
        if not normalized:
            continue
        token_tuple = tuple(normalized.split())
        anchors[normalized] = DiscoveryAnchor(
            anchor=row.get("anchor") or normalized,
            normalized_form=normalized,
            anchor_class=row.get("anchor_class", ""),
            weight=int(row.get("weight") or 1),
            source_ids=list(row.get("source_ids") or []),
            source_labels=list(row.get("source_labels") or []),
            token_tuple=token_tuple,
        )
    return anchors


def anchor_head_terms(anchors):
    heads = {}
    for anchor in anchors.values():
        for token in anchor.token_tuple:
            if not is_generic_single_word(token):
                current = heads.get(token)
                if current is None or anchor.weight > current.weight:
                    heads[token] = anchor
    return heads


def coverage_class_for_hits(hits, weak_coverage_hit_threshold):
    if not hits:
        return "uncovered"
    if len(hits) < weak_coverage_hit_threshold:
        return "weakly_covered"
    return "covered"


def topic_candidate_is_well_formed(candidate, exact_anchor=None):
    normalized = kb.mechanical_term_form(candidate)
    parts = normalized.split()
    if not parts:
        return False
    if any("'" in part for part in parts):
        return False
    if len(parts) == 1:
        if is_generic_single_word(parts[0]):
            return False
        if exact_anchor:
            return exact_anchor.anchor_class in TOPICAL_ANCHOR_CLASSES
        return False
    if any(part in STRUCTURAL_WORDS or part in ORDINAL_WORDS for part in parts):
        return False
    if parts[0] in STOPWORDS or parts[-1] in STOPWORDS:
        return False
    if len(set(parts)) == 1:
        return False
    meaningful = [
        part for part in parts
        if part not in STOPWORDS and part not in GENERIC_DISCOURSE_WORDS
    ]
    return bool(meaningful) and sum(len(part) for part in meaningful) >= 5


def iter_exact_anchor_matches(spans, anchors_by_tokens, max_anchor_tokens):
    for index in range(len(spans)):
        max_size = min(max_anchor_tokens, len(spans) - index)
        for size in range(max_size, 0, -1):
            token_tuple = tuple(token for token, _, _ in spans[index:index + size])
            anchor = anchors_by_tokens.get(token_tuple)
            if anchor:
                window = spans[index:index + size]
                yield anchor.normalized_form, window[0][1], window[-1][2], anchor


def iter_near_anchor_phrase_matches(spans, head_terms, anchors, min_gram=2, max_gram=5):
    for size in range(min_gram, max_gram + 1):
        for index in range(0, len(spans) - size + 1):
            window = spans[index:index + size]
            tokens = [token for token, _, _ in window]
            head_matches = [head_terms[token] for token in tokens if token in head_terms]
            if not head_matches:
                continue
            candidate = kb.mechanical_term_form(" ".join(tokens))
            exact_anchor = anchors.get(candidate)
            if not topic_candidate_is_well_formed(candidate, exact_anchor=exact_anchor):
                continue
            if exact_anchor:
                anchor = exact_anchor
                match_type = "exact"
            else:
                anchor = max(head_matches, key=lambda item: item.weight)
                match_type = "expanded" if len(anchor.token_tuple) == 1 else "near_anchor"
            yield candidate, window[0][1], window[-1][2], anchor, match_type


def add_topic_term_candidates(
    topic_candidates,
    text,
    source_id,
    evidence_id,
    layer,
    coverage_class,
    anchors,
    anchors_by_tokens,
    head_terms,
    registered_terms,
    requested_terms,
    heading=False,
):
    spans = token_spans(text)
    if not spans:
        return
    seen = set()
    max_anchor_tokens = max((len(anchor.token_tuple) for anchor in anchors.values()), default=1)

    for candidate, start, end, anchor in iter_exact_anchor_matches(
        spans,
        anchors_by_tokens,
        max_anchor_tokens,
    ):
        if candidate in registered_terms or candidate in requested_terms:
            continue
        if not topic_candidate_is_well_formed(candidate, exact_anchor=anchor):
            continue
        key = (candidate, start, end, "exact")
        if key in seen:
            continue
        seen.add(key)
        topic_candidates[candidate].add(
            source_id, evidence_id, layer, text, start, end, coverage_class,
            anchor, "exact", heading=heading,
        )

    for candidate, start, end, anchor, match_type in iter_near_anchor_phrase_matches(
        spans,
        head_terms,
        anchors,
    ):
        if candidate in registered_terms or candidate in requested_terms:
            continue
        key = (candidate, start, end, match_type)
        if key in seen:
            continue
        seen.add(key)
        topic_candidates[candidate].add(
            source_id, evidence_id, layer, text, start, end, coverage_class,
            anchor, match_type, heading=heading,
        )


def update_stats(groups, keys, page_key, chars, words, hits):
    for key in keys:
        groups[key].add_evidence(page_key, chars, words, hits)


def add_candidate_occurrences(candidates, text, source_id, evidence_id, layer, registered_terms, requested_terms):
    seen_in_evidence = set()
    for candidate, start, end in candidate_ngrams(text):
        if not candidate_is_reportable(candidate, registered_terms, requested_terms):
            continue
        key = (candidate, start, end)
        if key in seen_in_evidence:
            continue
        seen_in_evidence.add(key)
        candidates[candidate].add(
            source_id=source_id,
            evidence_id=evidence_id,
            layer=layer,
            text=text,
            start=start,
            end=end,
        )


def add_heading_candidates(candidates, source_id, evidence_id, layer, heading, registered_terms, requested_terms):
    for candidate, start, end in candidate_ngrams(heading):
        if not candidate_is_reportable(candidate, registered_terms, requested_terms):
            continue
        candidates[candidate].add(
            source_id=source_id,
            evidence_id=evidence_id,
            layer=layer,
            text=heading,
            start=start,
            end=end,
            heading=True,
        )


def candidate_score(stats):
    archival_count = stats.layer_counts.get("archival", 0)
    return round(
        math.log1p(stats.occurrence_count) * 8
        + len(stats.sources) * 5
        + stats.heading_hits * 3
        - archival_count * 0.25,
        4,
    )


def topic_candidate_score(candidate, stats):
    gram_size = len(candidate.split())
    exact_bonus = 45 if "exact" in stats.anchor_match_types else 0
    expanded_bonus = 20 if "expanded" in stats.anchor_match_types else 0
    near_bonus = 10 if "near_anchor" in stats.anchor_match_types else 0
    single_bonus = 12 if gram_size == 1 else 0
    phrase_bonus = 18 if 2 <= gram_size <= 5 else 0
    coverage_bonus = len(stats.uncovered_evidence_ids) * 8 + len(stats.weakly_covered_evidence_ids) * 4
    spread_bonus = len(stats.sources) * 6 + math.log1p(len(stats.evidence_ids)) * 7
    body_bonus = stats.body_hits * 1.5
    heading_bonus = stats.heading_hits * 0.25
    generic_penalty = 18 if gram_size == 1 and is_generic_single_word(candidate) else 0
    return round(
        exact_bonus
        + expanded_bonus
        + near_bonus
        + single_bonus
        + phrase_bonus
        + stats.anchor_weight * 6
        + coverage_bonus
        + spread_bonus
        + body_bonus
        + heading_bonus
        - generic_penalty,
        4,
    )


def merge_candidate_stats(target, source):
    target.occurrence_count += source.occurrence_count
    target.heading_hits += source.heading_hits
    target.sources.update(source.sources)
    target.evidence_ids.update(source.evidence_ids)
    target.layer_counts.update(source.layer_counts)
    for sample in source.samples:
        if len(target.samples) >= 3:
            break
        target.samples.append(sample)


def candidate_rows(candidates, registered_terms, requested_terms, min_occurrences, min_sources, top_candidates):
    rows = []
    for candidate, stats in candidates.items():
        normalized = kb.mechanical_term_form(candidate)
        if stats.occurrence_count < min_occurrences:
            continue
        if len(stats.sources) < min_sources:
            continue
        row = {
            "candidate": candidate,
            "normalized_form": normalized,
            "gram_size": len(candidate.split()),
            "occurrence_count": stats.occurrence_count,
            "source_count": len(stats.sources),
            "evidence_count": len(stats.evidence_ids),
            "registered_already": normalized in registered_terms,
            "in_term_requests": normalized in requested_terms,
            "heading_hits": stats.heading_hits,
            "score": candidate_score(stats),
            "sample_source_ids": sorted(stats.sources)[:5],
            "sample_evidence_ids": sorted(stats.evidence_ids)[:5],
            "sample_contexts": stats.samples,
            "layer_counts": dict(sorted(stats.layer_counts.items())),
        }
        rows.append(row)
    rows.sort(
        key=lambda row: (
            -row["score"],
            -row["source_count"],
            -row["occurrence_count"],
            row["candidate"],
        )
    )
    return rows[:top_candidates]


def topic_candidate_rows(topic_candidates, registered_terms, requested_terms, min_occurrences, min_sources, top_candidates):
    rows = []
    match_type_order = {"exact": 0, "expanded": 1, "near_anchor": 2}
    for candidate, stats in topic_candidates.items():
        normalized = kb.mechanical_term_form(candidate)
        exact = "exact" in stats.anchor_match_types
        if normalized in registered_terms or normalized in requested_terms:
            continue
        if stats.occurrence_count < min_occurrences and not exact:
            continue
        if len(stats.sources) < min_sources and not exact:
            continue
        if len(normalized.split()) == 1 and not exact:
            continue
        match_type = sorted(stats.anchor_match_types, key=lambda item: match_type_order.get(item, 9))[0]
        row = {
            "candidate": candidate,
            "normalized_form": normalized,
            "gram_size": len(normalized.split()),
            "candidate_kind": "single_word" if len(normalized.split()) == 1 else (
                "expanded_phrase" if match_type == "expanded" else "phrase"
            ),
            "anchor_match_type": match_type,
            "matched_anchor": sorted(stats.matched_anchors)[0] if stats.matched_anchors else "",
            "anchor_class": sorted(stats.anchor_classes)[0] if stats.anchor_classes else "",
            "anchor_weight": stats.anchor_weight,
            "anchor_source_ids": sorted(stats.anchor_source_ids),
            "occurrence_count": stats.occurrence_count,
            "source_count": len(stats.sources),
            "evidence_count": len(stats.evidence_ids),
            "uncovered_evidence_count": len(stats.uncovered_evidence_ids),
            "weakly_covered_evidence_count": len(stats.weakly_covered_evidence_ids),
            "heading_hits": stats.heading_hits,
            "body_hits": stats.body_hits,
            "anchor_score": topic_candidate_score(normalized, stats),
            "sample_source_ids": sorted(stats.sources)[:5],
            "sample_evidence_ids": sorted(stats.evidence_ids)[:5],
            "sample_contexts": stats.samples,
            "layer_counts": dict(sorted(stats.layer_counts.items())),
        }
        rows.append(row)
    rows.sort(
        key=lambda row: (
            0 if row["anchor_match_type"] == "exact" else 1,
            -row["anchor_score"],
            -row["source_count"],
            -row["evidence_count"],
            row["candidate"],
        )
    )
    return rows[:top_candidates]


def coverage_csv_ready(row):
    ready = {}
    for key, value in row.items():
        if isinstance(value, (dict, list)):
            ready[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            ready[key] = value
    return ready


def coverage_write_csv(path, rows, fieldnames):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(coverage_csv_ready(row))


def discover_coverage(
    output_dir,
    layers=DEFAULT_LAYERS,
    min_occurrences=10,
    min_sources=2,
    top_candidates=1000,
    low_coverage_threshold=25.0,
    selected_source_ids=None,
    local_min_occurrences=2,
    candidate_scope="headings",
    anchor_path=DEFAULT_ANCHOR_PATH,
    topic_min_occurrences=2,
    topic_min_sources=1,
    weak_coverage_hit_threshold=2,
):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    layers = tuple(layers)
    registered_terms = {row["lookup_form"] for row in kb.REGISTERED_TERM_ROWS}
    requested_terms = coverage_requested_lookup_terms()
    source_ids = coverage_active_source_ids(selected_source_ids)
    anchors = load_coverage_active_anchors(anchor_path)
    anchors_by_tokens = {
        anchor.token_tuple: anchor
        for anchor in anchors.values()
        if anchor.token_tuple
    }
    head_terms = anchor_head_terms(anchors)

    groups = defaultdict(CoverageStats)
    by_source_rows = []
    candidates = defaultdict(CandidateStats)
    topic_candidates = defaultdict(TopicCandidateStats)

    for source_index, source_id in enumerate(source_ids, start=1):
        print(f"[{source_index}/{len(source_ids)}] coverage discovery {source_id}", flush=True)
        package = kb.load_package(source_id, include_text=True)
        validation = package["validation"]
        if validation.get("status") != "pass":
            raise RuntimeError(f"Source package {source_id} is not valid")
        source = package["source"]
        pages = {row["page_number"]: row for row in package["pages"]}
        source_stats = CoverageStats()
        source_candidates = defaultdict(CandidateStats)
        heading_seen = set()

        for evidence in package["evidence"]:
            layer = evidence.get("retrieval_layer", "")
            if layer not in layers:
                continue
            page = pages[evidence["page_number"]]
            text = page.get("original_text", "")[evidence["char_start"]:evidence["char_end"]]
            if not text.strip():
                continue

            hits = registered_term_hits(text)
            coverage_class = coverage_class_for_hits(hits, weak_coverage_hit_threshold)
            chars = len(text)
            words = word_count(text)
            page_key = (source_id, evidence["page_number"])
            source_stats.add_evidence(page_key, chars, words, hits)
            update_stats(
                groups,
                [
                    ("corpus", "all"),
                    ("collection", source.get("collection", "Unknown")),
                    ("author", evidence.get("work_attributed_author") or "Unknown"),
                    ("layer", layer),
                ],
                page_key,
                chars,
                words,
                hits,
            )
            if candidate_scope == "all":
                add_candidate_occurrences(
                    source_candidates,
                    text,
                    source_id,
                    evidence["evidence_id"],
                    layer,
                    registered_terms,
                    requested_terms,
                )
            if layer in ANCHOR_DISCOVERY_LAYERS and coverage_class != "covered":
                add_topic_term_candidates(
                    topic_candidates,
                    text,
                    source_id,
                    evidence["evidence_id"],
                    layer,
                    coverage_class,
                    anchors,
                    anchors_by_tokens,
                    head_terms,
                    registered_terms,
                    requested_terms,
                )
            heading_key = (source_id, evidence["page_number"], page.get("outline_path", ""))
            if heading_key not in heading_seen:
                heading_seen.add(heading_key)
                heading = " ".join(
                    part for part in [page.get("active_heading", ""), page.get("outline_path", "")]
                    if part
                )
                add_heading_candidates(
                    source_candidates,
                    source_id,
                    evidence["evidence_id"],
                    layer,
                    heading,
                    registered_terms,
                    requested_terms,
                )
                if layer in ANCHOR_DISCOVERY_LAYERS and coverage_class != "covered":
                    add_topic_term_candidates(
                        topic_candidates,
                        heading,
                        source_id,
                        evidence["evidence_id"],
                        layer,
                        coverage_class,
                        anchors,
                        anchors_by_tokens,
                        head_terms,
                        registered_terms,
                        requested_terms,
                        heading=True,
                    )

        for candidate, stats in source_candidates.items():
            if stats.occurrence_count >= local_min_occurrences or stats.heading_hits:
                merge_candidate_stats(candidates[candidate], stats)

        source_row = {
            "source_id": source_id,
            "collection": source.get("collection", "Unknown"),
            "display_title": source.get("display_title", ""),
            "source_author": source.get("source_author", "Unknown"),
            **source_stats.as_row(),
        }
        by_source_rows.append(source_row)

    by_source_rows.sort(key=lambda row: (row["collection"], row["source_id"]))
    low_coverage_rows = sorted(
        [
            row for row in by_source_rows
            if row["evidence_coverage_pct"] <= low_coverage_threshold
        ],
        key=lambda row: (row["evidence_coverage_pct"], row["source_id"]),
    )
    candidates_report = candidate_rows(
        candidates,
        registered_terms,
        requested_terms,
        min_occurrences=min_occurrences,
        min_sources=min_sources,
        top_candidates=top_candidates,
    )
    topic_candidates_report = topic_candidate_rows(
        topic_candidates,
        registered_terms,
        requested_terms,
        min_occurrences=topic_min_occurrences,
        min_sources=topic_min_sources,
        top_candidates=top_candidates,
    )

    summary = {
        "generated_at": date.today().isoformat(),
        "source_count": len(source_ids),
        "layers": list(layers),
        "registered_terms": len(registered_terms),
        "requested_terms": len(requested_terms),
        "candidate_filters": {
            "min_occurrences": min_occurrences,
            "min_sources": min_sources,
            "local_min_occurrences": local_min_occurrences,
            "candidate_scope": candidate_scope,
            "top_candidates": top_candidates,
            "low_coverage_threshold": low_coverage_threshold,
        },
        "anchor_candidate_filters": {
            "anchor_path": str(Path(anchor_path)),
            "active_anchors": len(anchors),
            "anchor_head_terms": len(head_terms),
            "discovery_layers": sorted(ANCHOR_DISCOVERY_LAYERS),
            "min_occurrences": topic_min_occurrences,
            "min_sources": topic_min_sources,
            "weak_coverage_hit_threshold": weak_coverage_hit_threshold,
            "top_candidates": top_candidates,
            "single_word_policy": "exact active topical anchors only",
        },
        "corpus": groups[("corpus", "all")].as_row(),
        "by_collection": {
            key: stats.as_row()
            for (kind, key), stats in sorted(groups.items())
            if kind == "collection"
        },
        "by_author": {
            key: stats.as_row()
            for (kind, key), stats in sorted(groups.items())
            if kind == "author"
        },
        "by_layer": {
            key: stats.as_row()
            for (kind, key), stats in sorted(groups.items())
            if kind == "layer"
        },
        "report_files": {
            "summary": "term_coverage_summary.json",
            "by_source": "term_coverage_by_source.csv",
            "candidate_csv": "term_candidate_report.csv",
            "candidate_jsonl": "term_candidate_report.jsonl",
            "literal_topic_term_candidates_csv": "literal_topic_term_candidates.csv",
            "literal_topic_term_candidates_jsonl": "literal_topic_term_candidates.jsonl",
            "low_coverage_sources": "low_coverage_sources.csv",
        },
    }

    kb.write_json(output_dir / "term_coverage_summary.json", summary)
    source_fields = [
        "source_id", "collection", "display_title", "source_author",
        "evidence_total", "evidence_matched", "evidence_coverage_pct",
        "page_total", "page_matched", "page_coverage_pct",
        "word_total", "word_matched", "word_coverage_pct",
        "char_total", "char_matched", "char_coverage_pct",
        "term_occurrences", "distinct_registered_terms",
    ]
    coverage_write_csv(output_dir / "term_coverage_by_source.csv", by_source_rows, source_fields)
    coverage_write_csv(output_dir / "low_coverage_sources.csv", low_coverage_rows, source_fields)
    candidate_fields = [
        "candidate", "normalized_form", "gram_size", "occurrence_count",
        "source_count", "evidence_count", "registered_already",
        "in_term_requests", "heading_hits", "score", "sample_source_ids",
        "sample_evidence_ids", "sample_contexts", "layer_counts",
    ]
    coverage_write_csv(output_dir / "term_candidate_report.csv", candidates_report, candidate_fields)
    kb.write_jsonl(output_dir / "term_candidate_report.jsonl", candidates_report)
    topic_candidate_fields = [
        "candidate", "normalized_form", "gram_size", "candidate_kind",
        "anchor_match_type", "matched_anchor", "anchor_class", "anchor_weight",
        "anchor_source_ids", "occurrence_count", "source_count", "evidence_count",
        "uncovered_evidence_count", "weakly_covered_evidence_count",
        "heading_hits", "body_hits", "anchor_score", "sample_source_ids",
        "sample_evidence_ids", "sample_contexts", "layer_counts",
    ]
    coverage_write_csv(
        output_dir / "literal_topic_term_candidates.csv",
        topic_candidates_report,
        topic_candidate_fields,
    )
    kb.write_jsonl(
        output_dir / "literal_topic_term_candidates.jsonl",
        topic_candidates_report,
    )
    return summary


def default_output_dir():
    return kb.OUT_DIR / date.today().isoformat() / "term_coverage"



def run_build_anchors(args):
    document = build_anchor_files(args.output, args.review_csv)
    print(kb.json_dumps(document["counts"], pretty=True))


def run_attest_anchors(args):
    summary = attest_anchors(
        anchor_path=args.anchors,
        output_csv=args.output_csv,
        output_json=args.output_json,
        selected_source_ids=args.source_id or None,
        max_samples=args.max_samples,
    )
    printable = {key: value for key, value in summary.items() if key != "terms"}
    print(kb.json_dumps(printable, pretty=True))


def run_discover_coverage(args):
    summary = discover_coverage(
        output_dir=args.output_dir,
        layers=parse_layers(args.layers),
        min_occurrences=args.min_occurrences,
        min_sources=args.min_sources,
        top_candidates=args.top_candidates,
        low_coverage_threshold=args.low_coverage_threshold,
        selected_source_ids=args.source_id or None,
        local_min_occurrences=args.local_min_occurrences,
        candidate_scope=args.candidate_scope,
        anchor_path=args.anchors,
        topic_min_occurrences=args.topic_min_occurrences,
        topic_min_sources=args.topic_min_sources,
        weak_coverage_hit_threshold=args.weak_coverage_hit_threshold,
    )
    print(kb.json_dumps(summary, pretty=True))


def build_parser():
    parser = argparse.ArgumentParser(
        description="Term discovery, anchor building, and anchor attestation tools for the Christian KB."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser(
        "build-anchors",
        help="Build external discovery anchors used only for ranking literal term candidates.",
    )
    build_parser.add_argument("--output", default=str(DEFAULT_ANCHOR_PATH))
    build_parser.add_argument("--review-csv", default=str(DEFAULT_REVIEW_PATH))
    build_parser.set_defaults(func=run_build_anchors)

    attest_parser = subparsers.add_parser(
        "attest-anchors",
        help="Check which discovery anchors are exactly attested in supplied PDF text.",
    )
    attest_parser.add_argument("--anchors", default=str(DEFAULT_ANCHOR_PATH))
    attest_parser.add_argument("--output-csv", default=str(DEFAULT_CSV_PATH))
    attest_parser.add_argument("--output-json", default=str(DEFAULT_JSON_PATH))
    attest_parser.add_argument("--max-samples", type=int, default=5)
    attest_parser.add_argument(
        "--source-id",
        action="append",
        default=[],
        help="Limit attestation to one active source_id; may be repeated.",
    )
    attest_parser.set_defaults(func=run_attest_anchors)

    coverage_parser = subparsers.add_parser(
        "discover-coverage",
        help="Discover registered-term coverage and unregistered candidate terms.",
    )
    coverage_parser.add_argument("--layers", default=",".join(DEFAULT_LAYERS))
    coverage_parser.add_argument("--min-occurrences", type=int, default=10)
    coverage_parser.add_argument("--min-sources", type=int, default=2)
    coverage_parser.add_argument("--top-candidates", type=int, default=1000)
    coverage_parser.add_argument("--low-coverage-threshold", type=float, default=25.0)
    coverage_parser.add_argument("--anchors", default=str(DEFAULT_ANCHOR_PATH))
    coverage_parser.add_argument("--topic-min-occurrences", type=int, default=2)
    coverage_parser.add_argument("--topic-min-sources", type=int, default=1)
    coverage_parser.add_argument(
        "--weak-coverage-hit-threshold",
        type=int,
        default=2,
        help="Evidence chunks below this registered-term hit count are scanned for anchor-ranked candidates.",
    )
    coverage_parser.add_argument(
        "--local-min-occurrences",
        type=int,
        default=2,
        help="Keep a candidate from a source only after this many local occurrences, unless it appears in a heading.",
    )
    coverage_parser.add_argument("--output-dir", default=str(default_output_dir()))
    coverage_parser.add_argument(
        "--candidate-scope",
        choices=sorted(VALID_CANDIDATE_SCOPES),
        default="headings",
        help="Use headings for fast review candidates, or all for slower full body n-grams.",
    )
    coverage_parser.add_argument(
        "--source-id",
        action="append",
        default=[],
        help="Limit discovery to one active source_id; may be repeated.",
    )
    coverage_parser.set_defaults(func=run_discover_coverage)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
