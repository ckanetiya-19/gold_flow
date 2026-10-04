# Goldflow — Consolidated Structure (iTick real-spot, no PAXG, no Port 9000)

**Final architecture (2026-09-29):** 4 self-contained ports, each in its own folder,
each running its OWN iTick real-spot feed (`itick_feed.py`). No shared Port 9000
engine, no PAXG. iTick allows 5 connections → 4 used, 1 spare.

---

## Folders & how to run
> Run each **from inside its own folder** (so it uses that folder's local
> `itick_feed.py` + strategy modules + DB files). Set the iTick key first.

### `quant_9060/`  — AI Quant / Institutional Dashboard  → port **9060**
```
cd quant_9060
set GOLDFLOW_9060_ITICK_KEY=<key>
python PORT_9060_V2_INSTITUTIONAL_DASHBOARD_V2.py
python PORT_9061_INSTITUTIONAL_FULLSCREEN_CHART.py   # chart mirror → port 9061 (reads 9060)
```

### `quant_9080/`  — AI Quant Terminal V2 (VWAP Sniper, auto-trade)  → port **9080**
```
cd quant_9080
set GOLDFLOW_9080_ITICK_KEY=<key>
python PORT_9080_AI_QUANT_TERMINAL_V2.py
python PORT_9081_QUANT_FULLSCREEN_CHART.py           # chart mirror → port 9081 (reads 9080)
```

### `zones_9100/`  — Order-Flow Zones Chart (auto-trade)  → port **9100**
```
cd zones_9100
set GOLDFLOW_9100_ITICK_KEY=<key>
python PORT_9100_ORDERFLOW_ZONES_CHART.py
```

### `spot_9900/`  — iTick Real-Spot Terminal + PAPER strategies  → port **9900**
```
cd spot_9900
set GOLDFLOW_9900_ITICK_KEY=<key>
python PORT_9900_ITICK_ORDERFLOW_TERMINAL.py
```

### `_archive/`  — retired ports (backup): 9050, 9070, 9090

---

## What changed in each converted port
- Feed source: **Port 9000 (PAXG relay) → own iTick connection** (`ITickFeed`), emitting
  the same `tick`/`depth`/`mark_price` messages the port already understood.
- **PAXG kline bootstrap disabled** (9060/9080). 9100/9900 warm up from their own SQLite.
- **QuestDB ingest OFF** by default (`GOLDFLOW_QUESTDB_ENABLED=1` to re-enable) — retired
  with Port 9000, so no more connection-refused error spam.
- Everything else (UI, strategies, MT5 wiring, DBs) unchanged.

## iTick key
- One account gives **5 connections**. You can use the **same key** in all four
  `GOLDFLOW_90X0_ITICK_KEY` vars (4 connections, 1 spare), or separate keys.
- Persist at User scope so restarts keep them (see the env-var restart discipline).

## Still pending (post first-live-test)
1. **DOM ladder:** iTick free depth is L1 only → 9060/9080 DOM shows best bid/ask, not a
   deep ladder. Best fix = port 9900's synthetic traded-volume ladder (verify live).
2. **SQLite bar warmup** for 9060/9080 (so the chart survives restart, like 9100/9900).
3. **Switchover:** stop the ROOT-folder originals (9000/9050/9060/9070/9080/9090/9061/9081/
   9100/9900), then run these folder versions. **Copy existing `*.db` files into each folder
   first** to preserve trade history / bars (scripts use script-relative DB paths).
4. Delete/archive the removed ports from root.

## Safety
MT5 real-money auto-execution is NOT wired by these changes and stays declined
(the MT5 terminal is on a REAL account). Paper / demo only. `9080`/`9100` MT5 env
flags exist from before — verify they point at demo before ever enabling.
