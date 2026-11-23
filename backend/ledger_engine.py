"""ADR-2 双层查账引擎：L1 确定性审计算子 + L2 SQLite Authorizer 只读 SQL 沙箱。"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from backend.config import DB_PATH
from backend.database import clear_ledger, get_connection
from backend.masking import normalize_account

HOLIDAYS_2026 = {
    "2026-01-01",
    "2026-01-02",
    "2026-01-03",
    "2026-05-01",
    "2026-05-02",
    "2026-05-03",
    "2026-05-04",
    "2026-05-05",
}

ALLOWED_SQLITE_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
}


def _readonly_authorizer(action: int, arg1: Any, arg2: Any, dbname: Any, source: Any) -> int:
    if action in ALLOWED_SQLITE_ACTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def ingest_ledger_file(file_path: Path | None = None, file_bytes: bytes | None = None, filename: str = "", db_path: Path = DB_PATH) -> dict[str, Any]:
    """导入 Excel (.xlsx) 或 CSV 财务明细流水到 SQLite。"""
    rows: list[dict[str, Any]] = []
    ext = (file_path.suffix if file_path else Path(filename).suffix).lower()

    if ext == ".csv":
        raw_text = (file_path.read_text(encoding="utf-8-sig") if file_path else (file_bytes or b"").decode("utf-8-sig"))
        reader = csv.DictReader(io.StringIO(raw_text))
        for r in reader:
            rows.append(dict(r))
    else:
        wb = load_workbook(filename=file_path if file_path else io.BytesIO(file_bytes or b""), data_only=True)
        ws = wb.active
        iter_rows = list(ws.iter_rows(values_only=True))
        if not iter_rows:
            return {"inserted": 0}
        headers = [str(h).strip() if h is not None else "" for h in iter_rows[0]]
        for vals in iter_rows[1:]:
            if not any(vals):
                continue
            rows.append({headers[i]: vals[i] for i in range(min(len(headers), len(vals)))})

    clear_ledger(db_path)
    inserted = 0
    with get_connection(db_path) as conn:
        for r in rows:
            voucher_no = str(r.get("voucher_no") or "").strip()
            if not voucher_no:
                continue
            bank_acct = str(r.get("bank_account") or "").strip()
            conn.execute(
                """
                INSERT INTO ledger_transactions (
                    voucher_no, payment_date, payment_time, payee_name,
                    bank_account, bank_account_norm, tax_id, amount,
                    category, contract_no, approver, remark
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    voucher_no,
                    str(r.get("payment_date") or "").strip(),
                    str(r.get("payment_time") or "09:00:00").strip(),
                    str(r.get("payee_name") or "").strip(),
                    bank_acct,
                    normalize_account(bank_acct),
                    str(r.get("tax_id") or "").strip(),
                    float(r.get("amount") or 0.0),
                    str(r.get("category") or "").strip(),
                    str(r.get("contract_no") or "").strip(),
                    str(r.get("approver") or "").strip(),
                    str(r.get("remark") or "").strip(),
                ),
            )
            inserted += 1

    return {"inserted": inserted}


def get_known_entities(db_path: Path = DB_PATH) -> list[str]:
    with get_connection(db_path) as conn:
        payees = [r[0] for r in conn.execute("SELECT DISTINCT payee_name FROM ledger_transactions").fetchall()]
        cps = [r[0] for r in conn.execute("SELECT DISTINCT counterparty FROM contract_documents WHERE counterparty != ''").fetchall()]
    return sorted({e for e in payees + cps + ["星辰智造科技有限公司"] if e})


def execute_readonly_sql(sql: str, db_path: Path = DB_PATH, max_rows: int = 100) -> dict[str, Any]:
    """L2 只读 SQL 沙箱：通过 SQLite Authorizer 底层拦截所有写/DDL/系统指令。"""
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise ValueError("SQL 语句为空")
    if ";" in cleaned:
        raise PermissionError("SQL 沙箱拒绝多语句执行")
    if not re.match(r"^(SELECT|WITH)\b", cleaned, re.IGNORECASE):
        raise PermissionError("SQL 沙箱仅允许 SELECT / WITH 只读查询")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.set_authorizer(_readonly_authorizer)
    try:
        cur = conn.execute(cleaned)
        rows = [dict(r) for r in cur.fetchmany(max_rows)]
        return {"sql": cleaned, "row_count": len(rows), "rows": rows}
    except sqlite3.DatabaseError as exc:
        raise PermissionError(f"SQL 沙箱拦截或语法错误: {exc}") from exc
    finally:
        conn.close()


# ==================== L1 确定性审计算子 ====================


def get_ledger_summary(db_path: Path = DB_PATH) -> dict[str, Any]:
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total_rows,
                   COALESCE(SUM(amount), 0) AS total_amount,
                   MIN(payment_date) AS min_date,
                   MAX(payment_date) AS max_date,
                   COUNT(DISTINCT payee_name) AS payee_count
            FROM ledger_transactions
            """
        ).fetchone()
        top_payees = [
            dict(r)
            for r in conn.execute(
                """
                SELECT payee_name, COUNT(*) AS tx_count, ROUND(SUM(amount), 2) AS total_amount
                FROM ledger_transactions
                GROUP BY payee_name
                ORDER BY total_amount DESC
                LIMIT 6
                """
            ).fetchall()
        ]
    return {
        "total_rows": row["total_rows"] if row else 0,
        "total_amount": round(float(row["total_amount"] or 0), 2) if row else 0.0,
        "min_date": row["min_date"] if row else "",
        "max_date": row["max_date"] if row else "",
        "payee_count": row["payee_count"] if row else 0,
        "top_payees": top_payees,
    }


def detect_split_payments(threshold: float = 200000.0, db_path: Path = DB_PATH) -> dict[str, Any]:
    """L1 算子：识别同日同收款方拆分付款规避审批阈值（单笔 < threshold 且当日合计 >= threshold）。"""
    sql = """
    SELECT payee_name, payment_date, COUNT(*) AS tx_count,
           ROUND(SUM(amount), 2) AS total_amount,
           ROUND(MAX(amount), 2) AS max_single_amount,
           GROUP_CONCAT(voucher_no, ', ') AS vouchers,
           GROUP_CONCAT(DISTINCT approver) AS approvers
    FROM ledger_transactions
    WHERE amount < ?
    GROUP BY payee_name, payment_date
    HAVING COUNT(*) >= 2 AND SUM(amount) >= ?
    ORDER BY total_amount DESC
    """
    with get_connection(db_path) as conn:
        rows = [dict(r) for r in conn.execute(sql, (threshold, threshold)).fetchall()]
    return {
        "rule": "split_payment_evasion",
        "threshold": threshold,
        "sql": sql.strip(),
        "findings": rows,
        "count": len(rows),
    }


def detect_duplicate_payments(day_window: int = 3, db_path: Path = DB_PATH) -> dict[str, Any]:
    """L1 算子：识别短窗口内同收款方、同金额的疑似重复付款。"""
    sql = """
    SELECT a.voucher_no AS voucher_1, a.payment_date AS date_1,
           b.voucher_no AS voucher_2, b.payment_date AS date_2,
           a.payee_name, a.amount, a.remark AS remark_1, b.remark AS remark_2,
           CAST(ABS(julianday(b.payment_date) - julianday(a.payment_date)) AS INTEGER) AS day_diff
    FROM ledger_transactions a
    JOIN ledger_transactions b
      ON a.payee_name = b.payee_name
     AND ABS(a.amount - b.amount) < 0.01
     AND a.id < b.id
     AND ABS(julianday(b.payment_date) - julianday(a.payment_date)) <= ?
    ORDER BY a.amount DESC
    """
    with get_connection(db_path) as conn:
        rows = [dict(r) for r in conn.execute(sql, (day_window,)).fetchall()]
    return {
        "rule": "duplicate_payment",
        "day_window": day_window,
        "sql": sql.strip(),
        "findings": rows,
        "count": len(rows),
    }


def detect_off_hours_payments(db_path: Path = DB_PATH) -> dict[str, Any]:
    """L1 算子：识别周末、法定节假日及非工作时段（早 8 点前 / 晚 20 点后）付款。"""
    sql = """
    SELECT voucher_no, payment_date, payment_time, payee_name, amount, approver, remark
    FROM ledger_transactions
    ORDER BY payment_date, payment_time
    """
    findings: list[dict[str, Any]] = []
    with get_connection(db_path) as conn:
        rows = [dict(r) for r in conn.execute(sql).fetchall()]

    for r in rows:
        reasons: list[str] = []
        d_str = r["payment_date"]
        t_str = r["payment_time"]
        try:
            d_obj = date.fromisoformat(d_str)
            if d_obj.weekday() >= 5:
                reasons.append("周末付款")
        except ValueError:
            pass
        if d_str in HOLIDAYS_2026:
            reasons.append("法定节假日付款")
        try:
            t_obj = datetime.strptime(t_str, "%H:%M:%S").time()
            if t_obj.hour < 8 or t_obj.hour >= 20:
                reasons.append(f"非工作时段({t_str})")
        except ValueError:
            pass
        if reasons:
            r["anomaly_reasons"] = reasons
            findings.append(r)

    return {
        "rule": "off_hours_or_holiday_payment",
        "sql": sql.strip(),
        "findings": findings,
        "count": len(findings),
    }


def run_all_ledger_anomalies(db_path: Path = DB_PATH) -> dict[str, Any]:
    split_res = detect_split_payments(db_path=db_path)
    dup_res = detect_duplicate_payments(db_path=db_path)
    off_res = detect_off_hours_payments(db_path=db_path)
    return {
        "split_payments": split_res,
        "duplicate_payments": dup_res,
        "off_hours_payments": off_res,
        "sql_executed": [split_res["sql"], dup_res["sql"], off_res["sql"]],
    }
