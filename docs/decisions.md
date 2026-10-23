# 设计决策记录

每个关键选型记一条，编号依次递增。重点写清楚"为什么"，面试时会被追问。

## D-000 模板（复制后填写）

- **背景**：要解决什么问题，有哪些约束。
- **方案**：最终采用的做法。
- **备选**：还考虑过哪些做法，各自的优缺点。
- **为什么选它**：
- **代价与风险**：
- **相关代码**：`路径:行号`

## D-001 同一提交只分析一次：唯一约束，而不是分布式锁

- **背景**：多个用户（或同一用户连点两次）可能同时提交同一个仓库的同一个提交。分析很贵（克隆、解析、调用模型出题），不能做两遍，也不能出现两个快照。
- **方案**：`repo_snapshots` 上建 `UNIQUE(repository_id, commit_sha)`，插入用 `INSERT ... ON CONFLICT DO NOTHING RETURNING id`。拿到 id 的那个请求负责投递分析任务；没拿到的说明别人已经建了，直接读出已有快照复用。
- **备选**：Redis 分布式锁（`SET NX` + 过期时间）。要处理锁过期、持锁进程崩溃、锁和数据库状态不一致等问题。
- **为什么选它**：数据库唯一约束是"最终裁判"，即使锁失效也不可能插入两行；而且代码更少，不需要额外的锁服务。
- **代价与风险**：快照建好但任务投递失败时，会留下一个 `queued` 的快照，需要靠运维页"重试"处理。
- **相关代码**：`backend/app/repos/service.py:54`、`backend/app/models.py:91`、测试 `backend/tests/test_integration_flow.py:170`

## D-002 调用关系用"近似解析"，不做完整的 Java 类型推导

- **背景**：链路（Controller → Service → Mapper）和"改动影响了谁"都要知道"这个方法调用了哪个方法"。准确做到这一点需要完整的编译器（类型推导、泛型、重载、Lombok 生成的代码……），而 SPEC 禁止构建或运行被分析仓库。
- **方案**：用 tree-sitter 拿到语法树，按"接收者的声明类型（字段、参数、局部变量）→ 同名方法"解析；接口调用展开到所有实现类；解析不出的保留方法名，标记为未解析。
- **备选**：调用 javac / JDT 做完整解析（需要依赖下载和构建，违反安全约束）；用 LSP（jdtls）（重、慢、仍需构建信息）。
- **为什么选它**：Spring 项目里绝大多数调用是"注入的字段.方法()"，近似解析就能覆盖；画像里同时给出 `resolved_calls / calls` 比例，让用户知道准确度。
- **代价与风险**：链式调用、Lambda、重载、继承来的字段等会解析失败或解析到多个候选；出题时链路可能缺一站。考官在追问前还会用工具查代码，部分弥补。
- **相关代码**：`backend/app/analysis/code_model.py:63`、`backend/app/analysis/java_index.py:148`

## D-003 结构化输出用 JSON 模式 + Pydantic 校验 + 重试，而不是依赖 function calling

- **背景**：出题、阅卷、简历核验都需要模型返回固定结构。项目要兼容各家"OpenAI 兼容"服务，它们对 `response_format=json_schema` 和 function calling 的支持参差不齐。
- **方案**：统一用 `json_object` 模式，在提示词里给出 JSON Schema；返回后用 Pydantic 校验，失败时把错误信息回传给模型重试最多 2 次；强模型连续失败时降级到快模型。
- **备选**：`json_schema` 严格模式（很多国产服务商不支持）；function calling 当结构化输出（同样兼容性问题）。
- **为什么选它**：兼容面最广；Pydantic 校验还能做 schema 之外的业务约束（要点 2–5 个、每个要点至少一条证据）。
- **代价与风险**：偶尔多一次调用；模型越弱，重试越多，成本越高（费用页能看到）。
- **相关代码**：`backend/app/llm/client.py:68`（`structured`）、`backend/app/agents/schemas.py`

## D-004 追问和提示先完整生成、再切块推给前端

- **背景**：追问和提示要做"防泄露"检查（不能把参考答案的要点直接说出来）。如果边生成边推送，检查还没做完，泄露的内容已经到了用户屏幕上。
- **方案**：追问、提示先完整生成，做泄露检查（containment ≥ 0.6 就重写或降级成更笼统的提示），通过后再按 12 个字一块推给前端，视觉上仍是"流式"。出新题（不涉及答案）则真正边生成边推送。
- **备选**：全部真流式，事后发现泄露再撤回（用户已经看到了）。
- **为什么选它**：防泄露是硬要求，体验上的损失只是首字慢一点。
- **代价与风险**：追问的首字延迟等于整段生成时间；containment 是词面相似度，换个说法的泄露检测不到。
- **相关代码**：`backend/app/agents/interviewer.py:35`、`backend/app/sessions/graph.py:91`

## D-005 作答用 POST + fetch-event-source，而不是 WebSocket 或 EventSource

- **背景**：作答需要把回答发给后端，并实时收到面试官的流式回复。
- **方案**：`POST /api/sessions/{id}/answer` 直接返回 `text/event-stream`；前端用 `@microsoft/fetch-event-source` 读取。分析进度这种纯 GET 推送才用原生 `EventSource`。
- **备选**：原生 EventSource（只能 GET、不能带请求体）；WebSocket（要自己做心跳、重连、鉴权，Nginx 也要额外配置）。
- **为什么选它**：一次请求一次回答，语义清楚；Cookie 鉴权和 CSRF 校验与普通接口完全一致；Nginx 只需关掉缓冲。
- **代价与风险**：不能自动重连（作答不幂等，故意关掉了重试）；断线后前端重新拉取会话记录对齐状态。
- **相关代码**：`frontend/src/api/sse.ts`、`backend/app/sessions/router.py:148`、`frontend/nginx.conf`

## D-006 图在后台任务里跑，HTTP 连接只负责转发事件

- **背景**：用户关掉页面或网络抖动时，SSE 连接会断开。如果图直接在请求协程里跑，断开会取消协程，节点可能执行到一半（例如已经写了对话记录但没更新检查点）。
- **方案**：`resume_stream` 把图放进 `asyncio.create_task` 里跑，事件放进 `asyncio.Queue`，请求协程只是从队列读出来转发。再加一把 Redis 锁 `sess-lock:{id}`，同一会话同一时间只允许一个请求推进图。
- **备选**：在请求协程里直接 `astream`（简单但会被取消）；全部交给 worker（延迟大，流式推送要再绕一圈 Redis）。
- **为什么选它**：节点要么完整执行要么没开始，检查点一致；连点两次"发送"也不会让图并发推进。
- **代价与风险**：api 进程崩溃时后台任务也会丢，但检查点在 Postgres 里，重启后从上一个中断点继续（有测试）。
- **相关代码**：`backend/app/sessions/service.py:137`、测试 `backend/tests/test_integration_flow.py:134`

## D-007 自建 jobs 表记录后台任务，arq 只负责投递

- **背景**：运维页要能看到所有任务（排队、运行、失败原因）并一键重试；集成测试又希望不起 worker 进程。
- **方案**：`enqueue()` 先写一行 `jobs`，再交给 arq（生产）或当前进程的 asyncio 任务（`QUEUE_MODE=inline`，测试用）。任务函数统一由 `run_job` 包装：更新状态、记录错误、计算耗时。
- **备选**：只用 arq 自带的结果存储（在 Redis 里、会过期、不方便查询和统计）。
- **为什么选它**：任务历史可查询、可统计、可重试；测试和生产走同一套任务函数。
- **代价与风险**：两份状态（jobs 表和 arq 队列）可能不一致，例如 worker 被强杀时 jobs 停在 running，需要人工重试。
- **相关代码**：`backend/app/core/queue.py:41`、`backend/app/jobs.py:81`

## D-008 增量分析：解析缓存按内容哈希，考点和题目按考点键复用

- **背景**：同一个仓库有新提交时，大部分文件没变，重新解析、重新出题既慢又费钱。
- **方案**：解析结果按文件内容的 SHA-256 缓存（内存 + 磁盘）；考点用稳定的 `topic_key`（如 `chain:POST /api/feed/{id}/like`）标识，证据文件都没改的考点及其题目直接复制到新快照；代码向量按块内容哈希复用。快照的 `stats` 里记录复用数量和省下的 token。
- **备选**：只按文件路径 + 修改时间判断（git 检出后修改时间不可靠）；每次全量重做（简单但贵）。
- **为什么选它**：内容哈希天然正确；`topic_key` 让"同一个考点"跨快照可识别，掌握度也依赖它。
- **代价与风险**：考点证据之外的文件改了也可能影响考点（例如被调用方改了），这种情况会被错误复用。
- **相关代码**：`backend/app/analysis/pipeline.py:101`、`backend/app/analysis/pipeline.py:303`、测试 `backend/tests/test_integration_flow.py:213`

## D-009 限流用 Redis 固定窗口

- **背景**：防止脚本刷接口、刷模型调用。
- **方案**：`INCR ip:{ip}:{当前分钟}` + `EXPIRE 60`，超过阈值返回 429；另有每用户每日 token 配额和每用户并发任务、并发会话上限。
- **备选**：滑动窗口（更平滑，要用有序集合，复杂一些）；令牌桶（允许突发，实现更复杂）。
- **为什么选它**：一条 INCR 就够，足以挡住刷接口；真正的成本控制靠 token 配额。
- **代价与风险**：窗口边界处最多可能放过 2 倍请求；Redis 挂掉时放行（不能因为限流组件挂了而整个服务不可用）。
- **相关代码**：`backend/app/core/ratelimit.py:14`、`backend/app/main.py`

## D-010 api 只读挂载仓库目录

- **背景**：克隆、清理旧快照都会写仓库目录；api 处理的是用户请求，暴露面更大。
- **方案**：Compose 里 api 以 `:ro` 挂载仓库卷，`REPO_STORAGE_READONLY=true`；只有 worker 能写。api 发现快照代码已被清理时，投递 `checkout` 任务让 worker 重新拉取，并返回 503 让前端稍后重试。
- **备选**：两边都读写（简单，但 api 被攻破时能篡改代码，影响别人的考核）。
- **为什么选它**：最小权限；即使 api 出现路径穿越之类的漏洞，也写不了文件。
- **代价与风险**：被清理过的快照第一次访问会慢一下。
- **相关代码**：`backend/app/repos/storage.py:34`、`docker-compose.yml`

## D-011 测试代码不进画像和考点

- **背景**：在技术派（paicoding）上试跑时，测试类里的 `@Transactional`、Mock 调用大量进入候选考点，挤掉了真正的业务代码。
- **方案**：`src/test/`、`*Test.java` 等路径一律不参与画像、链路和候选考点（仍然建索引，考官查代码时能看到）。候选考点按类型设上限，同一类机制最多 4 个，保证多样性。
- **备选**：给测试代码降权（仍会混进来，规则更难解释）。
- **为什么选它**：面试问的是业务代码；规则简单、可解释。
- **代价与风险**："测试是怎么写的"这类问题出不来。
- **相关代码**：`backend/app/analysis/spring_profile.py:108`、`backend/app/analysis/candidates.py:45`

## D-012 MCP Server 用 mcp 2.x 的 MCPServer（即原 FastMCP）

- **背景**：SPEC 写的是 FastMCP；当前 `mcp` 2.x 把 `FastMCP` 改名为 `MCPServer`（`mcp.server.mcpserver`），用法相同。
- **方案**：用 `MCPServer`；MCP Server 是独立进程，用个人 API 令牌调用后端 HTTP 接口，不直接连数据库。
- **备选**：直接连数据库调用服务层（少一跳，但权限、限流、配额要再实现一遍）。
- **为什么选它**：网页端和 MCP 客户端走同一套权限和配额；令牌可以随时吊销。
- **代价与风险**：多一次 HTTP；后端接口变动时 MCP 工具要跟着改（有测试覆盖）。
- **相关代码**：`backend/app/mcp_server.py`、测试 `backend/tests/test_integration_modes.py:262`

## D-013 假模型：按任务名返回确定结果，并且"读输入"

- **背景**：测试不能依赖真实 API Key（SPEC 18.2），但只返回固定文本的假模型测不出出题、阅卷、追问的逻辑。
- **方案**：每次调用带任务名（`questions.generate`、`grade.judge`……）和结构化 `payload`，假模型按任务名分发到对应函数，根据输入算结果：例如阅卷时看回答里命中了要点的多少关键词，出题时从给定的代码片段里取标识符。嵌入用哈希词袋，同样确定。
- **备选**：录制真实模型的回复（VCR）回放（提示词一改就失效）；全部 Mock（测不出逻辑）。
- **为什么选它**：完全离线、结果确定、能区分"好回答"和"差回答"，评测流程也能在 CI 里跑通。
- **代价与风险**：假模型的"聪明程度"是写死的，评测分数不代表真实模型的表现；真实效果必须用真模型跑 M6 报告。
- **相关代码**：`backend/app/llm/fake.py`

## D-014 LangGraph 检查点表在进程启动时建，不在第一次请求里建

- **背景**：`AsyncPostgresSaver.setup()` 会执行 `CREATE INDEX CONCURRENTLY`，它要等数据库里所有已开始的事务结束。
- **方案**：api 的 lifespan 和 worker 的 startup 里用一条独立的 autocommit 连接调用 `setup_checkpointer()`；`get_graph()` 不再建表。多个进程之间用 Postgres advisory lock 排队，一次只让一个进程执行 `setup()`。
- **备选**：放进 Alembic 迁移（Alembic 默认在事务里执行，而 CONCURRENTLY 不能在事务里跑）；第一次请求时懒执行（会卡死，见 P-009）。
- **为什么选它**：启动时没有请求事务；`setup()` 本身幂等，后启动的进程发现表已建好就什么都不做。
- **v1 → v2**：v1 以为"幂等就能多进程同时执行"。2026-10-23 在 Windows 本机第一次 `docker compose up` 时，api 和 worker 在全新数据库上同时建表，api 报 `DeadlockDetected` 后被重启；测试里让两个 `setup_checkpointer()` 同时跑，又撞出 `UniqueViolation`（见 P-013）。幂等只保证"做两遍结果一样"，不保证"同时做不冲突"。v2 先用 `pg_try_advisory_lock` 轮询抢锁再执行 `setup()`；不用阻塞的 `pg_advisory_lock`，因为阻塞等锁的语句本身是进行中的事务，`CREATE INDEX CONCURRENTLY` 要等它结束，照样死锁。
- **代价与风险**：启动多一次数据库往返；后启动的进程要等先启动的那个建完表才能继续启动。
- **相关代码**：`backend/app/sessions/graph.py:473`、`backend/app/main.py:27`；测试 `backend/tests/test_integration_flow.py`（`test_checkpoint_setup_concurrent`）

## D-015 生产部署：Compose 覆盖文件 + Caddy 自动 HTTPS

- **背景**：要部署到一台 2 核 2 GB 的香港服务器，用 `https://exam.fufu235.top` 访问；开发配置（开发登录、数据库端口对外、默认密钥）不能带上线。
- **方案**：`docker-compose.prod.yml` 叠加在原配置上：关掉除 80/443 外的所有宿主机端口，强制 `ENV=prod` 和关闭开发登录，限制 Postgres/Redis/worker 的内存；新增 Caddy 容器做 HTTPS 终结，转发给原来的前端 Nginx。服务器 `.env` 里写 `COMPOSE_FILE`，平时只需 `docker compose up -d`。后端在 `ENV=prod` 且 JWT 密钥是默认值或过短时拒绝启动。
- **备选**：宿主机装 Nginx + certbot（多一套要维护的系统服务，证书续期要配定时任务）；单独写一份完整的生产 compose（和开发配置会慢慢不一致）。
- **为什么选它**：Caddy 证书申请、续期、http 跳转全自动，配置只有几行；覆盖文件只写差异，开发配置的改动自动带到生产。
- **代价与风险**：多一层代理；`ports: !reset []` 需要 Docker Compose 2.24+；Cloudflare 必须保持灰云，否则证书校验失败。
- **相关代码**：`docker-compose.prod.yml`、`deploy/`、`backend/app/core/config.py`（`_check_prod`），步骤见 `docs/deploy.md`

## D-016 评测仓库：spring-petclinic 和 code-nest 人工标注，技术派只测成本

- **背景**：M6 要一套人工标注的评测集，SPEC 原定 petclinic 和技术派各至少 20 题。标注要逐题核对证据位置、逐要点判定回答，标注者必须读懂这个仓库，否则标签本身就不可靠，算出来的要点一致率、Cohen's kappa 也就没有意义。用户没读过技术派；code-nest（项目 A）则是用户本来就要读透的项目。
- **方案**：人工标注集用 spring-petclinic（小型）和 code-nest（中型）各 20 题，题目和回答由 AI 起草，用户审题、标注。技术派（大型）只跑分析，测首次分析每千行代码的耗时、Token 和费用，以及缓存命中、增量分析各省多少，不出标注题。三个仓库都固定到具体提交。
- **备选**：
  - 按原计划标注技术派：标注者没读过，标签质量没法保证；要标得可靠，得先花十几个小时读代码。
  - 只用 petclinic 一个仓库：样本太少，也测不到中型项目。
  - 让模型自动标注：等于用模型评模型，回答不了"阅卷和人比准不准"。
- **为什么选它**：标签可信是评测有意义的前提。标注 code-nest 的过程本身就是在读项目 A，和学习计划合在一起，不额外占时间。规模上仍有小、中、大三档，成本指标不缺大型仓库。
- **代价与风险**：只有用户一个人标注，算不了标注者之间的一致性；code-nest 是用户自己的项目，题目可能偏向用户熟悉的地方；每个仓库 20 题，kappa 的置信区间会比较宽。这些都写进评测报告的"已知局限"。
- **相关代码**：`eval/datasets/`（格式参照 `eval/datasets/small/`）、`backend/app/eval/runner.py`、`backend/app/eval/metrics.py`；SPEC 15.1、15.2、M6

## D-017 M7 的取舍：GitHub App 接入私有仓库先搁置，Python 仓库支持不做

- **背景**：SPEC 的 M7 包括改动理解、MCP、API 令牌、GitHub App 接入私有仓库和可选的 Python 仓库支持，前三项已经完成。剩下的时间要优先花在真实评测、真实使用和读懂项目上。
- **方案**：GitHub App 先搁置，投递实习之后再议；Python 仓库支持不做。`.env.example` 里的 `GITHUB_APP_*` 变量先保留。
- **备选**：
  - 做 GitHub App：要创建 App、保管私钥、换安装令牌；更麻烦的是私有仓库的快照不能跨用户共享，"同一提交只分析一次"（D-001）的去重和题库共享都要按用户可见性重新设计。
  - 做 Python 支持：tree-sitter-python 的符号索引不难，但 Spring 画像、链路、候选考点都是按 Spring 写的，Python 要另写一套框架画像（FastAPI、Django 等），工作量接近再做一遍 M1。
- **为什么选它**：用户自己的仓库都是公开的，也只写 Java，这两项都用不上；面试里它们的追问价值也低（一个是接入工作，一个是重复 M1），不如把时间花在有实测数据支撑的亮点上。
- **代价与风险**：只能考公开仓库。克隆时设了 `GIT_TERMINAL_PROMPT=0`，私有仓库会直接克隆失败；SPEC 里"团队：合并前确认提交者讲得清"这个场景，在私有仓库上用不了，说服力打了折扣。被问到时说明这是有意的取舍。
- **相关代码**：`backend/app/repos/git.py:39`（克隆不带凭据）、`backend/app/models.py:83`（`is_private` 字段保留，目前始终为 false）、`.env.example`（`GITHUB_APP_*`）；SPEC 3.1、3.3、M7

## D-018 嵌入模型：阿里云百炼 text-embedding-v4，1024 维

- **背景**：对话模型用 DeepSeek，但 DeepSeek 没有嵌入接口，知识库和向量检索需要另找一家。原来的代码只适配了 OpenAI：只对 `text-embedding-3` 传 `dimensions`，每批写死 64 条，返回格式用 openai SDK 默认的 base64。数据库向量列的维度在第一次迁移时定死，改维度要清库，所以必须在有真实用户之前定下来。
- **方案**：用阿里云百炼 `text-embedding-v4`，`EMBEDDING_DIM=1024`。代码改成：
  - `EMBEDDING_SEND_DIMENSIONS`（默认 true）决定请求里带不带 `dimensions`，维度固定的模型可以关掉；
  - `EMBEDDING_BATCH_SIZE`（默认 10）决定每次请求几条，阿里云单次最多 10 条；
  - 固定 `encoding_format="float"`；
  - 返回的向量维度和 `EMBEDDING_DIM` 不一致、或者对话模型接了真实服务而嵌入模型还是假模型时，直接报出原因。
- **备选**（详细对比见 `docs/eval/embedding-choice.md`）：
  - 硅基流动 `Qwen/Qwen3-Embedding-8B`：和 v4 同系列的开源模型，分数可以直接引用；但国内站价格没查到，文档对单条长度的说法前后矛盾。
  - 硅基流动 `bge-m3`：免费，但代码检索比 Qwen3 系列低二十多分，会让评测里向量检索这一组被嵌入模型拖累。
  - 智谱 `embedding-3`：价格相同，但找不到公开的检索成绩，单条上限 3072 token 偏短。
  - OpenAI `text-embedding-3`：服务器在香港，不在 OpenAI API 支持的地区。
  - 维度保留 256：不用清库，但截得越短检索效果损失越大。
- **为什么选它**：同系列模型在中文（C-MTEB）和代码检索（MTEB-Code）上的公开成绩都高；有官方的 OpenAI 兼容文档，参数和限制写得清楚；国内付款方便，每百万 token 0.5 元。1024 是 v4 的默认维度，低于 pgvector HNSW 索引 2000 维的上限；线上还没有真实用户，正好清库。
- **代价与风险**：多一家服务商、多一个 Key；阿里云没说 v4 对应 Qwen3-Embedding 的哪个尺寸，评测报告里引用公开分数时只能当参考；批大小 10 意味着同样的文本要发更多次请求（量很小，影响可以忽略）；这些参数在 2026-10-23 线上清库重建后实测过：知识库上传笔记、检索测试都正常。
- **相关代码**：`backend/app/llm/providers.py:183`（`embed`）、`backend/app/llm/client.py:276`（分批和维度校验）、`backend/app/core/config.py:64`；测试 `backend/tests/test_unit_misc.py`（`test_embed_*`）；部署步骤见 `docs/deploy.md` 第 9 步
