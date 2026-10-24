# CodeProof（码证）

代码理解考核 Agent：提交一个公开的代码仓库（Java / Spring Boot、Python / FastAPI 和 Agent 项目，其他语言走通用模式），AI 当考官，**以代码为证据**考你对这个仓库的理解——会追问、会给分级提示、会查代码核对你的说法，结束后给出逐要点的评分报告和复习计划。

技术栈：Python 3.12 + FastAPI + LangGraph + PostgreSQL（pgvector）+ Redis（arq）+ Vue 3。完整规格见 [docs/SPEC.md](docs/SPEC.md)，当前进度和后续计划见 [docs/ROADMAP.md](docs/ROADMAP.md)。

**线上地址**：https://exam.fufu235.top （用 GitHub 账号登录；对话模型是 DeepSeek）。嵌入模型是阿里云百炼 `text-embedding-v4`（1024 维，D-018），线上知识库的上传和检索已验证可用。

## 架构

```mermaid
flowchart TB
    B["浏览器"]
    CC["Claude Code 等 MCP 客户端"]
    MCP["MCP Server（本机进程）<br/>app/mcp_server.py"]
    CC -->|stdio| MCP

    subgraph compose["Docker Compose"]
        Caddy["Caddy<br/>HTTPS 终结（只在生产环境）"]
        Nginx["frontend（Nginx）<br/>静态资源 + 转发 /api"]
        API["api（FastAPI）<br/>接口、SSE、面试官图"]
        W["worker（arq）<br/>克隆、分析、出题、阅卷、报告"]
        PG[("PostgreSQL + pgvector<br/>业务数据、向量<br/>检查点、trace")]
        R[("Redis<br/>任务队列、进度推送<br/>限流、会话锁")]
        Repo[["仓库卷 /data/repos"]]
    end

    LLM["模型服务<br/>对话：DeepSeek<br/>嵌入：阿里云百炼 text-embedding-v4"]

    B -->|HTTPS| Caddy
    MCP -->|"HTTPS + 个人 API 令牌"| Caddy
    Caddy --> Nginx
    Nginx -->|/api| API
    API --> PG
    API -->|投递任务| R
    R <-->|取任务、推进度| W
    W --> PG
    W -->|克隆、清理| Repo
    API -. 只读 .-> Repo
    API --> LLM
    W --> LLM
```

本机运行时没有 Caddy，浏览器和 MCP Server 直接访问 Nginx 的 8090 端口。api 和 worker 是同一个镜像，只是启动命令不同；api 只读挂载仓库卷，写仓库的操作只在 worker 里做（[D-010](docs/decisions.md)）。

一次作答在后端的经过（[backend/app/sessions/graph.py](backend/app/sessions/graph.py)、[backend/app/sessions/service.py](backend/app/sessions/service.py)）：

```mermaid
sequenceDiagram
    autonumber
    participant U as 浏览器
    participant A as api
    participant G as 面试官图（LangGraph）
    participant S as 工具沙箱
    participant L as 模型服务
    participant W as worker
    participant DB as Postgres

    U->>A: POST /api/sessions/{id}/answer
    A->>A: 加 Redis 锁 sess-lock:{id}，同一会话同一时间只推进一次
    A->>G: Command(resume=回答)，在后台任务里从 await_answer 的 interrupt 处恢复
    G->>DB: 读检查点，记下这次作答
    G->>L: assess：判断回答完整度，抽出回答里关于代码的说法
    G->>S: verify_with_code：find_symbol / grep / read_file 核对说法（每轮最多 5 次）
    G-->>U: SSE tool 事件："面试官正在查看 某文件"（图发出的事件都经 api 转成 SSE）
    G->>G: decide：有缺口且没到追问深度上限吗？
    alt 有缺口：追问
        G->>L: follow_up 生成追问
        G->>G: 防泄露检查：和参考答案要点重合太多就重写
        G-->>U: SSE 分块推送追问
        G->>DB: 停在 await_answer，检查点落库，等下一次作答
    else 本题结束：阅卷
        G->>W: close_question 投递 grade 任务（经 Redis）
        W->>L: 阅卷官逐要点判分，分数在及格线附近时再判 2 次取多数
        W->>DB: 写入 gradings
        G->>L: 还有题就 ask 下一题（真流式）；题量用完就 finish 并投递 report 任务
        G-->>U: SSE next_question + 题目，或 session_finished
    end
    A-->>U: SSE done（当前第几题、追问深度、提示级别）
```

追问先完整生成、过了防泄露检查再切块推送，所以看起来是流式，实际首字要等整段生成完（[D-004](docs/decisions.md)）。检查点存在 Postgres 里，刷新页面或重启 api 后能从上一次停下的地方接着答（[D-006](docs/decisions.md)）。

## 支持的语言

| 语言 | 符号索引 | 画像 | 说明 |
| --- | --- | --- | --- |
| Java | ✅ tree-sitter | Spring：接口、事务、缓存、Redis、消息、定时、并发、调用链 | |
| Python | ✅ tree-sitter | FastAPI：路由和依赖注入；AI 应用：工具调用、Agent 循环、状态图、多 Agent、MCP、RAG、上下文与安全、调用工程化，外加三条风险规则 | 调用解析比 Java 粗（D-022） |
| 其他 | ❌ | 通用模式：由模型根据目录、README、主要源码和关键词搜索挑考点，照样校验证据行号 | 页面会标明"通用模式"，题目质量比有画像的语言低（D-021） |

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

cd .. && python scripts/check_doc_refs.py   # 学习文档里的 文件:行号 引用是否还对得上（--fix 自动改正）
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

   用线上服务时，`PB_API_URL` 填 `https://exam.fufu235.top/api`，令牌在线上的「设置」页生成。
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
| M0 骨架 | ✅ | CI 已通过，线上已用 GitHub 登录 |
| M1 仓库分析 | ✅ | petclinic、技术派都能分析完成（假模型出题）；画像的人工抽查还没做 |
| M2 出题与阅卷 | ✅ | 证据行号可点开高亮；真实模型下的出题质量待验证 |
| M3 追问循环 | ✅ | interrupt + AsyncPostgresSaver，重启后续答有测试 |
| M4 简历/掌握度/知识库/模式 | ✅ | 四种模式可用 |
| M5 工程化 | ✅ | 增量分析、降级重试、限流配额、运维页、注入防护 |
| M6 评测 | ⚠️ 部分 | 运行器、指标、对比、报告、CI 小样本都已完成；**缺**：用真实模型（DeepSeek）跑出的完整报告，以及 spring-petclinic 和 [code-nest](https://github.com/qq3939/code-nest) 各 20 题的人工标注评测集。技术派只用来测分析耗时和成本，不做标注 |
| M7 泛化 | ⚠️ 部分 | 改动理解（提交范围 + 公开 PR）、MCP、API 令牌已完成；**缺**：在 Claude Code 里通过 MCP 实际走一遍。GitHub App 接入私有仓库已搁置；Python 支持 10-24 改为要做，移到 M8 |
| M8 语言扩展与 AI 应用画像 | ⚠️ 部分 | 通用兜底、Python 与 FastAPI、AI 应用画像的代码和测试已完成（D-021～D-023）；**缺**：用真实模型对本项目和开源 Agent 项目试出题、用户验收；最晚 11-01 冻结（D-020） |

## 还需要你提供或确认的

- 仓库画像的人工抽查（M1 完成标准）。
- 评测集的审题和人工标注：spring-petclinic 和 code-nest 各 20 题，题目和回答由 AI 起草。

## 文档

- [docs/ROADMAP.md](docs/ROADMAP.md)：现状、已定决策和后续任务
- [docs/deploy.md](docs/deploy.md)：部署到云服务器
- [docs/decisions.md](docs/decisions.md)：设计决策
- [docs/pitfalls.md](docs/pitfalls.md)：踩坑记录
- [docs/learn/](docs/learn/)：每个里程碑的学习文档
- [docs/eval/](docs/eval/)：评测报告
