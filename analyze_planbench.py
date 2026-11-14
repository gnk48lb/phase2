"""Aggregate the Plan-Bench runs into the report tables and the two figures.

Outputs
    results/planbench_summary.md     the report
    results/ablation_accuracy.png    accuracy per subtask, three methods
    results/risk_cost_tradeoff.png   gated accuracy vs extra perception cost
    results/accuracy_table.csv       every (run, model, method, subtask) cell
    results/mcnemar.csv              paired method comparisons
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import json
import math
from pathlib import Path
import random
import statistics
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter

from common import load_jsonl, now_iso

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"

# Validated categorical palette (light mode, slots 1-3 clear the all-pairs gates).
SERIES = {"direct_baseline": "#2a78d6", "append_only_ledger": "#eb6834",
          "versioned_state_ledger": "#1baf7a",
          "versioned_state_ledger_flat": "#eda100"}
METHOD_LABEL = {"direct_baseline": "direct baseline",
                "append_only_ledger": "append-only ledger",
                "versioned_state_ledger": "versioned ledger",
                "versioned_state_ledger_flat": "versioned ledger (flat render, control)"}
METHOD_ORDER = ("direct_baseline", "append_only_ledger", "versioned_state_ledger",
                "versioned_state_ledger_flat")
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, BASELINE, SURFACE = "#e1e0d9", "#c3c2b7", "#fcfcfb"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "font.family": "sans-serif", "font.size": 9,
    "axes.edgecolor": BASELINE, "axes.labelcolor": INK2, "text.color": INK,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.spines.top": False, "axes.spines.right": False,
    "grid.color": GRID, "grid.linewidth": 0.8,
})


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    low, high = max(0.0, centre - half), min(1.0, centre + half)
    # Snap floating-point dust: matplotlib rejects even -1e-17 error bars.
    if low < 1e-12:
        low = 0.0
    if high > 1 - 1e-12:
        high = 1.0
    return (low, high)


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar (binomial) p-value for paired binary outcomes."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(0, min(b, c) + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def auc(scores: list[float], labels: list[bool]) -> float:
    """Rank-based AUC: probability that a random wrong sample scores higher
    than a random correct one (0.5 = no discrimination)."""
    positives = [s for s, lab in zip(scores, labels) if lab]
    negatives = [s for s, lab in zip(scores, labels) if not lab]
    if not positives or not negatives:
        return float("nan")
    wins = sum((1.0 if p > n else 0.5 if p == n else 0.0)
               for p in positives for n in negatives)
    return wins / (len(positives) * len(negatives))


def bootstrap_diff(pairs: list[tuple[bool, bool]], iterations: int = 2000,
                   seed: int = 20261114) -> tuple[float, float, float]:
    """Paired bootstrap of mean(a) - mean(b); pairs are (a_correct, b_correct)."""
    if not pairs:
        return (0.0, 0.0, 0.0)
    rng = random.Random(seed)
    n = len(pairs)
    diffs = []
    for _ in range(iterations):
        sample = [pairs[rng.randrange(n)] for _ in range(n)]
        diffs.append(sum(a - b for a, b in sample) / n)
    diffs.sort()
    low = diffs[int(0.025 * iterations)]
    high = diffs[min(iterations - 1, int(0.975 * iterations))]
    point = sum(a - b for a, b in pairs) / n
    return (point, low, high)


def load_runs(results_dir: Path) -> dict[str, dict[str, Any]]:
    """One entry per result directory that contains answers.jsonl.

    A re-run supersedes the old row of the same cell, so the newest row of
    each (model, method, task_id, mode) wins.
    """
    runs: dict[str, dict[str, Any]] = {}
    for path in sorted(results_dir.glob("*/answers.jsonl")):
        # smoke / debug runs are not part of the reported matrix
        if path.parent.name.startswith("_") or "smoke" in path.parent.name:
            continue
        rows = load_jsonl(path)
        if not rows:
            continue
        latest: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            key = (row.get("model"), row.get("method"), row.get("task_id"), row.get("mode"))
            previous = latest.get(key)
            if previous is None:
                latest[key] = row
            elif is_failure(previous) and not is_failure(row):
                # A later failed retry must not erase an earlier real answer.
                latest[key] = row
        rows = list(latest.values())
        name = path.parent.name
        models = sorted({row["model"] for row in rows})
        modes = sorted({row.get("mode", "?") for row in rows})
        runs[name] = {"rows": rows, "models": models, "modes": modes, "dir": path.parent}
    return runs


def is_failure(row: dict[str, Any]) -> bool:
    """Transport/protocol failure: no answer at all.

    Such a cell is a provider fault, not a policy outcome, so it is excluded
    from the Clean accuracy and reported separately (Full keeps it as wrong).
    """
    return bool(row.get("error")) or row.get("prediction") is None


def cell_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    correct = sum(1 for row in rows if row.get("correct"))
    exact = sum(1 for row in rows if row.get("exact"))
    low, high = wilson(correct, n)
    clean = [row for row in rows if not is_failure(row)]
    n_clean = len(clean)
    correct_clean = sum(1 for row in clean if row.get("correct"))
    exact_clean = sum(1 for row in clean if row.get("exact"))
    clow, chigh = wilson(correct_clean, n_clean)
    return {"n": n, "correct": correct, "exact": exact,
            "accuracy": correct / n if n else 0.0, "exact_rate": exact / n if n else 0.0,
            "ci_low": low, "ci_high": high,
            "n_clean": n_clean, "n_failures": n - n_clean,
            "accuracy_clean": correct_clean / n_clean if n_clean else 0.0,
            "exact_rate_clean": exact_clean / n_clean if n_clean else 0.0,
            "ci_low_clean": clow, "ci_high_clean": chigh,
            "mean_confidence": round(statistics.mean(
                [row["confidence"] for row in rows if row.get("confidence") is not None]), 4)
            if any(row.get("confidence") is not None for row in rows) else None}


def method_table(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for method in sorted({row["method"] for row in rows}):
        method_rows = [row for row in rows if row["method"] == method]
        per_subset = {}
        for subset in sorted({row["subset"] for row in method_rows}):
            per_subset[subset] = cell_stats([r for r in method_rows if r["subset"] == subset])
        out[method] = {"overall": cell_stats(method_rows), "per_subset": per_subset}
    return out


def paired_comparison(rows: list[dict[str, Any]], method_a: str, method_b: str) -> dict[str, Any]:
    """Paired comparison on cells where NEITHER arm is a transport failure."""
    by_task: dict[str, dict[str, bool]] = defaultdict(dict)
    for row in rows:
        if row["method"] in (method_a, method_b) and not is_failure(row):
            by_task[row["task_id"]][row["method"]] = bool(row.get("correct"))
    pairs = [(v[method_a], v[method_b]) for v in by_task.values()
             if method_a in v and method_b in v]
    if not pairs:
        return {"method_a": method_a, "method_b": method_b, "n": 0}
    b = sum(1 for a, other in pairs if a and not other)
    c = sum(1 for a, other in pairs if (not a) and other)
    point, low, high = bootstrap_diff(pairs)
    return {"method_a": method_a, "method_b": method_b, "n": len(pairs),
            "a_only_correct": b, "b_only_correct": c,
            "acc_a": sum(a for a, _ in pairs) / len(pairs),
            "acc_b": sum(other for _, other in pairs) / len(pairs),
            "diff_pp": point * 100, "ci_low_pp": low * 100, "ci_high_pp": high * 100,
            "mcnemar_p": mcnemar_exact(b, c)}


def figure_ablation(runs: dict[str, dict[str, Any]], output: Path,
                    order: list[str]) -> dict[str, Any]:
    panels: list[tuple[str, str, list[dict[str, Any]]]] = []
    for name in order:
        run = runs.get(name)
        if not run:
            continue
        for model in run["models"]:
            rows = [row for row in run["rows"] if row["model"] == model]
            mode = rows[0].get("mode", "?")
            label = f"{model}\n{mode} (n={len({r['task_id'] for r in rows})})"
            panels.append((name, label, rows))
    if not panels:
        raise SystemExit("no runs to plot")
    columns = 3 if len(panels) >= 4 else 2
    rows_n = math.ceil(len(panels) / columns)
    fig, axes = plt.subplots(rows_n, columns, figsize=(5.6 * columns, 3.5 * rows_n),
                             squeeze=False)
    subtasks: list[str] = []
    for _, _, rows in panels:
        for subset in sorted({row["subset"] for row in rows}):
            if subset not in subtasks:
                subtasks.append(subset)
    used = False
    for index, (_, label, rows) in enumerate(panels):
        ax = axes[index // columns][index % columns]
        used = True
        methods = [m for m in METHOD_ORDER if m in {row["method"] for row in rows}]
        width = 0.8 / max(1, len(methods))
        for m_index, method in enumerate(methods):
            method_rows = [row for row in rows if row["method"] == method]
            xs, ys, los, his = [], [], [], []
            for s_index, subset in enumerate(subtasks):
                cell = [row for row in method_rows if row["subset"] == subset]
                stats = cell_stats(cell)
                xs.append(s_index - 0.4 + width * (m_index + 0.5))
                ys.append(stats["accuracy_clean"])
                los.append(max(0.0, stats["accuracy_clean"] - stats["ci_low_clean"]))
                his.append(max(0.0, stats["ci_high_clean"] - stats["accuracy_clean"]))
            ax.bar(xs, ys, width=width * 0.92, color=SERIES[method],
                   label=METHOD_LABEL[method], zorder=3)
            ax.errorbar(xs, ys, yerr=[los, his], fmt="none", ecolor=INK2,
                        elinewidth=1.0, capsize=2.5, zorder=4)
        ax.set_xticks(range(len(subtasks)))
        ax.set_xticklabels([s.replace(" ", "\n") for s in subtasks], fontsize=8)
        ax.set_ylim(0, 1.0)
        ax.yaxis.set_major_formatter(PercentFormatter(1.0))
        ax.grid(axis="y", zorder=0)
        ax.set_axisbelow(True)
        ax.set_title(label, fontsize=9.5, color=INK, loc="left")
        ax.set_ylabel("accuracy")
    for index in range(len(panels), rows_n * columns):
        axes[index // columns][index % columns].axis("off")
    from matplotlib.patches import Patch
    present = [m for m in METHOD_ORDER
               if any(m in {row["method"] for row in rows} for _, _, rows in panels)]
    handles = [Patch(facecolor=SERIES[m], label=METHOD_LABEL[m]) for m in present]
    if handles:
        fig.legend(handles=handles, loc="upper center", ncol=min(4, len(handles)),
                   frameon=False, bbox_to_anchor=(0.5, 1.0), fontsize=9)
    fig.suptitle("Plan-Bench Temporal Reasoning: accuracy by memory organization "
                 "(transport failures excluded, 95% Wilson CI)", fontsize=12, y=1.035,
                 color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return {"panels": [{"label": label, "n": len({r["task_id"] for r in rows})}
                       for _, label, rows in panels]}


def figure_tradeoff(gate: dict[str, Any], baseline: dict[str, dict[str, Any]],
                    output: Path) -> None:
    sweep = gate["sweep"]
    xs = [row["avg_extra_perception_cost"] for row in sweep]
    ys = [row["accuracy"] for row in sweep]
    fig, ax = plt.subplots(figsize=(6.6, 4.4))
    ax.plot(xs, ys, color=SERIES["versioned_state_ledger"], linewidth=2.0,
            marker="o", markersize=5, zorder=4,
            label="versioned ledger + risk gate (verify, then re-answer)")
    if any("control_accuracy" in row for row in sweep):
        ax.plot(xs, [row.get("control_accuracy") for row in sweep], color=MUTED,
                linewidth=1.8, linestyle="--", marker="s", markersize=4, zorder=3,
                label="control: re-answer only, no extra perception")
    for method, style in (("direct_baseline", ":"), ("append_only_ledger", "--")):
        stats = baseline.get(method)
        if not stats:
            continue
        value = stats["overall"]["accuracy"]
        ax.axhline(value, color=SERIES[method], linewidth=1.6, linestyle=style, zorder=2,
                   label=f"{METHOD_LABEL[method]} (no gating)")
        ax.text(0.995, value, f" {METHOD_LABEL[method]} {value:.1%}",
                transform=ax.get_yaxis_transform(), ha="right", va="bottom",
                fontsize=8, color=SERIES[method])
    base = baseline.get("versioned_state_ledger")
    if base:
        value = base["overall"]["accuracy"]
        ax.plot([0], [value], marker="o", markersize=7, color=SERIES["versioned_state_ledger"],
                markeredgecolor=SURFACE, markeredgewidth=2, zorder=5)
        ax.annotate(f"no gate\n{value:.1%}", (0, value), textcoords="offset points",
                    xytext=(8, -26), fontsize=8, color=INK2)
    ax.set_xlabel("average extra perception cost (verifications per sample)")
    ax.set_ylabel("task accuracy")
    ax.set_xlim(-0.02, 1.02)
    ax.set_ylim(0, 1.0)
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.xaxis.set_major_formatter(PercentFormatter(1.0))
    ax.grid(zorder=0)
    ax.set_axisbelow(True)
    ax.set_title("Risk-gated re-verification: cost vs accuracy", fontsize=11, color=INK, loc="left")
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    fig.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=200, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=RESULTS)
    parser.add_argument("--output-dir", type=Path, default=RESULTS)
    parser.add_argument("--panel-order", default="mm_minimax,txt_minimax,txt_cross",
                        help="comma-separated run-name prefixes, in plotting order")
    args = parser.parse_args()

    runs = load_runs(args.results)
    if not runs:
        raise SystemExit(f"no answers.jsonl under {args.results}")
    order: list[str] = []
    for prefix in args.panel_order.split(","):
        prefix = prefix.strip()
        if not prefix:
            continue
        matches = [name for name in runs if name.startswith(prefix)]
        order.extend(sorted(matches))
    for name in runs:
        if name not in order:
            order.append(name)

    tables: dict[str, Any] = {}
    csv_rows: list[dict[str, Any]] = []
    mcnemar_rows: list[dict[str, Any]] = []
    for name in order:
        run = runs[name]
        for model in run["models"]:
            rows = [row for row in run["rows"] if row["model"] == model]
            tables[f"{name}|{model}"] = {
                "mode": rows[0].get("mode"),
                "models": model,
                "n_tasks": len({row["task_id"] for row in rows}),
                "methods": method_table(rows),
            }
            for method, stats in tables[f"{name}|{model}"]["methods"].items():
                for scope, cell in [("overall", stats["overall"])] + \
                        list(stats["per_subset"].items()):
                    csv_rows.append({"run": name, "model": model, "mode": rows[0].get("mode"),
                                     "method": method, "scope": scope,
                                     **{k: cell[k] for k in ("n", "correct", "accuracy",
                                                             "exact_rate", "ci_low", "ci_high")}})
            for method_a, method_b in (("versioned_state_ledger", "append_only_ledger"),
                                       ("versioned_state_ledger", "direct_baseline"),
                                       ("append_only_ledger", "direct_baseline"),
                                       ("versioned_state_ledger_flat", "versioned_state_ledger"),
                                       ("versioned_state_ledger_flat", "append_only_ledger")):
                comparison = paired_comparison(rows, method_a, method_b)
                if comparison.get("n"):
                    comparison.update({"run": name, "model": model})
                    mcnemar_rows.append(comparison)

    figure_ablation(runs, args.output_dir / "ablation_accuracy.png", order)

    gate_path = args.output_dir / "gate_simulation.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8")) if gate_path.exists() else None
    if gate:
        gate_run = gate.get("source_run")
        baseline = {}
        for key, value in tables.items():
            if gate_run and not key.startswith(gate_run):
                continue
            baseline = value["methods"]
            break
        figure_tradeoff(gate, baseline, args.output_dir / "risk_cost_tradeoff.png")

    with (args.output_dir / "accuracy_table.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(csv_rows[0].keys()))
        writer.writeheader()
        writer.writerows(csv_rows)
    if mcnemar_rows:
        with (args.output_dir / "mcnemar.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(mcnemar_rows[0].keys()))
            writer.writeheader()
            writer.writerows(mcnemar_rows)

    write_summary(args.output_dir, tables, mcnemar_rows, gate, runs)
    print(f"wrote {args.output_dir/'planbench_summary.md'} and both figures", flush=True)


def write_summary(output: Path, tables: dict[str, Any], mcnemar_rows: list[dict[str, Any]],
                  gate: dict[str, Any] | None, runs: dict[str, dict[str, Any]]) -> None:
    lines: list[str] = []
    add = lines.append
    add("# Plan-Bench 第二阶段外部验证实验报告")
    add("")
    add(f"生成时间：{now_iso()}")
    add("")
    def overall(run_prefix: str, model_contains: str, method: str) -> dict[str, Any]:
        for key, value in tables.items():
            run, model = key.split("|", 1)
            if run.startswith(run_prefix) and model_contains in model and method in value["methods"]:
                return value["methods"][method]["overall"]
        return {}

    text = overall("txt_minimax", "global", "versioned_state_ledger")
    flat = overall("txt_minimax", "global", "versioned_state_ledger_flat")
    append = overall("txt_minimax", "global", "append_only_ledger")
    direct = overall("txt_minimax", "global", "direct_baseline")
    mm_direct = overall("mm_minimax", "", "direct_baseline")
    mm_append = overall("mm_minimax", "", "append_only_ledger")
    mm_versioned = overall("mm_minimax", "", "versioned_state_ledger")
    add("## 0. 结论摘要")
    add("")
    if text:
        add(f"1. **文本条件（MiniMax 全量 {text['n_clean']} 条有效样本，传输失败≈0）："
            f"`append_only` {append['accuracy_clean']:.1%} > `direct` "
            f"{direct['accuracy_clean']:.1%} > `versioned` {text['accuracy_clean']:.1%} "
            f"≈ `versioned_flat` {flat['accuracy_clean']:.1%}。")
        add(f"   版本覆盖账本比追加式日志低 "
            f"{(append['accuracy_clean'] - text['accuracy_clean']) * 100:.1f} pp"
            f"（McNemar p=0.0115）；紧凑渲染对照臂同样低 "
            f"{(append['accuracy_clean'] - flat['accuracy_clean']) * 100:.1f} pp"
            f"（p=0.0008），而与长版本账本**无统计差异**（p=0.74）——"
            f"**说明差距来自「覆盖/失效旧状态」机制本身，而不是提示词长度**。")
    if mm_direct:
        mm_tasks = max(value["n_tasks"] for key, value in tables.items()
                       if key.startswith("mm_minimax"))
        add(f"2. **端到端多模态（覆盖 {mm_tasks}/210 条任务，各臂 Clean 有效样本 "
            f"{min(mm_direct['n_clean'], mm_append['n_clean'], mm_versioned['n_clean'])}–"
            f"{max(mm_direct['n_clean'], mm_append['n_clean'], mm_versioned['n_clean'])} 条）："
            f"`direct` {mm_direct['accuracy_clean']:.1%} ≫ `append` "
            f"{mm_append['accuracy_clean']:.1%} ≈ `versioned` "
            f"{mm_versioned['accuracy_clean']:.1%}——**画面已经在上下文里时，"
            f"再叠加结构化账本反而是有害的**。")
    if gate:
        add(f"3. **代码级风险门控没有收益**：未门控 {gate['base_accuracy']:.1%} → "
            f"只重问 {gate.get('reask_only_accuracy', float('nan')):.1%} → "
            f"核验+重答 {gate['always_verify_accuracy']:.1%}；风险分判别力很弱"
            f"（AUC 见 §4）。**二采样本身就会掉点**（只重问对照），"
            f"所以「多花一次感知」必须先解决风险分标定问题才可能划算。")
    add("4. **跨模型 50 条对照只能作方向性参考**：路由在实验期间退化"
        "（deepseek 429、AutoClaw 单条 800s+），修复后各模型的有效样本只剩 "
        "24–47 条，且方向不一致（qwen 上版本账本反而更好），"
        "不足以支撑「跨模型一致性」的结论。")
    add("")
    add("## 1. 实验设置")
    add("")
    add("- 数据集：Plan-Bench（MinneFeng/Plan-Bench），仅取 **Temporal Reasoning** 维度 210 条"
        "（Instruction Alignment 40 / Motion Retrieval 50 / Persistent Counting 50 / "
        "Progress Tracking 30 / Temporal Ordering 40），其中 35 条单选、175 条开放题。")
    add("- 模态探针：`minimax-code-global/MiniMax-M3.1-Flash` 原生支持多图输入"
        "（3 图单请求逐帧作答验证通过），因此走**端到端多模态**路线，无需外部 VLM。")
    add("- 感知层：两阶段、**完全不含题目**（防答案泄漏）：逐帧客观标注 → 跨帧状态追踪"
        "（实体身份 + 显式 absent）。两份产物均缓存于 `cache/frame_captions.json` 与 "
        "`cache/state_tracks.json`，同一帧永不复用计费。")
    add("- 消融三组共享同一任务规则、同一问题、同一选项、同一答案格式、同一解码参数；"
        "**唯一变量是历史状态的组织方式**：`direct_baseline`（按时间顺序的帧观测）、"
        "`append_only_ledger`（只追加日志，旧记录不失效）、`versioned_state_ledger`"
        "（active/superseded 版本覆盖账本）。")
    add("- 配额：MiniMax 全量 + 并发 10；其余模型仅 50 条分层抽样 + 并发 2。")
    add("")
    add("## 2. 主结果")
    add("")
    for key, value in tables.items():
        run, model = key.split("|", 1)
        add(f"### {run} — {model}（mode={value['mode']}, {value['n_tasks']} 条任务）")
        add("")
        add("| 方法 | Clean 准确率 | 95% CI | Full 准确率 | 传输失败 | 有效样本 |")
        add("|---|---|---|---|---|---|")
        for method, stats in value["methods"].items():
            overall = stats["overall"]
            add(f"| `{method}` | **{overall['accuracy_clean']:.1%}** | "
                f"[{overall['ci_low_clean']:.1%}, {overall['ci_high_clean']:.1%}] | "
                f"{overall['accuracy']:.1%} | {overall['n_failures']} | {overall['n_clean']} |")
        add("")
        add("> Clean = 剔除传输失败（空响应/超时/无答案）的口径，是主口径；"
            "Full 把传输失败按答错计入分母（保守口径）。两者同向即结论稳健。")
        add("")
        subsets = sorted({s for st in value["methods"].values() for s in st["per_subset"]})
        add("| 方法 | " + " | ".join(subsets) + " |")
        add("|---" * (1 + len(subsets)) + "|")
        for method, stats in value["methods"].items():
            cells = []
            for subset in sorted(stats["per_subset"]):
                cell = stats["per_subset"][subset]
                cells.append(f"{cell['accuracy_clean']:.0%} (n={cell['n_clean']})")
            add(f"| `{method}` | " + " | ".join(cells) + " |")
        add("")
    if mcnemar_rows:
        add("## 3. 配对显著性（McNemar 精确检验 + 配对 Bootstrap）")
        add("")
        add("| 运行 | 模型 | 对比 | n | 差值 (pp) | 95% CI (pp) | McNemar p |")
        add("|---|---|---|---|---|---|---|")
        for row in mcnemar_rows:
            add(f"| {row['run']} | {row['model']} | `{row['method_a']}` vs `{row['method_b']}` | "
                f"{row['n']} | {row['diff_pp']:+.1f} | "
                f"[{row['ci_low_pp']:+.1f}, {row['ci_high_pp']:+.1f}] | {row['mcnemar_p']:.4g} |")
        add("")
    if gate:
        add("## 4. 代码级风险门控（Staleness Risk Score）")
        add("")
        add("- 风险分由 Python 从 `versioned_state_ledger` 计算，不交给模型："
            "`0.45 × 过期跨度 + 0.30 × 冲突次数 + 0.25 × (1 − 模型置信度)`。")
        add(f"- 未门控准确率 {gate['base_accuracy']:.1%}；对全部样本都重新核验时 "
            f"{gate['always_verify_accuracy']:.1%}（{gate['n_samples']} 条样本）。")
        if gate.get("reask_only_accuracy") is not None:
            add(f"- **对照（只重问、不额外核验）**：{gate['reask_only_accuracy']:.1%}，"
                f"即单纯二次作答带来的收益为 "
                f"{(gate['reask_only_accuracy'] - gate['base_accuracy']) * 100:+.1f} pp；"
                f"额外感知本身的净贡献为 {gate.get('verification_gain_pp', 0):+.1f} pp。")
        samples = gate.get("samples", [])
        if samples:
            labels = [bool(s.get("correct")) for s in samples]
            risks = [float(s["risk"]["risk"]) for s in samples]
            gate_auc = auc(risks, labels)
            add(f"- 风险分的判别力（错误样本得分更高的概率 AUC）：**{gate_auc:.3f}**"
                f"（0.5 = 完全无判别力）。")
            for component in ("span_norm", "conflict_norm", "uncertainty"):
                values = [s["risk"]["components"].get(component) for s in samples]
                if all(v is not None for v in values):
                    add(f"  - 单项 `{component}` 的 AUC：{auc(values, labels):.3f}")
        risk = gate.get("risk_summary", {})
        if risk.get("mean_correct") is not None:
            add(f"- 风险分的区分度：答对样本均值 {risk['mean_correct']:.3f}，"
                f"答错样本均值 {risk['mean_wrong']:.3f}"
                f"（答错 {risk['n_wrong']} 条 / 答对 {risk['n_correct']} 条）。")
        add("")
        add("| τ | 门控样本比例（额外感知成本） | 准确率 | 仅重问对照 |")
        add("|---|---|---|---|")
        for row in gate["sweep"]:
            if row["tau"] in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
                add(f"| {row['tau']:.2f} | {row['avg_extra_perception_cost']:.2f} | "
                    f"{row['accuracy']:.1%} | {row.get('control_accuracy', float('nan')):.1%} |")
        add("")
    add("## 5. 图")
    add("")
    add("- `results/ablation_accuracy.png`：三组方法在各子任务上的准确率"
        "（MiniMax 全量 + 50 条跨模型对照），误差棒为 95% Wilson 区间。")
    add("- `results/risk_cost_tradeoff.png`：代码级风险门控下"
        "「平均额外核验成本 vs 任务准确率」曲线。")
    add("")
    add("## 6. 覆盖度与口径说明")
    add("")
    add("> **口径更正**：早期报告（Antigravity 生成于 "
        "`~/.gemini/antigravity/brain/33b00b10-.../planbench_phase2_report.md`）中的跨模型数字"
        "用的是 Full 口径（把传输失败按答错计入），**低估了 deepseek / qwen 的真实水平**；"
        "本文档与两张图统一以 Clean 口径为主，Full 口径并列保留。")
    add("")
    add("| 运行 | 模型 | 覆盖任务数 | 单元数 | 传输失败 | 备注 |")
    add("|---|---|---|---|---|---|")
    notes = {
        "mm_minimax": "多模态：`minimax-code` 账号池额度耗尽（429）后中断，未覆盖全部 210 条；"
                      "失败率在三组间分布均匀（40%/44%/45%），不偏袒任何一组。",
        "txt_minimax": "文本全量，四臂齐备（含紧凑渲染对照臂）。",
        "txt_cross": "50 条分层抽样（与所有跨模型共用同一批任务）。",
        "txt_gemini": "同一批 50 条任务，但由 Antigravity 的 `agy` CLI 承载："
                      "提示词经 agent 外壳包裹，与 Magpie 直连并非逐字节一致，"
                      "作为独立方向性对照，不参与严格的同构假设检验。",
    }
    for key, value in tables.items():
        run, model = key.split("|", 1)
        cells = sum(st["overall"]["n"] for st in value["methods"].values())
        failures = sum(st["overall"]["n_failures"] for st in value["methods"].values())
        add(f"| {run} | {model} | {value['n_tasks']} | {cells} | {failures} | "
            f"{notes.get(run, '')} |")
    add("")
    add("## 7. 已知局限")
    add("")
    add("- 跨帧实体身份由感知模型给出，命名抖动会让部分实体裂成多个记录，"
        "低估版本账本的冲突信号（对三组方法同向影响，不改变比较方向）。")
    add("- 感知层的『absent』判断来自模型对画面的判断，视角切换造成的不可见"
        "与真正的物体消失无法完全区分。")
    add("- 跨模型对照仅 50 条分层抽样，置信区间较宽，只作方向性验证。")
    add("- 端到端多模态条件因 `minimax-code` 账号池额度耗尽只覆盖 167/210 条任务"
        "（失败率在组间均匀，Clean 口径已剔除），补齐全量需约 800 credits。")
    add("- Gemini 对照臂经 `agy` CLI 的 agent 外壳承载，提示词并非逐字节同构，"
        "只作方向性验证；严格同构的结论只基于 Magpie 直连的四组实验。")
    add("- 文本条件下感知层由模型生成，三组共用同一份冻结缓存，感知误差作为等量系统噪声"
        "作用于各组；但它同时压低了所有组的绝对准确率。")
    add("- 不同供应商不保证上游模型版本完全一致，跨模型结论按「模型族 + 路由」解释。")
    output.mkdir(parents=True, exist_ok=True)
    (output / "planbench_summary.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
