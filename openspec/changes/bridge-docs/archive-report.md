# Archive Report — bridge-docs

- **Change**: `bridge-docs` — OpenCode Bridge Reference Documentation
- **Archived**: 2026-08-08
- **Type**: Pure documentation (docs-only) — no spec-level runtime behavior changes
- **Persistence mode**: both — filesystem authoritative; archive report at
  `openspec/changes/bridge-docs/archive-report.md`; Engram handled by orchestrator
- **Verified commit**: `d9a234c` (`d9a234caa085797c8b55b41b77f5d4c7b719f6a19`) on branch
  `sdd/opencode-bridge-sdd-reliability/pr-4`
- **Verdict**: PASS — all 3 REQ-DOC requirements / 6 scenarios, per `verify-report.md`

## Change Summary

The OpenCode bridge (`opencode_bridge.py`, repo root, ~1530 lines) had grown
significant machinery — serve lifecycle/recycling, permission relay, wedge
detection, SDD-autonomous mode — with no stable reference doc (`AGENTS.md` maps it
in one line; `docs/brainstorms/` are dated requirement docs). Wrong mental models
(blocking/streaming confusion, stale pre-`d9f7284` serve-scoped config) had bitten
maintainers. This change delivered one current reference doc at
`docs/opencode-bridge.md` (301 lines), structured per the seven proposal scope
areas, anchored to current code by function/identifier name (never line numbers).

The archive is a close-out report, not a delta-spec sync: the change is
docs-only, with no spec-level runtime behavior changes to merge into
`openspec/specs/`. Runtime bridge behavior remains governed by the sibling change
`opencode-bridge-sdd-reliability` (`openspec/specs/opencode-bridge`).

## Artifact Inventory

| Artifact | Path | Status |
|---|---|---|
| Proposal | `openspec/changes/bridge-docs/proposal.md` | ✅ read (8 obs) |
| Spec | `openspec/changes/bridge-docs/specs/bridge-docs/spec.md` | ✅ read (1 obs) |
| Design | `openspec/changes/bridge-docs/design.md` | ✅ read (1 obs) |
| Tasks | `openspec/changes/bridge-docs/tasks.md` | ✅ read (1 obs) — all 5 tasks `[x]` |
| Verify report | `openspec/changes/bridge-docs/verify-report.md` | ✅ read (1 obs) — PASS |
| Archive report | `openspec/changes/bridge-docs/archive-report.md` | ✅ this file |
| Deliverable | `docs/opencode-bridge.md` | ✅ committed `d9a234c`, 301 lines |

Observation IDs read (Engram traceability): proposal, spec, design, tasks,
verify-report — read in full from filesystem; Engram mirror handled by
orchestrator. No `reviewGate` present in structured status: no review was started
for this candidate (kill switch context, docs-only); nothing to read or block on,
archive proceeds under ordinary repository policy.

## Requirement Satisfaction (from verify-report)

| Requirement | Scenarios | Verdict |
|---|---|---|
| REQ-DOC-1: Document exists at canonical path as plain reference markdown | 1/1 | **PASS** |
| REQ-DOC-2: Covers the seven scope areas, blocking and streaming paths separate | 2/2 | **PASS** |
| REQ-DOC-3: Accuracy matches CURRENT code | 3/3 | **PASS** |

Total: 3/3 requirements, 6/6 scenarios PASS, per `verify-report.md` (verified
against committed bytes via `git show d9a234c`). No CRITICAL or WARNING issues in
the verify report. One SUGGESTION recorded for traceability: the comparison
table's Trigger-entrypoint cell names `opencode_escalation` where the design
verification-plan shorthand writes `opencode_chat` — both identifiers are
accurate (queue-worker entrypoint vs underlying blocking function) and
REQ-DOC-2 Scenario-2 is satisfied; not a defect.

## Final-State Facts (at close)

- **Deliverable**: `docs/opencode-bridge.md` (301 lines) committed as `d9a234c` on
  branch `sdd/opencode-bridge-sdd-reliability/pr-4`, message
  `docs(bridge): add opencode bridge reference doc`, no AI attribution
  (author: Enigmajazz; no `Co-Authored-By` trailer). Commit surface = exactly that
  one file (corroborated: `git show --stat d9a234c` → `1 file changed, 301
  insertions(+); docs/opencode-bridge.md`).
- **Verify verdict**: PASS — all 3 REQ-DOC requirements / 6 scenarios, per
  `verify-report.md`.
- **Deviation recorded during apply**: `docs/` is deliberately gitignored
  (`.gitignore:249` — "Local brainstorms and design scratch; tracked copies live in
  openspec/"); the commit force-added the doc (`git add -f`) because the repo had
  never tracked a file under `docs/`. This is the first tracked `docs/` file. The
  gitignore rule is UNCHANGED.
- **No runtime code, tests, `AGENTS.md`, or READMEs touched** — no-code-change
  guard held (commit touches only `docs/opencode-bridge.md`).

## Open Items

- **gitignore tension (open maintainer decision)**: `.gitignore:249` (`docs/`)
  now conflicts with a tracked file. Two options for the maintainer, not resolved
  here:
  1. Add a `.gitignore` exception for pinned docs paths (e.g. un-ignore
     `docs/opencode-bridge.md` or `docs/*.md` selectively) so future doc updates
     track normally; or
  2. Accept force-add (`git add -f`) as the standing workflow for pinned docs.
  This archive deliberately does NOT touch `.gitignore` — the decision is the
  maintainer's.

## Rollback Note

File-only, zero runtime risk: delete `docs/opencode-bridge.md` or revert
`d9a234c` (`git revert d9a234c` / reset the docs commit on the feature branch).
No migration required; nothing else in the change needs unwinding.

## Final State

SDD cycle complete: planned, implemented, verified (PASS), and archived. No
runtime behavior changed; the reference doc is the sole deliverable.
