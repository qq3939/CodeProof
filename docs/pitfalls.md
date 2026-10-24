# 踩坑记录

开发中遇到的问题，按编号依次记录。

## P-000 模板（复制后填写）

- **现象**：
- **原因**：
- **解决**：
- **教训**：
- **相关提交**：

## P-001 pydantic-settings 把逗号分隔的列表当 JSON 解析

- **现象**：`.env` 里写 `ADMIN_GITHUB_LOGINS=alice,bob`，启动时报 JSON 解析错误。
- **原因**：pydantic-settings 对 `list[str]` 类型的环境变量默认按 JSON 解析。
- **解决**：字段类型加 `Annotated[list[str], NoDecode]`，再用 `field_validator(mode="before")` 自己按逗号切分。
- **教训**：复杂类型的环境变量，先查文档确认解析规则。
- **相关提交**：后端主体提交（`app/core/config.py`）

## P-002 pytest 找不到 `app` 模块

- **现象**：`ModuleNotFoundError: No module named 'app'`。
- **原因**：项目没有作为包安装（`package = false`），pytest 的工作目录不在 `sys.path` 里。
- **解决**：`pyproject.toml` 里加 `[tool.pytest.ini_options] pythonpath = ["."]`；单独运行脚本时用 `PYTHONPATH=.`。
- **教训**：不打包的应用项目要显式配置导入路径。
- **相关提交**：后端主体提交

## P-003 BM25 在很小的语料上给出负分

- **现象**：知识库只有两三篇文档时，检索结果里明明包含关键词的块排在最后。
- **原因**：BM25 的 IDF 公式在"词出现在一半以上的文档里"时为负数；小语料里这很常见。
- **解决**：先过滤掉和查询没有任何词重合的块，再按 BM25 排序；最终和向量检索用 RRF 融合，RRF 只看名次不看分数。
- **教训**：经典算法有适用前提，测试数据量太小时要特别检查。
- **相关提交**：后端主体提交（`app/kb/service.py:105`）

## P-004 `hash()` 每次启动结果都不同

- **现象**：开发登录同一个用户名，重启后变成了另一个用户。
- **原因**：Python 对字符串的 `hash()` 默认加随机盐（`PYTHONHASHSEED`），每个进程不同。
- **解决**：改用 `hashlib.sha256` 派生稳定的 id。
- **教训**：需要跨进程稳定的值，不要用内置 `hash()`。
- **相关提交**：后端主体提交（`app/auth/router.py:142`）

## P-005 测试输出接到管道后卡住

- **现象**：`uv run pytest ... | grep xxx` 偶尔一直不结束。
- **原因**：测试里起了后台任务和子进程，管道另一端提前关闭或缓冲区满时会互相等待。
- **解决**：把输出重定向到文件（`> t.log`）再查看。
- **教训**：排查"卡住"时先排除外部因素（管道、终端）。
- **相关提交**：无

## P-006 测试代码污染候选考点

- **现象**：在技术派仓库上，候选考点一大半来自 `src/test/`，例如测试方法上的 `@Transactional`。
- **原因**：画像提取没有区分业务代码和测试代码。
- **解决**：引入 `is_test_path`，测试代码不进画像和候选；同类候选设上限（见 D-011）。
- **教训**：小样例跑通不代表真实仓库能用，要尽早拿真实仓库试。
- **相关提交**：后端主体提交（`app/analysis/spring_profile.py:126`）

## P-007 改动影响分析把删除的行算到了错误的方法上

- **现象**：改动理解模式里，一个只删除了几行的改动被判定影响了文件里的所有方法。
- **原因**：解析 diff 时只记录了新文件里的行号，被删除的行在新文件里不存在，回退成了整个文件。
- **解决**：`Hunk` 增加 `removed_at`，记录删除发生在新文件的哪一行附近，再定位所在方法。
- **教训**：diff 有"旧文件"和"新文件"两套行号，处理时要分清。
- **相关提交**：后端主体提交（`app/agents/change.py:33`）

## P-008 `.gitignore` 里的 `repos/` 把源码目录也忽略了

- **现象**：在 Docker 里启动 api 报 `ModuleNotFoundError: No module named 'app.repos'`，本机却一切正常。
- **原因**：`.gitignore` 里写的是 `repos/`（想忽略仓库存储目录），它会匹配任意层级的 `repos` 目录，`backend/app/repos/` 也被忽略，从没提交过。本机文件一直在，所以没发现。
- **解决**：改成 `/repos/`、`/data/`，只匹配仓库根目录。
- **教训**：`.gitignore` 的规则不带前导 `/` 时匹配所有层级；用 `git status --ignored` 检查有没有误伤；用干净环境（Docker、CI）构建能发现这类问题。
- **相关提交**：`M0: Docker Compose 一键部署；修正 .gitignore 误忽略 app/repos`

## P-009 全新数据库上第一次开考核请求永远卡住

- **现象**：Compose 全新启动后，点"开始考核"一直转圈，api 日志里没有任何报错；测试却全部通过。
- **原因**：第一次调用 `get_graph()` 时执行 `AsyncPostgresSaver.setup()`，里面的 `CREATE INDEX CONCURRENTLY` 要等数据库里所有已开始的事务结束；而这个请求自己的 SQLAlchemy 会话正开着事务（刚查过会话），于是自己等自己。测试库里检查点表早就建好了（迁移降级不会删它们），`setup()` 什么都不用做，所以测不出来。
- **解决**：建表移到进程启动时（见 D-014）；测试夹具每次先删掉检查点表再建，覆盖"全新数据库"这条路径。
- **教训**：只在"第一次"发生的逻辑最容易漏测；数据库里"一直等待、不报错"的问题，先查锁和 CONCURRENTLY。排查方法：在进程里打印所有 asyncio 任务的 await 链，直接看到卡在 `setup → execute`。
- **相关提交**：`M0: CI；修复全新数据库首次开考核卡死`

## P-010 npm 在安装完成后不退出

- **现象**：`npm install` 打印完结果后卡住，最后被终止，但依赖其实已经装好。
- **原因**：npm 退出前会请求审计和赞助信息接口，网络受限时一直等待。
- **解决**：加 `--no-audit --no-fund`（Dockerfile 和 CI 里都这样用）。
- **教训**：受限网络里，工具的"附加功能"往往是卡住的原因。
- **相关提交**：Docker Compose 提交

## P-011 Windows 上读夹具报 `'gbk' codec can't decode`

- **现象**：在 Windows 本机跑 `uv run pytest tests/test_unit_analysis.py`，4 个测试报 `UnicodeDecodeError: 'gbk' codec can't decode byte 0xa1`；CI（Linux）上一直是通过的。
- **原因**：`Path.read_text()` / `write_text()` 不写 `encoding` 时用系统默认编码，Linux 是 UTF-8，简体中文 Windows 是 GBK。夹具 Java 文件里有 UTF-8 的中文注释，按 GBK 解码就失败。业务代码里也有同样的写法：解析缓存 `pipeline.py` 用 `ensure_ascii=False` 写 JSON，在 Windows 上会按 GBK 写，遇到 GBK 表示不了的字符（比如 emoji）直接抛错，而外面只捕获了 `OSError`，整个分析任务会失败。同一次还发现测试里用 `str(p.relative_to(FIX))` 拼路径，Windows 上得到的是反斜杠，和流水线统一用的 `/` 对不上。
- **解决**：所有文本读写都显式写 `encoding="utf-8"`；测试里的相对路径改用 `.as_posix()`。子进程输出用的是 `bytes.decode()`，默认就是 UTF-8，不受影响。
- **教训**：只在 Linux 上跑过的代码，换到 Windows 首先查编码和路径分隔符。可以用 `rg -n "read_text\(|write_text\(|open\(" | rg -v encoding` 把没写编码的地方一次找全。
- **相关提交**：`修复：Windows 本机的文件编码和符号链接测试问题`

## P-012 Windows 上建符号链接报 `WinError 1314`

- **现象**：Windows 本机跑沙箱测试，3 个测试在夹具阶段报 `OSError: [WinError 1314] 客户端没有所需的特权`。
- **原因**：夹具里用 `os.symlink` 造了一个指向根目录外的符号链接，用来测沙箱拒绝读取。Windows 默认只有管理员或开了"开发者模式"的用户才能创建符号链接。符号链接放在三条测试共用的夹具里，所以三条一起报错，连路径穿越、敏感文件这些和符号链接无关的检查也跑不了。
- **解决**：符号链接单独拆成 `test_sandbox_symlink_escape`，建不了就 `pytest.skip` 并写明原因；其余三条测试不再依赖它。生产环境和 CI 都是 Linux，这条在那里照常执行。
- **教训**：依赖平台能力的准备步骤不要放进共用夹具，否则一个前提不满足会连带其他测试一起失败；跳过时把原因写进 skip 信息，`pytest -rs` 能看到。
- **相关提交**：`修复：Windows 本机的文件编码和符号链接测试问题`

## P-013 全新数据库上 api 和 worker 同时启动，建检查点表时死锁

- **现象**：Windows 本机第一次 `docker compose up -d --build`，命令报 `dependency failed to start: container project-b-api-1 is unhealthy`，frontend 没启动。api 日志里是 `psycopg.errors.DeadlockDetected`，出在启动时的 `setup_checkpointer()`；容器自动重启后又好了。在测试里让两个 `setup_checkpointer()` 同时跑，报的是另一个错：`UniqueViolation ... pg_type_typname_nsp_index`。
- **原因**：api 和 worker 都在 `migrate` 结束后立刻启动，各自调用 LangGraph 的 `setup()` 建检查点表。两边同时 `CREATE TABLE IF NOT EXISTS` 会撞系统表的唯一约束；同时对一张表 `CREATE INDEX CONCURRENTLY` 会互相等待，被 Postgres 判成死锁。D-014 当时以为 `setup()` 幂等就能多进程同时执行，但幂等只保证"做两遍结果一样"，不保证"同时做不冲突"。只有全新数据库会遇到：表建好以后 `setup()` 什么都不做。CI 的测试只调用一次 `setup()`，测不出来；服务器第一次部署时就算撞上，容器自动重启后也会恢复，不容易察觉。
- **解决**：`setup_checkpointer()` 先用 `pg_try_advisory_lock` 轮询抢锁，抢到再执行 `setup()`，一次只让一个进程建表。不能用阻塞的 `pg_advisory_lock`：阻塞等锁的那条语句本身是一个进行中的事务，`CREATE INDEX CONCURRENTLY` 要等所有已开始的事务结束，又会互等。测试夹具改成同时调两次 `setup_checkpointer()`，再加一个测试检查检查点索引都有效（被中断的 `CREATE INDEX CONCURRENTLY` 会留下 INVALID 的索引）。修复后用全新数据卷重新 `docker compose up`，api、worker 重启次数为 0。
- **教训**：多个进程启动时都要做的初始化，要么交给只跑一次的步骤（像 `migrate` 服务），要么加锁排队；"幂等"不等于"并发安全"。线上清库重建（B0-4 第 3 步）正好是全新数据库，这次修复要先部署上去。
- **相关提交**：`修复：检查点建表加锁排队；Windows 本机跑通完整测试`

## P-014 Windows 上 psycopg 异步模式报 `ProactorEventLoop`

- **现象**：Windows 本机跑集成测试，所有用到数据库的测试在准备阶段报 `psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode`。
- **原因**：Windows 上 asyncio 默认用 `ProactorEventLoop`，psycopg（LangGraph 的 Postgres 检查点在用）的异步模式不支持它，只能用 `SelectorEventLoop`。但 Windows 的 `SelectorEventLoop` 又不支持 `asyncio.create_subprocess_exec`，而调 git 和 ripgrep 用的正是它。两者不能同时满足。
- **解决**：`app/__init__.py` 里判断是 Windows 时把事件循环策略换成 `WindowsSelectorEventLoopPolicy`；git 和 ripgrep 改成在线程里跑 `subprocess.run`（`app/core/proc.py`），哪种事件循环都能用。超时由 `subprocess.run(timeout=...)` 负责，它会先杀掉子进程再抛 `TimeoutExpired`。容器里是 Linux，行为不变。
- **教训**：asyncio 的事件循环在 Windows 和 Linux 上默认实现不同，依赖底层 I/O 的库（数据库驱动、子进程）要分别确认。本机直接跑 api 时，`uvicorn --reload` 会自己用 `SelectorEventLoop`；不带 `--reload` 时 uvicorn 在 Windows 上用 `ProactorEventLoop`（`uvicorn/loops/asyncio.py`），psycopg 仍会报这个错。
- **相关提交**：`修复：检查点建表加锁排队；Windows 本机跑通完整测试`

## P-015 Windows 上 alembic 读 `alembic.ini` 报 `'gbk' codec can't decode`

- **现象**：Windows 本机跑集成测试，准备数据库时 alembic 报 `UnicodeDecodeError: 'gbk' codec can't decode byte 0xa8`。本机直接跑 `uv run alembic upgrade head` 也一样。
- **原因**：alembic 1.20 读配置文件时明确用 `encoding="locale"`（`alembic/util/compat.py` 的 `read_config_parser`），简体中文 Windows 上就是 GBK，而 `alembic.ini` 里有一行 UTF-8 的中文注释。这里没法像 P-011 那样在调用处指定编码。
- **解决**：把 `alembic.ini` 里唯一一行中文注释改成英文，并注明这个文件要保持纯 ASCII。
- **教训**：交给第三方库读的配置文件，编码由库决定；不确定时只写 ASCII。
- **相关提交**：`修复：检查点建表加锁排队；Windows 本机跑通完整测试`

## P-016 Python 代码不在仓库根目录时，跨模块调用全都解析不到

- **现象**：用本项目自己的后端试 Python 解析（B0-10），接口都抽出来了，但"作答"接口的链路走到路由层就断了，调不到 `app/sessions/service.py` 里的 `resume_stream`；3386 处调用只解析到 461 处。
- **原因**：模块名是按文件在仓库里的路径推的，`backend/app/sessions/service.py` 被叫成 `backend.app.sessions.service`；而代码里写的是 `from app.sessions import service`，别名表里记的是 `app.sessions.service`，两边对不上。夹具仓库的 Python 代码在根目录，测试测不出来。
- **解决**：先找出包的根目录（有 `pyproject.toml`、`setup.py`、`setup.cfg`、`requirements*.txt` 的目录），模块名相对最近的根目录算。修完后解析到 837 处，链路一直走到服务层。加了 `test_python_source_roots`。
- **教训**：夹具都放在"标准位置"时，测不出真实仓库的目录结构问题；新语言接进来之后，要拿一个真实仓库（最方便的就是本项目自己）跑一遍，看解析率和链路，而不只是看测试绿了。
- **相关提交**：`功能：语言接口与 Python 支持`

## P-017 学习文档里的行号悄悄跑偏，而且没人发现

- **现象**：写引用检查脚本（B0-13）时一跑，146 处 `文件:行号` 引用里 104 处有问题。`learn/M3.md` 里面试官图的节点函数整体偏了 3 行（`select_question` 写的是 160，实际是 163）；`learn/M0.md` 指向 `config.py:18` 的"所有配置项"其实已经指到一个常量上了。
- **原因**：
  - 代码每改一次，后面的行号就整体移动，文档不会跟着变；
  - 这次会话里每个提交都用脚本按 diff 对齐过行号，但脚本只认完整路径（`graph.py:160`），没认只写 `:160` 的简写，简写全部漏改；
  - 大部分引用没写函数名，偏了以后也看不出来；`config.py:18` 偏到的正好是另一个定义，看起来还挺像回事。
- **解决**：约定引用后面一律紧跟函数名（`` `graph.py:163` `select_question` ``），检查脚本要求"这一行出现这个名字（字符串里的不算），或它的定义在上下两行之内"，加进 CI；`--fix` 能按函数名自动改正行号。一次性补齐了全部函数名；补的时候，凡是指到的不是定义行的都列出来人工核对，又查出三处。
- **教训**：靠人记得同步的东西迟早会漂；能机器检查的约定就交给 CI。只认一种写法的对齐脚本会给人"已经对齐过了"的错觉。
- **相关提交**：`文档：学习文档引用检查加进 CI`

## P-018 分析本项目自己时，测试夹具里的代码被出成了考点

- **现象**：B0-12 用 DeepSeek 分析本项目自己，出了一道风险题"点赞链路中分布式锁、缓存与消息的一致性窗口"，证据全在 `backend/tests/fixtures/mini-feed/` 里。那是测试用的示例 Java 仓库，不是本项目的业务代码。
- **原因**：D-011 规定测试代码不进画像，但 v1 只在"关键用法"里排除了测试路径，接口抽取没排除。夹具的接口进了画像摘要，选考点的模型看到后，自己补了一个考点；考点校验只查证据行号存不存在，没查是不是测试代码。
- **解决**：画像里的接口排除测试路径；考点校验拒绝证据落在测试代码里的考点（模型补充的考点也一样），并把原因回传给模型重试。加了 `test_test_code_not_in_profile_or_topics`。
- **教训**：同一条规则（测试代码不进画像）分散在好几个抽取函数里各写一遍，漏一处就破功；规则最好在汇总的地方再兜一次底。本项目自己就是最好的测试仓库：它的 tests/ 里恰好放着另一个完整仓库。
- **相关提交**：`修复：测试代码不进画像和考点`

## P-019 没配嵌入模型时，Agent 调一次向量检索，整轮评测就崩了

- **现象**：本机跑探针评测（B0-15），只评 agentic 检索，跑到一半报 `LLMFatalError: 还没有配置嵌入模型`，整轮评测失败，前面花的钱也白花了。
- **原因**：评测给沙箱开了向量检索工具（`allow_vector=True`），agentic 检索里的模型自己选了 `vector_search`；这个工具要调嵌入模型，而沙箱只把"越权""参数错误"转成工具结果，模型服务的错误直接往外抛。线上开着 vector / hybrid 而嵌入模型不可用时，考核里也会这样。
- **解决**：沙箱把工具里的模型服务错误也转成工具结果，告诉 Agent 换用 grep 等工具；评测只在评 vector / hybrid 时才开向量工具。顺手修了评测运行器：仓库分析失败时抛的是 `SystemExit`，`finally` 里用到还没赋值的变量，把真正的错误盖住了；失败过的快照重新评测时也会重试。
- **教训**：交给模型调用的工具，任何异常都应该变成模型能读懂的结果，而不是让整条链路崩掉。
- **相关提交**：`修复：探针评测里发现的问题`

## P-020 Windows 上 ripgrep 输出反斜杠路径，检索结果全都对不上

- **现象**：探针评测里 agentic 检索的 recall@5 是 0：3 道题的标准证据一个都没找到，可 Agent 明明调了 18 次工具。
- **原因**：Windows 上 rg 默认输出 `.\src\a.py`。沙箱只去掉 `./` 前缀，路径还是反斜杠，和仓库里统一用的 `src/a.py` 对不上，后面按路径合并、比对证据全部落空。Linux（线上、CI）没有这个问题，所以以前没发现。
- **解决**：调 rg 时加 `--path-separator /`。修复后 recall@5 为 0.194（还是偏低，留给寒假的正式评测分析）。加了 `test_sandbox_grep_uses_forward_slashes`。
- **教训**：Windows 和 Linux 的差别不只是编码（P-011）和事件循环（P-014），外部命令的输出格式也不一样。指标为 0 时先怀疑度量本身。
- **相关提交**：`修复：探针评测里发现的问题`

## P-021 阅卷官把"不会"读成了"不会有问题"

- **现象**：探针评测里，第 2 题回答"不会"，两次运行阅卷官都记了一条矛盾："回答'不会'暗示在数据库事务里直接操作 Redis 没有问题"。分数是 0 没错，但报告里多了一条莫须有的矛盾。
- **原因**："不会"在中文里既可以是"我不会答"，也可以是"不会出问题"。题目问的正好是"会有什么问题"，模型选了后一种理解。
- **解决**：回答只有"不会""不知道"之类时，阅卷结果里的矛盾列表清空：没有任何关于代码的说法，谈不上矛盾。加了 `test_unknown_answer_is_not_a_claim`。
- **教训**：探针测试的价值就在这里：构造好的极端回答（"不会"），能暴露正常使用里很难注意到的问题。
- **相关提交**：`修复：探针评测里发现的问题`
