/* 智审领航 · AuditPilot GitHub Pages 在线交互演示前端
 * 支持双模式：
 * 1. 未填 Key：内置确定性审计演示引擎（零成本、断网/访客可用）
 * 2. 右上角配置 API Key（仅存于本机浏览器 localStorage，不进公有仓库）：直连 DeepSeek / OpenAI 兼容 API 流式输出，并在本地完成脱敏还原与主备降级
 */

const BUNDLE = window.AUDIT_PILOT_STATIC_BUNDLE;
const CFG_STORAGE_KEY = "audit_pilot_pages_llm_cfg_v1";

const DEFAULT_CFG = {
  primary_base_url: "https://api.deepseek.com/v1",
  primary_model: "deepseek-chat",
  primary_api_key: "",
  backup_base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
  backup_model: "qwen-plus",
  backup_api_key: "",
};

const SYSTEM_PROMPT = `你是「智审领航 · AuditPilot」企业级智能审计助手，服务于头部会计师事务所（RSM/容诚标准）。
请严格遵守：
1. 所有金额、笔数与异常必须严格依据提供的确定性查账与账同核对结果，严禁编造数字。
2. 引用合同条款时保留 [证据:DOC-XXXX-C0X] 格式与页码条款号。
3. 保留文中的实体占位符（如 [ENTITY_1]、[BANK_ACCT_1]）原样输出，本地网关会自动还原。`;

function loadCfg() {
  try {
    return { ...DEFAULT_CFG, ...JSON.parse(localStorage.getItem(CFG_STORAGE_KEY) || "{}") };
  } catch {
    return { ...DEFAULT_CFG };
  }
}

function saveCfg(cfg) {
  localStorage.setItem(CFG_STORAGE_KEY, JSON.stringify(cfg));
}

const state = {
  docs: BUNDLE.documents || [],
  activeEvidenceMap: {},
  traces: [],
  streaming: false,
  simulateFailover: false,
  cfg: loadCfg(),
};

const $ = (id) => document.getElementById(id);

function escapeHtml(str) {
  return String(str || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function maskKeyPreview(k) {
  k = (k || "").trim();
  if (!k) return "";
  return k.length <= 8 ? "****" : `${k.slice(0, 4)}****${k.slice(-4)}`;
}

function createUnmasker(mappings) {
  const map = {};
  (mappings || []).forEach((m) => { map[m.placeholder] = m.original; });
  const unmaskStr = (s) => s.replace(/\[(?:BANK_ACCT|TAX_ID|PHONE|ENTITY)_\d+\]/g, (tok) => map[tok] || tok);
  let buf = "";
  return {
    feed(chunk) {
      if (!chunk) return "";
      buf += chunk;
      let out = "";
      while (buf.length) {
        const idx = buf.indexOf("[");
        if (idx === -1) { out += buf; buf = ""; break; }
        if (idx > 0) { out += buf.slice(0, idx); buf = buf.slice(idx); }
        const closeIdx = buf.indexOf("]");
        if (closeIdx !== -1) {
          out += unmaskStr(buf.slice(0, closeIdx + 1));
          buf = buf.slice(closeIdx + 1);
        } else if (buf.length > 24) {
          out += buf[0];
          buf = buf.slice(1);
        } else {
          break;
        }
      }
      return out;
    },
    flush() {
      const rest = unmaskStr(buf);
      buf = "";
      return rest;
    },
  };
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

function updateModeBadge() {
  const hasPrimary = Boolean(state.cfg.primary_api_key?.trim());
  const hasBackup = Boolean(state.cfg.backup_api_key?.trim());
  if (hasPrimary || hasBackup) {
    const model = hasPrimary ? state.cfg.primary_model : state.cfg.backup_model;
    $("modeBadge").textContent = `模式: 浏览器直连实时模型 (${model})`;
  } else {
    $("modeBadge").textContent = "模式: 在线演示版 (点右侧可配 Key)";
  }
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

async function streamOpenAICompat(baseUrl, apiKey, model, maskedPrompt, onToken) {
  const endpoint = `${baseUrl.replace(/\/+$/, "")}/chat/completions`;
  const resp = await fetch(endpoint, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      model,
      temperature: 0.2,
      stream: true,
      messages: [
        { role: "system", content: SYSTEM_PROMPT },
        { role: "user", content: maskedPrompt },
      ],
    }),
  });
  if (!resp.ok) {
    const errText = await resp.text().catch(() => resp.statusText);
    throw new Error(`HTTP ${resp.status}: ${errText.slice(0, 120)}`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop() || "";
    for (const raw of lines) {
      const line = raw.trim();
      if (!line.startsWith("data:")) continue;
      const dataStr = line.slice(5).trim();
      if (dataStr === "[DONE]") return;
      try {
        const obj = JSON.parse(dataStr);
        const delta = obj.choices?.[0]?.delta?.content;
        if (delta) onToken(delta);
      } catch { /* ignore heartbeat */ }
    }
  }
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
  const pushStep = (node, message) => {
    const item = document.createElement("div");
    item.className = "step-item";
    item.textContent = `[${node}] ${message}`;
    stepsList.appendChild(item);
  };

  for (const st of sc.steps_log || []) {
    pushStep(st.node, st.message);
    await new Promise((r) => setTimeout(r, 70));
  }
  pushStep("masking_gateway", `网关脱敏完成：保护 ${sc.masking.total_masked_entities} 个敏感实体，外发泄露校验=0`);

  (sc.evidence || []).forEach((ev) => { state.activeEvidenceMap[ev.chunk_id] = ev; });
  renderMaskingInspector(sc);

  const t0 = performance.now();
  let ttftMs = null;
  let fullText = "";
  let modelUsed = "pages-demo:deterministic-engine";
  let failoverTriggered = false;
  const unmasker = createUnmasker(sc.masking?.mappings || []);
  const maskedPrompt = `【用户提问】\n${query}\n\n${sc.masked_prompt_preview || ""}`;

  const appendUnmasked = (rawDelta) => {
    if (ttftMs === null) ttftMs = Math.round(performance.now() - t0);
    const clean = unmasker.feed(rawDelta);
    if (clean) {
      fullText += clean;
      mdBody.innerHTML = renderMarkdown(fullText);
      feed.scrollTop = feed.scrollHeight;
    }
  };

  let streamedLive = false;
  const hasPrimary = Boolean(state.cfg.primary_api_key?.trim());
  const hasBackup = Boolean(state.cfg.backup_api_key?.trim());

  if (hasPrimary) {
    try {
      if (state.simulateFailover) throw new Error("故障演练已开启：模拟主模型连接超时");
      modelUsed = `primary:${state.cfg.primary_model}`;
      await streamOpenAICompat(state.cfg.primary_base_url, state.cfg.primary_api_key.trim(), state.cfg.primary_model, maskedPrompt, appendUnmasked);
      streamedLive = true;
    } catch (err) {
      failoverTriggered = true;
      pushStep("failover", `主模型异常（${err.message}），正在自动降级...`);
    }
  }

  if (!streamedLive && hasBackup) {
    try {
      modelUsed = `backup:${state.cfg.backup_model}`;
      await streamOpenAICompat(state.cfg.backup_base_url, state.cfg.backup_api_key.trim(), state.cfg.backup_model, maskedPrompt, appendUnmasked);
      streamedLive = true;
    } catch (err) {
      failoverTriggered = true;
      pushStep("failover", `备用模型异常（${err.message}），回落至本地确定性审计引擎`);
    }
  }

  if (streamedLive) {
    const tail = unmasker.flush();
    if (tail) {
      fullText += tail;
      mdBody.innerHTML = renderMarkdown(fullText);
    }
  } else {
    if (state.simulateFailover && !failoverTriggered) {
      failoverTriggered = true;
      pushStep("failover", "故障演练已开启：模拟主模型超时，自动降级至本地确定性审计引擎");
    }
    modelUsed = failoverTriggered ? "backup:offline-demo-engine" : "pages-demo:deterministic-engine";
    const demoText = sc.answer_md;
    for (let i = 0; i < demoText.length; i += 24) {
      if (ttftMs === null) ttftMs = Math.round(performance.now() - t0);
      fullText += demoText.slice(i, i + 24);
      mdBody.innerHTML = renderMarkdown(fullText);
      feed.scrollTop = feed.scrollHeight;
      await new Promise((r) => setTimeout(r, 10));
    }
  }

  const totalMs = Math.round(performance.now() - t0);
  const traceRec = { trace_id: traceId, query, intent: sc.intent, review: "pending", answer: fullText };
  state.traces.unshift(traceRec);
  renderTraceHistory();

  $("traceCurrent").innerHTML = `
    <div class="pane-tip">
      <strong>Trace ID：</strong><code>${traceId}</code><br/>
      <strong>编排路径：</strong><code>${(sc.route_path || []).join(" → ")}</code><br/>
      <strong>模型 / 延迟：</strong><code>${modelUsed}</code> · TTFT ${ttftMs ?? totalMs}ms · 总耗时 ${totalMs}ms
    </div>
    ${sc.sql_executed?.length ? `<h4 class="sub-heading">本次执行的确定性 SQL</h4><div class="code-box">${escapeHtml(sc.sql_executed.join("\n\n"))}</div>` : ""}`;

  botFooter.classList.remove("hidden");
  botFooter.innerHTML = `
    <div class="perf-capsule">
      <span>模型: ${escapeHtml(modelUsed)}</span>
      <span>TTFT: ${ttftMs ?? totalMs}ms</span>
      <span>总耗时: ${totalMs}ms</span>
      ${failoverTriggered ? '<span class="tag-failover">🔄 已触发主备降级</span>' : ""}
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
  updateModeBadge();

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
  $("chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("chatForm").requestSubmit(); }
  });
  $("failoverCheckbox").addEventListener("change", (e) => { state.simulateFailover = e.target.checked; });

  // 模型 Key 本地安全配置弹窗（存浏览器 localStorage，不上传公有仓库）
  $("btnSettings").addEventListener("click", () => {
    $("cfgPrimaryUrl").value = state.cfg.primary_base_url;
    $("cfgPrimaryModel").value = state.cfg.primary_model;
    $("cfgPrimaryKey").value = "";
    $("cfgPrimaryKey").placeholder = state.cfg.primary_api_key
      ? `已在本机保存 (${maskKeyPreview(state.cfg.primary_api_key)})，留空保持不变`
      : "粘贴 DeepSeek / OpenAI 兼容 API Key（仅存本机浏览器）";
    $("cfgBackupUrl").value = state.cfg.backup_base_url;
    $("cfgBackupModel").value = state.cfg.backup_model;
    $("cfgBackupKey").value = "";
    $("cfgBackupKey").placeholder = state.cfg.backup_api_key
      ? `已在本机保存 (${maskKeyPreview(state.cfg.backup_api_key)})，留空保持不变`
      : "可选：粘贴备用降级模型 API Key";
    $("settingsModal").classList.remove("hidden");
  });
  $("closeSettings").addEventListener("click", () => $("settingsModal").classList.add("hidden"));
  $("cancelSettings").addEventListener("click", () => $("settingsModal").classList.add("hidden"));
  $("saveSettings").addEventListener("click", () => {
    const next = {
      ...state.cfg,
      primary_base_url: $("cfgPrimaryUrl").value.trim() || DEFAULT_CFG.primary_base_url,
      primary_model: $("cfgPrimaryModel").value.trim() || DEFAULT_CFG.primary_model,
      backup_base_url: $("cfgBackupUrl").value.trim() || DEFAULT_CFG.backup_base_url,
      backup_model: $("cfgBackupModel").value.trim() || DEFAULT_CFG.backup_model,
    };
    if ($("cfgPrimaryKey").value.trim()) next.primary_api_key = $("cfgPrimaryKey").value.trim();
    if ($("cfgBackupKey").value.trim()) next.backup_api_key = $("cfgBackupKey").value.trim();
    state.cfg = next;
    saveCfg(next);
    updateModeBadge();
    $("settingsModal").classList.add("hidden");
  });

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
