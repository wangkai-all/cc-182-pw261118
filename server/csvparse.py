"""CSV parsing and per-file structural/row-level validation."""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field

KINDS = ("customers", "contracts", "invoices")

COLUMNS = {
    "customers": ["customer_id", "name", "region", "contact_email"],
    "contracts": ["contract_id", "customer_id", "title", "amount", "signed_on"],
    "invoices": ["invoice_id", "contract_id", "amount", "issued_on"],
}

ID_PATTERNS = {
    "customers": re.compile(r"^CUST-[0-9]{4}$"),
    "contracts": re.compile(r"^CTR-[0-9]{4}$"),
    "invoices": re.compile(r"^INV-[0-9]{4}$"),
}

ID_FIELD = {
    "customers": "customer_id",
    "contracts": "contract_id",
    "invoices": "invoice_id",
}

KIND_LABELS = {
    "customers": "客户",
    "contracts": "合同",
    "invoices": "发票",
}

MAX_BYTES = 512 * 1024
MAX_ROWS = 2000

_MONEY = re.compile(r"^\d{1,12}(\.\d{1,2})?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class Issue:
    rule: str
    message: str
    severity: str = "error"
    kind: str | None = None
    filename: str | None = None
    line_no: int | None = None
    field_name: str | None = None
    value: str | None = None
    related: dict | None = None

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "rule": self.rule,
            "kind": self.kind,
            "filename": self.filename,
            "line_no": self.line_no,
            "field": self.field_name,
            "value": self.value,
            "message": self.message,
            "related": self.related or {},
        }


@dataclass
class ParsedRow:
    line_no: int
    data: dict | None
    raw: dict
    field_errors: list[str] = field(default_factory=list)


@dataclass
class ParsedFile:
    kind: str
    rows: list[ParsedRow]
    issues: list[Issue]
    fatal: bool = False
    header_error: bool = False


def parse_money(raw: str) -> int | None:
    text = (raw or "").strip()
    if not _MONEY.match(text):
        return None
    whole, _, frac = text.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


def parse_csv(kind: str, filename: str, content: str) -> ParsedFile:
    """Parse and lint one CSV file. Never raises for bad content."""
    if kind not in COLUMNS:
        raise ValueError(f"unknown kind {kind!r}")
    issues: list[Issue] = []

    def add(rule: str, message: str, **kw):
        issues.append(Issue(rule=rule, message=message, kind=kind, filename=filename, **kw))

    if len(content.encode("utf-8", "replace")) > MAX_BYTES:
        add("file.too_large", f"文件超过 {MAX_BYTES // 1024} KiB 上限，请拆分后上传")
        return ParsedFile(kind, [], issues, fatal=True)

    if content.startswith("﻿"):
        content = content[1:]

    # csv.reader.line_num gives the physical line count after each record, so
    # the start line of a record is the previous line_num + 1. This stays
    # correct when quoted fields contain embedded newlines.
    records: list[tuple[int, list[str]]] = []
    prev_line = 0
    try:
        reader = csv.reader(io.StringIO(content, newline=""))
        for cells in reader:
            records.append((prev_line + 1, cells))
            prev_line = reader.line_num
    except csv.Error as exc:
        add("file.unreadable_csv", f"无法解析 CSV：{exc}")
        return ParsedFile(kind, [], issues, fatal=True)

    if not records:
        add("file.empty", "文件为空，至少需要表头与一行数据", line_no=1)
        return ParsedFile(kind, [], issues, fatal=True)

    header_line, header = records[0]
    header = [h.strip().lower() for h in header]
    expected = COLUMNS[kind]
    if header != expected:
        add(
            "file.header_mismatch",
            f"表头不符合{KIND_LABELS[kind]}数据契约：期望 {', '.join(expected)}；"
            f"实际 {', '.join(header) or '(空)'}",
            line_no=header_line,
        )
        return ParsedFile(kind, [], issues, fatal=True, header_error=True)

    if len(records) == 1:
        add("file.no_data", "文件只有表头，没有任何数据行", line_no=header_line)
        return ParsedFile(kind, [], issues, fatal=True)

    if len(records) - 1 > MAX_ROWS:
        add("file.too_many_rows", f"数据行超过 {MAX_ROWS} 行上限", line_no=MAX_ROWS + 2)
        return ParsedFile(kind, [], issues, fatal=True)

    rows: list[ParsedRow] = []
    for line_no, cells in records[1:]:
        if not cells or all(not c.strip() for c in cells):
            add("row.empty", "空行，已忽略；建议从文件中删除", line_no=line_no, severity="warning")
            continue
        raw = {col: (cells[i] if i < len(cells) else "") for i, col in enumerate(expected)}
        if len(cells) > len(expected):
            add(
                "row.extra_columns",
                f"该行有 {len(cells)} 个字段，超过表头定义的 {len(expected)} 个",
                line_no=line_no,
            )
            rows.append(ParsedRow(line_no, None, raw, ["字段数过多"]))
            continue
        if len(cells) < len(expected):
            add(
                "row.missing_columns",
                f"该行只有 {len(cells)} 个字段，需要 {len(expected)} 个",
                line_no=line_no,
            )
            rows.append(ParsedRow(line_no, None, raw, ["字段数不足"]))
            continue
        data, row_errors = _lint_row(kind, raw, line_no, add)
        rows.append(ParsedRow(line_no, data, raw, row_errors))

    # Primary-key uniqueness is deliberately *not* checked here: the batch is
    # allowed to arrive in any order and uniqueness is a seal-time rule.
    return ParsedFile(kind, rows, issues)


def _lint_row(kind: str, raw: dict, line_no: int, add) -> tuple[dict | None, list[str]]:
    """Validate one row. Returns (clean data or None, human field errors)."""
    errors: list[str] = []
    clean: dict = {}

    def err(rule: str, field_name: str, message: str, value: str | None = None):
        errors.append(field_name)
        add(rule, message, line_no=line_no, field_name=field_name, value=value)

    for col in COLUMNS[kind]:
        if not raw.get(col, "").strip():
            err("row.required", col, f"{col} 不能为空", raw.get(col, ""))

    id_field = ID_FIELD[kind]
    ident = raw.get(id_field, "").strip()
    if ident and not ID_PATTERNS[kind].match(ident):
        err(
            "row.id_format",
            id_field,
            f"{id_field} 必须形如 {_id_sample(kind)}（大写前缀 + 4 位数字）",
            ident,
        )
    clean[id_field] = ident

    if kind == "customers":
        clean["name"] = raw.get("name", "").strip()
        clean["region"] = raw.get("region", "").strip()
        email = raw.get("contact_email", "").strip()
        if email and not _EMAIL.match(email):
            err("row.email_format", "contact_email", "contact_email 不是合法邮箱地址", email)
        clean["contact_email"] = email

    elif kind == "contracts":
        cid = raw.get("customer_id", "").strip()
        if cid and not ID_PATTERNS["customers"].match(cid):
            err("row.id_format", "customer_id", "customer_id 必须形如 CUST-0001", cid)
        clean["customer_id"] = cid
        clean["title"] = raw.get("title", "").strip()
        amount_raw = raw.get("amount", "").strip()
        amount = parse_money(amount_raw)
        if amount_raw and amount is None:
            err(
                "row.money_format",
                "amount",
                "amount 必须是最多两位小数的非负金额（如 12500.00）",
                amount_raw,
            )
        clean["amount_cents"] = amount
        signed_on = raw.get("signed_on", "").strip()
        if signed_on and not _DATE.match(signed_on):
            err("row.date_format", "signed_on", "signed_on 必须是 YYYY-MM-DD", signed_on)
        clean["signed_on"] = signed_on

    elif kind == "invoices":
        cid = raw.get("contract_id", "").strip()
        if cid and not ID_PATTERNS["contracts"].match(cid):
            err("row.id_format", "contract_id", "contract_id 必须形如 CTR-0001", cid)
        clean["contract_id"] = cid
        amount_raw = raw.get("amount", "").strip()
        amount = parse_money(amount_raw)
        if amount_raw and amount is None:
            err(
                "row.money_format",
                "amount",
                "amount 必须是最多两位小数的非负金额（如 12500.00）",
                amount_raw,
            )
        clean["amount_cents"] = amount
        issued_on = raw.get("issued_on", "").strip()
        if issued_on and not _DATE.match(issued_on):
            err("row.date_format", "issued_on", "issued_on 必须是 YYYY-MM-DD", issued_on)
        clean["issued_on"] = issued_on

    if errors:
        return None, list(dict.fromkeys(errors))
    return clean, []


def _id_sample(kind: str) -> str:
    return {"customers": "CUST-0001", "contracts": "CTR-0001", "invoices": "INV-0001"}[kind]


def rows_to_json(parsed: ParsedFile) -> list[dict]:
    return [
        {
            "line_no": row.line_no,
            "data": row.data,
            "raw": row.raw,
            "field_errors": row.field_errors,
        }
        for row in parsed.rows
    ]


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
