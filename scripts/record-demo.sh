#!/usr/bin/env bash
# Get the local app ready for recording the demo video:
# load keys, start the app with speech input, warm up Parakeet with a synthetic take,
# check that the take gives all four verdicts, then open the browser.
#
#   scripts/record-demo.sh                     # keys from .env (see .env.example)
#   ENV_FILE=path/to/.env scripts/record-demo.sh
#
# Stop the app with Ctrl-C. The warm-up check costs about 10 Tavily credits.
set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${PORT:-8765}"
ENV_FILE="${ENV_FILE:-.env}"
WORK="$(mktemp -d)"
TAKE="Good morning everyone. Pluto is our ninth planet. You can see the Great Wall of China from space with the naked eye. Octopuses have three hearts. And since our launch, our users tripled."

cleanup() { [[ -n "${SERVER:-}" ]] && kill "$SERVER" 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT

# 1. Memory: Parakeet needs free RAM, on an 8 GB Mac it slows from 5 s to a minute when apps compete.
free=$(memory_pressure 2>/dev/null | awk -F': ' '/free percentage/ {print $2+0}')
echo "Free memory: ${free:-?}%"
if pgrep -xq "Brave Browser" || pgrep -xq "Google Chrome" || pgrep -xq "Slack"; then
  echo "  ! Brave, Chrome or Slack is running. Quit them for a faster transcription."
fi

# 2. Keys
if [[ -z "${NEBIUS_API_KEY:-}" || -z "${TAVILY_API_KEY:-}" ]]; then
  [[ -f "$ENV_FILE" ]] || { echo "No keys: set NEBIUS_API_KEY and TAVILY_API_KEY, or ENV_FILE=<path to .env>"; exit 1; }
  set -a; source "$ENV_FILE"; set +a
fi

# 3. App
pkill -f "uvicorn sayproof.app" 2>/dev/null || true
uv run uvicorn sayproof.app:app --port "$PORT" >"$WORK/server.log" 2>&1 &
SERVER=$!
for _ in $(seq 30); do curl -sf "localhost:$PORT/api/health" >/dev/null && break; sleep 1; done
health=$(curl -sf "localhost:$PORT/api/health") || { echo "The app did not start:"; cat "$WORK/server.log"; exit 1; }
echo "App: $health"
[[ "$health" == *'"speech":true'* ]] || { echo "Speech input is off. Run: uv sync --extra speech"; exit 1; }

# 4. Warm-up take: loads Parakeet (about a minute the first time) and checks the demo verdicts.
echo "Warm-up take (loads Parakeet, then checks the claims)..."
say -o "$WORK/take.wav" --data-format=LEI16@16000 "$TAKE"
started=$(date +%s)
curl -s -F "audio=@$WORK/take.wav" "localhost:$PORT/api/check-audio" >"$WORK/receipt.json"
python3 - "$WORK/receipt.json" "$(( $(date +%s) - started ))" <<'EOF'
import json, sys
r = json.load(open(sys.argv[1]))
if "claims" not in r:
    sys.exit(f"Warm-up failed: {r.get('detail', r)}")
print(f"  {sys.argv[2]} s in total (Parakeet {r['asr_seconds']} s, check {r['seconds']} s)")
for c in r["claims"]:
    print(f"  {c['time'] if c['time'] is not None else '?':>5} s  {c['verdict']:<13} {c['claim']}")
got = sorted(c["verdict"] for c in r["claims"])
want = sorted(["outdated", "contradicted", "supported", "unverified"])
print("  All four verdicts: OK" if got == want else f"  ! Expected {want}, got {got}. Run the take again before recording.")
EOF

[[ -n "${NO_OPEN:-}" ]] || open "http://localhost:$PORT"
cat <<EOF

Ready. Recording checklist:
  - Start the screen recording, press record in the app, say the demo take:
    "$TAKE"
  - Script and shot list: VIDEO.md
Ctrl-C stops the app.
EOF
wait "$SERVER"
