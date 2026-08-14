# ROUTER-LOG — kinver-hub/proxy

One row per routed task in this repo, per the canonical recipe at
`/home/james/ai-workspace/workflow_optimisation/WORKFLOW.md`. The workspace
log remains the prove-out ledger; this repo-local log is the visible evidence
at the point of work.

| Date | Task | Class chosen | Reclassification | Gate outcome | Probe | Evidence reference | Notes |
|---|---|---|---|---|---|---|---|
| (first routed task lands here) |
| 2026-08-13 | Professional incident fixes (liveness probe, image downscaling, -np 1 drop-in) | bug investigation | none | disabled (RDD wedged; user chose kill switch) | no | review-f5290ace885bfe8a (wedged in correction_required; provider defect #2541 comment); 545 tests pass | SSRF fix from R1/R4 CRITICAL included in the commit |
| 2026-08-13 | opencode bridge pinned-session continuation broken (status-map semantics) | bug investigation | none | disabled (clone-local kill switch still off from #599 incident) | yes (live probe: follow-up resumed same session after fix; regression test reproduced drop) | tests/test_opencode_bridge.py TestStalePinSelfHeal; serve DB + journal evidence; 546 tests pass | root cause: 0ecda69 stale-pin check misread /session/status (idle sessions deleted from map by design); fix probes GET /session/<id> instead; ROOT CAUSE: external-path tools (read/glob/grep) trigger the serve permission gate whose ask-event delivery is broken headless (upstream #35066) — tools parked 'running' forever; fixed by pre-allowing permissions in the serve template (6142b34) + auto-retry wrap (4aa57c0) + tool rules + visibility |
| 2026-08-14 | check-updates.sh uv tool detection/updater fix | bug investigation | tiny fix | n/a (outside repo — ~/.local/bin; direct verification, no RDD gate) | yes (scratch venv six 1.16.0→1.17.0 through generated updater; strict env -i timer simulation rc=0) | ~/.local/bin/check-updates.sh; ~/.cache/check-updates log 09:40:28 | root cause: XDG_DATA_HOME=serve-config made `uv tool dir` resolve to a nonexistent dir, so the per-venv scan silently found nothing, and the stale "registry missing" assumption made the generated updater use `uv pip install` (bypassing uv's registry) instead of native `uv tool upgrade`; fix: validate tool dir exists + always scan the default location + dedupe by name; generated updater now uses native `uv tool upgrade` with per-venv fallback for orphaned venvs |
