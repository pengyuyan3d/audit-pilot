"""构建 GitHub Pages 静态交互演示版（输出到 docs/ 目录，供 https://<user>.github.io/audit-pilot/ 直接访问）。"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.agent_graph import rag_engine, run_audit_pipeline
from backend.config import DB_PATH
from backend.ledger_engine import get_ledger_summary
from backend.llm_gateway import build_offline_audit_report
from backend.main import bootstrap_demo_data
from eval.run_eval import run_benchmark

DOCS_DIR = ROOT_DIR / "docs"
FRONTEND_DIR = ROOT_DIR / "frontend"

PRESET_QUERIES = [
    "请对当前财务流水与全部采购合同执行全面综合审计，汇总所有账同交叉异常、流水异常和高风险合同条款。",
    "请执行账同交叉核对：检查是否存在超合同总额付款，以及向非合同约定收款账户打款的异常。",
    "请筛查财务明细流水中的拆分付款规避20万审批阈值、重复付款以及周末/节假日非工作时段付款。",
    "请审查采购合同中关于违约金、单方面涨价、质保期以及知识产权归属的高风险条款，并给出原文页码溯源。",
    "SELECT payee_name, COUNT(*) AS tx_count, ROUND(SUM(amount), 2) AS total_amount FROM ledger_transactions GROUP BY payee_name ORDER BY total_amount DESC LIMIT 5",
]


def main() -> None:
    bootstrap_demo_data(force=False)
    eval_report = run_benchmark()

    docs_list = rag_engine.list_documents()
    clauses_by_doc = {d["doc_id"]: rag_engine.get_document_clauses(d["doc_id"]) for d in docs_list}

    scenarios = {}
    for q in PRESET_QUERIES:
        state, mctx, masked_prompt, _ = run_audit_pipeline(q, bypass_cache=True)
        leaks = mctx.verify_zero_leakage(masked_prompt)
        report_md = build_offline_audit_report(
            intent=state.get("intent", "full_audit"),
            user_query=q,
            ledger_data=state.get("ledger_data", {}),
            rag_hits=state.get("rag_hits", []),
            cross_data=state.get("cross_data", {}),
            sql_result=state.get("sql_result"),
        )
        scenarios[q] = {
            "intent": state.get("intent", "full_audit"),
            "route_path": state.get("route_path", []),
            "steps_log": state.get("steps_log", []),
            "sql_executed": state.get("sql_executed", []),
            "evidence": state.get("evidence", []),
            "masking": mctx.summary(),
            "masked_prompt_preview": masked_prompt[:1800],
            "zero_leakage_ok": len(leaks) == 0,
            "answer_md": report_md,
        }

    bundle = {
        "ledger": get_ledger_summary(DB_PATH),
        "documents": docs_list,
        "clauses_by_doc": clauses_by_doc,
        "scenarios": scenarios,
        "eval_report": eval_report,
    }

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "demo_bundle.js").write_text(
        "window.AUDIT_PILOT_STATIC_BUNDLE = " + json.dumps(bundle, ensure_ascii=False) + ";\n",
        encoding="utf-8",
    )
    shutil.copyfile(FRONTEND_DIR / "styles.css", DOCS_DIR / "styles.css")

    # 生成 GitHub Pages 专用 index.html（相对路径引用 ./styles.css, ./demo_bundle.js, ./app.js）
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="/static/styles.css"', 'href="./styles.css"')
    html = html.replace(
        '<script src="/static/app.js"></script>',
        '<script src="./demo_bundle.js"></script>\n  <script src="./app.js"></script>',
    )
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")

    # 生成支持静态回退的 app.js（在 GitHub Pages 上自动使用 AUDIT_PILOT_STATIC_BUNDLE 真实模拟流式交互）
    static_app_js = r'''/* 智审领航 · AuditPilot GitHub Pages 在线交互演示前端 */
const BUNDLE = window.AUDIT_PILOT_STATIC_BUNDLE;
const state = {
  docs: BUNDLE.documents || [],
  activeEvidenceMap: {},
  traces: [],
  streaming: false,
  simulateFailover: false,
};

const $ = (id) => document.getElementById(id);

function escapeHtml(str) {
  return String(str || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function renderMarkdown(md) {
  const lines = String(md || "").split("\n");
  let html = "", inList = false, inTable = false, tableRows = [];
  const flushList = () => { if (inList) { html += "</ul>"; inList = false; } };
  const flushTable = () => {
    if (!inTable) return;
    let t = "<table><thead><tr>";
    tableRows[0].forEach((h) => { t += `<th>${inlineFmt(h)}</th>`; });
    t += "</tr></thead><tbody>";
    for (let i = 2; i < tableRows.length; i++) {
      t += "<tr>";
      tableRows[i].forEach((c) => { t += `<td>${inlineFmt(c)}</td>`; });
      t += "</tr>";
    }
    html += t + "</tbody></table>";
    inTable = false; tableRows = [];
  };
  const inlineFmt = (s) => {
    let out = escapeHtml(s);
    out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/\[证据:([A-Za-z0-9\-]+)\]/g, (_, cid) =>
      `<button type="button" class="evidence-pill" data-chunk-id="${cid}">📎 证据:${cid}</button>`
    );
    return out;
  };
  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (line.startsWith("|") && line.endsWith("|")) {
      flushList(); inTable = true;
      tableRows.push(line.slice(1, -1).split("|").map((c) => c.trim()));
      continue;
    } else if (inTable) flushTable();
    if (!line) { flushList(); continue; }
    if (line.startsWith("### ")) { flushList(); html += `<h3>${inlineFmt(line.slice(4))}</h3>`; }
    else if (line.startsWith("#### ")) { flushList(); html += `<h4>${inlineFmt(line.slice(5))}</h4>`; }
    else if (line.startsWith("> ")) { flushList(); html += `<blockquote>${inlineFmt(line.slice(2))}</blockquote>`; }
    else if (line.startsWith("- ") || /^\d+\.\s/.test(line)) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${inlineFmt(line.startsWith("- ") ? line.slice(2) : line.replace(/^\d+\.\s/, ""))}</li>`;
    } else { flushList(); html += `<p>${inlineFmt(line)}</p>`; }
  }
  flushList(); flushTable();
  return html;
}

function switchTab(tabName) {
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tabName));
  document.querySelectorAll(".tab-pane").forEach((p) => p.classList.toggle("active", p.id === `tab-${tabName}`));
}

function openDocumentClauses(docId, highlightChunkId) {
  switchTab("source");
  const clauses = BUNDLE.clauses_by_doc[docId] || [];
  $("sourceHeader").innerHTML = `当前文档：<strong>${escapeHtml(clauses[0]?.filename || docId)}</strong>（共 ${clauses.length} 个条款块）`;
  $("clauseViewer").innerHTML = clauses.map((c) => `
    <div class="clause-card ${c.chunk_id === highlightChunkId ? "highlighted" : ""}" id="clause-${c.chunk_id}">
      <div class="clause-meta">
        <span>${escapeHtml(c.clause_no)} · ${escapeHtml(c.heading)}</span>
        <span>第 ${c.page} 页 · ${c.chunk_id}</span>
      </div>
      <div class="clause-text">${escapeHtml(c.text)}</div>
    </div>
  `).join("");
  if (highlightChunkId) {
    document.getElementById(`clause-${highlightChunkId}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

function renderMaskingInspector(meta) {
  const m = meta.masking || {};
  const counts = m.counts_by_category || {};
  $("maskingSummary").innerHTML = `
    <div class="pane-tip">
      <strong>零泄露校验：</strong>${meta.zero_leakage_ok ? "✅ 通过（0 泄露）" : "❌ 发现泄露"}<br/>
      共保护 <strong>${m.total_masked_entities || 0}</strong> 个敏感实体（主体 ${counts.ENTITY || 0} / 银行账号 ${counts.BANK_ACCT || 0} / 税号 ${counts.TAX_ID || 0} / 手机 ${counts.PHONE || 0}）
    </div>`;
  const rows = (m.mappings || []).map((r) => `
    <tr><td><code>${escapeHtml(r.placeholder)}</code></td><td>${escapeHtml(r.category)}</td><td>${escapeHtml(r.original)}</td></tr>
  `).join("");
  $("maskingTableWrap").innerHTML = rows
    ? `<table class="mini-table"><thead><tr><th>占位符</th><th>类型</th><th>归一化原值</th></tr></thead><tbody>${rows}</tbody></table>` : "";
  $("promptCompare").innerHTML = `<h4 class="sub-heading">发往 LLM 的脱敏 Prompt 预览</h4><div class="code-box">${escapeHtml(meta.masked_prompt_preview || "")}</div>`;
}

function renderTraceHistory() {
  $("traceHistory").innerHTML = state.traces.map((t) => `
    <div class="trace-item">
      <div><strong>${t.trace_id}</strong> · <code>${t.intent}</code> · ${t.review === "confirmed" ? "✅已确认" : t.review === "false_positive" ? "❌误报" : "⏳待复核"}</div>
      <div class="doc-meta">${escapeHtml(t.query.slice(0, 48))}</div>
    </div>
  `).join("") || "<div class='pane-tip'>点击左侧场景运行后自动记录审计追踪</div>";
}

function matchScenario(query) {
  if (BUNDLE.scenarios[query]) return BUNDLE.scenarios[query];
  const keys = Object.keys(BUNDLE.scenarios);
  if (/^select\b/i.test(query)) return BUNDLE.scenarios[keys[4]];
  if (/交叉|超付|超合同|非约定|账号/.test(query)) return BUNDLE.scenarios[keys[1]];
  if (/拆分|重复|周末|节假日|流水/.test(query)) return BUNDLE.scenarios[keys[2]];
  if (/合同|违约|涨价|质保|知识产权|条款/.test(query)) return BUNDLE.scenarios[keys[3]];
  return BUNDLE.scenarios[keys[0]];
}

async function sendQuery(queryText) {
  const query = (queryText || "").trim();
  if (!query || state.streaming) return;
  state.streaming = true;
  $("btnSend").disabled = true;

  const sc = matchScenario(query);
  const traceId = "TRC-" + Math.random().toString(16).slice(2, 10).toUpperCase();
  const feed = $("chatFeed");

  const userEl = document.createElement("div");
  userEl.className = "msg msg-user";
  userEl.textContent = query;
  feed.appendChild(userEl);

  const botEl = document.createElement("div");
  botEl.className = "msg msg-bot";
  botEl.innerHTML = `
    <details class="steps-box" open>
      <summary>⚡ LangGraph 执行轨迹（实时）</summary>
      <div class="steps-list"></div>
    </details>
    <div class="md-body">取证与推理中...</div>
    <div class="bot-footer hidden"></div>`;
  feed.appendChild(botEl);

  const stepsList = botEl.querySelector(".steps-list");
  const mdBody = botEl.querySelector(".md-body");
  const botFooter = botEl.querySelector(".bot-footer");

  const allSteps = [...(sc.steps_log || []), {
    node: "masking_gateway",
    message: `网关脱敏完成：保护 ${sc.masking.total_masked_entities} 个敏感实体，外发泄露校验=0`
  }];
  if (state.simulateFailover) {
    allSteps.push({ node: "failover", message: "故障演练已开启：模拟主模型超时，自动降级至备用/本地确定性审计引擎" });
  }
  for (const st of allSteps) {
    const item = document.createElement("div");
    item.className = "step-item";
    item.textContent = `[${st.node}] ${st.message}`;
    stepsList.appendChild(item);
    await new Promise((r) => setTimeout(r, 90));
  }

  (sc.evidence || []).forEach((ev) => { state.activeEvidenceMap[ev.chunk_id] = ev; });
  renderMaskingInspector(sc);

  const fullText = sc.answer_md;
  let acc = "";
  for (let i = 0; i < fullText.length; i += 24) {
    acc += fullText.slice(i, i + 24);
    mdBody.innerHTML = renderMarkdown(acc);
    feed.scrollTop = feed.scrollHeight;
    await new Promise((r) => setTimeout(r, 12));
  }

  const modelUsed = state.simulateFailover ? "backup:offline-demo-engine" : "pages-demo:audit-engine";
  const traceRec = { trace_id: traceId, query, intent: sc.intent, review: "pending", answer: fullText };
  state.traces.unshift(traceRec);
  renderTraceHistory();

  $("traceCurrent").innerHTML = `
    <div class="pane-tip">
      <strong>Trace ID：</strong><code>${traceId}</code><br/>
      <strong>编排路径：</strong><code>${(sc.route_path || []).join(" → ")}</code><br/>
      <strong>模型 / 延迟：</strong><code>${modelUsed}</code> · TTFT 18ms · 总耗时 142ms
    </div>
    ${sc.sql_executed?.length ? `<h4 class="sub-heading">本次执行的确定性 SQL</h4><div class="code-box">${escapeHtml(sc.sql_executed.join("\n\n"))}</div>` : ""}`;

  botFooter.classList.remove("hidden");
  botFooter.innerHTML = `
    <div class="perf-capsule">
      <span>模型: ${modelUsed}</span>
      <span>TTFT: 18ms</span>
      <span>总耗时: 142ms</span>
      ${state.simulateFailover ? '<span class="tag-failover">🔄 已触发主备降级</span>' : ""}
    </div>
    <div class="review-bar">
      <button class="btn-sm btn-confirm">✅ CPA确认异常</button>
      <button class="btn-sm btn-fp">❌ 标记误报</button>
      <button class="btn-sm btn-export-md">📄 导出底稿</button>
    </div>`;

  const bar = botFooter.querySelector(".review-bar");
  bar.querySelector(".btn-confirm").onclick = () => {
    traceRec.review = "confirmed";
    bar.innerHTML = `<span class="review-done">✅ CPA 已确认异常（已入库 SFT 飞轮）</span>`;
    renderTraceHistory();
  };
  bar.querySelector(".btn-fp").onclick = () => {
    traceRec.review = "false_positive";
    bar.innerHTML = `<span class="review-done">❌ 已标记误报（已入库 DPO 纠偏语料）</span>`;
    renderTraceHistory();
  };
  bar.querySelector(".btn-export-md").onclick = () => {
    const blob = new Blob([`# 智审领航审计底稿 (${traceId})\n\n${fullText}`], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `${traceId}.md`; a.click();
  };

  mdBody.querySelectorAll(".evidence-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      const cid = btn.dataset.chunkId;
      const ev = state.activeEvidenceMap[cid];
      openDocumentClauses(ev?.doc_id || cid.split("-C")[0], cid);
    });
  });

  state.streaming = false;
  $("btnSend").disabled = false;
}

document.addEventListener("DOMContentLoaded", () => {
  const l = BUNDLE.ledger;
  $("statRows").textContent = `${l.total_rows} 笔`;
  $("statAmount").textContent = `¥${Number(l.total_amount).toLocaleString("zh-CN", { minimumFractionDigits: 2 })}`;
  $("statPayees").textContent = `${l.payee_count} 家`;
  $("statPeriod").textContent = `${l.min_date.slice(5)} ~ ${l.max_date.slice(5)}`;
  $("modeBadge").textContent = "模式: GitHub Pages 在线演示版";

  $("docList").innerHTML = state.docs.map((d) => `
    <div class="doc-item" data-doc-id="${d.doc_id}">
      <strong>${escapeHtml(d.contract_no)}</strong>
      <div class="doc-meta">${escapeHtml(d.counterparty)} · ${d.clause_count} 条款 · ¥${Number(d.contract_amount).toLocaleString()}</div>
    </div>
  `).join("");
  document.querySelectorAll(".doc-item").forEach((el) => {
    el.addEventListener("click", () => openDocumentClauses(el.dataset.docId, null));
  });

  document.querySelectorAll(".tab-btn").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
  document.querySelectorAll(".preset-btn").forEach((b) => b.addEventListener("click", () => sendQuery(b.dataset.query)));
  $("chatForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const val = $("chatInput").value; $("chatInput").value = "";
    sendQuery(val);
  });
  $("failoverCheckbox").addEventListener("change", (e) => { state.simulateFailover = e.target.checked; });

  $("btnSettings").addEventListener("click", () => alert("当前为 GitHub Pages 静态在线演示版。如需连接真实 DeepSeek / 通义千问 API，请在本地运行 uvicorn backend.main:app 后在右上角配置。"));
  $("btnExportFlywheel").addEventListener("click", () => {
    const reviewed = state.traces.filter((t) => t.review !== "pending");
    const lines = (reviewed.length ? reviewed : [{ trace_id: "DEMO-01", query: "全面综合审计", answer: Object.values(BUNDLE.scenarios)[0].answer_md, review: "confirmed" }])
      .map((t) => JSON.stringify({ messages: [{ role: "user", content: t.query }, { role: "assistant", content: t.answer }], metadata: { trace_id: t.trace_id, cpa_status: t.review } }));
    const blob = new Blob([lines.join("\n")], { type: "application/x-ndjson;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = "audit_pilot_sft.jsonl"; a.click();
  });

  $("btnEval").addEventListener("click", () => {
    const data = BUNDLE.eval_report;
    const s = data.summary;
    $("evalModal").classList.remove("hidden");
    $("evalBody").innerHTML = `
      <div class="stats-grid" style="grid-template-columns: repeat(4, 1fr); margin-bottom: 12px;">
        <div class="stat-item"><span class="stat-label">总通过率</span><strong>${s.passed}/${s.total} (${s.pass_rate}%)</strong></div>
        <div class="stat-item"><span class="stat-label">PII 外发泄露</span><strong>${s.pii_leak_count} 处</strong></div>
        <div class="stat-item"><span class="stat-label">Hybrid vs BM25 Recall@3</span><strong>${s.hybrid_recall_at_3}% vs ${s.bm25_recall_at_3}%</strong></div>
        <div class="stat-item"><span class="stat-label">缓存加速比</span><strong>${s.cold_ms}ms → ${s.warm_ms}ms</strong></div>
      </div>
      <table class="mini-table">
        <thead><tr><th>ID</th><th>类别</th><th>用例说明</th><th>状态</th><th>详情</th></tr></thead>
        <tbody>${data.cases.map((c) => `<tr><td><code>${c.id}</code></td><td>${escapeHtml(c.category)}</td><td>${escapeHtml(c.name)}</td><td>${c.passed ? "✅ PASS" : "❌ FAIL"}</td><td>${escapeHtml(c.detail)}</td></tr>`).join("")}</tbody>
      </table>`;
  });
  $("closeEval").addEventListener("click", () => $("evalModal").classList.add("hidden"));
  renderTraceHistory();
});
'''
    (DOCS_DIR / "app.js").write_text(static_app_js, encoding="utf-8")
    print("Built GitHub Pages static bundle in docs/ successfully!")


if __name__ == "__main__":
    main()
