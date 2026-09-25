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

## Search

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
