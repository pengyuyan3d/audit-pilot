"""SQLite 数据层：管理财务流水表、合同条款表、全链路审计追踪表与 CPA 人工复核表。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from backend.config import DATA_DIR, DB_PATH


def ensure_db_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_connection(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    ensure_db_dir()
    conn = sqlite3.connect(str(db_path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    with get_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ledger_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_no TEXT NOT NULL,
                payment_date TEXT NOT NULL,
                payment_time TEXT NOT NULL,
                payee_name TEXT NOT NULL,
                bank_account TEXT NOT NULL,
                bank_account_norm TEXT NOT NULL,
                tax_id TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                contract_no TEXT DEFAULT '',
                approver TEXT NOT NULL,
                remark TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_ledger_payee ON ledger_transactions(payee_name);
            CREATE INDEX IF NOT EXISTS idx_ledger_date ON ledger_transactions(payment_date);
            CREATE INDEX IF NOT EXISTS idx_ledger_contract ON ledger_transactions(contract_no);

            CREATE TABLE IF NOT EXISTS contract_documents (
                doc_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                contract_no TEXT DEFAULT '',
                counterparty TEXT DEFAULT '',
                contract_amount REAL DEFAULT 0,
                designated_account TEXT DEFAULT '',
                designated_account_norm TEXT DEFAULT '',
                raw_text TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS contract_clauses (
                chunk_id TEXT PRIMARY KEY,
                doc_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                contract_no TEXT DEFAULT '',
                page INTEGER NOT NULL,
                clause_no TEXT NOT NULL,
                heading TEXT NOT NULL,
                text TEXT NOT NULL,
                FOREIGN KEY (doc_id) REFERENCES contract_documents(doc_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS audit_traces (
                trace_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                user_query TEXT NOT NULL,
                intent TEXT NOT NULL,
                route_path TEXT NOT NULL,
                model_used TEXT NOT NULL,
                failover_triggered INTEGER DEFAULT 0,
                cache_hit INTEGER DEFAULT 0,
                ttft_ms REAL DEFAULT 0,
                total_ms REAL DEFAULT 0,
                sql_executed TEXT DEFAULT '[]',
                evidence_json TEXT DEFAULT '[]',
                masking_json TEXT DEFAULT '{}',
                masked_prompt TEXT DEFAULT '',
                final_answer TEXT DEFAULT '',
                cpa_review_status TEXT DEFAULT 'pending',
                cpa_review_note TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS cpa_reviews (
                review_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                note TEXT DEFAULT '',
                reviewer TEXT DEFAULT 'CPA_Auditor',
                created_at TEXT NOT NULL,
                FOREIGN KEY (trace_id) REFERENCES audit_traces(trace_id) ON DELETE CASCADE
            );
            """
        )


def clear_ledger(db_path: Path = DB_PATH) -> None:
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM ledger_transactions")


def clear_contracts(db_path: Path = DB_PATH) -> None:
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM contract_clauses")
        conn.execute("DELETE FROM contract_documents")


def save_trace(trace: dict[str, Any], db_path: Path = DB_PATH) -> str:
    trace_id = trace.get("trace_id") or f"TRC-{uuid.uuid4().hex[:10].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO audit_traces (
                trace_id, created_at, user_query, intent, route_path,
                model_used, failover_triggered, cache_hit, ttft_ms, total_ms,
                sql_executed, evidence_json, masking_json, masked_prompt, final_answer
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace_id,
                now,
                trace.get("user_query", ""),
                trace.get("intent", "unknown"),
                json.dumps(trace.get("route_path", []), ensure_ascii=False),
                trace.get("model_used", "demo-engine"),
                1 if trace.get("failover_triggered") else 0,
                1 if trace.get("cache_hit") else 0,
                round(float(trace.get("ttft_ms", 0.0)), 2),
                round(float(trace.get("total_ms", 0.0)), 2),
                json.dumps(trace.get("sql_executed", []), ensure_ascii=False),
                json.dumps(trace.get("evidence", []), ensure_ascii=False),
                json.dumps(trace.get("masking", {}), ensure_ascii=False),
                trace.get("masked_prompt", ""),
                trace.get("final_answer", ""),
            ),
        )
    return trace_id


def list_traces(limit: int = 30, db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM audit_traces ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_deserialize_trace(dict(r)) for r in rows]


def get_trace(trace_id: str, db_path: Path = DB_PATH) -> dict[str, Any] | None:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM audit_traces WHERE trace_id = ?",
            (trace_id,),
        ).fetchone()
    return _deserialize_trace(dict(row)) if row else None


def record_cpa_review(
    trace_id: str,
    decision: str,
    note: str = "",
    reviewer: str = "CPA_Auditor",
    db_path: Path = DB_PATH,
) -> dict[str, Any]:
    review_id = f"REV-{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO cpa_reviews (review_id, trace_id, decision, note, reviewer, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (review_id, trace_id, decision, note, reviewer, now),
        )
        conn.execute(
            """
            UPDATE audit_traces
            SET cpa_review_status = ?, cpa_review_note = ?
            WHERE trace_id = ?
            """,
            (decision, note, trace_id),
        )
    return {
        "review_id": review_id,
        "trace_id": trace_id,
        "decision": decision,
        "note": note,
        "reviewer": reviewer,
        "created_at": now,
    }


def export_flywheel_jsonl(fmt: str = "sft", db_path: Path = DB_PATH) -> str:
    """将已复核的审计 Trace 导出为标准 SFT 或 DPO JSONL 训练语料（ADR-4 数据飞轮）。"""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM audit_traces
            WHERE cpa_review_status IN ('confirmed', 'false_positive')
            ORDER BY created_at DESC
            """
        ).fetchall()

    lines: list[str] = []
    for raw in rows:
        t = _deserialize_trace(dict(raw))
        status = t["cpa_review_status"]
        note = t["cpa_review_note"] or ""
        if fmt == "dpo":
            if status == "confirmed":
                record = {
                    "prompt": t["user_query"],
                    "chosen": t["final_answer"],
                    "rejected": "（待补充未溯源或未复核的基线回答）",
                    "metadata": {"trace_id": t["trace_id"], "cpa_status": status, "note": note},
                }
            else:
                record = {
                    "prompt": t["user_query"],
                    "chosen": f"【CPA 复核纠偏】{note or '经人工复核该项不构成异常，已修正判断口径。'}",
                    "rejected": t["final_answer"],
                    "metadata": {"trace_id": t["trace_id"], "cpa_status": status, "note": note},
                }
        else:
            assistant_text = (
                t["final_answer"]
                if status == "confirmed"
                else f"【CPA 复核纠偏结论】{note or '经人工复核排除异常。'}"
            )
            record = {
                "messages": [
                    {"role": "system", "content": "你是企业级智能审计助手 AuditPilot，必须基于确定性账目计算与合同条款原文溯源作答。"},
                    {"role": "user", "content": t["user_query"]},
                    {"role": "assistant", "content": assistant_text},
                ],
                "metadata": {"trace_id": t["trace_id"], "cpa_status": status, "note": note},
            }
        lines.append(json.dumps(record, ensure_ascii=False))
    return "\n".join(lines)


def _deserialize_trace(row: dict[str, Any]) -> dict[str, Any]:
    for key, default in (
        ("route_path", []),
        ("sql_executed", []),
        ("evidence_json", []),
        ("masking_json", {}),
    ):
        try:
            parsed = json.loads(row.get(key) or json.dumps(default))
        except Exception:
            parsed = default
        if key == "evidence_json":
            row["evidence"] = parsed
        elif key == "masking_json":
            row["masking"] = parsed
        else:
            row[key] = parsed
    row["failover_triggered"] = bool(row.get("failover_triggered"))
    row["cache_hit"] = bool(row.get("cache_hit"))
    return row
