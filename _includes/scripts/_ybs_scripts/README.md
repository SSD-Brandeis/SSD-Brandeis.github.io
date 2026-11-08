# `_ybs_scripts/`

Helper scripts that keep the SSD Lab site's auto-generated artifacts (citation counts, publication previews) in sync with their upstream sources.
Run `_populate_lab_papers.py` before each `bundle exec jekyll build` whenever the underlying data changes (new paper, new Scholar refresh, updated PDFs).

---

## `_fetch_artifacts.py`

**Description.** Mirrors three artifact folders from `~/Dropbox/_control/` into `assets/`, treating `_control/` as ground truth. Called automatically at the start of `_populate_lab_papers.py` — no need to run manually.

**Folder pairs:**

| Source | Destination |
| --- | --- |
| `~/Dropbox/_control/fulltext/` | `assets/fulltext/` |
| `~/Dropbox/_control/img/` | `assets/img/` |
| `~/Dropbox/_control/resources/` | `assets/resources/` |

**Behaviour.** New and updated files are copied; files present in `assets/` but absent in `_control/` are deleted. Runs silently — output only appears when files are copied or deleted.

**Prerequisites.** Dropbox is mounted and the three source folders exist.

---

## `_populate_lab_papers.py`

**Description.** Source-of-truth bridge between the shared `~/Dropbox/_control/publication.json`
and this site's `_bibliography/papers.bib`. Syncs artifacts first, then diffs the two,
applies user-confirmed changes, fetches/verifies DBLP metadata, generates missing PDF
previews, and (optionally) refreshes Scholar citations and rebuilds the site.

Only papers **at most 3 years old** are synced (controlled by `WINDOW_YEARS = 3`).

**`publication.json`** is the shared source of truth for recent papers (at most 3 years old). It is
owned by the personal site (`_populate_papers.py`): this script never creates or bootstraps it, and
when it rewrites the file it keeps every record, including ones it doesn't sync. Each record's
`status` is `open` (no verified DBLP entry yet) or `consolidated` (backed by one); every paper should
be consolidated within 2 years of publication. The full workflow is in `~/Dropbox/_control/README.md`.

> **⚠️ The artifact sync deletes lab images.** It mirrors `_control/{fulltext,img,resources}/` into
> `assets/` and deletes anything not in `_control/`. Lab-only images (`assets/img/gallery/`, `media/`,
> `project_preview/`, team photos) are not in `_control/`, so a normal run deletes them. Until they are
> moved into `_control/img/` or committed, run this script only with `--dry-run`.

**Workflow.**

0. **Artifact sync.** Runs `_fetch_artifacts.py` to mirror `_control/` → `assets/` (skipped under `--dry-run`).
1. **No bootstrap.** If `publication.json` is missing, or `--bootstrap` is passed, the script stops with an error; bootstrap from the personal site instead.
2. **Load both sides.** Reads `publication.json` and parses `papers.bib`.
3. **Sync existing entries.** Diffs each tracked field; shows a comparison on any difference and applies changes on confirmation.
4. **DBLP re-check (≤ 3 years).** Retries DBLP for any record with a DBLP key (the `dblp` field, or the key minus `DBLP:`) and `dblp_citation_verified: false`, within three years of publication. dblp.org's `.bib` export is behind a bot check, so the entry is rebuilt from DBLP's SPARQL endpoint (`sparql.dblp.org`); if both fail, the run prints why.
5. **New entries.** Records not in `papers.bib` are shown, DBLP-verified, and prepended on confirmation.
5b. **Status.** Sets each record's `status`, writes `publication.json` (all records) and `papers.bib` if changed, and lists open papers, flagging any older than 2 years.
6. **Generate previews.** Runs ImageMagick on `assets/fulltext/<pdf>` → `assets/img/publication_preview/<stem>.png` for any entry missing a preview.
7. **Refresh citations (optional).** Runs `_fetch_citations.py` on prompt.
8. **Rebuild site (optional).** Runs `bundle exec jekyll build` via chruby/ruby-3.2.2 on prompt.

**Flags.**

- `--dry-run` — print every action but write nothing (the artifact sync is skipped too).
- `--yes` — auto-confirm every prompt.
- `--bootstrap` — refused: `publication.json` is shared and bootstrapped only from the personal site.

**`publication.json` schema.** Shared with the personal site at `~/Dropbox/_control/publication.json`.

| field | required | notes |
| --- | --- | --- |
| `key` | yes | DBLP key (`DBLP:` prefix) or custom. Never change it. |
| `status` | | set by the script: `open` or `consolidated`; don't edit |
| `dblp` | | DBLP key (e.g. `conf/sigmod/SarkarPSA20`) for a record whose `key` isn't `DBLP:…`; set once the paper is on DBLP |
| `type` | yes | `inproceedings` / `article` / `book` / etc. |
| `title` | yes | HTML and BibTeX braces preserved |
| `authors` | yes | list of full-name strings |
| `year` | yes | int |
| `venue_long` | | `booktitle` (inproceedings) or `journal` (article) |
| `venue_short` | | becomes `abbr` → badge text |
| `pages`, `volume`, `number`, `series`, `publisher`, `editor`, `isbn`, `issn`, `month` | | optional bibliographic fields |
| `doi`, `url`, `abstract` | | self-explanatory |
| `pdf` | | filename relative to `assets/fulltext/` — drives preview thumbnail |
| `slides`, `poster` | | filename relative to `assets/resources/` |
| `arxiv`, `website`, `video`, `code_repo`, `purchase` | | artifact links |
| `selected`, `bibtex_show` | | display flags |
| `dblp_citation_verified` | | flipped to `true` after DBLP sync; forced `false` when there is no DBLP key |

**Prerequisites.** Python 3.8+, ImageMagick at `/opt/homebrew/bin/magick`, network access for DBLP. No external Python packages needed.

---

## `_fetch_citations.py`

**Description.** Scrapes the Google Scholar profile for `lKxl5G4AAAAJ` (Subhadeep Sarkar) and writes fresh citation data to `_data/citations.json`.

**Output schema.**

```json
{
  "profile": {"name": "...", "affiliation": "...", "hindex": 0, "i10index": 0, "citedby": 0},
  "publications": [
    {"title": "...", "authors": "...", "year": 0, "venue": "...", "cited_by": 0, "url": "...", "eprint": ""}
  ]
}
```

If Scholar serves a CAPTCHA the script aborts with exit code 1 and does not touch the existing file.

**Prerequisites.** Python 3.8+, network access. Stdlib only.

---

## Typical usage

```bash
# publication.json is created and bootstrapped only by the personal site's _populate_papers.py.

# Routine: edit ~/Dropbox/_control/publication.json, then sync:
python3 _populate_lab_papers.py --dry-run    # see what would happen (safe: no artifact sync)
python3 _populate_lab_papers.py              # interactive — first read the image-deletion warning above

# Refresh Scholar citation counts only:
python3 _fetch_citations.py

# Then rebuild the site:
source /opt/homebrew/opt/chruby/share/chruby/chruby.sh && chruby ruby-3.2.2 && LANG=en_US.UTF-8 bundle exec jekyll build
```
