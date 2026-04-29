import asyncio
import json
import os
import platform
import socket
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psutil
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

load_dotenv()

CONFIG_PATH = Path(__file__).parent / "config.json"
INDEX_HTML = Path(__file__).parent / "web" / "index.html"

DEFAULTS = {
    "discord_webhook_url": "",
    "poll_interval_seconds": 15,
    "host": "0.0.0.0",
    "port": 5002,
    # Percentage thresholds we alert on when crossed
    # (descending while discharging, ascending while charging).
    "discharge_thresholds": [50, 30, 20, 10, 5],
    "charge_thresholds": [80, 100],
    "username": "Battery Monitor",
    "alert_on_startup": True,
}

# These keys take effect only after restart (uvicorn binds them at startup).
RESTART_REQUIRED_KEYS = {"host", "port"}


def _seed_from_env(cfg: dict) -> dict:
    """First-run only: pull values from environment so existing .env users migrate cleanly."""
    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if webhook:
        cfg["discord_webhook_url"] = webhook.strip()
    poll = os.getenv("POLL_INTERVAL_SECONDS")
    if poll:
        try:
            cfg["poll_interval_seconds"] = int(poll)
        except ValueError:
            pass
    host = os.getenv("HOST")
    if host:
        cfg["host"] = host
    port = os.getenv("PORT")
    if port:
        try:
            cfg["port"] = int(port)
        except ValueError:
            pass
    return cfg


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg = {**DEFAULTS, **data}
            # Persist any newly-introduced default keys so the file stays complete.
            if data.keys() != cfg.keys():
                save_config(cfg)
            return cfg
        except (OSError, json.JSONDecodeError) as e:
            print(f"[config] failed to read {CONFIG_PATH}: {e}; using defaults")
            return dict(DEFAULTS)
    cfg = _seed_from_env(dict(DEFAULTS))
    save_config(cfg)
    print(f"[config] created {CONFIG_PATH}")
    return cfg


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def _validate_thresholds(value, name: str) -> list[int]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list of integers 0-100")
    cleaned = []
    for v in value:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"{name} contains non-integer value: {v!r}")
        if not 0 <= iv <= 100:
            raise ValueError(f"{name} value {iv} outside 0-100")
        cleaned.append(iv)
    return cleaned


def validate_and_merge(current: dict, patch: dict) -> dict:
    merged = dict(current)
    for k, v in patch.items():
        if k not in DEFAULTS:
            continue  # silently ignore unknown keys
        if k == "discord_webhook_url":
            if v is None:
                v = ""
            if not isinstance(v, str):
                raise ValueError("discord_webhook_url must be a string")
            merged[k] = v.strip()
        elif k == "poll_interval_seconds":
            try:
                iv = int(v)
            except (TypeError, ValueError):
                raise ValueError("poll_interval_seconds must be an integer")
            if not 5 <= iv <= 3600:
                raise ValueError("poll_interval_seconds must be between 5 and 3600")
            merged[k] = iv
        elif k == "host":
            if not isinstance(v, str) or not v.strip():
                raise ValueError("host must be a non-empty string")
            merged[k] = v.strip()
        elif k == "port":
            try:
                iv = int(v)
            except (TypeError, ValueError):
                raise ValueError("port must be an integer")
            if not 1 <= iv <= 65535:
                raise ValueError("port must be between 1 and 65535")
            merged[k] = iv
        elif k == "discharge_thresholds":
            merged[k] = sorted(set(_validate_thresholds(v, k)), reverse=True)
        elif k == "charge_thresholds":
            merged[k] = sorted(set(_validate_thresholds(v, k)))
        elif k == "username":
            if not isinstance(v, str) or not v.strip():
                raise ValueError("username must be a non-empty string")
            merged[k] = v.strip()[:80]
        elif k == "alert_on_startup":
            merged[k] = bool(v)
    return merged


config = load_config()

BOOT_TIME = psutil.boot_time()
APP_START = time.time()

state = {
    "last_percent": None,
    "last_plugged": None,
    "last_alerted_low": None,    # lowest discharge threshold already alerted in current discharge cycle
    "last_alerted_high": None,   # highest charge threshold already alerted in current charge cycle
    "last_event": None,
    "events": [],                # rolling list of recent events (newest first)
}

EVENT_LIMIT = 50


def get_battery():
    b = psutil.sensors_battery()
    if b is None:
        return None
    secsleft = b.secsleft
    if secsleft == psutil.POWER_TIME_UNLIMITED:
        time_left = "unlimited"
    elif secsleft == psutil.POWER_TIME_UNKNOWN or secsleft is None or secsleft < 0:
        time_left = "unknown"
    else:
        h, rem = divmod(int(secsleft), 3600)
        m, s = divmod(rem, 60)
        time_left = f"{h:d}h {m:02d}m"
    return {
        "percent": round(b.percent, 1),
        "plugged": bool(b.power_plugged),
        "time_left": time_left,
        "secsleft": secsleft if isinstance(secsleft, int) else None,
    }


def get_system_stats():
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    du = psutil.disk_usage(os.path.abspath(os.sep))
    cpu_freq = psutil.cpu_freq()
    load_avg = None
    if hasattr(psutil, "getloadavg"):
        try:
            load_avg = psutil.getloadavg()
        except OSError:
            load_avg = None
    uptime = int(time.time() - BOOT_TIME)
    app_uptime = int(time.time() - APP_START)
    return {
        "host": socket.gethostname(),
        "platform": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpu_count": psutil.cpu_count(logical=True),
        "cpu_freq_mhz": round(cpu_freq.current, 0) if cpu_freq else None,
        "load_avg": load_avg,
        "memory": {
            "total": vm.total,
            "used": vm.used,
            "available": vm.available,
            "percent": vm.percent,
        },
        "swap": {
            "total": sm.total,
            "used": sm.used,
            "percent": sm.percent,
        },
        "disk": {
            "path": os.path.abspath(os.sep),
            "total": du.total,
            "used": du.used,
            "free": du.free,
            "percent": du.percent,
        },
        "uptime_seconds": uptime,
        "app_uptime_seconds": app_uptime,
        "now": datetime.now(timezone.utc).isoformat(),
    }


def record_event(kind: str, message: str, battery: dict | None):
    evt = {
        "kind": kind,
        "message": message,
        "battery": battery,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    state["last_event"] = evt
    state["events"].insert(0, evt)
    del state["events"][EVENT_LIMIT:]


async def send_discord(message: str, battery: dict | None):
    url = config.get("discord_webhook_url", "").strip()
    if not url:
        return
    color = 0x2ecc71 if battery and battery["plugged"] else 0xe67e22
    if battery and not battery["plugged"] and battery["percent"] <= 10:
        color = 0xe74c3c
    embed = {
        "title": "Battery Monitor",
        "description": message,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": [],
    }
    if battery is not None:
        embed["fields"] = [
            {"name": "Charge", "value": f"{battery['percent']}%", "inline": True},
            {"name": "Power", "value": "Plugged" if battery["plugged"] else "On battery", "inline": True},
            {"name": "Time left", "value": battery["time_left"], "inline": True},
        ]
    embed["footer"] = {"text": socket.gethostname()}
    payload = {"username": config.get("username", "Battery Monitor"), "embeds": [embed]}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload)
            if r.status_code >= 300:
                print(f"[discord] non-2xx response {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[discord] post failed: {e}")


def detect_threshold_events(prev_pct, prev_plugged, b):
    """Return list of (kind, message) events to emit based on the new sample."""
    events = []
    pct = b["percent"]
    plugged = b["plugged"]
    discharge_thresholds = sorted(config.get("discharge_thresholds", []), reverse=True)
    charge_thresholds = sorted(config.get("charge_thresholds", []))

    # Plug/unplug transition.
    if prev_plugged is not None and plugged != prev_plugged:
        if plugged:
            events.append(("plugged", f"Charger connected at {pct}%"))
            state["last_alerted_low"] = None
        else:
            events.append(("unplugged", f"Running on battery at {pct}%"))
            state["last_alerted_high"] = None

    # Full charge.
    if plugged and pct >= 100 and (prev_pct is None or prev_pct < 100):
        events.append(("full", "Battery fully charged (100%)"))

    # Discharge thresholds: alert when we drop to/below an unalerted level.
    if not plugged:
        for t in discharge_thresholds:
            if pct <= t and (state["last_alerted_low"] is None or t < state["last_alerted_low"]):
                events.append(("low", f"Battery at {pct}% (≤{t}%)"))
                state["last_alerted_low"] = t
                break

    # Charge thresholds while plugged.
    if plugged:
        for t in charge_thresholds:
            if pct >= t and (state["last_alerted_high"] is None or t > state["last_alerted_high"]):
                events.append(("charge", f"Battery charged to {pct}% (≥{t}%)"))
                state["last_alerted_high"] = t

    return events


async def battery_monitor_loop():
    print(f"[monitor] starting, poll every {config['poll_interval_seconds']}s, webhook configured: {bool(config.get('discord_webhook_url'))}")
    first = True
    while True:
        try:
            b = get_battery()
            if b is None:
                if first:
                    print("[monitor] no battery detected on this system; loop will idle")
                    first = False
                await asyncio.sleep(config["poll_interval_seconds"])
                continue

            prev_pct = state["last_percent"]
            prev_plugged = state["last_plugged"]

            if first:
                msg = f"Monitor started. Battery {b['percent']}%, {'plugged' if b['plugged'] else 'on battery'}."
                record_event("startup", msg, b)
                if config.get("alert_on_startup", True):
                    await send_discord(msg, b)
                first = False
            else:
                events = detect_threshold_events(prev_pct, prev_plugged, b)
                for kind, msg in events:
                    record_event(kind, msg, b)
                    await send_discord(msg, b)

            state["last_percent"] = b["percent"]
            state["last_plugged"] = b["plugged"]
        except Exception as e:
            print(f"[monitor] loop error: {e}")
        await asyncio.sleep(config["poll_interval_seconds"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Prime CPU percent so the first reading is meaningful.
    psutil.cpu_percent(interval=None)
    task = asyncio.create_task(battery_monitor_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Battery & System Monitor", lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(INDEX_HTML)


@app.get("/api/stats")
async def api_stats():
    return JSONResponse(
        {
            "battery": get_battery(),
            "system": get_system_stats(),
            "events": state["events"],
            "webhook_configured": bool(config.get("discord_webhook_url")),
            "poll_interval_seconds": config["poll_interval_seconds"],
        }
    )


@app.get("/api/config")
async def api_get_config():
    return {
        "config": config,
        "defaults": DEFAULTS,
        "restart_required_keys": sorted(RESTART_REQUIRED_KEYS),
        "config_path": str(CONFIG_PATH),
    }


@app.post("/api/config")
async def api_set_config(request: Request):
    try:
        patch = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=400)
    if not isinstance(patch, dict):
        return JSONResponse({"ok": False, "error": "body must be a JSON object"}, status_code=400)
    try:
        new_cfg = validate_and_merge(config, patch)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    restart_changed = [
        k for k in RESTART_REQUIRED_KEYS if config.get(k) != new_cfg.get(k)
    ]
    thresholds_changed = (
        config.get("discharge_thresholds") != new_cfg.get("discharge_thresholds")
        or config.get("charge_thresholds") != new_cfg.get("charge_thresholds")
    )

    config.clear()
    config.update(new_cfg)
    save_config(config)

    if thresholds_changed:
        # Allow new thresholds to fire on next sample.
        state["last_alerted_low"] = None
        state["last_alerted_high"] = None

    return {
        "ok": True,
        "config": config,
        "restart_required": bool(restart_changed),
        "restart_required_keys_changed": restart_changed,
    }


@app.post("/api/config/reset")
async def api_reset_config():
    config.clear()
    config.update(DEFAULTS)
    save_config(config)
    state["last_alerted_low"] = None
    state["last_alerted_high"] = None
    return {"ok": True, "config": config}


@app.post("/api/test-webhook")
async def test_webhook():
    if not config.get("discord_webhook_url"):
        return JSONResponse({"ok": False, "error": "webhook not configured"}, status_code=400)
    b = get_battery()
    await send_discord("Test message from Battery Monitor", b)
    record_event("test", "Manual webhook test sent", b)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("monitor:app", host=config["host"], port=config["port"], reload=False)
