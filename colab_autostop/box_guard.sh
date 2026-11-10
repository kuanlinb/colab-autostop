#!/usr/bin/env bash
# colab-autostop box guard: runs on the Colab VM next to your job and ends the VM by itself.
#
# Every POLL seconds it checks three things, in this order:
#   DONE  - a done marker exists (a file, or a regex in a log file)
#   STUCK - no progress for IDLE seconds. Progress means a file under the watched paths was written, or the GPU
#           or the CPU was busy.
#   CAP   - the hard cap since the guard started has passed
# On any of them it packs the result paths into results.tgz, uploads it and status.txt with presigned PUT URLs
# (no long-lived credential on the VM), then asks the Colab runtime manager to release the VM, the same request
# google.colab.runtime.unassign() sends. The laptop does not need to be on for any of this.
#
# Usage: box_guard.sh [ENV_FILE]
# Settings come from the environment, or from ENV_FILE (sourced) if given. Lists are space separated.
#   AUTOSTOP_DIR              guard state, logs and the packed archive (default /content/autostop; never watched,
#                             never packed except for guard.log)
#   AUTOSTOP_WATCH            paths whose file writes count as progress (default /content)
#   AUTOSTOP_IGNORE           paths under AUTOSTOP_WATCH that never count (default: Colab's own housekeeping dirs)
#   AUTOSTOP_IDLE_MIN         minutes without progress before STUCK (default 20; AUTOSTOP_IDLE_SEC overrides)
#   AUTOSTOP_CAP_MIN          hard cap in minutes (default 720; AUTOSTOP_CAP_SEC overrides)
#   AUTOSTOP_POLL_SEC         check interval (default 30)
#   AUTOSTOP_GPU_BUSY_PCT     GPU utilization at or above this counts as progress (default 5; 101 turns it off)
#   AUTOSTOP_JOB_PID_FILE     file holding the pid of the job's session leader (default AUTOSTOP_DIR/job.pid; the
#                             launcher writes it). Without it, CPU is not used as a progress signal.
#   AUTOSTOP_CPU_BUSY_PCT     CPU used by the job's own processes (its session), in % of one core averaged over a
#                             poll, at or above this counts as progress (default 30; "off" turns it off). Only the
#                             job counts: an idle Colab VM's own services use 17-29 % of a core with spikes above.
#   AUTOSTOP_DONE_FILE        DONE when any of these files exists; if one holds just a number, it is reported as
#                             the exit code
#   AUTOSTOP_DONE_LOG/_REGEX  DONE when the extended regex matches a line of this log file
#   AUTOSTOP_RESULTS          paths packed into results.tgz (the guard log is always added)
#   AUTOSTOP_PUT_RESULTS_URL_FILE / AUTOSTOP_PUT_STATUS_URL_FILE
#                             files holding the presigned PUT URLs (default AUTOSTOP_DIR/put_results.url and
#                             put_status.url); if neither exists, nothing is uploaded and the VM is still released
#   AUTOSTOP_UPLOAD_RETRIES   tries per upload (default 5), AUTOSTOP_RETRY_SLEEP_SEC between tries (default 20)
#   AUTOSTOP_FALLBACK_WAIT_MIN  if uploads fail, wait this long before releasing anyway, so someone can still
#                             `colab download` the archive (default 45; AUTOSTOP_FALLBACK_WAIT_SEC overrides)
#   AUTOSTOP_RUNTIME_ADDR     runtime manager address (default: $TBE_RUNTIME_ADDR, set by Colab)
#   AUTOSTOP_RELEASE_TRIES    release attempts before giving up (default 30), AUTOSTOP_RELEASE_RETRY_SEC apart
#                             (default 60)
#   AUTOSTOP_PROBE_TIMEOUT_SEC  time limit for each nvidia-smi and find call (default 60)
set -u
if [ -n "${1:-}" ]; then
  # shellcheck disable=SC1090
  . "$1" || { echo "box_guard: cannot read $1" >&2; exit 1; }
fi

DIR=${AUTOSTOP_DIR:-/content/autostop}
WATCH=${AUTOSTOP_WATCH:-/content}
IGNORE=${AUTOSTOP_IGNORE:-/content/.config /content/drive /content/sample_data}
IDLE_SEC=${AUTOSTOP_IDLE_SEC:-$(( ${AUTOSTOP_IDLE_MIN:-20} * 60 ))}
CAP_SEC=${AUTOSTOP_CAP_SEC:-$(( ${AUTOSTOP_CAP_MIN:-720} * 60 ))}
POLL=${AUTOSTOP_POLL_SEC:-30}
GPU_BUSY=${AUTOSTOP_GPU_BUSY_PCT:-5}
CPU_BUSY=${AUTOSTOP_CPU_BUSY_PCT:-30}
PROC_DIR=${AUTOSTOP_PROC_DIR:-/proc}
CLK_TCK=${AUTOSTOP_CLK_TCK:-$(getconf CLK_TCK 2>/dev/null || echo 100)}
DONE_FILES=${AUTOSTOP_DONE_FILE:-}
DONE_LOG=${AUTOSTOP_DONE_LOG:-}
DONE_REGEX=${AUTOSTOP_DONE_REGEX:-}
RESULTS=${AUTOSTOP_RESULTS:-}
PUT_RESULTS=${AUTOSTOP_PUT_RESULTS_URL_FILE:-$DIR/put_results.url}
PUT_STATUS=${AUTOSTOP_PUT_STATUS_URL_FILE:-$DIR/put_status.url}
RETRIES=${AUTOSTOP_UPLOAD_RETRIES:-5}
RETRY_SLEEP=${AUTOSTOP_RETRY_SLEEP_SEC:-20}
FALLBACK_SEC=${AUTOSTOP_FALLBACK_WAIT_SEC:-$(( ${AUTOSTOP_FALLBACK_WAIT_MIN:-45} * 60 ))}
RUNTIME_ADDR=${AUTOSTOP_RUNTIME_ADDR:-${TBE_RUNTIME_ADDR:-}}
RELEASE_TRIES=${AUTOSTOP_RELEASE_TRIES:-30}
RELEASE_RETRY_SEC=${AUTOSTOP_RELEASE_RETRY_SEC:-60}
PROBE_SEC=${AUTOSTOP_PROBE_TIMEOUT_SEC:-60}

mkdir -p "$DIR" || exit 1
LOG=$DIR/guard.log
ARCHIVE=$DIR/results.tgz
STATUS=$DIR/status.txt
say() { echo "[$(date -u +%FT%TZ)] $*" >> "$LOG"; }
# Every probe gets a time limit: a wedged GPU can hang nvidia-smi and a network mount can hang find, and one hung
# probe would freeze the loop before STUCK or CAP could fire.
limit() { local s=$1; shift; if command -v timeout > /dev/null; then timeout "$s" "$@"; else "$@"; fi; }

release() {  # release <why>; never returns. Keeps trying for a while: a VM left up bills by the hour.
  local i code
  say "releasing VM ($1)"
  for i in $(seq 1 "$RELEASE_TRIES"); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -X POST "http://$RUNTIME_ADDR/unassign")
    say "unassign http $code (try $i of $RELEASE_TRIES)"
    case $code in 2??) exit 0 ;; esac
    if [ "$i" = 1 ] && command -v python3 > /dev/null; then  # Colab's own wrapper around the same request
      if limit 60 python3 -c "from google.colab import runtime; runtime.unassign()" >> "$LOG" 2>&1; then
        say "runtime.unassign() returned"; exit 0
      fi
    fi
    [ "$i" -lt "$RELEASE_TRIES" ] && sleep "$RELEASE_RETRY_SEC"
  done
  say "could not release the VM; it stays up until Colab reclaims it"
  exit 2
}

put() {  # put <file> <url-file>; gives up on a stalled transfer, not on a slow one
  local i code
  for i in $(seq 1 "$RETRIES"); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --speed-limit 10000 --speed-time 300 --max-time 43200 \
           -T "$1" "$(cat "$2")")
    case $code in 2??) return 0 ;; esac
    say "upload of $(basename "$1") failed (http $code), try $i of $RETRIES"
    [ "$i" -lt "$RETRIES" ] && sleep "$RETRY_SLEEP"
  done
  return 1
}

pack() {  # results that exist + the guard log; a tar warning (file changed while read) still leaves an archive
  local p have=()
  for p in $RESULTS; do [ -e "$p" ] && have+=("$p"); done
  # Never pack the guard dir itself (upload URLs, the archive being written); guard.log goes in at the top level.
  # Write through stdout: GNU tar reads "C:/x" in -f as a remote host, and stdout works with every tar.
  limit 3600 tar czf - --exclude="$DIR" ${have[@]+"${have[@]}"} -C "$DIR" guard.log 2>> "$DIR/tar.err" > "$ARCHIVE"
  [ -s "$ARCHIVE" ] || limit 600 tar czf - -C "$DIR" guard.log 2>> "$DIR/tar.err" > "$ARCHIVE"
}

finish() {  # finish <STATE> <reason>; never returns
  local state=$1 reason=$2 f code
  for f in $DONE_FILES; do  # an exit code is reported only from a marker that holds just a number
    [ -f "$f" ] || continue
    code=$(tr -d ' \t\r\n' < "$f")
    if [[ $code =~ ^-?[0-9]+$ ]]; then reason="$reason, exit code $code"; break; fi
  done
  say "$state: $reason"
  pack
  echo "$state $(date -u +%FT%TZ) $reason" > "$STATUS"
  if [ ! -f "$PUT_RESULTS" ] && [ ! -f "$PUT_STATUS" ]; then
    release "$state, no upload configured"
  fi
  if [ ! -s "$ARCHIVE" ]; then
    say "$state: could not build the archive (see tar.err); not uploading an empty one"
  elif put "$ARCHIVE" "$PUT_RESULTS" && put "$STATUS" "$PUT_STATUS"; then
    say "$state: results and status uploaded"
    release "$state, uploaded"
  fi
  say "$state: upload failed; keeping the VM $FALLBACK_SEC s for a manual download, then releasing"
  sleep "$FALLBACK_SEC"
  release "$state, upload failed"
}

is_done() {
  local f
  for f in $DONE_FILES; do [ -f "$f" ] && return 0; done
  [ -n "$DONE_LOG" ] && [ -n "$DONE_REGEX" ] && grep -Eq -- "$DONE_REGEX" "$DONE_LOG" 2>/dev/null
}

gpu_util() {  # highest utilization over all GPUs; 0 without a GPU or when nvidia-smi hangs
  command -v nvidia-smi > /dev/null 2>&1 || { echo 0; return; }
  limit "$PROBE_SEC" nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null \
    | tr -dc '0-9\n' | sort -n | tail -n 1 | grep . || echo 0
}

JOB_PID_FILE=${AUTOSTOP_JOB_PID_FILE:-$DIR/job.pid}
job_ticks() {  # CPU ticks (user + system, own + reaped children) of every process in the job's session
  local sid
  sid=$(tr -dc '0-9' < "$JOB_PID_FILE" 2>/dev/null)
  [ -n "$sid" ] || return 1
  # /proc/<pid>/stat: after the last ") " the fields are state ppid pgrp session ... utime(12) stime cutime cstime
  cat "$PROC_DIR"/[0-9]*/stat 2>/dev/null \
    | awk -v sid="$sid" '{ sub(/^.*\) /, ""); if ($4 == sid) t += $12 + $13 + $14 + $15 } END { print t + 0 }'
}
cpu_prev_ticks=; cpu_prev_ns=
cpu_util() {  # sets cpu = the job's CPU use since the last call, in % of one core (no subshell: keeps the state)
  local ticks ns
  cpu=0
  [ "$CPU_BUSY" = off ] && return
  ticks=$(job_ticks) || { cpu_prev_ticks=; return; }
  ns=$(date +%s%N)
  if [ -n "$cpu_prev_ticks" ] && [ "$ns" -gt "$cpu_prev_ns" ] && [ "$ticks" -ge "$cpu_prev_ticks" ]; then
    cpu=$(( (ticks - cpu_prev_ticks) * 100 * 1000000000 / (CLK_TCK * (ns - cpu_prev_ns)) ))
  fi
  cpu_prev_ticks=$ticks; cpu_prev_ns=$ns
}

# find arguments: prune the guard's own dir and the ignored paths, then look for any file newer than the stamp
PRUNE=(-path "$DIR")
for p in $IGNORE; do PRUNE+=(-o -path "$p"); done
moved_since() {  # moved_since <stamp>: prints one changed file, if any
  # shellcheck disable=SC2086
  limit "$PROBE_SEC" find $WATCH \( "${PRUNE[@]}" \) -prune -o -type f -newer "$1" -print -quit 2>/dev/null
}

[ -n "$RUNTIME_ADDR" ] || { say "TBE_RUNTIME_ADDR is not set (not a Colab VM?); guard not armed"; exit 1; }
command -v curl > /dev/null || { say "curl not found; guard not armed"; exit 1; }
echo $$ > "$DIR/guard.pid"
say "armed: idle ${IDLE_SEC}s, cap ${CAP_SEC}s, poll ${POLL}s, gpu busy ${GPU_BUSY}%, cpu busy ${CPU_BUSY}%," \
    "watch [$WATCH], done files [$DONE_FILES]"

start=$SECONDS; last_move=$SECONDS
touch "$DIR/.stamp"
while true; do
  sleep "$POLL"
  if is_done; then finish DONE "done marker found"; fi
  touch "$DIR/.stamp.next"
  moved=$(moved_since "$DIR/.stamp")
  mv -f "$DIR/.stamp.next" "$DIR/.stamp"
  gpu=$(gpu_util)
  cpu_util
  polls=$((${polls:-0} + 1))
  if [ $((polls % 10)) = 1 ]; then  # a reading now and then, to tune the thresholds afterwards
    say "poll $polls: file written: $([ -n "$moved" ] && echo yes || echo no), gpu ${gpu}%," \
        "cpu ${cpu}% of one core, no progress for $((SECONDS - last_move))s"
  fi
  if [ -n "$moved" ] || [ "${gpu:-0}" -ge "$GPU_BUSY" ]; then
    last_move=$SECONDS
  elif [ "$CPU_BUSY" != off ] && [ "${cpu:-0}" -ge "$CPU_BUSY" ]; then
    last_move=$SECONDS
  fi
  if [ $((SECONDS - last_move)) -ge "$IDLE_SEC" ]; then
    finish STUCK "no file written, GPU and CPU idle for ${IDLE_SEC}s"
  fi
  if [ $((SECONDS - start)) -ge "$CAP_SEC" ]; then finish CAP "hard cap of ${CAP_SEC}s reached"; fi
done
