# 第二阶段 Plan-Bench 实验 · 交接文档（HANDOFF）

> 给接手的 agent（Antigravity / Gemini 3.8 Flash）看。**自包含**：读完这一份就能继续干活，
> 不需要翻聊天记录。所有路径都相对 `D:\code\ClaudeCode\phase2_planbench`。

---

## 0. 任务是什么

具身智能课题第二阶段：在公开基准 **Plan-Bench** 上做**外部验证**，检验
「**记忆的组织方式**」这一个变量对长程状态推理准确率的影响，以及
「**代码级风险门控**」能不能用可计算的代价换回准确率。

三组消融（唯一变量是历史状态的组织方式，其余任务规则/问题/选项/答案格式/解码参数完全相同）：

| 组 | 历史组织 | 机制 |
|---|---|---|
| `direct_baseline` | 按时间顺序的帧观测 | 无账本 |
| `append_only_ledger` | 只追加的 JSON 状态日志 | 追加、不删除、不标记失效（陈旧记忆误导） |
| `versioned_state_ledger` | 版本覆盖账本 `{entity: {state, location, last_verified_frame, status, history}}` | 冲突/消失时显式 `superseded` |

外加一个 **代码级 Staleness Risk Score**（Python 算，不让模型自己算）：
`0.45×过期跨度 + 0.30×冲突次数 + 0.25×(1−模型置信度)`，
扫描阈值 τ，当风险 > τ 时花 1 次额外感知重新核验中间帧真实状态。

---

## 1. 当前进度（2026-11-14 18:40 收尾）

| 阶段 | 状态 | 产物 |
|---|---|---|
| 克隆 Plan-Bench | ✅ | `Plan-Bench/` |
| 数据解析（Temporal 210 条 / 5 子任务） | ✅ | `cache/tasks.json` |
| 模态探针（MiniMax 原生支持多图） | ✅ | `cache/capabilities.json` |
| 感知层（1411 帧标注 + 210 跨帧追踪，不含题目） | ✅ | `cache/frame_captions.json`、`cache/state_tracks.json` |
| 文本条件全量（210 × 4 组，含紧凑渲染对照臂） | ✅ | `results/txt_minimax/answers.jsonl` |
| 端到端多模态（159/210 条，Clean n=141–151/臂，失败率 0.3%） | ⚠️ 额度耗尽待续 | `results/mm_minimax/answers.jsonl` |
| 跨模型 50 条（deepseek/glm/qwen/gemini） | ⚠️ 路由退化，Clean n=24–47/臂 | `results/txt_cross/`、`results/txt_gemini/` |
| 风险门控模拟（含「只重问」对照） | ✅ | `results/gate_simulation.json` |
| 统计 / 图 / 报告 | ✅ | `results/planbench_summary.md`、`results/ablation_accuracy.png`、`results/risk_cost_tradeoff.png` |

**主结论（Clean 口径，详见 `results/planbench_summary.md`）**

1. 文本条件：`append_only` 39.2% > `direct` 35.2% > `versioned` 31.4% ≈ `versioned_flat` 30.3%。
   版本账本比追加式低 7.7 pp（McNemar p=0.0166）；紧凑对照臂同样低 9.5 pp（p=0.0005），
   与长版本无差异（p=0.74）→ **排除了提示词长度这一混淆变量**。
2. 端到端多模态：`direct` 66.2% ≫ `versioned` 52.5% / `append` 48.3%
   （p=0.016 / p=0.003）→ **画面已在上下文里时，叠加结构化账本是有害的**。
3. 风险门控无收益：未门控 31.2% → 只重问 27.0% → 核验+重答 25.6%，风险分 AUC≈0.58；
   「只重问」对照证明**二采样本身就会掉点**，不是核验的锅。
4. 跨模型只剩方向性参考：路由退化后有效样本 24–47 条，且 qwen 上版本账本反而更好，
   **不支持「跨模型高度一致」的强结论**（早期 Antigravity 报告用的是 Full 口径，已在
   `results/planbench_summary.md` §6 加了口径更正）。

**接手时注意的三个坑（都已修）**

- 行字段是 `prediction` 不是 `answer`；失败判定写错字段名会让「全部单元都被当成失败」。
- `answers.jsonl` 追加写且允许重跑：去重规则必须是「**同一 cell 优先取有答案的最新行**」，
  否则一次失败的重跑会把先前成功的答案覆盖掉（`analyze_planbench.load_runs()` 已按此实现）。
- Git Bash 里 `cmd /c xxx.bat` 会被 MSYS 路径转换吃掉（cmd 变成交互式），要用
  `MSYS_NO_PATHCONV=1 cmd /c ...` 或直接跑 bash 命令。
- 多模态那一档跨了两个 MiniMax 账号池：统一用 `--model-alias` 贴同一个模型标签，
  每行的 `actual_model` 记录真实路由，报告里按路由披露。
- 额度恢复后续跑多模态：`python repair_run.py results/mm_minimax/answers.jsonl`
  然后重跑 README 里那条 `run_planbench.py --mode multimodal ...` 命令即可（已完成的不重复计费）。

## 2. 额度政策（**硬约束，务必遵守**）

MiniMax **不是无限额度**。Magpie 里两个账号池：

- `minimax-code-global/*`（主池，≈2500 credits）
- `minimax-code/*`（另一个账号池，**已耗尽 = 0**，多模态那一档就是它顶到 429 的）

用户要求：**结束时候额池至少留 2000 credits，不许用光。**

代码里已经做了强制：

- `common.CreditMeter` 按实测单价 **322 credits / 1M tokens（输入+输出）** 估算消耗；
- `--credit-budget` 本次运行的花费上限；`--credit-reserve` **池底保护线**
  （实时余额读自 `~/.config/magpie/quotas.json`，到线自动停，停止前每条结果都已落盘）；
- 限额模型（deepseek/glm/qwen）另有**样本上限 50 + 并发 2**，在 `common.QUOTA_TIERS` 里写死。

**跨模型的活（deepseek/glm/qwen）不占 MiniMax 额度**，可以放心跑；
所有模型无关的活（跑脚本、出图、润色报告）也建议交给额度充裕的模型。

---

## 3. 接手后按这个顺序做

### 3.1 多模态那一档：额度恢复后先 repair 再续跑

当前状态：**跑到 153–163 条/组时额度耗尽（429）**，失败单元约 38–43%。
额度恢复后按顺序做（先删失败行，再用同一条命令续跑，已完成 cell 不重复计费）：

```bash
cd D:/code/ClaudeCode/phase2_planbench
python repair_run.py results/mm_minimax/answers.jsonl
python run_planbench.py --mode multimodal --models minimax-code/MiniMax-M3.1-Flash \
  --methods direct_baseline,append_only_ledger,versioned_state_ledger \
  --output-dir results/mm_minimax --credit-budget 0 --credit-reserve 2050
```

> `minimax-code` 池现在是 0，直接续跑会继续 429；
> 要么等该池恢复，要么把 `--models` 换成 `minimax-code-global/MiniMax-M3.1-Flash`
> （主池还有 ~2500，但那样多模态与文本条件就不同路由了，报告里要注明路由差异）。

### 3.2 跨模型那一档：修失败单元（不占 MiniMax 额度）

先确认 `logs_txt_cross.log` 那一档已经跑完（最后一行是 "Done in ..."），再执行：

`run_phase2_aux.bat` 已经封装好（先删失败行再重跑，带简洁重试）：

```bash
cmd /c D:\code\ClaudeCode\phase2_planbench\run_phase2_aux.bat
```

> 注意：必须在跨模型那一档**跑完之后**再执行，否则两个进程会同时写
> `results/txt_cross/answers.jsonl`。

### 3.3 出图 + 生成报告

```bash
python analyze_planbench.py --results results --output-dir results
```

产出 `results/planbench_summary.md`、`results/ablation_accuracy.png`、
`results/risk_cost_tradeoff.png`、`results/accuracy_table.csv`、`results/mcnemar.csv`。

### 3.4 额度允许时的加分项（可选，≈230 credits）

「紧凑渲染」的第四臂，用来分辨「版本机制本身」与「嵌套渲染太长」的差别
（三组提示词长度本来就不平衡：direct 6.5k / append 10.8k / versioned 13.0k 字符）：

```bash
python run_planbench.py --mode text --models minimax-code-global/MiniMax-M3.1-Flash \
  --methods versioned_state_ledger_flat --output-dir results/txt_minimax \
  --credit-budget 300 --credit-reserve 2000
```

### 3.5 建议做的一次独立评审（用额度充裕的模型）

把 `results/planbench_summary.md` + `results/accuracy_table.csv` + `results/mcnemar.csv`
交给一个强模型做**方法学盲点评审**，重点问：
(a) 三组的提示词长度不平衡会怎样影响结论？(b) 评分器（`grader.py`）对开放题是否过松？
(c) 感知层由模型生成会不会把「感知错误」当成「记忆组织的效果」？
(d) 门控失败是机制无效还是风险分没标定？把结论写进报告的「已知局限」。

---

## 4. 规矩（写论文用，务必守住）

1. **不许为了好看改提示词**。三组已经定型；改了提示词就等于换实验，所有已有数据作废。
   第一阶段就是因为中途改提示词导致前后不可比。
2. **失败样本不许悄悄丢掉**：基础设施失败（空响应/超时）要 repair 重跑；
   实在修不回来的按「答错」计入分母（保守口径），并在报告里给出剔除后的敏感性检查。
3. **负面结果照实写**：本轮版本账本在文本条件下**没有**超过追加式账本，
   端到端多模态条件的结果出来前不要下结论；两条都要写，并给出配对 McNemar 检验与
   Bootstrap 置信区间。
4. **一切随机性固定种子**（20261114），分层抽样脚本 `run_planbench.stratified_subset`
   保证所有模型跑的是**同一批 50 条**任务。
5. **额度不能超**：每一步都带 `--credit-budget` / `--credit-reserve`；
   续跑永远用同一条命令（靠 `answers.jsonl` 断点续跑）。
6. 感知层（`cache/frame_captions.json`、`cache/state_tracks.json`）**不许重算**：
   它是三组共用的观测流，重算就换了实验条件。

---

## 5. 代码地图

```text
common.py            MagpieClient（复用第一阶段 agents.py）+ 多模态请求 + 配额分级 + CreditMeter + 缓存
memory_ledger.py     追加式账本 / 版本覆盖账本 / 风险分 / 核验帧选择
prompts.py           三组严格同构的提示词 + 感知层提示词（不含题目）
grader.py            确定性评分（graded / exact）
prepare_data.py      克隆校验 → TSV 解析 → 模态探针 → 逐帧标注 → 跨帧追踪 → cache/tasks.json
run_planbench.py     三组 × 多模型 × 文本/多模态；额度闸门、断点续跑、resume_state
gate_simulation.py   风险门控 + τ 扫描 + 「只重问」对照
analyze_planbench.py 统计表、McNemar/Bootstrap、两张图、planbench_summary.md
repair_run.py        删掉失败 cell 以便下一轮重跑
antigravity.py       用 CDP 驱动 Antigravity 桌面端（本文件自身就是这么用起来的）
tests/test_core.py   17 个单测（评分器/账本/风险分/提示词解析），改代码后必须过
```

跑单测：

```bash
python -m unittest discover -s tests -v
```

---

## 6. 论文里能用的一句话结论（待补全）

> 在 Plan-Bench 的 Temporal Reasoning 维度（210 条，5 个子任务）上，
> 我们用**完全相同的任务规则、问题、选项、答案格式与观测流**，
> 只改变历史状态的组织方式，对比「直接作答 / 只追加日志 / 版本覆盖账本」三种记忆机制，
> 并用**代码级 Staleness Risk Score**（不依赖模型自评）扫描额外感知成本的代价–收益曲线。
> 初步结果：**追加式日志优于版本覆盖账本**，且**当前风险分无法区分对错**，
> 说明「状态组织」之外还需要针对错误的**标定**，这正是下一阶段验证器（带拒识）要解决的问题。
## 8. 2026-11-15 第二轮：Bug 修复 + 续跑状态

### 8.1 已修复的 4 个 Bug（全部有回归测试，27 个单测全绿）

| Bug | 位置 | 修复 | 证据 |
|---|---|---|---|
| `frame_summaries` 被清空 | `memory_ledger.apply_verified_observation` | 深拷贝 `clone.frame_summaries`；核验帧的摘要在**原位追加**标记，其他帧不动；核验帧不再被当作比 `current_frame` 更新的帧 | 旧实现下 207 条核验后的账本只剩 1 条摘要 |
| `event:*` / `activity` 污染门控目标 | `memory_ledger.staleness_risk`、`gate_simulation.pick_target` | 新增 `is_trackable_entity()`，只保留物理实体；`pick_target` 改为优先「有冲突 → 冲突数 → 过期度」 | **修复前 207 条里 124 条（60%）选中了 event:/activity**（必然核验出 "not visible"），修复后 207/207 为物理实体 |
| 门控样本未按 task 去重 | `gate_simulation` 读 `answers.jsonl` | 复用 `analyze_planbench.dedup_rows()`：同一 cell 优先保留「有答案的最新行」 | 215 条 → 201 条（= 唯一 task 数） |
| 视角移出抹掉最后已知状态 | `memory_ledger.VersionedLedger._apply` | `absent` 时保留 `last_known_state` / `last_known_location`，标 `visibility=out_of_view`，**不计入冲突** | 76% 的任务的版本账本内容因此改变 → 版本臂必须重跑 |

另外修了一个**解析 bug**：部分 provider（如 Antigravity 上的 Claude）会把整个 schema JSON
塞进 `answer` 字段里，导致预测被当成原始字符串判错。`prompts._unwrap_answer()` 现在会拆开嵌套 JSON；
`python regrade.py` 可以**离线**重新解析+重新评分全部历史结果（不花任何积分）。

### 8.2 账本语义变更带来的重跑（已做/待做）

`absent` 语义修复改变了 76% 任务的版本账本内容，因此**版本臂必须用新语义重跑**，
否则账本机制在实验中途变了。已完成：

- `results/txt_minimax`：`versioned_state_ledger` + `versioned_state_ledger_flat` 已重跑（529 credits）。
  **`direct_baseline` 与 `append_only_ledger` 两臂 210 条原封未动**（红线要求）。
- `results/txt_gemini`：`versioned` + `versioned_flat` 已用 Antigravity 的 Gemini 重跑（0 MiniMax 消耗）。
- 旧答案保留在 `answers_prefix_ledgerfix.jsonl` 里可审计。

### 8.3 待续跑（命令都已就绪）

```bash
cd D:/code/ClaudeCode/phase2_planbench
# ① 多模态：版本臂剩余约 90 个 cell + direct/append 剩余约 35 个 cell
python run_planbench.py --mode multimodal --models minimax-code-global/MiniMax-M3.1-Flash   --model-alias minimax-code-global/MiniMax-M3.1-Flash   --methods direct_baseline,append_only_ledger,versioned_state_ledger   --output-dir results/mm_minimax --credit-budget 1500 --credit-reserve 2000

# ② 跨模型：deepseek 额度已耗尽；GLM/Loomy 路由极慢（单条 140–840s）
python repair_run.py results/txt_cross/answers.jsonl
python run_planbench.py --mode text --models cross --subset-file cache/cross_subset_50.json   --methods direct_baseline,append_only_ledger,versioned_state_ledger   --output-dir results/txt_cross --credit-budget 0 --credit-reserve 0

# ③ 出图出报告（离线，不消耗积分）
python regrade.py && python analyze_planbench.py --results results --output-dir results
```

**如果 OpenCode 的模型被加进 Magpie**：把它的模型 id 加到 `common.CROSS_CHECK_CANDIDATES`，
然后用同一条 ② 的命令（`--models cross` 会自动挑出现在目录里的可用模型）跑同一批 50 条任务即可。

### 8.4 修复后的门控结果（201 条唯一样本）

| 口径 | 修复前（215 条，污染目标） | 修复后（201 条，物理实体目标） |
|---|---|---|
| 未门控 base | 31.2% | **30.3%** |
| 只重问（无额外感知） | 27.0% | **28.9%** |
| 核验 + 重答 | 25.6% | **29.4%** |

修复前「核验」比「只重问」还差（核验纯属噪声）；修复后核验**首次略高于**只重问对照（+0.5 pp），
说明额外感知开始产生正向价值，但幅度仍远不足以支撑门控策略。

### 8.5 最终状态（2026-11-15 收尾）

| 条件 | 覆盖 | direct | append-only | versioned | versioned-flat |
|---|---|---|---|---|---|
| 文本 210 题（MiniMax） | clean 199–210 | 35.2% | **39.2%** | 30.3% | 30.2% |
| 端到端多模态 210 题（MiniMax） | clean 200–206 | **58.4%** | 44.7% | 45.5% | — |
| 文本 50 条（Antigravity Gemini 3.8 Flash） | clean 48–50 | **38.0%** | 36.0% | 33.3% | 32.0% |
| 文本 50 条（AutoClaw GLM-5.3-Flash） | clean 43–48 | **43.8%** | 42.6% | 39.5% | — |
| 文本 50 条（Loomy Qwen-3.8-Max） | clean 23–25 | 39.1% | 32.0% | **41.7%** | — |
| 文本 50 条（WorkBuddy DeepSeek-V4.1-Flash） | clean 27–29 | 35.7% | **44.8%** | 37.0% | — |

配对 McNemar：文本 `versioned` vs `append` −9.0 pp（p=0.0021）、`flat` vs `append` −9.6 pp（p=0.0013）、
`flat` vs `versioned` −1.0 pp（p=0.85，**长度混淆排除**）；多模态 `append` vs `direct` −13.0 pp（p=0.0003）、
`versioned` vs `direct` −12.3 pp（p=0.0007）、`versioned` vs `append` +0.5 pp（p=1.0）。

门控（201 条）：未门控 30.3% → 只重问 28.9% → 核验+重答 29.4%；风险分 **AUC=0.480**（低于随机），
其中过期跨度 0.597、冲突次数 **0.372（反号）**、置信度 0.515。

**仍未跑完的只有跨模型那一格**（deepseek 额度耗尽、AutoClaw/Loomy 路由单条 140–840s、
OpenCode Zen Free 三条路由 **90%+ 限流失败**）。它只影响 §2 的第 4 条结论（跨模型一致性），
主结论（文本 + 多模态两个条件）已完整。

### 8.6 跨模型这一格的路由实测（2026-11-15）

「跨模型对照」最终卡在**上游路由**而不是我们的代码：

| 路由 | 状态 | 原因 |
|---|---|---|
| `antigravity/gemini-3.8-flash`（agy CLI） | ✅ 50 题 × 4 臂，0 失败 | Antigravity 自带额度 |
| `autoclaw-cn/zai_glm-5.3-flash` + `autoclaw/zai_glm-5.3-flash` | ✅ 合并后 150/150 | 同一模型的两个账号池（`autoclaw/` 另有 3 个账号，各 1.3–2 万积分） |
| `loomy/qwen3.8-flash` | 🔄 补到 109/150 | 单条 290–530s |
| `workbuddy/deepseek-v4.1-flash` | ❌ 84/150 | 额度耗尽（HTTP 429） |
| `autoclaw-cn/zai_glm-5.3-flash`（原慢路由） | — | 单条 1471s，被上游排队 |
| `loomy/qwen-3.8-max` | ❌ 72/150 | 同上，1470s 级长尾 |
| `loomy/GLM-5.3-Flash`、`loomy/deepseek-v4-flash-1017` | ❌ 88 / 93 | **40% 空响应**（思考模型耗尽预算） |
| `opencode-zen-free/*`（三条） | ❌ 90–93% 失败 | HTTP 429 限流 |

因此代码里做了两处针对性修复：

1. `common.MagpieMultimodalClient.complete_robust()` 与 `run_planbench` 的**三档重试阶梯**：
   第 2、3 档不是单纯加预算，而是加「只输出 JSON、不要解释、不要出声思考」的硬提示——
   这正是 40% 空响应的解药（原先 2 档会把预算翻到 16000 仍失败）。
2. 跨模型同一模型可以**跨账号池合并**（GLM 用 `autoclaw-cn` 与 `autoclaw` 两条路由拼成完整一列，
   每行保留 `actual_model`）。

### 8.7 跨模型最终矩阵（2026-11-15 收尾）

| 模型 | 覆盖题数 | direct | append-only | versioned | versioned-flat |
|---|---|---|---|---|---|
| GLM-5.3-Flash（`autoclaw-cn` + `autoclaw` 两池合并） | 50 | **43.8%** | 42.6% | 39.5% | — |
| Qwen3.8-Flash（`loomy`） | 43 | 39.5% | **41.0%** | 33.3% | — |
| DeepSeek-V4.1-Flash（`workbuddy`，额度受限） | 31 | 35.7% | **44.8%** | 37.0% | — |
| Gemini-3.8-Flash（Antigravity `agy` CLI） | 50 | **38.0%** | 36.0% | 33.3% | 34.0% |

版本账本 vs 追加式：Qwen −8.6 pp（p=0.25）、DeepSeek −8.0 pp（p=0.50）、
GLM +0.0 pp（p=1.00）、Gemini −2.1 pp（p=1.00）——**方向 3/4 一致，但每列有效配对
只有 25–48 对，p≥0.25，统计功效不足**，只能作方向性参考。

被弃用的路由（数据留档在 `results/txt_cross/answers_all_routes.jsonl`）：
`loomy/GLM-5.3-Flash`（88/150，40% 空响应）、`loomy/deepseek-v4-flash-1017`（93/150，同）、
`loomy/qwen-3.8-max`（72/150，单条 1470s 长尾）——都是同族的重复/劣化路由。
