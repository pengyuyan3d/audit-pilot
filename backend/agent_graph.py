"""LangGraph 显式状态图编排引擎 + TTL 查询缓存。

状态图流转：
  intent_router -> [ledger_node | rag_node | cross_verify_node | full_audit_node] -> prepare_gateway_node -> END
随后由 FastAPI SSE 端点通过 llm_gateway 流式吐字并本地还原，彻底规避流式 Tool Calling 兼容风险。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.config import DB_PATH, runtime_config
from backend.cross_verifier import run_cross_verification
from backend.ledger_engine import (
    execute_readonly_sql,
    get_known_entities,
    get_ledger_summary,
    run_all_ledger_anomalies,
)
from backend.masking import MaskingContext
from backend.rag_engine import HybridRAGEngine


class AuditState(TypedDict, total=False):
    user_query: str
    intent: str
    route_path: list[str]
    steps_log: list[dict[str, str]]
    ledger_data: dict[str, Any]
    rag_hits: list[dict[str, Any]]
    cross_data: dict[str, Any]
    sql_result: dict[str, Any] | None
    sql_executed: list[str]
    evidence: list[dict[str, Any]]
    raw_context_prompt: str


class TTLCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, dict[str, Any]]] = {}

    def _key(self, query: str) -> str:
        return hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()

    def get(self, query: str) -> dict[str, Any] | None:
        k = self._key(query)
        entry = self._store.get(k)
        if not entry:
            return None
        expires_at, payload = entry
        if time.time() > expires_at:
            self._store.pop(k, None)
            return None
        return payload

    def set(self, query: str, payload: dict[str, Any], ttl: int | None = None) -> None:
        ttl_s = ttl if ttl is not None else runtime_config.query_cache_ttl_seconds
        self._store[self._key(query)] = (time.time() + ttl_s, payload)

    def clear(self) -> None:
        self._store.clear()


query_cache = TTLCache()
rag_engine = HybridRAGEngine(DB_PATH)


def classify_intent(query: str) -> str:
    q = query.strip()
    if re.match(r"^(SELECT|WITH)\b", q, re.IGNORECASE):
        return "sql_query"
    has_cross = any(k in q for k in ("交叉", "超付", "超合同", "非约定", "收款账号", "账户不符", "账同", "比对"))
    has_contract = any(k in q for k in ("合同", "条款", "违约", "质保", "知识产权", "风险条款", "CT-2026"))
    has_ledger = any(k in q for k in ("流水", "拆分", "重复", "周末", "节假日", "深夜", "异常付款", "财务明细"))
    has_full = any(k in q for k in ("全面", "全量", "一键", "综合", "全部异常"))

    if has_full or (has_cross and has_ledger and has_contract):
        return "full_audit"
    if has_cross:
        return "cross_verify"
    if has_contract and not has_ledger:
        return "contract_rag"
    if has_ledger and not has_contract:
        return "ledger_anomaly"
    return "full_audit"


def _node_intent_router(state: AuditState) -> AuditState:
    intent = classify_intent(state["user_query"])
    return {
        "intent": intent,
        "route_path": ["intent_router"],
        "steps_log": [{"node": "intent_router", "message": f"意图识别完成 → 路由至 `{intent}`"}],
        "sql_executed": [],
        "evidence": [],
    }


def _node_sql_query(state: AuditState) -> AuditState:
    res = execute_readonly_sql(state["user_query"], DB_PATH)
    return {
        "sql_result": res,
        "sql_executed": [res["sql"]],
        "route_path": state.get("route_path", []) + ["sql_sandbox"],
        "steps_log": state.get("steps_log", []) + [
            {"node": "sql_sandbox", "message": f"L2 只读 SQL 沙箱执行成功，返回 {res['row_count']} 条记录"}
        ],
    }


def _node_ledger(state: AuditState) -> AuditState:
    anomalies = run_all_ledger_anomalies(DB_PATH)
    summary = get_ledger_summary(DB_PATH)
    anomalies["summary"] = summary
    return {
        "ledger_data": anomalies,
        "sql_executed": state.get("sql_executed", []) + anomalies["sql_executed"],
        "route_path": state.get("route_path", []) + ["ledger_engine"],
        "steps_log": state.get("steps_log", []) + [
            {
                "node": "ledger_engine",
                "message": (
                    f"L1 审计算子扫描 {summary['total_rows']} 条流水：拆分付款 {anomalies['split_payments']['count']} 组、"
                    f"重复付款 {anomalies['duplicate_payments']['count']} 组、非工作时段 {anomalies['off_hours_payments']['count']} 笔"
                ),
            }
        ],
    }


def _node_rag(state: AuditState) -> AuditState:
    hits = rag_engine.search(state["user_query"], top_k=4, mode="hybrid")
    return {
        "rag_hits": hits,
        "evidence": state.get("evidence", []) + hits,
        "route_path": state.get("route_path", []) + ["hybrid_rag"],
        "steps_log": state.get("steps_log", []) + [
            {"node": "hybrid_rag", "message": f"混合检索（BM25 + Dense RRF + 精排）命中 {len(hits)} 条合同原文条款"}
        ],
    }


def _node_cross_verify(state: AuditState) -> AuditState:
    cross = run_cross_verification(DB_PATH)
    # 同步检索对应合同金额与收款账号条款，保证交叉核对也有原文证据高亮
    hits = rag_engine.search("合同总金额上限 指定收款账号 变更收款账户", top_k=4, mode="hybrid")
    return {
        "cross_data": cross,
        "rag_hits": hits,
        "evidence": state.get("evidence", []) + hits,
        "sql_executed": state.get("sql_executed", []) + cross["sql_executed"],
        "route_path": state.get("route_path", []) + ["cross_verifier", "hybrid_rag"],
        "steps_log": state.get("steps_log", []) + [
            {
                "node": "cross_verifier",
                "message": (
                    f"账同交叉核对完成：检出超合同总额付款 {len(cross['overpayments'])} 项、"
                    f"非约定账户打款 {len(cross['account_mismatches'])} 笔（账号格式已归一化）"
                ),
            }
        ],
    }


def _node_full_audit(state: AuditState) -> AuditState:
    s1 = _node_ledger(state)
    merged1 = {**state, **s1}
    s2 = _node_cross_verify(merged1)
    merged2 = {**merged1, **s2}
    # 补充检索高风险违约/排他条款
    extra_hits = rag_engine.search("高风险条款 违约金 单方面上调 知识产权 质保期", top_k=3, mode="hybrid")
    seen = {h["chunk_id"] for h in merged2.get("rag_hits", [])}
    combined_hits = list(merged2.get("rag_hits", []))
    for h in extra_hits:
        if h["chunk_id"] not in seen:
            combined_hits.append(h)
            seen.add(h["chunk_id"])
    merged2["rag_hits"] = combined_hits[:5]
    merged2["evidence"] = combined_hits[:5]
    return merged2


def _node_prepare_context(state: AuditState) -> AuditState:
    parts = [f"【用户审计提问】\n{state['user_query']}"]
    if state.get("sql_result"):
        parts.append("【L2 只读 SQL 沙箱结果】\n" + json.dumps(state["sql_result"], ensure_ascii=False, indent=2))
    if state.get("cross_data"):
        parts.append("【账同交叉核对结果】\n" + json.dumps(state["cross_data"], ensure_ascii=False, indent=2))
    if state.get("ledger_data"):
        slim_ledger = {
            k: v for k, v in state["ledger_data"].items() if k != "sql_executed"
        }
        parts.append("【L1 财务流水异常筛查结果】\n" + json.dumps(slim_ledger, ensure_ascii=False, indent=2))
    if state.get("rag_hits"):
        rag_lines = [
            f"[证据:{h['chunk_id']}] {h['filename']} 第{h['page']}页 {h['clause_no']} {h['heading']} (精排分:{h['relevance_score']})\n{h['text']}"
            for h in state["rag_hits"]
        ]
        parts.append("【合同混合检索证据】\n" + "\n\n".join(rag_lines))

    return {
        "raw_context_prompt": "\n\n".join(parts),
        "route_path": state.get("route_path", []) + ["masking_gateway", "synthesizer"],
    }


def _route_by_intent(state: AuditState) -> str:
    return state.get("intent", "full_audit")


def build_audit_graph():
    builder = StateGraph(AuditState)
    builder.add_node("intent_router", _node_intent_router)
    builder.add_node("sql_query", _node_sql_query)
    builder.add_node("ledger_anomaly", _node_ledger)
    builder.add_node("contract_rag", _node_rag)
    builder.add_node("cross_verify", _node_cross_verify)
    builder.add_node("full_audit", _node_full_audit)
    builder.add_node("prepare_context", _node_prepare_context)

    builder.add_edge(START, "intent_router")
    builder.add_conditional_edges(
        "intent_router",
        _route_by_intent,
        {
            "sql_query": "sql_query",
            "ledger_anomaly": "ledger_anomaly",
            "contract_rag": "contract_rag",
            "cross_verify": "cross_verify",
            "full_audit": "full_audit",
        },
    )
    for n in ("sql_query", "ledger_anomaly", "contract_rag", "cross_verify", "full_audit"):
        builder.add_edge(n, "prepare_context")
    builder.add_edge("prepare_context", END)
    return builder.compile()


audit_graph = build_audit_graph()


def run_audit_pipeline(user_query: str, bypass_cache: bool = False) -> tuple[dict[str, Any], MaskingContext, str, bool]:
    """执行 LangGraph 本地检索/查账/核对，并在外发网关层执行实体脱敏。"""
    cache_hit = False
    cached = None if bypass_cache else query_cache.get(user_query)
    if cached is not None:
        state = dict(cached)
        cache_hit = True
    else:
        state = audit_graph.invoke({"user_query": user_query})
        query_cache.set(user_query, state)

    known_entities = get_known_entities(DB_PATH)
    masking_ctx = MaskingContext(known_entities=known_entities)
    masked_prompt = masking_ctx.mask(state.get("raw_context_prompt", ""))
    return state, masking_ctx, masked_prompt, cache_hit
