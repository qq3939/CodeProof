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
- **相关代码**：`backend/app/repos/service.py:54`、`backend/app/models.py:91`、测试 `backend/tests/test_integration_flow.py:177`

## D-002 调用关系用"近似解析"，不做完整的 Java 类型推导

- **背景**：链路（Controller → Service → Mapper）和"改动影响了谁"都要知道"这个方法调用了哪个方法"。准确做到这一点需要完整的编译器（类型推导、泛型、重载、Lombok 生成的代码……），而 SPEC 禁止构建或运行被分析仓库。
- **方案**：用 tree-sitter 拿到语法树，按"接收者的声明类型（字段、参数、局部变量）→ 同名方法"解析；接口调用展开到所有实现类；解析不出的保留方法名，标记为未解析。
- **备选**：调用 javac / JDT 做完整解析（需要依赖下载和构建，违反安全约束）；用 LSP（jdtls）（重、慢、仍需构建信息）。
- **为什么选它**：Spring 项目里绝大多数调用是"注入的字段.方法()"，近似解析就能覆盖；画像里同时给出 `resolved_calls / calls` 比例，让用户知道准确度。
- **代价与风险**：链式调用、Lambda、重载、继承来的字段等会解析失败或解析到多个候选；出题时链路可能缺一站。考官在追问前还会用工具查代码，部分弥补。
- **相关代码**：`backend/app/analysis/code_model.py:62`、`backend/app/analysis/java_index.py:104`

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
- **相关代码**：`backend/app/agents/interviewer.py:45`、`backend/app/sessions/graph.py:91`

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
- **相关代码**：`backend/app/sessions/service.py:137`、测试 `backend/tests/test_integration_flow.py:141`

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
- **相关代码**：`backend/app/analysis/pipeline.py:73`、`backend/app/analysis/pipeline.py:307`、测试 `backend/tests/test_integration_flow.py:220`

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
- **相关代码**：`backend/app/analysis/spring_profile.py:126`、`backend/app/analysis/candidates.py:45`

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
- **更新（2026-10-24）**："Python 仓库支持不做"这一半被 D-020 取代，Python 支持改为在 M8 做；GitHub App 仍然搁置。

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

## D-019 项目深挖：只考设计和风险，题干用面试官的问法

- **背景**：2026-10-23 第一次真实使用（ROADMAP B0-6），用户在线上用"项目深挖"考 code-nest，反馈题目"太具体、偏向代码实现，和真实面试题不符"。查下来有两个原因：
  1. 选题范围偏了。SPEC 3.2 规定项目深挖的出题依据是"设计决策点和风险点"，但 v1 的出题计划从整个题库里挑，不分考点类型；而题库按重要度取前 20 个考点，候选里机制类最多（测试夹具里机制 12 个、链路 3 个、设计 3 个、风险 1 个），抽出来多是"某个类里的事务怎么用""某个接口的调用链路"。
  2. 题干问法偏了。出题提示词要求每个答案要点带文件和行号、关键词填类名方法名，模型为了让答案对得上代码，题干也直接点名类和方法，问"这段代码做了什么"，像代码阅读题。
- **方案**：
  1. 项目深挖只从设计（design）和风险（risk）两类考点选题，这两类凑不够题量时才用其他类型补齐；生成题库时这两类先出题，考点生成的提示词也要求尽量保留这两类。
  2. 项目深挖出题时额外加一段问法要求：题干从设计意图、取舍、替代方案、故障和边界、数据量上来以后怎么办这些角度问，不出现类名、方法名、文件名和行号，用业务功能指代代码；答案要点照常落到具体代码行并带证据，追问方向往具体实现上钻。
- **备选**：
  - 只改问法、不改选题：机制题换个问法还是在考"某个机制怎么用"，那是"八股联系项目"模式的事。
  - 题干和答案要点都去掉代码细节：阅卷就没法对照代码判分，丢了"以代码为证据"这个核心。
  - 另开一个"面试题"模式、保留原来的深挖：模式变多，而 SPEC 里的项目深挖本来就该是这样。
  - 严格只出这两类、不够就报错：小仓库开不了考，测试夹具里这两类一共只有 4 个考点。
- **为什么选它**：选题回到 SPEC 的定义；题干和答案要点分开对待，题干像真实面试，答案要点仍然能对照代码判分。先问设计、再往实现上追问，也是真实面试的节奏。
- **代价与风险**：
  - 题干不点名代码，考生可能讲到别处去，要靠追问和面试官当场查代码（`verify_with_code`）拉回来；
  - 小仓库补题时仍会混进机制题；
  - 题库在分析时生成：线上已经分析好的快照，部署后选题范围马上生效，但题干还是旧问法，要重新分析才会换；增量分析会复用没改动的考点和题目，旧问法会一直沿用到这些考点的代码改动为止；
  - 新问法还没用真实模型验证过，修改前后的题目对比等线上重新分析后补上。
- **相关代码**：`backend/app/analysis/generation.py:20`（`DEEP_DIVE_KINDS`）、`:40`（`DEEP_DIVE_STYLE`）、`backend/app/sessions/planner.py:204`（`pick_deep_dive`）、`backend/app/analysis/pipeline.py:417`（题库出题顺序）；测试 `backend/tests/test_unit_misc.py`（`test_pick_deep_dive_only_design_and_risk`）、`backend/tests/test_integration_flow.py`（`test_full_session_flow` 里检查出题计划的考点类型）

## D-020 功能定稿与冻结：加通用兜底、Python 支持和 AI 应用画像，最晚 11-01 冻结

- **背景**：2026-10-24 用户在规划会话里确认功能定稿。D-017 当时决定不做 Python 支持，理由是"用户自己用不上、面试价值低"，这两点后来都变了：
  - 只支持 Java 时考不了本项目自己（后端是 Python），而 B0-16 要在线上分析本项目自己考一场，B3-11 的 Agent 项目评测集也要用本项目自己出题；同学的项目多半也用不了，请他们试用（B3-9）时会碰壁；
  - 用户的目标岗位是 AI 应用开发，面试会问 Agent、工具调用、RAG 这些设计；只会看 Spring 的画像抓不到这些考点；
  - 同时，用户要开始读本项目的代码和学习文档，代码一直在变的话，读到的东西很快就过时（`learn/M3.md` 里 `graph.py` 的引用在 `b584223` 之后一天之内就整体偏了 3 行）。
- **方案**：
  1. 11-01 前做完三项扩展（SPEC 7.3、7.4、M8）：
     - **通用兜底**：不认识的语言跳过符号索引和框架画像，由快模型根据目录结构、README、构建文件和 grep 结果生成候选考点；页面标明"通用模式"；修掉"非 Java 仓库显示分析完成、却出不了题"；提示词按仓库主语言填写；
     - **Python 支持**：先把分析层抽成与语言无关的接口，再加 tree-sitter-python 符号索引；FastAPI 只抽路由和依赖注入；
     - **AI 应用画像**：和 Spring 画像一样用规则做确定性抽取，识别工具调用、Agent 循环与终止条件、状态图、多 Agent、MCP、RAG 链路以及这些地方的风险点。
  2. **日期固定，范围可砍**：最晚 2026-11-01 冻结；到 10-30 AI 应用画像还没达到验收标准，先砍其他向量库（Milvus、Chroma、FAISS）的识别，再砍 OpenAI 和 Anthropic SDK 原生的工具调用写法。本项目自己用到的 LangGraph 状态图、LangChain 工具调用、MCP Python SDK、pgvector 上的 RAG 必须保留。
  3. **冻结后只允许**：bug，包括使用中发现的"行为不符合预期"的质量问题（如 D-019）；评测（数据集、标注工具、基线）；文档和检查（学习文档、知识地图、亮点、README、`usage.md`、CI 里的引用检查）。新需求记进 ROADMAP 第 7 节，投递之后再议。
  4. **明确不做**：GitHub App（投递后再议，D-017）；第三种语言（投递后按分析时记录的 `lang_stats` 使用数据决定）；JS 和 Go 的专门支持；运行被分析仓库的代码（永远不做）；SPEC 3.3 的其他内容。
  5. 评测和学习文档跟着补齐（SPEC 14、15、17）：出题基线、提示基线、判分基线判 3 次；盲评和盲标；Agent 项目评测集和"通用兜底 vs AI 应用画像"的消融；知识地图、Python 入门文档、`learn/M8.md`；CI 检查学习文档的 `文件:行号` 引用，引用一律带函数名。
- **备选**：
  - 维持 D-017，只支持 Java：考不了本项目自己，同学的项目也用不了，和 AI 应用开发的目标岗位对不上。
  - 只做通用兜底，不做 Python 的专门支持：没有符号和调用关系，候选考点只能靠模型从目录和 grep 结果里猜，证据定位弱，题目质量低；本项目自己是 Python，用通用兜底考自己说服力不够。
  - 一次支持更多语言（JS、Go）：工作量成倍增加，冻结只能往后推；先看使用数据再决定第三种语言更稳。
  - 不设冻结，边用边加：用户读到的代码和学习文档会一直在变，读懂项目、准备面试的时间被挤掉。
- **为什么选它**：三项扩展正好覆盖"考本项目自己"和"考同学的项目"两个真实需求，又贴合目标岗位；先抽语言接口再加 Python，Java 这条路不受影响；日期固定、范围可砍，保证 11-01 之后代码稳定下来。
- **代价与风险**：
  - 一周内做三项扩展，时间紧，所以事先定好了砍范围的顺序；
  - Python 的动态特性（鸭子类型、运行时赋值、装饰器包装）会让近似调用解析的准确度比 Java 低；
  - 通用兜底的题目质量比有画像的语言低，必须在页面上标明，免得用户以为判分同样可靠；
  - 冻结后想到的新功能都要等投递之后。
- **相关代码**：要改的位置见 ROADMAP 的 B0-9 至 B0-11（`backend/app/analysis/pipeline.py`、`backend/app/analysis/candidates.py` 的 `build_candidates`、`backend/app/analysis/code_model.py`、`backend/app/analysis/spring_profile.py`、`backend/app/agents/change.py`）；SPEC 3.1、3.3、3.4、7.3、7.4、14、15、17、M7、M8

## D-021 通用兜底：规则抽不出考点时由快模型挑，照样校验证据

- **背景**：v1 的候选考点只从 Java 符号索引和 Spring 画像里来（`build_candidates`）。提交一个非 Java 仓库时，符号索引和画像都是空的，候选考点也是空的，流水线却照常走完，显示"分析完成"，题库里一道题都没有，开考时才报"还没有题库"。出题和面试官的提示词也写死了"Java 面试官"。D-020 定下要支持其他语言，这一步是最低保障：任何语言都能出题。
- **方案**：
  - **什么时候用**：规则抽出的候选考点为空时走通用模式。不是看主语言是不是 Java：前后端在一个仓库里的项目（Java + Vue），Vue 文件可能比 Java 文件还多，按文件数判断会误判。
  - **怎么出考点**：快模型看目录结构、README、构建文件、主语言里最大的几个非测试文件（带行号）和七类关键词（重试与超时、缓存、并发、事务、鉴权、消息与定时、路由与入口）的搜索结果，挑出候选考点，每个都要给证据行号；再照常交给 `generate_topics`，和规则考点走同一套证据校验。
  - **出不了题就失败**：一道题都没生成时，分析直接失败并写明原因，不再显示"分析完成"；用户点"检查新提交"可以重试。
  - **提示词按主语言填写**：主语言是文件数最多的编程语言，写进画像（`primary_language`），出题、选考点和面试官的提示词都按它填（"资深 Go 面试官"），出题计划的每一项也带上它。
  - **页面标明**：仓库页、开始考核页、考核页都标出"通用模式"，说明题目质量比有画像的语言低。
- **备选**：
  - 遇到不支持的语言直接报"不支持"：同学的项目大多用不了，D-020 要解决的正是这个。
  - 让模型直接出题，跳过候选考点：少一步，但题目的证据就没有"候选考点 → 证据校验"这一道关，编出来的行号更难发现。
  - 只用关键词规则出考点，不调用模型：不同语言的写法差别太大，规则挑出来的多是噪声（比如注释里的 commit）。
  - 一道题都出不来时仍显示"分析完成"，只在页面上提示：用户会以为可以开考，到开考时才发现不行。
- **为什么选它**：用一次快模型调用换来"任何语言都能出题"，成本低；证据校验和规则考点共用同一套，编造的行号仍然会被拦下；模式按"规则有没有抽出考点"判断，Java 仓库的行为完全不变。
- **代价与风险**：
  - 通用模式多一次快模型调用，选考点又是一次，比 Java 仓库多花一点钱；
  - 模型给的 `topic_key` 不稳定，同一个考点换个提交可能换了名字，掌握度和增量分析的复用效果比规则考点差；
  - 关键词搜索有噪声，要靠模型筛掉；
  - 出题模型临时故障也会让分析失败（v1 是"完成但没题"），需要用户手动重试。
- **相关代码**：`backend/app/analysis/generic.py:110`（`generic_candidates`）、`backend/app/analysis/pipeline.py:224`（判断通用模式）、`:235`（一道题都没有就失败）、`backend/app/analysis/languages.py:86`（`primary_language`）、`backend/app/agents/interviewer.py:22`（`role`）；测试 `backend/tests/test_integration_generic.py`；前端 `frontend/src/api/types.ts`（`GENERIC_MODE_NOTE`）

## D-022 语言接口与 Python 支持：先抽统一结构，FastAPI 只抽路由和依赖注入

- **背景**：v1 的代码模型（`CodeModel`）、Spring 画像、改动理解都直接依赖 Java 的解析结果（`JavaFileIndex`、`parse_java`）。D-020 定下要支持 Python。如果为 Python 再写一套平行的流程，两边的链路、考点、改动理解很快就会不一致。
- **方案**：
  - **统一结构**（`lang_index.py`）：一个文件解析成 `FileIndex`，里面是符号（类、函数、方法、字段，含起止行号）、注解或装饰器、调用，外加导入别名和一个按语言约定的 `extras`。Java 和 Python 各写一个解析器，按扩展名分派（`parser_for`）；代码模型、画像、改动理解只认 `FileIndex`。表结构不变，加语言不用清库。
  - **Python 解析**（`python_index.py`，tree-sitter-python）：
    - 模块名按"相对包根目录的路径"推出；包根目录指有 `pyproject.toml`、`setup.py`、`setup.cfg`、`requirements*.txt` 的目录；
    - `self.f()` 的接收者类型就是所在的类；`x = Foo()`、`x: Foo` 记成变量类型；
    - `import x as y`、`from a import b`（含相对导入和写在函数里的导入）收进别名表，用来解析 `y.f()`；
    - 装饰器存进注解，保留点号全名（`router.post`、`mcp.tool`）。
  - **代码模型**：把 Python 的函数也当成可调用符号；Java 的"不写接收者就是调本类方法"这条规则不用在 Python 上；按别名解析 `模块.函数()`；`self.f()` 找不到时去父类里找。
  - **FastAPI**（`fastapi_profile.py`）：只抽路由和依赖注入。
    - 完整路径 = `include_router` 前缀 + `APIRouter` 前缀 + 装饰器路径；`include_router` 的参数解析不出是哪个路由器时（比如在循环里挂载），用这些调用共同的前缀当默认前缀；
    - 参数里的 `Depends(x)` 和路由器上的 `dependencies=[Depends(x)]` 都作为链路的"依赖注入"一站。
    - 接口格式和 Spring 一样，后面的链路和候选考点不区分框架。
  - **改动理解**：`affected_symbols` 走同一个接口，Python 文件的改动也能找到受影响的函数。
- **备选**：
  - 用 Python 自带的 `ast`：遇到一处语法错误整个文件都解析不了，也只认当前解释器版本的语法；tree-sitter 容错，和 Java 那边是同一套做法。
  - 用 Pyright、Jedi 这类类型推导工具：解析准确得多，但要装依赖、读虚拟环境，接近"构建"仓库，违背"不运行被分析的代码"；还很慢。
  - FastAPI 之外也做 Django、Flask：D-020 定下只做 FastAPI，冻结之前做不完。
- **为什么选它**：先抽接口再加语言，Java 的全部测试一行没改就通过了；Python 只多一个解析器和一个 FastAPI 抽取器，链路、候选考点、出题流程直接复用。
- **代价与风险**：
  - Python 的调用解析比 Java 更粗：鸭子类型、运行时赋值、装饰器包装、`getattr` 都解析不到。在本项目自己的后端上，3386 处调用解析到 837 处（2026-10-24 本机统计，不含内置函数）；
  - FastAPI 的默认前缀是猜的：挂载时前缀各不相同又解析不出路由器，就不加前缀，路径会少一段；
  - 一个仓库里有多个 Python 包、模块名重名时（比如测试夹具里也有 `app` 包），可能解析到别的包里的同名函数。
- **相关代码**：`backend/app/analysis/lang_index.py:65`（`FileIndex`）、`:101`（`parser_for`）、`backend/app/analysis/python_index.py:73`（`source_roots`）、`:195`（`parse_python`）、`backend/app/analysis/code_model.py:142`（`_targets`）、`backend/app/analysis/fastapi_profile.py:58`（`extract_fastapi_endpoints`）、`backend/app/agents/change.py:109`（`affected_symbols`）；测试 `backend/tests/test_unit_analysis.py`（`test_python_*`、`test_fastapi_endpoints_and_chains`、`test_change_affected_python`）、`backend/tests/test_integration_languages.py`；夹具 `backend/tests/fixtures/mini-agent/`

## D-023 AI 应用画像：看解析出来的结构做规则抽取，不调模型

- **背景**：用户的目标岗位是 AI 应用开发，面试会追问工具调用、Agent 循环、状态图、RAG 这些设计。Spring 画像只认 `@Transactional`、Redis 这类写法，分析一个 Agent 项目（包括本项目自己）时抓不到这些考点。D-020 定下要做 AI 应用画像，而且本项目用到的 LangGraph 状态图、LangChain 工具调用、MCP Python SDK、pgvector 上的 RAG 必须覆盖，否则没法用本项目考本项目。
- **方案**：和 Spring 画像一样，用规则做确定性抽取，不调用模型（`ai_profile.py`）。
  - **八类写法**：工具调用（`@tool`、`bind_tools`、`tools=`）、Agent 循环（调模型并处理 `tool_calls` 的循环、`recursion_limit`）、状态图（`StateGraph` 的节点和条件边、`interrupt`、检查点）、多 Agent（`create_supervisor`、`Send` 等）、MCP（Server、tool、resource、鉴权、传输）、RAG（分块、嵌入、向量检索和索引、重排、引用和拒答）、上下文与安全（提示词常量、截断、隔离）、调用工程化（重试、超时、降级、记账）。
  - **三条风险规则**：
    - 发网络请求的工具没设超时；
    - `while True` 里既没有 break 也没有任何上限；
    - 提示词常量和函数返回值直接拼接、没经过隔离。
  - **规则优先看解析出来的结构**（调用、装饰器、赋值），少看原文：分析本项目自己时，这个文件里的正则和注释写满了这些关键词，只看原文会把检测代码本身当成 AI 用法。必须看原文的几条都跳过注释行，正则也写成不会匹配自己的形式。
  - **出题**：每一类出一个设计类考点（`ai.<类别>`，键稳定，掌握度能跨提交追踪），每个风险点出一个风险类考点。设计和风险类考点的数量上限从总数的 1/6、1/5 放宽到 1/4，因为项目深挖只考这两类（D-019）。仓库页加了"AI 应用"页签，画像摘要里也加了一节。
- **备选**：
  - 让模型直接读代码找 AI 写法：每次分析都要花钱，结果不确定，也没法用假模型写测试。
  - 只按导入了哪些库来判断：只能说"用了 LangGraph"，给不出节点、终止条件、风险点这些能出题的证据。
  - 只用关键词搜原文：分析本项目自己时会把检测代码、注释、文档字符串都算进来。
- **为什么选它**：确定性、零成本、能测；证据都落在具体的函数和行上，出题和阅卷能直接用。在测试夹具里埋的三个风险点都能抓到；在本项目自己的后端上，八类里除了多 Agent 都识别出来了，风险点是 0 个，检测代码也没把自己当成 AI 用法（2026-10-24 本机检查）。
- **代价与风险**：
  - 规则是按常见写法写的，换个写法就漏：比如工具不是用装饰器、而是用类注册的，或者循环上限写在别处；
  - "上下文与安全"的风险规则只认"常量 + 函数调用"直接拼接，f-string 拼接、多步拼接都检测不到；
  - 按 D-020 的砍范围顺序，没做 Milvus、Chroma、FAISS 的专门识别，只有 LangChain 的 `similarity_search` 能覆盖一部分；OpenAI、Anthropic SDK 原生的工具调用只认出带 `tools=` 参数的模型调用；
  - 只分析 Python 文件，Java 的 Spring AI、LangChain4j 不在范围内。
- **相关代码**：`backend/app/analysis/ai_profile.py:117`（`extract_ai_profile`）、`:99`（`_while_forever_without_limit`）、`:89`（`_calls_llm`）、`backend/app/analysis/candidates.py:219`（`AI_TOPICS`）、`:262`（`_ai_candidates`）、`backend/app/analysis/spring_profile.py:484`（`build_profile` 里接入）；测试 `backend/tests/test_unit_analysis.py`（`test_ai_profile_*`）、`backend/tests/test_integration_languages.py`；夹具 `backend/tests/fixtures/mini-agent/`；前端 `frontend/src/views/RepoView.vue`（"AI 应用"页签）
