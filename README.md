# Christian Theology Knowledge Base

This repository contains the source code and configuration for building a
source-faithful knowledge base from a separately supplied collection of
Christian theological PDFs. It records literal evidence, page and section
metadata, printed author attribution, Scripture references, term occurrences,
and deterministic cross-book links.

It does not generate theological conclusions, doctrinal summaries, inferred
author positions, or claims of agreement between authors. `Unknown` and
`unresolved` are intentional results when the supplied PDFs do not establish a
fact directly.

## Repository contents

The Git repository is the reproducible builder and configuration project. The
large generated KB bundle and the original PDFs are distributed separately.

Retained project files include:

- `source_faithful_kb.py`: build, rebuild, validation, and term-registration engine.
- `search_kb.py`: literal search CLI for a downloaded/generated KB bundle.
- `term_tools.py`: conservative term-discovery and coverage tools.
- `kb_common.py`: shared paths, registries, JSONL helpers, and source utilities.
- `author_registry.json`: canonical authors, aliases, and folder mappings.
- `term_requests.json`: requested literal terms for registration.
- `term_discovery_anchors.json`: external discovery anchors used only to rank candidates.
- `christian_kb_term_registry.jsonl`: mechanically attested literal-term registry.
- `author_attribution_reviews.jsonl`: evidence-bound review decisions for author attribution.

The generated KB bundle contains files such as `christian_kb_evidence_*.jsonl`,
`christian_kb_pages_*.jsonl`, `christian_kb_sections_*.jsonl`, source registries,
concordances, and manifests. These generated files are intentionally excluded
from ordinary Git history because they are large derived artifacts. The
original PDFs are also not included in this repository.

The complete generated bundle is intended to be released separately, for
example as a GitHub Release archive. A release archive should include every
`christian_kb_*` file, including `christian_kb_manifest.json`, and must not
include `_source_cache/`, dated audit folders, or the original PDFs.

## Obtain or build the KB bundle

To search this project, place a compatible generated KB bundle in this folder,
or obtain the separately published data artifact. The bundle must contain
`christian_kb_manifest.json` and the files listed by that manifest.

To rebuild from the supplied PDFs, place the PDF corpus under
`../raw_documents/` using the registered folder and source layout, then run:

```powershell
python .\source_faithful_kb.py
```

The builder creates local source packages and dated validation reports for
incremental work. Those outputs are ignored by Git and are not part of the
portable publication.

Validate an available generated bundle with:

```powershell
python .\source_faithful_kb.py --validate-only
```

The build is source-faithful: PDF text, page hashes, and evidence content are
not replaced with outside summaries or generated interpretations.

## Application: Theologia Search

One application of this knowledge base is building the searchable SQLite index
for [Theologia Search](https://github.com/zjuerc/theologia_search), a
deterministic retrieval application for source-faithful Christian theological
evidence. After obtaining a compatible KB release, build the index from the
Theologia Search project with:

```powershell
python .\build_index.py --kb-dir "..\knowledge_base"
```

## Author attribution

For ordinary first-level `raw_documents` folders, the folder name is resolved
against `author_registry.json` and must identify exactly one registered author.
The two anthology collections, `Ante-Nicene Fathers` and
`Nicene and Post-Nicene Fathers`, use deterministic printed attribution and
scope evidence followed by evidence-bound review decisions recorded in
`author_attribution_reviews.jsonl`.

Ordinary name mentions remain mentioned-author metadata; they do not become
work authors without authorship evidence. Uncertain attribution is retained as
`Unknown` rather than deleting evidence.

## Page numbers and incremental pagination

The public labels are **PDF file page**, **Header/footer page**, and
**Printed book reference**, with natural plurals for ranges. For example:
`PDF file page: 818 · Header/footer page: 768 · Printed book reference: 358`.
The printed book reference is a number shown in the PDF margin and may not
pinpoint every word of the cited passage. `pagination.display_labels` in the
manifest stores the labels and descriptions; existing JSONL field names stay
compatible.

To refresh descriptions only, without scanning PDFs or rewriting JSONL:

```powershell
python .\page_locations.py --refresh-labels
python ..\theologia_search\pagination.py --kb-dir . --labels-only
```

`page_number`, `page_start`, and `page_end` always refer to PDF file positions,
counted from 1. The optional `christian_kb_page_locations.jsonl` extension stores
the Header/footer page number and printed book references in the margin separately.
Search output exposes these records as `page_locations`. The older
`printed_page_number(s)` fields are retained as legacy extraction data and can
contain mixed labels; use the verified extension for citations.

Printed book references describe what is visibly marked on the cited PDF page,
not an inferred pinpoint for every word of an evidence passage. Missing,
unavailable, and unresolved labels are explicit. Roman numerals, numbering
resets, and multiple printed book references on one PDF page are preserved.
PDF viewer page labels and calculated offsets are not used as evidence.

Each `page_locations` entry has `source_id`, `source_format` (`pdf`),
`pdf_page_number` (1-based integer), `pdf_sha256`,
`pdf_edition_page_number` (string or null), `original_book_page_numbers`
(ordered strings), `pdf_edition_status`, `original_book_status`, and
`observations` with label text, bounding box, method, and verification status.
Statuses are `verified`, `not_marked`, `unavailable`, or `unresolved`.
Observations excluded as TOC/heading/footnote numerals retain an explicit reason;
they do not become page labels. Readers without this extension still show PDF
file positions and report visible numbering as unavailable.

To add pagination to an existing downloaded/released bundle without rebuilding:

```powershell
python -m pip install -r .\requirements.txt
python .\page_locations.py
python ..\theologia_search\pagination.py --kb-dir .
```

The first command above installs processing dependencies; the two scripts only
add pagination metadata. They do not invoke the KB builder or reindex terms.
Keep the original release archive. The migration backs up the SQLite database
and index manifest before updating them; the pagination script saves the prior
KB manifest. `pagination_coverage.json` reports verified/missing/unresolved
coverage. `_pagination_cache/` checkpoints one source at a time; rerunning is safe.

Future runs of the ordinary PDF builder retain fingerprint-matching pagination
and inspect pagination for new or changed PDFs automatically. Intentional future
Theologia Search index builds also import the extension. If only pagination
changed, use `check_index_sync.py --sync-pagination`, not a full index rebuild.
The pagination schema and digest are versioned independently of evidence caches.

PDFs are the currently supported input format. Pagination records include
`source_format` so future importers can describe real fixed-page locations;
EPUB/Word/plain-text ingestion is not implemented by this update. A future
format without stable pages must use its native locator, not fabricate PDF pages.
Unrecognized PDF layouts retain unresolved observations for review. Image-only
pages remain unavailable until an explicit OCR workflow is added.

## Search usage

After a KB bundle is available locally, run:

```powershell
python .\search_kb.py --author "Augustine of Hippo" --term grace --section "Chapter IX"
```

Default search uses clean body evidence. Footnotes, archival material,
extended attribution results, and other layers must be requested explicitly.
See [`search_kb_usage.md`](search_kb_usage.md) for examples and verification
guidance.

The optional `--raw` mode requires the separately maintained source-audit
package under `_source_cache/`. It is not included in the public repository.

## Licensing and third-party material

Original project code, configuration, and documentation are covered by
[`license/LICENSE.txt`](license/LICENSE.txt). CCEL-derived text, PDF editions,
introductions, cover art, and other third-party material are not relicensed by
that notice. See [`license/THIRD-PARTY-NOTICES.txt`](license/THIRD-PARTY-NOTICES.txt)
and [`license/CCEL-Copyright-Policy.txt`](license/CCEL-Copyright-Policy.txt).

Users are responsible for checking the rights applicable to each source,
including jurisdiction-specific copyright and any publisher or edition
restrictions.

## Privacy and publication boundary

The public project must not contain local filesystem paths, personal email
addresses, machine identifiers, original PDFs, source-audit caches, or dated
development reports. Build and review outputs are reproducible from the
supplied source corpus and are intentionally kept outside the public Git tree.
