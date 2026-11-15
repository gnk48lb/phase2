# Plan-Bench 第二阶段外部验证实验报告

生成时间：2026-11-15T08:57:40.342854+00:00

## 0. 结论摘要

1. **文本条件（MiniMax 全量 201 条有效样本，传输失败≈0）：`append_only` 39.2% > `direct` 35.2% > `versioned` 30.3% ≈ `versioned_flat` 30.2%。
   版本覆盖账本比追加式日志低 8.9 pp（n=201，McNemar p=0.002102）；紧凑渲染对照臂同样低 9.1 pp（p=0.001319），而与长版本账本**无统计差异**（p=0.8506）——**说明差距来自「覆盖/失效旧状态」机制本身，而不是提示词长度**。
2. **端到端多模态（覆盖 210/210 条任务，各臂 Clean 有效样本 200–206 条）：`direct` 58.4% ≫ `append` 44.7% ≈ `versioned` 45.5%——**画面已经在上下文里时，再叠加结构化账本反而是有害的**。
3. **代码级风险门控没有收益**：未门控 30.3% → 只重问 28.9% → 核验+重答 29.4%；风险分判别力很弱（AUC 见 §4）。**二采样本身就会掉点**（只重问对照），所以「多花一次感知」必须先解决风险分标定问题才可能划算。
4. **跨模型 50 条对照：方向一致但不显著**。3/4 个模型上版本账本不优于追加式日志——zai_glm-5.3-flash +0.0 pp（n=41, p=1.00）；qwen3.8-flash -8.6 pp（n=35, p=0.25）；deepseek-v4.1-flash -8.0 pp（n=25, p=0.50）；gemini-3.8-flash -2.1 pp（n=48, p=1.00）。每列有效配对只有 25–42 对，**统计功效不足（p≥0.25）**，因此只能作方向性参考，不能声称「跨模型高度一致」。（上游路由在实验期间退化：deepseek 额度耗尽、AutoClaw/Loomy 慢速或空响应、OpenCode Zen Free 90%+ 限流。）
5. **机制适用域分层（文本条件）**：正序状态演进 80 题上三者接近——`append` 58.8% ≈ `direct` 55.0% ≈ `versioned` 53.8%；乱序重排 / 跨视角去重 130 题上差距被放大——`direct` 23.1% vs `versioned` 14.9% / `append` 27.1%。**版本账本在它本该占优的「正序状态演进」上并没有优势，劣势主要出现在帧序被破坏的任务上。**

## 1. 实验设置

- 数据集：Plan-Bench（MinneFeng/Plan-Bench），仅取 **Temporal Reasoning** 维度 210 条（Instruction Alignment 40 / Motion Retrieval 50 / Persistent Counting 50 / Progress Tracking 30 / Temporal Ordering 40），其中 35 条单选、175 条开放题。
- 模态探针：`minimax-code-global/MiniMax-M3.1-Flash` 原生支持多图输入（3 图单请求逐帧作答验证通过），因此走**端到端多模态**路线，无需外部 VLM。
- 感知层：两阶段、**完全不含题目**（防答案泄漏）：逐帧客观标注 → 跨帧状态追踪（实体身份 + 显式 absent）。两份产物均缓存于 `cache/frame_captions.json` 与 `cache/state_tracks.json`，同一帧永不复用计费。
- 消融三组共享同一任务规则、同一问题、同一选项、同一答案格式、同一解码参数；**唯一变量是历史状态的组织方式**：`direct_baseline`（按时间顺序的帧观测）、`append_only_ledger`（只追加日志，旧记录不失效）、`versioned_state_ledger`（active/superseded 版本覆盖账本）。
- 配额：MiniMax 全量 + 并发 10；其余模型仅 50 条分层抽样 + 并发 2。

## 2. 主结果

### mm_minimax — minimax-code-global/MiniMax-M3.1-Flash（mode=multimodal, 210 条任务）

| 方法 | Clean 准确率 | 95% CI | Full 准确率 | 传输失败 | 有效样本 |
|---|---|---|---|---|---|
| `append_only_ledger` | **44.7%** | [38.0%, 51.5%] | 44.8% | 4 | 206 |
| `direct_baseline` | **58.4%** | [51.5%, 65.0%] | 57.1% | 8 | 202 |
| `versioned_state_ledger` | **45.5%** | [38.7%, 52.4%] | 45.2% | 10 | 200 |

> Clean = 剔除传输失败（空响应/超时/无答案）的口径，是主口径；Full 把传输失败按答错计入分母（保守口径）。两者同向即结论稳健。

| 方法 | Instruction Alignment | Motion Retrieval | Persistent Counting | Progress Tracking | Temporal Ordering |
|---|---|---|---|---|---|
| `append_only_ledger` | 41% (n=39) | 67% (n=49) | 15% (n=48) | 87% (n=30) | 25% (n=40) |
| `direct_baseline` | 44% (n=39) | 71% (n=48) | 36% (n=45) | 87% (n=30) | 62% (n=40) |
| `versioned_state_ledger` | 41% (n=37) | 65% (n=49) | 17% (n=46) | 87% (n=30) | 26% (n=38) |

### txt_minimax — minimax-code-global/MiniMax-M3.1-Flash（mode=text, 210 条任务）

| 方法 | Clean 准确率 | 95% CI | Full 准确率 | 传输失败 | 有效样本 |
|---|---|---|---|---|---|
| `append_only_ledger` | **39.2%** | [32.9%, 46.0%] | 39.5% | 1 | 209 |
| `direct_baseline` | **35.2%** | [29.1%, 41.9%] | 35.2% | 0 | 210 |
| `versioned_state_ledger` | **30.3%** | [24.4%, 37.0%] | 30.0% | 9 | 201 |
| `versioned_state_ledger_flat` | **30.2%** | [24.2%, 36.9%] | 30.5% | 11 | 199 |

> Clean = 剔除传输失败（空响应/超时/无答案）的口径，是主口径；Full 把传输失败按答错计入分母（保守口径）。两者同向即结论稳健。

| 方法 | Instruction Alignment | Motion Retrieval | Persistent Counting | Progress Tracking | Temporal Ordering |
|---|---|---|---|---|---|
| `append_only_ledger` | 41% (n=39) | 50% (n=50) | 6% (n=50) | 73% (n=30) | 40% (n=40) |
| `direct_baseline` | 42% (n=40) | 42% (n=50) | 10% (n=50) | 77% (n=30) | 20% (n=40) |
| `versioned_state_ledger` | 20% (n=35) | 44% (n=50) | 8% (n=50) | 70% (n=30) | 19% (n=36) |
| `versioned_state_ledger_flat` | 38% (n=32) | 35% (n=48) | 6% (n=50) | 63% (n=30) | 23% (n=39) |

### txt_cross — autoclaw-cn/zai_glm-5.3-flash（mode=text, 50 条任务）

| 方法 | Clean 准确率 | 95% CI | Full 准确率 | 传输失败 | 有效样本 |
|---|---|---|---|---|---|
| `append_only_ledger` | **42.6%** | [29.5%, 56.7%] | 42.6% | 0 | 47 |
| `direct_baseline` | **43.8%** | [30.7%, 57.7%] | 42.9% | 1 | 48 |
| `versioned_state_ledger` | **39.5%** | [26.4%, 54.4%] | 39.5% | 0 | 43 |
| `versioned_state_ledger_flat` | **0.0%** | [0.0%, 65.8%] | 0.0% | 0 | 2 |

> Clean = 剔除传输失败（空响应/超时/无答案）的口径，是主口径；Full 把传输失败按答错计入分母（保守口径）。两者同向即结论稳健。

| 方法 | Instruction Alignment | Motion Retrieval | Persistent Counting | Progress Tracking | Temporal Ordering |
|---|---|---|---|---|---|
| `append_only_ledger` | 50% (n=8) | 50% (n=12) | 0% (n=11) | 71% (n=7) | 56% (n=9) |
| `direct_baseline` | 44% (n=9) | 58% (n=12) | 0% (n=11) | 86% (n=7) | 44% (n=9) |
| `versioned_state_ledger` | 50% (n=4) | 42% (n=12) | 9% (n=11) | 86% (n=7) | 33% (n=9) |
| `versioned_state_ledger_flat` | 0% (n=1) | 0% (n=1) |

### txt_cross — loomy/qwen3.8-flash（mode=text, 43 条任务）

| 方法 | Clean 准确率 | 95% CI | Full 准确率 | 传输失败 | 有效样本 |
|---|---|---|---|---|---|
| `append_only_ledger` | **41.0%** | [27.1%, 56.6%] | 39.0% | 2 | 39 |
| `direct_baseline` | **39.5%** | [25.6%, 55.3%] | 39.5% | 5 | 38 |
| `versioned_state_ledger` | **33.3%** | [20.2%, 49.7%] | 31.6% | 2 | 36 |

> Clean = 剔除传输失败（空响应/超时/无答案）的口径，是主口径；Full 把传输失败按答错计入分母（保守口径）。两者同向即结论稳健。

| 方法 | Instruction Alignment | Motion Retrieval | Persistent Counting | Progress Tracking | Temporal Ordering |
|---|---|---|---|---|---|
| `append_only_ledger` | 17% (n=6) | 75% (n=8) | 9% (n=11) | 83% (n=6) | 38% (n=8) |
| `direct_baseline` | 50% (n=6) | 56% (n=9) | 9% (n=11) | 83% (n=6) | 17% (n=6) |
| `versioned_state_ledger` | 20% (n=5) | 75% (n=8) | 0% (n=11) | 83% (n=6) | 0% (n=6) |

### txt_cross — workbuddy/deepseek-v4.1-flash（mode=text, 31 条任务）

| 方法 | Clean 准确率 | 95% CI | Full 准确率 | 传输失败 | 有效样本 |
|---|---|---|---|---|---|
| `append_only_ledger` | **44.8%** | [28.4%, 62.5%] | 44.8% | 0 | 29 |
| `direct_baseline` | **35.7%** | [20.7%, 54.2%] | 35.7% | 0 | 28 |
| `versioned_state_ledger` | **37.0%** | [21.5%, 55.8%] | 37.0% | 0 | 27 |

> Clean = 剔除传输失败（空响应/超时/无答案）的口径，是主口径；Full 把传输失败按答错计入分母（保守口径）。两者同向即结论稳健。

| 方法 | Instruction Alignment | Motion Retrieval | Persistent Counting | Progress Tracking | Temporal Ordering |
|---|---|---|---|---|---|
| `append_only_ledger` | 50% (n=10) | 0% (n=10) | 100% (n=7) | 50% (n=2) |
| `direct_baseline` | 50% (n=2) | 30% (n=10) | 0% (n=9) | 86% (n=7) |
| `versioned_state_ledger` | 50% (n=2) | 38% (n=8) | 0% (n=10) | 86% (n=7) |

### txt_gemini — antigravity/gemini-3.8-flash（mode=text, 50 条任务）

| 方法 | Clean 准确率 | 95% CI | Full 准确率 | 传输失败 | 有效样本 |
|---|---|---|---|---|---|
| `append_only_ledger` | **36.0%** | [24.1%, 49.9%] | 36.0% | 0 | 50 |
| `direct_baseline` | **38.0%** | [25.9%, 51.8%] | 38.0% | 0 | 50 |
| `versioned_state_ledger` | **33.3%** | [21.7%, 47.5%] | 32.0% | 2 | 48 |
| `versioned_state_ledger_flat` | **34.0%** | [22.4%, 47.8%] | 34.0% | 0 | 50 |

> Clean = 剔除传输失败（空响应/超时/无答案）的口径，是主口径；Full 把传输失败按答错计入分母（保守口径）。两者同向即结论稳健。

| 方法 | Instruction Alignment | Motion Retrieval | Persistent Counting | Progress Tracking | Temporal Ordering |
|---|---|---|---|---|---|
| `append_only_ledger` | 40% (n=10) | 33% (n=12) | 0% (n=11) | 86% (n=7) | 40% (n=10) |
| `direct_baseline` | 50% (n=10) | 33% (n=12) | 0% (n=11) | 86% (n=7) | 40% (n=10) |
| `versioned_state_ledger` | 30% (n=10) | 30% (n=10) | 9% (n=11) | 71% (n=7) | 40% (n=10) |
| `versioned_state_ledger_flat` | 40% (n=10) | 33% (n=12) | 0% (n=11) | 71% (n=7) | 40% (n=10) |

## 3. 机制适用域分层（各记忆组织方式的适用边界）

Plan-Bench 的 Temporal Reasoning 混了两种机制完全不同的子任务：**正序状态演进**（Progress Tracking 30 + Motion Retrieval 50 = 80 题，帧按真实时间正序排列，考察实体状态与进度的动态更新，是版本覆盖账本的目标适用域）；**乱序重排与跨视角去重**（Temporal Ordering 40 + Instruction Alignment 40 + Persistent Counting 50 = 130 题，帧被打乱或需要跨视角去重计数）。

### mm_minimax — minimax-code-global/MiniMax-M3.1-Flash · Chronological State-Tracking (n=80)

| 方法 | Clean 准确率 | 95% CI | 有效样本 |
|---|---|---|---|
| `append_only_ledger` | 74.7% | [64.1%, 83.0%] | 79 |
| `direct_baseline` | 76.9% | [66.4%, 84.9%] | 78 |
| `versioned_state_ledger` | 73.4% | [62.8%, 81.9%] | 79 |

### mm_minimax — minimax-code-global/MiniMax-M3.1-Flash · Shuffled / Cumulative (n=130)

| 方法 | Clean 准确率 | 95% CI | 有效样本 |
|---|---|---|---|
| `append_only_ledger` | 26.0% | [19.1%, 34.2%] | 127 |
| `direct_baseline` | 46.8% | [38.2%, 55.5%] | 124 |
| `versioned_state_ledger` | 27.3% | [20.1%, 35.8%] | 121 |

### txt_minimax — minimax-code-global/MiniMax-M3.1-Flash · Chronological State-Tracking (n=80)

| 方法 | Clean 准确率 | 95% CI | 有效样本 |
|---|---|---|---|
| `append_only_ledger` | 58.8% | [47.8%, 68.9%] | 80 |
| `direct_baseline` | 55.0% | [44.1%, 65.4%] | 80 |
| `versioned_state_ledger` | 53.8% | [42.9%, 64.3%] | 80 |
| `versioned_state_ledger_flat` | 46.2% | [35.5%, 57.1%] | 78 |

### txt_minimax — minimax-code-global/MiniMax-M3.1-Flash · Shuffled / Cumulative (n=130)

| 方法 | Clean 准确率 | 95% CI | 有效样本 |
|---|---|---|---|
| `append_only_ledger` | 27.1% | [20.2%, 35.4%] | 129 |
| `direct_baseline` | 23.1% | [16.7%, 31.0%] | 130 |
| `versioned_state_ledger` | 14.9% | [9.6%, 22.3%] | 121 |
| `versioned_state_ledger_flat` | 19.8% | [13.7%, 27.8%] | 121 |

### txt_cross — autoclaw-cn/zai_glm-5.3-flash · Chronological State-Tracking (n=80)

| 方法 | Clean 准确率 | 95% CI | 有效样本 |
|---|---|---|---|
| `append_only_ledger` | 57.9% | [36.3%, 76.9%] | 19 |
| `direct_baseline` | 68.4% | [46.0%, 84.6%] | 19 |
| `versioned_state_ledger` | 57.9% | [36.3%, 76.9%] | 19 |
| `versioned_state_ledger_flat` | 0.0% | [0.0%, 79.3%] | 1 |

### txt_cross — autoclaw-cn/zai_glm-5.3-flash · Shuffled / Cumulative (n=130)

| 方法 | Clean 准确率 | 95% CI | 有效样本 |
|---|---|---|---|
| `append_only_ledger` | 32.1% | [17.9%, 50.7%] | 28 |
| `direct_baseline` | 27.6% | [14.7%, 45.7%] | 29 |
| `versioned_state_ledger` | 25.0% | [12.0%, 44.9%] | 24 |
| `versioned_state_ledger_flat` | 0.0% | [0.0%, 79.3%] | 1 |

### txt_cross — loomy/qwen3.8-flash · Chronological State-Tracking (n=80)

| 方法 | Clean 准确率 | 95% CI | 有效样本 |
|---|---|---|---|
| `append_only_ledger` | 78.6% | [52.4%, 92.4%] | 14 |
| `direct_baseline` | 66.7% | [41.7%, 84.8%] | 15 |
| `versioned_state_ledger` | 78.6% | [52.4%, 92.4%] | 14 |

### txt_cross — loomy/qwen3.8-flash · Shuffled / Cumulative (n=130)

| 方法 | Clean 准确率 | 95% CI | 有效样本 |
|---|---|---|---|
| `append_only_ledger` | 20.0% | [8.9%, 39.1%] | 25 |
| `direct_baseline` | 21.7% | [9.7%, 41.9%] | 23 |
| `versioned_state_ledger` | 4.5% | [0.8%, 21.8%] | 22 |

### txt_cross — workbuddy/deepseek-v4.1-flash · Chronological State-Tracking (n=80)

| 方法 | Clean 准确率 | 95% CI | 有效样本 |
|---|---|---|---|
| `append_only_ledger` | 70.6% | [46.9%, 86.7%] | 17 |
| `direct_baseline` | 52.9% | [31.0%, 73.8%] | 17 |
| `versioned_state_ledger` | 60.0% | [35.7%, 80.2%] | 15 |

### txt_cross — workbuddy/deepseek-v4.1-flash · Shuffled / Cumulative (n=130)

| 方法 | Clean 准确率 | 95% CI | 有效样本 |
|---|---|---|---|
| `append_only_ledger` | 8.3% | [1.5%, 35.4%] | 12 |
| `direct_baseline` | 9.1% | [1.6%, 37.7%] | 11 |
| `versioned_state_ledger` | 8.3% | [1.5%, 35.4%] | 12 |

### txt_gemini — antigravity/gemini-3.8-flash · Chronological State-Tracking (n=80)

| 方法 | Clean 准确率 | 95% CI | 有效样本 |
|---|---|---|---|
| `append_only_ledger` | 52.6% | [31.7%, 72.7%] | 19 |
| `direct_baseline` | 52.6% | [31.7%, 72.7%] | 19 |
| `versioned_state_ledger` | 47.1% | [26.2%, 69.0%] | 17 |
| `versioned_state_ledger_flat` | 47.4% | [27.3%, 68.3%] | 19 |

### txt_gemini — antigravity/gemini-3.8-flash · Shuffled / Cumulative (n=130)

| 方法 | Clean 准确率 | 95% CI | 有效样本 |
|---|---|---|---|
| `append_only_ledger` | 25.8% | [13.7%, 43.2%] | 31 |
| `direct_baseline` | 29.0% | [16.1%, 46.6%] | 31 |
| `versioned_state_ledger` | 25.8% | [13.7%, 43.2%] | 31 |
| `versioned_state_ledger_flat` | 25.8% | [13.7%, 43.2%] | 31 |

## 4. 配对显著性（McNemar 精确检验 + 配对 Bootstrap）

| 运行 | 模型 | 子集分组 | 对比 | n | 差值 (pp) | 95% CI (pp) | McNemar p |
|---|---|---|---|---|---|---|---|
| mm_minimax | minimax-code-global/MiniMax-M3.1-Flash | All 210 | `versioned_state_ledger` vs `append_only_ledger` | 198 | +0.5 | [-5.1, +6.1] | 1 |
| mm_minimax | minimax-code-global/MiniMax-M3.1-Flash | All 210 | `versioned_state_ledger` vs `direct_baseline` | 195 | -12.3 | [-19.0, -5.6] | 0.0007173 |
| mm_minimax | minimax-code-global/MiniMax-M3.1-Flash | All 210 | `append_only_ledger` vs `direct_baseline` | 200 | -13.0 | [-19.5, -6.5] | 0.0003059 |
| mm_minimax | minimax-code-global/MiniMax-M3.1-Flash | Chronological State-Tracking (n=80) | `versioned_state_ledger` vs `append_only_ledger` | 79 | -1.3 | [-10.1, +6.3] | 1 |
| mm_minimax | minimax-code-global/MiniMax-M3.1-Flash | Chronological State-Tracking (n=80) | `versioned_state_ledger` vs `direct_baseline` | 78 | -2.6 | [-11.5, +7.7] | 0.8036 |
| mm_minimax | minimax-code-global/MiniMax-M3.1-Flash | Chronological State-Tracking (n=80) | `append_only_ledger` vs `direct_baseline` | 78 | -1.3 | [-9.0, +7.7] | 1 |
| mm_minimax | minimax-code-global/MiniMax-M3.1-Flash | Shuffled / Cumulative (n=130) | `versioned_state_ledger` vs `append_only_ledger` | 119 | +1.7 | [-5.9, +9.2] | 0.8238 |
| mm_minimax | minimax-code-global/MiniMax-M3.1-Flash | Shuffled / Cumulative (n=130) | `versioned_state_ledger` vs `direct_baseline` | 117 | -18.8 | [-27.4, -10.3] | 0.0001131 |
| mm_minimax | minimax-code-global/MiniMax-M3.1-Flash | Shuffled / Cumulative (n=130) | `append_only_ledger` vs `direct_baseline` | 122 | -20.5 | [-30.3, -11.5] | 7.025e-05 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | All 210 | `versioned_state_ledger` vs `append_only_ledger` | 201 | -9.0 | [-14.4, -4.0] | 0.002102 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | All 210 | `versioned_state_ledger` vs `direct_baseline` | 201 | -5.5 | [-10.9, +0.0] | 0.07076 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | All 210 | `append_only_ledger` vs `direct_baseline` | 209 | +3.8 | [-1.4, +9.6] | 0.2153 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | All 210 | `versioned_state_ledger_flat` vs `versioned_state_ledger` | 191 | -1.0 | [-6.8, +4.2] | 0.8506 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | All 210 | `versioned_state_ledger_flat` vs `append_only_ledger` | 198 | -9.6 | [-15.2, -4.0] | 0.001319 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | Chronological State-Tracking (n=80) | `versioned_state_ledger` vs `append_only_ledger` | 80 | -5.0 | [-15.0, +3.8] | 0.424 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | Chronological State-Tracking (n=80) | `versioned_state_ledger` vs `direct_baseline` | 80 | -1.2 | [-10.0, +8.8] | 1 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | Chronological State-Tracking (n=80) | `append_only_ledger` vs `direct_baseline` | 80 | +3.8 | [-6.2, +13.8] | 0.6072 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | Chronological State-Tracking (n=80) | `versioned_state_ledger_flat` vs `versioned_state_ledger` | 78 | -7.7 | [-19.2, +2.6] | 0.2379 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | Chronological State-Tracking (n=80) | `versioned_state_ledger_flat` vs `append_only_ledger` | 78 | -12.8 | [-23.1, -2.6] | 0.02127 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | Shuffled / Cumulative (n=130) | `versioned_state_ledger` vs `append_only_ledger` | 121 | -11.6 | [-19.0, -5.0] | 0.001312 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | Shuffled / Cumulative (n=130) | `versioned_state_ledger` vs `direct_baseline` | 121 | -8.3 | [-14.9, -2.5] | 0.02127 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | Shuffled / Cumulative (n=130) | `append_only_ledger` vs `direct_baseline` | 129 | +3.9 | [-2.3, +10.1] | 0.3323 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | Shuffled / Cumulative (n=130) | `versioned_state_ledger_flat` vs `versioned_state_ledger` | 113 | +3.5 | [-1.8, +8.8] | 0.3438 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | Shuffled / Cumulative (n=130) | `versioned_state_ledger_flat` vs `append_only_ledger` | 120 | -7.5 | [-14.2, -1.7] | 0.04904 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | All 210 | `versioned_state_ledger` vs `append_only_ledger` | 41 | +0.0 | [-14.6, +14.6] | 1 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | All 210 | `versioned_state_ledger` vs `direct_baseline` | 42 | -2.4 | [-11.9, +7.1] | 1 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | All 210 | `append_only_ledger` vs `direct_baseline` | 46 | +0.0 | [-10.9, +10.9] | 1 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | All 210 | `versioned_state_ledger_flat` vs `versioned_state_ledger` | 2 | +0.0 | [+0.0, +0.0] | 1 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | All 210 | `versioned_state_ledger_flat` vs `append_only_ledger` | 2 | +0.0 | [+0.0, +0.0] | 1 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | Chronological State-Tracking (n=80) | `versioned_state_ledger` vs `append_only_ledger` | 19 | +0.0 | [-26.3, +26.3] | 1 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | Chronological State-Tracking (n=80) | `versioned_state_ledger` vs `direct_baseline` | 19 | -10.5 | [-31.6, +10.5] | 0.625 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | Chronological State-Tracking (n=80) | `append_only_ledger` vs `direct_baseline` | 19 | -10.5 | [-26.3, +0.0] | 0.5 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | Chronological State-Tracking (n=80) | `versioned_state_ledger_flat` vs `versioned_state_ledger` | 1 | +0.0 | [+0.0, +0.0] | 1 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | Chronological State-Tracking (n=80) | `versioned_state_ledger_flat` vs `append_only_ledger` | 1 | +0.0 | [+0.0, +0.0] | 1 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | Shuffled / Cumulative (n=130) | `versioned_state_ledger` vs `append_only_ledger` | 22 | +0.0 | [-18.2, +18.2] | 1 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | Shuffled / Cumulative (n=130) | `versioned_state_ledger` vs `direct_baseline` | 23 | +4.3 | [+0.0, +13.0] | 1 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | Shuffled / Cumulative (n=130) | `append_only_ledger` vs `direct_baseline` | 27 | +7.4 | [-7.4, +22.2] | 0.625 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | Shuffled / Cumulative (n=130) | `versioned_state_ledger_flat` vs `versioned_state_ledger` | 1 | +0.0 | [+0.0, +0.0] | 1 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | Shuffled / Cumulative (n=130) | `versioned_state_ledger_flat` vs `append_only_ledger` | 1 | +0.0 | [+0.0, +0.0] | 1 |
| txt_cross | loomy/qwen3.8-flash | All 210 | `versioned_state_ledger` vs `append_only_ledger` | 35 | -8.6 | [-20.0, +0.0] | 0.25 |
| txt_cross | loomy/qwen3.8-flash | All 210 | `versioned_state_ledger` vs `direct_baseline` | 32 | -3.1 | [-12.5, +6.2] | 1 |
| txt_cross | loomy/qwen3.8-flash | All 210 | `append_only_ledger` vs `direct_baseline` | 35 | +2.9 | [-5.7, +11.4] | 1 |
| txt_cross | loomy/qwen3.8-flash | Chronological State-Tracking (n=80) | `versioned_state_ledger` vs `append_only_ledger` | 14 | +0.0 | [+0.0, +0.0] | 1 |
| txt_cross | loomy/qwen3.8-flash | Chronological State-Tracking (n=80) | `versioned_state_ledger` vs `direct_baseline` | 14 | +7.1 | [+0.0, +21.4] | 1 |
| txt_cross | loomy/qwen3.8-flash | Chronological State-Tracking (n=80) | `append_only_ledger` vs `direct_baseline` | 14 | +7.1 | [+0.0, +21.4] | 1 |
| txt_cross | loomy/qwen3.8-flash | Shuffled / Cumulative (n=130) | `versioned_state_ledger` vs `append_only_ledger` | 21 | -14.3 | [-28.6, +0.0] | 0.25 |
| txt_cross | loomy/qwen3.8-flash | Shuffled / Cumulative (n=130) | `versioned_state_ledger` vs `direct_baseline` | 18 | -11.1 | [-27.8, +0.0] | 0.5 |
| txt_cross | loomy/qwen3.8-flash | Shuffled / Cumulative (n=130) | `append_only_ledger` vs `direct_baseline` | 21 | +0.0 | [-14.3, +14.3] | 1 |
| txt_cross | workbuddy/deepseek-v4.1-flash | All 210 | `versioned_state_ledger` vs `append_only_ledger` | 25 | -8.0 | [-20.0, +0.0] | 0.5 |
| txt_cross | workbuddy/deepseek-v4.1-flash | All 210 | `versioned_state_ledger` vs `direct_baseline` | 26 | +0.0 | [-11.5, +11.5] | 1 |
| txt_cross | workbuddy/deepseek-v4.1-flash | All 210 | `append_only_ledger` vs `direct_baseline` | 26 | +11.5 | [-3.8, +26.9] | 0.375 |
| txt_cross | workbuddy/deepseek-v4.1-flash | Chronological State-Tracking (n=80) | `versioned_state_ledger` vs `append_only_ledger` | 15 | -13.3 | [-33.3, +0.0] | 0.5 |
| txt_cross | workbuddy/deepseek-v4.1-flash | Chronological State-Tracking (n=80) | `versioned_state_ledger` vs `direct_baseline` | 15 | +0.0 | [-20.0, +20.0] | 1 |
| txt_cross | workbuddy/deepseek-v4.1-flash | Chronological State-Tracking (n=80) | `append_only_ledger` vs `direct_baseline` | 17 | +17.6 | [-5.9, +41.2] | 0.375 |
| txt_cross | workbuddy/deepseek-v4.1-flash | Shuffled / Cumulative (n=130) | `versioned_state_ledger` vs `append_only_ledger` | 10 | +0.0 | [+0.0, +0.0] | 1 |
| txt_cross | workbuddy/deepseek-v4.1-flash | Shuffled / Cumulative (n=130) | `versioned_state_ledger` vs `direct_baseline` | 11 | +0.0 | [+0.0, +0.0] | 1 |
| txt_cross | workbuddy/deepseek-v4.1-flash | Shuffled / Cumulative (n=130) | `append_only_ledger` vs `direct_baseline` | 9 | +0.0 | [+0.0, +0.0] | 1 |
| txt_gemini | antigravity/gemini-3.8-flash | All 210 | `versioned_state_ledger` vs `append_only_ledger` | 48 | -2.1 | [-14.6, +10.4] | 1 |
| txt_gemini | antigravity/gemini-3.8-flash | All 210 | `versioned_state_ledger` vs `direct_baseline` | 48 | -4.2 | [-18.8, +10.4] | 0.7744 |
| txt_gemini | antigravity/gemini-3.8-flash | All 210 | `append_only_ledger` vs `direct_baseline` | 50 | -2.0 | [-14.0, +10.0] | 1 |
| txt_gemini | antigravity/gemini-3.8-flash | All 210 | `versioned_state_ledger_flat` vs `versioned_state_ledger` | 48 | +0.0 | [-12.5, +12.5] | 1 |
| txt_gemini | antigravity/gemini-3.8-flash | All 210 | `versioned_state_ledger_flat` vs `append_only_ledger` | 50 | -2.0 | [-10.0, +6.0] | 1 |
| txt_gemini | antigravity/gemini-3.8-flash | Chronological State-Tracking (n=80) | `versioned_state_ledger` vs `append_only_ledger` | 17 | -5.9 | [-23.5, +11.8] | 1 |
| txt_gemini | antigravity/gemini-3.8-flash | Chronological State-Tracking (n=80) | `versioned_state_ledger` vs `direct_baseline` | 17 | -5.9 | [-23.5, +11.8] | 1 |
| txt_gemini | antigravity/gemini-3.8-flash | Chronological State-Tracking (n=80) | `append_only_ledger` vs `direct_baseline` | 19 | +0.0 | [+0.0, +0.0] | 1 |
| txt_gemini | antigravity/gemini-3.8-flash | Chronological State-Tracking (n=80) | `versioned_state_ledger_flat` vs `versioned_state_ledger` | 17 | +0.0 | [-17.6, +17.6] | 1 |
| txt_gemini | antigravity/gemini-3.8-flash | Chronological State-Tracking (n=80) | `versioned_state_ledger_flat` vs `append_only_ledger` | 19 | -5.3 | [-15.8, +0.0] | 1 |
| txt_gemini | antigravity/gemini-3.8-flash | Shuffled / Cumulative (n=130) | `versioned_state_ledger` vs `append_only_ledger` | 31 | +0.0 | [-16.1, +19.4] | 1 |
| txt_gemini | antigravity/gemini-3.8-flash | Shuffled / Cumulative (n=130) | `versioned_state_ledger` vs `direct_baseline` | 31 | -3.2 | [-22.6, +16.1] | 1 |
| txt_gemini | antigravity/gemini-3.8-flash | Shuffled / Cumulative (n=130) | `append_only_ledger` vs `direct_baseline` | 31 | -3.2 | [-22.6, +16.1] | 1 |
| txt_gemini | antigravity/gemini-3.8-flash | Shuffled / Cumulative (n=130) | `versioned_state_ledger_flat` vs `versioned_state_ledger` | 31 | +0.0 | [-16.1, +19.4] | 1 |
| txt_gemini | antigravity/gemini-3.8-flash | Shuffled / Cumulative (n=130) | `versioned_state_ledger_flat` vs `append_only_ledger` | 31 | +0.0 | [-12.9, +12.9] | 1 |

## 5. 代码级风险门控（Staleness Risk Score）

- 风险分由 Python 从 `versioned_state_ledger` 计算，不交给模型：`0.45 × 过期跨度 + 0.30 × 冲突次数 + 0.25 × (1 − 模型置信度)`。
- 未门控准确率 30.3%；对全部样本都重新核验时 29.4%（201 条样本）。
- **对照（只重问、不额外核验）**：28.9%，即单纯二次作答带来的收益为 -1.5 pp；额外感知本身的净贡献为 +0.5 pp。
- 风险分的判别力（错误样本得分更高的概率 AUC）：**0.480**（0.5 = 完全无判别力）。
  - 单项 `span_norm` 的 AUC：0.597
  - 单项 `conflict_norm` 的 AUC：0.372
  - 单项 `uncertainty` 的 AUC：0.515
- 风险分的区分度：答对样本均值 0.286，答错样本均值 0.302（答错 140 条 / 答对 61 条）。

| τ | 门控样本比例（额外感知成本） | 准确率 | 仅重问对照 |
|---|---|---|---|
| 0.00 | 1.00 | 29.4% | 28.9% |
| 0.10 | 0.93 | 27.9% | 27.9% |
| 0.20 | 0.83 | 28.9% | 28.9% |
| 0.30 | 0.69 | 29.4% | 29.4% |
| 0.40 | 0.11 | 29.9% | 29.4% |
| 0.50 | 0.00 | 30.3% | 30.3% |
| 0.60 | 0.00 | 30.3% | 30.3% |
| 0.70 | 0.00 | 30.3% | 30.3% |
| 0.80 | 0.00 | 30.3% | 30.3% |
| 0.90 | 0.00 | 30.3% | 30.3% |
| 1.00 | 0.00 | 30.3% | 30.3% |

## 6. 图

- `results/ablation_accuracy.png`：三组方法在各子任务上的准确率（MiniMax 全量 + 50 条跨模型对照），误差棒为 95% Wilson 区间。
- `results/risk_cost_tradeoff.png`：代码级风险门控下「平均额外核验成本 vs 任务准确率」曲线。

## 7. 覆盖度与口径说明

> **口径更正**：早期报告（Antigravity 生成于 `~/.gemini/antigravity/brain/33b00b10-.../planbench_phase2_report.md`）中的跨模型数字用的是 Full 口径（把传输失败按答错计入），**低估了 deepseek / qwen 的真实水平**；本文档与两张图统一以 Clean 口径为主，Full 口径并列保留。

| 运行 | 模型 | 覆盖任务数 | 单元数 | 传输失败 | 备注 |
|---|---|---|---|---|---|
| mm_minimax | minimax-code-global/MiniMax-M3.1-Flash | 210 | 630 | 22 | 多模态：`minimax-code` 账号池额度耗尽（429）后中断，未覆盖全部 210 条；失败率在三组间分布均匀（40%/44%/45%），不偏袒任何一组。 |
| txt_minimax | minimax-code-global/MiniMax-M3.1-Flash | 210 | 840 | 21 | 文本全量，四臂齐备（含紧凑渲染对照臂）。 |
| txt_cross | autoclaw-cn/zai_glm-5.3-flash | 50 | 141 | 1 | 50 条分层抽样（与所有跨模型共用同一批任务）。实测 `opencode-zen-free/*` 三条免费路由 **90%+ 请求被限流**（HTTP 429，每模型仅 ~15/160 成功），无法作为对照；WorkBuddy 的 deepseek 额度耗尽，AutoClaw/Loomy 路由单条 140–840s。 |
| txt_cross | loomy/qwen3.8-flash | 43 | 122 | 9 | 50 条分层抽样（与所有跨模型共用同一批任务）。实测 `opencode-zen-free/*` 三条免费路由 **90%+ 请求被限流**（HTTP 429，每模型仅 ~15/160 成功），无法作为对照；WorkBuddy 的 deepseek 额度耗尽，AutoClaw/Loomy 路由单条 140–840s。 |
| txt_cross | workbuddy/deepseek-v4.1-flash | 31 | 84 | 0 | 50 条分层抽样（与所有跨模型共用同一批任务）。实测 `opencode-zen-free/*` 三条免费路由 **90%+ 请求被限流**（HTTP 429，每模型仅 ~15/160 成功），无法作为对照；WorkBuddy 的 deepseek 额度耗尽，AutoClaw/Loomy 路由单条 140–840s。 |
| txt_gemini | antigravity/gemini-3.8-flash | 50 | 200 | 2 | 同一批 50 条任务，但由 Antigravity 的 `agy` CLI 承载：提示词经 agent 外壳包裹，与 Magpie 直连并非逐字节一致，作为独立方向性对照，不参与严格的同构假设检验。 |

## 8. 已知局限

- 跨帧实体身份由感知模型给出，命名抖动会让部分实体裂成多个记录，低估版本账本的冲突信号（对三组方法同向影响，不改变比较方向）。
- 感知层的『absent』判断来自模型对画面的判断，视角切换造成的不可见与真正的物体消失无法完全区分。
- 跨模型对照仅 50 条分层抽样，置信区间较宽，只作方向性验证。
- 端到端多模态条件因 `minimax-code` 账号池额度耗尽只覆盖 167/210 条任务（失败率在组间均匀，Clean 口径已剔除），补齐全量需约 800 credits。
- Gemini 对照臂经 `agy` CLI 的 agent 外壳承载，提示词并非逐字节同构，只作方向性验证；严格同构的结论只基于 Magpie 直连的四组实验。
- 文本条件下感知层由模型生成，三组共用同一份冻结缓存，感知误差作为等量系统噪声作用于各组；但它同时压低了所有组的绝对准确率。
- 不同供应商不保证上游模型版本完全一致，跨模型结论按「模型族 + 路由」解释。