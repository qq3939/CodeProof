# 嵌入模型选型对比（ROADMAP B0-4 第 1 步）

> 资料查询日期：2026-10-23。价格和限制都来自各家官方文档（链接见文末），没有用真实 Key 实测过；表里的检索分数是公开基准的成绩，不是在本项目上测出来的。本项目上的真实效果要等 B3-6 的评测（检索 recall@k）。

## 结论

> **已选定（2026-10-23）**：阿里云百炼 `text-embedding-v4`，`EMBEDDING_DIM=1024`，见 D-018。

**推荐：阿里云百炼 `text-embedding-v4`，`EMBEDDING_DIM=1024`。** 备选是硅基流动的 `Qwen/Qwen3-Embedding-8B`（同一系列的开源模型）。需要你定的只有两件事：用哪一家，维度是不是 1024。

理由：

1. **中文和代码都强。** 阿里云官方说 v4 属于 Qwen3-Embedding 系列。Qwen3-Embedding 4B / 8B 在中文基准 C-MTEB 上是 72.27 / 73.84，在代码检索基准 MTEB-Code 上是 80.06 / 80.68；同一份报告里 BGE-M3 的 MTEB-Code 是 58.22，OpenAI text-embedding-3-large 是 58.95。本项目的向量检索既要搜 Java 代码，也要搜中文笔记，两头都要顾到。
2. **接口兼容、参数清楚。** 官方有专门的"OpenAI 兼容"文档，`dimensions` 参数直接可用，单次条数和单条长度都写明了。
3. **付款方便。** 国内平台，支付宝付款，不需要外币卡；服务器在香港，访问国内公网接口没有地区限制。
4. **价格可以忽略。** 每百万 token 0.5 元。按 `client.py` 里"4 个字符约 1 个 token"的估算，一个 10 万行的 Java 仓库全量建索引大约 100 万 token，也就是 0.5 元左右；知识库笔记更少。全年预计在几元以内（估算，未实测）。

**为什么是 1024 维**：这是 v4 的默认维度；比现在的 256 检索效果好，又低于 pgvector HNSW 索引 2000 维的上限。每个向量 4 KB，几万个代码块也只占一两百 MB 磁盘。代价是要清库重建（`EMBEDDING_DIM` 改了，迁移建的向量列就对不上），而现在线上没有真实用户，正是清库的时机。如果不想清库，可以保留 256：上面几家都支持 256 维，但维度截得越短，检索效果损失越大，B3-6 里向量检索这一组的成绩会被压低，和 agentic 检索的对比就不公平了。

## 对比表

| 服务 / 模型 | 价格 | 可选维度 | 单次条数 / 单条长度 | 中文（C-MTEB） | 代码（MTEB-Code） | 接口 | 付款 |
|---|---|---|---|---|---|---|---|
| **阿里云百炼 `text-embedding-v4`** | 0.5 元 / 百万 token | 64–2048（8 档），默认 1024 | 10 条 / 8192 token | 官方未单独公布；同系列 4B/8B 为 72.27 / 73.84 | 同系列 4B/8B 为 80.06 / 80.68 | 有官方 OpenAI 兼容文档，支持 `dimensions` | 支付宝，需实名 |
| 硅基流动 `Qwen/Qwen3-Embedding-8B` | 国内站价格没查到；国际站 $0.04 / 百万 token | 64–4096（8 档） | 32 条 / 32768 token（文档另一处写 512，有矛盾） | 73.84 | 80.68 | OpenAI 风格，支持 `dimensions` | 支付宝 |
| 硅基流动 `Qwen/Qwen3-Embedding-0.6B` | 同上，未查到 | 64–1024 | 同上 | 66.33 | 75.41 | 同上 | 支付宝 |
| 硅基流动 `BAAI/bge-m3` | **免费** | 固定 1024，不支持 `dimensions` | 32 条 / 8192 token | 同一份报告里没有 | 58.22 | 同上 | 支付宝 |
| 智谱 `embedding-3` | 0.5 元 / 百万 token | 256–2048，默认 2048 | 64 条 / 3072 token | 没查到公开成绩 | 没查到公开成绩 | 请求格式像 OpenAI，但官方没写兼容声明 | 支付宝 |
| Voyage `voyage-code-4` | $0.12 / 百万 token，送 2 亿 token | 256 / 512 / 1024 / 2048 | 单条 32000 token | 没查到 | 只有厂商自己的对比数据 | 维度参数叫 `output_dimension`，不是 `dimensions` | 外币卡 |
| OpenAI `text-embedding-3-*` | — | — | — | — | large 为 58.95 | 原生 | **排除**：香港不在 OpenAI API 支持的地区，服务器在香港 |
| DeepSeek | — | — | — | — | — | — | **没有嵌入模型**：官方价格页只列了两个对话模型 |

"中文"和"代码"两列来自 Qwen3-Embedding 技术报告（arXiv 2506.05176）的表 3 和表 9，同一份报告、同一口径，便于横向比。注意两点：阿里云没说 v4 对应哪个尺寸，所以 v4 那一行只能参考；硅基流动提供的是开源权重本身，分数可以直接对应。

**和 DeepSeek 共用一个 Key**：哪一家都做不到，因为 DeepSeek 没有嵌入接口。所以 `.env` 里要单独填 `EMBEDDING_BASE_URL` 和 `EMBEDDING_API_KEY`（留空会沿用 DeepSeek 的地址和 Key，正是 ROADMAP 第 1 节担心的线上故障）。

## 各候选的补充说明

- **阿里云百炼 `text-embedding-v4`**：接口地址是 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`，`WorkspaceId` 在百炼控制台的业务空间详情页查（旧域名 `dashscope.aliyuncs.com` 还能用）。同一页还有更新的 `qwen3.7-text-embedding`（维度最高 2560、单次 20 条、单条 128K token，北京地域价格和 v4 相同），但它刚出，公开成绩少，评测报告里不好引用，所以不推荐。
- **硅基流动 Qwen3-Embedding-8B**：好处是"用的就是论文里那个模型"，评测报告写模型名最准确。缺点是国内站价格没查到，而且文档对单条长度有两种说法（32768 和 512）；如果真是 512 token，本项目最长 6000 字符的代码块会超限，必须先实测。
- **硅基流动 bge-m3**：免费，但代码检索比 Qwen3 系列低二十多分，而且维度固定 1024。它会把 B3-6 里向量检索这一组拖得很低，结论变成"向量检索不行"，其实只是嵌入模型弱。
- **智谱 embedding-3**：价格和阿里云一样，单次 64 条正好和代码里现在的批大小一致。问题是找不到公开的检索成绩，单条上限 3072 token 也偏短。
- **Voyage voyage-code-4**：专门为代码检索做的，有大量免费额度。但只有厂商自己的对比数据，中文效果不清楚，要外币卡，从香港访问没验证过；维度参数名也不一样，要另写代码。

## 接入时要改的代码（B0-4 第 2 步，你选定后做）

现在的写法只适配了 OpenAI，换成上面任何一家都要改三处：

1. **`dimensions` 只对 `text-embedding-3` 传**（`backend/app/llm/providers.py:189`）。换成别的模型时这个参数不会发出去，服务商会返回默认维度（v4 是 1024、Qwen3-8B 是 4096、智谱是 2048），和 `EMBEDDING_DIM` 对不上就写不进数据库。改成可配置，并加测试。
2. **批大小写死 64**（`backend/app/llm/client.py:281`）。阿里云单次最多 10 条、硅基流动最多 32 条，64 条一批会被拒绝。改成可配置（比如 `EMBEDDING_BATCH_SIZE`）。
3. **返回格式**：openai SDK 不指定 `encoding_format` 时默认要 base64（`.venv/Lib/site-packages/openai/resources/embeddings.py:110`），不是每家都支持。langchain-openai 的文档也建议非 OpenAI 服务商显式用 `float`。接入时固定传 `encoding_format="float"`。

改完后在线上上传一篇笔记、跑一次知识库检索，就能确认三处都对了（B0-4 第 4 步）。

## 资料来源

- 阿里云百炼 OpenAI 兼容嵌入接口：https://help.aliyun.com/zh/model-studio/embedding-interfaces-compatible-with-openai
- 阿里云百炼 `qwen3.7-text-embedding`：https://www.alibabacloud.com/help/en/model-studio/developer-reference/text-embedding-synchronous-api
- 智谱 embedding-3：https://docs.bigmodel.cn/cn/guide/models/embedding/embedding-3
- 硅基流动嵌入接口：https://docs.siliconflow.com/cn/api-reference/embeddings/create-embeddings.md
- 硅基流动价格（bge-m3 免费）：https://siliconflow.cn/pricing
- 硅基流动国际站 Qwen3-Embedding-8B：https://www.siliconflow.com/models/qwen3-embedding-8b
- Qwen3-Embedding 模型卡：https://huggingface.co/Qwen/Qwen3-Embedding-8B
- Qwen3-Embedding 技术报告（表 3、表 9）：https://arxiv.org/abs/2506.05176
- Voyage 价格：https://docs.voyageai.com/docs/pricing
- voyage-code-4 维度和发布时间：https://openrouter.ai/voyageai/voyage-code-4
- DeepSeek 价格页（只列对话模型）：https://api-docs.deepseek.com/quick_start/pricing
- OpenAI 支持的国家和地区：https://help.openai.com/zh-hant-hk/articles/5347006-openai-api-supported-countries-and-territories
- OpenAI 2024 年起限制中国大陆和香港调用 API：https://www.scmp.com/tech/big-tech/article/3268233/microsoft-maintains-ai-services-hong-kong-openai-curbs-api-access-china
