@echo off
REM Phase-2 auxiliary work for the Antigravity agent to execute.
REM Cross-model cells only: these run on WorkBuddy / AutoClaw / Loomy quotas,
REM so they do NOT touch the MiniMax credit pool.
cd /d D:\code\ClaudeCode\phase2_planbench
echo === [1/2] drop infrastructure-failed cross-model cells ===
python repair_run.py results/txt_cross\answers.jsonl
echo === [2/2] re-run those cells (terse-retry enabled) ===
python run_planbench.py --mode text --models cross --subset-file cache\cross_subset_50.json --output-dir results\txt_cross --credit-budget 0 --credit-reserve 0
echo === AUX DONE ===
