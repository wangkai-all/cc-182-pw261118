"""HTTP-level tests exercising the real threaded server."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.app import build_server  # noqa: E402

SAMPLES = ROOT / "samples"


def sample(name: str, kind: str) -> str:
    return (SAMPLES / name / f"{kind}.csv").read_text(encoding="utf-8")


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        """Fresh database per test keeps assertions about pointers exact."""
        self.tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.tmp.name, "http.db")
        self.httpd = build_server("127.0.0.1", 0, db_path)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        os.environ["WAREHOUSE_QUIET"] = "1"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    # ---------------------------------------------------------------- helpers
    def request(self, method, path, body=None, raw_body=None, content_type=None, expect=200):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = raw_body
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = resp.read()
                status = resp.status
                resp_headers = dict(resp.headers)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            status = exc.code
            resp_headers = dict(exc.headers)
        self.assertEqual(status, expect, payload[:400])
        ctype = resp_headers.get("Content-Type", "")
        if "application/json" in ctype:
            return json.loads(payload)
        return payload, resp_headers

    def make_batch(self, label="HTTP 批次"):
        return self.request("POST", "/api/batches", {"label": label})["id"]

    def upload_json(self, batch_id, kind, content, filename=None):
        return self.request(
            "POST",
            f"/api/batches/{batch_id}/files",
            {"kind": kind, "filename": filename or f"{kind}.csv", "content": content},
        )

    def seal(self, batch_id, expect=200):
        return self.request("POST", f"/api/batches/{batch_id}/seal", {}, expect=expect)

    def publish(self, batch_id, interrupt="none", expect=200):
        return self.request(
            "POST", f"/api/batches/{batch_id}/publish", {"interrupt": interrupt}, expect=expect
        )

    # ------------------------------------------------------------------- tests
    def test_health_and_static_index(self):
        health = self.request("GET", "/api/health")
        self.assertTrue(health["ok"])
        body, headers = self.request("GET", "/")
        self.assertIn("入仓台".encode(), body)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        app_js, headers = self.request("GET", "/app.js")
        self.assertIn(b"loadSample", app_js)

    def test_static_path_traversal_rejected(self):
        self.request("GET", "/../server/app.py", expect=403)

    def test_full_flow_over_http(self):
        batch_id = self.make_batch()
        for kind in ("invoices", "contracts", "customers", "invoices"):
            self.upload_json(batch_id, kind, sample("scenario_initial", kind))
        detail = self.request("GET", f"/api/batches/{batch_id}")
        self.assertEqual(detail["files"]["invoices"]["version"], 2)
        self.assertEqual(len(detail["files"]), 3)
        seal = self.seal(batch_id)
        self.assertFalse(seal["valid"])
        self.assertEqual(seal["error_count"], 4)
        self.assertEqual(self.request("POST", f"/api/batches/{batch_id}/seal", {})["id"], seal["id"])

        self.upload_json(batch_id, "customers", sample("scenario_fixed", "customers"))
        self.upload_json(batch_id, "contracts", sample("scenario_fixed", "contracts"))
        self.upload_json(batch_id, "invoices", sample("scenario_fixed", "invoices"))
        seal2 = self.seal(batch_id)
        self.assertTrue(seal2["valid"])

        result = self.publish(batch_id)
        self.assertEqual(result["status"], "published")
        again = self.publish(batch_id)
        self.assertEqual(again["status"], "already_published")

        latest = self.request("GET", "/api/releases/latest?kind=customers")
        self.assertEqual(latest["total"], 4)
        self.assertEqual(latest["release"]["id"], result["release"]["id"])

        releases = self.request("GET", "/api/releases")
        self.assertEqual(releases["latest_release_id"], result["release"]["id"])

    def test_multipart_upload(self):
        batch_id = self.make_batch("multipart")
        content = sample("scenario_fixed", "customers").encode()
        boundary = "----warehouse-test-boundary"
        parts = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="kind"\r\n\r\n'
            "customers\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="customers.csv"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        payload = self.request(
            "POST",
            f"/api/batches/{batch_id}/files",
            raw_body=parts,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        self.assertEqual(payload["kind"], "customers")
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["row_count"], 4)

    def test_rejection_csv_download_headers(self):
        batch_id = self.make_batch()
        for kind in ("customers", "contracts", "invoices"):
            self.upload_json(batch_id, kind, sample("scenario_initial", kind))
        self.seal(batch_id)
        body, headers = self.request("GET", f"/api/batches/{batch_id}/rejections.csv")
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn(".csv", headers["Content-Disposition"])
        text = body.decode("utf-8-sig")
        self.assertIn("customers.csv", text)
        self.assertIn("amount.contract_cap_exceeded", text)
        csv_rows = [line for line in text.splitlines() if line.strip()]
        self.assertEqual(len(csv_rows), 5)  # header + 4 errors

    def test_error_codes(self):
        self.request("GET", "/api/batches/nope", expect=404)
        self.request("POST", "/api/batches", {"label": "", }, expect=400)
        self.request("POST", "/api/batches", raw_body=b"{not json", content_type="application/json", expect=400)

    def test_interrupt_flow_over_http(self):
        baseline = self.make_batch("baseline")
        for kind in ("customers", "contracts", "invoices"):
            self.upload_json(baseline, kind, sample("scenario_fixed", kind))
        self.seal(baseline)
        rel = self.publish(baseline)["release"]

        batch_id = self.make_batch("interrupted")
        for kind in ("customers", "contracts", "invoices"):
            self.upload_json(batch_id, kind, sample("scenario_next", kind))
        self.seal(batch_id)
        out = self.publish(batch_id, interrupt="after_stage")
        self.assertEqual(out["status"], "interrupted")

        # Public query must not see the staged snapshot.
        latest = self.request("GET", "/api/releases/latest?kind=customers")
        self.assertEqual(latest["release"]["id"], rel["id"])
        self.assertEqual(latest["rows"][0]["customer_id"], "CUST-0001")

        op_id = out["op"]["id"]
        rec = self.request(
            "POST", f"/api/batches/{batch_id}/ops/{op_id}", {"action": "resume"}
        )
        self.assertEqual(rec["status"], "published")
        latest = self.request("GET", "/api/releases/latest?kind=customers")
        self.assertEqual(latest["rows"][0]["customer_id"], "CUST-1001")

        # A second resume is an idempotent no-op.
        again = self.request("POST", f"/api/batches/{batch_id}/ops/{op_id}", {"action": "resume"})
        self.assertEqual(again["status"], "already_completed")

    def test_discard_keeps_pointer(self):
        batch_id = self.make_batch()
        for kind in ("customers", "contracts", "invoices"):
            self.upload_json(batch_id, kind, sample("scenario_fixed", kind))
        self.seal(batch_id)
        out = self.publish(batch_id, interrupt="mid_copy")
        disc = self.request(
            "POST", f"/api/batches/{batch_id}/ops/{out['op']['id']}", {"action": "discard"}
        )
        self.assertEqual(disc["status"], "discarded")
        releases = self.request("GET", "/api/releases")
        self.assertEqual(releases["latest_release_id"], 0)

    def test_concurrent_publish_clicks_yield_one_version(self):
        batch_id = self.make_batch()
        for kind in ("customers", "contracts", "invoices"):
            self.upload_json(batch_id, kind, sample("scenario_fixed", kind))
        self.seal(batch_id)
        results = []

        def click():
            try:
                results.append(("ok", self.publish(batch_id)))
            except AssertionError:
                results.append(("err", None))

        threads = [threading.Thread(target=click) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        statuses = sorted(r[1]["status"] for r in results if r[0] == "ok")
        self.assertEqual(statuses.count("published"), 1)
        self.assertEqual(statuses.count("already_published"), 3)
        releases = self.request("GET", "/api/releases")
        self.assertEqual(len([r for r in releases["releases"] if r["state"] == "active"]), 1)

    def test_concurrent_resume_yields_one_activation(self):
        batch_id = self.make_batch()
        for kind in ("customers", "contracts", "invoices"):
            self.upload_json(batch_id, kind, sample("scenario_fixed", kind))
        self.seal(batch_id)
        out = self.publish(batch_id, interrupt="after_stage")
        op_path = f"/api/batches/{batch_id}/ops/{out['op']['id']}"
        results = []

        def resume():
            try:
                results.append(self.request("POST", op_path, {"action": "resume"}))
            except AssertionError:
                results.append(None)

        threads = [threading.Thread(target=resume) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        statuses = sorted(r["status"] for r in results if r)
        self.assertEqual(statuses.count("published"), 1)
        self.assertEqual(statuses.count("already_completed"), 3)
        releases = self.request("GET", "/api/releases")
        active = [r for r in releases["releases"] if r["state"] == "active"]
        self.assertEqual([r["id"] for r in active], [out["release"]["id"]])

    def test_sample_scenarios_endpoint(self):
        scenarios = self.request("GET", "/api/samples")["scenarios"]
        self.assertEqual(set(scenarios), {"initial", "fixed", "next"})
        loaded = self.request("POST", "/api/samples/initial")
        self.assertEqual(loaded["arrival_order"], ["invoices", "contracts", "customers", "invoices"])
        self.assertTrue(loaded["is_sample"])


if __name__ == "__main__":
    unittest.main()
