# 智审领航 · AuditPilot — 企业级智能审计与风险核查工作台

面向会计师事务所（年报审计 / IPO 审计 / 内控合规审查）的智能审计 AI 工作台，集成了 **L1 确定性审计算子**、**L2 SQLite Authorizer 只读 SQL 沙箱**、**BM25 + Dense RRF 混合检索与条款级高亮溯源**、**LLM 网关层归一化脱敏与流式还原**、**LangGraph 显式状态图编排 + 主备模型故障降级**，以及 **CPA 人工复核 SFT/DPO 数据飞轮**。

## 快速启动

```powershell
# 1. 进入项目目录
cd "g:\antigravity workspace\audit-pilot"

# 2. 启动后端服务（已内置 72 条高仿真财务流水 + 2 份采购合同，零 API Key 亦可离线完整运行）
.venv\Scripts\uvicorn.exe backend.main:app --host 127.0.0.1 --port 8000
```

浏览器访问：`http://127.0.0.1:8000`

### 配置真实大模型 API Key（随时热更新，无需重启）
- **方式 A（推荐）**：打开页面右上角 **「⚙ 模型/Key 配置」**，填入任意 OpenAI 兼容 API（如 DeepSeek / 通义千问 / 豆包）的主备 Key 并保存，立即生效。
- **方式 B**：复制 `.env.example` 为 `.env` 并填入 `PRIMARY_LLM_API_KEY` / `BACKUP_LLM_API_KEY`。

## 运行 12 条自动化评测

```powershell
.venv\Scripts\python.exe eval\run_eval.py
```

评测完成后自动生成 `eval/report.json` 与 `eval/report.md`，涵盖 L1 审计算子、L2 SQL 注入拦截、Hybrid vs BM25 A/B Recall@3 对比、账同交叉核对、实体归一化零泄露与 TTL 缓存延迟对比。

## 容器化与 K8s 部署

- Docker：`docker compose up --build`
- Kubernetes：`k8s/deployment.yaml`（含 Readiness/Liveness 探针与 Nginx Ingress SSE 防缓冲超时配置）
