# Downloading PLUDOS Data from the Jetson

Jetson IP: `192.168.0.100`  
User: `warehouse1`  
Data directory: `~/PLUDOS/client/ram_buffer/`

---

## File types in the buffer

| Filename pattern | What it is |
|---|---|
| `YYYY-MM-DD.parquet` | Daily file — all shuttles, full day, created at midnight UTC |
| `mission_s{id}_{ms}.parquet` | Intra-day files — individual mission flushes from today, merged into the daily file at midnight |

---

## Download commands (run from your laptop)

```bash
# All files (daily + today's pending missions)
scp 'warehouse1@192.168.0.100:~/PLUDOS/client/ram_buffer/*.parquet' ./data/

# Only daily files (clean, one per day, all shuttles)
scp 'warehouse1@192.168.0.100:~/PLUDOS/client/ram_buffer/????-??-??.parquet' ./data/

# Specific day
scp warehouse1@192.168.0.100:~/PLUDOS/client/ram_buffer/2026-05-20.parquet ./data/

# Latest daily file only
ssh warehouse1@192.168.0.100 \
  "ls ~/PLUDOS/client/ram_buffer/????-??-??.parquet | sort | tail -1" \
  | xargs -I{} scp warehouse1@192.168.0.100:{} ./data/

# Sync everything new (rsync — skips files already downloaded)
rsync -avz --include='*.parquet' --exclude='*' \
  warehouse1@192.168.0.100:~/PLUDOS/client/ram_buffer/ ./data/
```

---

## Trigger daily consolidation manually (without waiting for midnight)

SSH into the Jetson and run a one-liner:

```bash
ssh warehouse1@192.168.0.100

# Inside the running container — consolidate today's mission files now
podman exec pludos-data-engine python3 -c "
import sys; sys.path.insert(0, '/app')
from datetime import datetime
import data_engine as de
de._consolidate_day(datetime.utcnow().strftime('%Y-%m-%d'))
"
```

Or the simpler option: restart the container (it consolidates stale files at startup):

```bash
ssh warehouse1@192.168.0.100
podman restart pludos-data-engine
```

---

## Read data in Python

```python
import pandas as pd, glob

# Load a specific day (all shuttles)
df = pd.read_parquet("data/2026-05-20.parquet")

# Load all days
frames = [pd.read_parquet(f) for f in sorted(glob.glob("data/????-??-??.parquet"))]
df = pd.concat(frames, ignore_index=True)

# Filter by shuttle
s1 = df[df["shuttle_id"] == 1]
s2 = df[df["shuttle_id"] == 2]

# Filter to MOVING only
moving = df[df["state"] == 1]

# Sort correctly (always use seq, not timestamp)
df.sort_values(["shuttle_id", "seq"], inplace=True)
```

---

## Weekly or monthly files

Daily files are already small enough to work with. If you want to consolidate
multiple days into a single file for analysis:

```python
import pandas as pd, glob

# Build a weekly file (May 13–19)
frames = [pd.read_parquet(f) for f in sorted(glob.glob("data/2026-05-1?.parquet"))]
week = pd.concat(frames, ignore_index=True)
week.sort_values(["shuttle_id", "seq"], inplace=True)
week.to_parquet("data/2026-week20.parquet", index=False)
```
