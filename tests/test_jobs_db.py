"""Tests for tools/jobs_db.py - the CLI over seen_jobs.json and its posting store.

Every test runs the real code path (`main([...])`) against a database inside
a tempfile directory, with a fixture shaped like real portal CLI output
(`{"meta": ..., "results": [...]}`), a tracker CSV using the exact header from
/apply Step 6b, and /rank Step 2 agent JSON. No repo data path is touched and
nothing hits the network.
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools import jobs_db
from tools.jobs_db import main, make_key, overall_score, verdict_for

TRACKER_HEADER = (
    "date,company,sector,role,role_type,channel,status,contact_person,"
    "fit_rating,notes,cv_file,cover_letter_file,source,deadline"
)

PORTAL_OUTPUT = {
    "meta": {"count": 3, "page": 1, "total": 3},
    "results": [
        {
            "id": "data-engineer-acme-1a2b3c",
            "title": "Data Engineer",
            "company": "Acme ApS",
            "location": "Aarhus, Denmark",
            "date": "2026-11-01",
            "url": "https://portal.example/jobs/1",
            "description": "## About the role\n\nBuild   pipelines in Python and SQL.\n\n- Airflow\n- dbt\n",
        },
        {
            "id": "ml-lead-beta-4d5e6f",
            "title": "ML Lead",
            "company": "Beta A/S",
            "location": "Odense, Denmark",
            "date": None,
            "url": "https://portal.example/jobs/2",
            "deadline": "2026-11-20",
            "description": None,
        },
        {
            "title": "Geophysicist",
            "company": "Gamma Energy",
            "location": "Esbjerg",
            "date": "2026-10-30",
            "url": None,
        },
    ],
}


def run_cli(argv):
    """Run main() and return (exit code, parsed stdout JSON, stderr text)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    text = out.getvalue()
    payload = json.loads(text) if text.strip() else None
    return code, payload, err.getvalue()


class JobsDbCliTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = self.root / "job_scraper" / "seen_jobs.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_json(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def read_db(self):
        return json.loads(self.db.read_text(encoding="utf-8"))

    def seed_portal(self, today="2026-11-03"):
        fixture = self.write_json("linkedin.json", PORTAL_OUTPUT)
        return run_cli(
            ["--db", str(self.db), "upsert", "--from", fixture,
             "--portal", "linkedin-search", "--source", "cli", "--today", today]
        )

    def cli(self, *argv):
        return run_cli(["--db", str(self.db), *argv])


class UpsertTests(JobsDbCliTestCase):
    def test_creates_db_in_skill_shape_and_stores_description(self):
        code, result, _ = self.seed_portal()
        self.assertEqual(code, 0)
        self.assertEqual(result["existing"], 0)
        self.assertEqual(result["stored_postings"], 1)
        self.assertEqual(
            [row["key"] for row in result["added"]],
            ["https://portal.example/jobs/1", "https://portal.example/jobs/2", "gamma energy|geophysicist"],
        )
        self.assertEqual(result["added"][1]["deadline"], "2026-11-20")
        self.assertIsNone(result["added"][0]["deadline"], "deadline is never inferred")

        data = self.read_db()
        self.assertEqual(list(data), ["seen"], "SKILL.md Step 0 shape: {\"seen\": {...}}")
        entry = data["seen"]["https://portal.example/jobs/1"]
        self.assertEqual(entry["status"], "new")
        self.assertIsNone(entry["fit"])
        self.assertEqual(entry["first_seen"], "2026-11-03")
        self.assertEqual(entry["portal"], "linkedin-search")
        self.assertEqual(entry["source"], "cli")
        self.assertEqual(entry["date"], "2026-11-01")
        self.assertEqual(entry["posting_file"], "postings/" + jobs_db.posting_filename(entry["url"]))
        self.assertEqual(entry["posting_fetched"], "2026-11-03")
        posting = (self.db.parent / entry["posting_file"]).read_text(encoding="utf-8")
        self.assertIn("key: https://portal.example/jobs/1", posting)
        self.assertIn("Build   pipelines in Python and SQL.", posting)
        self.assertNotIn("posting_file", data["seen"]["https://portal.example/jobs/2"])
        self.assertEqual(self.db.read_text(encoding="utf-8")[:10], "{\n  \"seen\"", "indent=2")

    def test_reupsert_is_idempotent_and_preserves_unknown_fields(self):
        self.seed_portal()
        data = self.read_db()
        data["seen"]["https://portal.example/jobs/2"]["custom_note"] = "keep me"
        data["seen"]["https://portal.example/jobs/2"]["status"] = "skipped"
        self.db.write_text(json.dumps(data, indent=2), encoding="utf-8")

        code, result, _ = self.seed_portal(today="2026-11-10")
        self.assertEqual(code, 0)
        self.assertEqual(result["added"], [])
        self.assertEqual(result["existing"], 3)
        self.assertEqual(result["stored_postings"], 0)
        after = self.read_db()["seen"]
        self.assertEqual(len(after), 3)
        self.assertEqual(after["https://portal.example/jobs/2"]["custom_note"], "keep me")
        self.assertEqual(after["https://portal.example/jobs/2"]["status"], "skipped")
        self.assertEqual(after["https://portal.example/jobs/1"]["first_seen"], "2026-11-03")

    def test_existing_entry_gains_posting_when_later_output_carries_description(self):
        self.seed_portal()
        richer = {"results": [dict(PORTAL_OUTPUT["results"][1], description="Lead the ML team.")]}
        fixture = self.write_json("later.json", richer)
        code, result, _ = self.cli("upsert", "--from", fixture, "--portal", "freehire-search",
                                   "--source", "cli", "--today", "2026-11-04")
        self.assertEqual(code, 0)
        self.assertEqual(result["existing"], 1)
        self.assertEqual(result["stored_postings"], 1)
        entry = self.read_db()["seen"]["https://portal.example/jobs/2"]
        self.assertEqual(entry["posting_fetched"], "2026-11-04")
        self.assertEqual(entry["portal"], "linkedin-search", "existing entries stay untouched")

    def test_company_title_dedup_across_urls(self):
        self.seed_portal()
        repost = {"results": [{"title": "  data   ENGINEER ", "company": "ACME ApS",
                               "location": "Aarhus", "date": "2026-11-05",
                               "url": "https://other.example/jobs/99"}]}
        fixture = self.write_json("repost.json", repost)
        code, result, _ = self.cli("upsert", "--from", fixture, "--portal", "jobindex-search",
                                   "--source", "websearch")
        self.assertEqual(code, 0)
        self.assertEqual(result["added"], [])
        self.assertEqual(result["existing"], 1)

    def test_accepts_top_level_array(self):
        fixture = self.write_json("arr.json", PORTAL_OUTPUT["results"][:1])
        code, result, _ = self.cli("upsert", "--from", fixture, "--portal", "x", "--source", "cli")
        self.assertEqual(code, 0)
        self.assertEqual(len(result["added"]), 1)

    def test_missing_input_file_is_a_data_error(self):
        code, _, err = self.cli("upsert", "--from", str(self.root / "nope.json"),
                                "--portal", "x", "--source", "cli")
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)

    def test_bad_source_is_a_usage_error(self):
        fixture = self.write_json("arr.json", [])
        code, _, _ = self.cli("upsert", "--from", fixture, "--portal", "x", "--source", "magic")
        self.assertEqual(code, 2)


class PostingStoreTests(JobsDbCliTestCase):
    def test_store_and_get_posting(self):
        self.seed_portal()
        text_file = self.root / "detail.txt"
        text_file.write_text("Line one.\n\n  Line   two with   spaces.\n" + "x" * 400, encoding="utf-8")
        code, result, _ = self.cli("store-posting", "https://portal.example/jobs/2",
                                   "--from", str(text_file), "--source", "webfetch",
                                   "--today", "2026-11-03")
        self.assertEqual(code, 0)
        self.assertEqual(result["key"], "https://portal.example/jobs/2")
        self.assertEqual(result["chars"], len(text_file.read_text(encoding="utf-8")))
        self.assertEqual(len(result["snippet"]), 300)
        self.assertTrue(result["snippet"].startswith("Line one. Line two with spaces. xxx"))
        stored = (self.db.parent / result["posting_file"]).read_text(encoding="utf-8")
        self.assertIn("source: webfetch", stored)

        code, got, _ = self.cli("get-posting", "https://portal.example/jobs/2")
        self.assertEqual(code, 0)
        self.assertEqual(got["url"], "https://portal.example/jobs/2")
        self.assertEqual(got["fetched"], "2026-11-03")
        self.assertTrue(got["text"].startswith("Line one.\n\n  Line   two"), "header stripped, body verbatim")
        self.assertNotIn("stale", got)

    def test_get_posting_stale_and_fresh(self):
        self.seed_portal()
        code, got, _ = self.cli("get-posting", "https://portal.example/jobs/1",
                                "--max-age-days", "7", "--today", "2026-11-20")
        self.assertEqual(code, 0)
        self.assertEqual(got, {"key": "https://portal.example/jobs/1", "stale": True, "fetched": "2026-11-03"})
        code, got, _ = self.cli("get-posting", "https://portal.example/jobs/1",
                                "--max-age-days", "7", "--today", "2026-11-10")
        self.assertEqual(code, 0)
        self.assertIn("text", got)

    def test_get_posting_missing(self):
        self.seed_portal()
        code, got, _ = self.cli("get-posting", "gamma energy|geophysicist")
        self.assertEqual(code, 0)
        self.assertEqual(got, {"key": "gamma energy|geophysicist", "missing": True})

    def test_store_posting_unknown_key_exits_1(self):
        self.seed_portal()
        text_file = self.root / "detail.txt"
        text_file.write_text("whatever", encoding="utf-8")
        code, payload, err = self.cli("store-posting", "nope", "--from", str(text_file))
        self.assertEqual(code, 1)
        self.assertIsNone(payload)
        self.assertIn("unknown key", err)


class ListTests(JobsDbCliTestCase):
    def test_list_excludes_tracker_rows_case_insensitively(self):
        self.seed_portal()
        tracker = self.root / "job_search_tracker.csv"
        tracker.write_text(
            TRACKER_HEADER + "\n"
            "2026-10-01,acme aps,Tech,DATA ENGINEER,,portal,rejected,,70,,cv/a.tex,cl/a.tex,,\n"
            "2026-10-02,Beta A/S,Tech,Something Else,,portal,applied,,60,,,,,\n",
            encoding="utf-8",
        )
        code, rows, _ = self.cli("list", "--status", "new", "--exclude-tracker", str(tracker))
        self.assertEqual(code, 0)
        self.assertEqual([r["key"] for r in rows],
                         ["https://portal.example/jobs/2", "gamma energy|geophysicist"])
        self.assertEqual(
            list(rows[0]),
            ["key", "title", "company", "location", "url", "status", "deadline",
             "posting_fetched", "rank_score"],
        )
        self.assertIsNone(rows[0]["rank_score"])

    def test_list_focus_fields_and_min_score(self):
        self.seed_portal()
        code, rows, _ = self.cli("list", "--focus", "odense", "--fields", "key,title")
        self.assertEqual(code, 0)
        self.assertEqual(rows, [{"key": "https://portal.example/jobs/2", "title": "ML Lead"}])
        code, rows, _ = self.cli("list", "--min-score", "50")
        self.assertEqual(rows, [])

    def test_list_missing_tracker_excludes_nothing(self):
        self.seed_portal()
        code, rows, _ = self.cli("list", "--exclude-tracker", str(self.root / "absent.csv"))
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 3)


class SetFitTests(JobsDbCliTestCase):
    def test_set_fit_and_status(self):
        self.seed_portal()
        fixture = self.write_json("fit.json", [
            {"key": "https://portal.example/jobs/1", "fit": "high"},
            {"key": "https://portal.example/jobs/2", "fit": "low", "status": "skipped"},
        ])
        code, result, _ = self.cli("set-fit", "--from", fixture)
        self.assertEqual(code, 0)
        self.assertEqual(result, {"updated": 2, "unknown": [], "invalid": []})
        seen = self.read_db()["seen"]
        self.assertEqual(seen["https://portal.example/jobs/1"]["fit"], "high")
        self.assertEqual(seen["https://portal.example/jobs/1"]["status"], "new")
        self.assertEqual(seen["https://portal.example/jobs/2"]["status"], "skipped")

    def test_set_fit_unknown_key_exits_1(self):
        self.seed_portal()
        fixture = self.write_json("fit.json", [{"key": "ghost", "fit": "high"}])
        code, result, _ = self.cli("set-fit", "--from", fixture)
        self.assertEqual(code, 1)
        self.assertEqual(result["unknown"], ["ghost"])


class ApplyRankingTests(JobsDbCliTestCase):
    def agent_result(self, key, **overrides):
        base = {
            "key": key,
            "status": "scored",
            "scores": {"technical": 80, "experience": 70, "behavioral": 60, "career": 90},
            "location_verdict": "PASS",
            "language_gate": "FLAG",
            "language_note": "posting: fluent Danish; declared: B2",
            "deadline": "2026-12-01",
            "strengths": ["Python pipelines", "SQL"],
            "gaps": ["No Airflow"],
            "language": "en",
        }
        base.update(overrides)
        return base

    def test_scores_use_documented_weights_and_bands(self):
        # 0.30*80 + 0.25*70 + 0.15*60 + 0.30*90 = 24 + 17.5 + 9 + 27 = 77.5 -> 78
        self.assertEqual(overall_score({"technical": 80, "experience": 70, "behavioral": 60, "career": 90}), 78)
        self.assertEqual(verdict_for(78), "Strong Fit")
        self.assertEqual(verdict_for(75), "Strong Fit")
        self.assertEqual(verdict_for(74), "Good Fit")
        self.assertEqual(verdict_for(60), "Good Fit")
        self.assertEqual(verdict_for(59), "Moderate Fit")
        self.assertEqual(verdict_for(45), "Moderate Fit")
        self.assertEqual(verdict_for(44), "Weak Fit")
        self.assertEqual(verdict_for(30), "Weak Fit")
        self.assertEqual(verdict_for(29), "Poor Fit")

    def test_apply_ranking_persists_step4_fields(self):
        self.seed_portal()
        fixture = self.write_json("scores.json", [self.agent_result("https://portal.example/jobs/1")])
        code, rows, _ = self.cli("apply-ranking", "--from", fixture, "--date", "2026-11-03")
        self.assertEqual(code, 0)
        self.assertEqual(rows, [{
            "key": "https://portal.example/jobs/1", "rank_score": 78, "rank_verdict": "Strong Fit",
            "status": "ranked", "location_verdict": "PASS", "language_gate": "FLAG",
            "deadline": "2026-12-01",
        }])
        entry = self.read_db()["seen"]["https://portal.example/jobs/1"]
        self.assertEqual(entry["rank_date"], "2026-11-03")
        self.assertEqual(entry["language_note"], "posting: fluent Danish; declared: B2")
        self.assertEqual(entry["strengths"], ["Python pipelines", "SQL"])
        self.assertEqual(entry["gaps"], ["No Airflow"])
        self.assertEqual(entry["location"], "Aarhus, Denmark", "place field untouched")
        self.assertNotIn("language", entry)
        self.assertEqual(entry["posting_file"], "postings/" + jobs_db.posting_filename(entry["url"]))

    def test_legacy_location_verdict_is_migrated(self):
        self.seed_portal()
        data = self.read_db()
        data["seen"]["https://portal.example/jobs/2"]["location"] = "PASS"
        self.db.write_text(json.dumps(data, indent=2), encoding="utf-8")
        fixture = self.write_json("scores.json", [
            self.agent_result("https://portal.example/jobs/2", location_verdict="FLAG"),
        ])
        code, rows, _ = self.cli("apply-ranking", "--from", fixture, "--date", "2026-11-03")
        self.assertEqual(code, 0)
        entry = self.read_db()["seen"]["https://portal.example/jobs/2"]
        self.assertEqual(entry["location_verdict"], "FLAG", "fresh agent verdict wins")
        self.assertIsNone(entry["location"], "verdict string cleared from the place field")
        self.assertIn("location", entry, "field kept, never dropped")

    def test_rerank_replaces_strengths_and_keeps_deadline_on_null(self):
        self.seed_portal()
        fixture = self.write_json("scores1.json", [self.agent_result("https://portal.example/jobs/1")])
        self.cli("apply-ranking", "--from", fixture, "--date", "2026-11-03")
        fixture2 = self.write_json("scores2.json", [
            self.agent_result("https://portal.example/jobs/1", strengths=["Fresh bullet"],
                              gaps=["Fresh gap"], deadline=None, language_gate="PASS",
                              language_note=None,
                              scores={"technical": 40, "experience": 40, "behavioral": 40, "career": 40}),
        ])
        code, rows, _ = self.cli("apply-ranking", "--from", fixture2, "--date", "2026-11-10")
        self.assertEqual(code, 0)
        entry = self.read_db()["seen"]["https://portal.example/jobs/1"]
        self.assertEqual(entry["strengths"], ["Fresh bullet"], "replaced, never accumulated")
        self.assertEqual(entry["gaps"], ["Fresh gap"])
        self.assertEqual(entry["deadline"], "2026-12-01", "null from the agent is not a correction")
        self.assertEqual(entry["rank_score"], 40)
        self.assertEqual(entry["rank_verdict"], "Weak Fit")
        self.assertEqual(entry["rank_date"], "2026-11-10")
        self.assertIsNone(entry["language_note"])

    def test_expired_agent_result(self):
        self.seed_portal()
        fixture = self.write_json("scores.json", [{"key": "gamma energy|geophysicist", "status": "expired"}])
        code, rows, _ = self.cli("apply-ranking", "--from", fixture, "--date", "2026-11-03")
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]["status"], "expired")
        self.assertIsNone(rows[0]["rank_score"])
        entry = self.read_db()["seen"]["gamma energy|geophysicist"]
        self.assertEqual(entry["status"], "expired")
        self.assertNotIn("rank_score", entry)

    def test_unknown_key_reported_but_others_persisted(self):
        self.seed_portal()
        fixture = self.write_json("scores.json", [
            self.agent_result("ghost"),
            self.agent_result("https://portal.example/jobs/1"),
        ])
        code, rows, err = self.cli("apply-ranking", "--from", fixture, "--date", "2026-11-03")
        self.assertEqual(code, 1)
        self.assertIn("unknown key", err)
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.read_db()["seen"]["https://portal.example/jobs/1"]["status"], "ranked")

    def test_bad_date_is_a_data_error(self):
        self.seed_portal()
        fixture = self.write_json("scores.json", [])
        code, _, err = self.cli("apply-ranking", "--from", fixture, "--date", "03.11.2026")
        self.assertEqual(code, 1)
        self.assertIn("--date", err)


class ExpireSweepTests(JobsDbCliTestCase):
    def seed_deadlines(self):
        self.seed_portal()
        data = self.read_db()
        seen = data["seen"]
        seen["https://portal.example/jobs/1"]["deadline"] = "2026-10-31"  # past
        seen["https://portal.example/jobs/2"]["deadline"] = "ASAP"  # unparseable
        seen["gamma energy|geophysicist"]["deadline"] = "2026-11-08"  # closing soon
        seen["https://old.example/x"] = {"title": "Old", "company": "Old Co", "url": "https://old.example/x",
                                         "status": "skipped", "deadline": "2020-01-01"}
        seen["https://nodate.example/y"] = {"title": "Undated", "company": "N", "url": "https://nodate.example/y",
                                            "status": "ranked"}
        self.db.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def test_sweep_rules(self):
        self.seed_deadlines()
        code, result, _ = self.cli("expire-sweep", "--today", "2026-11-03")
        self.assertEqual(code, 0)
        self.assertEqual(result["expired"], ["https://portal.example/jobs/1"])
        self.assertEqual(result["closing_soon"], [{
            "key": "gamma energy|geophysicist", "title": "Geophysicist",
            "company": "Gamma Energy", "deadline": "2026-11-08",
        }])
        self.assertEqual(result["unparseable"], [{
            "key": "https://portal.example/jobs/2", "deadline": "ASAP", "portal": "linkedin-search",
        }])
        self.assertEqual(result["counts"], {"swept": 4, "expired": 1, "closing_soon": 1,
                                            "unparseable": 1, "no_deadline": 1})
        seen = self.read_db()["seen"]
        self.assertEqual(seen["https://portal.example/jobs/1"]["status"], "expired")
        self.assertEqual(seen["https://portal.example/jobs/1"]["deadline"], "2026-10-31", "other fields untouched")
        self.assertEqual(seen["https://portal.example/jobs/2"]["status"], "new", "ASAP left alone")
        self.assertEqual(seen["https://portal.example/jobs/2"]["deadline"], "ASAP")
        self.assertEqual(seen["https://nodate.example/y"]["status"], "ranked", "missing deadline untouched")
        self.assertEqual(seen["https://old.example/x"]["status"], "skipped", "only new/ranked are swept")

    def test_within_window_is_configurable(self):
        self.seed_deadlines()
        code, result, _ = self.cli("expire-sweep", "--today", "2026-11-03", "--within", "2")
        self.assertEqual(code, 0)
        self.assertEqual(result["closing_soon"], [])


class PruneTests(JobsDbCliTestCase):
    def seed_old(self):
        self.seed_portal(today="2026-01-10")
        data = self.read_db()
        seen = data["seen"]
        seen["https://portal.example/jobs/1"]["status"] = "expired"
        seen["https://portal.example/jobs/2"]["status"] = "skipped"
        seen["https://portal.example/jobs/2"]["rank_date"] = "2026-10-01"  # recent activity
        seen["gamma energy|geophysicist"]["status"] = "expired"
        seen["gamma energy|geophysicist"]["first_seen"] = "2026-10-20"
        self.db.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def test_dry_run_lists_without_removing(self):
        self.seed_old()
        code, result, _ = self.cli("prune", "--today", "2026-11-03")
        self.assertEqual(code, 0)
        self.assertFalse(result["applied"])
        self.assertEqual([c["key"] for c in result["candidates"]], ["https://portal.example/jobs/1"])
        self.assertEqual(result["candidates"][0]["last_activity"], "2026-01-10")
        self.assertEqual(len(self.read_db()["seen"]), 3)
        self.assertTrue((self.db.parent / "postings" / jobs_db.posting_filename("https://portal.example/jobs/1")).exists())

    def test_apply_removes_entries_and_posting_files(self):
        self.seed_old()
        code, result, _ = self.cli("prune", "--today", "2026-11-03", "--apply")
        self.assertEqual(code, 0)
        self.assertTrue(result["applied"])
        self.assertEqual(result["removed"], ["https://portal.example/jobs/1"])
        self.assertEqual(result["posting_files_removed"], 1)
        seen = self.read_db()["seen"]
        self.assertNotIn("https://portal.example/jobs/1", seen)
        self.assertIn("https://portal.example/jobs/2", seen)
        self.assertFalse((self.db.parent / "postings" / jobs_db.posting_filename("https://portal.example/jobs/1")).exists())

    def test_prune_refuses_new_and_ranked(self):
        self.seed_old()
        code, payload, err = self.cli("prune", "--today", "2026-11-03", "--status", "expired,ranked")
        self.assertEqual(code, 2)
        self.assertIsNone(payload)
        self.assertIn("ranked", err)


class GetAndPathTests(JobsDbCliTestCase):
    def test_get_prints_entry_and_unknown_exits_1(self):
        self.seed_portal()
        code, entry, _ = self.cli("get", "https://portal.example/jobs/2")
        self.assertEqual(code, 0)
        self.assertEqual(entry["title"], "ML Lead")
        self.assertEqual(entry["deadline"], "2026-11-20")
        code, payload, err = self.cli("get", "ghost")
        self.assertEqual(code, 1)
        self.assertIsNone(payload)
        self.assertIn("unknown key", err)

    def test_legacy_path_is_used_with_a_note_and_never_moved(self):
        legacy = self.root / ".claude" / "skills" / "job-scraper" / "job_scraper" / "seen_jobs.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"seen": {
            "https://legacy.example/1": {"title": "Legacy", "company": "L", "status": "new",
                                         "url": "https://legacy.example/1"},
        }}, indent=2), encoding="utf-8")
        code, rows, err = run_cli(["--repo", str(self.root), "list"])
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]["key"], "https://legacy.example/1")
        self.assertIn("legacy", err)
        self.assertIn(str(legacy), err)
        self.assertFalse((self.root / "job_scraper" / "seen_jobs.json").exists())

        fixture = self.write_json("arr.json", PORTAL_OUTPUT["results"][:1])
        code, _, err = run_cli(["--repo", str(self.root), "upsert", "--from", fixture,
                                "--portal", "x", "--source", "cli"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(legacy.read_text(encoding="utf-8"))["seen"]), 2)
        self.assertFalse((self.root / "job_scraper" / "seen_jobs.json").exists(), "never copied")
        self.assertTrue(legacy.parent.joinpath("postings").is_dir(), "posting store sits next to the db")

    def test_default_path_wins_over_legacy_and_is_created_on_first_write(self):
        fixture = self.write_json("arr.json", PORTAL_OUTPUT["results"][:1])
        code, _, err = run_cli(["--repo", str(self.root), "upsert", "--from", fixture,
                                "--portal", "x", "--source", "cli"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertTrue((self.root / "job_scraper" / "seen_jobs.json").exists())

    def test_flat_legacy_shape_is_preserved(self):
        self.db.parent.mkdir(parents=True)
        self.db.write_text(json.dumps({
            "https://flat.example/1": {"title": "Flat", "company": "F", "status": "new",
                                       "url": "https://flat.example/1", "deadline": "2020-01-01"},
        }), encoding="utf-8")
        code, result, _ = self.cli("expire-sweep", "--today", "2026-11-03")
        self.assertEqual(code, 0)
        self.assertEqual(result["expired"], ["https://flat.example/1"])
        data = self.read_db()
        self.assertNotIn("seen", data, "a flat file stays flat")
        self.assertEqual(data["https://flat.example/1"]["status"], "expired")


class RegressionTests(JobsDbCliTestCase):
    """One test per bug found by the adversarial review, each through main([...])."""

    def test_upsert_accepts_bom_prefixed_cli_output(self):
        # PowerShell's `>` redirect writes a UTF-8 BOM in front of the portal CLI's JSON.
        fixture = self.root / "raw_linkedin.json"
        fixture.write_bytes(b"\xef\xbb\xbf" + json.dumps(PORTAL_OUTPUT).encode("utf-8"))
        code, result, err = self.cli("upsert", "--from", str(fixture), "--portal", "x", "--source", "cli")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(result["added"]), 3)

    def test_upsert_stores_the_stripped_url_it_keys_on(self):
        fixture = self.write_json("padded.json", [{"title": "T", "company": "C", "url": "  https://e/2  "}])
        code, result, _ = self.cli("upsert", "--from", fixture, "--portal", "x", "--source", "cli")
        self.assertEqual(code, 0)
        self.assertEqual(result["added"][0]["key"], "https://e/2")
        self.assertEqual(result["added"][0]["url"], "https://e/2", "url field matches the key")
        self.assertEqual(self.read_db()["seen"]["https://e/2"]["url"], "https://e/2")

    def test_output_survives_an_ascii_only_stdout(self):
        # A redirected stdout on Windows is cp1252/ascii; non-ASCII titles must
        # still come out as valid JSON instead of a UnicodeEncodeError traceback.
        fixture = self.write_json("uni.json", [{"title": "Geofysiker \u2013 \u00c5rhus \U0001f30d",
                                                "company": "\u00d8rsted", "url": "https://e/u"}])
        self.cli("upsert", "--from", fixture, "--portal", "x", "--source", "cli")
        out = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--db", str(self.db), "get", "https://e/u"])
        out.flush()
        self.assertEqual(code, 0, err.getvalue())
        payload = json.loads(out.buffer.getvalue().decode("ascii"))
        self.assertEqual(payload["title"], "Geofysiker \u2013 \u00c5rhus \U0001f30d")

    def test_store_posting_refuses_empty_text(self):
        self.seed_portal()
        blank = self.root / "blank.txt"
        blank.write_text("  \n\n", encoding="utf-8")
        code, payload, err = self.cli("store-posting", "https://portal.example/jobs/2", "--from", str(blank))
        self.assertEqual(code, 1)
        self.assertIsNone(payload)
        self.assertIn("empty", err)
        self.assertNotIn("posting_file", self.read_db()["seen"]["https://portal.example/jobs/2"])
        code, got, _ = self.cli("get-posting", "https://portal.example/jobs/2")
        self.assertEqual(got, {"key": "https://portal.example/jobs/2", "missing": True})

    def test_upsert_restores_a_posting_whose_file_is_gone(self):
        self.seed_portal()
        entry = self.read_db()["seen"]["https://portal.example/jobs/1"]
        (self.db.parent / entry["posting_file"]).unlink()
        code, got, _ = self.cli("get-posting", "https://portal.example/jobs/1")
        self.assertEqual(got["missing"], True)
        code, result, _ = self.seed_portal(today="2026-11-09")
        self.assertEqual(code, 0)
        self.assertEqual(result["stored_postings"], 1, "field set but file missing counts as missing")
        code, got, _ = self.cli("get-posting", "https://portal.example/jobs/1")
        self.assertEqual(got["fetched"], "2026-11-09")
        self.assertIn("Build   pipelines", got["text"])

    def test_prune_never_unlinks_outside_the_posting_store(self):
        self.seed_portal(today="2020-01-01")
        victim = self.root / "victim.txt"
        victim.write_text("keep", encoding="utf-8")
        data = self.read_db()
        entry = data["seen"]["https://portal.example/jobs/1"]
        entry["status"] = "expired"
        entry["posting_file"] = "../victim.txt"
        self.db.write_text(json.dumps(data, indent=2), encoding="utf-8")
        code, result, err = self.cli("prune", "--today", "2026-11-03", "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(result["removed"], ["https://portal.example/jobs/1"])
        self.assertEqual(result["posting_files_removed"], 0)
        self.assertTrue(victim.exists(), "a posting_file escaping postings/ is ignored")
        self.assertIn("outside", err)

    def test_apply_ranking_keeps_stored_deadline_on_non_iso_agent_value(self):
        self.seed_portal()
        fixture = self.write_json("scores.json", [{
            "key": "https://portal.example/jobs/2", "status": "scored",
            "scores": {"technical": 50, "experience": 50, "behavioral": 50, "career": 50},
            "location_verdict": "PASS", "language_gate": "PASS", "deadline": "ASAP",
            "strengths": ["x"], "gaps": ["y"],
        }])
        code, rows, err = self.cli("apply-ranking", "--from", fixture, "--date", "2026-11-03")
        self.assertEqual(code, 0, "the score is still persisted")
        self.assertEqual(rows[0]["status"], "ranked")
        self.assertEqual(rows[0]["deadline"], "2026-11-20", "Step 2 schema is YYYY-MM-DD | null")
        self.assertIn("ASAP", err)
        code, result, _ = self.cli("expire-sweep", "--today", "2026-11-03")
        self.assertEqual(result["unparseable"], [], "nothing for the sweep to blame on the portal")

    def test_set_fit_only_toggles_new_or_skipped(self):
        self.seed_portal()
        fixture = self.write_json("fit.json", [
            {"key": "https://portal.example/jobs/1", "fit": "high", "status": "ranked"},
            {"key": "https://portal.example/jobs/2", "fit": "low", "status": "new"},
        ])
        code, result, _ = self.cli("set-fit", "--from", fixture)
        self.assertEqual(code, 1)
        self.assertEqual(result["invalid"], ["https://portal.example/jobs/1"])
        self.assertEqual(result["updated"], 1)
        seen = self.read_db()["seen"]
        self.assertEqual(seen["https://portal.example/jobs/1"]["status"], "new", "ranked is apply-ranking's")
        self.assertIsNone(seen["https://portal.example/jobs/1"]["fit"])
        self.assertEqual(seen["https://portal.example/jobs/2"]["fit"], "low")


class KeyTests(unittest.TestCase):
    def test_make_key_prefers_url_then_company_title(self):
        self.assertEqual(make_key({"url": " https://a.example/1 ", "company": "A", "title": "T"}),
                         "https://a.example/1")
        self.assertEqual(make_key({"url": None, "company": " Acme  ApS", "title": "Data\tEngineer "}),
                         "acme aps|data engineer")
        self.assertIsNone(make_key({"url": "", "company": "", "title": "T"}))


class UpsertSinceTests(JobsDbCliTestCase):
    """`--since` is the client-side recency filter /scrape Step 1b.3 applies for
    a portal with no recency flag: results dated before the window are dropped
    before registration, results with no parseable date are kept (never
    guessed at), and the count of drops is reported."""

    def test_since_drops_old_dated_results_and_keeps_undated_ones(self):
        fixture = self.write_json("portal.json", PORTAL_OUTPUT)
        code, out, _ = self.cli(
            "upsert", "--from", fixture, "--portal", "jobdanmark-search",
            "--source", "cli", "--today", "2026-11-03", "--since", "2026-10-31",
        )
        self.assertEqual(code, 0)
        # 2026-11-01 kept, date None kept, 2026-10-30 dropped
        self.assertEqual(out["dropped_before_since"], 1)
        titles = sorted(a["title"] for a in out["added"])
        self.assertEqual(titles, ["Data Engineer", "ML Lead"])
        self.assertNotIn("gamma energy|geophysicist", {k.lower() for k in self.read_db()["seen"]})

    def test_since_rejects_a_non_iso_value(self):
        fixture = self.write_json("portal.json", PORTAL_OUTPUT)
        code, out, err = self.cli(
            "upsert", "--from", fixture, "--portal", "x", "--source", "cli",
            "--since", "31.10.2026",
        )
        self.assertEqual(code, 1)
        self.assertIn("--since", err)

    def test_without_since_nothing_is_dropped(self):
        code, out, _ = self.seed_portal()
        self.assertEqual(code, 0)
        self.assertEqual(out["dropped_before_since"], 0)


if __name__ == "__main__":
    unittest.main()
