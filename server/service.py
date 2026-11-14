"""Business logic for the batch warehouse tool.

Design notes
------------
* Candidate files live in ``batch_files`` / ``candidate_rows``. A revision
  replaces only the *current* candidate file of one kind; archived versions
  remain readable but are never considered by sealing or publishing.
* Sealing is the only place where cross-file rules run. The seal is stored
  against a fingerprint of the current candidates, which makes repeated
  sealing clicks idempotent and lets publishing detect stale seals.
* Publishing is two-phase: a complete snapshot is written into a staged
  release, then a single pointer (``meta.latest_release_id``) is flipped inside
  the same transaction. Readers only ever follow the pointer, so an interrupted
  publish never exposes partial data. Recovery can resume or discard the staged
  snapshot.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Iterator

from .csvparse import (
    COLUMNS,
    ID_FIELD,
    KIND_LABELS,
    KINDS,
    Issue,
    dumps,
    parse_csv,
    rows_to_json,
)
from .db import connect, latest_release_id, utc_now

MAX_LABEL = 80
MAX_FILENAME = 120


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


class WarehouseService:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    # ------------------------------------------------------------------
    # Batches
    # ------------------------------------------------------------------
    def create_batch(self, label: str) -> dict:
        label = (label or "").strip()
        if not label:
            raise ServiceError("批次名称不能为空")
        if len(label) > MAX_LABEL:
            raise ServiceError(f"批次名称不能超过 {MAX_LABEL} 个字符")
        batch_id = "b_" + uuid.uuid4().hex[:10]
        now = utc_now()
        conn = self._conn()
        try:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO batches (id, label, status, created_at, updated_at)"
                    " VALUES (?, ?, 'open', ?, ?)",
                    (batch_id, label, now, now),
                )
                self._event(conn, batch_id, "batch.created", label)
            return self.get_batch(batch_id)
        finally:
            conn.close()

    def list_batches(self) -> list[dict]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT b.*,"
                " (SELECT COUNT(*) FROM batch_files f WHERE f.batch_id=b.id AND f.is_current=1) AS file_count,"
                " (SELECT COUNT(*) FROM candidate_rows r JOIN batch_files f ON f.id=r.batch_file_id"
                "   WHERE f.batch_id=b.id AND f.is_current=1 AND r.parse_ok=1 AND r.data_json IS NOT NULL) AS candidate_rows,"
                " (SELECT s.valid FROM seals s WHERE s.id=b.latest_seal_id) AS latest_seal_valid,"
                " (SELECT s.fingerprint FROM seals s WHERE s.id=b.latest_seal_id) AS latest_fingerprint,"
                " (SELECT s.created_at FROM seals s WHERE s.id=b.latest_seal_id) AS latest_seal_at,"
                " (SELECT r.label FROM releases r JOIN meta m ON m.key='latest_release_id'"
                "   WHERE r.id=CAST(m.value AS INTEGER)) AS current_release_label"
                " FROM batches b ORDER BY b.created_at DESC, b.id DESC"
            ).fetchall()
            return [self._batch_summary(dict(r)) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _batch_summary(row: dict) -> dict:
        return {
            "id": row["id"],
            "label": row["label"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "file_count": row.get("file_count", 0),
            "candidate_rows": row.get("candidate_rows", 0),
            "latest_seal_id": row.get("latest_seal_id"),
            "latest_seal_valid": (
                None
                if row.get("latest_seal_valid") is None
                else bool(row.get("latest_seal_valid"))
            ),
            "latest_seal_at": row.get("latest_seal_at"),
            "published_release_id": row.get("published_release_id"),
            "current_release_label": row.get("current_release_label"),
            "is_sample": bool(row.get("is_sample")),
        }

    def get_batch(self, batch_id: str) -> dict:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if row is None:
                raise ServiceError("批次不存在", 404, "batch_not_found")
            batch = dict(row)
            files = {
                r["kind"]: self._file_dict(r)
                for r in conn.execute(
                    "SELECT * FROM batch_files WHERE batch_id=? AND is_current=1",
                    (batch_id,),
                ).fetchall()
            }
            seal = None
            if batch["latest_seal_id"]:
                seal = self._seal_dict(
                    conn, conn.execute(
                        "SELECT * FROM seals WHERE id=?", (batch["latest_seal_id"],)
                    ).fetchone()
                )
            ops = [
                self._op_dict(r)
                for r in conn.execute(
                    "SELECT * FROM publish_ops WHERE batch_id=? ORDER BY started_at DESC",
                    (batch_id,),
                ).fetchall()
            ]
            releases = [
                self._release_dict(r)
                for r in conn.execute(
                    "SELECT * FROM releases WHERE batch_id=? ORDER BY id DESC", (batch_id,)
                ).fetchall()
            ]
            current = latest_release_id(conn)
            published = conn.execute("SELECT * FROM releases WHERE id=?", (current,)).fetchone()
            batch.update(
                {
                    "files": files,
                    "latest_seal": seal,
                    "seals": [
                        self._seal_summary(r)
                        for r in conn.execute(
                            "SELECT * FROM seals WHERE batch_id=?"
                            " ORDER BY created_at DESC, id DESC",
                            (batch_id,),
                        ).fetchall()
                    ],
                    "ops": ops,
                    "batch_releases": releases,
                    "events": self.events(batch_id),
                    "current_release": self._release_dict(published),
                    "current_fingerprint": self._fingerprint(conn, batch_id),
                    "seal_matches": (
                        seal is not None
                        and seal["fingerprint"] == self._fingerprint(conn, batch_id)
                    ),
                }
            )
            return batch
        finally:
            conn.close()

    @staticmethod
    def _seal_summary(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "fingerprint": row["fingerprint"],
            "created_at": row["created_at"],
            "valid": bool(row["valid"]),
            "error_count": row["error_count"],
            "warning_count": row["warning_count"],
        }

    # ------------------------------------------------------------------
    # Upload / revision
    # ------------------------------------------------------------------
    def upload_file(self, batch_id: str, kind: str, filename: str, content: str) -> dict:
        if kind not in KINDS:
            raise ServiceError(f"未知文件类型 {kind!r}")
        filename = os.path.basename((filename or "").strip()) or f"{kind}.csv"
        if len(filename) > MAX_FILENAME:
            raise ServiceError(f"文件名不能超过 {MAX_FILENAME} 个字符")
        if not isinstance(content, str):
            raise ServiceError("文件内容必须是文本")
        conn = self._conn()
        try:
            with transaction(conn):
                batch = conn.execute(
                    "SELECT * FROM batches WHERE id=?", (batch_id,)
                ).fetchone()
                if batch is None:
                    raise ServiceError("批次不存在", 404, "batch_not_found")
                old = conn.execute(
                    "SELECT * FROM batch_files WHERE batch_id=? AND kind=? AND is_current=1",
                    (batch_id, kind),
                ).fetchone()
                version = conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM batch_files WHERE batch_id=? AND kind=?",
                    (batch_id, kind),
                ).fetchone()[0]
                parsed = parse_csv(kind, filename, content)
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                file_id = uuid.uuid4().hex[:12]
                now = utc_now()
                if old is not None:
                    conn.execute(
                        "UPDATE batch_files SET is_current=0, replaced_at=? WHERE id=?",
                        (now, old["id"]),
                    )
                row_count = sum(1 for r in parsed.rows if r.data is not None)
                conn.execute(
                    "INSERT INTO batch_files (id, batch_id, kind, filename, version,"
                    " content_hash, content, row_count, is_current, uploaded_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (
                        file_id,
                        batch_id,
                        kind,
                        filename,
                        version,
                        content_hash,
                        content,
                        row_count,
                        now,
                    ),
                )
                for entry in rows_to_json(parsed):
                    conn.execute(
                        "INSERT INTO candidate_rows (batch_file_id, kind, line_no, data_json,"
                        " raw_json, field_errors_json, parse_ok) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            file_id,
                            kind,
                            entry["line_no"],
                            dumps(entry["data"]) if entry["data"] is not None else None,
                            dumps(entry["raw"]),
                            dumps(entry["field_errors"]),
                            1 if entry["data"] is not None else 0,
                        ),
                    )
                for issue in parsed.issues:
                    conn.execute(
                        "INSERT INTO file_issues (batch_file_id, line_no, field, rule, severity,"
                        " value, message, related_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            file_id,
                            issue.line_no,
                            issue.field_name,
                            issue.rule,
                            issue.severity,
                            issue.value,
                            issue.message,
                            dumps(issue.related or {}),
                        ),
                    )
                conn.execute(
                    "UPDATE batches SET status=?, updated_at=? WHERE id=?",
                    (
                        "revising" if batch["published_release_id"] else "open",
                        now,
                        batch_id,
                    ),
                )
                self._event(
                    conn,
                    batch_id,
                    "file.revised" if old is not None else "file.uploaded",
                    f"{KIND_LABELS[kind]} v{version} {filename}（{len(parsed.rows)} 行）",
                )
                replaced = dict(old) if old is not None else None
            result = self._file_dict(
                self._get_file_row(file_id)
            )
            result["replaced_file"] = (
                {"id": replaced["id"], "version": replaced["version"], "filename": replaced["filename"]}
                if replaced
                else None
            )
            result["issues"] = len(parsed.issues)
            result["fatal"] = parsed.fatal
            result["batch_status"] = "open"
            return result
        finally:
            conn.close()

    def _get_file_row(self, file_id: str) -> sqlite3.Row:
        conn = self._conn()
        try:
            return conn.execute("SELECT * FROM batch_files WHERE id=?", (file_id,)).fetchone()
        finally:
            conn.close()

    @staticmethod
    def _file_dict(row: sqlite3.Row | dict) -> dict:
        data = dict(row)
        data.pop("content", None)
        return {
            "id": data["id"],
            "batch_id": data["batch_id"],
            "kind": data["kind"],
            "kind_label": KIND_LABELS[data["kind"]],
            "filename": data["filename"],
            "version": data["version"],
            "content_hash": data["content_hash"],
            "row_count": data["row_count"],
            "is_current": bool(data["is_current"]),
            "uploaded_at": data["uploaded_at"],
            "replaced_at": data["replaced_at"],
        }

    def file_versions(self, batch_id: str, kind: str | None = None) -> list[dict]:
        if kind is not None and kind not in KINDS:
            raise ServiceError(f"未知文件类型 {kind!r}")
        conn = self._conn()
        try:
            sql = "SELECT * FROM batch_files WHERE batch_id=?"
            params: list = [batch_id]
            if kind:
                sql += " AND kind=?"
                params.append(kind)
            sql += " ORDER BY kind, version DESC"
            out = []
            for row in conn.execute(sql, params).fetchall():
                item = self._file_dict(row)
                item["issues"] = conn.execute(
                    "SELECT COUNT(*) FROM file_issues WHERE batch_file_id=?", (row["id"],)
                ).fetchone()[0]
                out.append(item)
            return out
        finally:
            conn.close()

    def candidate_preview(self, batch_id: str, kind: str) -> dict:
        if kind not in KINDS:
            raise ServiceError(f"未知文件类型 {kind!r}")
        conn = self._conn()
        try:
            f = conn.execute(
                "SELECT * FROM batch_files WHERE batch_id=? AND kind=? AND is_current=1",
                (batch_id, kind),
            ).fetchone()
            if f is None:
                return {"kind": kind, "kind_label": KIND_LABELS[kind], "file": None, "rows": [], "file_issues": []}
            file_issues = [
                {
                    "severity": r["severity"],
                    "rule": r["rule"],
                    "kind": kind,
                    "filename": f["filename"],
                    "line_no": r["line_no"],
                    "field": r["field"],
                    "value": r["value"],
                    "message": r["message"],
                    "related": json.loads(r["related_json"]) if r["related_json"] else {},
                }
                for r in conn.execute(
                    "SELECT * FROM file_issues WHERE batch_file_id=? ORDER BY line_no, id",
                    (f["id"],),
                ).fetchall()
            ]
            by_line: dict[int | None, list[dict]] = {}
            for issue in file_issues:
                by_line.setdefault(issue["line_no"], []).append(issue)
            rows = []
            for r in conn.execute(
                "SELECT * FROM candidate_rows WHERE batch_file_id=? ORDER BY line_no, id",
                (f["id"],),
            ).fetchall():
                data = json.loads(r["data_json"]) if r["data_json"] else None
                raw = json.loads(r["raw_json"]) if r["raw_json"] else {}
                field_errors = json.loads(r["field_errors_json"]) if r["field_errors_json"] else []
                rows.append(
                    {
                        "line_no": r["line_no"],
                        "data": data,
                        "raw": raw,
                        "field_errors": field_errors,
                        "parse_ok": bool(r["parse_ok"]),
                        "issues": by_line.get(r["line_no"], []),
                    }
                )
            return {
                "kind": kind,
                "kind_label": KIND_LABELS[kind],
                "file": self._file_dict(f),
                "rows": rows,
                "file_issues": file_issues,
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Sealing
    # ------------------------------------------------------------------
    def _fingerprint(self, conn: sqlite3.Connection, batch_id: str) -> str:
        files = conn.execute(
            "SELECT kind, version, content_hash, filename FROM batch_files"
            " WHERE batch_id=? AND is_current=1 ORDER BY kind",
            (batch_id,),
        ).fetchall()
        parts = [f"{f['kind']}:{f['version']}:{f['content_hash']}:{f['filename']}" for f in files]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    def seal(self, batch_id: str) -> dict:
        conn = self._conn()
        try:
            with transaction(conn):
                batch = conn.execute(
                    "SELECT * FROM batches WHERE id=?", (batch_id,)
                ).fetchone()
                if batch is None:
                    raise ServiceError("批次不存在", 404, "batch_not_found")
                files = conn.execute(
                    "SELECT * FROM batch_files WHERE batch_id=? AND is_current=1 ORDER BY kind",
                    (batch_id,),
                ).fetchall()
                fingerprint = self._fingerprint(conn, batch_id)
                existing = conn.execute(
                    "SELECT * FROM seals WHERE batch_id=? AND fingerprint=?",
                    (batch_id, fingerprint),
                ).fetchone()
                if existing is not None:
                    self._event(
                        conn,
                        batch_id,
                        "seal.reused",
                        f"重复封口点击复用封口 {existing['id']}",
                    )
                    conn.execute(
                        "UPDATE batches SET updated_at=? WHERE id=?", (utc_now(), batch_id)
                    )
                    return self._seal_dict(conn, existing, reuse=True)

                issues, summary, valid_counts = self._run_constraints(conn, files)
                now = utc_now()
                seal_id = "s_" + uuid.uuid4().hex[:12]
                errors = sum(1 for i in issues if i.severity == "error")
                warnings = sum(1 for i in issues if i.severity == "warning")
                summary = dict(summary)
                summary["valid_counts"] = valid_counts
                summary["fingerprint"] = fingerprint
                conn.execute(
                    "INSERT INTO seals (id, batch_id, fingerprint, created_at, valid,"
                    " error_count, warning_count, issue_count, content_summary, base_release_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        seal_id,
                        batch_id,
                        fingerprint,
                        now,
                        1 if errors == 0 else 0,
                        errors,
                        warnings,
                        len(issues),
                        dumps(summary),
                        latest_release_id(conn),
                    ),
                )
                for issue in issues:
                    conn.execute(
                        "INSERT INTO seal_issues (seal_id, severity, rule, kind, filename,"
                        " line_no, field, value, message, related_json)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            seal_id,
                            issue.severity,
                            issue.rule,
                            issue.kind,
                            issue.filename,
                            issue.line_no,
                            issue.field_name,
                            issue.value,
                            issue.message,
                            dumps(issue.related or {}),
                        ),
                    )
                status = "sealed_valid" if errors == 0 else "sealed_invalid"
                if errors == 0 and batch["published_release_id"]:
                    status = "sealed_valid_republish"
                conn.execute(
                    "UPDATE batches SET latest_seal_id=?, status=?, updated_at=? WHERE id=?",
                    (seal_id, status, now, batch_id),
                )
                self._event(
                    conn,
                    batch_id,
                    "seal.created",
                    f"封口 {seal_id}：{errors} 项错误 / {warnings} 项警告",
                )
                seal = conn.execute("SELECT * FROM seals WHERE id=?", (seal_id,)).fetchone()
                return self._seal_dict(conn, seal)
        finally:
            conn.close()

    def _run_constraints(
        self,
        conn: sqlite3.Connection,
        files: list[sqlite3.Row],
    ) -> tuple[list[Issue], dict, dict]:
        """Run every cross-file rule exactly once, at seal time."""
        issues: list[Issue] = []
        file_map = {f["kind"]: f for f in files}
        file_summary = []

        for issue in self._structural_issues(conn, files):
            issues.append(issue)

        kinds_rows: dict[str, list[dict]] = {}
        for kind in KINDS:
            f = file_map.get(kind)
            if f is None:
                issues.append(
                    Issue(
                        rule="file.missing",
                        message=f"批次缺少{KIND_LABELS[kind]} CSV，封口后无法完成跨表校验",
                        severity="error",
                        kind=kind,
                    )
                )
                kinds_rows[kind] = []
                continue
            file_summary.append(
                {
                    "kind": kind,
                    "filename": f["filename"],
                    "version": f["version"],
                    "content_hash": f["content_hash"],
                    "row_count": f["row_count"],
                }
            )
            rows = []
            for r in conn.execute(
                "SELECT * FROM candidate_rows WHERE batch_file_id=? AND parse_ok=1"
                " AND data_json IS NOT NULL ORDER BY line_no, id",
                (f["id"],),
            ).fetchall():
                rows.append(
                    {
                        "line_no": r["line_no"],
                        "data": json.loads(r["data_json"]),
                        "filename": f["filename"],
                    }
                )
            kinds_rows[kind] = rows

        accepted: dict[str, list[dict]] = {kind: [] for kind in KINDS}
        id_field = {kind: ID_FIELD[kind] for kind in KINDS}

        # 1) primary key uniqueness across the whole seal scope
        for kind in KINDS:
            first_seen: dict[str, dict] = {}
            for row in kinds_rows[kind]:
                ident = row["data"].get(id_field[kind])
                if not ident:
                    continue
                if ident in first_seen:
                    first = first_seen[ident]
                    issues.append(
                        Issue(
                            rule="pk.duplicate",
                            message=(
                                f"{id_field[kind]} {ident} 重复：首次出现在 {first['filename']} 第 "
                                f"{first['line_no']} 行，此处再次出现"
                            ),
                            severity="error",
                            kind=kind,
                            filename=row["filename"],
                            line_no=row["line_no"],
                            field_name=id_field[kind],
                            value=ident,
                            related={
                                "first_file": first["filename"],
                                "first_line": first["line_no"],
                            },
                        )
                    )
                else:
                    first_seen[ident] = row
                    accepted[kind].append(row)

        customer_ids = {r["data"]["customer_id"] for r in accepted["customers"]}
        contract_ids = {r["data"]["contract_id"] for r in accepted["contracts"]}

        # 2) cross-table references
        for row in accepted["contracts"]:
            cid = row["data"].get("customer_id")
            if cid and cid not in customer_ids:
                issues.append(
                    Issue(
                        rule="fk.missing_customer",
                        message=f"合同引用了批次内不存在的客户 {cid}（客户主键缺失或重复被剔除）",
                        severity="error",
                        kind="contracts",
                        filename=row["filename"],
                        line_no=row["line_no"],
                        field_name="customer_id",
                        value=cid,
                    )
                )
        for row in accepted["invoices"]:
            cid = row["data"].get("contract_id")
            if cid and cid not in contract_ids:
                issues.append(
                    Issue(
                        rule="fk.missing_contract",
                        message=f"发票引用了批次内不存在的合同 {cid}（合同主键缺失或重复被剔除）",
                        severity="error",
                        kind="invoices",
                        filename=row["filename"],
                        line_no=row["line_no"],
                        field_name="contract_id",
                        value=cid,
                    )
                )

        # 3) contract amount ceiling, checked only after the whole batch is present
        contracts_by_id = {}
        for row in accepted["contracts"]:
            contracts_by_id.setdefault(row["data"]["contract_id"], row)
        invoice_totals: dict[str, int] = {}
        invoice_refs: dict[str, list[dict]] = {}
        for row in accepted["invoices"]:
            cid = row["data"].get("contract_id")
            if cid not in contract_ids:
                continue
            amount = row["data"].get("amount_cents")
            if amount is None:
                continue
            invoice_totals[cid] = invoice_totals.get(cid, 0) + amount
            invoice_refs.setdefault(cid, []).append(row)
        for cid, contract in contracts_by_id.items():
            cap = contract["data"].get("amount_cents")
            total = invoice_totals.get(cid, 0)
            if cap is not None and total > cap:
                refs = invoice_refs.get(cid, [])
                related = {
                    "contract_id": cid,
                    "contract_amount_cents": cap,
                    "invoice_total_cents": total,
                    "invoices": [
                        {
                            "filename": r["filename"],
                            "line_no": r["line_no"],
                            "invoice_id": r["data"].get("invoice_id"),
                            "amount_cents": r["data"].get("amount_cents"),
                        }
                        for r in refs
                    ],
                }
                issues.append(
                    Issue(
                        rule="amount.contract_cap_exceeded",
                        message=(
                            f"合同 {cid} 金额上限 {cap / 100:.2f}，已开票合计 {total / 100:.2f}，"
                            f"超出 {(total - cap) / 100:.2f}"
                        ),
                        severity="error",
                        kind="contracts",
                        filename=contract["filename"],
                        line_no=contract["line_no"],
                        field_name="amount",
                        value=f"{cap / 100:.2f}",
                        related=related,
                    )
                )

        summary = {
            "files": file_summary,
            "row_counts": {
                kind: len(kinds_rows[kind]) for kind in KINDS
            },
        }
        valid_counts = {kind: len(accepted[kind]) for kind in KINDS}
        return issues, summary, valid_counts

    def _structural_issues(self, conn: sqlite3.Connection, files: list[sqlite3.Row]) -> list[Issue]:
        issues = []
        for f in files:
            for r in conn.execute(
                "SELECT * FROM file_issues WHERE batch_file_id=? ORDER BY line_no, id",
                (f["id"],),
            ).fetchall():
                issues.append(
                    Issue(
                        rule=r["rule"],
                        message=r["message"],
                        severity=r["severity"],
                        kind=f["kind"],
                        filename=f["filename"],
                        line_no=r["line_no"],
                        field_name=r["field"],
                        value=r["value"],
                        related=json.loads(r["related_json"]) if r["related_json"] else {},
                    )
                )
        return issues

    @staticmethod
    def _seal_dict(
        conn: sqlite3.Connection, row: sqlite3.Row, reuse: bool = False
    ) -> dict:
        summary = json.loads(row["content_summary"])
        issues = [
            {
                "severity": r["severity"],
                "rule": r["rule"],
                "kind": r["kind"],
                "filename": r["filename"],
                "line_no": r["line_no"],
                "field": r["field"],
                "value": r["value"],
                "message": r["message"],
                "related": json.loads(r["related_json"]) if r["related_json"] else {},
            }
            for r in conn.execute(
                "SELECT * FROM seal_issues WHERE seal_id=? ORDER BY"
                " CASE severity WHEN 'error' THEN 0 ELSE 1 END, filename, line_no, id",
                (row["id"],),
            ).fetchall()
        ]
        return {
            "id": row["id"],
            "batch_id": row["batch_id"],
            "fingerprint": row["fingerprint"],
            "created_at": row["created_at"],
            "valid": bool(row["valid"]),
            "error_count": row["error_count"],
            "warning_count": row["warning_count"],
            "issue_count": row["issue_count"],
            "summary": summary,
            "issues": issues,
            "reused": reuse,
        }

    def list_seals(self, batch_id: str) -> list[dict]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM seals WHERE batch_id=? ORDER BY created_at DESC, id DESC",
                (batch_id,),
            ).fetchall()
            return [self._seal_dict(conn, r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Publishing & recovery
    # ------------------------------------------------------------------
    def publish(self, batch_id: str, interrupt: str = "none") -> dict:
        if interrupt not in ("none", "after_stage", "mid_copy"):
            raise ServiceError("未知的中断模拟模式")

        # ---------------------------------------------------------- phase 1
        conn = self._conn()
        op_id = "op_" + uuid.uuid4().hex[:12]
        try:
            with transaction(conn):
                batch = conn.execute(
                    "SELECT * FROM batches WHERE id=?", (batch_id,)
                ).fetchone()
                if batch is None:
                    raise ServiceError("批次不存在", 404, "batch_not_found")
                fingerprint = self._fingerprint(conn, batch_id)
                latest = (
                    conn.execute(
                        "SELECT * FROM seals WHERE id=?", (batch["latest_seal_id"],)
                    ).fetchone()
                    if batch["latest_seal_id"]
                    else None
                )
                if latest is None:
                    raise ServiceError("请先封口并完成校验", 409, "not_sealed")
                if latest["fingerprint"] != fingerprint:
                    raise ServiceError(
                        "候选文件在封口后已变更，请重新封口后再发布", 409, "stale_seal"
                    )
                if not latest["valid"]:
                    raise ServiceError(
                        "封口校验未通过，不能发布；请按拒绝原因修订文件", 409, "invalid_seal"
                    )

                existing_release = conn.execute(
                    "SELECT * FROM releases WHERE batch_id=? AND seal_id=? AND"
                    " state IN ('active','superseded')",
                    (batch_id, latest["id"]),
                ).fetchone()
                if existing_release is not None:
                    if existing_release["state"] == "superseded":
                        raise ServiceError(
                            "该封口版本已发布并被后续版本取代", 409, "already_superseded"
                        )
                    self._event(
                        conn, batch_id, "publish.idempotent",
                        f"重复发布点击复用版本 R{existing_release['id']}",
                    )
                    return {
                        "status": "already_published",
                        "release": self._release_dict(existing_release),
                        "op": None,
                    }

                active_op = conn.execute(
                    "SELECT * FROM publish_ops WHERE batch_id=? AND status IN"
                    " ('running','interrupted') ORDER BY started_at DESC LIMIT 1",
                    (batch_id,),
                ).fetchone()
                if active_op is not None:
                    raise ServiceError(
                        "存在未完成的发布操作，请先在发布页恢复或丢弃", 409, "publish_in_progress"
                    )

                staged = conn.execute(
                    "SELECT * FROM releases WHERE batch_id=? AND state='staged'"
                    " ORDER BY id DESC LIMIT 1",
                    (batch_id,),
                ).fetchone()
                if staged is not None:
                    raise ServiceError(
                        "存在待恢复的暂存版本，请先恢复或丢弃", 409, "staged_release_exists"
                    )

                summary = json.loads(latest["content_summary"])
                expected = {
                    kind: summary.get("valid_counts", {}).get(kind, 0) for kind in KINDS
                }
                release_id = conn.execute(
                    "SELECT COALESCE(MAX(id),0)+1 FROM releases"
                ).fetchone()[0]
                base = latest_release_id(conn)
                now = utc_now()
                label = f"{batch['label']} · R{release_id}"
                conn.execute(
                    "INSERT INTO releases (id, batch_id, batch_label, seal_id, label, state,"
                    " created_at, prev_release_id) VALUES (?, ?, ?, ?, ?, 'staged', ?, ?)",
                    (release_id, batch_id, batch["label"], latest["id"], label, now, base),
                )
                conn.execute(
                    "INSERT INTO publish_ops (id, batch_id, status, started_at, updated_at,"
                    " fault, phase, release_id, note)"
                    " VALUES (?, ?, 'running', ?, ?, ?, 'copying', ?, ?)",
                    (
                        op_id,
                        batch_id,
                        now,
                        now,
                        None if interrupt == "none" else interrupt,
                        release_id,
                        dumps({"expected": expected, "base_release_id": base}),
                    ),
                )
                copied = self._copy_candidates(
                    conn, latest, release_id, batch, expected, interrupt
                )
                conn.execute(
                    "UPDATE releases SET customer_count=?, contract_count=?, invoice_count=?"
                    " WHERE id=?",
                    (
                        copied["customers"],
                        copied["contracts"],
                        copied["invoices"],
                        release_id,
                    ),
                )
                note = {
                    "expected": expected,
                    "base_release_id": base,
                    "copied": copied,
                    "partial": interrupt == "mid_copy",
                }
                interrupted_phase = "copying" if interrupt == "mid_copy" else "staged"
                if interrupt == "none":
                    conn.execute(
                        "UPDATE publish_ops SET phase='staged', note=? WHERE id=?",
                        (dumps(note), op_id),
                    )
                    op_status = "running"
                else:
                    conn.execute(
                        "UPDATE publish_ops SET status='interrupted', updated_at=?,"
                        " phase=?, note=? WHERE id=?",
                        (utc_now(), interrupted_phase, dumps(note), op_id),
                    )
                    op_status = "interrupted"
                self._event(
                    conn,
                    batch_id,
                    "publish.staged",
                    f"暂存版本 R{release_id} 已落盘（{copied}），对外指针尚未切换",
                )
                if op_status == "interrupted":
                    self._event(
                        conn, batch_id, "publish.interrupted",
                        f"模拟中断 {interrupt}，暂存版本 R{release_id} 未切换指针",
                    )
        finally:
            conn.close()

        release = self._get_release_row(release_id)
        op = self._get_op_row(op_id)
        if interrupt != "none":
            return {
                "status": "interrupted",
                "release": self._release_dict(release),
                "op": self._op_dict(op),
            }

        # ---------------------------------------------------------- phase 2
        # Stage-then-flip: re-validate inside one write transaction. A failed
        # validation must still commit the "interrupted" marker (otherwise the
        # op would stay 'running' and block the batch forever), so validation
        # failures are collected and raised after the transaction commits.
        failure: ServiceError | None = None
        conn = self._conn()
        try:
            with transaction(conn):
                release = conn.execute(
                    "SELECT * FROM releases WHERE id=?", (release_id,)
                ).fetchone()
                current_seal = conn.execute(
                    "SELECT * FROM seals WHERE id=?", (release["seal_id"],)
                ).fetchone()
                fingerprint = self._fingerprint(conn, batch_id)
                base = latest_release_id(conn)
                counts = {
                    "customers": release["customer_count"],
                    "contracts": release["contract_count"],
                    "invoices": release["invoice_count"],
                }
                if fingerprint != current_seal["fingerprint"]:
                    failure = ServiceError(
                        "暂存期间候选文件被修订，版本未切换；请恢复或丢弃该操作",
                        409,
                        "staged_stale",
                    )
                elif base != release["prev_release_id"]:
                    failure = ServiceError(
                        "暂存期间已有其他版本发布，版本未切换；请恢复或丢弃该操作",
                        409,
                        "base_moved",
                    )
                else:
                    try:
                        self._verify_counts(conn, release_id, counts)
                    except ServiceError as exc:
                        failure = exc
                if failure is not None:
                    conn.execute(
                        "UPDATE publish_ops SET status='interrupted', phase='stale',"
                        " updated_at=? WHERE id=?",
                        (utc_now(), op_id),
                    )
                    self._event(
                        conn,
                        batch_id,
                        "publish.superseded",
                        f"暂存版本 R{release_id} 未能激活：{failure.code}",
                    )
                else:
                    self._activate(conn, release_id, batch_id, current_seal, counts, op_id)
                    self._event(
                        conn, batch_id, "publish.completed",
                        f"发布 R{release_id} 并切换对外版本指针",
                    )
        finally:
            conn.close()

        if failure is not None:
            raise failure

        return {
            "status": "published",
            "release": self._release_dict(self._get_release_row(release_id)),
            "op": self._op_dict(self._get_op_row(op_id)),
        }

    def _get_release_row(self, release_id: int) -> sqlite3.Row:
        conn = self._conn()
        try:
            return conn.execute(
                "SELECT * FROM releases WHERE id=?", (release_id,)
            ).fetchone()
        finally:
            conn.close()

    def _get_op_row(self, op_id: str) -> sqlite3.Row:
        conn = self._conn()
        try:
            return conn.execute(
                "SELECT * FROM publish_ops WHERE id=?", (op_id,)
            ).fetchone()
        finally:
            conn.close()

    def _verify_counts(self, conn: sqlite3.Connection, release_id: int, counts: dict) -> None:
        for kind, table in (
            ("customers", "published_customers"),
            ("contracts", "published_contracts"),
            ("invoices", "published_invoices"),
        ):
            actual = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE release_id=?", (release_id,)
            ).fetchone()[0]
            if actual != counts[kind]:
                raise ServiceError(
                    f"暂存版本 {kind} 数量不一致（记录 {counts[kind]}，实际 {actual}）",
                    409,
                    "staged_incomplete",
                )


    def _copy_candidates(
        self,
        conn: sqlite3.Connection,
        seal: sqlite3.Row,
        release_id: int,
        batch: sqlite3.Row,
        expected: dict,
        interrupt: str,
    ) -> dict:
        total = sum(expected.values())
        limit = total if interrupt != "mid_copy" else max(1, total // 2)
        copied = {kind: 0 for kind in KINDS}
        written = 0
        for kind in KINDS:
            files = conn.execute(
                "SELECT * FROM batch_files WHERE batch_id=? AND kind=? AND is_current=1",
                (batch["id"], kind),
            ).fetchall()
            for f in files:
                rows = conn.execute(
                    "SELECT * FROM candidate_rows WHERE batch_file_id=? AND parse_ok=1"
                    " AND data_json IS NOT NULL ORDER BY line_no, id",
                    (f["id"],),
                ).fetchall()
                for r in rows:
                    if written >= limit:
                        continue
                    data = json.loads(r["data_json"])
                    self._insert_published(
                        conn, kind, release_id, data, batch["id"], f["filename"], r["line_no"]
                    )
                    copied[kind] += 1
                    written += 1
        return copied

    @staticmethod
    def _insert_published(
        conn: sqlite3.Connection,
        kind: str,
        release_id: int,
        data: dict,
        batch_id: str,
        filename: str,
        line_no: int,
    ) -> None:
        if kind == "customers":
            conn.execute(
                "INSERT INTO published_customers (release_id, customer_id, name, region,"
                " contact_email, source_batch, source_file, source_line)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    release_id,
                    data["customer_id"],
                    data["name"],
                    data["region"],
                    data["contact_email"],
                    batch_id,
                    filename,
                    line_no,
                ),
            )
        elif kind == "contracts":
            conn.execute(
                "INSERT INTO published_contracts (release_id, contract_id, customer_id, title,"
                " amount_cents, signed_on, source_batch, source_file, source_line)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    release_id,
                    data["contract_id"],
                    data["customer_id"],
                    data["title"],
                    data["amount_cents"],
                    data["signed_on"],
                    batch_id,
                    filename,
                    line_no,
                ),
            )
        elif kind == "invoices":
            conn.execute(
                "INSERT INTO published_invoices (release_id, invoice_id, contract_id, amount_cents,"
                " issued_on, source_batch, source_file, source_line)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    release_id,
                    data["invoice_id"],
                    data["contract_id"],
                    data["amount_cents"],
                    data["issued_on"],
                    batch_id,
                    filename,
                    line_no,
                ),
            )

    def _activate(
        self,
        conn: sqlite3.Connection,
        release_id: int,
        batch_id: str,
        seal: sqlite3.Row,
        counts: dict,
        op_id: str,
    ) -> None:
        now = utc_now()
        previous = latest_release_id(conn)
        # Guard against two concurrent resume/publish requests both activating:
        # only the request that still finds the op un-activated may flip the
        # pointer. This runs inside BEGIN IMMEDIATE, so the check is atomic.
        claimed = conn.execute(
            "UPDATE publish_ops SET phase='activating', updated_at=?"
            " WHERE id=? AND status IN ('running','interrupted')",
            (now, op_id),
        ).rowcount
        if claimed == 0:
            raise ServiceError(
                "该发布操作已由其他请求完成", 409, "op_already_active"
            )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "UPDATE releases SET state='superseded' WHERE id=? AND state='active'", (previous,)
        )
        conn.execute(
            "UPDATE releases SET state='active', activated_at=?, customer_count=?,"
            " contract_count=?, invoice_count=?, prev_release_id=? WHERE id=?",
            (
                now,
                counts["customers"],
                counts["contracts"],
                counts["invoices"],
                previous,
                release_id,
            ),
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('latest_release_id', ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(release_id),),
        )
        conn.execute(
            "UPDATE batches SET status='published', published_release_id=?, updated_at=?"
            " WHERE id=?",
            (release_id, now, batch_id),
        )
        conn.execute(
            "UPDATE publish_ops SET status='completed', phase='activated', updated_at=?"
            " WHERE id=?",
            (now, op_id),
        )

    def list_ops(self, batch_id: str) -> list[dict]:
        conn = self._conn()
        try:
            return [
                self._op_dict(r)
                for r in conn.execute(
                    "SELECT * FROM publish_ops WHERE batch_id=? ORDER BY started_at DESC, id DESC",
                    (batch_id,),
                ).fetchall()
            ]
        finally:
            conn.close()

    @staticmethod
    def _op_dict(row: sqlite3.Row | dict) -> dict:
        d = dict(row)
        d["note"] = json.loads(d["note"]) if d.get("note") else {}
        return d

    def recover(self, op_id: str, action: str) -> dict:
        if action not in ("resume", "discard"):
            raise ServiceError("恢复动作必须是 resume 或 discard")
        conn = self._conn()
        try:
            with transaction(conn):
                op = conn.execute("SELECT * FROM publish_ops WHERE id=?", (op_id,)).fetchone()
                if op is None:
                    raise ServiceError("发布操作不存在", 404, "op_not_found")
                if op["status"] == "completed":
                    if action == "resume":
                        release = conn.execute(
                            "SELECT * FROM releases WHERE id=?", (op["release_id"],)
                        ).fetchone()
                        return {
                            "status": "already_completed",
                            "release": self._release_dict(release),
                            "op": self._op_dict(op),
                        }
                    raise ServiceError("该发布操作已完成，无法丢弃", 409, "op_completed")
                if op["status"] not in ("interrupted", "running"):
                    raise ServiceError(f"当前状态 {op['status']} 无法恢复", 409, "op_not_recoverable")
                batch = conn.execute(
                    "SELECT * FROM batches WHERE id=?", (op["batch_id"],)
                ).fetchone()
                if batch is None:
                    raise ServiceError("批次不存在", 404, "batch_not_found")
                note = json.loads(op["note"]) if op["note"] else {}
                release = None
                if op["release_id"]:
                    release = conn.execute(
                        "SELECT * FROM releases WHERE id=?", (op["release_id"],)
                    ).fetchone()

                if action == "discard":
                    if release is not None:
                        if release["state"] == "active":
                            raise ServiceError("该版已激活，无法丢弃", 409, "release_active")
                        for table in (
                            "published_customers",
                            "published_contracts",
                            "published_invoices",
                        ):
                            conn.execute(f"DELETE FROM {table} WHERE release_id=?", (release["id"],))
                        conn.execute(
                            "UPDATE releases SET state='discarded', seal_id=NULL WHERE id=?",
                            (release["id"],),
                        )
                    conn.execute(
                        "UPDATE publish_ops SET status='discarded', updated_at=?, phase='discarded'"
                        " WHERE id=?",
                        (utc_now(), op_id),
                    )
                    self._event(
                        conn, op["batch_id"], "publish.discarded",
                        f"丢弃暂存版本 {op['release_id'] or '(无)'}",
                    )
                    return {
                        "status": "discarded",
                        "release": None,
                        "op": self._op_dict(
                            conn.execute(
                                "SELECT * FROM publish_ops WHERE id=?", (op_id,)
                            ).fetchone()
                        ),
                    }

                # resume: re-verify everything before flipping the pointer
                if release is None or release["state"] != "staged":
                    raise ServiceError(
                        "没有可恢复的暂存版本，请丢弃该操作", 409, "no_staged_release"
                    )
                expected = note.get("expected", {})
                actual = {
                    "customers": release["customer_count"],
                    "contracts": release["contract_count"],
                    "invoices": release["invoice_count"],
                }
                if note.get("partial") or any(
                    expected.get(k, 0) != actual[k] for k in KINDS
                ):
                    raise ServiceError(
                        "暂存版本不完整（应有 "
                        f"{expected}，实际 {actual}），只能丢弃后重新发布",
                        409,
                        "staged_incomplete",
                    )
                self._verify_counts(conn, release["id"], actual)
                current = latest_release_id(conn)
                base = note.get("base_release_id")
                if base is not None and base != current:
                    raise ServiceError(
                        "暂存版本的基线版本已变化，恢复会回退新数据；请丢弃该操作",
                        409,
                        "base_moved",
                    )
                seal = conn.execute(
                    "SELECT * FROM seals WHERE id=?", (release["seal_id"],)
                ).fetchone()
                if seal is None:
                    raise ServiceError("暂存版本关联的封口不存在", 409, "seal_missing")
                if self._fingerprint(conn, op["batch_id"]) != seal["fingerprint"]:
                    raise ServiceError(
                        "候选文件在中断后已被修订，暂存版本已过期；请丢弃该操作",
                        409,
                        "staged_stale",
                    )
                if self._fingerprint(conn, op["batch_id"]) != seal["fingerprint"]:
                    raise ServiceError(
                        "候选文件在中断后已被修订，暂存版本已过期；请丢弃该操作",
                        409,
                        "staged_stale",
                    )
                self._activate(
                    conn,
                    release["id"],
                    op["batch_id"],
                    seal,
                    actual,
                    op_id,
                )
                self._event(
                    conn,
                    op["batch_id"],
                    "publish.recovered",
                    f"恢复中断发布，切换指针到 R{release['id']}",
                )
                release = conn.execute(
                    "SELECT * FROM releases WHERE id=?", (release["id"],)
                ).fetchone()
                op = conn.execute("SELECT * FROM publish_ops WHERE id=?", (op_id,)).fetchone()
                return {
                    "status": "published",
                    "release": self._release_dict(release),
                    "op": self._op_dict(op),
                }
        finally:
            conn.close()

    def latest_release(self) -> dict | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM releases WHERE id=?", (latest_release_id(conn),)
            ).fetchone()
            return self._release_dict(row)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Read-only published queries
    # ------------------------------------------------------------------
    def list_releases(self) -> dict:
        conn = self._conn()
        try:
            current = latest_release_id(conn)
            rows = conn.execute(
                "SELECT * FROM releases WHERE state IN ('active','superseded')"
                " ORDER BY id DESC"
            ).fetchall()
            return {
                "latest_release_id": current,
                "releases": [
                    {**self._release_dict(r), "is_latest": r["id"] == current} for r in rows
                ],
            }
        finally:
            conn.close()

    def query_release(
        self,
        release_id: int | None = None,
        kind: str = "customers",
        search: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        if kind not in KINDS:
            raise ServiceError(f"未知数据表 {kind!r}")
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        conn = self._conn()
        try:
            latest = latest_release_id(conn)
            target = latest if release_id is None else int(release_id)
            release = conn.execute(
                "SELECT * FROM releases WHERE id=? AND state IN ('active','superseded')",
                (target,),
            ).fetchone()
            if release is None:
                raise ServiceError("该版本不存在或尚未发布", 404, "release_not_found")
            table = {
                "customers": "published_customers",
                "contracts": "published_contracts",
                "invoices": "published_invoices",
            }[kind]
            where = "release_id = ?"
            params: list = [target]
            if search.strip():
                pattern = f"%{search.strip()}%"
                if kind == "customers":
                    where += " AND (customer_id LIKE ? OR name LIKE ? OR region LIKE ? OR contact_email LIKE ?)"
                    params += [pattern] * 4
                elif kind == "contracts":
                    where += " AND (contract_id LIKE ? OR customer_id LIKE ? OR title LIKE ?)"
                    params += [pattern] * 3
                else:
                    where += " AND (invoice_id LIKE ? OR contract_id LIKE ?)"
                    params += [pattern] * 2
            total = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE {where} ORDER BY 2 LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return {
                "release": self._release_dict(release),
                "latest_release_id": latest,
                "is_latest": target == latest,
                "access": "read-only",
                "kind": kind,
                "kind_label": KIND_LABELS[kind],
                "total": total,
                "limit": limit,
                "offset": offset,
                "rows": [dict(r) for r in rows],
                "columns": COLUMNS[kind],
            }
        finally:
            conn.close()

    @staticmethod
    def _release_dict(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        return {
            "id": d["id"],
            "batch_id": d["batch_id"],
            "batch_label": d["batch_label"],
            "seal_id": d["seal_id"],
            "label": d["label"],
            "state": d["state"],
            "created_at": d["created_at"],
            "activated_at": d["activated_at"],
            "prev_release_id": d["prev_release_id"],
            "customer_count": d["customer_count"],
            "contract_count": d["contract_count"],
            "invoice_count": d["invoice_count"],
        }

    # ------------------------------------------------------------------
    # Rejection report download
    # ------------------------------------------------------------------
    def rejections_csv(self, batch_id: str, seal_id: str | None = None) -> str:
        conn = self._conn()
        try:
            batch = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                raise ServiceError("批次不存在", 404, "batch_not_found")
            target = seal_id or batch["latest_seal_id"]
            if not target:
                raise ServiceError("该批次尚未封口，暂无拒绝原因", 404, "no_seal")
            seal = conn.execute(
                "SELECT * FROM seals WHERE id=? AND batch_id=?", (target, batch_id)
            ).fetchone()
            if seal is None:
                raise ServiceError("封口记录不存在", 404, "seal_not_found")
            out = io.StringIO()
            out.write("﻿")
            writer = csv.writer(out, lineterminator="\r\n")
            writer.writerow(
                ["严重级别", "规则", "数据类型", "文件名", "行号", "字段", "值", "拒绝原因", "关联位置"]
            )

            def safe(value):
                # Prevent spreadsheet formula injection from CSV-controlled text.
                text = "" if value is None else str(value)
                if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
                    return "'" + text
                return text

            for r in conn.execute(
                "SELECT * FROM seal_issues WHERE seal_id=? ORDER BY"
                " CASE severity WHEN 'error' THEN 0 ELSE 1 END, filename, line_no, id",
                (target,),
            ).fetchall():
                related = ""
                if r["related_json"]:
                    payload = json.loads(r["related_json"])
                    if payload.get("first_file"):
                        related = f"{payload['first_file']}:{payload.get('first_line')}"
                    elif payload.get("invoices"):
                        related = "; ".join(
                            f"{i['filename']}:{i['line_no']}" for i in payload["invoices"]
                        )
                    elif payload:
                        related = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                writer.writerow(
                    [
                        "错误" if r["severity"] == "error" else "警告",
                        r["rule"],
                        KIND_LABELS.get(r["kind"], "") if r["kind"] else "",
                        safe(r["filename"] or ""),
                        r["line_no"] if r["line_no"] is not None else "",
                        safe(r["field"] or ""),
                        safe(r["value"]),
                        safe(r["message"]),
                        safe(related),
                    ]
                )
            return out.getvalue()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def _event(self, conn: sqlite3.Connection, batch_id: str, event: str, detail: str) -> None:
        conn.execute(
            "INSERT INTO batch_events (batch_id, at, event, detail) VALUES (?, ?, ?, ?)",
            (batch_id, utc_now(), event, detail),
        )

    def events(self, batch_id: str) -> list[dict]:
        conn = self._conn()
        try:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM batch_events WHERE batch_id=? ORDER BY id DESC", (batch_id,)
                ).fetchall()
            ]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def reconcile_interrupted(self) -> int:
        """Mark operations left 'running' by a process crash as interrupted."""
        conn = self._conn()
        try:
            with transaction(conn):
                cur = conn.execute(
                    "UPDATE publish_ops SET status='interrupted', phase='unknown', updated_at=?"
                    " WHERE status='running'",
                    (utc_now(),),
                )
                return cur.rowcount
        finally:
            conn.close()
