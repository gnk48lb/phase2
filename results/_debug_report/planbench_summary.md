# Plan-Bench 第二阶段外部验证实验报告

生成时间：2026-11-14T03:36:46.466510+00:00

## 1. 实验设置

- 数据集：Plan-Bench（MinneFeng/Plan-Bench），仅取 **Temporal Reasoning** 维度 210 条（Instruction Alignment 40 / Motion Retrieval 50 / Persistent Counting 50 / Progress Tracking 30 / Temporal Ordering 40），其中 35 条单选、175 条开放题。
- 模态探针：`minimax-code-global/MiniMax-M3.1-Flash` 原生支持多图输入（3 图单请求逐帧作答验证通过），因此走**端到端多模态**路线，无需外部 VLM。
- 感知层：两阶段、**完全不含题目**（防答案泄漏）：逐帧客观标注 → 跨帧状态追踪（实体身份 + 显式 absent）。两份产物均缓存于 `cache/frame_captions.json` 与 `cache/state_tracks.json`，同一帧永不复用计费。
- 消融三组共享同一任务规则、同一问题、同一选项、同一答案格式、同一解码参数；**唯一变量是历史状态的组织方式**：`direct_baseline`（按时间顺序的帧观测）、`append_only_ledger`（只追加日志，旧记录不失效）、`versioned_state_ledger`（active/superseded 版本覆盖账本）。
- 配额：MiniMax 全量 + 并发 10；其余模型仅 50 条分层抽样 + 并发 2。

## 2. 主结果

### smoke_mm — minimax-code-global/MiniMax-M3.1-Flash（mode=multimodal, 10 条任务）

| 方法 | 总体准确率 | 95% CI | 精确匹配 | 样本数 |
|---|---|---|---|---|
| `append_only_ledger` | 90.0% | [59.6%, 98.2%] | 90.0% | 10 |
| `direct_baseline` | 100.0% | [72.2%, 100.0%] | 100.0% | 10 |
| `versioned_state_ledger` | 90.0% | [59.6%, 98.2%] | 90.0% | 10 |

| 方法 | Progress Tracking |
|---|---|
| `append_only_ledger` | 90% (n=10) |
| `direct_baseline` | 100% (n=10) |
| `versioned_state_ledger` | 90% (n=10) |

### smoke_txt — minimax-code-global/MiniMax-M3.1-Flash（mode=text, 10 条任务）

| 方法 | 总体准确率 | 95% CI | 精确匹配 | 样本数 |
|---|---|---|---|---|
| `append_only_ledger` | 90.0% | [59.6%, 98.2%] | 90.0% | 10 |
| `direct_baseline` | 90.0% | [59.6%, 98.2%] | 90.0% | 10 |
| `versioned_state_ledger` | 60.0% | [31.3%, 83.2%] | 60.0% | 10 |

| 方法 | Progress Tracking |
|---|---|
| `append_only_ledger` | 90% (n=10) |
| `direct_baseline` | 90% (n=10) |
| `versioned_state_ledger` | 60% (n=10) |

## 3. 配对显著性（McNemar 精确检验 + 配对 Bootstrap）

| 运行 | 模型 | 对比 | n | 差值 (pp) | 95% CI (pp) | McNemar p |
|---|---|---|---|---|---|---|
| smoke_mm | minimax-code-global/MiniMax-M3.1-Flash | `versioned_state_ledger` vs `append_only_ledger` | 10 | +0.0 | [+0.0, +0.0] | 1 |
| smoke_mm | minimax-code-global/MiniMax-M3.1-Flash | `versioned_state_ledger` vs `direct_baseline` | 10 | -10.0 | [-30.0, +0.0] | 1 |
| smoke_mm | minimax-code-global/MiniMax-M3.1-Flash | `append_only_ledger` vs `direct_baseline` | 10 | -10.0 | [-30.0, +0.0] | 1 |
| smoke_txt | minimax-code-global/MiniMax-M3.1-Flash | `versioned_state_ledger` vs `append_only_ledger` | 10 | -30.0 | [-60.0, +0.0] | 0.25 |
| smoke_txt | minimax-code-global/MiniMax-M3.1-Flash | `versioned_state_ledger` vs `direct_baseline` | 10 | -30.0 | [-60.0, +0.0] | 0.25 |
| smoke_txt | minimax-code-global/MiniMax-M3.1-Flash | `append_only_ledger` vs `direct_baseline` | 10 | +0.0 | [+0.0, +0.0] | 1 |

## 5. 图

- `results/ablation_accuracy.png`：三组方法在各子任务上的准确率（MiniMax 全量 + 50 条跨模型对照），误差棒为 95% Wilson 区间。
- `results/risk_cost_tradeoff.png`：代码级风险门控下「平均额外核验成本 vs 任务准确率」曲线。

## 6. 已知局限

- 跨帧实体身份由感知模型给出，命名抖动会让部分实体裂成多个记录，低估版本账本的冲突信号（对三组方法同向影响，不改变比较方向）。
- 感知层的『absent』判断来自模型对画面的判断，视角切换造成的不可见与真正的物体消失无法完全区分。
- 跨模型对照仅 50 条分层抽样，置信区间较宽，只作方向性验证。
- 不同供应商不保证上游模型版本完全一致，跨模型结论按「模型族 + 路由」解释。