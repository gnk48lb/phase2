@echo off
REM Phase-2 auxiliary work: repair the failed cross-model cells.
REM These run on WorkBuddy / AutoClaw / Loomy quotas, so they do NOT touch the
REM MiniMax credit pool.  Only the three primary arms are repaired (the flat
REM control arm was never run for the cross models).
cd /d D:\code\ClaudeCode\phase2_planbench
echo === [1/2] drop infrastructure-failed cross-model cells ===
python repair_run.py results\txt_cross\answers.jsonl
echo === [2/2] re-run those cells (terse-retry enabled) ===
python run_planbench.py --mode text --models cross --subset-file cache\cross_subset_50.json --methods direct_baseline,append_only_ledger,versioned_state_ledger --output-dir results\txt_cross --credit-budget 0 --credit-reserve 0
echo === AUX DONE ===
