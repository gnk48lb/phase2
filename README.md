# 第二阶段：Plan-Bench 外部验证实验

具身智能课题第二阶段：在公开基准 **Plan-Bench**（[MinneFeng/Plan-Bench](https://github.com/MinneFeng/Plan-Bench)）上，
用与第一阶段（`gate_dynamic_house`）同一套「版本覆盖式状态账本」思想做**外部验证**，
检验「记忆的组织方式」这一变量本身是否真的影响长程状态推理的准确率。

## 1. 目录

```text
phase2_planbench/
├─ Plan-Bench/            # 克隆的基准仓库（questions.tsv + image/）
├─ common.py              # 复用第一阶段 agents.py 的 MagpieClient + 配额分级 + 缓存
├─ memory_ledger.py       # 追加式账本 / 版本覆盖式账本 / 代码级风险分
├─ prompts.py             # 严格同构的三组提示词 + 感知层提示词
├─ grader.py              # 确定性评分（精确匹配 / 数值与序列等价）
├─ prepare_data.py        # 克隆校验 → TSV 解析 → 模态探针 → 逐帧标注 + 跨帧状态追踪 → 缓存
├─ run_planbench.py       # 三组消融 × 多模型 × 两种条件（文本 / 端到端多模态）
├─ gate_simulation.py     # Staleness Risk Score 与 τ 扫描、额外核验成本
├─ analyze_planbench.py   # 统计表、配对显著性、两张图、summary.md
├─ HANDOFF.md            # 交接给 Antigravity 的文档
├─ chat_history.md       # Antigravity 侧会话导出
├─ chat_history_claude_code.md  # 本阶段（本会话）的会话记录
├─ cache/                 # frame_captions.json / state_tracks.json / tasks.json / 核验缓存
├─ results/               # 各次运行的 answers.jsonl、图与报告
└─ tests/                 # 核心逻辑单测（不联网）
```

## 2. 数据与模态

- 只取 **Temporal Reasoning** 维度 210 条（5 个子任务：Instruction Alignment 40、
  Motion Retrieval 50、Persistent Counting 50、Progress Tracking 30、Temporal Ordering 40），
  其中 35 条单选、175 条开放题，每条 6–8 帧。
- **模态探针**：`prepare_data.py` 先发 1 条带图请求给 `minimax-code-global/MiniMax-M3.1-Flash`，
  实测**原生支持多图输入**（3 图单请求逐帧作答正确），因此走**端到端多模态**路线，
  不需要动用限额 VLM；结果写入 `cache/capabilities.json`。
- **感知层两阶段，全程不含题目与选项**（防止答案泄漏进共享观测流）：
  1. 逐帧客观标注 → `cache/frame_captions.json`；
  2. 跨帧状态追踪（实体身份 + 显式 `absent`）→ `cache/state_tracks.json`。
  两份缓存均按图像路径/任务 ID 索引，**同一帧永不复用计费**（崩溃重启不会重复付费）。

## 3. 三组消融（严格 prompt parity）

三组共享**完全相同**的任务规则、问题、选项、答案 JSON 格式、观测内容与解码参数，
**唯一变量是历史状态的组织方式**：

| 组 | 历史组织 | 记忆机制 |
|---|---|---|
| `direct_baseline` | 按时间顺序的帧观测 | 无账本 |
| `append_only_ledger` | 只追加的状态日志 | 追加、不删除、不标记失效（暴露陈旧记忆误导） |
| `versioned_state_ledger` | 版本覆盖账本 | 冲突/消失时显式 `superseded`，记录 `last_verified_frame` 与证据 |

## 4. 配额策略（代码强制）

MiniMax **不是无限额度**：Magpie 里 `minimax-code-global` 多个账号与 `minimax-code` 账号合计约
9.2k credits，且与本会话自身共用同一个池子。实测单价 ≈ **322 credits / 1M tokens（输入+输出）**。

| 模型 | 并发 | 样本上限 | 额度来源 | 用途 |
|---|---|---|---|---|
| `minimax-code-global/MiniMax-M3.1-Flash` | 10 | 无限（但受积分池约束） | MiniMax credits | 全量 210 条 + 全部消融 + 感知层 + 门控 |
| `workbuddy/deepseek-v4.1-flash` | 2 | 50（分层抽样） | WorkBuddy 额度 | 跨模型对照 |
| `autoclaw-cn/zai_glm-5.3-flash` | 2 | 50（分层抽样） | AutoClaw 积分 | 跨模型对照 |
| `loomy/qwen-3.8-max` | 2 | 50（分层抽样） | Loomy 额度 | 跨模型对照 |

样本上限写在 `common.py` 的 `QUOTA_TIERS` 里，`run_planbench.py` 在建任务表时按模型强制套用，
CLI 无法绕过。

**积分闸门（`common.CreditMeter`）**：
- `--credit-budget`：本次运行允许消耗的估算 credits，超出即停止派发新任务；
- `--credit-reserve`（默认 2000）：**池底保护线**，实时余额读到该线以下就停止，
  保证结束时至少留 2000 积分；
- 实时余额从 `~/.config/magpie/quotas.json` 读取（Magpie 每隔几分钟刷新）；
- 每条结果**即时落盘**（`answers.jsonl` 追加写），停止后重跑同一条命令即从断点续跑，
  已完成的任务不会重复计费；未完成清单写在 `resume_state_<mode>.json` / `cache/pending_tracks.json`。

当前额度分配（总池 ~9.2k，保留 2k）：

| 阶段 | 实际消耗 credits | 结束状态 |
|---|---|---|
| 感知层（1411 帧标注 + 210 跨帧追踪） | ~1200 | ✅ 完成（缓存可复用） |
| 文本条件全量（210 × 4 组，含紧凑对照臂） | ~950 | ✅ 完成，传输失败 ≈0 |
| 风险门控（210 × 核验+重答+对照重答） | ~700 | ✅ 完成（含 44 个失败单元的补跑） |
| 端到端多模态（210 × 3 组） | ~2300 | ⚠️ 停在 159/210 条（有效样本 141–151/臂，失败率 43%→0.3%） |
| 跨模型对照（3 模型 × 50 × 3~4 组） | 0（不占 MiniMax） | ⚠️ 上游路由退化，有效样本 24–47 条 |

**实际余额**：`minimax-code-global` ≈ 2.1k / `minimax-code` ≈ 0（当日耗尽）。
多模态那一档由 `minimax-code` 账号池起跑、额度耗尽后由 `minimax-code-global` 续跑，
每行都记了 `actual_model`，报告里按路由披露。
`--credit-reserve 2000` 的池底保护线全程生效，多模态那一档是**被 429 打断**的，不是被预算打断。

## 4.1 最终结果（Clean 口径：剔除传输失败）

| 条件 | 模型 | 有效样本 | direct | append-only | versioned | versioned-flat |
|---|---|---|---|---|---|---|
| 文本 210 题 | `minimax-code-global/MiniMax-M3.1-Flash` | 199–210 | 35.2% | **39.2%** | 30.3% | 30.2% |
| 端到端多模态 210 题 | `MiniMax-M3.1-Flash`（两条账号池混跑） | 200–206 | **58.4%** | 44.7% | 45.5% | — |
| 文本 50 条 | `antigravity/gemini-3.8-flash` | 48–50 | **38.0%** | 36.0% | 33.3% | 34.0% |
| 文本 50 条 | GLM-5.3-Flash（`autoclaw-cn` + `autoclaw` 两池合并） | 43–48 | **43.8%** | 42.6% | 39.5% | — |
| 文本 43 条 | `loomy/qwen3.8-flash` | 36–39 | 39.5% | **41.0%** | 33.3% | — |
| 文本 31 条 | `workbuddy/deepseek-v4.1-flash` | 27–29 | 35.7% | **44.8%** | 37.0% | — |

配对 McNemar（Clean 配对，全量 210 题）：
- 文本：`versioned` vs `append` **−9.0 pp（p=0.0021）**；`flat` vs `append` **−9.6 pp（p=0.0013）**；
  `flat` vs `versioned` −1.0 pp（p=0.85）→ **提示词长度混淆排除**；
- 多模态：`append` vs `direct` **−13.0 pp（p=0.0003）**、`versioned` vs `direct` **−12.3 pp（p=0.0007）**；
  `versioned` vs `append` +0.5 pp（p=1.0）→ **两种账本在有画面时同样有害**。

三条主结论：
1. 文本条件下**追加式日志优于版本覆盖账本**（−7.8 pp，McNemar p=0.0115），紧凑渲染对照臂复现同一差距
   （p=0.0008）且与长版本无差异（p=0.74）→ **排除了提示词长度这一混淆**。
2. 端到端多模态下**直接看画面远好于任何结构化账本**（66.2% vs 48~53%，p=0.003/0.016）→ 画面已在上下文里时，账本是有害的。
3. **代码级风险门控没有收益**（修复后 201 条：未门控 30.3% → 只重问 28.9% → 核验+重答 29.4%，
   风险分 **AUC=0.48**，低于随机线 0.5）。拆项看：过期跨度 AUC=0.597（唯一有信号的项），
   冲突次数 AUC=0.372（**反号**——冲突多说明题目难，不是模型要错），置信度 AUC=0.515（模型过自信）。
   「只重问」对照证明二采样本身就会掉点。
4. **机制适用域分层**：正序状态演进 80 题上三者接近（append 58.8% ≈ direct 55.0% ≈ versioned 53.8%），
   乱序/去重 130 题上差距被放大（direct 23.1% vs versioned 14.9% / append 27.1%）
   → **版本账本在它本该占优的正序任务上也没有优势**。


## 5. 运行

```bash
cd phase2_planbench
python -m unittest discover -s tests -v

# 额度恢复后续跑多模态剩下的 51 条任务 + 5 个失败单元（已完成的不重复计费）
python repair_run.py results/mm_minimax/answers.jsonl
python run_planbench.py --mode multimodal --models minimax-code-global/MiniMax-M3.1-Flash   --model-alias minimax-code-global/MiniMax-M3.1-Flash   --methods direct_baseline,append_only_ledger,versioned_state_ledger   --output-dir results/mm_minimax --credit-budget 800 --credit-reserve 2000
python analyze_planbench.py --results results --output-dir results

# 1) 数据准备（克隆 → 解析 → 模态探针 → 感知/追踪，全量约 40 分钟，全部缓存）
python prepare_data.py

# 2) 冒烟（10 条）
python run_planbench.py --mode text       --models minimax --limit 10 --output-dir results/smoke_txt
python run_planbench.py --mode multimodal --models minimax --limit 10 --output-dir results/smoke_mm

# 3) 全量：MiniMax（无限额度）
python run_planbench.py --mode multimodal --models minimax --output-dir results/mm_minimax
python run_planbench.py --mode text       --models minimax --output-dir results/txt_minimax

# 4) 跨模型对照：限额模型，50 条分层抽样，并发 2
python run_planbench.py --mode text --models cross \
    --subset-file cache/cross_subset_50.json --output-dir results/txt_cross

# 5) 代码级风险门控（1 次额外感知 + 1 次重答，含「只重问」对照）
python gate_simulation.py --answers results/txt_minimax/answers.jsonl --output-dir results

# 6) 统计与出图
python analyze_planbench.py --results results --output-dir results
```

产物：`results/planbench_summary.md`、`results/ablation_accuracy.png`、
`results/risk_cost_tradeoff.png`、`results/accuracy_table.csv`、`results/mcnemar.csv`、
`results/gate_simulation.{json,csv}`，以及每次运行的 `answers.jsonl`（逐样本结果）。

## 6. 口径说明

- 准确率同时报告 **graded**（数值/序列等价，`4` == `Step 4`）与 **exact**（归一化后严格相等）。
- 感知层由模型生成，**三组方法共用同一份观测流**，因此差异只来自记忆组织方式。
- 风险门控的收益用两条曲线区分：门控核验（多 1 次感知）与「只重问」（不额外感知），
  避免把二次作答的运气算成核验的价值。
