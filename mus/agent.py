"""The errand loop: memory first, then Gemini, which runs commands by writing a <run> block."""
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import context, memory, quota

MODEL = "gemini-3.5-flash-lite"  # Google's fastest model
THINKING = "minimal"
MAX_TURNS = 6
TIMEOUT = 120
OUT_LIMIT = 4000
HERE = Path(__file__).parent
SYSTEM = (HERE / "system.md").read_text()
ASKPASS = HERE / "askpass.sh"
# anything root or irreversible waits for the one-tap approval
GATED = re.compile(r"\b(sudo|pkexec|su|rm|shred|wipefs|mkfs(\.\w+)?|fdisk|parted)\b|\bdd\b[^\n]*\bof="
                   r"|>\s*/dev/(sd|nvme|vd|mmc)|\b(chmod|chown)\s+-R\b[^\n]*\s/(\s|$)")
BLOCKED = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}
# a plain-text protocol instead of a declared tool: on Flash-Lite a function declaration roughly doubled time to first token
RUN = re.compile(r"<run>[ \t]*\n?(.*?)</run>\s*(?:<done>(.*?)</done>)?", re.S)
HOLD = len("<run>")  # stream this many chars late so a half-arrived <run> or line-start § never reaches the screen


class NoKey(Exception):
    pass


def secret(kind):
    try:
        r = subprocess.run(["secret-tool", "lookup", "service", "mus", "key", kind], capture_output=True, text=True,
                           timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):  # no libsecret, or no keyring service answering
        return None
    return r.stdout.strip() or None


def api_keys():
    """Every Gemini key: GEMINI_API_KEY / GOOGLE_API_KEY (comma or space separated) or the keyring, one per line."""
    raw = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or secret("api") or ""
    return re.split(r"[\s,]+", raw.strip()) if raw.strip() else []


_clients = {}
_last_call = 0.0


def client(key=None):
    if key is None:
        if not (ks := api_keys()):
            raise NoKey
        key = ks[0]
    if key not in _clients:
        from google import genai  # ~0.4 s, so only paid when the API is actually needed
        _clients[key] = genai.Client(api_key=key)
    return _clients[key]


def config():
    from google.genai import types
    # The random tag makes every prompt a cache miss on purpose. Measured on 2026-10-15: Gemini's implicit cache hits
    # answered 1-2 s slower than misses, and free-tier keys get no caching discount to lose.
    return types.GenerateContentConfig(
        system_instruction=f"[{uuid.uuid4().hex[:8]}]\n" + SYSTEM + context.get(), max_output_tokens=8000,
        thinking_config=types.ThinkingConfig(thinking_level=THINKING))


def warm():
    """Open the HTTPS connection while the user is still typing; skipped if a call kept it open recently."""
    global _last_call
    if time.time() - _last_call < 240:
        return
    _last_call = time.time()
    try:
        client(quota.take(api_keys(), record=False)).models.get(model=MODEL)  # free metadata call, no tokens
    except Exception:  # warming is best effort; the real request reports real errors
        pass


def run_script(script, sudo_ok):
    env = dict(os.environ)
    pre = ""
    if sudo_ok:
        env["SUDO_ASKPASS"] = str(ASKPASS)
        pre = 'sudo() { command sudo -A "$@"; }\n'
    # a file, not a pipe: a GUI app that inherits stdout must not keep us waiting until the timeout
    with tempfile.TemporaryFile("w+") as f:
        try:
            rc = subprocess.run(["bash", "-c", pre + script], cwd=Path.home(), env=env, stdin=subprocess.DEVNULL,
                                stdout=f, stderr=subprocess.STDOUT, timeout=TIMEOUT).returncode
        except subprocess.TimeoutExpired:
            rc = 124
        f.seek(0)
        out = f.read()
    if rc == 124 and not out.endswith("\n"):
        out += f"\n(timed out after {TIMEOUT}s)"
    return rc, out if len(out) <= OUT_LIMIT else "...(truncated)\n" + out[-OUT_LIMIT:]


_approve_lock = threading.Lock()


def _exec(script, done, session, emit, approve):
    """Run one <run> block; returns whether it succeeded, the line to show, and what the model should hear."""
    script = re.sub(r"^```\w*\n|\n?```$", "", script.strip())  # models sometimes fence the script anyway
    if not script:
        return {"ok": False, "done": "", "feedback": "Your <run> block was empty."}
    emit({"t": "run", "cmd": script})
    if GATED.search(script):
        with _approve_lock:
            if session["approved"] is None:
                session["approved"] = bool(approve(script))
        if not session["approved"]:
            return {"ok": False, "done": "",
                    "feedback": "User declined to run this. Do not retry; reply with the commands for them to run themselves."}
    rc, out = run_script(script, session["approved"])
    done = (done or "").strip()
    return {"ok": rc == 0, "done": done.replace("{out}", out.strip()[:500]) if rc == 0 else "",
            "feedback": f"<output exit={rc}>\n{out or '(no output)'}\n</output>"}


def _clean(text):
    """Visible answer text: no <run> block, and no § at the start of any line (only the first one means anything)."""
    return re.sub(r"(^|\n)([ \t]*)§[ \t]?", r"\1\2", text.split("<run>")[0]).lstrip()


def _stream(c, contents, emit):
    """Stream one model turn. Returns its parts exactly as received (thought signatures must go back unchanged)."""
    parts, raw, sent, finish = [], "", 0, None
    for chunk in c.models.generate_content_stream(model=MODEL, contents=contents, config=config()):
        cand = (chunk.candidates or [None])[0]
        if cand is None:
            continue
        finish = cand.finish_reason or finish
        for p in (cand.content.parts or []) if cand.content else []:
            parts.append(p)
            if p.text and not p.thought:
                raw += p.text
        shown = _clean(raw if "<run>" in raw else raw[:max(0, len(raw) - HOLD)])
        if len(shown) > sent:
            emit({"t": "text", "v": shown[sent:]})
            sent = len(shown)
    text = _clean(raw)
    if len(text) > sent:
        emit({"t": "text", "v": text[sent:]})
    return parts, raw, text, raw.lstrip().startswith("§"), getattr(finish, "value", finish)


def _turn(contents, emit):
    """One model turn on the key with quota left; a 429 benches that key and the turn moves to the next one."""
    from google.genai import errors
    ks = api_keys()
    if not ks:
        raise NoKey
    for i in range(len(ks)):
        key = quota.take(ks)
        try:
            return _stream(client(key), contents, emit)
        except errors.APIError as e:
            if e.code != 429:
                raise
            quota.exhausted(key, e.details)
            if i == len(ks) - 1:
                raise


def ask(prompt, emit, approve, fresh=False):
    """Run one prompt to completion. emit(event) streams progress; approve(script) -> bool gates root/irreversible."""
    global _last_call
    if not fresh and (hit := memory.lookup(prompt)):
        return emit({"t": "done", "v": hit, "cached": True})
    from google.genai import errors

    contents = [{"role": "user", "parts": [{"text": f"{prompt}\n\n(now: {time.strftime('%a %d %b %Y %H:%M')})"}]}]
    session = {"approved": None}  # one approval covers the rest of this prompt
    ran = False
    for _ in range(MAX_TURNS):
        try:
            parts, raw, text, keep, finish = _turn(contents, emit)
        except NoKey:
            return emit({"t": "error", "v": "no API key; run `mus setup` (remembered answers still work)"})
        except errors.APIError as e:
            if e.code == 429:
                return emit({"t": "error", "v": f"every API key is out of quota ({len(api_keys())} tried); try later"})
            return emit({"t": "error", "v": f"API error {e.code}: {e.message or e.status}"})
        _last_call = time.time()
        if finish in BLOCKED:
            return emit({"t": "error", "v": f"the model declined this one ({finish.lower()})"})
        runs = RUN.findall(raw)
        if "<run>" not in raw:
            if keep and not ran:
                memory.store(prompt, text)
            return emit({"t": "done"})
        if finish == "MAX_TOKENS":
            return emit({"t": "error", "v": "response cut off (max_tokens)"})
        ran = True
        results = [_exec(script, done, session, emit, approve) for script, done in runs] or [
            {"ok": False, "done": "", "feedback": "Your <run> block had no closing </run>; send the whole block again."}]
        if all(r["ok"] and r["done"] for r in results):  # short-circuit: no second API call just to say "done"
            return emit({"t": "done", "v": "\n".join(r["done"] for r in results)})
        contents += [{"role": "model", "parts": parts},
                     {"role": "user", "parts": [{"text": "\n\n".join(r["feedback"] for r in results)}]}]
    emit({"t": "error", "v": f"gave up after {MAX_TURNS} attempts"})
