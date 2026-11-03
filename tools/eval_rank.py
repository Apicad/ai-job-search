#!/usr/bin/env python3
"""Offline eval harness for /rank: compare a scoring run against a saved golden run.

The scoring agents in `.claude/commands/rank.md` Step 2 return one JSON object
per job (dimension scores, location/language vetoes, strengths, gaps, expired
marker). Step 3 turns the dimensions into an overall score with fixed weights
(Technical 30%, Experience 25%, Behavioral 15%, Career 30%) and maps it to a
verdict band. This tool recomputes overall and band from the dimensions with
those exact weights, so two runs of the same postings can be compared
independently of which model or API route produced them. Use it to gate a
model switch: save a golden run on the current route, re-score the same
postings on the candidate route, and compare.

Usage:
    python tools/eval_rank.py save-golden --scores run.json --out golden.json --label "opus-4"
    python tools/eval_rank.py compare --scores run_new.json --golden golden.json --max-delta 5
    python tools/eval_rank.py table --scores run_new.json --golden golden.json

`--scores` accepts the raw Step 2 agent array or an object `{"jobs": [...]}`.
Output is one compact JSON document on stdout (or an aligned table with
`--format table` / the `table` subcommand); errors go to stderr. Exit 0 when the
comparison passes, 1 when it fails or the input is unusable, 2 on usage error.
"""

import argparse
import json
import math
import os
import sys
from pathlib import Path

WEIGHTS = {
    "technical": 0.30,
    "experience": 0.25,
    "behavioral": 0.15,
    "career": 0.30,
}
DIMENSIONS = tuple(WEIGHTS)

# (lower bound inclusive, band name), checked top-down - rank.md Step 3 rule 2.
VERDICT_BANDS = (
    (75, "Strong Fit"),
    (60, "Good Fit"),
    (45, "Moderate Fit"),
    (30, "Weak Fit"),
    (0, "Poor Fit"),
)

VETO_FAIL = "FAIL"


class EvalError(Exception):
    """Raised when an input file cannot be used for a comparison."""


# --------------------------------------------------------------------------
# Scoring arithmetic (model-independent)
# --------------------------------------------------------------------------


def compute_overall(dimensions):
    """Weighted overall score from a complete {dimension: number} mapping."""
    total = sum(float(dimensions.get(name, 0) or 0) * weight for name, weight in WEIGHTS.items())
    return round(total, 1)


def verdict_band(overall):
    for lower, name in VERDICT_BANDS:
        if overall >= lower:
            return name
    return VERDICT_BANDS[-1][1]


# --------------------------------------------------------------------------
# Loading and normalizing runs
# --------------------------------------------------------------------------


def _read_json(path):
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvalError(f"{path}: cannot read: {exc.strerror or exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise EvalError(f"{path}: not valid JSON: {exc}") from exc


def extract_job_list(data, path="<scores>"):
    """Return the raw job array from either the agent array or {"jobs": [...]}."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        jobs = data.get("jobs")
        if isinstance(jobs, list):
            return jobs
        raise EvalError(f"{path}: object input must carry a \"jobs\" array")
    raise EvalError(f"{path}: expected a JSON array or an object with a \"jobs\" array")


def _as_list_of_str(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _as_score(value):
    """Return a finite float, or None when the value is not a usable score.

    Booleans, non-numeric strings and NaN/inf are rejected: a `true` must not
    silently become 1.0, and a NaN would poison every delta and print invalid
    JSON on stdout.
    """
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
    value = float(value)
    return value if math.isfinite(value) else None


def normalize_job(raw, path="<scores>", index=0):
    """Reduce one Step 2 agent object to the comparable fields.

    Missing, non-numeric, boolean or non-finite dimensions count as 0 and are
    listed in "incomplete" so a comparison can show why a delta looks odd. An
    expired job is expected to carry no scores and is not flagged incomplete
    for that.
    """
    if not isinstance(raw, dict):
        raise EvalError(f"{path}: job #{index} is not an object")
    key = raw.get("key")
    if not isinstance(key, str) or not key.strip():
        raise EvalError(f"{path}: job #{index} has no \"key\"")
    expired = raw.get("status") == "expired" or raw.get("expired") is True

    scores = raw.get("scores")
    if not isinstance(scores, dict):
        scores = {}
    dimensions = {}
    incomplete = []
    for name in DIMENSIONS:
        value = _as_score(scores.get(name))
        if value is None:
            dimensions[name] = 0.0
            incomplete.append(name)
        else:
            dimensions[name] = value
    if expired:
        incomplete = []

    overall = compute_overall(dimensions)
    return {
        "key": key,
        "dimensions": dimensions,
        "overall": overall,
        "verdict": verdict_band(overall),
        "location_verdict": raw.get("location_verdict"),
        "language_gate": raw.get("language_gate"),
        "strengths": _as_list_of_str(raw.get("strengths")),
        "gaps": _as_list_of_str(raw.get("gaps")),
        "expired": bool(expired),
        "incomplete": incomplete,
    }


def normalize_run(jobs, path="<scores>"):
    """Map key -> normalized job. Duplicate keys keep the last occurrence."""
    result = {}
    for index, raw in enumerate(jobs):
        job = normalize_job(raw, path, index)
        if job["key"] in result:
            print(f"warning: {path}: duplicate key {job['key']!r}, keeping the last one", file=sys.stderr)
        result[job["key"]] = job
    return result


def load_run(path):
    """Load a scores file (agent array or {"jobs": [...]}) as key -> normalized job."""
    data = _read_json(path)
    return normalize_run(extract_job_list(data, str(path)), str(path))


def load_golden(path):
    """Load a golden file; a raw scores file is also accepted for convenience."""
    data = _read_json(path)
    if isinstance(data, dict) and isinstance(data.get("jobs"), dict):
        jobs = {}
        for key, entry in data["jobs"].items():
            if not isinstance(entry, dict):
                raise EvalError(f"{path}: golden entry {key!r} is not an object")
            entry = dict(entry)
            entry.setdefault("key", key)
            if "dimensions" in entry and "scores" not in entry:
                entry["scores"] = entry["dimensions"]
            if entry.get("expired") and "status" not in entry:
                entry["status"] = "expired"
            jobs[key] = normalize_job(entry, str(path))
        return jobs
    return normalize_run(extract_job_list(data, str(path)), str(path))


def build_golden(jobs_map, source, label=None):
    return {
        "label": label,
        "count": len(jobs_map),
        "source": os.path.basename(str(source)),
        "weights": dict(WEIGHTS),
        "jobs": {key: jobs_map[key] for key in sorted(jobs_map)},
    }


def save_golden(scores_path, out_path, label=None):
    jobs_map = load_run(scores_path)
    golden = build_golden(jobs_map, scores_path, label)
    out_path = Path(out_path)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(golden, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    except OSError as exc:
        raise EvalError(f"{out_path}: cannot write: {exc.strerror or exc}") from exc
    return golden


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


def _location_flip(old, new):
    """A location veto flips only when FAIL is entered or left."""
    return old != new and (old == VETO_FAIL or new == VETO_FAIL)


def compare_job(golden, scores):
    """Compare one job present in both runs.

    When the expired status differs, the job was scored in only one run, so
    neither the delta nor the veto fields are comparable: delta is None and no
    band/veto flip is reported. The mismatch itself is still surfaced via
    "expired_mismatch" and the summary's "expired_mismatches".
    """
    expired_mismatch = golden["expired"] != scores["expired"]
    if expired_mismatch:
        delta = None
        location_flip = False
        language_flip = False
    else:
        delta = round(scores["overall"] - golden["overall"], 1)
        location_flip = _location_flip(golden["location_verdict"], scores["location_verdict"])
        language_flip = golden["language_gate"] != scores["language_gate"]
    incomplete = sorted(set(golden["incomplete"]) | set(scores["incomplete"]))
    return {
        "key": golden["key"],
        "golden_overall": golden["overall"],
        "scores_overall": scores["overall"],
        "delta": delta,
        "golden_verdict": golden["verdict"],
        "scores_verdict": scores["verdict"],
        "band_flip": (not expired_mismatch) and golden["verdict"] != scores["verdict"],
        "golden_location": golden["location_verdict"],
        "scores_location": scores["location_verdict"],
        "location_flip": location_flip,
        "golden_language": golden["language_gate"],
        "scores_language": scores["language_gate"],
        "language_flip": language_flip,
        "veto_flip": location_flip or language_flip,
        "golden_expired": golden["expired"],
        "scores_expired": scores["expired"],
        "expired_mismatch": expired_mismatch,
        "incomplete": incomplete,
    }


def compare_runs(scores_map, golden_map, max_delta=5.0, min_overlap=1):
    """Compare two key -> normalized-job maps. Returns {"summary": ..., "jobs": [...]}."""
    common = sorted(set(scores_map) & set(golden_map))
    if len(common) < min_overlap:
        raise EvalError(
            f"only {len(common)} key(s) overlap between scores and golden; "
            f"--min-overlap is {min_overlap}"
        )
    jobs = [compare_job(golden_map[key], scores_map[key]) for key in common]
    deltas = [abs(job["delta"]) for job in jobs if job["delta"] is not None]
    max_abs = max(deltas) if deltas else 0.0
    mean_abs = round(sum(deltas) / len(deltas), 2) if deltas else 0.0
    band_flips = sum(1 for job in jobs if job["band_flip"])
    veto_flips = sum(1 for job in jobs if job["veto_flip"])
    expired_mismatches = sum(1 for job in jobs if job["expired_mismatch"])
    incomplete = [job["key"] for job in jobs if job["incomplete"]]
    passed = max_abs <= max_delta and veto_flips == 0
    summary = {
        "compared": len(common),
        "only_in_scores": sorted(set(scores_map) - set(golden_map)),
        "only_in_golden": sorted(set(golden_map) - set(scores_map)),
        "max_delta": max_delta,
        "max_abs_delta": max_abs,
        "mean_abs_delta": mean_abs,
        "band_flips": band_flips,
        "veto_flips": veto_flips,
        "expired_mismatches": expired_mismatches,
        "incomplete": incomplete,
        "pass": passed,
    }
    return {"summary": summary, "jobs": jobs}


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def _fmt_num(value):
    return "-" if value is None else f"{value:.1f}"


def _fmt_delta(value):
    return "-" if value is None else f"{value:+.1f}"


def _fmt_change(old, new, flipped):
    old = "-" if old is None else str(old)
    new = "-" if new is None else str(new)
    if old == new:
        return old
    return f"{old} -> {new}" + (" !" if flipped else "")


def format_table(result):
    """Render a compare result as an aligned text table, one line per job."""
    headers = ["key", "golden", "scores", "delta", "band", "location", "language", "expired", "notes"]
    rows = []
    for job in result["jobs"]:
        notes = []
        if job["expired_mismatch"]:
            notes.append("expired-mismatch")
        if job["incomplete"]:
            notes.append("incomplete:" + ",".join(job["incomplete"]))
        rows.append([
            job["key"],
            _fmt_num(job["golden_overall"]),
            _fmt_num(job["scores_overall"]),
            _fmt_delta(job["delta"]),
            "expired" if (job["golden_expired"] and job["scores_expired"])
            else _fmt_change(job["golden_verdict"], job["scores_verdict"], job["band_flip"]),
            _fmt_change(job["golden_location"], job["scores_location"], job["location_flip"]),
            _fmt_change(job["golden_language"], job["scores_language"], job["language_flip"]),
            _fmt_change(job["golden_expired"], job["scores_expired"], job["expired_mismatch"]),
            " ".join(notes) or "-",
        ])
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    right = {1, 2, 3}

    def render(cells):
        parts = []
        for i, cell in enumerate(cells):
            parts.append(cell.rjust(widths[i]) if i in right else cell.ljust(widths[i]))
        return "  ".join(parts).rstrip()

    lines = [render(headers), render(["-" * w for w in widths])]
    lines.extend(render(row) for row in rows)
    summary = result["summary"]
    lines.append("")
    lines.append(
        "compared={compared} only_in_scores={only_scores} only_in_golden={only_golden} "
        "max_abs_delta={max_abs:.1f} (limit {limit:.1f}) mean_abs_delta={mean_abs:.2f} "
        "band_flips={band} veto_flips={veto} expired_mismatches={expired} pass={passed}".format(
            compared=summary["compared"],
            only_scores=len(summary["only_in_scores"]),
            only_golden=len(summary["only_in_golden"]),
            max_abs=summary["max_abs_delta"],
            limit=summary["max_delta"],
            mean_abs=summary["mean_abs_delta"],
            band=summary["band_flips"],
            veto=summary["veto_flips"],
            expired=summary["expired_mismatches"],
            passed="true" if summary["pass"] else "false",
        )
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _add_compare_args(sub, with_format):
    sub.add_argument("--scores", type=Path, required=True, help="scoring run to evaluate (agent array or {\"jobs\": [...]})")
    sub.add_argument("--golden", type=Path, required=True, help="golden run written by save-golden")
    sub.add_argument("--max-delta", type=float, default=5.0, help="largest tolerated |overall delta| per job (default: 5)")
    sub.add_argument("--min-overlap", type=int, default=1, help="minimum keys present in both runs (default: 1)")
    if with_format:
        sub.add_argument("--format", choices=("json", "table"), default="json", help="output format (default: json)")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Compare a /rank scoring run against a saved golden run of the same postings."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    save = subparsers.add_parser("save-golden", help="normalize a scoring run and save it as the golden reference")
    save.add_argument("--scores", type=Path, required=True, help="scoring run to freeze (agent array or {\"jobs\": [...]})")
    save.add_argument("--out", type=Path, required=True, help="golden file to write")
    save.add_argument("--label", default=None, help="free-text label, e.g. the model or route that produced the run")

    compare = subparsers.add_parser("compare", help="compare a scoring run against a golden file")
    _add_compare_args(compare, with_format=True)

    table = subparsers.add_parser("table", help="same as compare --format table")
    _add_compare_args(table, with_format=False)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "save-golden":
            golden = save_golden(args.scores, args.out, args.label)
            incomplete = [key for key, job in golden["jobs"].items() if job["incomplete"]]
            print(json.dumps({
                "out": str(args.out),
                "label": golden["label"],
                "count": golden["count"],
                "source": golden["source"],
                "incomplete": incomplete,
            }))
            return 0

        if not math.isfinite(args.max_delta) or args.max_delta < 0:
            print("Error: --max-delta must be a finite number >= 0", file=sys.stderr)
            return 2
        if args.min_overlap < 1:
            print("Error: --min-overlap must be >= 1", file=sys.stderr)
            return 2
        scores_map = load_run(args.scores)
        golden_map = load_golden(args.golden)
        result = compare_runs(scores_map, golden_map, args.max_delta, args.min_overlap)
        fmt = "table" if args.command == "table" else args.format
        if fmt == "table":
            print(format_table(result))
        else:
            print(json.dumps(result))
        return 0 if result["summary"]["pass"] else 1
    except EvalError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
