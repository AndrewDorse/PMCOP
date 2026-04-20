#!/usr/bin/env sh
set -e

# Build watch_wallets.json from env (COPY_WALLET_1/2/3 and optional WATCHED_WALLETS CSV).
python << 'PY'
import json
import os
from pathlib import Path

state_dir = Path(os.environ.get("STATE_DIR", "./state"))
state_dir.mkdir(parents=True, exist_ok=True)

watch_path = Path(os.environ.get("WATCHLIST_FILE", str(state_dir / "watch_wallets.json")))
watch_path.parent.mkdir(parents=True, exist_ok=True)

wallets: list[str] = []
for key in ("COPY_WALLET_1", "COPY_WALLET_2", "COPY_WALLET_3"):
    v = os.environ.get(key, "").strip()
    if v:
        wallets.append(v.lower())

extra = os.environ.get("WATCHED_WALLETS", "").strip()
if extra:
    for part in extra.split(","):
        part = part.strip()
        if part:
            wallets.append(part.lower())

wallets = sorted(set(wallets))
with open(watch_path, "w", encoding="utf-8") as f:
    json.dump({"wallets": wallets}, f, indent=2)
    f.write("\n")

if not wallets:
    print(
        "polymarket-copy-bot: warning — no wallets in COPY_WALLET_1/2/3 or WATCHED_WALLETS; "
        "add-wallet / sync will fail until you set them.",
        flush=True,
    )
PY

exec "$@"
