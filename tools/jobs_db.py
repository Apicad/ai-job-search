#!/usr/bin/env python3
"""Deterministic CLI over job_scraper/seen_jobs.json and its posting store.

/scrape and /rank used to read the whole JSON into the model context and write
it back as model output. This tool owns those reads and writes instead: every
subcommand prints one compact JSON value on stdout, errors go to stderr, and
the on-disk shape stays exactly what .claude/skills/job-scraper/SKILL.md
documents (`{"seen": {<url_or_company_title_key>: {...}}}`). Unknown fields on
an entry are always preserved; nothing is ever dropped.

Layout next to the database file (default <repo>/job_scraper/seen_jobs.json):
  postings/<sha1(key)[:16]>.md   full posting text, one file per entry
  raw/                           convention for redirected portal-CLI output;
                                 this tool never creates it

Examples:
  # Ingest one portal's search output (saved by the caller under raw/)
  python3 tools/jobs_db.py upsert --from job_scraper/raw/linkedin.json \\
      --portal linkedin-search --source cli

  # List unranked jobs minus everything already in the tracker
  python3 tools/jobs_db.py list --status new --exclude-tracker job_search_tracker.csv

  # Persist a /rank Step 2 agent result and retire past-deadline entries
  python3 tools/jobs_db.py apply-ranking --from scores.json --date 2026-11-03
  python3 tools/jobs_db.py expire-sweep --today 2026-11-03 --within 7

Exit codes: 0 success, 1 check failed / data error, 2 usage error.
"""

import argparse
import csv
import datetime as _dt
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path

DEFAULT_DB_RELATIVE = Path("job_scraper") / "seen_jobs.json"
LEGACY_DB_RELATIVE = Path(".claude") / "skills" / "job-scraper" / "job_scraper" / "seen_jobs.json"
POSTINGS_DIRNAME = "postings"

# rank.md Step 3 / 04-job-evaluation.md "Weighting" (location is unweighted).
SCORE_WEIGHTS = {
    "technical": 0.30,
    "experience": 0.25,
    "behavioral": 0.15,
    "career": 0.30,
}
# rank.md Step 3 / 04-job-evaluation.md "Thresholds": (lower bound, band).
VERDICT_BANDS = (
    (75, "Strong Fit"),
    (60, "Good Fit"),
    (45, "Moderate Fit"),
    (30, "Weak Fit"),
    (0, "Poor Fit"),
)
VERDICT_VALUES = ("PASS", "FAIL", "FLAG")
FIT_VALUES = ("high", "medium", "low")
STATUS_VALUES = ("new", "skipped", "ranked", "expired")
# set-fit may only toggle the scraper-owned statuses; ranked/expired belong to
# apply-ranking and expire-sweep (a "ranked" entry without a score is nonsense).
SET_FIT_STATUSES = ("new", "skipped")
SWEEPABLE_STATUSES = ("new", "ranked")
PROTECTED_STATUSES = ("new", "ranked")
DEFAULT_LIST_FIELDS = (
    "key,title,company,location,url,status,deadline,posting_fetched,rank_score"
)
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SNIPPET_CHARS = 300


class JobsDbError(Exception):
    """Raised for data errors (exit 1): unknown keys, unreadable input, bad values."""


class UsageError(Exception):
    """Raised for argument combinations argparse cannot express (exit 2)."""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def collapse_ws(text):
    return " ".join(str(text).split())


def norm_text(text):
    """Case-insensitive, whitespace-collapsed form used for keys and matching."""
    return collapse_ws(text).lower()


def make_key(item):
    """URL when the item has one, else "company|title" lowercased and collapsed.

    Returns None for an item that carries neither, which cannot be deduplicated.
    """
    url = item.get("url")
    if isinstance(url, str) and url.strip():
        return url.strip()
    company = item.get("company")
    title = item.get("title")
    if not (isinstance(company, str) and company.strip()) or not (
        isinstance(title, str) and title.strip()
    ):
        return None
    return f"{norm_text(company)}|{norm_text(title)}"


def company_title_key(entry):
    company = entry.get("company")
    title = entry.get("title")
    if not isinstance(company, str) or not isinstance(title, str):
        return None
    if not company.strip() or not title.strip():
        return None
    return f"{norm_text(company)}|{norm_text(title)}"


def parse_iso_date(value):
    """Return a date for a strict YYYY-MM-DD string, else None (never guess)."""
    if not isinstance(value, str) or not ISO_DATE_RE.match(value):
        return None
    try:
        return _dt.date.fromisoformat(value)
    except ValueError:
        return None


def require_iso_date(value, flag):
    parsed = parse_iso_date(value)
    if parsed is None:
        raise JobsDbError(f"{flag} must be a YYYY-MM-DD date, got {value!r}")
    return parsed


def today_string(override=None):
    if override:
        return require_iso_date(override, "--today").isoformat()
    return _dt.date.today().isoformat()


def posting_filename(key):
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".md"


def read_json_input(path):
    """Read a JSON file ('-' for stdin) and raise JobsDbError on any failure."""
    try:
        if str(path) == "-":
            return json.load(sys.stdin)
        # utf-8-sig: a PowerShell `>` redirect of portal-CLI output starts
        # with a BOM, which plain utf-8 json.load rejects.
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise JobsDbError(f"input file does not exist: {path}")
    except (OSError, UnicodeDecodeError) as exc:
        raise JobsDbError(f"could not read {path}: {exc}")
    except json.JSONDecodeError as exc:
        raise JobsDbError(f"{path} is not valid JSON: {exc}")


def read_text_input(path):
    try:
        if str(path) == "-":
            return sys.stdin.read()
        return Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except FileNotFoundError:
        raise JobsDbError(f"input file does not exist: {path}")
    except OSError as exc:
        raise JobsDbError(f"could not read {path}: {exc}")


def emit(payload):
    text = json.dumps(payload, ensure_ascii=False)
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        # A redirected stdout on Windows may be cp1252/ascii; \uXXXX escapes
        # keep the output valid JSON for every consumer.
        sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")


def warn(message):
    try:
        print(message, file=sys.stderr)
    except UnicodeEncodeError:
        print(message.encode("ascii", "backslashreplace").decode("ascii"), file=sys.stderr)


# --------------------------------------------------------------------------
# Database file handling
# --------------------------------------------------------------------------


def resolve_db_path(repo, db_flag=None):
    """Apply the path precedence: --db, default location, legacy skill-relative path."""
    if db_flag:
        return Path(db_flag)
    default = Path(repo) / DEFAULT_DB_RELATIVE
    if default.exists():
        return default
    legacy = Path(repo) / LEGACY_DB_RELATIVE
    if legacy.exists():
        print(
            f"note: using legacy seen_jobs.json at {legacy} (not moved; "
            f"{default} does not exist)",
            file=sys.stderr,
        )
        return legacy
    return default


class JobsDb:
    """In-memory view of seen_jobs.json plus the posting store next to it."""

    def __init__(self, path):
        self.path = Path(path)
        self.dir = self.path.parent
        self.postings_dir = self.dir / POSTINGS_DIRNAME
        self.indent = 2
        self.ensure_ascii = False
        self.wrapped = True  # {"seen": {...}} per SKILL.md Step 0
        self.extra_top_level = {}
        self.entries = {}
        self._load()

    # -- loading / saving ---------------------------------------------------

    def _load(self):
        if not self.path.exists():
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise JobsDbError(f"could not read {self.path}: {exc}")
        if not raw.strip():
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise JobsDbError(f"{self.path} is not valid JSON: {exc}")
        if not isinstance(data, dict):
            raise JobsDbError(f"{self.path} must hold a JSON object")
        self._detect_style(raw)
        if isinstance(data.get("seen"), dict):
            self.wrapped = True
            self.entries = data["seen"]
            self.extra_top_level = {k: v for k, v in data.items() if k != "seen"}
        else:
            # Flat legacy shape: top-level keys are entries. Keep it that way.
            self.wrapped = False
            self.entries = data
        for key, entry in self.entries.items():
            if not isinstance(entry, dict):
                raise JobsDbError(f"entry {key!r} in {self.path} is not an object")

    def _detect_style(self, raw):
        lines = raw.splitlines()
        if len(lines) > 1:
            leading = len(lines[1]) - len(lines[1].lstrip(" "))
            if leading > 0:
                self.indent = leading
        has_non_ascii = any(ord(ch) > 127 for ch in raw)
        if not has_non_ascii and "\\u00" in raw:
            self.ensure_ascii = True

    def save(self):
        if self.wrapped:
            data = dict(self.extra_top_level)
            data["seen"] = self.entries
        else:
            data = self.entries
        text = json.dumps(data, indent=self.indent, ensure_ascii=self.ensure_ascii) + "\n"
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=self.path.name + ".", suffix=".tmp", dir=str(self.dir)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
                os.replace(tmp_name, self.path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError as exc:
            raise JobsDbError(f"could not write {self.path}: {exc}")

    # -- lookups --------------------------------------------------------------

    def require(self, key):
        entry = self.entries.get(key)
        if entry is None:
            raise JobsDbError(f"unknown key: {key!r}")
        return entry

    def company_title_index(self):
        index = {}
        for key, entry in self.entries.items():
            ct_key = company_title_key(entry)
            if ct_key is not None and ct_key not in index:
                index[ct_key] = key
        return index

    # -- posting store --------------------------------------------------------

    def posting_path(self, key):
        return self.postings_dir / posting_filename(key)

    def posting_relative(self, key):
        return f"{POSTINGS_DIRNAME}/{posting_filename(key)}"

    def _stored_posting_path(self, key):
        """Path named by entry.posting_file (or the default), None when it escapes postings/.

        seen_jobs.json was model-written before this tool existed and posting
        text is untrusted, so a `posting_file` of "../x" must never be read or
        unlinked.
        """
        entry = self.entries.get(key, {})
        rel = entry.get("posting_file")
        path = self.dir / rel if isinstance(rel, str) and rel else self.posting_path(key)
        try:
            resolved = path.resolve()
            root = self.postings_dir.resolve()
        except OSError:
            return None
        if resolved != root and root not in resolved.parents:
            warn(f"warning: {key!r}: posting_file {rel!r} is outside {POSTINGS_DIRNAME}/, ignored")
            return None
        return path

    def has_posting(self, key):
        path = self._stored_posting_path(key)
        return path is not None and path.is_file()

    def write_posting(self, key, text, fetched, source):
        entry = self.require(key)
        if not text.strip():
            raise JobsDbError(f"{key!r}: posting text is empty, nothing stored")
        body = text if text.endswith("\n") else text + "\n"
        chars = len(text)
        header = "\n".join(
            [
                "---",
                f"key: {key}",
                f"url: {entry.get('url') or ''}",
                f"fetched: {fetched}",
                f"source: {source}",
                f"chars: {chars}",
                "---",
                "",
            ]
        )
        path = self.posting_path(key)
        try:
            self.postings_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(self.postings_dir))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(header + "\n" + body)
                os.replace(tmp_name, path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError as exc:
            raise JobsDbError(f"could not write posting for {key!r}: {exc}")
        entry["posting_file"] = self.posting_relative(key)
        entry["posting_fetched"] = fetched
        return chars

    def read_posting(self, key):
        """Return (header dict, body text) or None when no posting is stored."""
        self.require(key)
        path = self._stored_posting_path(key)
        if path is None or not path.is_file():
            return None
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise JobsDbError(f"could not read posting for {key!r}: {exc}")
        header = {}
        body = raw
        if raw.startswith("---\n"):
            end = raw.find("\n---\n", 4)
            if end != -1:
                for line in raw[4:end].splitlines():
                    name, sep, value = line.partition(":")
                    if sep:
                        header[name.strip()] = value.strip()
                body = raw[end + len("\n---\n"):]
                if body.startswith("\n"):
                    body = body[1:]
        return header, body

    def remove_posting(self, key):
        path = self._stored_posting_path(key)
        if path is not None and path.is_file():
            try:
                path.unlink()
            except OSError as exc:
                raise JobsDbError(f"could not remove posting for {key!r}: {exc}")
            return True
        return False


# --------------------------------------------------------------------------
# Tracker (read-only)
# --------------------------------------------------------------------------


def load_tracker_pairs(tracker_path):
    """Set of (company, role) pairs from job_search_tracker.csv, normalized.

    Any status counts (rank.md Step 1). A missing tracker excludes nothing.
    """
    path = Path(tracker_path)
    if not path.is_file():
        return set()
    pairs = set()
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                company = (row.get("company") or "").strip()
                role = (row.get("role") or "").strip()
                if company and role:
                    pairs.add((norm_text(company), norm_text(role)))
    except (OSError, csv.Error) as exc:
        raise JobsDbError(f"could not read tracker {path}: {exc}")
    return pairs


def entry_in_tracker(entry, pairs):
    if not pairs:
        return False
    company = entry.get("company")
    title = entry.get("title")
    if not isinstance(company, str) or not isinstance(title, str):
        return False
    return (norm_text(company), norm_text(title)) in pairs


# --------------------------------------------------------------------------
# Scoring (rank.md Step 3)
# --------------------------------------------------------------------------


def overall_score(scores):
    if not isinstance(scores, dict):
        raise JobsDbError("scores must be an object with technical/experience/behavioral/career")
    total = 0.0
    for dimension, weight in SCORE_WEIGHTS.items():
        value = scores.get(dimension)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise JobsDbError(f"scores.{dimension} must be a number 0-100")
        if value < 0 or value > 100:
            raise JobsDbError(f"scores.{dimension} must be within 0-100, got {value}")
        total += weight * float(value)
    return int(math.floor(total + 0.5))


def verdict_for(score):
    for lower, band in VERDICT_BANDS:
        if score >= lower:
            return band
    return VERDICT_BANDS[-1][1]


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------


def _open_db(args):
    repo = Path(args.repo) if args.repo else Path(__file__).resolve().parent.parent
    return JobsDb(resolve_db_path(repo, args.db))


def _iter_results(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return payload["results"]
    raise JobsDbError("input must be a JSON array or an object with a \"results\" array")


def cmd_upsert(args):
    db = _open_db(args)
    today = today_string(args.today)
    results = _iter_results(read_json_input(args.from_path))
    ct_index = db.company_title_index()
    since = None
    if getattr(args, "since", None):
        try:
            since = _dt.date.fromisoformat(args.since)
        except ValueError:
            raise JobsDbError(f"--since must be YYYY-MM-DD, got {args.since!r}")
    added = []
    existing = 0
    skipped = 0
    stored = 0
    dropped_before_since = 0
    for item in results:
        if not isinstance(item, dict):
            skipped += 1
            continue
        key = make_key(item)
        if key is None:
            skipped += 1
            warn("warning: skipped a result with no url and no company+title")
            continue
        if since is not None:
            raw_date = item.get("date")
            item_date = None
            if isinstance(raw_date, str):
                try:
                    item_date = _dt.date.fromisoformat(raw_date.strip()[:10])
                except ValueError:
                    item_date = None
            if item_date is not None and item_date < since:
                dropped_before_since += 1
                continue
        description = item.get("description")
        has_description = isinstance(description, str) and description.strip() != ""
        target_key = None
        if key in db.entries:
            target_key = key
        else:
            ct_key = company_title_key(item)
            if ct_key is not None and ct_key in ct_index:
                target_key = ct_index[ct_key]
        if target_key is not None:
            existing += 1
            if has_description and not db.has_posting(target_key):
                db.write_posting(target_key, description, today, "cli")
                stored += 1
            continue
        entry = {
            "title": item.get("title"),
            "company": item.get("company"),
            "location": item.get("location"),
            "url": item["url"].strip() if isinstance(item.get("url"), str) else None,
        }
        if item.get("date") is not None:
            entry["date"] = item.get("date")
        entry["deadline"] = item.get("deadline") if item.get("deadline") is not None else None
        entry["fit"] = None
        entry["status"] = "new"
        entry["first_seen"] = today
        entry["portal"] = args.portal
        entry["source"] = args.source
        db.entries[key] = entry
        ct_key = company_title_key(entry)
        if ct_key is not None and ct_key not in ct_index:
            ct_index[ct_key] = key
        if has_description:
            db.write_posting(key, description, today, "cli")
            stored += 1
        added.append(
            {
                "key": key,
                "title": entry["title"],
                "company": entry["company"],
                "location": entry["location"],
                "url": entry["url"],
                "deadline": entry["deadline"],
            }
        )
    if added or stored:
        db.save()
    emit(
        {
            "added": added,
            "existing": existing,
            "stored_postings": stored,
            "skipped": skipped,
            "dropped_before_since": dropped_before_since,
        }
    )
    return 0


def cmd_store_posting(args):
    db = _open_db(args)
    today = today_string(args.today)
    text = read_text_input(args.from_path)
    chars = db.write_posting(args.key, text, today, args.source)
    db.save()
    emit(
        {
            "key": args.key,
            "posting_file": db.entries[args.key]["posting_file"],
            "chars": chars,
            "snippet": collapse_ws(text)[:SNIPPET_CHARS],
        }
    )
    return 0


def cmd_get_posting(args):
    db = _open_db(args)
    entry = db.require(args.key)
    stored = db.read_posting(args.key)
    if stored is None:
        emit({"key": args.key, "missing": True})
        return 0
    header, body = stored
    fetched = entry.get("posting_fetched") or header.get("fetched") or None
    if args.max_age_days is not None:
        today = require_iso_date(today_string(args.today), "--today")
        fetched_date = parse_iso_date(fetched)
        if fetched_date is None or (today - fetched_date).days > args.max_age_days:
            emit({"key": args.key, "stale": True, "fetched": fetched})
            return 0
    emit({"key": args.key, "url": entry.get("url"), "fetched": fetched, "text": body})
    return 0


def _split_csv_flag(value):
    return [part.strip() for part in value.split(",") if part.strip()]


def cmd_list(args):
    db = _open_db(args)
    statuses = set(_split_csv_flag(args.status)) if args.status else None
    pairs = load_tracker_pairs(args.exclude_tracker) if args.exclude_tracker else set()
    focus = norm_text(args.focus) if args.focus else None
    fields = _split_csv_flag(args.fields or DEFAULT_LIST_FIELDS)
    rows = []
    for key, entry in db.entries.items():
        if statuses is not None and entry.get("status") not in statuses:
            continue
        if entry_in_tracker(entry, pairs):
            continue
        if focus is not None:
            haystack = norm_text(
                " ".join(
                    str(entry.get(field) or "") for field in ("title", "company", "location")
                )
            )
            if focus not in haystack:
                continue
        if args.min_score is not None:
            score = entry.get("rank_score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or score < args.min_score:
                continue
        row = {}
        for field in fields:
            row[field] = key if field == "key" else entry.get(field)
        rows.append(row)
    emit(rows)
    return 0


def cmd_set_fit(args):
    db = _open_db(args)
    payload = read_json_input(args.from_path)
    if not isinstance(payload, list):
        raise JobsDbError("set-fit input must be a JSON array of {key, fit, status?}")
    updated = 0
    unknown = []
    invalid = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            invalid.append(item)
            continue
        key = item["key"]
        entry = db.entries.get(key)
        if entry is None:
            unknown.append(key)
            continue
        fit = item.get("fit")
        status = item.get("status")
        if fit not in FIT_VALUES or (status is not None and status not in SET_FIT_STATUSES):
            invalid.append(key)
            continue
        entry["fit"] = fit
        if status is not None:
            entry["status"] = status
        updated += 1
    if updated:
        db.save()
    emit({"updated": updated, "unknown": unknown, "invalid": invalid})
    return 1 if (unknown or invalid) else 0


def _verdict_or_none(value, field, key):
    if value is None:
        return None
    if value not in VERDICT_VALUES:
        raise JobsDbError(f"{key!r}: {field} must be one of {', '.join(VERDICT_VALUES)}")
    return value


def cmd_apply_ranking(args):
    db = _open_db(args)
    rank_date = require_iso_date(args.date, "--date").isoformat()
    payload = read_json_input(args.from_path)
    if not isinstance(payload, list):
        raise JobsDbError("apply-ranking input must be the Step 2 JSON array")
    output = []
    failures = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            failures.append("result without a string key")
            continue
        key = item["key"]
        entry = db.entries.get(key)
        if entry is None:
            failures.append(f"unknown key: {key!r}")
            continue
        try:
            output.append(_apply_one_ranking(entry, key, item, rank_date))
        except JobsDbError as exc:
            failures.append(str(exc))
    if output:
        db.save()
    for failure in failures:
        warn(f"error: {failure}")
    emit(output)
    return 1 if failures else 0


def _apply_one_ranking(entry, key, item, rank_date):
    # Legacy migration (rank.md Step 4): a PASS/FAIL/FLAG string stored under
    # `location` is a verdict, not a place; move it and clear the place field.
    legacy_verdict = None
    if entry.get("location") in VERDICT_VALUES:
        legacy_verdict = entry["location"]
        if entry.get("location_verdict") is None:
            entry["location_verdict"] = legacy_verdict
        entry["location"] = None

    agent_status = item.get("status")
    if agent_status == "expired":
        entry["status"] = "expired"
        return _ranking_row(key, entry)
    if agent_status not in (None, "scored"):
        raise JobsDbError(f"{key!r}: status must be \"scored\" or \"expired\"")

    score = overall_score(item.get("scores"))
    location_verdict = _verdict_or_none(item.get("location_verdict"), "location_verdict", key)
    language_gate = _verdict_or_none(item.get("language_gate"), "language_gate", key)

    entry["status"] = "ranked"
    entry["rank_score"] = score
    entry["rank_verdict"] = verdict_for(score)
    entry["rank_date"] = rank_date
    if location_verdict is not None:
        entry["location_verdict"] = location_verdict
    elif "location_verdict" not in entry:
        entry["location_verdict"] = None
    if language_gate is not None:
        entry["language_gate"] = language_gate
    elif "language_gate" not in entry:
        entry["language_gate"] = None
    note = item.get("language_note")
    if entry.get("language_gate") == "PASS" or not note:
        entry["language_note"] = None
    else:
        entry["language_note"] = str(note)
    deadline = item.get("deadline")
    if deadline is not None:
        # Step 2 schema: "YYYY-MM-DD" | null. Anything else is not a date the
        # sweep can compare, so it is no correction either; the stored value
        # stays and the bad value is traced here rather than to the portal.
        if parse_iso_date(deadline) is None:
            warn(f"warning: {key!r}: agent deadline {deadline!r} is not YYYY-MM-DD, stored value kept")
        else:
            entry["deadline"] = deadline
    elif "deadline" not in entry:
        entry["deadline"] = None
    for field in ("strengths", "gaps"):
        bullets = item.get(field)
        if bullets is None:
            continue
        if not isinstance(bullets, list):
            raise JobsDbError(f"{key!r}: {field} must be an array of strings")
        entry[field] = [str(bullet) for bullet in bullets]
    return _ranking_row(key, entry)


def _ranking_row(key, entry):
    return {
        "key": key,
        "rank_score": entry.get("rank_score"),
        "rank_verdict": entry.get("rank_verdict"),
        "status": entry.get("status"),
        "location_verdict": entry.get("location_verdict"),
        "language_gate": entry.get("language_gate"),
        "deadline": entry.get("deadline"),
    }


def cmd_expire_sweep(args):
    db = _open_db(args)
    today = require_iso_date(args.today, "--today")
    expired = []
    closing_soon = []
    unparseable = []
    swept = 0
    no_deadline = 0
    for key, entry in db.entries.items():
        if entry.get("status") not in SWEEPABLE_STATUSES:
            continue
        swept += 1
        deadline = entry.get("deadline")
        if deadline is None:
            no_deadline += 1
            continue
        parsed = parse_iso_date(deadline)
        if parsed is None:
            unparseable.append({"key": key, "deadline": deadline, "portal": entry.get("portal")})
            continue
        if parsed < today:
            entry["status"] = "expired"
            expired.append(key)
        elif (parsed - today).days <= args.within:
            closing_soon.append(
                {
                    "key": key,
                    "title": entry.get("title"),
                    "company": entry.get("company"),
                    "deadline": deadline,
                }
            )
    if expired:
        db.save()
    emit(
        {
            "expired": expired,
            "closing_soon": closing_soon,
            "unparseable": unparseable,
            "counts": {
                "swept": swept,
                "expired": len(expired),
                "closing_soon": len(closing_soon),
                "unparseable": len(unparseable),
                "no_deadline": no_deadline,
            },
        }
    )
    return 0


def _last_activity(entry):
    first_seen = parse_iso_date(entry.get("first_seen"))
    rank_date = parse_iso_date(entry.get("rank_date"))
    if first_seen is None:
        return rank_date
    if rank_date is not None and rank_date > first_seen:
        return rank_date
    return first_seen


def cmd_prune(args):
    statuses = _split_csv_flag(args.status)
    protected = [status for status in statuses if status in PROTECTED_STATUSES]
    if protected:
        raise UsageError(f"prune never touches {', '.join(protected)} entries")
    db = _open_db(args)
    today = require_iso_date(args.today, "--today")
    candidates = []
    for key, entry in db.entries.items():
        if entry.get("status") not in statuses:
            continue
        last = _last_activity(entry)
        if last is None:
            continue  # no parseable date: never guess
        if (today - last).days > args.older_than:
            candidates.append(
                {
                    "key": key,
                    "title": entry.get("title"),
                    "company": entry.get("company"),
                    "status": entry.get("status"),
                    "last_activity": last.isoformat(),
                }
            )
    if not args.apply:
        emit({"candidates": candidates, "applied": False})
        return 0
    removed_files = 0
    for row in candidates:
        if db.remove_posting(row["key"]):
            removed_files += 1
        del db.entries[row["key"]]
    if candidates:
        db.save()
    emit(
        {
            "removed": [row["key"] for row in candidates],
            "posting_files_removed": removed_files,
            "applied": True,
        }
    )
    return 0


def cmd_get(args):
    db = _open_db(args)
    emit(db.require(args.key))
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="jobs_db.py",
        description="Read and update job_scraper/seen_jobs.json and its posting store.",
    )
    parser.add_argument("--db", help="path to seen_jobs.json (default: <repo>/job_scraper/seen_jobs.json)")
    parser.add_argument("--repo", help="repository root (default: parent of tools/)")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    def add_today(p):
        p.add_argument("--today", help="override today's date (YYYY-MM-DD), for tests")

    p = sub.add_parser("upsert", help="add new entries from a portal CLI JSON output")
    p.add_argument("--from", dest="from_path", required=True, help="portal CLI JSON ('-' for stdin)")
    p.add_argument("--portal", required=True, help="portal skill name, e.g. linkedin-search")
    p.add_argument("--source", required=True, choices=("cli", "websearch"))
    p.add_argument(
        "--since",
        help=(
            "drop results whose date (YYYY-MM-DD) is older than this - the "
            "client-side recency filter for portals with no recency flag; "
            "results with no parseable date are kept, never guessed at"
        ),
    )
    add_today(p)
    p.set_defaults(func=cmd_upsert)

    p = sub.add_parser("store-posting", help="store the full posting text for one entry")
    p.add_argument("key")
    p.add_argument("--from", dest="from_path", required=True, help="text file ('-' for stdin)")
    p.add_argument("--source", default="cli-detail", choices=("cli-detail", "webfetch"))
    add_today(p)
    p.set_defaults(func=cmd_store_posting)

    p = sub.add_parser("get-posting", help="print a stored posting, or stale/missing markers")
    p.add_argument("key")
    p.add_argument("--max-age-days", type=int, help="report stale when fetched more than N days ago")
    add_today(p)
    p.set_defaults(func=cmd_get_posting)

    p = sub.add_parser("list", help="list entries as a JSON array")
    p.add_argument("--status", help="comma-separated statuses to include (default: all)")
    p.add_argument("--exclude-tracker", help="job_search_tracker.csv; drops company+role matches")
    p.add_argument("--focus", help="case-insensitive substring over title, company and location")
    p.add_argument("--fields", help=f"comma-separated fields (default: {DEFAULT_LIST_FIELDS})")
    p.add_argument("--min-score", type=float, help="keep only entries with rank_score >= N")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("set-fit", help="set the quick-fit level (and optional status) per key")
    p.add_argument("--from", dest="from_path", required=True, help="JSON array of {key, fit, status?}")
    p.set_defaults(func=cmd_set_fit)

    p = sub.add_parser("apply-ranking", help="persist /rank Step 2 agent results")
    p.add_argument("--from", dest="from_path", required=True, help="Step 2 agent JSON array")
    p.add_argument("--date", required=True, help="rank_date (YYYY-MM-DD)")
    p.set_defaults(func=cmd_apply_ranking)

    p = sub.add_parser("expire-sweep", help="retire past-deadline entries (rank.md rule 6)")
    p.add_argument("--today", required=True, help="YYYY-MM-DD")
    p.add_argument("--within", type=int, default=7, help="closing-soon window in days (default: 7)")
    p.set_defaults(func=cmd_expire_sweep)

    p = sub.add_parser("prune", help="list (or with --apply remove) old expired/skipped entries")
    p.add_argument("--status", default="expired,skipped", help="statuses to prune (never new/ranked)")
    p.add_argument("--older-than", type=int, default=90, help="days since last activity (default: 90)")
    p.add_argument("--today", required=True, help="YYYY-MM-DD")
    p.add_argument("--apply", action="store_true", help="remove entries and their posting files")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("get", help="print one entry")
    p.add_argument("key")
    p.set_defaults(func=cmd_get)
    return parser


def main(argv=None):
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 0 for --help and 2 for usage errors; keep both.
        return int(exc.code) if isinstance(exc.code, int) else 2
    try:
        return args.func(args)
    except UsageError as exc:
        warn(f"usage error: {exc}")
        return 2
    except JobsDbError as exc:
        warn(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
