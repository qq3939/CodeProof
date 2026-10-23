# CodeProof（码证）

代码理解考核 Agent：AI 当考官，以代码为证据检验一个人对代码的理解。技术栈：Python 3.12 + FastAPI + LangGraph + Vue 3。完整规格见 `docs/SPEC.md`。

## 开工须知

- **每次进入先读 `docs/ROADMAP.md`**：M0–M5 已完成，M6、M7 部分完成，项目已上线 https://exam.fufu235.top （GitHub 登录、DeepSeek）。现在的工作是补真实评测和真实使用数据、让用户读懂项目、准备面试。ROADMAP 记录了当前状态、已定决策和任务清单，从第一个没勾的任务接着做；完成一项就在 ROADMAP 里勾掉并写上提交号。
- **SPEC 的地位**：`docs/SPEC.md` 仍是功能边界的依据；ROADMAP 第 2 节的决策优先于 SPEC 中与之冲突的条目（评测仓库、M7 的取舍）。
- **Git**：每项任务单独提交；不改写历史（不 rebase、不 amend 已推送的提交、不 force push）；推送前先确认用户同意。
- **测试和 CI 仍然只用假模型**（`LLM_PROVIDER=fake`）；真实模型只用于线上和评测。
- **范围**：
  - 不做 SPEC 第 3.3 节列出的内容；
  - 任何情况下都不运行被分析仓库里的代码；
  - 要加重要依赖或改动功能边界，先问用户。

## 关于用户

- 大二学生，靠读代码、读文档学习，除算法外不自己写代码。所以：
  - 代码注释写"为什么"；
  - 每个里程碑写一份 `docs/learn/M<n>.md`；
  - 选型记进 `docs/decisions.md`，踩坑记进 `docs/pitfalls.md`。
- 回复用简体中文，代码和命令保持英文。

## 环境

- Windows + Docker Desktop（WSL2 后端）、Python 3.12（用 uv 管理）、Node.js LTS、Git。截至 2026-10-23，Docker Desktop 还没装（见 ROADMAP 的 B0-5）。
- 端口：前端 8090、api 8000、Postgres 5433、Redis 6380。这些端口避开了项目 A，两个项目可以同时运行。
- 密钥只放在 `.env` 里，只提交 `.env.example`。

## 常用命令

```bash
docker compose up -d --build                     # 全部启动，打开 http://localhost:8090
docker compose up -d postgres redis              # 只启动依赖，本机跑 api/worker/前端
cd backend && uv sync                            # 安装后端依赖
uv run alembic upgrade head                      # 迁移
uv run uvicorn app.main:app --reload --port 8000 # api
uv run arq app.worker.WorkerSettings             # worker
uv run pytest -q                                 # 测试（testcontainers；或设 TEST_DATABASE_URL / TEST_REDIS_URL）
uv run ruff check . && uv run mypy               # 检查
uv run python -m app.eval run --suite small      # 评测
cd frontend && npm run dev                       # 前端开发（5173，代理 /api 到 8000）
npm run build                                    # 前端类型检查 + 构建
```
