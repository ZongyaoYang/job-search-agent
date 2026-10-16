"""
job_search.py - PHASE 1: collect US job postings that match your experience and save
them to a SQLite database plus CSV lists. No Claude/API cost, no applying.

Sources:
  * Greenhouse / Lever company boards listed in sources.json   (always on, free)
  * Adzuna job aggregator                                      (optional, free API key)
      export ADZUNA_APP_ID=...   ADZUNA_APP_KEY=...   (from developer.adzuna.com)
    If you publish or share Adzuna-sourced listings, the Adzuna terms require
    "Jobs by Adzuna" attribution.

Outputs (in the current folder):
  jobs.db                    SQLite database, one row per job (the source of truth)
  jobs_greenhouse_lever.csv  jobs you can apply to with greenhouse_apply.py (Greenhouse)
                             or by hand (Lever)
  jobs_other.csv             aggregator jobs (apply by hand on the employer's site)
  jobs_all.csv               everything above the match threshold

Files you provide: resume.md, profile.json, sources.json (see profile keys below).

profile.json keys used here (all optional):
    target_titles     ["Full-Stack Engineer", ...]
    title_keywords    ["full stack", "frontend", "software engineer", ...]
    title_exclude     ["staff", "principal", "manager", "intern", ...]
    skills            ["React", "Angular", "Django", ...]  (default: parsed from the
                      "## Skills" section of resume.md)
    search_queries    ["full stack developer", ...]  Adzuna queries (default: target_titles)
    max_age_days      30
    adzuna_pages      3          pages of 50 results per query
    min_match_score   25         only jobs at/above this go to the CSV lists

Run:
    python job_search.py
    python job_search.py --min-match 40 --no-adzuna
"""
import argparse
import csv
import html
import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

PROFILE = json.loads(Path("profile.json").read_text())
SOURCES = json.loads(Path("sources.json").read_text()) if Path("sources.json").exists() else {}
RESUME = Path("resume.md").read_text()
DB = Path("jobs.db")

SKILLS_FOR_FULL_SCORE = 8  # matching this many of your skills = match_score 100 (rough heuristic)


# ---------- Helpers ----------

def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(text or ""))).strip()


def parse_dt(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ---------- Sources ----------

def fetch_greenhouse(board: str) -> list[dict]:
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true", timeout=30)
    r.raise_for_status()
    return [
        {
            "source": "greenhouse", "ats": "greenhouse", "company": board,
            "title": j["title"], "location": (j.get("location") or {}).get("name", ""),
            "url": j["absolute_url"], "description": strip_html(j.get("content", "")),
            "posted": parse_dt(j.get("updated_at")), "workplace": "",
        }
        for j in r.json().get("jobs", [])
    ]


def fetch_lever(company: str) -> list[dict]:
    r = requests.get(f"https://api.lever.co/v0/postings/{company}?mode=json", timeout=30)
    r.raise_for_status()
    out = []
    for j in r.json():
        created = j.get("createdAt")
        out.append(
            {
                "source": "lever", "ats": "lever", "company": company,
                "title": j["text"], "location": (j.get("categories") or {}).get("location", "") or "",
                "url": j["hostedUrl"], "description": j.get("descriptionPlain", ""),
                "posted": datetime.fromtimestamp(created / 1000, tz=timezone.utc) if created else None,
                "workplace": (j.get("workplaceType") or "").lower(),
            }
        )
    return out


def fetch_boards() -> list[dict]:
    tasks = [("greenhouse", n) for n in SOURCES.get("greenhouse", [])]
    tasks += [("lever", n) for n in SOURCES.get("lever", [])]

    def run(task):
        kind, name = task
        try:
            return fetch_greenhouse(name) if kind == "greenhouse" else fetch_lever(name)
        except Exception:
            print(f"[warn] {kind}/{name}: could not fetch (bad name or no board)")
            return []

    with ThreadPoolExecutor(8) as ex:
        return [job for batch in ex.map(run, tasks) for job in batch]


def fetch_adzuna(max_age_days: int) -> list[dict]:
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        print("[info] Adzuna keys not set; skipping aggregator source")
        return []
    queries = PROFILE.get("search_queries") or PROFILE.get("target_titles", [])
    pages = PROFILE.get("adzuna_pages", 3)
    out = []
    for q in queries:
        for page in range(1, pages + 1):
            try:
                r = requests.get(
                    f"https://api.adzuna.com/v1/api/jobs/us/search/{page}",
                    params={
                        "app_id": app_id, "app_key": app_key, "what": q,
                        "results_per_page": 50, "max_days_old": max_age_days, "sort_by": "date",
                    },
                    timeout=30,
                )
                r.raise_for_status()
                results = r.json().get("results", [])
            except Exception as e:
                print(f"[warn] adzuna '{q}' page {page}: {e}")
                break
            for j in results:
                out.append(
                    {
                        "source": "adzuna", "ats": "other",
                        "company": (j.get("company") or {}).get("display_name", ""),
                        "title": j.get("title", ""),
                        "location": (j.get("location") or {}).get("display_name", ""),
                        "url": j.get("redirect_url", ""),
                        "description": strip_html(j.get("description", "")),  # Adzuna gives a short snippet
                        "posted": parse_dt(j.get("created")), "workplace": "",
                    }
                )
            if len(results) < 50:
                break
    return out


# ---------- Filters (free, local) ----------

STATE_ABBR = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
    "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
STATE_NAMES = (
    "alabama alaska arizona arkansas california colorado connecticut delaware florida georgia hawaii idaho "
    "illinois indiana iowa kansas kentucky louisiana maine maryland massachusetts michigan minnesota "
    "mississippi missouri montana nebraska nevada new.hampshire new.jersey new.mexico new.york "
    "north.carolina north.dakota ohio oklahoma oregon pennsylvania rhode.island south.carolina "
    "south.dakota tennessee texas utah vermont virginia washington west.virginia wisconsin wyoming"
).split()
US_RE = re.compile(r"\b(us|usa|u\.s\.a?\.?|united states|north america|americas)\b", re.I)
STATE_ABBR_RE = re.compile(r",\s*(?:" + "|".join(STATE_ABBR) + r")\b")
STATE_NAME_RE = re.compile(r"\b(?:" + "|".join(STATE_NAMES) + r")\b", re.I)
NON_US_RE = re.compile(
    r"\b(emea|europe|eu|uk|united kingdom|canada|india|latam|latin america|apac|asia|australia|germany|"
    r"france|spain|brazil|mexico|ireland|netherlands|poland|portugal|israel|singapore|japan|london|berlin|"
    r"toronto|vancouver|montreal|dublin|paris|amsterdam|bangalore|bengaluru|hyderabad|mumbai|pune|sydney|"
    r"melbourne|tel aviv|warsaw|lisbon|madrid|barcelona|sao paulo|buenos aires|tokyo|seoul|dubai)\b",
    re.I,
)


def us_match(location: str):
    """Returns 'explicit', 'ambiguous', or None (explicitly outside the US)."""
    loc = location or ""
    if US_RE.search(loc) or STATE_ABBR_RE.search(loc) or STATE_NAME_RE.search(loc):
        return "explicit"
    if NON_US_RE.search(loc):
        return None
    return "ambiguous"  # plain "Remote", "Anywhere", empty, or an unrecognized city


def work_mode(job: dict) -> str:
    text = f"{job['location']} {job['title']}".lower()
    if "remote" in text or job.get("workplace") == "remote":
        return "remote"
    if "hybrid" in text or job.get("workplace") == "hybrid":
        return "hybrid"
    return "onsite/unspecified"


def has_word(text: str, word: str) -> bool:
    return re.search(r"\b" + re.escape(word.lower()) + r"\b", text) is not None


def title_ok(job: dict) -> bool:
    t = job["title"].lower()
    keywords = [k.lower() for k in PROFILE.get("title_keywords", [])] or [
        k.lower().split()[0] for k in PROFILE.get("target_titles", [])
    ]
    if keywords and not any(k in t for k in keywords):
        return False
    return not any(has_word(t, e) for e in PROFILE.get("title_exclude", []))


def load_skills() -> list[str]:
    if PROFILE.get("skills"):
        return PROFILE["skills"]
    m = re.search(r"^## Skills\s*\n(.*?)(?=^## |\Z)", RESUME, re.S | re.M)
    terms = []
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                items = line.split(":", 1)[1].replace("**", "")
                terms += [t.strip() for t in re.split(r",|/", items) if 1 < len(t.strip()) <= 30]
    seen, out = set(), []
    for t in terms:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def match_skills(text: str, skills: list[str]) -> list[str]:
    t = text.lower()
    return [
        s for s in skills
        if re.search(r"(?<![a-z0-9])" + re.escape(s.lower()) + r"(?![a-z0-9])", t)
    ]


# ---------- Pipeline ----------

def process(jobs: list[dict], max_age_days: int, skills: list[str]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    rows, seen = [], set()
    for j in jobs:
        if not j["url"] or j["url"] in seen:
            continue
        seen.add(j["url"])
        if j["posted"] and j["posted"] < cutoff:
            continue
        us = us_match(j["location"])
        if us is None or not title_ok(j):
            continue
        hits = match_skills(f"{j['title']} {j['description']}", skills)
        rows.append(
            {
                "url": j["url"], "source": j["source"], "ats": j["ats"], "company": j["company"],
                "title": j["title"], "location": j["location"], "work_mode": work_mode(j),
                "us_match": us, "posted": j["posted"].date().isoformat() if j["posted"] else "",
                "skills_matched": ", ".join(hits),
                "match_score": min(100, round(100 * len(hits) / SKILLS_FOR_FULL_SCORE)),
            }
        )
    return rows


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
            url TEXT PRIMARY KEY, source TEXT, ats TEXT, company TEXT, title TEXT,
            location TEXT, work_mode TEXT, us_match TEXT, posted TEXT,
            skills_matched TEXT, match_score INTEGER,
            first_seen TEXT, last_seen TEXT, status TEXT DEFAULT 'new')"""
    )


def save(conn: sqlite3.Connection, rows: list[dict]) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    for r in rows:
        conn.execute(
            """INSERT INTO jobs (url, source, ats, company, title, location, work_mode, us_match,
                                 posted, skills_matched, match_score, first_seen, last_seen)
               VALUES (:url, :source, :ats, :company, :title, :location, :work_mode, :us_match,
                       :posted, :skills_matched, :match_score, :now, :now)
               ON CONFLICT(url) DO UPDATE SET
                 last_seen = :now, match_score = :match_score, skills_matched = :skills_matched,
                 title = :title, location = :location, work_mode = :work_mode, posted = :posted""",
            {**r, "now": now},
        )
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] - before


CSV_COLS = ["match_score", "company", "title", "location", "work_mode", "us_match", "ats",
            "posted", "skills_matched", "url", "status", "first_seen"]


def export(conn: sqlite3.Connection, min_match: int) -> dict[str, int]:
    conn.row_factory = sqlite3.Row
    files = {
        "jobs_all.csv": "match_score >= ?",
        "jobs_greenhouse_lever.csv": "match_score >= ? AND ats IN ('greenhouse', 'lever')",
        "jobs_other.csv": "match_score >= ? AND ats = 'other'",
    }
    counts = {}
    for name, where in files.items():
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE {where} ORDER BY match_score DESC, posted DESC", (min_match,)
        ).fetchall()
        with open(name, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(CSV_COLS)
            w.writerows([[r[c] for c in CSV_COLS] for r in rows])
        counts[name] = len(rows)
    return counts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-match", type=int, default=PROFILE.get("min_match_score", 25))
    ap.add_argument("--no-adzuna", action="store_true")
    args = ap.parse_args()

    max_age = PROFILE.get("max_age_days", 30)
    skills = load_skills()
    print(f"Matching against {len(skills)} skills from your profile/resume")

    jobs = fetch_boards()
    print(f"Fetched {len(jobs)} postings from Greenhouse/Lever boards")
    if not args.no_adzuna:
        extra = fetch_adzuna(max_age)
        print(f"Fetched {len(extra)} postings from Adzuna")
        jobs += extra

    rows = process(jobs, max_age, skills)
    print(f"{len(rows)} US jobs passed the location, age, and title filters")

    conn = sqlite3.connect(DB)
    init_db(conn)
    new = save(conn, rows)
    counts = export(conn, args.min_match)
    conn.close()

    print(f"{new} new jobs added to jobs.db")
    for name, n in counts.items():
        print(f"  {name}: {n} jobs (match score >= {args.min_match})")


if __name__ == "__main__":
    main()