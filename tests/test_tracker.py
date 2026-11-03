"""Tests for tools/tracker.py - the CLI over job_search_tracker.csv.

Every test drives the real code path through `main([...])` against a tracker
written into a tempfile directory; nothing touches the repo's own tracker and
nothing hits the network.
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools import tracker
from tools.tracker import main

HEADER = (
    "date,company,sector,role,role_type,channel,status,contact_person,"
    "fit_rating,notes,cv_file,cover_letter_file,source,deadline"
)
LEGACY_HEADER = HEADER[: -len(",deadline")]

LEGACY_ROWS = (
    "2026-09-01,Acme Corp,fintech,ML Engineer,full-time,portal,applied,Jane Doe,82,"
    '"2026-09-01 applied via portal; 2026-09-15 followed up 2026-09-15",'
    "cv/main_acme_ml.pdf,cover_letters/cover_acme_ml.pdf,https://example.test/acme\n"
    "2026-08-10,Globex,energy,Data Scientist,,online,no response,,70,,,,\n"
    '2026-10-20,"Initech, Inc.",software,Backend Dev,,portal,drafted,,65,,,,\n'
)


def run(argv):
    """Run main() capturing stdout/stderr; return (exit_code, parsed_json_or_None, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    text = out.getvalue().strip()
    payload = json.loads(text) if text else None
    return code, payload, err.getvalue()


class TrackerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "job_search_tracker.csv"

    def write(self, text):
        self.path.write_text(text, encoding="utf-8", newline="")

    def read_bytes(self):
        return self.path.read_bytes()

    def cli(self, *argv):
        return run(["--tracker", str(self.path), *argv])


class VocabularyConstants(unittest.TestCase):
    """Pinned to the literal lists in /outcome's vocabulary block and /apply's header."""

    def test_header_matches_apply_step_6b(self):
        self.assertEqual(",".join(tracker.HEADER), HEADER)
        self.assertEqual(tracker.HEADER[-1], "deadline")

    def test_statuses_match_outcome_vocabulary(self):
        self.assertEqual(
            tracker.STATUSES,
            ["ready", "drafted", "applied", "interview", "offer", "hired", "rejected",
             "no_response", "offer_declined", "withdrawn"],
        )
        self.assertEqual(
            tracker.FINAL_STATUSES,
            ["hired", "rejected", "no_response", "offer_declined", "withdrawn"],
        )
        self.assertEqual(tracker.OPEN_STATUSES, ["ready", "drafted", "applied", "interview", "offer"])
        self.assertEqual(tracker.NOT_SENT_STATUSES, ("ready", "drafted"))
        self.assertEqual(
            tracker.LEGACY_ALIASES,
            {"no response": "no_response", "offer declined": "offer_declined"},
        )

    def test_status_order_ready_to_terminal(self):
        order = tracker.STATUS_ORDER
        self.assertLess(order["ready"], order["drafted"])
        self.assertLess(order["drafted"], order["applied"])
        self.assertLess(order["applied"], order["interview"])
        self.assertLess(order["interview"], order["offer"])
        for final in tracker.FINAL_STATUSES:
            self.assertGreater(order[final], order["offer"])


class InitAndMigrate(TrackerTestCase):
    def test_init_creates_header_once(self):
        code, payload, _ = self.cli("init")
        self.assertEqual(code, 0)
        self.assertEqual(payload, {"created": True})
        self.assertEqual(self.read_bytes(), (HEADER + "\n").encode("utf-8"))
        code, payload, _ = self.cli("init")
        self.assertEqual(code, 0)
        self.assertEqual(payload, {"created": False})
        self.assertEqual(self.read_bytes(), (HEADER + "\n").encode("utf-8"))

    def test_migrate_appends_deadline_to_header_only(self):
        self.write(LEGACY_HEADER + "\n" + LEGACY_ROWS)
        before = self.read_bytes()
        code, payload, _ = self.cli("migrate")
        self.assertEqual(code, 0)
        self.assertEqual(payload, {"migrated": True, "columns": 14})
        after = self.read_bytes()
        header_before, body_before = before.split(b"\n", 1)
        header_after, body_after = after.split(b"\n", 1)
        self.assertEqual(header_after, HEADER.encode("utf-8"))
        self.assertEqual(header_before + b",deadline", header_after)
        self.assertEqual(body_after, body_before, "data rows must be byte-identical")
        # Second run is a no-op.
        code, payload, _ = self.cli("migrate")
        self.assertEqual(payload, {"migrated": False, "columns": 14})
        self.assertEqual(self.read_bytes(), after)

    def test_migrate_missing_file_exits_1(self):
        code, payload, err = self.cli("migrate")
        self.assertEqual(code, 1)
        self.assertIsNone(payload)
        self.assertIn("not found", err)

    def test_legacy_rows_read_with_empty_deadline(self):
        self.write(LEGACY_HEADER + "\n" + LEGACY_ROWS)
        code, rows, _ = self.cli("list")
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["deadline"] for r in rows], ["", "", ""])
        self.assertEqual([r["row"] for r in rows], [1, 2, 3])

    def test_init_writes_header_into_empty_file(self):
        """A 0-byte tracker is as good as absent: init must make it usable."""
        self.path.write_bytes(b"")
        code, payload, _ = self.cli("init")
        self.assertEqual(code, 0)
        self.assertEqual(payload, {"created": True})
        self.assertEqual(self.read_bytes(), (HEADER + "\n").encode("utf-8"))
        self.assertEqual(self.cli("list"), (0, [], ""))

    def test_bom_header_is_read_and_preserved(self):
        """A spreadsheet 'save as CSV' prefixes a UTF-8 BOM; the header still parses."""
        self.path.write_bytes(b"\xef\xbb\xbf" + (LEGACY_HEADER + "\n" + LEGACY_ROWS).encode("utf-8"))
        code, rows, err = self.cli("list")
        self.assertEqual(code, 0, err)
        self.assertEqual([r["date"] for r in rows], ["2026-09-01", "2026-08-10", "2026-10-20"])
        code, payload, _ = self.cli("migrate")
        self.assertEqual(payload, {"migrated": True, "columns": 14})
        self.assertTrue(self.read_bytes().startswith(b"\xef\xbb\xbf" + HEADER.encode("utf-8") + b"\n"))


class Find(TrackerTestCase):
    def setUp(self):
        super().setUp()
        self.write(HEADER + "\n" + LEGACY_ROWS)

    def test_find_is_case_insensitive_and_whitespace_collapsed(self):
        code, rows, _ = self.cli("find", "--company", "  acme   CORP ")
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["company"], "Acme Corp")
        self.assertEqual(rows[0]["row"], 1)
        self.assertTrue(rows[0]["is_open"])
        self.assertFalse(rows[0]["is_final"])

    def test_find_with_role(self):
        code, rows, _ = self.cli("find", "--company", "acme corp", "--role", "ml  engineer")
        self.assertEqual(len(rows), 1)
        code, rows, _ = self.cli("find", "--company", "acme corp", "--role", "designer")
        self.assertEqual(rows, [])

    def test_legacy_no_response_is_final(self):
        code, rows, _ = self.cli("find", "--company", "globex")
        self.assertEqual(rows[0]["status"], "no response")
        self.assertTrue(rows[0]["is_final"])
        self.assertFalse(rows[0]["is_open"])
        code, rows, _ = self.cli("find", "--company", "globex", "--open")
        self.assertEqual(rows, [])
        code, rows, _ = self.cli("find", "--company", "globex", "--status", "no_response")
        self.assertEqual(len(rows), 1, "canonical --status must match the legacy spelling on read")

    def test_quoted_company_matches(self):
        code, rows, _ = self.cli("find", "--company", "initech, inc.")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["company"], "Initech, Inc.")

    def test_find_rejects_empty_company(self):
        """An empty match key must not silently match every row (apply 6b would
        then 'update' the first open row instead of appending)."""
        for value in ("", "   "):
            code, payload, err = self.cli("find", "--company", value)
            self.assertEqual(code, 1, value)
            self.assertIsNone(payload)
            self.assertIn("--company", err)


class Add(TrackerTestCase):
    def setUp(self):
        super().setUp()
        self.write(HEADER + "\n" + LEGACY_ROWS)

    def test_add_appends_row_and_preserves_others(self):
        before = self.read_bytes()
        code, payload, _ = self.cli(
            "add", "--date", "2026-11-03", "--company", "Hooli", "--role", "Platform Engineer",
            "--channel", "portal", "--fit-rating", "77", "--deadline", "2026-11-20",
            "--cv-file", "cv/main_hooli_platform.tex", "--source", "https://example.test/hooli",
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["row"], 4)
        self.assertEqual(payload["status"], "drafted")
        self.assertEqual(payload["fit_rating"], "77")
        self.assertEqual(payload["deadline"], "2026-11-20")
        self.assertTrue(payload["is_open"])
        after = self.read_bytes()
        self.assertTrue(after.startswith(before))
        new_line = after[len(before):]
        self.assertEqual(
            new_line,
            b"2026-11-03,Hooli,,Platform Engineer,,portal,drafted,,77,,cv/main_hooli_platform.tex,,"
            b"https://example.test/hooli,2026-11-20\n",
        )

    def test_add_to_legacy_header_migrates_header_first(self):
        self.write(LEGACY_HEADER + "\n" + LEGACY_ROWS)
        code, payload, _ = self.cli("add", "--date", "2026-11-03", "--company", "Hooli", "--role", "Dev")
        self.assertEqual(code, 0)
        self.assertTrue(payload.get("migrated"))
        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(HEADER + "\n"))
        self.assertIn(LEGACY_ROWS, text)
        self.assertEqual(text.count(","), (HEADER + "\n" + LEGACY_ROWS).count(",") + 13)

    def test_add_with_status_ready(self):
        """/quick-apply writes the same row as /apply 6b with --status ready."""
        code, payload, err = self.cli(
            "add", "--date", "2026-11-03", "--company", "Hooli", "--role", "Platform Engineer",
            "--status", "ready", "--role-type", "ml_engineer",
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["status"], "ready")
        self.assertTrue(payload["is_open"])
        self.assertFalse(payload["is_final"])
        code, rows, _ = self.cli("list", "--status", "ready")
        self.assertEqual([r["company"] for r in rows], ["Hooli"])
        code, rows, _ = self.cli("list", "--open")
        self.assertIn("Hooli", [r["company"] for r in rows])

    def test_add_default_status_stays_drafted(self):
        code, payload, _ = self.cli("add", "--date", "2026-11-03", "--company", "X", "--role", "Y")
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "drafted")

    def test_add_rejects_bad_status(self):
        code, payload, err = self.cli("add", "--date", "2026-11-03", "--company", "X", "--role", "Y", "--status", "pending")
        self.assertEqual(code, 1)
        self.assertIsNone(payload)
        self.assertIn("status", err)

    def test_add_rejects_legacy_spelling(self):
        code, _, err = self.cli("add", "--date", "2026-11-03", "--company", "X", "--role", "Y", "--status", "no response")
        self.assertEqual(code, 1)
        self.assertIn("legacy", err)
        self.assertIn("no_response", err)

    def test_add_rejects_fit_rating_out_of_range(self):
        code, _, err = self.cli("add", "--date", "2026-11-03", "--company", "X", "--role", "Y", "--fit-rating", "101")
        self.assertEqual(code, 1)
        self.assertIn("fit_rating", err)
        code, _, err = self.cli("add", "--date", "2026-11-03", "--company", "X", "--role", "Y", "--fit-rating", "82/100")
        self.assertEqual(code, 1)

    def test_add_rejects_bad_deadline_and_date(self):
        code, _, err = self.cli("add", "--date", "2026-11-03", "--company", "X", "--role", "Y", "--deadline", "20/11/2026")
        self.assertEqual(code, 1)
        self.assertIn("deadline", err)
        code, _, err = self.cli("add", "--date", "03-11-2026", "--company", "X", "--role", "Y")
        self.assertEqual(code, 1)
        self.assertIn("date", err)

    def test_validation_failure_leaves_file_untouched(self):
        before = self.read_bytes()
        self.cli("add", "--date", "2026-11-03", "--company", "X", "--role", "Y", "--fit-rating", "101")
        self.assertEqual(self.read_bytes(), before)

    def test_missing_required_flag_is_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stderr(io.StringIO()):
                main(["--tracker", str(self.path), "add", "--company", "X"])
        self.assertEqual(ctx.exception.code, 2)


class Update(TrackerTestCase):
    def setUp(self):
        super().setUp()
        self.write(HEADER + "\n" + LEGACY_ROWS)

    def test_status_forward_and_note_and_date(self):
        code, payload, _ = self.cli(
            "update", "1", "--set", "status=interview", "--note", "2026-11-03 phone screen booked",
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "interview")
        self.assertTrue(payload["notes"].endswith("; 2026-11-03 phone screen booked"))
        self.assertEqual(payload["fit_rating"], "82", "other fields preserved")
        self.assertEqual(payload["source"], "https://example.test/acme")
        self.assertNotIn("date_unchanged_warning", payload)
        # Untouched rows byte-identical.
        lines = self.read_bytes().split(b"\n")
        original = (HEADER + "\n" + LEGACY_ROWS).encode("utf-8").split(b"\n")
        self.assertEqual(lines[0], original[0])
        self.assertEqual(lines[2:], original[2:])

    def test_backwards_rejected_without_force(self):
        before = self.read_bytes()
        code, payload, err = self.cli("update", "1", "--set", "status=drafted")
        self.assertEqual(code, 1)
        self.assertIsNone(payload)
        self.assertIn("backwards", err)
        self.assertEqual(self.read_bytes(), before)

    def test_backwards_allowed_with_force(self):
        code, payload, _ = self.cli("update", "1", "--set", "status=drafted", "--force")
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "drafted")

    def test_leaving_final_requires_force(self):
        code, _, err = self.cli("update", "2", "--set", "status=interview")
        self.assertEqual(code, 1)
        self.assertIn("final", err)
        code, payload, _ = self.cli("update", "2", "--set", "status=interview", "--force")
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "interview")

    def test_leaving_drafted_without_date_warns(self):
        code, payload, _ = self.cli("update", "3", "--set", "status=applied")
        self.assertEqual(code, 0)
        self.assertEqual(payload["date"], "2026-10-20")
        self.assertIn("date_unchanged_warning", payload)
        code, payload, _ = self.cli("update", "3", "--set", "status=interview", "--set", "date=2026-11-01")
        self.assertEqual(code, 0)
        self.assertEqual(payload["date"], "2026-11-01")
        self.assertNotIn("date_unchanged_warning", payload)

    def test_ready_to_drafted_is_forward_and_keeps_date_without_warning(self):
        """/quick-apply approve: both statuses are unsent, so `date` still means
        'drafted on' and no submission-date warning fires."""
        self.write(HEADER + "\n" + "2026-10-20,Hooli,,Dev,,portal,ready,,,,,,,\n")
        code, payload, err = self.cli(
            "update", "1", "--set", "status=drafted", "--note", "2026-11-03 approved via /quick-apply",
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["status"], "drafted")
        self.assertEqual(payload["date"], "2026-10-20")
        self.assertNotIn("date_unchanged_warning", payload)

    def test_ready_to_applied_without_date_warns(self):
        self.write(HEADER + "\n" + "2026-10-20,Hooli,,Dev,,portal,ready,,,,,,,\n")
        code, payload, _ = self.cli("update", "1", "--set", "status=applied")
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "applied")
        self.assertIn("date_unchanged_warning", payload)
        self.assertIn("ready", payload["date_unchanged_warning"])
        self.write(HEADER + "\n" + "2026-10-20,Hooli,,Dev,,portal,ready,,,,,,,\n")
        code, payload, _ = self.cli("update", "1", "--set", "status=applied", "--set", "date=2026-11-03")
        self.assertEqual(code, 0)
        self.assertEqual(payload["date"], "2026-11-03")
        self.assertNotIn("date_unchanged_warning", payload)

    def test_drafted_to_ready_is_backwards(self):
        before = self.read_bytes()
        code, payload, err = self.cli("update", "3", "--set", "status=ready")
        self.assertEqual(code, 1)
        self.assertIsNone(payload)
        self.assertIn("backwards", err)
        self.assertEqual(self.read_bytes(), before)
        code, payload, _ = self.cli("update", "3", "--set", "status=ready", "--force")
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "ready")

    def test_legacy_spelling_rejected_on_write(self):
        code, _, err = self.cli("update", "1", "--set", "status=offer declined", "--force")
        self.assertEqual(code, 1)
        self.assertIn("legacy", err)

    def test_set_rejects_empty_company_or_role(self):
        before = self.read_bytes()
        for item in ("company=", "role=  "):
            code, payload, err = self.cli("update", "1", "--set", item)
            self.assertEqual(code, 1, item)
            self.assertIsNone(payload)
            self.assertIn("must not be empty", err)
        self.assertEqual(self.read_bytes(), before)
        code, _, err = self.cli("add", "--date", "2026-11-03", "--company", " ", "--role", "Y")
        self.assertEqual(code, 1)
        self.assertIn("company", err)

    def test_set_validates_like_add(self):
        self.assertEqual(self.cli("update", "1", "--set", "fit_rating=101")[0], 1)
        self.assertEqual(self.cli("update", "1", "--set", "deadline=soon")[0], 1)
        self.assertEqual(self.cli("update", "1", "--set", "nonexistent=1")[0], 1)
        self.assertEqual(self.cli("update", "1", "--set", "novalue")[0], 1)
        self.assertEqual(self.cli("update", "9", "--set", "fit_rating=50")[0], 1)

    def test_marker_redrafted_is_idempotent(self):
        code, payload, _ = self.cli("update", "3", "--marker", "redrafted")
        self.assertEqual(code, 0)
        self.assertEqual(payload["notes"], "redrafted")
        code, payload, _ = self.cli("update", "3", "--marker", "redrafted", "--set", "fit_rating=70")
        self.assertEqual(payload["notes"], "redrafted")
        self.assertEqual(payload["fit_rating"], "70")
        code, payload, _ = self.cli("update", "3", "--note", "2026-11-05 sent")
        self.assertEqual(payload["notes"], "redrafted; 2026-11-05 sent")

    def test_note_requires_date(self):
        code, _, err = self.cli("update", "1", "--note", "called them")
        self.assertEqual(code, 1)
        self.assertIn("YYYY-MM-DD", err)
        code, payload, _ = self.cli("update", "1", "--note", "followed up 2026-11-03")
        self.assertEqual(code, 0)
        self.assertTrue(payload["notes"].endswith("; followed up 2026-11-03"))

    def test_gmail_sync_note_format_round_trips(self):
        note = '2026-11-03 gmail-sync: rejection ("Your application to Acme")'
        code, payload, _ = self.cli("update", "1", "--set", "status=rejected", "--note", note)
        self.assertEqual(code, 0)
        self.assertTrue(payload["notes"].endswith(note))
        code, rows, _ = self.cli("find", "--company", "acme corp")
        self.assertTrue(rows[0]["notes"].endswith(note))
        self.assertTrue(rows[0]["is_final"])

    def test_editing_legacy_width_row_gains_deadline_column(self):
        self.write(LEGACY_HEADER + "\n" + LEGACY_ROWS)
        code, payload, _ = self.cli("update", "2", "--set", "deadline=2026-12-01", "--force")
        self.assertEqual(code, 0)
        self.assertTrue(payload.get("migrated"))
        lines = self.path.read_text(encoding="utf-8").split("\n")
        self.assertEqual(lines[0], HEADER)
        self.assertEqual(lines[2], "2026-08-10,Globex,energy,Data Scientist,,online,no response,,70,,,,,2026-12-01")
        self.assertEqual(lines[1].count(","), 12, "untouched legacy row keeps its 13-column width")
        self.assertEqual(lines[3].count(","), 13, "untouched quoted row keeps its original bytes")


class ListRows(TrackerTestCase):
    def setUp(self):
        super().setUp()
        self.write(HEADER + "\n" + LEGACY_ROWS)

    def test_list_filters(self):
        code, rows, _ = self.cli("list")
        self.assertEqual([r["row"] for r in rows], [1, 2, 3])
        code, rows, _ = self.cli("list", "--open")
        self.assertEqual([r["company"] for r in rows], ["Acme Corp", "Initech, Inc."])
        code, rows, _ = self.cli("list", "--final")
        self.assertEqual([r["company"] for r in rows], ["Globex"])
        code, rows, _ = self.cli("list", "--status", "drafted")
        self.assertEqual([r["company"] for r in rows], ["Initech, Inc."])


class DateMath(TrackerTestCase):
    ROWS = (
        "2026-09-01,Quiet Co,,Analyst,,email,applied,,,,,,,\n"                      # 63 days quiet
        "2026-10-25,Recent Co,,Analyst,,email,applied,,,,,,,\n"                     # 9 days quiet
        "2026-09-01,Noted Co,,Analyst,,email,interview,,,\"2026-10-20 onsite\",,,,\n"  # 14 days quiet via note
        "2026-09-01,Chased Co,,Analyst,,email,applied,,,\"followed up 2026-09-15; followed up 2026-10-01\",,,,\n"
        "2026-09-01,Once Co,,Analyst,,email,applied,Bob,,\"followed up 2026-10-10\",,,,\n"  # 24 days quiet, 1 sent
        "2026-09-01,Draft Co,,Analyst,,email,drafted,,,,,,,2026-10-30\n"              # drafted, deadline passed
        "2026-09-01,Ready Co,,Analyst,,email,ready,,,,,,,2026-11-01\n"                # ready, deadline passed
        "2026-09-01,Closed Co,,Analyst,,email,no response,,,,,,,2026-10-30\n"         # final
        "2026-10-30,Soon Co,,Analyst,,email,applied,,,,,,,2026-11-08\n"              # due in 5 days
        "2026-10-30,Later Co,,Analyst,,email,applied,,,,,,,2026-11-30\n"             # due in 27 days
        "2026-10-30,Today Co,,Analyst,,email,offer,,,,,,,2026-11-03\n"               # due today
        "unknown,Bad Date Co,,Analyst,,email,applied,,,,,,,\n"                       # unparseable
    )

    def setUp(self):
        super().setUp()
        self.write(HEADER + "\n" + self.ROWS)

    def test_stale_30_days(self):
        code, rows, err = self.cli("stale", "--days", "30", "--today", "2026-11-03")
        self.assertEqual(code, 0)
        self.assertEqual(
            [(r["company"], r["days_quiet"], r["last_activity"]) for r in rows],
            [("Quiet Co", 63, "2026-09-01"), ("Chased Co", 33, "2026-10-01")],
        )
        self.assertIn("Bad Date Co", err)
        self.assertNotIn("Ready Co", [r["company"] for r in rows], "ready rows are unsent: never stale")
        for row in rows:
            self.assertEqual(set(row), {"row", "company", "role", "status", "days_quiet", "last_activity"})

    def test_stale_threshold_is_inclusive(self):
        code, rows, _ = self.cli("stale", "--days", "14", "--today", "2026-11-03")
        self.assertIn("Noted Co", [r["company"] for r in rows])
        code, rows, _ = self.cli("stale", "--days", "15", "--today", "2026-11-03")
        self.assertNotIn("Noted Co", [r["company"] for r in rows])

    def test_deadlines(self):
        code, payload, _ = self.cli("deadlines", "--within", "7", "--today", "2026-11-03")
        self.assertEqual(code, 0)
        self.assertEqual(
            [(r["company"], r["days_left"]) for r in payload["passed"]],
            [("Draft Co", -4), ("Ready Co", -2)],
        )
        self.assertEqual(
            [(r["company"], r["days_left"]) for r in payload["due_soon"]],
            [("Soon Co", 5), ("Today Co", 0)],
        )
        self.assertEqual(
            [r["company"] for r in payload["drafted_past_deadline"]],
            ["Draft Co", "Ready Co"],
            "a past-deadline ready row is the same failure as a past-deadline drafted one",
        )
        self.assertEqual(payload["passed"][1]["status"], "ready")
        self.assertEqual(payload["passed"][0]["status"], "drafted")
        self.assertEqual(payload["passed"][0]["deadline"], "2026-10-30")
        self.assertNotIn("Closed Co", json.dumps(payload), "final rows are excluded")
        self.assertNotIn("Later Co", json.dumps(payload))

    def test_followups_default_rule(self):
        code, rows, err = self.cli("followups", "--today", "2026-11-03")
        self.assertEqual(code, 0)
        self.assertEqual(
            [(r["company"], r["days_quiet"], r["followups_sent"]) for r in rows],
            [("Quiet Co", 63, 0), ("Noted Co", 14, 0), ("Once Co", 24, 1)],
        )
        self.assertEqual(rows[2]["contact_person"], "Bob")
        self.assertEqual(rows[2]["channel"], "email")
        self.assertIn("Bad Date Co", err)
        companies = [r["company"] for r in rows]
        self.assertNotIn("Chased Co", companies, "two follow-ups sent: at max")
        self.assertNotIn("Draft Co", companies, "drafted rows are never chased")
        self.assertNotIn("Ready Co", companies, "ready rows are never chased either")
        self.assertNotIn("Recent Co", companies, "9 days is under the 10-day threshold")

    def test_followups_marker_count_is_case_and_space_tolerant(self):
        """outcome.md writes `followed up YYYY-MM-DD`; a hand-edited `Followed up`
        or a double space is the same marker and must still count toward --max."""
        self.write(
            HEADER + "\n"
            '2026-09-01,Hand Co,,Analyst,,email,applied,,,"Followed up 2026-10-01; followed  up 2026-10-05",,,,\n'
        )
        code, rows, _ = self.cli("followups", "--today", "2026-11-03")
        self.assertEqual(code, 0)
        self.assertEqual(rows, [], "two markers sent: at max, never a candidate")
        code, rows, _ = self.cli("followups", "--today", "2026-11-03", "--max", "3")
        self.assertEqual([(r["followups_sent"], r["last_activity"]) for r in rows], [(2, "2026-10-05")])

    def test_followups_custom_threshold_and_max(self):
        code, rows, _ = self.cli("followups", "--today", "2026-11-03", "--quiet-days", "5", "--max", "3")
        companies = [r["company"] for r in rows]
        self.assertIn("Recent Co", companies)
        self.assertIn("Chased Co", companies)

    def test_bad_today_is_data_error(self):
        code, payload, err = self.cli("stale", "--today", "yesterday")
        self.assertEqual(code, 1)
        self.assertIn("--today", err)


if __name__ == "__main__":
    unittest.main()
