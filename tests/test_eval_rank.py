import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools.eval_rank import (
    EvalError,
    compare_runs,
    compute_overall,
    extract_job_list,
    format_table,
    main,
    normalize_job,
    normalize_run,
    verdict_band,
)


def job(key, technical, experience, behavioral, career, **overrides):
    """One scoring-agent object in the rank.md Step 2 schema."""
    entry = {
        "key": key,
        "status": "scored",
        "scores": {
            "technical": technical,
            "experience": experience,
            "behavioral": behavioral,
            "career": career,
        },
        "location_verdict": "PASS",
        "language_gate": "PASS",
        "language_note": None,
        "deadline": None,
        "strengths": ["posting asks for the candidate's primary stack"],
        "gaps": ["wants three more years of seniority"],
        "language": "English",
    }
    entry.update(overrides)
    return entry


def expired(key):
    return {"key": key, "status": "expired"}


def baseline_run():
    return [
        job("acme-eng-1", 80, 70, 60, 85),        # overall 76.0 -> Strong Fit
        job("beta-data-2", 60, 60, 60, 60),       # overall 60.0 -> Good Fit
        job("gamma-ops-3", 40, 50, 30, 45, location_verdict="FLAG"),  # 42.0 -> Weak Fit
        job("delta-abroad-4", 90, 90, 90, 90, location_verdict="FAIL"),
        expired("omega-dead-5"),
    ]


def shift(run, key, **new_scores):
    """Copy a run and change one job's dimension scores."""
    out = json.loads(json.dumps(run))
    for entry in out:
        if entry["key"] == key:
            entry["scores"].update(new_scores)
    return out


def set_field(run, key, field, value):
    out = json.loads(json.dumps(run))
    for entry in out:
        if entry["key"] == key:
            entry[field] = value
    return out


def write_json(directory, name, data):
    path = Path(directory) / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def run_main(argv):
    """Run the CLI through main([...]) capturing stdout/stderr."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class ScoringArithmeticTests(unittest.TestCase):
    def test_overall_uses_rank_md_weights(self):
        # 30/25/15/30: 80*.3 + 70*.25 + 60*.15 + 85*.3 = 24 + 17.5 + 9 + 25.5
        self.assertEqual(compute_overall({"technical": 80, "experience": 70, "behavioral": 60, "career": 85}), 76.0)
        self.assertEqual(compute_overall({"technical": 100, "experience": 100, "behavioral": 100, "career": 100}), 100.0)
        self.assertEqual(compute_overall({"technical": 0, "experience": 0, "behavioral": 0, "career": 0}), 0.0)

    def test_verdict_bands_match_rank_md(self):
        self.assertEqual(verdict_band(75), "Strong Fit")
        self.assertEqual(verdict_band(74.9), "Good Fit")
        self.assertEqual(verdict_band(60), "Good Fit")
        self.assertEqual(verdict_band(59.9), "Moderate Fit")
        self.assertEqual(verdict_band(45), "Moderate Fit")
        self.assertEqual(verdict_band(30), "Weak Fit")
        self.assertEqual(verdict_band(29.9), "Poor Fit")

    def test_normalize_job_keeps_comparable_fields(self):
        normalized = normalize_job(baseline_run()[0])
        self.assertEqual(normalized["key"], "acme-eng-1")
        self.assertEqual(normalized["overall"], 76.0)
        self.assertEqual(normalized["verdict"], "Strong Fit")
        self.assertEqual(normalized["location_verdict"], "PASS")
        self.assertEqual(normalized["language_gate"], "PASS")
        self.assertEqual(len(normalized["strengths"]), 1)
        self.assertEqual(len(normalized["gaps"]), 1)
        self.assertFalse(normalized["expired"])
        self.assertEqual(normalized["incomplete"], [])

    def test_missing_dimension_counts_as_zero_and_is_recorded(self):
        raw = job("x", 80, 80, 80, 80)
        del raw["scores"]["career"]
        normalized = normalize_job(raw)
        self.assertEqual(normalized["dimensions"]["career"], 0.0)
        self.assertEqual(normalized["incomplete"], ["career"])
        self.assertEqual(normalized["overall"], round(80 * 0.7, 1))

    def test_expired_job_is_not_flagged_incomplete(self):
        normalized = normalize_job(expired("dead"))
        self.assertTrue(normalized["expired"])
        self.assertEqual(normalized["incomplete"], [])
        self.assertEqual(normalized["overall"], 0.0)

    def test_job_without_key_is_a_data_error(self):
        with self.assertRaises(EvalError):
            normalize_job({"status": "scored", "scores": {}})

    def test_bool_string_and_non_finite_dimensions(self):
        # Regression: `true` used to become 1.0 and "nan" a NaN overall that printed invalid JSON.
        raw = job("x", True, "70", "nan", "inf")
        normalized = normalize_job(raw)
        self.assertEqual(normalized["incomplete"], ["technical", "behavioral", "career"])
        self.assertEqual(normalized["dimensions"]["experience"], 70.0)
        self.assertEqual(normalized["overall"], 17.5)


class InputShapeTests(unittest.TestCase):
    def test_array_and_jobs_object_are_equivalent(self):
        run = baseline_run()
        self.assertEqual(extract_job_list(run), run)
        self.assertEqual(extract_job_list({"jobs": run}), run)
        with self.assertRaises(EvalError):
            extract_job_list({"scores": run})
        with self.assertRaises(EvalError):
            extract_job_list("nope")

    def test_object_vs_array_input_via_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            golden_src = write_json(tmp, "golden_src.json", baseline_run())
            golden = Path(tmp) / "golden.json"
            self.assertEqual(run_main(["save-golden", "--scores", str(golden_src), "--out", str(golden)])[0], 0)
            as_object = write_json(tmp, "run_obj.json", {"jobs": baseline_run()})
            code, out, _ = run_main(["compare", "--scores", str(as_object), "--golden", str(golden)])
            self.assertEqual(code, 0)
            result = json.loads(out)
            self.assertEqual(result["summary"]["compared"], 5)
            self.assertTrue(result["summary"]["pass"])


class CompareTests(unittest.TestCase):
    def test_identical_runs_pass_with_zero_deltas(self):
        scores = normalize_run(baseline_run())
        golden = normalize_run(baseline_run())
        result = compare_runs(scores, golden, max_delta=5)
        summary = result["summary"]
        self.assertTrue(summary["pass"])
        self.assertEqual(summary["compared"], 5)
        self.assertEqual(summary["max_abs_delta"], 0.0)
        self.assertEqual(summary["mean_abs_delta"], 0.0)
        self.assertEqual(summary["band_flips"], 0)
        self.assertEqual(summary["veto_flips"], 0)
        self.assertEqual(summary["only_in_scores"], [])
        self.assertEqual(summary["only_in_golden"], [])
        self.assertTrue(all(j["delta"] == 0.0 for j in result["jobs"] if j["delta"] is not None))

    def test_seven_point_drift_fails_default_threshold(self):
        # technical +10 and career +10 moves overall by 3 + 3 = +6 -> >5, fail.
        # Make it a clean 7: technical +10 (3.0), experience +8 (2.0), career +6.67 (~2.0)
        drifted = shift(baseline_run(), "beta-data-2", technical=70, experience=68, behavioral=60, career=66.67)
        scores = normalize_run(drifted)
        golden = normalize_run(baseline_run())
        result = compare_runs(scores, golden, max_delta=5)
        beta = next(j for j in result["jobs"] if j["key"] == "beta-data-2")
        self.assertEqual(beta["delta"], 7.0)
        self.assertFalse(result["summary"]["pass"])
        self.assertEqual(result["summary"]["max_abs_delta"], 7.0)
        # Same drift passes with a looser threshold.
        self.assertTrue(compare_runs(scores, golden, max_delta=10)["summary"]["pass"])

    def test_band_flip_is_reported_but_not_a_failure_alone(self):
        # 60.0 (Good) -> 59.1 (Moderate): delta -0.9, band flips.
        flipped = shift(baseline_run(), "beta-data-2", technical=57)
        result = compare_runs(normalize_run(flipped), normalize_run(baseline_run()), max_delta=5)
        beta = next(j for j in result["jobs"] if j["key"] == "beta-data-2")
        self.assertTrue(beta["band_flip"])
        self.assertEqual(result["summary"]["band_flips"], 1)
        self.assertTrue(result["summary"]["pass"])

    def test_location_fail_to_pass_veto_flip_fails_even_with_small_deltas(self):
        flipped = set_field(baseline_run(), "delta-abroad-4", "location_verdict", "PASS")
        result = compare_runs(normalize_run(flipped), normalize_run(baseline_run()), max_delta=5)
        delta_job = next(j for j in result["jobs"] if j["key"] == "delta-abroad-4")
        self.assertEqual(delta_job["delta"], 0.0)
        self.assertTrue(delta_job["location_flip"])
        self.assertTrue(delta_job["veto_flip"])
        self.assertEqual(result["summary"]["veto_flips"], 1)
        self.assertEqual(result["summary"]["max_abs_delta"], 0.0)
        self.assertFalse(result["summary"]["pass"])

    def test_location_pass_to_flag_is_not_a_veto_flip(self):
        changed = set_field(baseline_run(), "acme-eng-1", "location_verdict", "FLAG")
        result = compare_runs(normalize_run(changed), normalize_run(baseline_run()), max_delta=5)
        acme = next(j for j in result["jobs"] if j["key"] == "acme-eng-1")
        self.assertFalse(acme["location_flip"])
        self.assertTrue(result["summary"]["pass"])

    def test_any_language_gate_change_is_a_veto_flip(self):
        changed = set_field(baseline_run(), "acme-eng-1", "language_gate", "FLAG")
        result = compare_runs(normalize_run(changed), normalize_run(baseline_run()), max_delta=5)
        acme = next(j for j in result["jobs"] if j["key"] == "acme-eng-1")
        self.assertTrue(acme["language_flip"])
        self.assertFalse(result["summary"]["pass"])

    def test_expired_mismatch_is_reported_without_a_delta(self):
        revived = [j for j in baseline_run() if j["key"] != "omega-dead-5"] + [job("omega-dead-5", 50, 50, 50, 50)]
        result = compare_runs(normalize_run(revived), normalize_run(baseline_run()), max_delta=5)
        omega = next(j for j in result["jobs"] if j["key"] == "omega-dead-5")
        self.assertTrue(omega["expired_mismatch"])
        self.assertIsNone(omega["delta"])
        self.assertEqual(result["summary"]["expired_mismatches"], 1)
        # Regression: the revived job's veto fields went None -> PASS and were
        # counted as a language veto flip, failing the gate on expiry alone.
        self.assertFalse(omega["language_flip"])
        self.assertFalse(omega["location_flip"])
        self.assertFalse(omega["veto_flip"])
        self.assertFalse(omega["band_flip"])
        self.assertEqual(result["summary"]["veto_flips"], 0)
        self.assertTrue(result["summary"]["pass"])
        # The reverse direction (scored in golden, expired now) behaves the same.
        dead_now = set_field(baseline_run(), "acme-eng-1", "status", "expired")
        result = compare_runs(normalize_run(dead_now), normalize_run(baseline_run()), max_delta=5)
        acme = next(j for j in result["jobs"] if j["key"] == "acme-eng-1")
        self.assertTrue(acme["expired_mismatch"])
        self.assertFalse(acme["veto_flip"])
        self.assertTrue(result["summary"]["pass"])

    def test_only_in_lists_and_min_overlap(self):
        scores = normalize_run(baseline_run()[:2] + [job("new-6", 50, 50, 50, 50)])
        golden = normalize_run(baseline_run())
        result = compare_runs(scores, golden, max_delta=5)
        self.assertEqual(result["summary"]["compared"], 2)
        self.assertEqual(result["summary"]["only_in_scores"], ["new-6"])
        self.assertEqual(result["summary"]["only_in_golden"], ["delta-abroad-4", "gamma-ops-3", "omega-dead-5"])
        with self.assertRaises(EvalError):
            compare_runs(scores, golden, max_delta=5, min_overlap=3)
        with self.assertRaises(EvalError):
            compare_runs(normalize_run([job("zzz", 1, 1, 1, 1)]), golden, max_delta=5)

    def test_incomplete_dimensions_are_recorded_per_job(self):
        broken = baseline_run()
        del broken[1]["scores"]["behavioral"]
        result = compare_runs(normalize_run(broken), normalize_run(baseline_run()), max_delta=5)
        beta = next(j for j in result["jobs"] if j["key"] == "beta-data-2")
        self.assertEqual(beta["incomplete"], ["behavioral"])
        self.assertEqual(result["summary"]["incomplete"], ["beta-data-2"])


class CliRoundTripTests(unittest.TestCase):
    def test_save_golden_then_compare_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            scores = write_json(tmp, "run_a.json", baseline_run())
            golden = Path(tmp) / "nested" / "golden.json"
            code, out, err = run_main([
                "save-golden", "--scores", str(scores), "--out", str(golden), "--label", "route-A",
            ])
            self.assertEqual(code, 0, err)
            summary = json.loads(out)
            self.assertEqual(summary["count"], 5)
            self.assertEqual(summary["label"], "route-A")
            self.assertEqual(summary["source"], "run_a.json")
            stored = json.loads(golden.read_text(encoding="utf-8"))
            self.assertEqual(stored["label"], "route-A")
            self.assertEqual(stored["count"], 5)
            self.assertEqual(stored["source"], "run_a.json")
            self.assertEqual(set(stored["jobs"]), {j["key"] for j in baseline_run()})
            acme = stored["jobs"]["acme-eng-1"]
            self.assertEqual(acme["overall"], 76.0)
            self.assertEqual(acme["verdict"], "Strong Fit")
            self.assertEqual(acme["location_verdict"], "PASS")
            self.assertEqual(acme["language_gate"], "PASS")
            self.assertFalse(acme["expired"])
            self.assertTrue(stored["jobs"]["omega-dead-5"]["expired"])

            same = write_json(tmp, "run_b.json", baseline_run())
            code, out, err = run_main(["compare", "--scores", str(same), "--golden", str(golden)])
            self.assertEqual(code, 0, err)
            result = json.loads(out)
            self.assertTrue(result["summary"]["pass"])
            self.assertEqual(result["summary"]["max_abs_delta"], 0.0)
            self.assertEqual(len(out.strip().splitlines()), 1, "stdout must be one compact JSON document")

    def test_compare_exits_1_on_drift_and_on_veto_flip(self):
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "golden.json"
            run_main(["save-golden", "--scores", str(write_json(tmp, "base.json", baseline_run())), "--out", str(golden)])
            drifted = write_json(tmp, "drift.json", shift(baseline_run(), "beta-data-2", technical=70, experience=68, career=66.67))
            code, out, _ = run_main(["compare", "--scores", str(drifted), "--golden", str(golden), "--max-delta", "5"])
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(out)["summary"]["pass"])
            code, out, _ = run_main(["compare", "--scores", str(drifted), "--golden", str(golden), "--max-delta", "8"])
            self.assertEqual(code, 0)

            veto = write_json(tmp, "veto.json", set_field(baseline_run(), "delta-abroad-4", "location_verdict", "PASS"))
            code, out, _ = run_main(["compare", "--scores", str(veto), "--golden", str(golden)])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out)["summary"]["veto_flips"], 1)

    def test_min_overlap_error_via_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "golden.json"
            run_main(["save-golden", "--scores", str(write_json(tmp, "base.json", baseline_run())), "--out", str(golden)])
            partial = write_json(tmp, "partial.json", baseline_run()[:2])
            code, out, err = run_main(["compare", "--scores", str(partial), "--golden", str(golden), "--min-overlap", "3"])
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("overlap", err)
            disjoint = write_json(tmp, "disjoint.json", [job("unknown", 1, 1, 1, 1)])
            code, out, err = run_main(["compare", "--scores", str(disjoint), "--golden", str(golden)])
            self.assertEqual(code, 1)
            self.assertIn("overlap", err)

    def test_table_subcommand_and_format_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "golden.json"
            run_main(["save-golden", "--scores", str(write_json(tmp, "base.json", baseline_run())), "--out", str(golden)])
            drifted = write_json(tmp, "drift.json", shift(baseline_run(), "beta-data-2", technical=70, experience=68, career=66.67))
            code, table_out, _ = run_main(["table", "--scores", str(drifted), "--golden", str(golden)])
            self.assertEqual(code, 1)
            code, fmt_out, _ = run_main(["compare", "--format", "table", "--scores", str(drifted), "--golden", str(golden)])
            self.assertEqual(code, 1)
            self.assertEqual(table_out, fmt_out)
            lines = table_out.splitlines()
            self.assertTrue(lines[0].startswith("key"))
            job_lines = [line for line in lines if line.startswith(("acme-", "beta-", "gamma-", "delta-", "omega-"))]
            self.assertEqual(len(job_lines), 5)
            beta_line = next(line for line in job_lines if line.startswith("beta-data-2"))
            self.assertIn("+7.0", beta_line)
            self.assertIn("Good Fit", beta_line)
            self.assertNotIn("->", beta_line)
            self.assertIn("pass=false", lines[-1])
            # delta column stays aligned: every job line has the delta at the same offset
            offsets = {line.index(" -") if " -" in line[:40] else None for line in job_lines}
            self.assertLessEqual(len(offsets), 2)
            veto = write_json(tmp, "veto.json", set_field(baseline_run(), "delta-abroad-4", "location_verdict", "PASS"))
            code, veto_out, _ = run_main(["table", "--scores", str(veto), "--golden", str(golden)])
            self.assertEqual(code, 1)
            delta_line = next(line for line in veto_out.splitlines() if line.startswith("delta-abroad-4"))
            self.assertIn("FAIL -> PASS !", delta_line)

    def test_format_table_direct(self):
        result = compare_runs(normalize_run(baseline_run()), normalize_run(baseline_run()), max_delta=5)
        text = format_table(result)
        self.assertIn("pass=true", text)
        self.assertIn("omega-dead-5", text)

    def test_bad_input_files_exit_1_with_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "golden.json"
            run_main(["save-golden", "--scores", str(write_json(tmp, "base.json", baseline_run())), "--out", str(golden)])
            missing = Path(tmp) / "nope.json"
            code, out, err = run_main(["compare", "--scores", str(missing), "--golden", str(golden)])
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("nope.json", err)
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            code, _, err = run_main(["save-golden", "--scores", str(bad), "--out", str(golden)])
            self.assertEqual(code, 1)
            self.assertIn("JSON", err)
            wrong_shape = write_json(tmp, "shape.json", {"scores": []})
            code, _, err = run_main(["save-golden", "--scores", str(wrong_shape), "--out", str(golden)])
            self.assertEqual(code, 1)
            self.assertIn("jobs", err)

    def test_revived_expired_job_passes_via_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "golden.json"
            run_main(["save-golden", "--scores", str(write_json(tmp, "base.json", baseline_run())), "--out", str(golden)])
            revived = [j for j in baseline_run() if j["key"] != "omega-dead-5"] + [job("omega-dead-5", 50, 50, 50, 50)]
            code, out, err = run_main(["compare", "--scores", str(write_json(tmp, "revived.json", revived)), "--golden", str(golden)])
            self.assertEqual(code, 0, err)
            summary = json.loads(out)["summary"]
            self.assertEqual(summary["expired_mismatches"], 1)
            self.assertEqual(summary["veto_flips"], 0)
            self.assertTrue(summary["pass"])

    def test_non_finite_scores_still_print_valid_json_via_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "golden.json"
            run_main(["save-golden", "--scores", str(write_json(tmp, "base.json", baseline_run())), "--out", str(golden)])
            weird = set_field(baseline_run(), "acme-eng-1", "scores",
                              {"technical": True, "experience": "70", "behavioral": "nan", "career": 85})
            code, out, err = run_main(["compare", "--scores", str(write_json(tmp, "weird.json", weird)), "--golden", str(golden)])
            self.assertEqual(code, 1, err)
            self.assertNotIn("NaN", out)
            result = json.loads(out)  # must be strictly valid JSON (no NaN / Infinity tokens)
            acme = next(j for j in result["jobs"] if j["key"] == "acme-eng-1")
            self.assertEqual(acme["incomplete"], ["behavioral", "technical"])
            self.assertEqual(acme["scores_overall"], round(70 * 0.25 + 85 * 0.30, 1))
            self.assertEqual(result["summary"]["incomplete"], ["acme-eng-1"])

    def test_usage_errors_exit_2(self):
        with self.assertRaises(SystemExit) as ctx:
            run_main([])
        self.assertEqual(ctx.exception.code, 2)
        with self.assertRaises(SystemExit) as ctx:
            run_main(["compare", "--scores", "a.json"])
        self.assertEqual(ctx.exception.code, 2)
        with tempfile.TemporaryDirectory() as tmp:
            a = write_json(tmp, "a.json", baseline_run())
            code, _, err = run_main(["compare", "--scores", str(a), "--golden", str(a), "--min-overlap", "0"])
            self.assertEqual(code, 2)
            self.assertIn("min-overlap", err)
            for bad in ("nan", "inf", "-1"):
                code, out, err = run_main(["compare", "--scores", str(a), "--golden", str(a), "--max-delta", bad])
                self.assertEqual(code, 2, bad)
                self.assertEqual(out, "")
                self.assertIn("max-delta", err)


if __name__ == "__main__":
    unittest.main()
