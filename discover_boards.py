"""
discover_boards.py - test a long list of company names and keep the ones that have a
working Greenhouse or Lever job board. Working names are merged into sources.json.

Usage:
    python discover_boards.py candidates_seed.txt [more_files.txt ...]

Each line of a candidates file is a company name (e.g. "Stripe", "Hash Corp").
Name variants are tried automatically ("hashcorp", "hash-corp"). Names that don't
match a board are simply dropped, so guessing wrong costs nothing.

Where to find more candidate names:
  - Company names from remote-friendly lists, e.g. the remoteintech/remote-jobs
    repository on GitHub.
  - Google: site:boards.greenhouse.io "remote" full stack  (the NAME in the URL is the board)
  - Companies you have seen hiring on LinkedIn or job boards.
"""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

SOURCES = Path("sources.json")


def variants(name: str) -> set[str]:
    base = re.sub(r"[^a-z0-9 -]", "", name.lower()).strip()
    return {base.replace(" ", ""), base.replace(" ", "-")} - {""}


def check_greenhouse(slug: str) -> bool:
    try:
        r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=15)
        return r.status_code == 200 and len(r.json().get("jobs", [])) > 0
    except Exception:
        return False


def check_lever(slug: str) -> bool:
    try:
        r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=15)
        if r.status_code != 200:
            return False
        data = r.json()
        return isinstance(data, list) and len(data) > 0
    except Exception:
        return False


def main(files: list[str]) -> None:
    names = []
    for f in files:
        names += [ln.strip() for ln in Path(f).read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    slugs = sorted({v for n in names for v in variants(n)})
    print(f"Testing {len(slugs)} name variants from {len(names)} companies...")

    def test(slug: str):
        return slug, check_greenhouse(slug), check_lever(slug)

    with ThreadPoolExecutor(10) as ex:
        results = list(ex.map(test, slugs))

    sources = json.loads(SOURCES.read_text()) if SOURCES.exists() else {}
    gh = set(sources.get("greenhouse", []))
    lv = set(sources.get("lever", []))
    new_gh = {s for s, g, _ in results if g} - gh
    new_lv = {s for s, _, l in results if l} - lv
    sources["greenhouse"] = sorted(gh | new_gh)
    sources["lever"] = sorted(lv | new_lv)
    SOURCES.write_text(json.dumps(sources, indent=2))

    print(f"Added {len(new_gh)} Greenhouse and {len(new_lv)} Lever boards.")
    print(f"sources.json now has {len(sources['greenhouse'])} Greenhouse and {len(sources['lever'])} Lever boards.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1:])