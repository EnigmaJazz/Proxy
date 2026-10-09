#!/usr/bin/env bash
# run_nail_mtp_sweep.sh — Nail-MTP narrow sweep + draft/MTP A/B (professional unit).
# Operator-locked scope (2026-08-16): KV q8_0/q4_1/q4_0 only (no f16, no q5_1);
# context 98304/106496/118784 only; MTP ON through the sweep; draft-length
# ladder (2/4/6/8) as a manual Phase B (the framework has no draft factor).
# Phase C reports ONLY — no unit config changes without operator approval.
set -uo pipefail

MODEL="/home/james/kinver-hub/models/Nail-Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL.gguf"
OPT_DIR="/home/james/llama-optimize"
LLAMA_DIR="/home/james/llama.cpp/build-vulkan"
SERVER="$LLAMA_DIR/bin/llama-server"
RESULTS="results/nail-mtp"
LOG_DIR="${LOG_DIR:-/tmp/nail-mtp-sweep}"
mkdir -p "$LOG_DIR" "$OPT_DIR/$RESULTS" 2>/dev/null
LOG="$LOG_DIR/sweep.log"
PY="/home/james/kinver-hub/proxy/.venv/bin/python3"

log()  { echo "$(date +%H:%M:%S) $*" | tee -a "$LOG"; }
fail() { echo "FATAL: $*" | tee -a "$LOG"; exit 1; }

preflight() {
  log "== preflight =="
  [ -f "$MODEL" ] || fail "model missing: $MODEL"
  SIZE=$(stat -c%s "$MODEL" 2>/dev/null || echo 0)
  [ "$SIZE" -ge 22500000000 ] || fail "model too small (${SIZE} bytes; need >=22.5 GB)"
  log "model OK: $((SIZE / 1000000000)) GB"
  # GPU window: the proxy residency monitor reloads the professional and every
  # benchmark then contends or crashes. Refuse unless both are down.
  for pat in "llama-professional" "ai-proxy"; do
    if pgrep -f "$pat" >/dev/null 2>&1; then
      echo "NEEDS_SERVICE_STOP: '$pat' is running." | tee -a "$LOG"
      echo "Run: sudo systemctl stop ai-proxy llama-professional" | tee -a "$LOG"
      echo "(keep frontdesk up: port 13100, CPU-only, zero VRAM)" | tee -a "$LOG"
      exit 1
    fi
  done
  log "GPU window clear (no professional / ai-proxy running)"
  [ -d "$OPT_DIR" ] || fail "llama-optimize missing: $OPT_DIR"
  [ -x "$SERVER" ] || fail "llama-server missing: $SERVER"
  log "tooling OK"
}

# Phase A — llama-optimize narrow sweep (crash-safe/resumable). Flags verified
# against the tool's own parser: --factor overrides plan levels; --min-context/
# --max-context bound the depth axis; --driver server measures MTP (bench cannot).
phase_a() {
  log "== phase A: narrow sweep (scope locked) =="
  log "  kv_type=q8_0,q4_1,q4_0 | n_depth=98304,106496,118784 | mtp=1"
  log "  ngl=28,30,32,40 | ncmoe=20,30,40 | ubatch=512,1024 | batch=2048"
  log "  threads=8 | nkvo=0 (KV never pinned — hard-hang family excluded)"
  cd "$OPT_DIR" || fail "cd $OPT_DIR"
  python3 llama-optimize.py "$MODEL" \
    --llama-cpp "$LLAMA_DIR" \
    --min-kv q4_0 \
    --min-context 98304 --max-context 118784 \
    --factor kv_type=q8_0,q4_1,q4_0 \
    --factor n_depth=98304,106496,118784 \
    --factor ngl=28,30,32,40 \
    --factor ncmoe=20,30,40 \
    --factor ubatch=512,1024 \
    --factor batch=2048 \
    --factor threads=8 \
    --factor nkvo=0 \
    --factor mtp=1 \
    --driver server \
    --results-dir "$RESULTS" \
    --run --resume \
    > "$LOG_DIR/optimize.out" 2>&1
  echo "== phase A exit: $? $(date) ==" | tee -a "$LOG"
}

# Winner = the OK row at the deepest n_depth with the highest tg_tps.
pick_winner() {
  local csv="$OPT_DIR/$RESULTS/results.csv"
  [ -f "$csv" ] || { echo ""; return 1; }
  "$PY" - "$csv" <<'PY'
import csv, json, sys
rows = list(csv.DictReader(open(sys.argv[1])))
ok = [r for r in rows if r.get("status") == "OK"]
if not ok:
    print(json.dumps(None)); raise SystemExit
best = max(ok, key=lambda r: (int(r.get("n_depth") or 0), float(r.get("tg_tps") or 0)))
keep = {k: best[k] for k in ("ngl","ncmoe","ubatch","batch","threads","kv_type","n_depth","poll") if k in best}
print(json.dumps(keep))
PY
}

# Phase B — draft-length A/B on the winner (server driver; speculative
# acceptance is content-dependent, so BOTH a repetitive and a prose prompt).
# Reference base = the config the MTP file is known to fit (ngl 30, ncmoe 20,
# q8_0 KV, ctx 98304, ub 512) until a sweep winner exists.
PORT=13109
AB_REP="Repeat the phrase: the quick brown fox jumps over the lazy dog. Then write PONG and stop."
AB_PROSE="Explain the difference between a queue and a stack in computing, with a concrete example of each, in under 150 words."
AB_PID=""

ab_stop() {
  if [ -n "${AB_PID:-}" ]; then
    kill "$AB_PID" 2>/dev/null || true
    wait "$AB_PID" 2>/dev/null || true
    AB_PID=""
  fi
}

trap 'ab_stop' EXIT
trap 'exit 130' INT TERM

ab_start() { # $@ = base server flags + case-specific extra flags
  ab_stop
  "$SERVER" -m "$MODEL" "$@" -t 8 -C 0x1FE -tb 15 -Cb 0xFFFE -np 1 --no-mmap --cpu-strict \
    --host 127.0.0.1 --port "$PORT" --jinja \
    > "$LOG_DIR/server-ab.log" 2>&1 &
  AB_PID=$!
  for _ in $(seq 1 90); do
    curl -s -m 2 "http://127.0.0.1:$PORT/props" 2>/dev/null | grep -q '"model"' && return 0
    sleep 2
  done
  fail "server did not load in 180s (see $LOG_DIR/server-ab.log)"
}

ab_measure() { # $1 = label, remaining args = extra flags
  local label="$1"
  shift
  local -a extra_flags=("$@")
  ab_start "${AB_BASE[@]}" "${extra_flags[@]}"
  for kind in rep prose; do
    local prompt="$AB_REP"; [ "$kind" = prose ] && prompt="$AB_PROSE"
    local json=$(mktemp)
    "$PY" -c 'import json,sys; print(json.dumps({"prompt": sys.argv[1], "n_predict": 128, "temperature": 0, "stream": false}))' "$prompt" > "$json"
    local t0 t1 out
    t0=$(date +%s%N)
    out=$(curl -s -m 300 -X POST "http://127.0.0.1:$PORT/completion" -H "Content-Type: application/json" -d @"$json")
    t1=$(date +%s%N)
    local secs toks tps
    secs=$("$PY" -c "print(($t1-$t0)/1e9)")
    toks=$(printf '%s' "$out" | "$PY" -c 'import json,sys
try: print(json.load(sys.stdin).get("timings",{}).get("predicted_n",0))
except Exception: print(0)')
    tps=$("$PY" -c 'import sys
toks = float(sys.argv[1]); secs = float(sys.argv[2])
print(f"{toks / max(1.0, secs):.2f}")' "$toks" "$secs")
    log "A/B [$label/$kind]: ${tps} t/s (${toks} tok in ${secs}s)"
    rm -f "$json"
  done
}

base_flags_from_winner() {
  local json="$1"
  "$PY" -c 'import json,sys
d = json.loads(sys.argv[1])
if not d: print("")
m = {"ngl": "-ngl", "ncmoe": "-ncmoe", "ubatch": "-ub", "batch": "-b",
     "threads": "-t", "kv_type": "-ctk", "n_depth": "-c", "poll": "--poll"}
out = []
for k in ("ngl", "ncmoe", "ubatch", "batch", "threads", "kv_type", "n_depth"):
    if k in d: out += [m[k], d[k]]
if "kv_type" in d: out += ["-ctv", d["kv_type"]]
if "poll" not in d: out += ["--poll", "50"]
print(" ".join(out))' "$json"
}

phase_b() {
  log "== phase B: draft-length A/B (draft off vs n-max 2/4/6/8) =="
  local wjson flags
  wjson=$(pick_winner)
  flags=$(base_flags_from_winner "$wjson")
  if [ -n "$flags" ]; then
    log "sweep winner flags: $flags"
    read -ra AB_BASE <<< "$flags"
  else
    AB_BASE=( -ngl 30 -ncmoe 20 -c 98304 -ctk q8_0 -ctv q8_0 -ub 512 -b 2048 --poll 50 )
    log "no winner yet — using reference base: ${AB_BASE[*]}"
  fi

  ab_measure "draft-off"
  for nmax in 2 4 6 8; do
    ab_measure "n-max-$nmax" --spec-type draft-mtp --spec-draft-n-max "$nmax" --spec-draft-p-min 0.6
  done
  ab_stop
  log "A/B complete — table above"
}

phase_c() {
  log "== phase C: report (no unit changes) =="
  log "Apply ONLY after operator approval via /home/james/kinver-hub/bin/apply-sweep-params.sh"
  log "(backs up to .bak-presweep; --revert restores). Stock-vs-MTP swap is cutover-nail.sh — separate."
}

main() {
  preflight
  phase_a
  phase_b
  phase_c
  log "done. full sweep output: $LOG_DIR/optimize.out"
}

main "$@"
