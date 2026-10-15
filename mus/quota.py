"""Spread calls over several Gemini API keys: count calls locally and move to the next key before one runs out."""
import hashlib
import json
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

STATE = Path.home() / ".local/share/mus/quota.json"
# Google publishes no free-tier numbers for every model, so these are guesses until a key's first 429 reports the
# real ones. A key past a guessed limit is still used once every key is past it, so a low guess wastes nothing.
GUESS = {"minute": 10, "day": 250}
PACIFIC = ZoneInfo("America/Los_Angeles")  # free-tier daily quotas reset at midnight Pacific
_lock = threading.Lock()  # ponytail: per process; the CLI's in-process fallback and the daemon can race on the file


def _id(key):
    return hashlib.sha256(key.encode()).hexdigest()[:12]  # never write the key itself to disk


def _today():
    return datetime.now(PACIFIC).strftime("%Y-%m-%d")


def _load():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _entry(state, key):
    e = state.setdefault("keys", {}).setdefault(_id(key), {})
    if e.get("day") != _today():
        e.update(day=_today(), n=0)
    e["recent"] = [t for t in e.get("recent", []) if time.time() - t < 60]
    return e


def take(keys, record=True):
    """The first key with room under both limits; else the least used key that isn't cooling down."""
    with _lock:
        state = _load()
        lim = {**GUESS, **state.get("limits", {})}
        now = time.time()
        entries = [(k, _entry(state, k)) for k in keys]
        live = [(k, e) for k, e in entries if e.get("until", 0) <= now]
        roomy = [(k, e) for k, e in live if e["n"] < lim["day"] and len(e["recent"]) < lim["minute"]]
        key, e = (roomy[0] if roomy else min(live, key=lambda ke: ke[1]["n"]) if live
                  else min(entries, key=lambda ke: ke[1]["until"]))
        if record:
            e["n"] += 1
            e["recent"].append(now)
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps(state))
        return key


def exhausted(key, details):
    """Bench a key after a 429 and learn the real limit from Google's QuotaFailure details."""
    text = json.dumps(details or {})
    per_day = "PerDay" in text
    with _lock:
        state = _load()
        if m := re.search(r'"quotaValue": "(\d+)"', text):
            state.setdefault("limits", {})["day" if per_day else "minute"] = int(m[1])
        if per_day:
            midnight = (datetime.now(PACIFIC) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            until = midnight.timestamp()
        else:
            m = re.search(r'"retryDelay": "(\d+)', text)
            until = time.time() + (int(m[1]) if m else 60)
        _entry(state, key)["until"] = until
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state))
