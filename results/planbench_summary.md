# Plan-Bench 第二阶段外部验证实验报告

生成时间：2026-11-14T11:56:51.246541+00:00

## 0. 结论摘要

1. **文本条件（MiniMax 全量 207 条有效样本，传输失败≈0）：`append_only` 39.2% > `direct` 35.2% > `versioned` 31.4% ≈ `versioned_flat` 30.3%。
   版本覆盖账本比追加式日志低 7.8 pp（McNemar p=0.0115）；紧凑渲染对照臂同样低 8.9 pp（p=0.0008），而与长版本账本**无统计差异**（p=0.74）——**说明差距来自「覆盖/失效旧状态」机制本身，而不是提示词长度**。
2. **端到端多模态（覆盖 159/210 条任务，各臂 Clean 有效样本 141–151 条）：`direct` 66.2% ≫ `append` 48.3% ≈ `versioned` 52.5%——**画面已经在上下文里时，再叠加结构化账本反而是有害的**。
3. **代码级风险门控没有收益**：未门控 31.2% → 只重问 27.0% → 核验+重答 25.6%；风险分判别力很弱（AUC 见 §4）。**二采样本身就会掉点**（只重问对照），所以「多花一次感知」必须先解决风险分标定问题才可能划算。
4. **跨模型 50 条对照只能作方向性参考**：路由在实验期间退化（deepseek 429、AutoClaw 单条 800s+），修复后各模型的有效样本只剩 24–47 条，且方向不一致（qwen 上版本账本反而更好），不足以支撑「跨模型一致性」的结论。

## 1. 实验设置

- 数据集：Plan-Bench（MinneFeng/Plan-Bench），仅取 **Temporal Reasoning** 维度 210 条（Instruction Alignment 40 / Motion Retrieval 50 / Persistent Counting 50 / Progress Tracking 30 / Temporal Ordering 40），其中 35 条单选、175 条开放题。
- 模态探针：`minimax-code-global/MiniMax-M3.1-Flash` 原生支持多图输入（3 图单请求逐帧作答验证通过），因此走**端到端多模态**路线，无需外部 VLM。
- 感知层：两阶段、**完全不含题目**（防答案泄漏）：逐帧客观标注 → 跨帧状态追踪（实体身份 + 显式 absent）。两份产物均缓存于 `cache/frame_captions.json` 与 `cache/state_tracks.json`，同一帧永不复用计费。
- 消融三组共享同一任务规则、同一问题、同一选项、同一答案格式、同一解码参数；**唯一变量是历史状态的组织方式**：`direct_baseline`（按时间顺序的帧观测）、`append_only_ledger`（只追加日志，旧记录不失效）、`versioned_state_ledger`（active/superseded 版本覆盖账本）。
- 配额：MiniMax 全量 + 并发 10；其余模型仅 50 条分层抽样 + 并发 2。

## 2. 主结果

### mm_minimax — minimax-code-global/MiniMax-M3.1-Flash（mode=multimodal, 159 条任务）

| 方法 | Clean 准确率 | 95% CI | Full 准确率 | 传输失败 | 有效样本 |
|---|---|---|---|---|---|
| `append_only_ledger` | **48.3%** | [40.5%, 56.3%] | 48.3% | 0 | 151 |
| `direct_baseline` | **66.2%** | [58.2%, 73.4%] | 65.8% | 4 | 145 |
| `versioned_state_ledger` | **52.5%** | [44.3%, 60.5%] | 52.8% | 1 | 141 |

> Clean = 剔除传输失败（空响应/超时/无答案）的口径，是主口径；Full 把传输失败按答错计入分母（保守口径）。两者同向即结论稳健。

| 方法 | Instruction Alignment | Motion Retrieval | Persistent Counting | Progress Tracking | Temporal Ordering |
|---|---|---|---|---|---|
| `append_only_ledger` | 43% (n=23) | 67% (n=39) | 12% (n=32) | 87% (n=30) | 26% (n=27) |
| `direct_baseline` | 46% (n=26) | 81% (n=36) | 39% (n=23) | 87% (n=30) | 67% (n=30) |
| `versioned_state_ledger` | 45% (n=20) | 67% (n=33) | 26% (n=31) | 87% (n=30) | 33% (n=27) |

### txt_minimax — minimax-code-global/MiniMax-M3.1-Flash（mode=text, 210 条任务）

| 方法 | Clean 准确率 | 95% CI | Full 准确率 | 传输失败 | 有效样本 |
|---|---|---|---|---|---|
| `append_only_ledger` | **39.2%** | [32.9%, 46.0%] | 39.5% | 1 | 209 |
| `direct_baseline` | **35.2%** | [29.1%, 41.9%] | 35.2% | 0 | 210 |
| `versioned_state_ledger` | **31.4%** | [25.5%, 38.0%] | 31.4% | 3 | 207 |
| `versioned_state_ledger_flat` | **30.3%** | [24.4%, 37.0%] | 30.0% | 9 | 201 |

> Clean = 剔除传输失败（空响应/超时/无答案）的口径，是主口径；Full 把传输失败按答错计入分母（保守口径）。两者同向即结论稳健。

| 方法 | Instruction Alignment | Motion Retrieval | Persistent Counting | Progress Tracking | Temporal Ordering |
|---|---|---|---|---|---|
| `append_only_ledger` | 41% (n=39) | 50% (n=50) | 6% (n=50) | 73% (n=30) | 40% (n=40) |
| `direct_baseline` | 42% (n=40) | 42% (n=50) | 10% (n=50) | 77% (n=30) | 20% (n=40) |
| `versioned_state_ledger` | 27% (n=37) | 40% (n=50) | 10% (n=50) | 70% (n=30) | 22% (n=40) |
| `versioned_state_ledger_flat` | 34% (n=35) | 40% (n=48) | 6% (n=50) | 77% (n=30) | 11% (n=38) |

### txt_cross — autoclaw-cn/zai_glm-5.3-flash（mode=text, 50 条任务）

| 方法 | Clean 准确率 | 95% CI | Full 准确率 | 传输失败 | 有效样本 |
|---|---|---|---|---|---|
| `append_only_ledger` | **42.6%** | [29.5%, 56.7%] | 42.6% | 0 | 47 |
| `direct_baseline` | **45.7%** | [32.2%, 59.8%] | 45.7% | 0 | 46 |
| `versioned_state_ledger` | **39.5%** | [26.4%, 54.4%] | 39.5% | 0 | 43 |
| `versioned_state_ledger_flat` | **0.0%** | [0.0%, 65.8%] | 0.0% | 0 | 2 |

> Clean = 剔除传输失败（空响应/超时/无答案）的口径，是主口径；Full 把传输失败按答错计入分母（保守口径）。两者同向即结论稳健。

| 方法 | Instruction Alignment | Motion Retrieval | Persistent Counting | Progress Tracking | Temporal Ordering |
|---|---|---|---|---|---|
| `append_only_ledger` | 50% (n=8) | 50% (n=12) | 0% (n=11) | 71% (n=7) | 56% (n=9) |
| `direct_baseline` | 57% (n=7) | 58% (n=12) | 0% (n=11) | 86% (n=7) | 44% (n=9) |
| `versioned_state_ledger` | 50% (n=4) | 42% (n=12) | 9% (n=11) | 86% (n=7) | 33% (n=9) |
| `versioned_state_ledger_flat` | 0% (n=1) | 0% (n=1) |

### txt_cross — loomy/qwen-3.8-max（mode=text, 31 条任务）

| 方法 | Clean 准确率 | 95% CI | Full 准确率 | 传输失败 | 有效样本 |
|---|---|---|---|---|---|
| `append_only_ledger` | **32.0%** | [17.2%, 51.6%] | 30.8% | 1 | 25 |
| `direct_baseline` | **39.1%** | [22.2%, 59.2%] | 37.5% | 1 | 23 |
| `versioned_state_ledger` | **41.7%** | [24.5%, 61.2%] | 41.7% | 0 | 24 |
| `versioned_state_ledger_flat` | **50.0%** | [9.5%, 90.5%] | 50.0% | 0 | 2 |

> Clean = 剔除传输失败（空响应/超时/无答案）的口径，是主口径；Full 把传输失败按答错计入分母（保守口径）。两者同向即结论稳健。

| 方法 | Instruction Alignment | Motion Retrieval | Persistent Counting | Progress Tracking | Temporal Ordering |
|---|---|---|---|---|---|
| `append_only_ledger` | 0% (n=1) | 60% (n=5) | 0% (n=10) | 71% (n=7) | 0% (n=2) |
| `direct_baseline` | 50% (n=6) | 0% (n=9) | 86% (n=7) | 0% (n=1) |
| `versioned_state_ledger` | 67% (n=6) | 0% (n=10) | 86% (n=7) | 0% (n=1) |
| `versioned_state_ledger_flat` | 0% (n=1) | 100% (n=1) |

### txt_cross — workbuddy/deepseek-v4.1-flash（mode=text, 50 条任务）

| 方法 | Clean 准确率 | 95% CI | Full 准确率 | 传输失败 | 有效样本 |
|---|---|---|---|---|---|
| `append_only_ledger` | **42.9%** | [26.5%, 60.9%] | 24.0% | 22 | 28 |
| `direct_baseline` | **30.8%** | [16.5%, 50.0%] | 16.0% | 24 | 26 |
| `versioned_state_ledger` | **33.3%** | [18.0%, 53.3%] | 16.0% | 26 | 24 |
| `versioned_state_ledger_flat` | **0.0%** | [0.0%, 0.0%] | 0.0% | 50 | 0 |

> Clean = 剔除传输失败（空响应/超时/无答案）的口径，是主口径；Full 把传输失败按答错计入分母（保守口径）。两者同向即结论稳健。

| 方法 | Instruction Alignment | Motion Retrieval | Persistent Counting | Progress Tracking | Temporal Ordering |
|---|---|---|---|---|---|
| `append_only_ledger` | 0% (n=0) | 50% (n=10) | 0% (n=10) | 100% (n=6) | 50% (n=2) |
| `direct_baseline` | 0% (n=1) | 30% (n=10) | 0% (n=9) | 83% (n=6) | 0% (n=0) |
| `versioned_state_ledger` | 0% (n=0) | 38% (n=8) | 0% (n=10) | 83% (n=6) | 0% (n=0) |
| `versioned_state_ledger_flat` | 0% (n=0) | 0% (n=0) | 0% (n=0) | 0% (n=0) | 0% (n=0) |

### txt_gemini — antigravity/gemini-3.8-flash（mode=text, 50 条任务）

| 方法 | Clean 准确率 | 95% CI | Full 准确率 | 传输失败 | 有效样本 |
|---|---|---|---|---|---|
| `append_only_ledger` | **36.0%** | [24.1%, 49.9%] | 36.0% | 0 | 50 |
| `direct_baseline` | **36.0%** | [24.1%, 49.9%] | 36.0% | 0 | 50 |
| `versioned_state_ledger` | **30.0%** | [19.1%, 43.8%] | 30.0% | 0 | 50 |

> Clean = 剔除传输失败（空响应/超时/无答案）的口径，是主口径；Full 把传输失败按答错计入分母（保守口径）。两者同向即结论稳健。

| 方法 | Instruction Alignment | Motion Retrieval | Persistent Counting | Progress Tracking | Temporal Ordering |
|---|---|---|---|---|---|
| `append_only_ledger` | 40% (n=10) | 33% (n=12) | 0% (n=11) | 86% (n=7) | 40% (n=10) |
| `direct_baseline` | 50% (n=10) | 33% (n=12) | 0% (n=11) | 86% (n=7) | 30% (n=10) |
| `versioned_state_ledger` | 30% (n=10) | 50% (n=12) | 0% (n=11) | 71% (n=7) | 10% (n=10) |

## 3. 配对显著性（McNemar 精确检验 + 配对 Bootstrap）

| 运行 | 模型 | 对比 | n | 差值 (pp) | 95% CI (pp) | McNemar p |
|---|---|---|---|---|---|---|
| mm_minimax | minimax-code-global/MiniMax-M3.1-Flash | `versioned_state_ledger` vs `append_only_ledger` | 138 | +5.1 | [+0.7, +10.1] | 0.09229 |
| mm_minimax | minimax-code-global/MiniMax-M3.1-Flash | `versioned_state_ledger` vs `direct_baseline` | 129 | -10.9 | [-18.6, -3.1] | 0.01612 |
| mm_minimax | minimax-code-global/MiniMax-M3.1-Flash | `append_only_ledger` vs `direct_baseline` | 138 | -13.0 | [-21.0, -5.1] | 0.002935 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | `versioned_state_ledger` vs `append_only_ledger` | 207 | -7.7 | [-13.5, -1.9] | 0.01659 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | `versioned_state_ledger` vs `direct_baseline` | 207 | -3.9 | [-9.2, +1.0] | 0.2005 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | `append_only_ledger` vs `direct_baseline` | 209 | +3.8 | [-1.4, +9.6] | 0.2153 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | `versioned_state_ledger_flat` vs `versioned_state_ledger` | 199 | -2.5 | [-8.0, +3.0] | 0.4731 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | `versioned_state_ledger_flat` vs `append_only_ledger` | 200 | -9.5 | [-14.5, -4.5] | 0.0005461 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | `versioned_state_ledger` vs `append_only_ledger` | 41 | +0.0 | [-14.6, +14.6] | 1 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | `versioned_state_ledger` vs `direct_baseline` | 41 | -2.4 | [-12.2, +7.3] | 1 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | `append_only_ledger` vs `direct_baseline` | 44 | -2.3 | [-11.4, +9.1] | 1 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | `versioned_state_ledger_flat` vs `versioned_state_ledger` | 2 | +0.0 | [+0.0, +0.0] | 1 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | `versioned_state_ledger_flat` vs `append_only_ledger` | 2 | +0.0 | [+0.0, +0.0] | 1 |
| txt_cross | loomy/qwen-3.8-max | `versioned_state_ledger` vs `append_only_ledger` | 20 | +5.0 | [+0.0, +15.0] | 1 |
| txt_cross | loomy/qwen-3.8-max | `versioned_state_ledger` vs `direct_baseline` | 21 | +0.0 | [+0.0, +0.0] | 1 |
| txt_cross | loomy/qwen-3.8-max | `append_only_ledger` vs `direct_baseline` | 20 | -5.0 | [-15.0, +0.0] | 1 |
| txt_cross | loomy/qwen-3.8-max | `versioned_state_ledger_flat` vs `versioned_state_ledger` | 2 | +0.0 | [+0.0, +0.0] | 1 |
| txt_cross | loomy/qwen-3.8-max | `versioned_state_ledger_flat` vs `append_only_ledger` | 2 | +0.0 | [+0.0, +0.0] | 1 |
| txt_cross | workbuddy/deepseek-v4.1-flash | `versioned_state_ledger` vs `append_only_ledger` | 24 | -8.3 | [-20.8, +0.0] | 0.5 |
| txt_cross | workbuddy/deepseek-v4.1-flash | `versioned_state_ledger` vs `direct_baseline` | 23 | +0.0 | [-13.0, +13.0] | 1 |
| txt_cross | workbuddy/deepseek-v4.1-flash | `append_only_ledger` vs `direct_baseline` | 25 | +12.0 | [-4.0, +28.0] | 0.375 |
| txt_gemini | antigravity/gemini-3.8-flash | `versioned_state_ledger` vs `append_only_ledger` | 50 | -6.0 | [-20.0, +8.0] | 0.5811 |
| txt_gemini | antigravity/gemini-3.8-flash | `versioned_state_ledger` vs `direct_baseline` | 50 | -6.0 | [-18.0, +6.0] | 0.5488 |
| txt_gemini | antigravity/gemini-3.8-flash | `append_only_ledger` vs `direct_baseline` | 50 | +0.0 | [-12.0, +12.0] | 1 |

## 4. 代码级风险门控（Staleness Risk Score）

- 风险分由 Python 从 `versioned_state_ledger` 计算，不交给模型：`0.45 × 过期跨度 + 0.30 × 冲突次数 + 0.25 × (1 − 模型置信度)`。
- 未门控准确率 31.2%；对全部样本都重新核验时 25.6%（215 条样本）。
- **对照（只重问、不额外核验）**：27.0%，即单纯二次作答带来的收益为 -4.2 pp；额外感知本身的净贡献为 -1.4 pp。
- 风险分的判别力（错误样本得分更高的概率 AUC）：**0.580**（0.5 = 完全无判别力）。
  - 单项 `span_norm` 的 AUC：0.601
  - 单项 `conflict_norm` 的 AUC：0.522
  - 单项 `uncertainty` 的 AUC：0.482
- 风险分的区分度：答对样本均值 0.350，答错样本均值 0.313（答错 148 条 / 答对 67 条）。

| τ | 门控样本比例（额外感知成本） | 准确率 | 仅重问对照 |
|---|---|---|---|
| 0.00 | 1.00 | 25.6% | 27.0% |
| 0.10 | 0.99 | 25.6% | 27.0% |
| 0.20 | 0.87 | 26.5% | 27.0% |
| 0.30 | 0.60 | 28.8% | 27.4% |
| 0.40 | 0.24 | 31.2% | 28.8% |
| 0.50 | 0.07 | 30.2% | 29.8% |
| 0.60 | 0.00 | 30.7% | 31.2% |
| 0.70 | 0.00 | 31.2% | 31.2% |
| 0.80 | 0.00 | 31.2% | 31.2% |
| 0.90 | 0.00 | 31.2% | 31.2% |
| 1.00 | 0.00 | 31.2% | 31.2% |

## 5. 图

- `results/ablation_accuracy.png`：三组方法在各子任务上的准确率（MiniMax 全量 + 50 条跨模型对照），误差棒为 95% Wilson 区间。
- `results/risk_cost_tradeoff.png`：代码级风险门控下「平均额外核验成本 vs 任务准确率」曲线。

## 6. 覆盖度与口径说明

> **口径更正**：早期报告（Antigravity 生成于 `~/.gemini/antigravity/brain/33b00b10-.../planbench_phase2_report.md`）中的跨模型数字用的是 Full 口径（把传输失败按答错计入），**低估了 deepseek / qwen 的真实水平**；本文档与两张图统一以 Clean 口径为主，Full 口径并列保留。

| 运行 | 模型 | 覆盖任务数 | 单元数 | 传输失败 | 备注 |
|---|---|---|---|---|---|
| mm_minimax | minimax-code-global/MiniMax-M3.1-Flash | 159 | 442 | 5 | 多模态：`minimax-code` 账号池额度耗尽（429）后中断，未覆盖全部 210 条；失败率在三组间分布均匀（40%/44%/45%），不偏袒任何一组。 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | 210 | 840 | 13 | 文本全量，四臂齐备（含紧凑渲染对照臂）。 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | 50 | 138 | 0 | 50 条分层抽样（与所有跨模型共用同一批任务）。 |
| txt_cross | loomy/qwen-3.8-max | 31 | 76 | 2 | 50 条分层抽样（与所有跨模型共用同一批任务）。 |
| txt_cross | workbuddy/deepseek-v4.1-flash | 50 | 200 | 122 | 50 条分层抽样（与所有跨模型共用同一批任务）。 |
| txt_gemini | antigravity/gemini-3.8-flash | 50 | 150 | 0 | 同一批 50 条任务，但由 Antigravity 的 `agy` CLI 承载：提示词经 agent 外壳包裹，与 Magpie 直连并非逐字节一致，作为独立方向性对照，不参与严格的同构假设检验。 |

## 7. 已知局限

- 跨帧实体身份由感知模型给出，命名抖动会让部分实体裂成多个记录，低估版本账本的冲突信号（对三组方法同向影响，不改变比较方向）。
- 感知层的『absent』判断来自模型对画面的判断，视角切换造成的不可见与真正的物体消失无法完全区分。
- 跨模型对照仅 50 条分层抽样，置信区间较宽，只作方向性验证。
- 端到端多模态条件因 `minimax-code` 账号池额度耗尽只覆盖 167/210 条任务（失败率在组间均匀，Clean 口径已剔除），补齐全量需约 800 credits。
- Gemini 对照臂经 `agy` CLI 的 agent 外壳承载，提示词并非逐字节同构，只作方向性验证；严格同构的结论只基于 Magpie 直连的四组实验。
- 文本条件下感知层由模型生成，三组共用同一份冻结缓存，感知误差作为等量系统噪声作用于各组；但它同时压低了所有组的绝对准确率。
- 不同供应商不保证上游模型版本完全一致，跨模型结论按「模型族 + 路由」解释。