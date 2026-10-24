# 评测报告：probe（2026-10-24）

- 运行 id：`265e1119-bbaf-42f3-89d4-83aa7bb69d55`
- 模型：openai_compatible / 强 `deepseek-v4-pro` / 快 `deepseek-flash`
- 检索模式：agentic；基线对比：否
- 数据：3 题、15 个回答、0 条简历说法、0 个注入样本

## 出题质量

| 指标 | 值 |
|---|---|
| dataset_evidence_valid_rate | 1.0 |
| bank_evidence_valid_rate | 1.0 |
| bank_questions | 10 |
| human_rating | 待人工抽样打分（基于代码事实 / 像真实面试 / 是否重复，各 1-5 分） |

## 阅卷（与人工标注对比）

| 指标 | 值 |
|---|---|
| answers | 15 |
| point_agreement | 0.964 |
| cohen_kappa | 0.921 |
| score_mae | 3.13 |
| stability_3_runs | 0.958 |
| latency_s | 454.72 |
| tokens | 101071 |
| cost | 1.737981 |
| calls | 33 |

## 成本

| 指标 | 值 |
|---|---|
| loc | 11196 |
| first_analysis_s_per_kloc | 37.353 |
| first_analysis_tokens_per_kloc | 5797.7 |
| incremental_runs | 0 |
| incremental_tokens_saved | 0 |
| incremental_reuse_ratio | None |
| avg_session_cost | None |
| avg_session_tokens | None |

## 逐个回答

| 回答 | 类型 | 系统得分 | 标注得分 | 系统要点判定 | 标注要点判定 | 指出的矛盾 |
|---|---|---|---|---|---|---|
| p1-complete | complete | 100 | 95 | hit/hit/hit/hit | hit/hit/hit/hit | （无） |
| p1-partial | partial | 35 | 40 | hit/miss/partial/miss | hit/miss/partial/miss | （无） |
| p1-wrong | wrong | 0 | 5 | miss/miss/miss/miss | miss/miss/miss/miss | 每次点赞都直接 UPDATE 数据库的 like_count = like_count + 1；Redis 只用来缓存文章详情；靠数据库行锁保证不丢更新 |
| p1-contradict | contradict | 10 | 10 | partial/miss/miss/miss | partial/miss/miss/miss | 定时任务写回的时候把 Redis 里攒的增量加到库里，写完就把 Redis 里的计数清零。；这样写回是增量的，所以必须保证任务只执行一次。 |
| p1-unknown | unknown | 0 | 0 | miss/miss/miss/miss | miss/miss/miss/miss | （无） |
| p2-complete | complete | 100 | 95 | hit/hit/hit | hit/hit/hit | （无） |
| p2-partial | partial | 33 | 35 | hit/miss/miss | hit/miss/miss | （无） |
| p2-wrong | wrong | 0 | 0 | miss/miss/miss | miss/miss/miss | Spring 的 @Transactional 会把 Redis 操作也包进事务里，回滚的时候一起回滚。 |
| p2-contradict | contradict | 0 | 10 | miss/miss/miss | partial/miss/miss | 项目里是在事务开始之前先把 Redis 计数加上，事务失败了再在 catch 里手动减回去。 |
| p2-unknown | unknown | 0 | 0 | miss/miss/miss | miss/miss/miss | 回答“不会”暗示在数据库事务里直接操作 Redis 没有问题。 |
| p3-complete | complete | 100 | 95 | hit/hit/hit/hit | hit/hit/hit/hit | （无） |
| p3-partial | partial | 40 | 50 | partial/hit/miss/miss | hit/hit/miss/miss | （无） |
| p3-wrong | wrong | 0 | 0 | miss/miss/miss/miss | miss/miss/miss/miss | 用 RocketMQ 的事务消息，先发半消息，本地事务成功后再提交消息。 |
| p3-contradict | contradict | 0 | 0 | miss/miss/miss/miss | miss/miss/miss/miss | 项目没有用 outbox 表；项目是在事务里直接调用 rabbitTemplate 发消息，发完再提交事务 |
| p3-unknown | unknown | 0 | 0 | miss/miss/miss/miss | miss/miss/miss/miss | （无） |

## 检索（三种模式对比）

| 模式 | recall@5 | 工具调用 | Token | 耗时(s) |
|---|---|---|---|---|
| agentic | 0.194 | 23 | 35649 | 11.59 |

## 结论

- 阅卷要点一致率 0.964，kappa 0.921，总分平均绝对误差 3.13。
- 检索召回最高的是 agentic（recall@5=0.194）。

## 失败案例分析

### retrieval（1 条）
- [agentic] p3 top-5 里没有任何标准证据

## 人工解读（AI 写，等用户核对）

- **能不能分出高低**：能。完整回答都是 100 分，只答一半的 33–40 分，答错和与代码矛盾的 0–10 分，"不会"0 分。和按构造给的标注比，要点一致率 0.964、kappa 0.921、总分平均误差 3.1 分。同一天跑了两次，第一次是 0.945 / 0.883 / 3.7，结论一致。
- **矛盾有没有被指出来**：3 个"与代码矛盾"的回答，矛盾的那句话都被逐条指出来了；"答错"的回答也都被指出和代码不符。
- **发现的问题**：
  - "不会"被当成了一条说法，记成矛盾，已修（P-021）；
  - Windows 上检索路径对不上，第一次 recall 是 0，已修（P-020），修复后 recall@5 是 0.194，仍然偏低；
  - 没配嵌入模型时整轮评测会崩，已修（P-019）。
- **局限**：
  - 题目、回答、标注都是 AI 根据项目 A 的学习文档写的，不是人工标注；分出高低只说明阅卷对"构造得很极端的回答"有效，真实回答往往介于两者之间；
  - 要请你按 `eval/datasets/probe/README.md` 核对这 15 个回答是不是真的"完整、一半、错误、矛盾、不会"。
- **费用**：两次运行各约 1.7–1.8 元（DeepSeek 高峰价），主要花在阅卷上：15 个回答各判一次，再挑 6 个各判 3 次看稳定性。
