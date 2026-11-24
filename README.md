# 智审领航 · AuditPilot — 企业级智能审计与风险核查工作台

[![Audit Eval CI](https://github.com/pengyuyan3d/audit-pilot/actions/workflows/ci.yml/badge.svg)](https://github.com/pengyuyan3d/audit-pilot/actions/workflows/ci.yml)

**在线工作台演示 → [pengyuyan3d.github.io/audit-pilot](https://pengyuyan3d.github.io/audit-pilot/)**（浏览器直接打开，内置高仿真账套与采购合同，零 API Key 可离线体验完整核查链路，亦支持在右上角填入个人 Key 切换实时流式推理）

面向会计师事务所（年报审计 / IPO 审计 / 内控合规审查）的智能审计 AI 工作台，集成了 **L1 确定性审计算子**、**L2 SQLite Authorizer 只读 SQL 沙箱**、**BM25 + Dense RRF 混合检索与条款级高亮溯源**、**LLM 网关层归一化脱敏与流式还原**、**LangGraph 显式状态图编排 + 主备模型故障降级**，以及 **CPA 人工复核 SFT/DPO 数据飞轮**。

## 快速启动

```bash
# 1. 克隆并安装依赖
git clone https://github.com/pengyuyan3d/audit-pilot.git
cd audit-pilot
pip install -e .

# 2. 启动后端服务（已内置 72 条高仿真财务流水 + 2 份采购合同，零 API Key 亦可离线完整运行）
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

浏览器访问：`http://127.0.0.1:8000`

### 配置真实大模型 API Key（随时热更新，无需重启）
- **方式 A（推荐）**：打开页面右上角 **「⚙ 模型/Key 配置」**，填入任意 OpenAI 兼容 API（如 DeepSeek / 通义千问 / 豆包）的主备 Key 并保存，立即生效。
- **方式 B**：复制 `.env.example` 为 `.env` 并填入 `PRIMARY_LLM_API_KEY` / `BACKUP_LLM_API_KEY`。

## 运行 12 条自动化评测

```bash
python eval/run_eval.py
```

评测完成后自动生成 `eval/report.json` 与 `eval/report.md`，涵盖 L1 审计算子、L2 SQL 注入拦截、Hybrid vs BM25 A/B Recall@3 对比、账同交叉核对、实体归一化零泄露与 TTL 缓存延迟对比。

## 容器化与 K8s 部署

- Docker：`docker compose up --build`
- Kubernetes：`k8s/deployment.yaml`（含 Readiness/Liveness 探针与 Nginx Ingress SSE 防缓冲超时配置）

## 边界说明

- 内置账套与合同均为脱敏虚构样本，用于验证确定性算账、只读 SQL 拦截、混合检索溯源与脱敏网关的工程闭环，不涉及任何真实企业财务数据。
- 在线 GitHub Pages 版将核心算子与样例数据预编译为前端可离线核验的演示包；完整的 SQLite Authorizer 只读沙箱与后端多进程网关需在本地或容器环境运行。
