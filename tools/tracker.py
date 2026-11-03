#!/usr/bin/env python3
"""Deterministic CLI over job_search_tracker.csv.

Lets /apply, /quick-apply, /outcome and /gmail-sync read and edit single
tracker rows without re-emitting the whole CSV by hand. Rules mirror the
specs: the 14-column header from /apply Step 6b, the status vocabulary from
/outcome (`ready` and `drafted` are the two unsent statuses: nothing was
submitted, so no follow-up or staleness clock runs on them), the
header-line-only `,deadline` migration, "never move a row backwards", dated
notes, the undated `redrafted` marker, and the stale / deadline / follow-up
date math.

Every untouched data row is written back byte-for-byte (its raw text is kept
on read); only rows this tool edits are re-serialised, with minimal quoting.
A legacy 13-column row (no deadline) reads as deadline "" and keeps its width
until it is edited, at which point it gains the 14th column.

Output is one compact JSON value on stdout; human-readable errors go to
stderr. Exit 0 on success, 1 on a data/check error, 2 on a usage error.

Usage examples:
    python tools/tracker.py init
    python tools/tracker.py find --company acme --role "ml engineer" --open
    python tools/tracker.py add --date 2026-11-03 --company Acme --role "ML Engineer" --fit-rating 82
    python tools/tracker.py add --date 2026-11-03 --company Acme --role "ML Engineer" --status ready
    python tools/tracker.py list --status ready
    python tools/tracker.py update 3 --set status=applied --set date=2026-11-04 --note "2026-11-04 submitted via portal"
    python tools/tracker.py followups --today 2026-11-03 --quiet-days 10 --max 2
    python tools/tracker.py --tracker /tmp/t.csv stale --today 2026-11-03 --days 30
"""

import argparse
import csv
import datetime as _dt
import io
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRACKER = REPO_ROOT / "job_search_tracker.csv"

# The exact 14 columns from /apply Step 6b and /outcome Step 1.1.
HEADER = [
    "date",
    "company",
    "sector",
    "role",
    "role_type",
    "channel",
    "status",
    "contact_person",
    "fit_rating",
    "notes",
    "cv_file",
    "cover_letter_file",
    "source",
    "deadline",
]
HEADER_LINE = ",".join(HEADER)

# /outcome "Tracker status vocabulary", in order.
STATUSES = [
    "ready",
    "drafted",
    "applied",
    "interview",
    "offer",
    "hired",
    "rejected",
    "no_response",
    "offer_declined",
    "withdrawn",
]
FINAL_STATUSES = ["hired", "rejected", "no_response", "offer_declined", "withdrawn"]
OPEN_STATUSES = [s for s in STATUSES if s not in FINAL_STATUSES]
# Open, but nothing was sent: `ready` (pre-drafted by /quick-apply, awaiting the
# user's approval) and `drafted` (documents written, not yet submitted). No
# follow-up or staleness clock runs on these; only the deadline clock does.
NOT_SENT_STATUSES = ("ready", "drafted")
# Accepted on read only, never written.
LEGACY_ALIASES = {"no response": "no_response", "offer declined": "offer_declined"}

# "Never move it backwards": ready < drafted < applied < interview < offer < terminal.
_TERMINAL_RANK = 5
STATUS_ORDER = {
    "ready": 0,
    "drafted": 1,
    "applied": 2,
    "interview": 3,
    "offer": 4,
}
for _final in FINAL_STATUSES:
    STATUS_ORDER[_final] = _TERMINAL_RANK

CHANNELS = ["portal", "online", "email", "linkedin", "referral"]
MARKERS = ["redrafted"]
NOTE_SEPARATOR = "; "

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
STRICT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FOLLOWED_UP_RE = re.compile(r"followed\s+up\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE)


class TrackerError(Exception):
    """Raised for data or check failures (exit 1)."""


# ---------------------------------------------------------------------------
# Normalisation and validation helpers
# ---------------------------------------------------------------------------


def normalize_status(value):
    """Canonical spelling for a status read from the file (legacy aliases folded)."""
    text = " ".join((value or "").split()).lower()
    return LEGACY_ALIASES.get(text, text)


def is_final(status):
    return normalize_status(status) in FINAL_STATUSES


def is_open(status):
    return not is_final(status)


def norm_key(value):
    """Case-insensitive, whitespace-collapsed match key."""
    return " ".join((value or "").split()).lower()


def parse_date(value):
    """Return a date for strict YYYY-MM-DD input, or None when it does not parse."""
    text = (value or "").strip()
    if not STRICT_DATE_RE.match(text):
        return None
    try:
        return _dt.date.fromisoformat(text)
    except ValueError:
        return None


def require_date(value, field):
    parsed = parse_date(value)
    if parsed is None:
        raise TrackerError(f"{field} must be YYYY-MM-DD, got {value!r}")
    return parsed


def validate_value(column, value):
    """Validate one column value for writing; return the value to store."""
    if column not in HEADER:
        raise TrackerError(f"unknown column {column!r}; expected one of {', '.join(HEADER)}")
    value = "" if value is None else str(value)
    if column == "status":
        text = value.strip()
        if text in LEGACY_ALIASES:
            raise TrackerError(
                f"status {text!r} is a legacy spelling; write {LEGACY_ALIASES[text]!r} instead"
            )
        if text not in STATUSES:
            raise TrackerError(f"status must be one of {', '.join(STATUSES)}, got {text!r}")
        return text
    if column == "fit_rating":
        text = value.strip()
        if text == "":
            return ""
        if not re.fullmatch(r"\d+", text):
            raise TrackerError(f"fit_rating must be a bare integer 0-100, got {value!r}")
        number = int(text)
        if not 0 <= number <= 100:
            raise TrackerError(f"fit_rating must be between 0 and 100, got {number}")
        return str(number)
    if column in ("date", "deadline"):
        text = value.strip()
        if column == "deadline" and text == "":
            return ""
        require_date(text, column)
        return text
    if column in ("company", "role") and not value.strip():
        raise TrackerError(f"{column} must not be empty")
    return value


# ---------------------------------------------------------------------------
# File model
# ---------------------------------------------------------------------------


class Record:
    """One physical CSV record: its raw text plus parsed fields."""

    __slots__ = ("raw", "fields", "dirty")

    def __init__(self, raw, fields):
        self.raw = raw
        self.fields = fields
        self.dirty = False

    @property
    def is_blank(self):
        return not self.fields or (len(self.fields) == 1 and self.fields[0].strip() == "")


class Tracker:
    """In-memory view of the tracker file that preserves untouched rows."""

    def __init__(self, path):
        self.path = Path(path)
        self.header_raw = ""
        self.header = []
        self.records = []
        self.header_dirty = False

    # -- loading ----------------------------------------------------------

    @classmethod
    def load(cls, path):
        tracker = cls(path)
        if not tracker.path.exists():
            raise TrackerError(f"tracker not found: {tracker.path} (run `tracker.py init` first)")
        with open(tracker.path, "r", encoding="utf-8", newline="") as handle:
            text = handle.read()
        lines = text.splitlines(keepends=True)
        reader = csv.reader(lines)
        consumed = 0
        records = []
        for fields in reader:
            raw = "".join(lines[consumed:reader.line_num])
            consumed = reader.line_num
            records.append(Record(raw, fields))
        if consumed < len(lines):
            # csv consumed fewer physical lines than exist (should not happen); keep bytes.
            records.append(Record("".join(lines[consumed:]), []))
        if not records or records[0].is_blank:
            raise TrackerError(f"tracker has no header line: {tracker.path}")
        header_record = records[0]
        tracker.header_raw = header_record.raw
        # A spreadsheet "save as CSV" may prefix a UTF-8 BOM; the raw header line
        # keeps it byte-for-byte, only the parsed column name is cleaned.
        tracker.header = [column.strip().lstrip("\ufeff") for column in header_record.fields]
        missing = [column for column in HEADER[:-1] if column not in tracker.header]
        if missing:
            raise TrackerError(
                f"tracker header is missing required columns: {', '.join(missing)}"
            )
        tracker.records = records[1:]
        return tracker

    @property
    def has_deadline(self):
        return "deadline" in self.header

    def column_index(self, column):
        if column in self.header:
            return self.header.index(column)
        return None

    # -- rows -------------------------------------------------------------

    def data_records(self):
        """(1-based index, Record) for every non-blank data record."""
        index = 0
        for record in self.records:
            if record.is_blank:
                continue
            index += 1
            yield index, record

    def get(self, column, record):
        position = self.column_index(column)
        if position is None or position >= len(record.fields):
            return ""
        return record.fields[position]

    def row_dict(self, index, record):
        row = {"row": index}
        for column in HEADER:
            row[column] = self.get(column, record)
        status = row["status"]
        row["is_final"] = is_final(status)
        row["is_open"] = is_open(status)
        return row

    def record_at(self, index):
        for current, record in self.data_records():
            if current == index:
                return record
        raise TrackerError(f"row {index} does not exist (tracker has {self.count()} data rows)")

    def count(self):
        return sum(1 for _ in self.data_records())

    def set_field(self, record, column, value):
        """Assign a parsed field; widens a short row (legacy width) as needed."""
        position = self.column_index(column)
        if position is None:
            raise TrackerError(f"column {column!r} is not in the tracker header")
        while len(record.fields) <= position:
            record.fields.append("")
        record.fields[position] = value
        record.dirty = True

    def new_record(self, values):
        fields = [values.get(column, "") for column in self.header]
        record = Record("", fields)
        record.dirty = True
        self.records.append(record)
        return record

    # -- header migration ---------------------------------------------------

    def migrate(self):
        """Append `,deadline` to the header line only. Returns True when changed."""
        if self.has_deadline:
            return False
        stripped = self.header_raw.rstrip("\r\n")
        ending = self.header_raw[len(stripped):] or "\n"
        self.header_raw = stripped + ",deadline" + ending
        self.header.append("deadline")
        self.header_dirty = True
        return True

    # -- saving -------------------------------------------------------------

    @staticmethod
    def serialize(fields):
        buffer = io.StringIO()
        writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        writer.writerow(fields)
        return buffer.getvalue()

    def save(self):
        parts = []
        header_raw = self.header_raw
        if not header_raw.endswith(("\n", "\r")):
            header_raw += "\n"
        parts.append(header_raw)
        for record in self.records:
            if record.dirty:
                if parts and not parts[-1].endswith(("\n", "\r")):
                    parts[-1] += "\n"
                width = max(len(self.header), len(record.fields))
                fields = list(record.fields) + [""] * (width - len(record.fields))
                parts.append(self.serialize(fields))
            else:
                parts.append(record.raw)
        with open(self.path, "w", encoding="utf-8", newline="") as handle:
            handle.write("".join(parts))


# ---------------------------------------------------------------------------
# Date math shared by stale / followups
# ---------------------------------------------------------------------------


def dated_notes(notes):
    """Every YYYY-MM-DD that parses inside a notes string."""
    found = []
    for match in DATE_RE.finditer(notes or ""):
        parsed = parse_date(match.group(0))
        if parsed is not None:
            found.append(parsed)
    return found


def latest_activity(row):
    """Most recent of the date column and any dated note; None when nothing parses."""
    candidates = dated_notes(row.get("notes", ""))
    date = parse_date(row.get("date", ""))
    if date is not None:
        candidates.append(date)
    return max(candidates) if candidates else None


def followups_sent(notes):
    return len(FOLLOWED_UP_RE.findall(notes or ""))


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")


def warn(message):
    sys.stderr.write(f"tracker: {message}\n")


def cmd_init(args):
    path = Path(args.tracker)
    if path.exists() and path.stat().st_size > 0:
        emit({"created": False})
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(HEADER_LINE + "\n")
    emit({"created": True})
    return 0


def cmd_migrate(args):
    tracker = Tracker.load(args.tracker)
    migrated = tracker.migrate()
    if migrated:
        tracker.save()
    emit({"migrated": migrated, "columns": len(tracker.header)})
    return 0


def _matches(row, args):
    if getattr(args, "company", None) and norm_key(row["company"]) != norm_key(args.company):
        return False
    if getattr(args, "role", None) and norm_key(row["role"]) != norm_key(args.role):
        return False
    if getattr(args, "open", False) and not row["is_open"]:
        return False
    if getattr(args, "final", False) and not row["is_final"]:
        return False
    if getattr(args, "status", None) and normalize_status(row["status"]) != normalize_status(args.status):
        return False
    return True


def cmd_find(args):
    if not norm_key(args.company):
        raise TrackerError("--company must not be empty")
    tracker = Tracker.load(args.tracker)
    rows = [tracker.row_dict(i, r) for i, r in tracker.data_records()]
    emit([row for row in rows if _matches(row, args)])
    return 0


def cmd_list(args):
    tracker = Tracker.load(args.tracker)
    rows = [tracker.row_dict(i, r) for i, r in tracker.data_records()]
    emit([row for row in rows if _matches(row, args)])
    return 0


_ADD_FLAGS = {
    "date": "date",
    "company": "company",
    "sector": "sector",
    "role": "role",
    "role_type": "role_type",
    "channel": "channel",
    "status": "status",
    "contact_person": "contact_person",
    "fit_rating": "fit_rating",
    "notes": "notes",
    "cv_file": "cv_file",
    "cover_letter_file": "cover_letter_file",
    "source": "source",
    "deadline": "deadline",
}


def cmd_add(args):
    tracker = Tracker.load(args.tracker)
    values = {}
    for attr, column in _ADD_FLAGS.items():
        raw = getattr(args, attr, None)
        values[column] = validate_value(column, "" if raw is None else raw)
    migrated = tracker.migrate()
    record = tracker.new_record(values)
    tracker.save()
    index = tracker.count()
    payload = tracker.row_dict(index, record)
    if migrated:
        payload["migrated"] = True
    emit(payload)
    return 0


def _parse_set(items):
    updates = {}
    for item in items or []:
        if "=" not in item:
            raise TrackerError(f"--set expects col=value, got {item!r}")
        column, value = item.split("=", 1)
        column = column.strip()
        updates[column] = validate_value(column, value)
    return updates


def _check_status_transition(current, new, force):
    cur = normalize_status(current)
    if cur == new:
        return
    cur_rank = STATUS_ORDER.get(cur)
    new_rank = STATUS_ORDER[new]
    if cur_rank is None:
        # Unknown current status: nothing to compare against; allow.
        return
    if cur_rank == _TERMINAL_RANK and not force:
        raise TrackerError(
            f"status {cur!r} is final; moving it to {new!r} requires --force"
        )
    if new_rank < cur_rank and not force:
        raise TrackerError(
            f"status {cur!r} -> {new!r} moves the row backwards; pass --force to override"
        )


def cmd_update(args):
    tracker = Tracker.load(args.tracker)
    record = tracker.record_at(args.row)
    before = tracker.row_dict(args.row, record)
    updates = _parse_set(args.set)
    if args.note is not None and not dated_notes(args.note):
        raise TrackerError("--note must carry a YYYY-MM-DD date (e.g. \"2026-11-03 applied via portal\")")
    if args.marker is not None and args.marker not in MARKERS:
        raise TrackerError(f"--marker must be one of {', '.join(MARKERS)}")

    warnings = []
    if "status" in updates:
        _check_status_transition(before["status"], updates["status"], args.force)
        current = normalize_status(before["status"])
        # Warn only when the row leaves the unsent set: ready -> drafted keeps
        # its "drafted on" date (both are unsent), so no warning there.
        if (
            current in NOT_SENT_STATUSES
            and updates["status"] not in NOT_SENT_STATUSES
            and "date" not in updates
        ):
            warnings.append(
                f"status left {current} (nothing was sent yet) but date was not set: "
                "/outcome and /gmail-sync overwrite date with the actual submission "
                "date (pass --set date=YYYY-MM-DD)"
            )

    if not updates and args.note is None and args.marker is None:
        raise TrackerError("nothing to update: pass --set, --note or --marker")

    migrated = tracker.migrate()
    for column, value in updates.items():
        tracker.set_field(record, column, value)

    notes = tracker.get("notes", record)
    if args.note is not None:
        note = args.note.strip()
        notes = note if not notes.strip() else notes.rstrip() + NOTE_SEPARATOR + note
        tracker.set_field(record, "notes", notes)
    if args.marker is not None:
        marker = args.marker
        tokens = [piece.strip() for piece in re.split(r"[;,]", notes)]
        if marker not in tokens:
            notes = marker if not notes.strip() else notes.rstrip() + NOTE_SEPARATOR + marker
            tracker.set_field(record, "notes", notes)

    tracker.save()
    payload = tracker.row_dict(args.row, record)
    if warnings:
        payload["date_unchanged_warning"] = warnings[0]
    if migrated:
        payload["migrated"] = True
    emit(payload)
    return 0


def _today(args):
    return require_date(args.today, "--today")


def cmd_stale(args):
    """Open rows quiet for N+ days; the unsent statuses (ready, drafted) never qualify."""
    tracker = Tracker.load(args.tracker)
    today = _today(args)
    out = []
    for index, record in tracker.data_records():
        row = tracker.row_dict(index, record)
        if not row["is_open"] or normalize_status(row["status"]) in NOT_SENT_STATUSES:
            continue
        last = latest_activity(row)
        if last is None:
            warn(f"row {index} ({row['company']} / {row['role']}): no parseable date; skipped")
            continue
        quiet = (today - last).days
        if quiet >= args.days:
            out.append({
                "row": index,
                "company": row["company"],
                "role": row["role"],
                "status": row["status"],
                "days_quiet": quiet,
                "last_activity": last.isoformat(),
            })
    emit(out)
    return 0


def cmd_deadlines(args):
    """Open rows whose deadline passed or is due within N days.

    `drafted_past_deadline` keeps its key for /outcome Step 1.3 but holds every
    unsent row (`ready` as well as `drafted`) whose deadline has passed: documents
    written, never sent, and now unsendable.
    """
    tracker = Tracker.load(args.tracker)
    today = _today(args)
    passed, due_soon, drafted_past = [], [], []
    for index, record in tracker.data_records():
        row = tracker.row_dict(index, record)
        if not row["is_open"] or not row["deadline"].strip():
            continue
        deadline = parse_date(row["deadline"])
        if deadline is None:
            warn(f"row {index} ({row['company']} / {row['role']}): deadline {row['deadline']!r} does not parse; skipped")
            continue
        days_left = (deadline - today).days
        entry = {
            "row": index,
            "company": row["company"],
            "role": row["role"],
            "deadline": row["deadline"],
            "status": row["status"],
            "days_left": days_left,
        }
        if days_left < 0:
            passed.append(entry)
            if normalize_status(row["status"]) in NOT_SENT_STATUSES:
                drafted_past.append(entry)
        elif days_left <= args.within:
            due_soon.append(entry)
    emit({"passed": passed, "due_soon": due_soon, "drafted_past_deadline": drafted_past})
    return 0


def cmd_followups(args):
    """Follow-up candidates per /outcome Step 2b; unsent rows (ready, drafted) are never chased."""
    tracker = Tracker.load(args.tracker)
    today = _today(args)
    out = []
    for index, record in tracker.data_records():
        row = tracker.row_dict(index, record)
        if not row["is_open"] or normalize_status(row["status"]) in NOT_SENT_STATUSES:
            continue
        sent = followups_sent(row["notes"])
        if sent >= args.max:
            continue
        last = latest_activity(row)
        if last is None:
            warn(f"row {index} ({row['company']} / {row['role']}): no parseable date; skipped")
            continue
        quiet = (today - last).days
        if quiet < args.quiet_days:
            continue
        out.append({
            "row": index,
            "company": row["company"],
            "role": row["role"],
            "status": row["status"],
            "days_quiet": quiet,
            "followups_sent": sent,
            "channel": row["channel"],
            "contact_person": row["contact_person"],
            "last_activity": last.isoformat(),
        })
    emit(out)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="tracker.py",
        description="Read and edit job_search_tracker.csv one row at a time.",
    )
    parser.add_argument(
        "--tracker",
        default=str(DEFAULT_TRACKER),
        help=f"path to the tracker CSV (default: {DEFAULT_TRACKER})",
    )
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    p = sub.add_parser("init", help="create the tracker with the standard header if absent")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("migrate", help="append ,deadline to a legacy 13-column header line")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("find", help="match rows on company (and role)")
    p.add_argument("--company", required=True)
    p.add_argument("--role")
    p.add_argument("--open", action="store_true", help="only rows whose status is not final")
    p.add_argument("--status", help="only rows with this status (legacy spellings accepted)")
    p.set_defaults(func=cmd_find, final=False)

    p = sub.add_parser("add", help="append a new row")
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--company", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--sector", default="")
    p.add_argument("--role-type", dest="role_type", default="")
    p.add_argument("--channel", default="", help="e.g. portal, online (empty when unknown)")
    p.add_argument(
        "--status",
        default="drafted",
        help="one of the /outcome vocabulary (default: drafted; /quick-apply writes ready)",
    )
    p.add_argument("--contact-person", dest="contact_person", default="")
    p.add_argument("--fit-rating", dest="fit_rating", default="", help="bare integer 0-100")
    p.add_argument("--notes", default="")
    p.add_argument("--cv-file", dest="cv_file", default="")
    p.add_argument("--cover-letter-file", dest="cover_letter_file", default="")
    p.add_argument("--source", default="")
    p.add_argument("--deadline", default="", help="YYYY-MM-DD or empty")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("update", help="edit one row by its 1-based data-row index")
    p.add_argument("row", type=int)
    p.add_argument("--set", action="append", metavar="COL=VALUE", help="repeatable")
    p.add_argument("--note", help='dated note to append to notes, e.g. "2026-11-03 applied"')
    p.add_argument("--marker", choices=MARKERS, help="append an undated marker once")
    p.add_argument("--force", action="store_true", help="allow a backwards or off-final status move")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("list", help="list rows")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--open", action="store_true")
    group.add_argument("--final", action="store_true")
    p.add_argument("--status")
    p.set_defaults(func=cmd_list, company=None, role=None)

    p = sub.add_parser("stale", help="open rows quiet for N+ days (ready/drafted never qualify: nothing was sent)")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--today", required=True, help="YYYY-MM-DD")
    p.set_defaults(func=cmd_stale)

    p = sub.add_parser(
        "deadlines",
        help="open rows whose deadline passed or is due soon (drafted_past_deadline covers ready and drafted)",
    )
    p.add_argument("--within", type=int, default=7)
    p.add_argument("--today", required=True, help="YYYY-MM-DD")
    p.set_defaults(func=cmd_deadlines)

    p = sub.add_parser("followups", help="follow-up candidates per /outcome Step 2b (ready/drafted never chased)")
    p.add_argument("--today", required=True, help="YYYY-MM-DD")
    p.add_argument("--quiet-days", dest="quiet_days", type=int, default=10)
    p.add_argument("--max", type=int, default=2, help="skip rows with this many follow-ups sent")
    p.set_defaults(func=cmd_followups)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except TrackerError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except OSError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
