# 评测报告：small（2026-10-22）

- 运行 id：`27e283df-2f0e-43dc-8b7d-76adb2d4f6ab`
- 模型：fake / 强 `fake-strong` / 快 `fake-fast`
- 检索模式：agentic, vector, hybrid；基线对比：否
- 数据：6 题、23 个回答、6 条简历说法、5 个注入样本

> ⚠️ 这次用的是假模型（LLM_PROVIDER=fake），只能说明流程跑通、输出格式正确，指标数值没有参考意义。

## 出题质量

| 指标 | 值 |
|---|---|
| dataset_evidence_valid_rate | 1.0 |
| bank_evidence_valid_rate | 1.0 |
| bank_questions | 19 |
| human_rating | 待人工抽样打分（基于代码事实 / 像真实面试 / 是否重复，各 1-5 分） |

## 阅卷（与人工标注对比）

| 指标 | 值 |
|---|---|
| answers | 23 |
| point_agreement | 0.897 |
| cohen_kappa | 0.793 |
| score_mae | 8.65 |
| stability_3_runs | 1.0 |
| latency_s | 0.3 |
| tokens | 34036 |
| cost | 0.0 |
| calls | 41 |

## 简历核验

| 指标 | 值 |
|---|---|
| claims | 6 |
| false_claim_precision | 1.0 |
| false_claim_recall | 1.0 |
| tokens | 5936 |
| cost | 0.0 |
| calls | 26 |

## 安全（提示注入）

| 指标 | 值 |
|---|---|
| samples | 5 |
| executed | 0 |
| executed_rate | 0.0 |

## 成本

| 指标 | 值 |
|---|---|
| loc | 242 |
| first_analysis_s_per_kloc | 6.103 |
| first_analysis_tokens_per_kloc | 110876.0 |
| incremental_runs | 0 |
| incremental_tokens_saved | 0 |
| incremental_reuse_ratio | None |
| avg_session_cost | None |
| avg_session_tokens | None |

## 检索（三种模式对比）

| 模式 | recall@5 | 工具调用 | Token | 耗时(s) |
|---|---|---|---|---|
| agentic | 0.833 | 20 | 1080 | 0.29 |
| vector | 0.722 | 6 | 0 | 0.03 |
| hybrid | 0.944 | 26 | 1080 | 0.32 |

## 结论

- 阅卷要点一致率 0.897，kappa 0.793，总分平均绝对误差 8.65。
- 检索召回最高的是 hybrid（recall@5=0.944）。
- 注入样本被执行比例 0.0（目标 0）。

## 失败案例分析

### grading（2 条）
- q2-wrong（wrong）：系统 50 分，人工 5 分
- q6-partial（partial）：系统 0 分，人工 50 分

### retrieval（2 条）
- [agentic] q1 top-5 里没有任何标准证据
- [vector] q4 top-5 里没有任何标准证据
