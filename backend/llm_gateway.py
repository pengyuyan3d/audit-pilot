"""LLM 网关模块：主备模型自动降级、故障演练、网关层强制脱敏与流式还原、零 Key 离线演示兜底。"""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from backend.config import RuntimeConfig, runtime_config
from backend.masking import MaskingContext, StreamingUnmasker

SYSTEM_PROMPT = """你是 AuditPilot 企业级智能审计助手，服务于头部会计师事务所（RSM/容诚标准）。
请严格遵守以下审计纪律：
1. 所有金额、笔数、日期与异常明细必须严格基于系统提供的【确定性查账结果】与【账同交叉核对结果】，严禁自行编造或重算错误数字。
2. 引用合同条款时，必须标注对应的证据编号（如 `[证据:DOC-XXXX-C04]`）与页码、条款号，方便审计底稿复核。
3. 回答结构清晰：先给【审计结论摘要】，再分点列出【异常明细与量化影响】，最后给出【审计复核建议】。
4. 保留文中的实体占位符（如 `[ENTITY_1]`、`[BANK_ACCT_1]`）原样输出，网关会自动在本地还原。"""


def build_offline_audit_report(
    intent: str,
    user_query: str,
    ledger_data: dict[str, Any],
    rag_hits: list[dict[str, Any]],
    cross_data: dict[str, Any],
    sql_result: dict[str, Any] | None = None,
) -> str:
    """离线演示合成器：基于真实本地引擎计算结果生成结构化中文审计底稿报告（再经脱敏/还原管道流式输出）。"""
    sections: list[str] = []
    sections.append(f"### 审计结论摘要（任务路由：`{intent}`）\n")

    if sql_result:
        rows = sql_result.get("rows", [])
        sections.append(f"已通过 **L2 SQLite Authorizer 只读沙箱**执行自定义查询，返回 **{len(rows)}** 条记录。\n")
        if rows:
            cols = list(rows[0].keys())
            header = "| " + " | ".join(cols) + " |"
            sep = "| " + " | ".join(["---"] * len(cols)) + " |"
            body = "\n".join("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |" for r in rows[:15])
            sections.append(f"{header}\n{sep}\n{body}\n")

    if cross_data:
        over = cross_data.get("overpayments", [])
        mismatches = cross_data.get("account_mismatches", [])
        sections.append("#### 一、 账同交叉核对异常（合同 vs 实际付款流水）\n")
        if over:
            for item in over:
                sections.append(
                    f"- **[高危 · 超合同总额付款]** 合同 `{item['contract_no']}`（供应商：**{item['counterparty']}**）"
                    f"约定总金额上限 **¥{item['contract_amount']:,.2f}**，实际累计付款 **¥{item['actual_paid']:,.2f}**"
                    f"（共 {item['tx_count']} 笔，凭证：`{', '.join(item['vouchers'])}`），"
                    f"**超付 ¥{item['excess_amount']:,.2f}**，且备注显示无书面补充协议。"
                )
        if mismatches:
            for item in mismatches:
                sections.append(
                    f"- **[高危 · 向非合同约定账户打款]** 合同 `{item['contract_no']}` 凭证 `{item['voucher_no']}`"
                    f"（{item['payment_date']}，供应商：**{item['payee_name']}**，金额 **¥{item['amount']:,.2f}**）"
                    f"实际打入账号 `{item['actual_account_norm']}`，与合同唯一指定收款账号 `{item['expected_account_norm']}` 不一致"
                    f"（经账号格式归一化比对，同合同其余正常付款均已匹配，排除格式误报）。"
                )
        if not over and not mismatches:
            sections.append("- 未发现超合同总额或收款账户不符异常。")
        sections.append("")

    if ledger_data:
        splits = ledger_data.get("split_payments", {}).get("findings", [])
        dups = ledger_data.get("duplicate_payments", {}).get("findings", [])
        offs = ledger_data.get("off_hours_payments", {}).get("findings", [])
        sections.append("#### 二、 财务明细流水异常筛查（L1 确定性审计算子）\n")
        if splits:
            for s in splits:
                sections.append(
                    f"- **[高危 · 疑似拆分付款规避 20 万审批阈值]** 收款方：**{s['payee_name']}** 于 `{s['payment_date']}` "
                    f"单日连续支付 **{s['tx_count']} 笔**，单笔最高 ¥{s['max_single_amount']:,.2f}（均略低于 ¥200,000 红线），"
                    f"当日合计 **¥{s['total_amount']:,.2f}**，审批人：`{s['approvers']}`，凭证：`{s['vouchers']}`。"
                )
        if dups:
            for d in dups:
                sections.append(
                    f"- **[中危 · 疑似重复付款]** 收款方：**{d['payee_name']}** 于 `{d['date_1']}`（`{d['voucher_1']}`）与 "
                    f"`{d['date_2']}`（`{d['voucher_2']}`）间隔 {d['day_diff']} 天支付完全相同金额 **¥{d['amount']:,.2f}**，"
                    f"摘要均为「{d['remark_1']}」。"
                )
        if offs:
            total_off = sum(float(o["amount"]) for o in offs)
            vlist = "、".join(
                f"`{o['voucher_no']}`（{o['payment_date']} {o['payment_time']}，{o['payee_name']}，¥{o['amount']:,.2f}，{'/'.join(o['anomaly_reasons'])}）"
                for o in offs
            )
            sections.append(
                f"- **[中危 · 非工作时段/节假日大额付款]** 共检出 **{len(offs)} 笔**（合计 **¥{total_off:,.2f}**）：{vlist}。"
            )
        sections.append("")

    if rag_hits:
        sections.append("#### 三、 合同条款混合检索与风险溯源（BM25 + Dense RRF + 精排）\n")
        for hit in rag_hits:
            snippet = hit["text"].replace("\n", " ")[:180]
            sections.append(
                f"- **[证据:{hit['chunk_id']}]** `{hit['filename']}` · 第 {hit['page']} 页 · **{hit['clause_no']} {hit['heading']}**"
                f"（精排分 `{hit['relevance_score']:.2f}` / RRF `{hit['rrf_score']:.4f}`）：\n"
                f"  > {snippet}..."
            )
        sections.append("")

    sections.append(
        "#### 四、 CPA 审计复核建议\n"
        "1. 针对超付合同与非指定账户打款，立即向财务部及供应商发函询证，核查是否存在未入账补充协议或资金舞弊风险。\n"
        "2. 针对拆分付款与节假日深夜付款，抽查 OA 原始审批流、发票与入库单，评估内部控制有效性缺陷。\n"
        "3. 请在下方点击 **「CPA 复核（确认异常 / 标记误报）」** 写入审计追踪库并沉淀高质量复核语料。"
    )
    return "\n".join(sections)


async def stream_llm_with_failover(
    masked_user_prompt: str,
    masking_ctx: MaskingContext,
    offline_fallback_raw: str,
    cfg: RuntimeConfig = runtime_config,
) -> AsyncIterator[dict[str, Any]]:
    """流式调用主备大模型（带故障演练与离线兜底），全程经 StreamingUnmasker 本地还原。"""
    t0 = time.perf_counter()
    first_token_ms: float | None = None
    unmasker = StreamingUnmasker(masking_ctx)
    failover_triggered = False
    failover_reason = ""
    model_used = "offline-demo-engine"

    async def _call_openai_stream(base_url: str, api_key: str, model: str) -> AsyncIterator[str]:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=cfg.llm_timeout_seconds)
        stream = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": masked_user_prompt},
            ],
            temperature=0.2,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta

    # 1. 尝试主模型
    streamed_any = False
    if cfg.has_primary_key:
        try:
            if cfg.simulate_primary_failure:
                raise TimeoutError("故障演练已开启：模拟主模型连接超时 (Primary Timeout)")
            model_used = f"primary:{cfg.primary_model}"
            async for raw_piece in _call_openai_stream(cfg.primary_base_url, cfg.primary_api_key, cfg.primary_model):
                if first_token_ms is None:
                    first_token_ms = (time.perf_counter() - t0) * 1000.0
                streamed_any = True
                clean = unmasker.feed(raw_piece)
                if clean:
                    yield {"type": "token", "text": clean}
        except Exception as exc:
            failover_triggered = True
            failover_reason = str(exc)

    # 2. 若主模型失败或未配置，尝试备用模型
    if not streamed_any and cfg.has_backup_key:
        try:
            model_used = f"backup:{cfg.backup_model}"
            if failover_triggered:
                yield {"type": "step", "node": "failover", "message": f"主模型异常（{failover_reason}），自动降级至备用模型 {cfg.backup_model}"}
            async for raw_piece in _call_openai_stream(cfg.backup_base_url, cfg.backup_api_key, cfg.backup_model):
                if first_token_ms is None:
                    first_token_ms = (time.perf_counter() - t0) * 1000.0
                streamed_any = True
                clean = unmasker.feed(raw_piece)
                if clean:
                    yield {"type": "token", "text": clean}
        except Exception as exc:
            failover_triggered = True
            failover_reason = f"{failover_reason} | 备用模型异常: {exc}".strip(" |")

    # 3. 零 Key 离线演示模式 / 双模型不可用保底（仍然完整走脱敏 -> 切块流式 -> StreamingUnmasker 还原验证！）
    if not streamed_any:
        if cfg.simulate_primary_failure and not failover_triggered:
            failover_triggered = True
            failover_reason = "故障演练已开启：模拟主模型超时，自动降级至本地确定性审计合成引擎"
            yield {"type": "step", "node": "failover", "message": failover_reason}
        model_used = "backup:offline-demo-engine" if failover_triggered else "demo:deterministic-audit-engine"

        # 刻意先脱敏再切分小块，真实压测 StreamingUnmasker 跨 chunk 还原占位符
        masked_demo = masking_ctx.mask(offline_fallback_raw)
        chunk_size = 14
        for i in range(0, len(masked_demo), chunk_size):
            piece = masked_demo[i : i + chunk_size]
            if first_token_ms is None:
                first_token_ms = (time.perf_counter() - t0) * 1000.0
            clean = unmasker.feed(piece)
            if clean:
                yield {"type": "token", "text": clean}
            await asyncio.sleep(0.006)

    tail = unmasker.flush()
    if tail:
        yield {"type": "token", "text": tail}

    total_ms = (time.perf_counter() - t0) * 1000.0
    yield {
        "type": "gateway_done",
        "model_used": model_used,
        "failover_triggered": failover_triggered,
        "failover_reason": failover_reason,
        "ttft_ms": round(first_token_ms or total_ms, 2),
        "total_ms": round(total_ms, 2),
    }
