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
