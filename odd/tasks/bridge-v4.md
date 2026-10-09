# Bridge v4: thin relay to the secure-opencode orchestrator

Handover and feature document. Written 2026-10-09 for the agent that continues this work.
Tracked as plan `pl-863`. Nothing in this document has been implemented yet.

## Objective

The proxy's opencode bridge becomes a thin relay. Every bridged request goes to the
systemd-owned secure-opencode server on `127.0.0.1:4096`, and that server's orchestrator
runs the process (ODD, sandbox workers). The proxy keeps:

- no SDD mode,
- no opencode server of its own,
- no opencode config of its own,
- no permission policy. `opencode-perm` owns permission prompts.

## Why

- Gentle AI v4.0.0 (released 2026-10-01) retires SDD. The proxy still carries an SDD mode.
- The user wants bridged sessions on the secure server so that permission prompts are
  revealed and resolved by the existing `opencode-perm` process, and so that the secure
  server's config is the only config.
- User decision, 2026-10-09: "we could just allow the orchestrator to run the process".
- User decision, 2026-10-09: the serve config will come from secure-opencode, so the
  global-config change in PR #6 (commit `e158204`) is moot and is not to be ported.

## Repository state at handover

- Branch `feat/openchamber-bridge-backend`, head `88f1c5b`, pushed to `origin`
  (`EnigmaJazz/Proxy`). Pull request #8 is open and mergeable.
- Pull requests #4, #5, #6 and #7 are superseded by #8 and should be closed unmerged.
  The user has to close them (the previous agent was not permitted to).
- This work should start on a new branch cut after #8 merges. If #8 is not merged yet,
  ask the user before branching from `feat/openchamber-bridge-backend`.
- This file is untracked. Commit it with the first work unit.
- Tests: `.venv/bin/python -m pytest tests/ -q` passes with 641 tests on this machine.
  Seven tests fail in a checkout that lacks the git-ignored `local_config.py`. That is
  environmental, not a regression.

## Rules that apply to every task

Read `AGENTS.md` and `CLAUDE.md` first. The points that are easy to miss:

- Conventional commits. No `Co-Authored-By` and no AI attribution.
- Each routed task appends a row to `ROUTER-LOG.md` and adds a
  `ROUTED: <class>@<gate-outcome>` trailer to its commit.
- New code paths need a regression test (`AGENTS.md` rule 7). Deletions need the tests
  that covered the deleted behaviour removed or rewritten, not left failing.
- A pre-push hook runs `tools/sync_model_profiles.py --check`. This work does not touch
  model profiles, so it should pass unchanged.
- TDD mode is not configured for this repository. Run the ordinary checks; do not invent
  a TDD setting.
- On the secure server the proxy repository is read-only for the orchestrator. All edits
  and test runs go through sandbox workers.

## Hard safety constraints

- Never start, restart, recycle, signal or stop `secure-opencode.service` or the process
  listening on port 4096. Restarts are the user's action.
- Never point the existing `ServeBackend` at port 4096 before task T4 lands. Its recycle
  code finds the server process by port and sends it SIGTERM.
- Never POST to a permission endpoint on port 4096 as a test. GET requests are fine.
- Do not edit files under `/home/james/agent-sandbox-integration`. T7 is run by the user.

## What the mapping found

Line numbers are as of commit `88f1c5b`. Re-check them before editing.

### Secure server

- Launch: `nono run --profile <repo>/nono/profile/opencode-secure.json -- opencode serve
  --hostname 127.0.0.1 --port 4096`, from
  `/home/james/agent-sandbox-integration/scripts/start-secure-opencode`. No auth and no
  XDG override, so it loads `~/.config/opencode/opencode.json`. Version 1.18.35.
- `default_agent` is `gentle-orchestrator`. Providers are `kinver` and `opencode-go`.
  No top-level `model`.
- Global permission: `bash`, `edit`, `write` and `apply_patch` are denied. `grep` is the
  only broad `ask`. Mutations go through `sandbox_*` tools.
- Sandbox scope: `/mnt/ai_storage/kinver-hub/proxy` is read-only. The nanobot workspace
  `/home/james/.nanobot/workspace` is not in scope at all.
- `scripts/register-project.ts <abs path>` adds a project root to the nono profile,
  the broker projects and `PROJECT_ROOTS`. It needs a restart of `sandbox-broker.service`
  and `secure-opencode.service` afterwards.
- The session store is the default one, shared with the TUI and OpenChamber.

### opencode-perm (`~/.local/bin/opencode-perm`)

- Discovery is by tailing `~/.local/share/opencode/log/opencode.log`. It sees every
  session on the server.
- It never resolves anything automatically. A human runs `opencode-perm once <id>` or
  `opencode-perm deny <id>`.
- It replies with `POST /permission/<id>/reply?directory=<dir>` and a body of
  `{"reply": "once"}` or `{"reply": "reject"}`. There is no "always".
- If something else answers first, it marks the prompt "resolved-elsewhere".

### Bridge today

- Backend interface: `OpenCodeBackend` in `opencode_backends.py:60`, 13 abstract methods.
  `ServeBackend` at line 236, `OpenChamberBackend` at line 644, selection at 1101-1118.
  `BACKEND` is resolved once at import. `OPENCODE_SERVE_URL` is `constants.py:186`.
- Server lifecycle in `opencode_bridge.py`: `is_opencode_serve_running` (623),
  `ensure_opencode_serve` (643), `_spawn_serve` (724), `_recycle_serve_if_low_memory`
  (2321), `_force_recycle_serve` (2362), `_sync_serve_config` (2412), `_find_serve_pid`
  (2549). Call sites: bridge 825, 839, 987, 992, 1703; `proxy.py:257`; both SDD cycle
  scripts.
- Permissions in `opencode_bridge.py`: `_RELAYED_PERMISSION_TYPES` (380),
  `_classify_permission_access` (407), `_post_permission_response` (452),
  `_handle_permission_event` (511), `_detect_pending_permission` (576). Reads are
  answered "always"; autonomous mode answers "always" for everything. The reply goes to
  the older route `POST /session/<sid>/permissions/<pid>` with `{"response": ...}`.
- Session directory: `OPENCODE_BRIDGE_DIRECTORY` (the nanobot workspace, set in
  `local_config.py`) for plain requests, `OPENCODE_SDD_DIRECTORY` (the proxy repo) for
  SDD. Chosen at `routes.py:2686-2690` and `2787`.
- Agent is the constant `OPENCODE_AGENT = "gentle-orchestrator"` (`constants.py:240`).
  The system prompt is `_BRIDGE_SYSTEM_PROMPT` or `_SDD_AUTONOMOUS_SYSTEM_PROMPT`.
- SDD surface: `routes.py` (model alias `opencode-sdd` near 1181-1193, coding gate
  2939-3046, `_copy_sdd_output_to_workspace` at 618, apply-executor carve-out near 1087),
  `opencode_bridge.py` (prompts at 92-172, the `autonomous` parameter throughout),
  `constants.py` (228, 280), `scripts/sdd_autonomous_cycle.py`, `scripts/sdd_bridge_cycle.py`,
  `scripts/sdd_cycle_common.py`, and tests in `test_coding_gate.py`,
  `test_opencode_bridge.py`, `test_repo_context.py`, `test_dream_routing.py`,
  `test_sdd_autonomous_cycle.py`, `test_sdd_cycle_drivers.py`.

## Tasks

Do them in this order. Each task is one work-unit commit with its tests. Check an item off
only after its checks were observed.

- [x] **T0 (ct-58460)** Map the secure server, `opencode-perm` and the bridge. Done; the
  findings are above.
- [ ] **T1 (ct-58668)** Remove SDD from `routes.py`.
  - Drop the `opencode-sdd` model alias, the `sdd` decision and reply in the coding gate
    (the prompt offers `opencode` or `local`), the apply-executor carve-out,
    `_copy_sdd_output_to_workspace` and its call.
  - Acceptance: `model: "opencode-sdd"` is no longer special-cased; the gate never
    mentions SDD; the gate tests pass.
- [ ] **T2 (ct-58669)** Remove autonomous mode from the bridge.
  - Delete the three `_SDD_*` prompt constants, the `autonomous` parameter and its
    auto-allow and force-recycle branches, `OPENCODE_SDD_DIRECTORY`,
    `OPENCODE_SDD_TIMEOUT`.
  - Acceptance: no `autonomous` parameter remains in `opencode_bridge.py` or `routes.py`.
- [ ] **T3 (ct-58670)** Delete the three `scripts/sdd_*` files and their two test files,
  plus the contract sync test. Update `AGENTS.md` and `CLAUDE.md`.
  - Acceptance: `rg -i sdd --glob '!openspec/**' --glob '!docs/**' --glob '!ROUTER-LOG.md'`
    returns nothing outside history documents.
- [ ] **T4 (ct-58462)** Add a secure backend, selected by `OPENCODE_BACKEND=secure`,
  targeting `127.0.0.1:4096`.
  - `ensure` is a health check only. If the server is down, fail the request with a clear
    error; do not start anything.
  - Every spawn, recycle, config-sync, auth-copy and SIGTERM path is unreachable on this
    backend. Cover that with a test that fails if a subprocess is created or a signal is
    sent.
  - The stale-session sweep touches only sessions the bridge created.
  - Acceptance: the test above passes; with the backend selected, `proxy.py` startup
    spawns nothing.
- [ ] **T5 (ct-58464)** Stop answering permissions on the secure backend.
  - No auto-allow and no relay of the question to the client. While a prompt is pending
    for a bridge session, the stream status says the session is waiting on a permission.
  - Acceptance: a scripted pending permission produces the status line and no POST.
- [ ] **T6 (ct-58463)** Stop using the proxy's serve config on the secure backend.
  - Decide what remains of `_BRIDGE_SYSTEM_PROMPT`. Recommendation: keep only the lines
    that describe the channel (single reply, no interactive questions) and drop anything
    that restates orchestration.
  - Send no model override, so the secure server's agent config chooses the model.
- [ ] **T7 (ct-58461)** User action: register the nanobot workspace with
  `scripts/register-project.ts`, then restart `sandbox-broker.service` and
  `secure-opencode.service`. Alternatively, change `OPENCODE_BRIDGE_DIRECTORY` to a
  directory that is already registered. Ask the user which.
- [ ] **T8 (ct-58465)** Full test suite, then a live probe: one bridged request on port
  4096 whose `grep` permission prompt is resolved by the user through `opencode-perm`.
- [ ] **T9 (ct-58671)** After the probe passes, delete the bridge-owned server code, the
  serve config template, `opencode_chamber.py` and `OpenChamberBackend`, with their
  constants and tests.

Route per task: delegated writer for T1 to T6 and T9 (each touches two or more
non-trivial files). T7 and the T8 probe need the user.

## Not yet verified

- Whether opencode 1.18.35 still accepts the bridge's older permission reply route. T5
  removes the bridge's replies, so this only matters if a reply path is kept.
- Whether the `kinver` provider's base URL is reachable under the sandbox network rules.
  The profile opens local ports 4096, 13000 and 8788.
- Whether `opencode-perm` reports bridge sessions correctly. It needs the session's
  directory in the log. Confirm in the T8 probe.
- The exact behaviour of the bridge's stale-session sweep on a shared session store. This
  was inferred from the interface, not read.
- The SSE event shapes on 1.18.35. Bridge comments reference 1.18.15.

## Risks

- The secure server loads heavier plugins than the bridge's reduced config. The bridge's
  wedge detector and its 30-minute recycle assumed they were absent. With recycling gone,
  a wedged session can only be aborted, not cured by a restart.
- Bridged requests lose direct `bash`, `edit` and `write`. Work that used to edit the
  nanobot workspace directly now has to go through sandbox workers.
- The secure stack still runs Gentle AI 3.7.0 and still has `sdd-*` agents. Its v4
  upgrade is planned separately in `agent-sandbox-integration`. This work does not depend
  on it.

## Unrelated state changed in the same session

- The professional model is now `Nail-Qwen3.6-35B-A3B-MTP-UD-IQ4_XS.gguf`, linked as
  `Professional.gguf`, running with `-ncmoe 24`. VRAM use is about 10.1 of 12.0 GiB.
  The old file is kept as `Professional-Q4_K_M-2026-10-09.gguf`.
- The proxy's VRAM guard now reads real usage (commit `29ffa1c`) and is live.
- Known open defect, not part of this plan: `proxy.py:264` calls
  `_opencode_session_state(app)` without awaiting it. T9 will likely remove that line.

## Progress log

- 2026-10-09: mapping done (T0); plan and this document written. No code changed.
