# `search_kb.py` Usage Guide

`search_kb.py` searches the local Christian theology knowledge base. The rebuilt KB uses reader-clean evidence by default: body text is separated from footnotes, running headers, printed page numbers, structural index/front-matter pages, and hidden/off-page PDF text.

The script does literal retrieval from the generated KB files. It does not create theological summaries, infer doctrine, or use outside sources.

## Basic Command

From the repository parent folder:

```powershell
python .\knowledge_base\search_kb.py --term grace --limit 5
```

From inside the `knowledge_base` folder:

```powershell
python .\search_kb.py --term grace --limit 5
```

## Common Searches

Search by author and term:

```powershell
.\knowledge_base\search_kb.py --author "Augustine of Hippo" --term grace
```

Search by book/source and Scripture reference:

```powershell
.\knowledge_base\search_kb.py --source ANF_01 --scripture "Romans 5:12"
```

Search a specific book by book title:

```powershell
.\knowledge_base\search_kb.py --source "Summa Theologica" --term grace
```

Search by section, chapter, book, article, or epistle:

```powershell
.\knowledge_base\search_kb.py --source "City of God" --term grace --section "Book XIV"
.\knowledge_base\search_kb.py --source "Summa Theologica" --term grace --section "Article. 7"
.\knowledge_base\search_kb.py --author "Augustine of Hippo" --term grace --section "Chapter IX"
```

Search cross-book evidence:

```powershell
.\knowledge_base\search_kb.py --crossbook --term grace --authors "Augustine of Hippo" "Martin Luther"
```

## Clean Evidence Model

Default results use `reader_text` from `body` evidence only. This removes:

- Running headers.
- Printed page numbers.
- Hidden/off-page duplicated text.
- Linked inline footnote markers and isolated margin marker glyphs.
- Footnote text.
- Structural auxiliary pages such as tables of contents, indexes, and Scripture indexes.

The splitter is conservative: color, font, italics, or "different-looking" text alone does not make text auxiliary. Blue Scripture references, bracketed citations such as `[ Rom 9:3 ]`, and source references inside normal paragraph flow remain in body evidence.

The optional source-audit package preserves original PDF page extraction and layout blocks. It is a separate local artifact and is not included in the public repository.

## Footnotes And Raw Text

Include footnotes as separate evidence:

```powershell
.\knowledge_base\search_kb.py --term grace --include-footnotes
```

Search only footnotes:

```powershell
.\knowledge_base\search_kb.py --term grace --role footnote
```

Search both body and footnotes:

```powershell
.\knowledge_base\search_kb.py --term grace --role all
```

Show snippets from the original mixed PDF page extraction:

```powershell
.\knowledge_base\search_kb.py --source NPNF1_05 --term grace --section "Chapter 29" --raw
```

Use `--raw` only when `_source_cache/<source_id>/pages.jsonl` is available beside the generated KB. If it is absent, the command stops with instructions to obtain the optional source-audit package. Use the default clean output for ordinary reading and "did the author say this?" checks.

## `--source` Values

`--source` accepts:

- Source ID: `ANF_01`
- Exact book title: `"Summa Theologica"`
- PDF path: `"Thomas Aquinas/summa.pdf"`
- Unique title/path substring: `"City of God"`

Use quotation marks for values with spaces:

```powershell
.\knowledge_base\search_kb.py --source "City of God" --term grace
```

## Verification Workflow

When someone claims an author said something, start with clean body evidence.

Example claim:

```text
Augustine says in City of God, Book XIV, that grace rescues people from the second death.
```

Start narrow:

```powershell
.\knowledge_base\search_kb.py --source "City of God" --author "Augustine of Hippo" --section "Book XIV" --term grace --limit 10
```

If there are no matches, broaden gradually:

```powershell
.\knowledge_base\search_kb.py --source "City of God" --author "Augustine of Hippo" --term grace --limit 10
.\knowledge_base\search_kb.py --author "Augustine of Hippo" --term grace --limit 20
```

If the claim depends on a footnote or printed Scripture reference, search footnotes explicitly:

```powershell
.\knowledge_base\search_kb.py --source "City of God" --author "Augustine of Hippo" --scripture "Romans 5:12" --include-footnotes --limit 10
```

## How To Read Results

A strong match has:

- The same author.
- The same book/source.
- The same section, chapter, book, article, or epistle.
- `body` as the evidence role for author-said claims.
- A snippet that actually supports the claim.
- A page number and evidence ID you can cite.

Example result fields:

```text
1. NPNF1_02 p.644 | Augustine of Hippo | body | NPNF1_02_EVID_00644_BODY_01 | occurrences: 3
   NPNF1-02. St. Augustine's City of God and Christian Doctrine
   Heading: That the Disobedience of the First Man Would Have Plunged All Men...
   Chapter 1.--That the Disobedience ... Had Not the [[Grace]] of God Rescued Many...
```

Interpretation:

```text
source_id: NPNF1_02
page: 644
author: Augustine of Hippo
evidence role: body
evidence_id: NPNF1_02_EVID_00644_BODY_01
matched term: grace
```

## Important Limits

`search_kb.py` proves only what the current KB contains.

```text
Matches found = matching evidence exists in the KB.
Matches: 0 = no matching literal evidence was found under those filters.
```

`Matches: 0` does not prove the author never said it anywhere. It means the current local KB did not find that literal term or Scripture reference under your filters.

This is literal search. For a broad claim, search several related literal terms or exact phrases from the claim.

Example:

```powershell
.\knowledge_base\search_kb.py --author "Augustine of Hippo" --term "original sin"
.\knowledge_base\search_kb.py --author "Augustine of Hippo" --term Adam
.\knowledge_base\search_kb.py --author "Augustine of Hippo" --scripture "Romans 5:12"
```

## Useful Options

Limit output:

```powershell
.\knowledge_base\search_kb.py --term grace --limit 3
```

Search non-primary layers:

```powershell
.\knowledge_base\search_kb.py --term grace --layer extended
.\knowledge_base\search_kb.py --term grace --layer archival
.\knowledge_base\search_kb.py --term grace --layer all
```

Output JSON Lines for scripting:

```powershell
.\knowledge_base\search_kb.py --source "Summa Theologica" --term grace --limit 5 --jsonl
```

Hide snippets:

```powershell
.\knowledge_base\search_kb.py --term grace --no-snippet
```
## Explicit page numbering

`page_number` is a 1-based PDF file position. Results additionally include
`page_start`, `page_end`, legacy `printed_page_number(s)`, and `page_locations`.
Use `page_locations` for verified Header/footer page numbers and printed book references.
Readable output labels each numbering system separately. For evidence spanning
multiple PDF pages, the list preserves each page's labels rather than applying
a single numeric offset. Printed book references are observations on those PDF
pages, not a guaranteed pinpoint for the quoted words. Missing metadata is
reported explicitly and never replaced with a guessed number.
