"""Domain-level tests for the batch warehouse tool (stdlib unittest)."""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.db import init_db  # noqa: E402
from server.service import ServiceError, WarehouseService  # noqa: E402

SAMPLES = ROOT / "samples"


def sample(name: str, kind: str) -> str:
    return (SAMPLES / name / f"{kind}.csv").read_text(encoding="utf-8")


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        init_db(self.db_path)
        self.svc = WarehouseService(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def new_batch(self, label="测试批次") -> str:
        return self.svc.create_batch(label)["id"]

    def fill_from(self, batch_id: str, scenario: str, order=("invoices", "contracts", "customers")) -> None:
        for kind in order:
            self.svc.upload_file(batch_id, kind, f"{kind}.csv", sample(scenario, kind))


class UploadTests(ServiceTestCase):
    def test_upload_in_any_order_is_accepted_without_fk_errors(self):
        batch_id = self.new_batch()
        self.svc.upload_file(batch_id, "invoices", "invoices.csv", sample("scenario_initial", "invoices"))
        preview = self.svc.candidate_preview(batch_id, "invoices")
        self.assertEqual(len(preview["rows"]), 6)
        # Cross-table rules must not fire at upload time even though the
        # referenced contracts are not present yet.
        rules = {i["rule"] for i in preview["file_issues"]}
        self.assertFalse(any(r.startswith("fk.") for r in rules))
        self.assertNotIn("file.missing", rules)

    def test_row_level_validation_reports_line_numbers(self):
        content = (
            "customer_id,name,region,contact_email\n"
            "CUST-0001,正常客户,华东,a@example.com\n"
            "CUST-002,格式错误,华东,b@example.com\n"
            "CUST-0003,,华东,not-an-email\n"
        )
        result = self.svc.upload_file(self.new_batch(), "customers", "customers.csv", content)
        self.assertEqual(result["row_count"], 1)
        preview = self.svc.candidate_preview(
            next(b["id"] for b in self.svc.list_batches()), "customers"
        )
        issues = preview["file_issues"]
        self.assertTrue(any(i["rule"] == "row.id_format" and i["line_no"] == 3 for i in issues))
        self.assertTrue(any(i["rule"] == "row.required" and i["line_no"] == 4 for i in issues))
        self.assertTrue(any(i["rule"] == "row.email_format" and i["line_no"] == 4 for i in issues))

    def test_header_mismatch_is_fatal_and_keeps_old_candidate(self):
        batch_id = self.new_batch()
        self.svc.upload_file(batch_id, "customers", "customers.csv", sample("scenario_fixed", "customers"))
        bad = "id,name,region,email\nCUST-0009,x,y,z@e.com\n"
        result = self.svc.upload_file(batch_id, "customers", "bad.csv", bad)
        self.assertTrue(result["fatal"])
        # A fatal revision replaces the current candidate (it is the user's
        # explicit intent) but keeps the archived previous version.
        versions = self.svc.file_versions(batch_id, "customers")
        self.assertEqual(len(versions), 2)
        self.assertTrue(any(v["is_current"] for v in versions))
        self.assertEqual(self.svc.get_batch(batch_id)["files"]["customers"]["filename"], "bad.csv")

    def test_revision_replaces_only_current_candidate(self):
        batch_id = self.new_batch()
        self.fill_from(batch_id, "scenario_initial")
        before = self.svc.get_batch(batch_id)
        self.svc.upload_file(batch_id, "customers", "customers.csv", sample("scenario_fixed", "customers"))
        after = self.svc.get_batch(batch_id)
        self.assertEqual(after["files"]["customers"]["version"], 2)
        self.assertEqual(after["files"]["contracts"]["version"], 1)
        self.assertEqual(after["files"]["invoices"]["version"], 1)
        versions = self.svc.file_versions(batch_id, "customers")
        self.assertEqual(len(versions), 2)
        self.assertEqual(sum(1 for v in versions if v["is_current"]), 1)
        # Archiving must not leak into sealed facts: old v1 issues stay attached
        # to the archived file only.
        self.assertNotEqual(before["files"]["customers"]["content_hash"], after["files"]["customers"]["content_hash"])

    def test_unknown_kind_and_empty_label_rejected(self):
        with self.assertRaises(ServiceError):
            self.svc.create_batch("   ")
        batch_id = self.new_batch()
        with self.assertRaises(ServiceError):
            self.svc.upload_file(batch_id, "payments", "x.csv", "a,b\n1,2\n")


class SealTests(ServiceTestCase):
    def test_sample_batch_reports_all_three_rule_categories(self):
        batch_id = self.new_batch()
        self.fill_from(batch_id, "scenario_initial")
        seal = self.svc.seal(batch_id)
        self.assertFalse(seal["valid"])
        rules = {i["rule"] for i in seal["issues"]}
        self.assertIn("pk.duplicate", rules)
        self.assertIn("fk.missing_contract", rules)
        self.assertIn("amount.contract_cap_exceeded", rules)
        locations = {(i["filename"], i["line_no"], i["field"]) for i in seal["issues"]}
        self.assertIn(("customers.csv", 5, "customer_id"), locations)
        self.assertIn(("invoices.csv", 6, "contract_id"), locations)
        self.assertIn(("contracts.csv", 3, "amount"), locations)

    def test_repeated_seal_click_is_idempotent(self):
        batch_id = self.new_batch()
        self.fill_from(batch_id, "scenario_fixed")
        first = self.svc.seal(batch_id)
        second = self.svc.seal(batch_id)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["reused"])
        self.assertEqual(len(self.svc.list_seals(batch_id)), 1)
        # And no candidate rows were duplicated.
        total = sum(len(self.svc.candidate_preview(batch_id, k)["rows"]) for k in ("customers", "contracts", "invoices"))
        self.assertEqual(total, 14)

    def test_seal_becomes_stale_after_revision(self):
        batch_id = self.new_batch()
        self.fill_from(batch_id, "scenario_fixed")
        self.svc.seal(batch_id)
        self.svc.upload_file(batch_id, "invoices", "invoices.csv", sample("scenario_next", "invoices"))
        batch = self.svc.get_batch(batch_id)
        self.assertFalse(batch["seal_matches"])
        with self.assertRaises(ServiceError) as ctx:
            self.svc.publish(batch_id)
        self.assertEqual(ctx.exception.code, "stale_seal")

    def test_missing_file_blocks_seal(self):
        batch_id = self.new_batch()
        self.svc.upload_file(batch_id, "customers", "customers.csv", sample("scenario_fixed", "customers"))
        seal = self.svc.seal(batch_id)
        self.assertFalse(seal["valid"])
        self.assertTrue(any(i["rule"] == "file.missing" for i in seal["issues"]))

    def test_seal_order_does_not_change_result(self):
        a = self.new_batch("顺序A")
        b = self.new_batch("顺序B")
        self.fill_from(a, "scenario_initial", ("invoices", "contracts", "customers"))
        self.fill_from(b, "scenario_initial", ("customers", "contracts", "invoices"))
        seal_a = self.svc.seal(a)
        seal_b = self.svc.seal(b)
        norm = lambda s: sorted((i["rule"], i["filename"], i["line_no"], i["message"]) for i in s["issues"])
        self.assertEqual(norm(seal_a), norm(seal_b))
        self.assertEqual(seal_a["fingerprint"], seal_b["fingerprint"])


class PublishTests(ServiceTestCase):
    def test_publish_requires_valid_seal(self):
        batch_id = self.new_batch()
        self.fill_from(batch_id, "scenario_fixed")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.publish(batch_id)
        self.assertEqual(ctx.exception.code, "not_sealed")
        self.svc.seal(batch_id)
        bad = self.new_batch()
        self.fill_from(bad, "scenario_initial")
        self.svc.seal(bad)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.publish(bad)
        self.assertEqual(ctx.exception.code, "invalid_seal")

    def test_publish_switches_pointer_and_is_idempotent(self):
        batch_id = self.new_batch()
        self.fill_from(batch_id, "scenario_fixed")
        self.svc.seal(batch_id)
        first = self.svc.publish(batch_id)
        self.assertEqual(first["status"], "published")
        release_id = first["release"]["id"]
        self.assertEqual(self.svc.list_releases()["latest_release_id"], release_id)
        second = self.svc.publish(batch_id)
        self.assertEqual(second["status"], "already_published")
        self.assertEqual(second["release"]["id"], release_id)
        rows = self.svc.query_release(None, "customers")["rows"]
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["customer_id"], "CUST-0001")

    def test_failed_publish_keeps_previous_version_visible(self):
        base = self.new_batch("基线")
        self.fill_from(base, "scenario_fixed")
        self.svc.seal(base)
        rel = self.svc.publish(base)["release"]
        broken = self.new_batch("坏批次")
        self.fill_from(broken, "scenario_initial")
        self.svc.seal(broken)
        with self.assertRaises(ServiceError):
            self.svc.publish(broken)
        self.assertEqual(self.svc.list_releases()["latest_release_id"], rel["id"])
        data = self.svc.query_release(None, "customers")
        self.assertEqual(data["release"]["id"], rel["id"])
        self.assertEqual({r["customer_id"] for r in data["rows"]}, {"CUST-0001", "CUST-0002", "CUST-0003", "CUST-0004"})

    def test_interrupted_after_stage_is_invisible_and_resumable(self):
        batch_id = self.new_batch("第二批")
        self.fill_from(batch_id, "scenario_fixed")
        self.svc.seal(batch_id)
        out = self.svc.publish(batch_id, interrupt="after_stage")
        self.assertEqual(out["status"], "interrupted")
        self.assertEqual(self.svc.list_releases()["latest_release_id"], 0)
        self.assertEqual(self.svc.query_release(None, "customers")["total"], 0)
        rec = self.svc.recover(out["op"]["id"], "resume")
        self.assertEqual(rec["status"], "published")
        self.assertEqual(self.svc.list_releases()["latest_release_id"], rec["release"]["id"])
        self.assertEqual(self.svc.query_release(None, "customers")["total"], 4)

    def test_mid_copy_interruption_must_be_discarded(self):
        batch_id = self.new_batch()
        self.fill_from(batch_id, "scenario_fixed")
        self.svc.seal(batch_id)
        out = self.svc.publish(batch_id, interrupt="mid_copy")
        self.assertEqual(out["status"], "interrupted")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.recover(out["op"]["id"], "resume")
        self.assertEqual(ctx.exception.code, "staged_incomplete")
        self.svc.recover(out["op"]["id"], "discard")
        self.assertEqual(self.svc.list_releases()["latest_release_id"], 0)
        republish = self.svc.publish(batch_id)
        self.assertEqual(republish["status"], "published")

    def test_recovery_blocks_when_base_version_moved(self):
        first = self.new_batch("A")
        self.fill_from(first, "scenario_fixed")
        self.svc.seal(first)
        self.svc.publish(first)

        second = self.new_batch("B")
        self.fill_from(second, "scenario_next")
        self.svc.seal(second)
        out = self.svc.publish(second, interrupt="after_stage")

        # Another batch may publish while B's snapshot is staged; the pointer
        # moves, which must invalidate B's now-stale snapshot.
        third = self.new_batch("C")
        self.fill_from(third, "scenario_next")
        self.svc.seal(third)
        res = self.svc.publish(third)
        self.assertEqual(res["status"], "published")

        with self.assertRaises(ServiceError) as ctx:
            self.svc.recover(out["op"]["id"], "resume")
        self.assertEqual(ctx.exception.code, "base_moved")
        self.svc.recover(out["op"]["id"], "discard")
        self.assertEqual(self.svc.list_releases()["latest_release_id"], res["release"]["id"])

    def test_resume_rejected_if_candidates_changed_after_interrupt(self):
        batch_id = self.new_batch()
        self.fill_from(batch_id, "scenario_fixed")
        self.svc.seal(batch_id)
        out = self.svc.publish(batch_id, interrupt="after_stage")
        self.svc.upload_file(batch_id, "customers", "customers.csv", sample("scenario_initial", "customers"))
        with self.assertRaises(ServiceError) as ctx:
            self.svc.recover(out["op"]["id"], "resume")
        self.assertEqual(ctx.exception.code, "staged_stale")

    def test_query_isolated_while_candidates_change(self):
        batch_id = self.new_batch()
        self.fill_from(batch_id, "scenario_fixed")
        self.svc.seal(batch_id)
        rel = self.svc.publish(batch_id)["release"]
        # Revise candidates in the same batch without sealing/publishing.
        self.svc.upload_file(batch_id, "customers", "customers.csv", sample("scenario_next", "customers"))
        data = self.svc.query_release(None, "customers")
        self.assertEqual(data["release"]["id"], rel["id"])
        self.assertEqual(set(r["customer_id"] for r in data["rows"]), {"CUST-0001", "CUST-0002", "CUST-0003", "CUST-0004"})
        # Historical release remains readable by id.
        hist = self.svc.query_release(rel["id"], "contracts")
        self.assertEqual(hist["total"], 4)
        self.assertEqual(self.svc.list_releases()["latest_release_id"], rel["id"])


class RejectionCsvTests(ServiceTestCase):
    def test_rejection_report_has_file_and_line_columns(self):
        batch_id = self.new_batch()
        self.fill_from(batch_id, "scenario_initial")
        self.svc.seal(batch_id)
        text = self.svc.rejections_csv(batch_id)
        rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["文件名"], "contracts.csv")
        for row in rows:
            self.assertIn(row["文件名"], {"customers.csv", "contracts.csv", "invoices.csv"})
            self.assertNotEqual(row["行号"], "")

    def test_rejection_csv_escapes_spreadsheet_formulas(self):
        content = (
            "customer_id,name,region,contact_email\n"
            "=cmd|' /C calc'!A0,客户,华东,a@example.com\n"
        )
        batch_id = self.new_batch()
        self.svc.upload_file(batch_id, "customers", "customers.csv", content)
        self.svc.seal(batch_id)
        text = self.svc.rejections_csv(batch_id)
        self.assertNotIn(",=cmd", text)
        self.assertIn("'=cmd", text)

    def test_report_without_seal_errors(self):
        batch_id = self.new_batch()
        with self.assertRaises(ServiceError) as ctx:
            self.svc.rejections_csv(batch_id)
        self.assertEqual(ctx.exception.code, "no_seal")


if __name__ == "__main__":
    unittest.main()
