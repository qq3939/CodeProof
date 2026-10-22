# CodeProof（码证）

代码理解考核 Agent：AI 当考官，以代码为证据检验一个人对代码的理解。技术栈：Python 3.12 + FastAPI + LangGraph + Vue 3。完整规格见 `docs/SPEC.md`。

## 开工须知

- **第一次进入本项目**：先通读 `docs/SPEC.md`，再向用户确认 SPEC 第 1 节列出的信息：
  - GitHub OAuth App；
  - 管理员登录名；
  - 模型服务。

  然后从 M0 开始。模型 Key 还没拿到时，先用假模型（`LLM_PROVIDER=fake`）开发。
- **之后每次进入**：先看 README 和 `git log`，确认做到了哪个里程碑，从中断的地方继续。
- **推进方式**：按 SPEC 第 16 节的里程碑顺序推进。每个里程碑达到完成标准后，依次：跑测试 → `docker compose up` 验证 → 更新 README 和学习文档 → 提交 → 向用户汇报 → 继续下一个。
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

- Windows + Docker Desktop（WSL2 后端）、Python 3.12（用 uv 管理）、Node.js LTS、Git。
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
