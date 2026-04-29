# Battery & System Monitor

A small FastAPI service that watches the laptop battery, posts state changes to a Discord webhook, and exposes a live system-stats dashboard on port 5002.

## Features

- Battery polling via `psutil` with configurable interval
- Discord webhook alerts on:
  - Plug / unplug transitions
  - Discharge threshold crossings (default: 50, 30, 20, 10, 5%)
  - Charge threshold crossings (default: 80, 100%)
  - Optional startup notification
- Web dashboard with battery, CPU, memory, disk, swap, host, and uptime
- Recent-events feed (last 50 events)
- All settings editable from the dashboard and persisted to `config.json`
- Manual "Test webhook" button

## Requirements

- Python 3.10+
- A Discord webhook URL (optional — the dashboard works without it)

## Install

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

## Run

```bash
python monitor.py
```

Then open <http://localhost:5002>.

On first run, `config.json` is created next to `monitor.py` with default values. Edit it from the **Settings** card on the dashboard, or by editing the file directly. The Discord webhook URL is read from `DISCORD_WEBHOOK_URL` (env / `.env`) only on first run, after which `config.json` is the source of truth.

## Configuration

| Key | Type | Default | Notes |
|---|---|---|---|
| `discord_webhook_url` | string | `""` | Empty disables Discord posts |
| `username` | string | `"Battery Monitor"` | Webhook display name |
| `poll_interval_seconds` | int | `15` | 5–3600 |
| `discharge_thresholds` | int[] | `[50,30,20,10,5]` | Alert when battery ≤ value while unplugged |
| `charge_thresholds` | int[] | `[80,100]` | Alert when battery ≥ value while plugged |
| `alert_on_startup` | bool | `true` | Send a Discord message when the monitor starts |
| `host` | string | `"0.0.0.0"` | Restart required |
| `port` | int | `5002` | Restart required |

`config.json` is in `.gitignore` because it may contain a webhook URL.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Dashboard |
| GET | `/api/stats` | Battery + system stats + recent events (JSON) |
| GET | `/api/config` | Current config + defaults |
| POST | `/api/config` | Patch config (JSON body) |
| POST | `/api/config/reset` | Reset config to defaults |
| POST | `/api/test-webhook` | Send a test Discord message |

## Project layout

```
monitor.py          # FastAPI app + battery monitor loop
web/index.html      # Dashboard
config.json         # Generated on first run (gitignored)
requirements.txt
```
