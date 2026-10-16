"""
job_agent.py - discover jobs from public ATS feeds, score them against your
profile with Claude, and prepare tailored materials. A human reviews and submits.

Setup:
    pip install anthropic requests
    export ANTHROPIC_API_KEY=sk-ant-...

Files you provide in the same folder:
    resume.md      your resume in plain text/markdown
    profile.json   your preferences, e.g.
        {
          "target_titles": ["Backend Engineer", "Platform Engineer"],
          "locations": ["Remote (US)", "Denver, CO"],
          "min_salary": 150000,
          "must_have": ["Python"],
          "deal_breakers": ["on-call 24/7", "unpaid trial"],
          "min_score": 70
        }
    sources.json   company boards to watch, e.g.
        {"greenhouse": ["airbnb", "stripe"], "lever": ["palantir"]}

Run:
    python job_agent.py
Output:
    tracker.csv and out/<company>-<id>/{cover_letter.md, resume_bullets.md}
"""
import csv
import html
import json
import re
from pathlib import Path

import anthropic
import requests

MODEL = "claude-sonnet-5-5"
client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment

RESUME = Path("resume.md").read_text()
PROFILE = json.loads(Path("profile.json").read_text())
SOURCES = json.loads(Path("sources.json").read_text())
TRACKER = Path("tracker.csv")
OUT_DIR = Path("out")
FIELDS = ["url", "company", "title", "location", "score", "reason", "status", "applied_at"]


# ---------- Discovery (public ATS endpoints, no LinkedIn scraping) ----------

def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(text or ""))).strip()


def fetch_greenhouse(board: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
    data = requests.get(url, timeout=30).json()
    return [
        {
            "id": str(j["id"]),
            "company": board,
            "title": j["title"],
            "location": (j.get("location") or {}).get("name", ""),
            "url": j["absolute_url"],
            "description": strip_html(j.get("content", "")),
        }
        for j in data.get("jobs", [])
    ]


def fetch_lever(company: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{company}?mode=json"
    data = requests.get(url, timeout=30).json()
    return [
        {
            "id": j["id"],
            "company": company,
            "title": j["text"],
            "location": (j.get("categories") or {}).get("location", ""),
            "url": j["hostedUrl"],
            "description": j.get("descriptionPlain", ""),
        }
        for j in data
    ]


def discover() -> list[dict]:
    jobs = []
    for board in SOURCES.get("greenhouse", []):
        try:
            jobs += fetch_greenhouse(board)
        except Exception as e:
            print(f"[warn] greenhouse/{board}: {e}")
    for company in SOURCES.get("lever", []):
        try:
            jobs += fetch_lever(company)
        except Exception as e:
            print(f"[warn] lever/{company}: {e}")
    return jobs


def title_prefilter(job: dict) -> bool:
    """Cheap filter before spending API calls."""
    t = job["title"].lower()
    return any(k.lower().split()[0] in t for k in PROFILE["target_titles"])


# ---------- Claude calls ----------

def ask_claude(system: str, user: str, max_tokens: int = 1500) -> str:
    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def score_job(job: dict) -> dict:
    system = (
        "You are a rigorous job-fit evaluator. Respond with ONLY a JSON object, "
        'no markdown: {"score": 0-100, "reason": "one or two sentences", '
        '"red_flags": ["..."]}. Penalize heavily for deal breakers and missing must-haves.'
    )
    user = (
        f"<profile>{json.dumps(PROFILE)}</profile>\n"
        f"<resume>{RESUME}</resume>\n"
        f"<job title='{job['title']}' company='{job['company']}' location='{job['location']}'>\n"
        f"{job['description'][:8000]}\n</job>"
    )
    raw = ask_claude(system, user, max_tokens=400)
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"score": 0, "reason": f"unparseable response: {raw[:100]}", "red_flags": []}


def prepare_materials(job: dict) -> tuple[str, str]:
    system = (
        "You tailor application materials. Use ONLY facts present in the resume. "
        "Never invent employers, titles, metrics, or skills. If something the job wants "
        "is missing from the resume, leave it out rather than stretch the truth."
    )
    context = (
        f"<resume>{RESUME}</resume>\n"
        f"<job title='{job['title']}' company='{job['company']}'>\n"
        f"{job['description'][:8000]}\n</job>"
    )
    bullets = ask_claude(
        system,
        context + "\nRewrite my 5-8 most relevant resume bullets to match this role's "
        "language and priorities. Output markdown bullets only.",
    )
    letter = ask_claude(
        system,
        context + "\nWrite a concise, specific cover letter (under 250 words). "
        "No clichés, no filler openers.",
    )
    return bullets, letter


# ---------- Tracking ----------

def load_tracker() -> dict[str, dict]:
    if not TRACKER.exists():
        return {}
    with TRACKER.open(newline="") as f:
        return {row["url"]: row for row in csv.DictReader(f)}


def save_tracker(rows: dict[str, dict]) -> None:
    with TRACKER.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows.values())


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ---------- Main ----------

def main() -> None:
    tracker = load_tracker()
    jobs = [j for j in discover() if title_prefilter(j) and j["url"] not in tracker]
    print(f"{len(jobs)} new candidate jobs after prefilter")

    for job in jobs:
        result = score_job(job)
        score = int(result.get("score", 0))
        status = "skipped_low_score"
        if score >= PROFILE.get("min_score", 70):
            bullets, letter = prepare_materials(job)
            folder = OUT_DIR / f"{slug(job['company'])}-{slug(job['id'])}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "resume_bullets.md").write_text(bullets)
            (folder / "cover_letter.md").write_text(letter)
            (folder / "job_url.txt").write_text(job["url"])
            status = "ready_for_review"
        tracker[job["url"]] = {
            "url": job["url"],
            "company": job["company"],
            "title": job["title"],
            "location": job["location"],
            "score": score,
            "reason": result.get("reason", ""),
            "status": status,
        }
        print(f"[{score:>3}] {job['company']}: {job['title']} -> {status}")
        save_tracker(tracker)  # save as we go so a crash loses nothing


if __name__ == "__main__":
    main()
