"""Minimal HTTP API and static file server (standard library only)."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import traceback
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .db import init_db, utc_now
from .service import ServiceError, WarehouseService

STATIC_DIR = Path(__file__).resolve().parent / "static"
SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"

MAX_BODY = 4 * 1024 * 1024

ROUTES = [
    ("GET", re.compile(r"^/api/health$"), "health"),
    ("GET", re.compile(r"^/api/overview$"), "overview"),
    ("GET", re.compile(r"^/api/samples$"), "samples"),
    ("POST", re.compile(r"^/api/samples/(?P<scenario>[a-z_]+)$"), "load_sample"),
    ("GET", re.compile(r"^/api/samples/(?P<scenario>[a-z_]+)/files/(?P<kind>[a-z]+)$"), "sample_file"),
    ("GET", re.compile(r"^/api/batches$"), "list_batches"),
    ("POST", re.compile(r"^/api/batches$"), "create_batch"),
    ("GET", re.compile(r"^/api/batches/(?P<batch_id>[A-Za-z0-9_-]+)$"), "get_batch"),
    ("POST", re.compile(r"^/api/batches/(?P<batch_id>[A-Za-z0-9_-]+)/files$"), "upload"),
    ("GET", re.compile(r"^/api/batches/(?P<batch_id>[A-Za-z0-9_-]+)/files$"), "list_files"),
    ("GET", re.compile(r"^/api/batches/(?P<batch_id>[A-Za-z0-9_-]+)/preview/(?P<kind>[a-z]+)$"), "preview"),
    ("POST", re.compile(r"^/api/batches/(?P<batch_id>[A-Za-z0-9_-]+)/seal$"), "seal"),
    ("GET", re.compile(r"^/api/batches/(?P<batch_id>[A-Za-z0-9_-]+)/seals$"), "seals"),
    ("GET", re.compile(r"^/api/batches/(?P<batch_id>[A-Za-z0-9_-]+)/rejections\.csv$"), "rejections"),
    ("POST", re.compile(r"^/api/batches/(?P<batch_id>[A-Za-z0-9_-]+)/publish$"), "publish"),
    ("GET", re.compile(r"^/api/batches/(?P<batch_id>[A-Za-z0-9_-]+)/ops$"), "ops"),
    ("POST", re.compile(r"^/api/batches/(?P<batch_id>[A-Za-z0-9_-]+)/ops/(?P<op_id>[A-Za-z0-9_-]+)$"), "recover"),
    ("GET", re.compile(r"^/api/batches/(?P<batch_id>[A-Za-z0-9_-]+)/events$"), "events"),
    ("GET", re.compile(r"^/api/releases$"), "releases"),
    ("GET", re.compile(r"^/api/releases/(?P<release_id>[0-9]+)$"), "release_rows"),
    ("GET", re.compile(r"^/api/releases/latest$"), "release_rows"),
]

SAMPLE_SCENARIOS = {
    "initial": {
        "title": "混乱批次（循环到达）",
        "description": "文件按 发票 → 合同 → 客户 → 发票修订 的循环顺序到达；包含重复主键、缺失外键与合同开票额超限。",
        "order": ["invoices", "contracts", "customers", "invoices"],
        "label": "演示 · 混乱批次",
        "dir": "scenario_initial",
    },
    "fixed": {
        "title": "修复后批次",
        "description": "在混乱批次基础上修订客户、合同、发票三份文件，重复主键与金额超限均已修正。",
        "order": ["customers", "contracts", "invoices"],
        "label": "演示 · 修复批次",
        "dir": "scenario_fixed",
    },
    "next": {
        "title": "下一完整批次",
        "description": "第二个完整版本，用于验证版本指针切换后对外查询仍读取最新完整版本。",
        "order": ["contracts", "invoices", "customers"],
        "label": "演示 · 下一批次",
        "dir": "scenario_next",
    },
}


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "WarehouseTool/1.0"
    service: WarehouseService

    # --------------------------------------------------------------- handler
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def log_message(self, fmt, *args):
        if os.environ.get("WAREHOUSE_QUIET") != "1":
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _dispatch(self, method: str):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if not path.startswith("/api/"):
            if method == "GET":
                self._serve_static(path)
            else:
                self._json(404, {"error": "not_found", "message": "不支持的路径"})
            return

        for route_method, pattern, name in ROUTES:
            if route_method != method:
                continue
            match = pattern.match(path)
            if match:
                try:
                    result = getattr(self, "_api_" + name)(match.groupdict(), query)
                    if result is not None:
                        self._json(200, result)
                except ServiceError as exc:
                    self._json(exc.status, {"error": exc.code, "message": exc.message})
                except Exception:  # noqa: BLE001 - report as server error
                    traceback.print_exc()
                    self._json(500, {"error": "internal_error", "message": "服务器内部错误"})
                return
        self._json(404, {"error": "not_found", "message": f"未知接口 {method} {path}"})

    # ------------------------------------------------------------ API methods
    def _api_health(self, params, query):
        return {"ok": True, "service": "batch-warehouse"}

    def _api_overview(self, params, query):
        return {
            "latest_release": self.service.latest_release(),
            "sample_scenarios": SAMPLE_SCENARIOS,
        }

    def _api_samples(self, params, query):
        return {"scenarios": SAMPLE_SCENARIOS}

    def _api_load_sample(self, params, query):
        scenario = params["scenario"]
        if scenario not in SAMPLE_SCENARIOS:
            raise ServiceError("样例场景不存在", 404, "scenario_not_found")
        meta = SAMPLE_SCENARIOS[scenario]
        order = meta["order"]
        batch = self.service.create_batch(meta["label"])
        batch_id = batch["id"]
        for kind in order:
            content = self._sample_content(scenario, kind)
            self.service.upload_file(batch_id, kind, f"{kind}.csv", content)
        # Mark as demo batch for UI affordance.
        conn = self.service._conn()
        try:
            conn.execute(
                "UPDATE batches SET is_sample=1, updated_at=? WHERE id=?",
                (utc_now(), batch_id),
            )
            conn.execute(
                "INSERT INTO batch_events (batch_id, at, event, detail) VALUES (?, ?, ?, ?)",
                (
                    batch_id,
                    utc_now(),
                    "sample.loaded",
                    f"样例 {meta['title']}，到达顺序：{' → '.join(order)}",
                ),
            )
        finally:
            conn.close()
        detail = self.service.get_batch(batch_id)
        detail["arrival_order"] = order
        return detail

    def _api_sample_file(self, params, query):
        scenario = params["scenario"]
        kind = params["kind"]
        if scenario not in SAMPLE_SCENARIOS:
            raise ServiceError("样例场景不存在", 404, "scenario_not_found")
        content = self._sample_content(scenario, kind)
        return {"kind": kind, "filename": f"{kind}.csv", "content": content}

    @staticmethod
    def _sample_content(scenario: str, kind: str) -> str:
        path = SAMPLES_DIR / SAMPLE_SCENARIOS[scenario]["dir"] / f"{kind}.csv"
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ServiceError(f"样例文件读取失败：{exc}", 500, "sample_unreadable") from exc

    def _api_list_batches(self, params, query):
        return {"batches": self.service.list_batches()}

    def _api_create_batch(self, params, query):
        body = self._body_json()
        return self.service.create_batch(body.get("label", ""))

    def _api_get_batch(self, params, query):
        return self.service.get_batch(params["batch_id"])

    def _api_upload(self, params, query):
        kind, filename, content = self._extract_upload()
        return self.service.upload_file(params["batch_id"], kind, filename, content)

    def _api_list_files(self, params, query):
        kind = (query.get("kind") or [None])[0]
        return {"files": self.service.file_versions(params["batch_id"], kind)}

    def _api_preview(self, params, query):
        return self.service.candidate_preview(params["batch_id"], params["kind"])

    def _api_seal(self, params, query):
        return self.service.seal(params["batch_id"])

    def _api_seals(self, params, query):
        return {"seals": self.service.list_seals(params["batch_id"])}

    def _api_events(self, params, query):
        return {"events": self.service.events(params["batch_id"])}

    def _api_rejections(self, params, query):
        seal_id = (query.get("seal_id") or [None])[0]
        csv_text = self.service.rejections_csv(params["batch_id"], seal_id)
        filename = f"rejections-{params['batch_id']}.csv"
        data = csv_text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
        return None

    def _api_publish(self, params, query):
        body = self._body_json()
        return self.service.publish(params["batch_id"], body.get("interrupt", "none"))

    def _api_ops(self, params, query):
        return {"ops": self.service.list_ops(params["batch_id"])}

    def _api_recover(self, params, query):
        body = self._body_json()
        return self.service.recover(params["op_id"], body.get("action", "resume"))

    def _api_releases(self, params, query):
        return self.service.list_releases()

    def _api_release_rows(self, params, query):
        release_id = params.get("release_id")
        kind = (query.get("kind") or ["customers"])[0]
        search = (query.get("search") or [""])[0]
        limit = int((query.get("limit") or ["50"])[0])
        offset = int((query.get("offset") or ["0"])[0])
        return self.service.query_release(
            int(release_id) if release_id else None, kind, search, limit, offset
        )

    # -------------------------------------------------------------- utilities
    def _body_bytes(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ServiceError("请求体超过 4 MiB 上限", 413, "payload_too_large")
        return self.rfile.read(length) if length else b""

    def _body_json(self) -> dict:
        raw = self._body_bytes()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ServiceError("请求体必须是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict):
            raise ServiceError("请求体必须是 JSON 对象", 400, "invalid_json")
        return data

    def _extract_upload(self) -> tuple[str, str, str]:
        ctype = self.headers.get("Content-Type", "")
        raw = self._body_bytes()
        if ctype.startswith("multipart/form-data"):
            message = BytesParser(policy=email_policy).parsebytes(
                b"Content-Type: " + ctype.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw
            )
            fields: dict[str, str] = {}
            filename = ""
            content = None
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                fname = part.get_filename()
                payload = part.get_payload(decode=True) or b""
                if fname:
                    filename = fname
                    content = payload.decode("utf-8", "replace")
                elif name:
                    fields[name] = payload.decode("utf-8", "replace")
            if content is None:
                raise ServiceError("multipart 请求缺少文件字段", 400, "no_file")
            kind = fields.get("kind", "")
            return kind, filename or fields.get("filename", ""), content
        if not raw:
            body: dict = {}
        else:
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ServiceError("请求体必须是合法 JSON", 400, "invalid_json")
            if not isinstance(body, dict):
                raise ServiceError("请求体必须是 JSON 对象", 400, "invalid_json")
        return (
            body.get("kind", ""),
            body.get("filename", ""),
            body.get("content", ""),
        )

    def _json(self, status: int, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        target = (STATIC_DIR / path.lstrip("/")).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._json(403, {"error": "forbidden", "message": "路径不允许"})
            return
        if not target.is_file():
            self._json(404, {"error": "not_found", "message": "文件不存在"})
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def build_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    init_db(db_path)
    service = WarehouseService(db_path)
    service.reconcile_interrupted()
    handler = type("BoundApiHandler", (ApiHandler,), {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批次入仓校验与发布工具")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--db",
        default=os.environ.get("WAREHOUSE_DB", str(Path(__file__).resolve().parent.parent / "data" / "warehouse.db")),
    )
    args = parser.parse_args(argv)
    httpd = build_server(args.host, args.port, args.db)
    print(f"批次入仓台已启动：http://{args.host}:{args.port}  (db={args.db})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
