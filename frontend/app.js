/* AuditPilot V2 前端交互逻辑（零外部依赖，支持 SSE 流式输出、证据高亮溯源、脱敏透视、CPA 复核与热配置） */

const state = {
  docs: [],
  activeEvidenceMap: {},
  streaming: false,
};

const $ = (id) => document.getElementById(id);

function escapeHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function renderMarkdown(md) {
  const lines = String(md || "").split("\n");
  let html = "";
  let inList = false;
  let inTable = false;
  let tableRows = [];

  const flushList = () => {
    if (inList) { html += "</ul>"; inList = false; }
  };
  const flushTable = () => {
    if (!inTable) return;
    let t = "<table><thead><tr>";
    const headers = tableRows[0];
    headers.forEach((h) => { t += `<th>${inlineFmt(h)}</th>`; });
    t += "</tr></thead><tbody>";
    for (let i = 2; i < tableRows.length; i++) {
      t += "<tr>";
      tableRows[i].forEach((c) => { t += `<td>${inlineFmt(c)}</td>`; });
      t += "</tr>";
    }
    t += "</tbody></table>";
    html += t;
    inTable = false;
    tableRows = [];
  };

  const inlineFmt = (s) => {
    let out = escapeHtml(s);
    out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    // 将 [证据:DOC-XXXX-C01] 转为可点击高亮按钮
    out = out.replace(/\[证据:([A-Za-z0-9\-]+)\]/g, (_, cid) =>
      `<button type="button" class="evidence-pill" data-chunk-id="${cid}">📎 证据:${cid}</button>`
    );
    return out;
  };

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (line.startsWith("|") && line.endsWith("|")) {
      flushList();
      inTable = true;
      tableRows.push(line.slice(1, -1).split("|").map((c) => c.trim()));
      continue;
    } else if (inTable) {
      flushTable();
    }

    if (!line) {
      flushList();
      continue;
    }
    if (line.startsWith("### ")) {
      flushList();
      html += `<h3>${inlineFmt(line.slice(4))}</h3>`;
    } else if (line.startsWith("#### ")) {
      flushList();
      html += `<h4>${inlineFmt(line.slice(5))}</h4>`;
    } else if (line.startsWith("> ")) {
      flushList();
      html += `<blockquote>${inlineFmt(line.slice(2))}</blockquote>`;
    } else if (line.startsWith("- ") || /^\d+\.\s/.test(line)) {
      if (!inList) { html += "<ul>"; inList = true; }
      const body = line.startsWith("- ") ? line.slice(2) : line.replace(/^\d+\.\s/, "");
      html += `<li>${inlineFmt(body)}</li>`;
    } else {
      flushList();
      html += `<p>${inlineFmt(line)}</p>`;
    }
  }
  flushList();
  flushTable();
  return html;
}

function switchTab(tabName) {
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tabName));
  document.querySelectorAll(".tab-pane").forEach((p) => p.classList.toggle("active", p.id === `tab-${tabName}`));
}

async function loadWorkspace() {
  const res = await fetch("/api/workspace/summary");
  const data = await res.json();

  const l = data.ledger || {};
  $("statRows").textContent = `${l.total_rows || 0} 笔`;
  $("statAmount").textContent = `¥${Number(l.total_amount || 0).toLocaleString("zh-CN", { minimumFractionDigits: 2 })}`;
  $("statPayees").textContent = `${l.payee_count || 0} 家`;
  $("statPeriod").textContent = l.min_date ? `${l.min_date.slice(5)} ~ ${l.max_date.slice(5)}` : "-";

  state.docs = data.documents || [];
  $("docList").innerHTML = state.docs.map((d) => `
    <div class="doc-item" data-doc-id="${d.doc_id}">
      <strong>${escapeHtml(d.contract_no || d.filename)}</strong>
      <div class="doc-meta">${escapeHtml(d.counterparty || d.filename)} · ${d.clause_count} 条款 · ¥${Number(d.contract_amount || 0).toLocaleString()}</div>
    </div>
  `).join("") || "<div class='pane-tip'>暂无合同</div>";

  document.querySelectorAll(".doc-item").forEach((el) => {
    el.addEventListener("click", () => openDocumentClauses(el.dataset.docId, null));
  });

  const cfg = data.config || {};
  $("modeBadge").textContent = cfg.mode === "live"
    ? `模式: 实时模型 (${cfg.primary_model || cfg.backup_model})`
    : "模式: 离线演示引擎 (未填 Key)";
  $("failoverCheckbox").checked = Boolean(cfg.simulate_primary_failure);

  await loadTraceHistory();
}

async function openDocumentClauses(docId, highlightChunkId) {
  switchTab("source");
  const res = await fetch(`/api/documents/${docId}/clauses`);
  if (!res.ok) return;
  const data = await res.json();
  $("sourceHeader").innerHTML = `当前文档：<strong>${escapeHtml(data.clauses[0]?.filename || docId)}</strong>（共 ${data.clauses.length} 个条款块）`;
  $("clauseViewer").innerHTML = data.clauses.map((c) => `
    <div class="clause-card ${c.chunk_id === highlightChunkId ? "highlighted" : ""}" id="clause-${c.chunk_id}">
      <div class="clause-meta">
        <span>${escapeHtml(c.clause_no)} · ${escapeHtml(c.heading)}</span>
        <span>第 ${c.page} 页 · ${c.chunk_id}</span>
      </div>
      <div class="clause-text">${escapeHtml(c.text)}</div>
    </div>
  `).join("");

  if (highlightChunkId) {
    const target = document.getElementById(`clause-${highlightChunkId}`);
    if (target) target.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

function renderMaskingInspector(meta) {
  const m = meta.masking || {};
  const counts = m.counts_by_category || {};
  $("maskingSummary").innerHTML = `
    <div class="pane-tip">
      <strong>零泄露校验：</strong>${meta.zero_leakage_ok ? "✅ 通过（0 泄露）" : "❌ 发现泄露"}<br/>
      共保护 <strong>${m.total_masked_entities || 0}</strong> 个敏感实体（主体 ${counts.ENTITY || 0} / 银行账号 ${counts.BANK_ACCT || 0} / 税号 ${counts.TAX_ID || 0} / 手机 ${counts.PHONE || 0}）
    </div>
  `;

  const rows = (m.mappings || []).map((r) => `
    <tr>
      <td><code>${escapeHtml(r.placeholder)}</code></td>
      <td>${escapeHtml(r.category)}</td>
      <td>${escapeHtml(r.original)}</td>
    </tr>
  `).join("");
  $("maskingTableWrap").innerHTML = rows
    ? `<table class="mini-table"><thead><tr><th>占位符</th><th>类型</th><th>归一化原值</th></tr></thead><tbody>${rows}</tbody></table>`
    : "";

  $("promptCompare").innerHTML = `
    <h4 class="sub-heading">发往 LLM 的脱敏 Prompt 预览</h4>
    <div class="code-box">${escapeHtml(meta.masked_prompt_preview || "")}</div>
  `;
}

function renderTraceCurrent(meta, done) {
  const sqls = (meta.sql_executed || []).join("\n\n");
  $("traceCurrent").innerHTML = `
    <div class="pane-tip">
      <strong>Trace ID：</strong><code>${meta.trace_id}</code><br/>
      <strong>编排路径：</strong><code>${(meta.route_path || []).join(" → ")}</code><br/>
      <strong>模型 / 延迟：</strong><code>${done?.model_used || "-"}</code> · TTFT ${done?.ttft_ms ?? "-"}ms · 总耗时 ${done?.total_ms ?? "-"}ms
    </div>
    ${sqls ? `<h4 class="sub-heading">本次执行的确定性 SQL</h4><div class="code-box">${escapeHtml(sqls)}</div>` : ""}
  `;
}

async function loadTraceHistory() {
  const res = await fetch("/api/traces?limit=15");
  const data = await res.json();
  $("traceHistory").innerHTML = (data.traces || []).map((t) => `
    <div class="trace-item" data-trace-id="${t.trace_id}">
      <div><strong>${escapeHtml(t.trace_id)}</strong> · <code>${escapeHtml(t.intent)}</code> · ${t.cpa_review_status === "confirmed" ? "✅已确认" : t.cpa_review_status === "false_positive" ? "❌误报" : "⏳待复核"}</div>
      <div class="doc-meta">${escapeHtml(t.user_query.slice(0, 48))}</div>
    </div>
  `).join("") || "<div class='pane-tip'>暂无历史记录</div>";
}

async function sendQuery(queryText) {
  const query = (queryText || "").trim();
  if (!query || state.streaming) return;
  state.streaming = true;
  $("btnSend").disabled = true;

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
    <div class="md-body">思考与取证中...</div>
    <div class="bot-footer hidden"></div>
  `;
  feed.appendChild(botEl);
  feed.scrollTop = feed.scrollHeight;

  const stepsList = botEl.querySelector(".steps-list");
  const mdBody = botEl.querySelector(".md-body");
  const botFooter = botEl.querySelector(".bot-footer");

  let rawMarkdown = "";
  let latestMeta = null;

  try {
    const resp = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, bypass_cache: $("bypassCacheCheck").checked }),
    });

    const reader = resp.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() || "";

      for (const part of parts) {
        if (!part.startsWith("data:")) continue;
        const payload = JSON.parse(part.slice(5).trim());

        if (payload.type === "step") {
          const item = document.createElement("div");
          item.className = "step-item";
          item.textContent = `[${payload.node}] ${payload.message}`;
          stepsList.appendChild(item);
        } else if (payload.type === "meta") {
          latestMeta = payload;
          (payload.evidence || []).forEach((ev) => {
            state.activeEvidenceMap[ev.chunk_id] = ev;
          });
          renderMaskingInspector(payload);
          renderTraceCurrent(payload, null);
        } else if (payload.type === "token") {
          rawMarkdown += payload.text;
          mdBody.innerHTML = renderMarkdown(rawMarkdown);
          feed.scrollTop = feed.scrollHeight;
        } else if (payload.type === "done") {
          if (latestMeta) renderTraceCurrent(latestMeta, payload);
          botFooter.classList.remove("hidden");
          botFooter.innerHTML = `
            <div class="perf-capsule">
              <span>模型: ${escapeHtml(payload.model_used)}</span>
              <span>TTFT: ${payload.ttft_ms}ms</span>
              <span>总耗时: ${payload.total_ms}ms</span>
              ${payload.cache_hit ? '<span class="tag-cache">⚡ 缓存命中</span>' : ""}
              ${payload.failover_triggered ? '<span class="tag-failover">🔄 已触发主备降级</span>' : ""}
            </div>
            <div class="review-bar" data-trace-id="${payload.trace_id}">
              <button class="btn-sm btn-confirm">✅ CPA确认异常</button>
              <button class="btn-sm btn-fp">❌ 标记误报</button>
              <a class="btn-sm" href="/api/traces/${payload.trace_id}/export" target="_blank" style="text-decoration:none;color:#0f172a;">📄 导出底稿</a>
            </div>
          `;
          bindReviewButtons(botFooter.querySelector(".review-bar"));
          bindEvidencePills(mdBody);
          await loadTraceHistory();
        } else if (payload.type === "error") {
          mdBody.innerHTML = `<p style="color:#dc2626;">❌ 执行失败：${escapeHtml(payload.message)}</p>`;
        }
      }
    }
  } catch (err) {
    mdBody.innerHTML = `<p style="color:#dc2626;">❌ 网络或流式请求异常：${escapeHtml(err.message)}</p>`;
  } finally {
    state.streaming = false;
    $("btnSend").disabled = false;
  }
}

function bindEvidencePills(container) {
  container.querySelectorAll(".evidence-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      const cid = btn.dataset.chunkId;
      const ev = state.activeEvidenceMap[cid];
      if (ev && ev.doc_id) {
        openDocumentClauses(ev.doc_id, cid);
      } else {
        const docId = cid.split("-C")[0];
        openDocumentClauses(docId, cid);
      }
    });
  });
}

function bindReviewButtons(bar) {
  if (!bar) return;
  const traceId = bar.dataset.traceId;
  const submitReview = async (decision) => {
    const note = decision === "false_positive"
      ? (window.prompt("请输入 CPA 误报说明（将写入 DPO/SFT 纠偏语料）：", "已核对补充协议，属正常业务") || "")
      : "CPA 人工复核确认异常，纳入审计底稿";
    const res = await fetch(`/api/traces/${traceId}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, note }),
    });
    if (res.ok) {
      bar.innerHTML = `<span class="review-done">${decision === "confirmed" ? "✅ CPA 已确认异常（已入库飞轮）" : "❌ 已标记误报（已入库纠偏语料）"}</span>
      <a class="btn-sm" href="/api/traces/${traceId}/export" target="_blank" style="text-decoration:none;color:#0f172a;">📄 导出底稿</a>`;
      loadTraceHistory();
    }
  };
  bar.querySelector(".btn-confirm")?.addEventListener("click", () => submitReview("confirmed"));
  bar.querySelector(".btn-fp")?.addEventListener("click", () => submitReview("false_positive"));
}

// 事件绑定
document.addEventListener("DOMContentLoaded", () => {
  loadWorkspace();

  document.querySelectorAll(".tab-btn").forEach((b) => {
    b.addEventListener("click", () => switchTab(b.dataset.tab));
  });

  document.querySelectorAll(".preset-btn").forEach((b) => {
    b.addEventListener("click", () => sendQuery(b.dataset.query));
  });

  $("chatForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const val = $("chatInput").value;
    $("chatInput").value = "";
    sendQuery(val);
  });

  $("chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      $("chatForm").requestSubmit();
    }
  });

  $("failoverCheckbox").addEventListener("change", async (e) => {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ simulate_primary_failure: e.target.checked }),
    });
  });

  // 文件上传
  $("ledgerFileInput").addEventListener("change", async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch("/api/upload/ledger", { method: "POST", body: fd });
    if (res.ok) { await loadWorkspace(); alert(`财务流水 ${file.name} 导入成功！`); }
  });

  $("contractFileInput").addEventListener("change", async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch("/api/upload/contract", { method: "POST", body: fd });
    if (res.ok) { await loadWorkspace(); alert(`合同 ${file.name} 解析并建库成功！`); }
  });

  // 模型设置弹窗
  const openSettings = async () => {
    const res = await fetch("/api/settings");
    const cfg = await res.json();
    $("cfgPrimaryUrl").value = cfg.primary_base_url || "";
    $("cfgPrimaryModel").value = cfg.primary_model || "";
    $("cfgPrimaryKey").value = "";
    $("cfgPrimaryKey").placeholder = cfg.primary_key_configured ? `已配置 (${cfg.primary_key_preview})，留空保持不变` : "填入主模型 API Key";
    $("cfgBackupUrl").value = cfg.backup_base_url || "";
    $("cfgBackupModel").value = cfg.backup_model || "";
    $("cfgBackupKey").value = "";
    $("cfgBackupKey").placeholder = cfg.backup_key_configured ? `已配置 (${cfg.backup_key_preview})，留空保持不变` : "填入备用模型 API Key";
    $("settingsModal").classList.remove("hidden");
  };
  $("btnSettings").addEventListener("click", openSettings);
  $("closeSettings").addEventListener("click", () => $("settingsModal").classList.add("hidden"));
  $("cancelSettings").addEventListener("click", () => $("settingsModal").classList.add("hidden"));
  $("saveSettings").addEventListener("click", async () => {
    const body = {
      primary_base_url: $("cfgPrimaryUrl").value,
      primary_model: $("cfgPrimaryModel").value,
      backup_base_url: $("cfgBackupUrl").value,
      backup_model: $("cfgBackupModel").value,
    };
    if ($("cfgPrimaryKey").value.trim()) body.primary_api_key = $("cfgPrimaryKey").value.trim();
    if ($("cfgBackupKey").value.trim()) body.backup_api_key = $("cfgBackupKey").value.trim();
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res.ok) {
      $("settingsModal").classList.add("hidden");
      await loadWorkspace();
    }
  });

  // 导出 SFT 飞轮语料
  $("btnExportFlywheel").addEventListener("click", () => {
    window.open("/api/flywheel/export?fmt=sft", "_blank");
  });

  // 一键自动化评测
  $("btnEval").addEventListener("click", async () => {
    $("evalModal").classList.remove("hidden");
    $("evalBody").innerHTML = "正在运行 12 条黄金审计测试用例（含 BM25 vs Hybrid A/B 对比与缓存压测）...";
    try {
      const res = await fetch("/api/eval/run", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "评测失败");
      const s = data.summary;
      $("evalBody").innerHTML = `
        <div class="stats-grid" style="grid-template-columns: repeat(4, 1fr); margin-bottom: 12px;">
          <div class="stat-item"><span class="stat-label">总通过率</span><strong>${s.passed}/${s.total} (${s.pass_rate}%)</strong></div>
          <div class="stat-item"><span class="stat-label">PII 外发泄露</span><strong>${s.pii_leak_count} 处</strong></div>
          <div class="stat-item"><span class="stat-label">Hybrid vs BM25 Recall@3</span><strong>${s.hybrid_recall_at_3}% vs ${s.bm25_recall_at_3}%</strong></div>
          <div class="stat-item"><span class="stat-label">缓存加速比</span><strong>${s.cold_ms}ms → ${s.warm_ms}ms</strong></div>
        </div>
        <table class="mini-table">
          <thead><tr><th>ID</th><th>类别</th><th>用例说明</th><th>状态</th><th>详情</th></tr></thead>
          <tbody>
            ${data.cases.map((c) => `
              <tr>
                <td><code>${c.id}</code></td>
                <td>${escapeHtml(c.category)}</td>
                <td>${escapeHtml(c.name)}</td>
                <td>${c.passed ? "✅ PASS" : "❌ FAIL"}</td>
                <td>${escapeHtml(c.detail)}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      `;
    } catch (err) {
      $("evalBody").innerHTML = `<p style="color:#dc2626;">❌ ${escapeHtml(err.message)}</p>`;
    }
  });
  $("closeEval").addEventListener("click", () => $("evalModal").classList.add("hidden"));
});
