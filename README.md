# CodeProof（码证）

代码理解考核 Agent：提交一个 Java / Spring Boot 仓库，AI 当考官，**以代码为证据**考你对这个仓库的理解——会追问、会给分级提示、会查代码核对你的说法，结束后给出逐要点的评分报告和复习计划。

技术栈：Python 3.12 + FastAPI + LangGraph + PostgreSQL（pgvector）+ Redis（arq）+ Vue 3。完整规格见 [docs/SPEC.md](docs/SPEC.md)。

## 快速开始（Docker）

```bash
cp .env.example .env          # 默认用假模型、开发登录，不需要任何密钥
docker compose up -d --build
```

打开 http://localhost:8090 ，用"开发模式登录"输入任意用户名即可。

部署到服务器（HTTPS、GitHub 登录、关闭开发登录）见 [docs/deploy.md](docs/deploy.md)。

| 服务 | 宿主机端口 | 说明 |
| --- | --- | --- |
| frontend（Nginx） | 8090 | 网页；`/api` 转发给 api |
| api（FastAPI） | 8000 | 接口文档：http://localhost:8000/api/docs |
| postgres（pgvector） | 5433 | 用户/密码/库名都是 `projectb` |
| redis | 6380 | 任务队列、进度推送、限流 |
| worker（arq） | - | 克隆、分析、出题、阅卷、生成报告 |

接入真实模型：在 `.env` 里设置 `LLM_PROVIDER=openai_compatible`、`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL_STRONG`、`LLM_MODEL_FAST` 和嵌入模型相关变量（每个变量的含义见 `.env.example`），然后 `docker compose up -d`。**注意**：`EMBEDDING_DIM` 必须和嵌入模型一致，改了要删库重建（`docker compose down -v`）。

## 本机开发

```bash
# 只启动数据库和 Redis
docker compose up -d postgres redis

# 后端
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000     # api
uv run arq app.worker.WorkerSettings                 # worker（另开一个终端）

# 前端（另开一个终端）
cd frontend
npm install --no-audit --no-fund
npm run dev                                          # http://localhost:5173，/api 代理到 8000
```

本机开发时 `.env` 放在项目根目录即可（在 `backend/` 里运行时会读取上一级的 `.env`；`backend/.env` 存在时优先）。注意本机运行时数据库和 Redis 要用 `localhost:5433` / `localhost:6380`，`.env.example` 里的默认值就是这样。

## 测试、检查、评测

```bash
cd backend
uv run pytest -q                       # 需要 Docker（testcontainers 自动起 Postgres 和 Redis）
# 已有数据库时可以指定，跳过 testcontainers：
TEST_DATABASE_URL=postgresql+asyncpg://projectb:projectb@localhost:5433/projectb \
TEST_REDIS_URL=redis://localhost:6380/1 uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run mypy                            # 检查 app/core、app/agents、app/tools

uv run python -m app.eval check --suite small    # 检查评测集格式
uv run python -m app.eval run --suite small      # 跑评测，报告写到 docs/eval/

cd ../frontend && npm run build        # vue-tsc 类型检查 + 构建
```

测试全部使用假模型（`LLM_PROVIDER=fake`），不需要也不会读取真实 API Key。

## 在 Claude Code 里使用（MCP）

1. 网页「设置」页生成个人 API 令牌（`pbt_` 开头，只显示一次）。
2. 注册 MCP Server：

```bash
claude mcp add codeproof \
  -e PB_API_URL=http://localhost:8090/api \
  -e PB_API_TOKEN=pbt_你的令牌 \
  -- uv run --directory /path/to/codeproof/backend python -m app.mcp_server
```

3. 在 Claude Code 里说："用 CodeProof 分析 https://github.com/spring-projects/spring-petclinic ，然后考我 3 道题"。

可用工具：`list_repos`、`analyze_repo`、`start_session`、`answer`、`hint`、`get_report`。

## 目录

```
backend/
  app/core        配置、数据库、日志、错误、鉴权依赖、队列、限流
  app/repos       仓库接入、克隆、快照状态机、存储清理、进度 SSE
  app/analysis    tree-sitter 符号索引、调用解析、Spring 画像、候选考点、出题、增量分析
  app/llm         模型客户端（重试、降级、记账、配额）、假模型
  app/tools       查代码工具沙箱
  app/retrieval   三种检索：agentic / vector / hybrid（RRF）
  app/agents      面试官、阅卷、改动理解
  app/sessions    LangGraph 面试官图、出题计划、会话接口
  app/reports     报告；app/mastery 掌握度；app/kb 知识库；app/resume 简历核验
  app/ops         运维接口和 trace；app/eval 评测；app/mcp_server.py MCP Server
  tests/          单元测试 + 集成测试（真 Postgres/Redis + 假模型），fixtures/mini-feed 是测试用的小仓库
frontend/         Vue 3 + Element Plus
eval/datasets/    评测集
docs/             SPEC、设计决策、踩坑记录、学习文档、评测报告
```

## 里程碑状态

| 里程碑 | 状态 | 说明 |
| --- | --- | --- |
| M0 骨架 | ✅ 代码完成 | GitHub OAuth 已实现，但还没配置 OAuth App，未用真实 GitHub 账号验证；CI 已写好，尚未在 GitHub 上运行 |
| M1 仓库分析 | ✅ | petclinic、技术派都能分析完成（假模型出题）；画像需要你人工抽查 |
| M2 出题与阅卷 | ✅ | 证据行号可点开高亮；真实模型下的出题质量待验证 |
| M3 追问循环 | ✅ | interrupt + AsyncPostgresSaver，重启后续答有测试 |
| M4 简历/掌握度/知识库/模式 | ✅ | 四种模式可用 |
| M5 工程化 | ✅ | 增量分析、降级重试、限流配额、运维页、注入防护 |
| M6 评测 | ⚠️ 部分 | 运行器、指标、对比、报告、CI 小样本都已完成；**缺**：用真实模型跑出的报告、petclinic 和技术派各 20 题的人工标注数据集 |
| M7 泛化 | ⚠️ 部分 | 改动理解（提交范围 + 公开 PR）、MCP、API 令牌已完成；**未做**：GitHub App 私有仓库、Python 仓库支持（可选） |

## 还需要你提供或确认的

- GitHub OAuth App 的 Client ID / Secret（M0 完成标准"用 GitHub 账号登录"）。
- 管理员的 GitHub 登录名（`ADMIN_GITHUB_LOGINS`）。
- 模型服务的地址、Key 和模型名（M6 真实评测报告）。
- 私有仓库需要的 GitHub App（M7）。
- 评测集的人工标注：petclinic 和技术派各至少 20 题的标准答案和阅卷标注。

## 文档

- [docs/deploy.md](docs/deploy.md)：部署到云服务器
- [docs/decisions.md](docs/decisions.md)：设计决策
- [docs/pitfalls.md](docs/pitfalls.md)：踩坑记录
- [docs/learn/](docs/learn/)：每个里程碑的学习文档
- [docs/eval/](docs/eval/)：评测报告
