"""AuditPilot V2 自动化评测套件（12 条黄金审计用例 + BM25 vs Hybrid A/B + 缓存加速基准）。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.agent_graph import query_cache, rag_engine, run_audit_pipeline
from backend.config import DB_PATH
from backend.cross_verifier import run_cross_verification
from backend.ledger_engine import (
    detect_duplicate_payments,
    detect_off_hours_payments,
    detect_split_payments,
    execute_readonly_sql,
)
from backend.main import bootstrap_demo_data
from backend.masking import MaskingContext, StreamingUnmasker

EVAL_DIR = ROOT_DIR / "eval"


def run_benchmark() -> dict:
    bootstrap_demo_data(force=False)
    cases: list[dict] = []

    def add_case(cid: str, category: str, name: str, passed: bool, detail: str) -> None:
        cases.append({"id": cid, "category": category, "name": name, "passed": bool(passed), "detail": detail})

    # ==================== 一、财务流水与 SQL 沙箱（4 用例） ====================
    split = detect_split_payments(db_path=DB_PATH)
    s_ok = split["count"] == 1 and split["findings"][0]["payee_name"] == "盛达物流有限公司" and abs(split["findings"][0]["total_amount"] - 545000.0) < 0.01
    add_case("L-01", "L1审计算子", "识别盛达物流同日拆分付款规避20万审批阈值", s_ok, f"检出 {split['count']} 组，合计 ¥{split['findings'][0]['total_amount'] if split['findings'] else 0:,.2f}")

    dup = detect_duplicate_payments(db_path=DB_PATH)
    d_ok = dup["count"] == 1 and dup["findings"][0]["payee_name"] == "恒信模具加工厂" and abs(dup["findings"][0]["amount"] - 86500.0) < 0.01
    add_case("L-02", "L1审计算子", "识别恒信模具连续两日重复付款 86,500 元", d_ok, f"检出 {dup['count']} 组，单笔 ¥{dup['findings'][0]['amount'] if dup['findings'] else 0:,.2f}")

    off = detect_off_hours_payments(db_path=DB_PATH)
    off_total = sum(float(x["amount"]) for x in off["findings"])
    o_ok = off["count"] == 2 and abs(off_total - 363000.0) < 0.01
    add_case("L-03", "L1审计算子", "识别五一节假日与周日深夜大额付款（2笔/36.3万）", o_ok, f"检出 {off['count']} 笔，合计 ¥{off_total:,.2f}")

    blocked = False
    block_msg = ""
    try:
        execute_readonly_sql("DELETE FROM ledger_transactions; DROP TABLE ledger_transactions;", DB_PATH)
    except PermissionError as exc:
        blocked = True
        block_msg = str(exc)
    add_case("L-04", "L2 SQL沙箱", "Authorizer 底层拦截恶意 DELETE/DROP 注入", blocked, block_msg or "未拦截")

    # ==================== 二、合同混合 RAG vs 纯 BM25 A/B 对比（4 用例） ====================
    rag_queries = [
        ("R-01", "采购方延迟打款每天罚多少违约金有没有封顶", "CT-2026-001", "第4条"),
        ("R-02", "铝材框架采购金额上限和补充协议要求", "CT-2026-001", "第2条"),
        ("R-03", "检测设备保修时长以及软件升级年费比例", "CT-2026-002", "第3条"),
        ("R-04", "设备运行产生的产线工艺数据归谁所有能否导出", "CT-2026-002", "第4条"),
    ]
    hybrid_hits_cnt = 0
    bm25_hits_cnt = 0
    for cid, q, exp_cno, exp_clause in rag_queries:
        h_res = rag_engine.search(q, top_k=3, mode="hybrid")
        b_res = rag_engine.search(q, top_k=3, mode="bm25_only")
        h_hit = any(r["contract_no"] == exp_cno and r["clause_no"] == exp_clause for r in h_res)
        b_hit = any(r["contract_no"] == exp_cno and r["clause_no"] == exp_clause for r in b_res)
        if h_hit:
            hybrid_hits_cnt += 1
        if b_hit:
            bm25_hits_cnt += 1
        top_h = f"{h_res[0]['contract_no']}:{h_res[0]['clause_no']}({h_res[0]['relevance_score']})" if h_res else "无"
        top_b = f"{b_res[0]['contract_no']}:{b_res[0]['clause_no']}({b_res[0]['bm25_score']})" if b_res else "无"
        add_case(cid, "合同混合RAG", f"条款精准溯源：{exp_cno} {exp_clause}", h_hit, f"HybridTop1={top_h} | BM25Top1={top_b} (BM25命中={b_hit})")

    # ==================== 三、账同交叉核对与脱敏零泄露（4 用例） ====================
    cross = run_cross_verification(DB_PATH)
    over_ok = len(cross["overpayments"]) == 1 and cross["overpayments"][0]["contract_no"] == "CT-2026-001" and abs(cross["overpayments"][0]["excess_amount"] - 380000.0) < 0.01
    add_case("C-01", "账同交叉核对", "识别 CT-2026-001 超合同总额付款 380,000 元", over_ok, f"检出 {len(cross['overpayments'])} 项超付")

    acc_ok = (
        len(cross["account_mismatches"]) == 1
        and cross["account_mismatches"][0]["voucher_no"] == "PZ-20260528-907"
        and all(v["mismatched_account_txs"] == 0 for v in cross["verified_contracts"] if v["contract_no"] == "CT-2026-001")
    )
    add_case("C-02", "账同交叉核对", "识别非约定账号打款且归一化零误报", acc_ok, f"异常账号 {len(cross['account_mismatches'])} 笔，CT-2026-001 带空格格式 0 误报")

    # 实体归一化与流式还原校验
    mctx = MaskingContext(known_entities=["华兴供应链管理有限公司"])
    sample_text = "华兴供应链管理有限公司 合同账号 6222 0210 0112 3456 789 流水账号 6222021001123456789 手机 13987654321 税号 91320594MA2233445B"
    masked = mctx.mask(sample_text)
    leaks = mctx.verify_zero_leakage(masked)
    same_placeholder = masked.count("[BANK_ACCT_1]") == 2
    # 模拟逐 3 字符 SSE 切块流式还原
    unmasker = StreamingUnmasker(mctx)
    restored = "".join(unmasker.feed(masked[i : i + 3]) for i in range(0, len(masked), 3)) + unmasker.flush()
    m_ok = len(leaks) == 0 and same_placeholder and ("6222021001123456789" in restored) and ("[BANK_ACCT_1]" not in restored)
    add_case("C-03", "ADR-1 脱敏网关", "跨格式账号归一化一致 + 流式还原无残留", m_ok, f"泄露={len(leaks)}, 同占位符={same_placeholder}")

    # 端到端网关外发零泄露 + 缓存冷热延迟基准
    query_cache.clear()
    t0 = time.perf_counter()
    _, pipe_ctx, masked_prompt, hit1 = run_audit_pipeline("全面综合审计", bypass_cache=True)
    cold_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    t1 = time.perf_counter()
    _, _, _, hit2 = run_audit_pipeline("全面综合审计", bypass_cache=False)
    warm_ms = round((time.perf_counter() - t1) * 1000.0, 2)

    pipe_leaks = pipe_ctx.verify_zero_leakage(masked_prompt)
    c4_ok = len(pipe_leaks) == 0 and (not hit1) and hit2 and warm_ms < cold_ms
    add_case("C-04", "端到端安全与性能", "全量审计外发 Prompt 零泄露 + TTL 缓存加速", c4_ok, f"泄露={len(pipe_leaks)}, 冷启动={cold_ms}ms -> 缓存命中={warm_ms}ms")

    passed_cnt = sum(1 for c in cases if c["passed"])
    report = {
        "summary": {
            "total": len(cases),
            "passed": passed_cnt,
            "pass_rate": round(passed_cnt / len(cases) * 100.0, 1),
            "pii_leak_count": len(leaks) + len(pipe_leaks),
            "hybrid_recall_at_3": round(hybrid_hits_cnt / len(rag_queries) * 100.0, 1),
            "bm25_recall_at_3": round(bm25_hits_cnt / len(rag_queries) * 100.0, 1),
            "cold_ms": cold_ms,
            "warm_ms": warm_ms,
        },
        "cases": cases,
    }

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    (EVAL_DIR / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    md_lines = [
        "# AuditPilot V2 自动化评测报告",
        "",
        f"- **总通过率**：{passed_cnt}/{len(cases)} ({report['summary']['pass_rate']}%)",
        f"- **PII 外发泄露数**：{report['summary']['pii_leak_count']}",
        f"- **合同检索 Recall@3 A/B 对比**：Hybrid `{report['summary']['hybrid_recall_at_3']}%` vs BM25-only `{report['summary']['bm25_recall_at_3']}%`",
        f"- **TTL 查询缓存延迟对比**：冷启动 `{cold_ms} ms` → 缓存命中 `{warm_ms} ms`",
        "",
        "| ID | 类别 | 用例 | 结果 | 详情 |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for c in cases:
        md_lines.append(f"| `{c['id']}` | {c['category']} | {c['name']} | {'✅ PASS' if c['passed'] else '❌ FAIL'} | {c['detail']} |")
    (EVAL_DIR / "report.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    rep = run_benchmark()
    s = rep["summary"]
    print(f"=== AuditPilot V2 Eval: {s['passed']}/{s['total']} ({s['pass_rate']}%) | PII Leaks: {s['pii_leak_count']} | Hybrid vs BM25 R@3: {s['hybrid_recall_at_3']}% vs {s['bm25_recall_at_3']}% | Cache: {s['cold_ms']}ms -> {s['warm_ms']}ms ===")
    for c in rep["cases"]:
        print(f"[{'PASS' if c['passed'] else 'FAIL'}] {c['id']} {c['name']} -> {c['detail']}")
    if s["passed"] != s["total"]:
        sys.exit(1)
