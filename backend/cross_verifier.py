"""跨文件账同交叉核对引擎：比对合同约定金额/唯一收款账号与实际财务付款流水。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.config import DB_PATH
from backend.database import get_connection


def run_cross_verification(db_path: Path = DB_PATH) -> dict[str, Any]:
    """执行全量账同交叉核对（超合同额付款 + 非约定收款账户打款）。"""
    overpayments: list[dict[str, Any]] = []
    account_mismatches: list[dict[str, Any]] = []
    verified_contracts: list[dict[str, Any]] = []

    sql_summary = """
    SELECT contract_no, COUNT(*) AS tx_count, ROUND(SUM(amount), 2) AS total_paid
    FROM ledger_transactions
    WHERE contract_no != ''
    GROUP BY contract_no
    """
    sql_accounts = """
    SELECT voucher_no, payment_date, payee_name, bank_account, bank_account_norm, amount, contract_no, approver, remark
    FROM ledger_transactions
    WHERE contract_no = ?
    ORDER BY payment_date
    """

    with get_connection(db_path) as conn:
        contracts = [dict(r) for r in conn.execute("SELECT * FROM contract_documents ORDER BY contract_no").fetchall()]
        paid_map = {
            r["contract_no"]: dict(r)
            for r in conn.execute(sql_summary).fetchall()
        }

        for c in contracts:
            cno = c["contract_no"]
            limit_amt = float(c["contract_amount"] or 0.0)
            expected_acc_norm = c["designated_account_norm"] or ""
            paid_info = paid_map.get(cno, {"tx_count": 0, "total_paid": 0.0})
            total_paid = float(paid_info["total_paid"] or 0.0)

            txs = [dict(r) for r in conn.execute(sql_accounts, (cno,)).fetchall()]
            matched_cnt = 0
            mismatch_cnt = 0

            for tx in txs:
                if expected_acc_norm and tx["bank_account_norm"] != expected_acc_norm:
                    mismatch_cnt += 1
                    account_mismatches.append(
                        {
                            "contract_no": cno,
                            "filename": c["filename"],
                            "doc_id": c["doc_id"],
                            "voucher_no": tx["voucher_no"],
                            "payment_date": tx["payment_date"],
                            "payee_name": tx["payee_name"],
                            "amount": tx["amount"],
                            "expected_account": c["designated_account"],
                            "expected_account_norm": expected_acc_norm,
                            "actual_account": tx["bank_account"],
                            "actual_account_norm": tx["bank_account_norm"],
                            "approver": tx["approver"],
                            "remark": tx["remark"],
                        }
                    )
                else:
                    matched_cnt += 1

            excess = round(total_paid - limit_amt, 2)
            if limit_amt > 0 and excess > 0.01:
                overpayments.append(
                    {
                        "contract_no": cno,
                        "filename": c["filename"],
                        "doc_id": c["doc_id"],
                        "counterparty": c["counterparty"],
                        "contract_amount": limit_amt,
                        "actual_paid": total_paid,
                        "excess_amount": excess,
                        "tx_count": paid_info["tx_count"],
                        "vouchers": [t["voucher_no"] for t in txs],
                    }
                )

            verified_contracts.append(
                {
                    "contract_no": cno,
                    "counterparty": c["counterparty"],
                    "contract_amount": limit_amt,
                    "actual_paid": total_paid,
                    "excess_amount": max(0.0, excess),
                    "matched_account_txs": matched_cnt,
                    "mismatched_account_txs": mismatch_cnt,
                }
            )

    return {
        "verified_contracts": verified_contracts,
        "overpayments": overpayments,
        "account_mismatches": account_mismatches,
        "sql_executed": [sql_summary.strip(), sql_accounts.strip()],
    }
