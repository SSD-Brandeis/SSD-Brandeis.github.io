#!/usr/bin/env python3
"""
_populate_lab_papers.py — keep `_bibliography/papers.bib` and the publications
section of the SSD Lab site in sync with a curated `publication.json`.

publication.json is shared with the personal site (_populate_papers.py), which
owns it: this script never bootstraps it, and rewrites keep every record.

Workflow on each invocation:
  0. Sync artifact folders from ~/Dropbox/_control/ to assets/ (_fetch_artifacts.py);
     skipped under --dry-run.
  1. Refuse to run if publication.json is missing (bootstrap it from the personal site).
  2. Compare each JSON record to its papers.bib counterpart; interactively
     apply diffs.
  3. For new JSON records, render and insert a fresh BibTeX entry; if the key
     is a DBLP key, fetch canonical metadata first and cross-verify.
  4. For records flagged `dblp_citation_verified: false` and a paper age <= 3
     years, retry DBLP — capture canonical data and flip the flag if found.
     The DBLP key is the `dblp` field, or the record key minus `DBLP:`.
  4b. Set each record's `status`: "consolidated" once backed by a verified DBLP
     entry, else "open"; report open papers, flagging any older than 2 years.
  5. Generate PDF preview thumbnails for any new local PDFs.
  6. Ask whether to refresh Google Scholar citations (runs _fetch_citations.py).
  7. Ask whether to rebuild the site.

Usage:
    ./_populate_lab_papers.py                # interactive
    ./_populate_lab_papers.py --dry-run      # show actions, never write
    ./_populate_lab_papers.py --yes          # auto-confirm every prompt
    ./_populate_lab_papers.py --bootstrap    # refused: publication.json is shared

Stdlib only — no external Python packages required.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]                      # site root
PUB_JSON = Path.home() / "Dropbox" / "_control" / "publication.json"
PAPERS_BIB = REPO_ROOT / "_bibliography" / "papers.bib"
PUB_PAGE = REPO_ROOT / "_pages" / "publications.md"   # may not exist on lab site
FULLTEXT_DIR = REPO_ROOT / "assets" / "fulltext"
PREVIEW_DIR = REPO_ROOT / "assets" / "img" / "publication_preview"
FETCH_CITATIONS_PY = SCRIPT_DIR / "_fetch_citations.py"
FETCH_ARTIFACTS_PY = SCRIPT_DIR / "_fetch_artifacts.py"


# DBLP-recheck cutoff: paper must be at most this many years old
DBLP_RECHECK_AGE_YEARS = 3

# Policy: every paper should be consolidated (backed by a verified DBLP entry)
# within this many years of publication; older open records are flagged.
CONSOLIDATE_WITHIN_YEARS = 2

# publication.json (shared with the personal site) holds papers at most this
# many years old (year >= current year - WINDOW_YEARS); only those are synced.
# Older papers live only in each site's papers.bib.
WINDOW_YEARS = 3

# Field name → display column width for diff
FIELDS_TO_COMPARE = (
    "type", "title", "authors", "year", "month",
    "venue_long", "venue_short", "pages", "volume", "number", "series",
    "publisher", "school", "editor", "isbn", "issn",
    "doi", "url", "abstract",
    "howpublished", "note",
    "pdf", "preview", "arxiv", "website", "video", "slides", "poster",
    "code_repo", "purchase",
    "selected", "bibtex_show",
)

# Type → bib `@kind{}` mapping
TYPE_TO_BIB = {
    "inproceedings": "inproceedings",
    "article":       "article",
    "book":          "book",
    "incollection":  "incollection",
    "phdthesis":     "phdthesis",
    "mastersthesis": "mastersthesis",
    "techreport":    "techreport",
    "misc":          "misc",
    "patent":        "misc",  # BibTeX has no @patent; render as @misc with howpublished
}

# Local-only fields preserved when re-syncing from DBLP
LOCAL_ONLY_FIELDS = {
    "abbr", "abstract", "pdf", "preview", "selected", "bibtex_show",
    "arxiv", "website", "video", "slides", "poster", "code_repo", "purchase",
}

# Sample template kept at the top of publication.json. Re-emitted on every
# rewrite so it never disappears, even if the user accidentally deletes it.
TEMPLATE_RECORD: Dict[str, Any] = {
    "_comment": ("TEMPLATE — copy this object and replace fields when adding a "
                 "new paper. The script skips any entry whose key starts with an "
                 "underscore. Required: key, type, title, authors, year. Leave "
                 "`dblp` empty until the paper is on DBLP; then set it to the "
                 "DBLP key (e.g. conf/sigmod/SarkarPSA20) with "
                 "dblp_citation_verified=false. `status` is set by the script."),
    "key": "_TEMPLATE_NEW_ENTRY",
    "status": "open",
    "dblp": "",
    "type": "inproceedings",
    "title": "",
    "authors": [],
    "editor": [],
    "year": 0,
    "month": "",
    "venue_long": "",
    "venue_short": "",
    "pages": "",
    "volume": "",
    "number": "",
    "series": "",
    "publisher": "",
    "isbn": "",
    "issn": "",
    "doi": "",
    "url": "",
    "abstract": "",
    "pdf": "",
    "preview": "",
    "arxiv": "",
    "website": "",
    "video": "",
    "slides": "",
    "poster": "",
    "code_repo": "",
    "purchase": "",
    "selected": True,
    "bibtex_show": True,
    "dblp_citation_verified": False,
}


# Header kept at the top of publication.json. Re-emitted on every rewrite.
# Must stay identical to the copy in the personal site's _populate_papers.py.
HEADER_LINES = [
    "publication.json — shared source of truth for recent papers: those at most "
    f"{WINDOW_YEARS} years old",
    f"(year >= current year - {WINDOW_YEARS}). Read by subhadeep.net "
    "(_populate_papers.py) and the SSD Lab site (_populate_lab_papers.py).",
    "Older papers live only in each site's _bibliography/papers.bib. When a paper "
    "ages out, the script",
    "says so; then delete its record here (it stays on both sites).",
    "",
    "status (set by the script on every run; don't edit it by hand):",
    '  "open"         — no verified DBLP entry yet; metadata may still change.',
    '  "consolidated" — backed by a verified DBLP entry (a `DBLP:` key or the '
    "`dblp` field, plus",
    "                   dblp_citation_verified: true); metadata is final.",
    "Every paper should be consolidated within "
    f"{CONSOLIDATE_WITHIN_YEARS} years of publication.",
    "See README.md in this folder.",
]


def in_window(rec: Dict[str, Any]) -> bool:
    """True if the paper is recent enough to belong in publication.json."""
    year = str(rec.get("year", ""))
    return year.isdigit() and current_year() - int(year) <= WINDOW_YEARS


def dblp_key_of(rec: Dict[str, Any]) -> str:
    """The record's DBLP key: the `dblp` field, else the key minus `DBLP:`."""
    if rec.get("dblp"):
        return rec["dblp"]
    key = rec.get("key", "")
    return key[len("DBLP:"):] if key.startswith("DBLP:") else ""


def derive_status(rec: Dict[str, Any]) -> str:
    """`consolidated` once backed by a verified DBLP entry, else `open`."""
    if dblp_key_of(rec) and rec.get("dblp_citation_verified"):
        return "consolidated"
    return "open"


def _with_status(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of `rec` with an up-to-date `status` placed right after `key`."""
    out: Dict[str, Any] = {}
    for k, v in rec.items():
        if k == "status":
            continue
        out[k] = v
        if k == "key":
            out["status"] = derive_status(rec)
    return out


def write_publication_json(records: List[Dict[str, Any]]) -> None:
    """Serialise records to publication.json, with HEADER_LINES and
    TEMPLATE_RECORD re-prepended as JSONC `//` comments so they always stay at
    the top. Pass *every* record — the file is shared with the personal site."""
    body = json.dumps([_with_status(r) for r in records], indent=2, ensure_ascii=False)
    header = "\n".join(("  // " + ln).rstrip() for ln in HEADER_LINES)
    tpl_text = json.dumps(TEMPLATE_RECORD, indent=2, ensure_ascii=False)
    tpl_block = "\n".join("  // " + ln for ln in tpl_text.split("\n")) + ","
    if body.startswith("[\n"):
        out = "[\n" + header + "\n  //\n" + tpl_block + "\n" + body[2:]
    else:
        out = body
    PUB_JSON.write_text(out + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="show planned actions but do not write any files")
    ap.add_argument("--yes", action="store_true",
                    help="auto-confirm every interactive prompt (use with care)")
    ap.add_argument("--bootstrap", action="store_true",
                    help="(re)generate publication.json from papers.bib and exit")
    return ap.parse_args()


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------

def ask(prompt: str, default: bool = True, force: Optional[bool] = None) -> bool:
    """Yes/no prompt. `force` overrides interactive (used by --yes)."""
    if force is not None:
        return force
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        ans = input(prompt + suffix).strip().lower()
        if ans == "":
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  please answer y or n")


def info(msg: str) -> None:
    print(f"  {msg}")


def heading(msg: str) -> None:
    print(f"\n=== {msg} ===")


# ---------------------------------------------------------------------------
# JSONC (JSON with comments) — pre-processor
# ---------------------------------------------------------------------------

def strip_jsonc_comments(text: str) -> str:
    """Strip `// line` and `/* block */` comments from a JSON-with-comments text.
    Comment-like sequences inside string literals are preserved."""
    out: List[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1]); i += 2; continue
            if c == '"':
                in_string = False
            i += 1; continue
        if c == '"':
            in_string = True; out.append(c); i += 1; continue
        if c == "/" and i + 1 < n:
            if text[i + 1] == "/":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if text[i + 1] == "*":
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i += 2
                continue
        out.append(c); i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Bib parsing (just enough to round-trip our entries)
# ---------------------------------------------------------------------------

def parse_bib_entry_block(text: str, start: int) -> Tuple[Dict[str, Any], int]:
    """Parse a single @TYPE{key, fields...} block starting at `start`.
    Returns (entry_dict, end_index_inclusive_of_closing_brace).
    """
    m = re.match(r"@(\w+)\s*\{\s*([A-Za-z0-9_:./\-]+)\s*,\s*", text[start:])
    if not m:
        raise ValueError(f"not an @entry at offset {start}")
    btype = m.group(1).lower()
    key = m.group(2)
    body_start = start + m.end()

    depth = 1
    i = body_start
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    body = text[body_start:i]
    end = i  # at the closing brace
    fields = parse_bib_fields(body)
    return {"type": btype, "key": key, "fields": fields, "raw": text[start:end + 1]}, end


def parse_bib_fields(body: str) -> Dict[str, str]:
    """Parse `field = {value}, field = "value", field = bareword,` style."""
    fields: Dict[str, str] = {}
    i = 0
    n = len(body)
    while i < n:
        while i < n and body[i] in " \t\n,":
            i += 1
        if i >= n:
            break
        m = re.match(r"(\w+)\s*=\s*", body[i:])
        if not m:
            break
        name = m.group(1).lower()
        i += m.end()
        if i >= n:
            break
        if body[i] == "{":
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                if body[j] == "{":
                    depth += 1
                elif body[j] == "}":
                    depth -= 1
                j += 1
            value = body[i + 1: j - 1]  # strip outer braces
            i = j
        elif body[i] == '"':
            j = i + 1
            while j < n and body[j] != '"':
                if body[j] == "\\":
                    j += 1
                j += 1
            value = body[i + 1: j]
            i = j + 1
        else:
            j = i
            while j < n and body[j] not in ",\n":
                j += 1
            value = body[i:j].strip()
            i = j
        fields[name] = value
    return fields


def load_bib_entries(path: Path) -> Dict[str, Dict[str, Any]]:
    """Return key → entry dict for all @TYPE{} blocks in the file."""
    text = path.read_text()
    entries: Dict[str, Dict[str, Any]] = {}
    for m in re.finditer(r"@\w+\{", text):
        try:
            entry, _ = parse_bib_entry_block(text, m.start())
        except ValueError:
            continue
        entries[entry["key"]] = entry
    return entries


# ---------------------------------------------------------------------------
# Bib emission
# ---------------------------------------------------------------------------

def render_authors(authors: List[str]) -> str:
    """List of names → BibTeX author string."""
    return " and ".join(authors)


def render_bib_entry(rec: Dict[str, Any], dblp_extras: Optional[Dict[str, str]] = None) -> str:
    """Render a publication.json record as a BibTeX entry block."""
    btype = TYPE_TO_BIB.get(rec.get("type", "misc"), "misc")
    key = rec["key"]
    out: List[Tuple[str, str]] = []

    if rec.get("authors"):
        out.append(("author", render_authors(rec["authors"])))
    if rec.get("editor"):
        out.append(("editor", render_authors(rec["editor"])))
    out.append(("title", rec.get("title", "")))

    if rec["type"] in ("inproceedings", "incollection") and rec.get("venue_long"):
        out.append(("booktitle", rec["venue_long"]))
    elif rec["type"] == "article" and rec.get("venue_long"):
        out.append(("journal", rec["venue_long"]))
    elif rec["type"] in ("phdthesis", "mastersthesis") and rec.get("school"):
        out.append(("school", rec["school"]))
    elif rec["type"] == "book" and rec.get("publisher"):
        pass
    elif rec.get("venue_long"):
        out.append(("booktitle", rec["venue_long"]))

    for field in ("series", "volume", "number", "pages"):
        if rec.get(field):
            out.append((field, str(rec[field])))
    if rec.get("year"):
        out.append(("year", str(rec["year"])))
    if rec.get("month"):
        out.append(("month", rec["month"]))
    if rec.get("publisher"):
        out.append(("publisher", rec["publisher"]))
    for field in ("howpublished", "note", "isbn", "issn", "doi", "url"):
        if rec.get(field):
            out.append((field, rec[field]))

    if dblp_extras:
        for f in ("timestamp", "biburl", "bibsource"):
            if f in dblp_extras:
                out.append((f, dblp_extras[f]))

    if rec.get("venue_short"):
        out.append(("abbr", rec["venue_short"]))
    if rec.get("abstract"):
        out.append(("abstract", rec["abstract"]))
    if rec.get("preview"):
        out.append(("preview", rec["preview"]))

    for field in ("pdf", "arxiv", "website", "video",
                  "slides", "poster", "code_repo", "purchase"):
        if rec.get(field):
            out.append((field, rec[field]))

    if rec.get("selected"):
        out.append(("selected", "true"))
    if rec.get("bibtex_show"):
        out.append(("bibtex_show", "true"))

    width = max(len(f) for f, _ in out)
    lines = [f"@{btype}{{{key},"]
    for f, v in out:
        lines.append(f"  {f.ljust(width)} = {{{v}}},")
    if lines[-1].endswith(","):
        lines[-1] = lines[-1][:-1]
    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bib → publication.json (bootstrap)
# ---------------------------------------------------------------------------

def bib_authors_to_list(s: str) -> List[str]:
    return [a.strip() for a in re.split(r"\band\b", s) if a.strip()]


def bib_to_record(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a parsed papers.bib entry to a publication.json record."""
    f = entry["fields"]
    btype = entry["type"]
    rec: Dict[str, Any] = {"key": entry["key"], "type": btype}
    if "title" in f:        rec["title"] = f["title"]
    if "author" in f:       rec["authors"] = bib_authors_to_list(f["author"])
    if "editor" in f:       rec["editor"] = bib_authors_to_list(f["editor"])
    if "year" in f:
        try: rec["year"] = int(f["year"])
        except ValueError: rec["year"] = f["year"]
    if "month" in f:        rec["month"] = f["month"]
    if "booktitle" in f:    rec["venue_long"] = f["booktitle"]
    elif "journal" in f:    rec["venue_long"] = f["journal"]
    if "series" in f:       rec["series"] = f["series"]
    if "volume" in f:       rec["volume"] = f["volume"]
    if "number" in f:       rec["number"] = f["number"]
    if "pages" in f:        rec["pages"] = f["pages"]
    if "publisher" in f:    rec["publisher"] = f["publisher"]
    if "school" in f:       rec["school"] = f["school"]
    if "isbn" in f:         rec["isbn"] = f["isbn"]
    if "issn" in f:         rec["issn"] = f["issn"]
    if "doi" in f:          rec["doi"] = f["doi"]
    if "url" in f:          rec["url"] = f["url"]
    if "howpublished" in f: rec["howpublished"] = f["howpublished"]
    if "note" in f:         rec["note"] = f["note"]
    if "abstract" in f:     rec["abstract"] = f["abstract"]
    if "abbr" in f:         rec["venue_short"] = f["abbr"]
    if "preview" in f:      rec["preview"] = f["preview"]
    for art in ("pdf", "arxiv", "website", "video",
                "slides", "poster", "code_repo", "purchase"):
        if art in f:
            rec[art] = f[art]
    if f.get("selected", "").lower() == "true":
        rec["selected"] = True
    if f.get("bibtex_show", "").lower() == "true":
        rec["bibtex_show"] = True
    rec["dblp_citation_verified"] = entry["key"].startswith("DBLP:") and "biburl" in f
    return rec


def bootstrap(args: argparse.Namespace) -> None:
    if PUB_JSON.exists() and not args.bootstrap:
        return
    if PUB_JSON.exists() and args.bootstrap:
        info(f"overwriting existing {PUB_JSON.name}")
    heading("Bootstrapping publication.json from papers.bib")
    bib = load_bib_entries(PAPERS_BIB)
    records = [bib_to_record(e) for e in bib.values()]
    records.sort(key=lambda r: (-(r.get("year") or 0), r.get("key", "")))
    if args.dry_run:
        info(f"DRY-RUN: would write {len(records)} records to {PUB_JSON}")
        return
    write_publication_json(records)
    info(f"wrote {len(records)} records to {PUB_JSON}")


# ---------------------------------------------------------------------------
# DBLP fetch
# ---------------------------------------------------------------------------

class DBLPError(Exception):
    """DBLP could not be asked, so whether it has the record is unknown."""


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "populate_papers/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def fetch_dblp_record(dblp_key: str) -> Optional[Dict[str, Any]]:
    """Fetch DBLP's BibTeX entry for a key like 'conf/sigmod/SarkarPSA20'.
    dblp.org puts its .bib export behind a bot check (Anubis), so when that
    fails the entry is rebuilt from DBLP's SPARQL endpoint, which is meant for
    scripts. Returns the parsed entry, or None if DBLP has no such record;
    raises DBLPError with the cause if neither source answers.
    """
    try:
        text = _http_get(f"https://dblp.org/rec/{dblp_key}.bib")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        problem = f"HTTP {e.code} {e.reason}"
    except (urllib.error.URLError, TimeoutError) as e:
        problem = f"network error: {getattr(e, 'reason', e)}"
    else:
        m = re.search(r"@\w+\{", text)
        if m:
            return parse_bib_entry_block(text, m.start())[0]
        problem = ("blocked by dblp.org's bot check" if "not a bot" in text
                   else "the response had no BibTeX entry")
    info(f"DBLP .bib export: {problem}; asking DBLP's SPARQL endpoint instead")
    try:
        return _dblp_entry_via_sparql(dblp_key)
    except urllib.error.HTTPError as e:
        raise DBLPError(f"{problem}; SPARQL endpoint: HTTP {e.code} {e.reason}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise DBLPError(f"{problem}; SPARQL endpoint: network error: "
                        f"{getattr(e, 'reason', e)}") from e
    except (ValueError, KeyError) as e:
        raise DBLPError(f"{problem}; SPARQL endpoint: unexpected response ({e})") from e


DBLP_SPARQL = "https://sparql.dblp.org/sparql"


def _sparql(query: str) -> List[Dict[str, Any]]:
    url = DBLP_SPARQL + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
    return json.loads(_http_get(url))["results"]["bindings"]


def _sparql_props(iri: str) -> Dict[str, List[str]]:
    """Property local name → values, for one DBLP resource."""
    props: Dict[str, List[str]] = {}
    for b in _sparql(f"SELECT ?p ?o WHERE {{ <{iri}> ?p ?o }}"):
        name = re.split(r"[#/]", b["p"]["value"])[-1]
        props.setdefault(name, []).append(b["o"]["value"])
    return props


def _sparql_names(iri: str) -> List[str]:
    """Authors of a paper (or editors of a proceedings volume), in order,
    without DBLP's homonym numbers ('Subhadeep Sarkar 0001')."""
    rows = _sparql(
        "PREFIX dblp: <https://dblp.org/rdf/schema#> "
        f"SELECT ?name ?ord WHERE {{ <{iri}> dblp:hasSignature ?s . "
        "?s dblp:signatureDblpName ?name ; dblp:signatureOrdinal ?ord }")
    rows.sort(key=lambda b: int(b["ord"]["value"]))
    return [re.sub(r"\s+\d{4}$", "", b["name"]["value"]) for b in rows]


def _bib_text(text: str, is_title: bool = False) -> str:
    """DBLP text → its BibTeX form: all-caps words (and a one-letter word not
    at the start) braced, as in DBLP's .bib export. Titles lose DBLP's final
    period; abbreviations like 'Syst.' keep theirs."""
    if is_title:
        text = text.rstrip(".")
    words = text.replace("&", "{\\&}").split(" ")
    for i, w in enumerate(words):
        m = re.fullmatch(r"([A-Z][A-Z0-9/]*)([,:;?!]?)", w)
        if m and (len(m.group(1)) > 1 or i > 0):
            words[i] = "{" + m.group(1) + "}" + m.group(2)
    return " ".join(words)


def _dblp_entry_via_sparql(dblp_key: str) -> Optional[Dict[str, Any]]:
    """Rebuild DBLP's .bib entry for `dblp_key` from its SPARQL endpoint, with
    the fields compare_with_dblp() uses. None if DBLP has no such record."""
    iri = f"https://dblp.org/rec/{dblp_key}"
    p = _sparql_props(iri)
    if "title" not in p:
        return None
    first = lambda props, name: props.get(name, [""])[0]
    f: Dict[str, str] = {}
    authors = _sparql_names(iri)
    if authors:
        f["author"] = " and ".join(authors)
    f["title"] = _bib_text(first(p, "title"), is_title=True)
    if "publishedInJournal" in p:
        f["journal"] = _bib_text(first(p, "publishedInJournal"))
        f["volume"] = first(p, "publishedInJournalVolume")
        f["number"] = first(p, "publishedInJournalVolumeIssue")
    if "publishedAsPartOf" in p:
        parent_iri = first(p, "publishedAsPartOf")
        parent = _sparql_props(parent_iri)
        f["booktitle"] = _bib_text(first(parent, "title"), is_title=True)
        editors = _sparql_names(parent_iri)
        if editors:
            f["editor"] = " and ".join(editors)
        f["series"] = first(parent, "publishedInSeries")
        f["volume"] = first(parent, "publishedInSeriesVolume")
        f["publisher"] = _bib_text(first(parent, "publishedBy"))
    f["pages"] = re.sub(r"(?<!-)-(?!-)", "--", first(p, "pagination"))
    f["year"] = first(p, "yearOfEvent") or first(p, "yearOfPublication")
    doi = first(p, "doi").replace("https://doi.org/", "")
    f["doi"] = doi.upper().replace("_", "\\_")
    f["url"] = first(p, "primaryDocumentPage").replace("_", "\\_")
    f["biburl"] = f"https://dblp.org/rec/{dblp_key}.bib"
    f["bibsource"] = "dblp computer science bibliography, https://dblp.org"
    btype = first(p, "bibtexType").split("#")[-1].lower() or "misc"
    return {"type": btype, "key": f"DBLP:{dblp_key}",
            "fields": {k: v for k, v in f.items() if v}}


def lookup_dblp(dblp_key: str) -> Optional[Dict[str, Any]]:
    """fetch_dblp_record(), saying why nothing came back."""
    try:
        entry = fetch_dblp_record(dblp_key)
    except DBLPError as e:
        info(f"could not check DBLP for {dblp_key}: {e}")
        return None
    if entry is None:
        info(f"DBLP has no record for {dblp_key} (yet)")
    return entry


# ---------------------------------------------------------------------------
# Diffing & sync
# ---------------------------------------------------------------------------

def _norm(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str) and v.strip() == "":
        return None
    if isinstance(v, (list, tuple)) and len(v) == 0:
        return None
    if v is False:
        return None
    return v


def field_diff(rec_json: Dict[str, Any], rec_bib: Dict[str, Any]) -> List[Tuple[str, Any, Any]]:
    diffs: List[Tuple[str, Any, Any]] = []
    bib_view = bib_to_record(rec_bib)
    for f in FIELDS_TO_COMPARE:
        jv = _norm(rec_json.get(f))
        bv = _norm(bib_view.get(f))
        if jv == bv:
            continue
        if f == "type" and TYPE_TO_BIB.get(str(jv)) == bv:
            continue
        diffs.append((f, bib_view.get(f), rec_json.get(f)))
    return diffs


def show_diffs(key: str, diffs: List[Tuple[str, Any, Any]]) -> None:
    heading(f"{key}: {len(diffs)} field(s) differ")
    for f, bib_val, json_val in diffs:
        b = json.dumps(bib_val, ensure_ascii=False) if bib_val is not None else "(absent)"
        j = json.dumps(json_val, ensure_ascii=False) if json_val is not None else "(absent)"
        print(f"  {f}:")
        print(f"    bib:  {b}")
        print(f"    json: {j}")


# ---------------------------------------------------------------------------
# Bib file mutation
# ---------------------------------------------------------------------------

def replace_bib_entry(text: str, key: str, new_block: str) -> Tuple[str, bool]:
    pat = re.compile(r"@\w+\{\s*" + re.escape(key) + r"\s*,")
    m = pat.search(text)
    if not m:
        return text, False
    start = m.start()
    depth = 0
    i = start
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return text[:start] + new_block + text[i + 1:], True


def prepend_bib_entry(text: str, new_block: str) -> str:
    m = re.search(r"^@\w+\{", text, re.MULTILINE)
    if not m:
        return new_block + "\n"
    head = text[:m.start()]
    rest = text[m.start():]
    return head + new_block + "\n\n" + rest


def order_bib_like_json(text: str, keys: List[str]) -> str:
    """Rearrange the entries whose key is in `keys` so they appear in that
    order (the publications page lists a year's papers in papers.bib order).
    They swap among the positions they already hold; other entries stay put."""
    spans: Dict[str, Tuple[int, int]] = {}
    for m in re.finditer(r"^@\w+\{\s*([^,\s]+)\s*,", text, re.MULTILINE):
        if m.group(1) in keys and m.group(1) not in spans:
            _, end = parse_bib_entry_block(text, m.start())
            spans[m.group(1)] = (m.start(), end + 1)
    slots = sorted(spans.values())
    wanted = [k for k in keys if k in spans]
    out, pos = [], 0
    for (start, end), key in zip(slots, wanted):
        s, e = spans[key]
        out.append(text[pos:start])
        out.append(text[s:e])
        pos = end
    out.append(text[pos:])
    return "".join(out)


# ---------------------------------------------------------------------------
# Preview generation
# ---------------------------------------------------------------------------

def ensure_publications_years(records: List[Dict[str, Any]], dry_run: bool) -> bool:
    if not PUB_PAGE.exists():
        return False
    text = PUB_PAGE.read_text()
    m = re.search(r"^years:\s*\[([^\]]*)\]\s*$", text, re.MULTILINE)
    if not m:
        info(f"WARN: {PUB_PAGE.name} has no `years:` front-matter; skipping")
        return False
    existing = [int(y.strip()) for y in m.group(1).split(",") if y.strip().isdigit()]
    needed = {int(r["year"]) for r in records if r.get("year")}
    merged = sorted(set(existing) | needed, reverse=True)
    if merged == existing:
        return False
    added = sorted(set(merged) - set(existing), reverse=True)
    info(f"adding year(s) {added} to {PUB_PAGE.name}")
    if dry_run:
        info(f"DRY-RUN: would update {PUB_PAGE.name}")
        return True
    new_line = "years: [" + ", ".join(str(y) for y in merged) + "]"
    PUB_PAGE.write_text(text[:m.start()] + new_line + text[m.end():])
    return True


def derive_pdf_filename(title: str) -> str:
    """Title → expected filename in `assets/fulltext/`."""
    s = re.sub(r"<[^>]+>", "", title)
    s = re.sub(r"[{}]", "", s)
    s = s.replace(":", "")
    s = re.sub(r"\s+", "_", s.strip())
    return s + ".pdf"


def resolve_pdf_for_new_entry(rec: Dict[str, Any], force_yes: bool) -> None:
    if rec.get("pdf"):
        return
    candidate = derive_pdf_filename(rec.get("title", ""))
    candidate_path = FULLTEXT_DIR / candidate
    if candidate_path.exists():
        info(f"auto-detected PDF: {candidate}")
        if ask("use this PDF?", default=True, force=force_yes or None):
            rec["pdf"] = candidate
            return
    if force_yes:
        return
    # Accept only a file that exists (or a URL), so a stray "y" can't become the pdf.
    while True:
        typed = input("  PDF filename in assets/fulltext/ (blank if there's no PDF yet): ").strip()
        if not typed or "://" in typed or (FULLTEXT_DIR / typed).is_file():
            break
        info(f"no such file: assets/fulltext/{typed}")
    if typed:
        rec["pdf"] = typed


def prompt_optional_fields(rec: Dict[str, Any], force_yes: bool) -> None:
    if force_yes:
        return
    field_prompts = [
        ("abstract",   "Abstract (single line)"),
        ("doi",        "DOI"),
        ("url",        "URL"),
        ("arxiv",      "arXiv ID (e.g. 2006.04777)"),
        ("code_repo",  "Code repo URL"),
        ("slides",     "Slides (filename in assets/resources/ or URL)"),
        ("poster",     "Poster (filename or URL)"),
        ("video",      "Video URL"),
        ("website",    "Project website URL"),
    ]
    empty = [(f, label) for f, label in field_prompts if not _norm(rec.get(f))]
    if not empty:
        return
    if not ask(f"fill any of {len(empty)} optional fields now?", default=True):
        return
    info("press Enter to skip a field; type a value to set it.")
    for field, label in empty:
        val = input(f"  {label}: ").strip()
        if val:
            rec[field] = val


def has_imagemagick() -> bool:
    for cmd in ("/opt/homebrew/bin/magick", "magick"):
        try:
            subprocess.run([cmd, "-version"], capture_output=True, check=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return False


def _magick_cmd() -> str:
    if Path("/opt/homebrew/bin/magick").exists():
        return "/opt/homebrew/bin/magick"
    return "magick"


def generate_preview(pdf_filename: str, dry_run: bool) -> bool:
    """Generate `assets/img/publication_preview/<basename>.png` if missing."""
    pdf_path = FULLTEXT_DIR / pdf_filename
    if not pdf_path.exists():
        info(f"skip preview: PDF missing → {pdf_path}")
        return False
    out_path = PREVIEW_DIR / (pdf_path.stem + ".png")
    if out_path.exists():
        return False
    if dry_run:
        info(f"DRY-RUN: would generate preview {out_path.name}")
        return True
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [_magick_cmd(), "-density", "150", f"{pdf_path}[0]",
         "-resize", "400x", "-quality", "90", str(out_path)],
        check=True,
    )
    info(f"generated preview {out_path.name}")
    return True


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def current_year() -> int:
    return dt.date.today().year


def process_existing(rec: Dict[str, Any], bib_entry: Dict[str, Any],
                     bib_text: str, args: argparse.Namespace) -> Tuple[str, bool]:
    diffs = field_diff(rec, bib_entry)
    if not diffs:
        return bib_text, False
    show_diffs(rec["key"], diffs)
    if not ask("apply these changes to papers.bib?", default=True, force=args.yes or None):
        return bib_text, False
    new_block = render_bib_entry(rec, _dblp_extras_from(bib_entry))
    if args.dry_run:
        info("DRY-RUN: would replace bib entry")
        return bib_text, True
    new_text, ok = replace_bib_entry(bib_text, rec["key"], new_block)
    if not ok:
        info(f"WARN: failed to replace {rec['key']} in papers.bib (key not found)")
        return bib_text, False
    return new_text, True


def _dblp_extras_from(bib_entry: Dict[str, Any]) -> Dict[str, str]:
    return {f: bib_entry["fields"][f] for f in ("timestamp", "biburl", "bibsource")
            if f in bib_entry["fields"]}


def _title_id(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"<[^>]+>", "", title).lower())


def find_renamed_entry(rec: Dict[str, Any], bib_text: str, json_keys: set) -> Optional[str]:
    """Key of a papers.bib entry for the same paper (same title) under a key
    publication.json no longer has, e.g. after a record's `key` was changed.
    That entry is replaced rather than the paper being added twice."""
    want = _title_id(rec.get("title", ""))
    if not want:
        return None
    for m in re.finditer(r"^@\w+\{", bib_text, re.MULTILINE):
        try:
            entry, _ = parse_bib_entry_block(bib_text, m.start())
        except ValueError:
            continue
        if entry["key"] not in json_keys and _title_id(entry["fields"].get("title", "")) == want:
            return entry["key"]
    return None


def process_new(rec: Dict[str, Any], bib_text: str, args: argparse.Namespace,
                json_keys: set) -> Tuple[str, bool]:
    heading(f"NEW: {rec['key']}")
    print(json.dumps(rec, indent=2, ensure_ascii=False))
    old_key = find_renamed_entry(rec, bib_text, json_keys)
    if old_key:
        info(f"papers.bib already lists this paper as {old_key}; that entry will be replaced")

    dblp_extras: Dict[str, str] = {}
    bare = dblp_key_of(rec)
    if bare and not rec.get("dblp_citation_verified", False):
        info(f"verifying with DBLP: {bare}")
        dblp = lookup_dblp(bare)
        if dblp:
            dblp_diff = compare_with_dblp(rec, dblp)
            if dblp_diff:
                heading("DBLP returned different metadata")
                for f, dval, jval in dblp_diff:
                    print(f"  {f}:")
                    print(f"    json: {json.dumps(jval, ensure_ascii=False)}")
                    print(f"    dblp: {json.dumps(dval, ensure_ascii=False)}")
                if ask("adopt DBLP values for these fields?", default=True, force=args.yes or None):
                    apply_dblp(rec, dblp)
            rec["dblp_citation_verified"] = True
            dblp_extras = {f: dblp["fields"][f] for f in ("timestamp", "biburl", "bibsource")
                           if f in dblp["fields"]}
        else:
            rec["dblp_citation_verified"] = False

    if not ask("add this entry to papers.bib?", default=True, force=args.yes or None):
        return bib_text, False
    resolve_pdf_for_new_entry(rec, args.yes)
    rec.setdefault("selected", True)
    if rec["selected"] is False:
        rec["selected"] = True
    new_block = render_bib_entry(rec, dblp_extras or None)
    if args.dry_run:
        info(f"DRY-RUN: would {'replace ' + old_key if old_key else 'append entry'}")
        return bib_text, True
    if old_key:
        return replace_bib_entry(bib_text, old_key, new_block)[0], True
    return prepend_bib_entry(bib_text, new_block), True


# Fields taken from DBLP when a paper is consolidated. `venue_long` only when
# the record has none: DBLP's booktitle carries edition, place and dates.
DBLP_FIELDS = ("title", "authors", "year", "venue_long",
               "pages", "volume", "number", "publisher", "doi", "url")


def compare_with_dblp(rec: Dict[str, Any], dblp: Dict[str, Any]) -> List[Tuple[str, Any, Any]]:
    dblp_view = bib_to_record(dblp)
    diffs: List[Tuple[str, Any, Any]] = []
    for f in DBLP_FIELDS:
        jv = rec.get(f)
        dv = dblp_view.get(f)
        if f == "venue_long" and jv:
            continue  # keep the site's own venue text (DBLP's is long)
        if dv is not None and jv != dv:
            diffs.append((f, dv, jv))
    return diffs


def apply_dblp(rec: Dict[str, Any], dblp: Dict[str, Any]) -> None:
    dblp_view = bib_to_record(dblp)
    for f in DBLP_FIELDS:
        if f == "venue_long" and rec.get(f):
            continue
        if f in dblp_view:
            rec[f] = dblp_view[f]


def recheck_dblp_missing(rec: Dict[str, Any], bib_text: str,
                         args: argparse.Namespace) -> Tuple[str, bool]:
    bare = dblp_key_of(rec)
    if not bare or rec.get("dblp_citation_verified", True):
        return bib_text, False
    age = current_year() - int(rec.get("year", 0))
    if age > DBLP_RECHECK_AGE_YEARS:
        return bib_text, False
    info(f"re-checking DBLP for {rec['key']} ({bare}, age {age}y) ...")
    dblp = lookup_dblp(bare)
    if not dblp:
        info("  will retry next run")
        return bib_text, False
    info("  found! showing diff vs JSON")
    diffs = compare_with_dblp(rec, dblp)
    for f, dv, jv in diffs:
        print(f"    {f}: dblp={json.dumps(dv, ensure_ascii=False)}  json={json.dumps(jv, ensure_ascii=False)}")
    if ask("adopt DBLP values & mark verified?", default=True, force=args.yes or None):
        apply_dblp(rec, dblp)
        rec["dblp_citation_verified"] = True
        new_block = render_bib_entry(rec, _dblp_extras_from(dblp))
        if args.dry_run:
            info("DRY-RUN: would update bib entry from DBLP")
            return bib_text, True
        new_text, ok = replace_bib_entry(bib_text, rec["key"], new_block)
        if ok:
            return new_text, True
        return prepend_bib_entry(bib_text, new_block), True
    return bib_text, False


def report_status(records: List[Dict[str, Any]]) -> None:
    """List open papers, flagging any past the consolidation deadline."""
    open_recs = [r for r in records if derive_status(r) == "open"]
    heading(f"Status: {len(records) - len(open_recs)} consolidated, {len(open_recs)} open")
    for r in open_recs:
        age = current_year() - int(r.get("year") or 0)
        late = (f"  <- over {CONSOLIDATE_WITHIN_YEARS} years without a verified DBLP entry"
                if age > CONSOLIDATE_WITHIN_YEARS else "")
        info(f"open: {r['key']} ({r.get('year')}){late}")


def main() -> int:
    args = parse_args()

    # 1. publication.json is shared with (and owned by) the personal site, so
    # never (re)generate it from the lab's smaller papers.bib.
    if args.bootstrap or not PUB_JSON.exists():
        info(f"ERROR: {PUB_JSON} is shared with the personal site and is not "
             f"bootstrapped from here. If it is missing, run the personal "
             f"site's _populate_papers.py --bootstrap.")
        return 1

    # 0. Sync artifact folders from Dropbox/_control/ to assets/
    if args.dry_run:
        info("DRY-RUN: skipping artifact sync")
    elif FETCH_ARTIFACTS_PY.exists():
        subprocess.run(["python3", str(FETCH_ARTIFACTS_PY)], check=False)
    else:
        info(f"WARN: {FETCH_ARTIFACTS_PY.name} not found, skipping artifact sync")

    # 2. Load both sides
    raw = PUB_JSON.read_text()
    had_comments = ("//" in raw) or ("/*" in raw)
    all_records: List[Dict[str, Any]] = json.loads(strip_jsonc_comments(raw))
    json_keys = {r.get("key", "") for r in all_records}
    # A space-only value (" ") means "not filled in yet"; treat it as empty so
    # it doesn't render as a blank link or badge on the site.
    for r in all_records:
        for f, v in r.items():
            if isinstance(v, str) and v and not v.strip():
                r[f] = [] if f == "editor" else ""
    records = [r for r in all_records
               if not r.get("key", "").startswith("_")
               and not r.get("placeholder")
               and in_window(r)]
    bib_entries = load_bib_entries(PAPERS_BIB)
    bib_text = PAPERS_BIB.read_text()

    json_dirty = False
    bib_dirty = False
    new_pdfs: List[str] = []

    # 3. Existing — compare and sync
    for rec in records:
        key = rec["key"]
        if key in bib_entries:
            bib_text, changed = process_existing(rec, bib_entries[key], bib_text, args)
            if changed:
                bib_dirty = True
            bib_text, drec = recheck_dblp_missing(rec, bib_text, args)
            if drec:
                json_dirty = True
                bib_dirty = True

    # 4. New — append entries
    for rec in records:
        key = rec["key"]
        if key not in bib_entries:
            bib_text, changed = process_new(rec, bib_text, args, json_keys)
            if changed:
                bib_dirty = True
                json_dirty = True
                if rec.get("pdf"):
                    new_pdfs.append(rec["pdf"])

    # 4b. Keep every record's `status` current (open / consolidated). Without a
    # DBLP key nothing can be verified, so keep that flag honest too.
    for r in records:
        if not dblp_key_of(r) and r.get("dblp_citation_verified"):
            r["dblp_citation_verified"] = False
            json_dirty = True
    if any(r.get("status") != derive_status(r) for r in records):
        json_dirty = True

    # 4c. List the papers in papers.bib in publication.json's order.
    ordered = order_bib_like_json(bib_text, [r["key"] for r in records])
    if ordered != bib_text:
        info("reordering papers.bib to match publication.json")
        bib_text, bib_dirty = ordered, True

    # 5. Persist — write back *all* records (not just the ones this site
    # syncs), since publication.json is shared with the personal site.
    if json_dirty and not args.dry_run:
        if had_comments:
            info(f"NOTE: {PUB_JSON.name} contained // or /* */ comments — "
                 "those are stripped on rewrite (the header and sample template "
                 "are re-prepended automatically; other comments need to be re-added).")
        write_publication_json(all_records)
        info(f"wrote {PUB_JSON.name}")
    if bib_dirty and not args.dry_run:
        PAPERS_BIB.write_text(bib_text)
        info(f"wrote {PAPERS_BIB.relative_to(REPO_ROOT)}")

    # 5a. Report open vs. consolidated papers
    report_status(records)

    # 5b. Update publications.md year list if the page exists
    ensure_publications_years(records, args.dry_run)

    # 6. Generate previews for any entry whose thumbnail is missing
    pdfs_to_preview: List[str] = list(new_pdfs)
    for r in records:
        pdf = r.get("pdf")
        if not pdf or pdf in pdfs_to_preview:
            continue
        if not (PREVIEW_DIR / (Path(pdf).stem + ".png")).exists():
            pdfs_to_preview.append(pdf)
    if pdfs_to_preview:
        if not has_imagemagick():
            info("WARN: ImageMagick not on PATH; skipping preview generation")
        else:
            heading("Generating PDF previews")
            for pdf in pdfs_to_preview:
                generate_preview(pdf, args.dry_run)

    # 7. Optionally refresh citations
    if not args.yes and not args.dry_run \
            and ask("refresh Google Scholar citations now?", default=False):
        if FETCH_CITATIONS_PY.exists():
            subprocess.run(["python3", str(FETCH_CITATIONS_PY)], check=False)
            info("citations refreshed")
        else:
            info(f"WARN: {FETCH_CITATIONS_PY.name} not found")

    # 8. Rebuild the site (optional)
    if not args.yes and not args.dry_run and ask("rebuild the site?", default=False):
        try:
            subprocess.run(
                ["bash", "-c",
                 "source /opt/homebrew/opt/chruby/share/chruby/chruby.sh && "
                 "chruby ruby-3.2.2 && LANG=en_US.UTF-8 bundle exec jekyll build"],
                cwd=REPO_ROOT, check=True,
            )
            info("site rebuilt")
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            info(f"WARN: jekyll build failed: {e}")

    print()
    info("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
