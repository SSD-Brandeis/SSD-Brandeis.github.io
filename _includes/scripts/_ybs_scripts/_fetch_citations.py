#!/usr/bin/env python3
"""
_fetch_citations.py — scrape Google Scholar for fresh citation data and write
the JSON the site reads via `site.data.citations`.

Fetches the user's Scholar profile directly, parses it, and writes the same
JSON structure to:

  - _data/citations.json                                      (consumed by site)

Output schema (matches what bib.html expects):

  {
    "profile": {"name": ..., "affiliation": ..., "hindex": int,
                "i10index": int, "citedby": int},
    "publications": [
      {"title": ..., "authors": ..., "year": int, "venue": ...,
       "cited_by": int, "url": ..., "eprint": ""},
      ...
    ]
  }

If Google Scholar serves a CAPTCHA / rate-limit page, the script aborts with
a non-zero exit code and does NOT touch the existing JSON files.

Stdlib only.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List


SCHOLAR_USER_ID = "lKxl5G4AAAAJ"  # Subhadeep Sarkar
PAGESIZE = 100  # one request per 100 papers; up to ~3 pages typical
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
OUTPUTS = [
    REPO_ROOT / "_data" / "citations.json",
]


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_profile_page(start: int = 0) -> str:
    url = (f"https://scholar.google.com/citations?hl=en"
           f"&user={SCHOLAR_USER_ID}&cstart={start}&pagesize={PAGESIZE}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def looks_like_captcha(html: str) -> bool:
    if "captcha" in html.lower():
        return True
    if "unusual traffic" in html.lower():
        return True
    if 'id="gsc_prf_in"' not in html:
        return True
    return False


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

def parse_profile(html: str) -> Dict[str, Any]:
    profile: Dict[str, Any] = {}
    m = re.search(r'<div id="gsc_prf_in">([^<]+)</div>', html)
    if m:
        profile["name"] = html_lib.unescape(m.group(1))
    # Affiliation: first .gsc_prf_il (Scholar puts the institution there)
    m = re.search(r'<div class="gsc_prf_il"[^>]*>([^<]+)</div>', html)
    if m:
        profile["affiliation"] = html_lib.unescape(m.group(1))
    # Stats table: 6 cells (citedby total + since-N, h-index total + since-N,
    # i10-index total + since-N).
    stats = re.findall(r'<td class="gsc_rsb_std">(\d+)</td>', html)
    if len(stats) >= 5:
        profile["citedby"] = int(stats[0])
        profile["hindex"] = int(stats[2])
        profile["i10index"] = int(stats[4])
    return profile


def _cell_text(fragment: str) -> str:
    """HTML fragment → plain text. NFKC turns styled Unicode (e.g. Scholar's
    "T𝒆𝒙𝑩𝒆𝒏𝒄𝒉") into plain letters, so the site can match titles."""
    text = html_lib.unescape(re.sub(r"<[^>]+>", "", fragment))
    return " ".join(unicodedata.normalize("NFKC", text).split())


def parse_publications(html: str) -> List[Dict[str, Any]]:
    pubs: List[Dict[str, Any]] = []
    for row_m in re.finditer(r'<tr class="gsc_a_tr">(.*?)</tr>', html, re.DOTALL):
        row = row_m.group(1)
        # The title <a> may have href before or after class — handle either.
        # Titles and venues may contain markup (e.g. "<i>TexBench</i>: ...").
        link_m = re.search(r'<a ([^>]*class="gsc_a_at"[^>]*)>(.*?)</a>', row, re.DOTALL)
        if not link_m:
            continue
        attrs = link_m.group(1)
        title = _cell_text(link_m.group(2))
        href_m = re.search(r'href="([^"]+)"', attrs)
        url_path = html_lib.unescape(href_m.group(1)) if href_m else ""
        # Two .gs_gray divs: authors then venue (the venue ends in a year span)
        gray = re.findall(r'<div class="gs_gray">(.*?)</div>', row, re.DOTALL)
        authors = _cell_text(gray[0]) if len(gray) > 0 else ""
        venue = _cell_text(re.sub(r'<span class="gs_oph">.*?</span>', "", gray[1])) if len(gray) > 1 else ""
        cited_m = re.search(r'<a [^>]*class="gsc_a_ac[^"]*"[^>]*>([^<]*)</a>', row)
        cited_by = 0
        if cited_m and cited_m.group(1).strip().isdigit():
            cited_by = int(cited_m.group(1).strip())
        year_m = re.search(r'<span class="gsc_a_h[^"]*">(\d{4})</span>', row)
        year = int(year_m.group(1)) if year_m else 0
        pubs.append({
            "title": title,
            "authors": authors,
            "year": year,
            "venue": venue,
            "abstract": "",
            "cited_by": cited_by,
            "url": "https://scholar.google.com" + url_path,
            "eprint": "",
        })
    return pubs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def fetch_all() -> Dict[str, Any]:
    """Fetch the user's profile + all paginated publication rows."""
    page = fetch_profile_page(0)
    if looks_like_captcha(page):
        raise RuntimeError(
            "Google Scholar served a CAPTCHA / blocked the request. "
            "Try again later from a different network, or use a VPN."
        )
    profile = parse_profile(page)
    pubs = parse_publications(page)
    # Pagination — keep fetching until a page yields fewer than PAGESIZE rows.
    start = PAGESIZE
    while True:
        if len(pubs) < start:
            break
        nxt = fetch_profile_page(start)
        if looks_like_captcha(nxt):
            print(f"  WARN: page at start={start} blocked; stopping early", file=sys.stderr)
            break
        more = parse_publications(nxt)
        if not more:
            break
        pubs.extend(more)
        start += PAGESIZE
    return {"profile": profile, "publications": pubs}


def main() -> int:
    print(f"Fetching Scholar profile for user={SCHOLAR_USER_ID} ...", file=sys.stderr)
    try:
        data = fetch_all()
    except (urllib.error.URLError, RuntimeError, TimeoutError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    n = len(data["publications"])
    cb = data["profile"].get("citedby", "?")
    print(f"  fetched {n} publications, total citedby={cb}", file=sys.stderr)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    for path in OUTPUTS:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload)
        print(f"  wrote {path.relative_to(REPO_ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
