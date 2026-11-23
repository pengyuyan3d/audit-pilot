"""AuditPilot V2 FastAPI 主服务入口。"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.agent_graph import query_cache, rag_engine, run_audit_pipeline
from backend.config import DB_PATH, ROOT_DIR, SAMPLE_DIR, runtime_config
from backend.database import (
    export_flywheel_jsonl,
    get_trace,
    init_db,
    list_traces,
    record_cpa_review,
    save_trace,
)
from backend.ledger_engine import execute_readonly_sql, get_ledger_summary, ingest_ledger_file
from backend.llm_gateway import build_offline_audit_report, stream_llm_with_failover
from backend.rag_engine import extract_text_from_upload

FRONTEND_DIR = ROOT_DIR / "frontend"


def bootstrap_demo_data(force: bool = False) -> dict[str, Any]:
    init_db(DB_PATH)
    summary = get_ledger_summary(DB_PATH)
    docs = rag_engine.list_documents()
    if not force and summary["total_rows"] > 0 and len(docs) > 0:
        rag_engine.rebuild_index()
        return {"loaded": False, "ledger_rows": summary["total_rows"], "documents": len(docs)}

    excel_path = SAMPLE_DIR / "财务明细流水.xlsx"
    if not excel_path.exists():
        # 自动生成演示数据
        from scripts.generate_demo_data import main as gen_main
        gen_main()

    ledger_res = ingest_ledger_file(file_path=excel_path, db_path=DB_PATH)
    doc_res = rag_engine.ingest_directory(SAMPLE_DIR, reset=True)
    query_cache.clear()
    return {"loaded": True, "ledger_rows": ledger_res["inserted"], "documents": len(doc_res)}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    bootstrap_demo_data(force=False)
    yield


app = FastAPI(title="AuditPilot V2", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


class ChatRequest(BaseModel):
    query: str
    bypass_cache: bool = False


class SqlRequest(BaseModel):
    sql: str


class ReviewRequest(BaseModel):
    decision: str  # "confirmed" | "false_positive"
    note: str = ""
    reviewer: str = "CPA_Auditor"


class SettingsUpdateRequest(BaseModel):
    primary_base_url: str | None = None
    primary_api_key: str | None = None
    primary_model: str | None = None
    backup_base_url: str | None = None
    backup_api_key: str | None = None
    backup_model: str | None = None
    simulate_primary_failure: bool | None = None
    llm_timeout_seconds: float | None = None


@app.get("/")
async def index_page():
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        return {"message": "AuditPilot V2 API is running. Frontend not built yet."}
    return FileResponse(index_file)


@app.get("/api/health")
async def health():
    return {"status": "ok", "mode": runtime_config.mode, "version": "0.2.0"}


@app.get("/api/workspace/summary")
async def workspace_summary():
    return {
        "ledger": get_ledger_summary(DB_PATH),
        "documents": rag_engine.list_documents(),
        "config": runtime_config.to_public_dict(),
        "traces_count": len(list_traces(limit=100, db_path=DB_PATH)),
    }


@app.post("/api/workspace/load-demo")
async def reload_demo():
    res = bootstrap_demo_data(force=True)
    return {"ok": True, **res, "summary": await workspace_summary()}


@app.post("/api/upload/ledger")
async def upload_ledger(file: UploadFile = File(...)):
    content = await file.read()
    res = ingest_ledger_file(file_bytes=content, filename=file.filename or "ledger.xlsx", db_path=DB_PATH)
    query_cache.clear()
    return {"ok": True, "filename": file.filename, **res, "ledger": get_ledger_summary(DB_PATH)}


@app.post("/api/upload/contract")
async def upload_contract(file: UploadFile = File(...)):
    content = await file.read()
    filename = file.filename or "contract.txt"
    text = extract_text_from_upload(filename, content)
    res = rag_engine.ingest_contract(filename, text)
    query_cache.clear()
    return {"ok": True, **res}


@app.get("/api/documents/{doc_id}/clauses")
async def document_clauses(doc_id: str):
    clauses = rag_engine.get_document_clauses(doc_id)
    if not clauses:
        raise HTTPException(status_code=404, detail="未找到该合同文档")
    return {"doc_id": doc_id, "clauses": clauses}


@app.post("/api/sql/query")
async def run_sql_sandbox(req: SqlRequest):
    try:
        return execute_readonly_sql(req.sql, DB_PATH)
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="提问内容不能为空")

    async def event_generator():
        trace_id = f"TRC-{uuid.uuid4().hex[:10].upper()}"
        try:
            state, masking_ctx, masked_prompt, cache_hit = run_audit_pipeline(query, bypass_cache=req.bypass_cache)
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
            return

        # 推送 LangGraph 执行轨迹
        if cache_hit:
            yield f"data: {json.dumps({'type': 'step', 'node': 'cache', 'message': '命中本地 TTL 查询缓存，跳过重复检索与算子计算'}, ensure_ascii=False)}\n\n"
        for st in state.get("steps_log", []):
            yield f"data: {json.dumps({'type': 'step', **st}, ensure_ascii=False)}\n\n"

        masking_summary = masking_ctx.summary()
        leaks = masking_ctx.verify_zero_leakage(masked_prompt)
        masked_cnt = masking_summary["total_masked_entities"]
        mask_step_msg = f"网关脱敏完成：保护 {masked_cnt} 个敏感实体，外发泄露校验={len(leaks)}"
        yield f"data: {json.dumps({'type': 'step', 'node': 'masking_gateway', 'message': mask_step_msg}, ensure_ascii=False)}\n\n"

        # 推送元信息（证据、脱敏表、SQL、路由）
        meta_payload = {
            "type": "meta",
            "trace_id": trace_id,
            "intent": state.get("intent", "full_audit"),
            "route_path": state.get("route_path", []),
            "cache_hit": cache_hit,
            "sql_executed": state.get("sql_executed", []),
            "evidence": state.get("evidence", []),
            "masking": masking_summary,
            "masked_prompt_preview": masked_prompt[:1800],
            "raw_prompt_preview": (state.get("raw_context_prompt") or "")[:1800],
            "zero_leakage_ok": len(leaks) == 0,
        }
        yield f"data: {json.dumps(meta_payload, ensure_ascii=False)}\n\n"

        offline_raw = build_offline_audit_report(
            intent=state.get("intent", "full_audit"),
            user_query=query,
            ledger_data=state.get("ledger_data", {}),
            rag_hits=state.get("rag_hits", []),
            cross_data=state.get("cross_data", {}),
            sql_result=state.get("sql_result"),
        )

        answer_parts: list[str] = []
        gw_stats: dict[str, Any] = {}
        async for ev in stream_llm_with_failover(masked_prompt, masking_ctx, offline_raw, runtime_config):
            if ev["type"] == "token":
                answer_parts.append(ev["text"])
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            elif ev["type"] == "step":
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            elif ev["type"] == "gateway_done":
                gw_stats = ev

        final_answer = "".join(answer_parts)
        save_trace(
            {
                "trace_id": trace_id,
                "user_query": query,
                "intent": state.get("intent", "full_audit"),
                "route_path": state.get("route_path", []),
                "model_used": gw_stats.get("model_used", "demo"),
                "failover_triggered": gw_stats.get("failover_triggered", False),
                "cache_hit": cache_hit,
                "ttft_ms": gw_stats.get("ttft_ms", 0.0),
                "total_ms": gw_stats.get("total_ms", 0.0),
                "sql_executed": state.get("sql_executed", []),
                "evidence": state.get("evidence", []),
                "masking": masking_summary,
                "masked_prompt": masked_prompt[:3000],
                "final_answer": final_answer,
            },
            DB_PATH,
        )

        done_payload = {
            "type": "done",
            "trace_id": trace_id,
            "model_used": gw_stats.get("model_used", "demo"),
            "failover_triggered": gw_stats.get("failover_triggered", False),
            "failover_reason": gw_stats.get("failover_reason", ""),
            "cache_hit": cache_hit,
            "ttft_ms": gw_stats.get("ttft_ms", 0.0),
            "total_ms": gw_stats.get("total_ms", 0.0),
        }
        yield f"data: {json.dumps(done_payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/traces")
async def api_list_traces(limit: int = Query(default=25, le=100)):
    return {"traces": list_traces(limit=limit, db_path=DB_PATH)}


@app.get("/api/traces/{trace_id}")
async def api_get_trace(trace_id: str):
    t = get_trace(trace_id, DB_PATH)
    if not t:
        raise HTTPException(status_code=404, detail="未找到审计追踪记录")
    return t


@app.get("/api/traces/{trace_id}/export")
async def api_export_trace_md(trace_id: str):
    t = get_trace(trace_id, DB_PATH)
    if not t:
        raise HTTPException(status_code=404, detail="未找到审计追踪记录")
    md = (
        f"# AuditPilot 审计底稿追踪报告 ({t['trace_id']})\n\n"
        f"- **生成时间**：{t['created_at']}\n"
        f"- **审计提问**：{t['user_query']}\n"
        f"- **编排路径**：`{' -> '.join(t['route_path'])}`\n"
        f"- **调用模型**：`{t['model_used']}`（降级触发：`{t['failover_triggered']}`，缓存命中：`{t['cache_hit']}`）\n"
        f"- **性能指标**：TTFT `{t['ttft_ms']} ms` / 总耗时 `{t['total_ms']} ms`\n"
        f"- **CPA 复核状态**：`{t['cpa_review_status']}` {('— ' + t['cpa_review_note']) if t['cpa_review_note'] else ''}\n\n"
        f"## 审计结论\n\n{t['final_answer']}\n\n"
        f"## 执行的 SQL 语句\n\n```sql\n" + "\n\n".join(t["sql_executed"]) + "\n```\n"
    )
    return PlainTextResponse(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={trace_id}.md"},
    )


@app.post("/api/traces/{trace_id}/review")
async def api_review_trace(trace_id: str, req: ReviewRequest):
    if req.decision not in ("confirmed", "false_positive"):
        raise HTTPException(status_code=400, detail="decision 必须为 confirmed 或 false_positive")
    if not get_trace(trace_id, DB_PATH):
        raise HTTPException(status_code=404, detail="未找到对应 Trace")
    review = record_cpa_review(trace_id, req.decision, req.note, req.reviewer, DB_PATH)
    return {"ok": True, "review": review}


@app.get("/api/flywheel/export")
async def api_export_flywheel(fmt: str = Query(default="sft", pattern="^(sft|dpo)$")):
    content = export_flywheel_jsonl(fmt=fmt, db_path=DB_PATH)
    return PlainTextResponse(
        content=content,
        media_type="application/x-ndjson; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=audit_pilot_{fmt}.jsonl"},
    )


@app.get("/api/settings")
async def api_get_settings():
    return runtime_config.to_public_dict()


@app.post("/api/settings")
async def api_update_settings(req: SettingsUpdateRequest):
    if req.primary_base_url is not None:
        runtime_config.primary_base_url = req.primary_base_url.strip()
    if req.primary_api_key is not None:
        runtime_config.primary_api_key = req.primary_api_key.strip()
    if req.primary_model is not None:
        runtime_config.primary_model = req.primary_model.strip()
    if req.backup_base_url is not None:
        runtime_config.backup_base_url = req.backup_base_url.strip()
    if req.backup_api_key is not None:
        runtime_config.backup_api_key = req.backup_api_key.strip()
    if req.backup_model is not None:
        runtime_config.backup_model = req.backup_model.strip()
    if req.simulate_primary_failure is not None:
        runtime_config.simulate_primary_failure = req.simulate_primary_failure
    if req.llm_timeout_seconds is not None:
        runtime_config.llm_timeout_seconds = max(3.0, min(120.0, req.llm_timeout_seconds))
    return {"ok": True, "config": runtime_config.to_public_dict()}


@app.post("/api/eval/run")
async def api_run_eval():
    eval_script = ROOT_DIR / "eval" / "run_eval.py"
    if not eval_script.exists():
        raise HTTPException(status_code=404, detail="评测脚本尚未创建")
    proc = subprocess.run(
        [sys.executable, str(eval_script)],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        timeout=60,
    )
    report_json = ROOT_DIR / "eval" / "report.json"
    if proc.returncode != 0 or not report_json.exists():
        raise HTTPException(status_code=500, detail=f"评测执行失败: {proc.stderr or proc.stdout}")
    return json.loads(report_json.read_text(encoding="utf-8"))
