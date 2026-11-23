# 任务进度：AuditPilot V2 —— 企业级智能审计与风险核查工作台

## 目标与验收标准
在 `g:\antigravity workspace\audit-pilot\` 下构建面向容诚会计师事务所（RSM China）社招「AI应用高级工程师」面试演示的完整可运行 Demo，已全部达成：
1. 双核心审计闭环（财务流水异常 L1/L2 + 合同条款混合 RAG 溯源 + 账同交叉核对）。
2. 红队 4 大架构决策（ADR-1 网关归一化脱敏与流式还原、ADR-2 L1 算子 + L2 Authorizer 只读沙箱、ADR-3 RRF 召回 + 0~1 精排、ADR-4 CPA 复核 SFT/DPO 飞轮 + Docker/K8s 清单）。
3. 零 API Key 开箱运行 + 前端弹窗热配置主备 OpenAI 兼容模型与故障演练降级。
4. 12 条黄金测试用例 100% 通过 + FastAPI 全链路端到端验证通过。

## 步骤清单
- [x] 步骤 1：更新 `AGENTS.md` 防超时铁律，初始化 `audit-pilot` 目录、`pyproject.toml`、`.env.example` 与 `uv` 虚拟环境
- [x] 步骤 2：生成高仿真审计演示数据（72 条财务流水含 5 类异常 + 2 份采购合同）
- [x] 步骤 3：实现 `backend/config.py`、`backend/database.py`、`backend/masking.py`
- [x] 步骤 4：实现 `backend/ledger_engine.py`、`backend/rag_engine.py`、`backend/cross_verifier.py`
- [x] 步骤 5：实现 `backend/llm_gateway.py`、`backend/agent_graph.py`、`backend/main.py`
- [x] 步骤 6：实现 `frontend/index.html`、`frontend/styles.css`、`frontend/app.js` 三栏零依赖专业审计工作台
- [x] 步骤 7：编写 `eval/run_eval.py`、`docs/PRD.md`、`Dockerfile`、`docker-compose.yml`、`k8s/deployment.yaml`、`README.md`
- [x] 步骤 8：完成 12 条自动化评测（12/12 100% 通过，PII 泄露 0）与 FastAPI SSE/复核/导出端到端验证

## 验证结果
- **12 条黄金评测（`eval/run_eval.py`）**：`12/12 (100.0%)` 通过，PII 外发泄露 `0`，Hybrid Recall@3 `100.0%`，TTL 查询缓存加速 `12.17ms -> 1.39ms`。
- **端到端服务验证**：首页、静态资源、工作区摘要、SSE 流式全量审计、零占位符残留还原、CPA 复核、SFT JSONL 导出、Markdown 底稿导出全部通过。

## 下一步行动
- 启动服务并在浏览器预览，或直接填入主/备模型 API Key 切换实时大模型模式。
