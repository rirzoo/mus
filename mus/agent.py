"""The errand loop: memory first, then Gemini with a single `run` tool."""
import os
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import context, memory

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
TOOL = {
    "name": "run",
    "description": "Run one bash script on the user's machine (cwd ~, 120 s limit). Put the whole errand in a single script "
                   "and end it with a check that it worked. Returns combined stdout/stderr and the exit code.",
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "script": {"type": "string"},
            "done": {"type": "string", "description": "Line shown to the user if the script exits 0; `{out}` inserts "
                                                      "its output. Empty only if you must read the output first."},
        },
        "required": ["script", "done"],
    },
}


class NoKey(Exception):
    pass


def secret(kind):
    try:
        r = subprocess.run(["secret-tool", "lookup", "service", "mus", "key", kind], capture_output=True, text=True,
                           timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):  # no libsecret, or no keyring service answering
        return None
    return r.stdout.strip() or None


_client = None
_last_call = 0.0


def client():
    global _client
    if _client is None:
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or secret("api")
        if not key:
            raise NoKey
        from google import genai  # ~0.4 s, so only paid when the API is actually needed
        _client = genai.Client(api_key=key)
    return _client


def config():
    # the system prompt + tools prefix never changes between prompts, so Gemini's implicit caching can reuse it
    from google.genai import types
    return types.GenerateContentConfig(
        system_instruction=SYSTEM + context.get(), max_output_tokens=8000,
        tools=[types.Tool(function_declarations=[types.FunctionDeclaration(**TOOL)])],
        thinking_config=types.ThinkingConfig(thinking_level=THINKING),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))


def warm():
    """Open the HTTPS connection while the user is still typing; skipped if a call kept it open recently."""
    global _last_call
    if time.time() - _last_call < 240:
        return
    _last_call = time.time()
    try:
        client().models.get(model=MODEL)  # free metadata call, no tokens
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


def _tool(call, session, emit, approve):
    from google.genai import types
    args = call.args

    def result(output, ok=False, done=""):
        part = types.Part.from_function_response(name=call.name or "run", response={"ok": ok, "output": output})
        part.function_response.id = call.id  # pairs the result with its call when the model sends several
        return {"ok": ok, "done": done, "part": part}

    # the model's arguments are untrusted output: check the shape before running anything
    if call.name != "run" or not (isinstance(args, dict) and isinstance(args.get("script"), str)
                                  and isinstance(args.get("done", ""), str)):
        return result(f"invalid call {call.name}({args!r}); call run with a string `script` and `done`")
    script, done = args["script"], args.get("done", "")
    emit({"t": "run", "cmd": script})
    if GATED.search(script):
        with _approve_lock:
            if session["approved"] is None:
                session["approved"] = bool(approve(script))
        if not session["approved"]:
            return result("User declined to run this. Do not retry; reply with the commands for them to run themselves.")
    rc, out = run_script(script, session["approved"])
    if rc:
        return result(f"exit {rc}\n{out}")
    return result(out or "(no output)", ok=True, done=done.replace("{out}", out.strip()[:500]))


def _turn(c, contents, emit):
    """Stream one model turn. Returns its parts exactly as received (thought signatures must go back unchanged)."""
    parts, text, keep, lead, finish = [], [], False, True, None
    for chunk in c.models.generate_content_stream(model=MODEL, contents=contents, config=config()):
        cand = (chunk.candidates or [None])[0]
        if cand is None:
            continue
        finish = cand.finish_reason or finish
        for p in (cand.content.parts or []) if cand.content else []:
            parts.append(p)
            if not p.text or p.thought:
                continue
            t = p.text
            if lead:  # the § marker can only sit at the very start
                t = t.lstrip()
                if t.startswith("§"):
                    keep, t = True, t[1:].lstrip()
                if not t:
                    continue
                lead = False
            text.append(t)
            emit({"t": "text", "v": t})
    return parts, "".join(text), keep, getattr(finish, "value", finish)


def ask(prompt, emit, approve, fresh=False):
    """Run one prompt to completion. emit(event) streams progress; approve(script) -> bool gates root/irreversible."""
    global _last_call
    if not fresh and (hit := memory.lookup(prompt)):
        return emit({"t": "done", "v": hit, "cached": True})
    try:
        c = client()
    except NoKey:
        return emit({"t": "error", "v": "no API key; run `mus setup` (remembered answers still work)"})
    from google.genai import errors

    contents = [{"role": "user", "parts": [{"text": f"{prompt}\n\n(now: {time.strftime('%a %d %b %Y %H:%M')})"}]}]
    session = {"approved": None}  # one approval covers the rest of this prompt
    ran, bad_json, turns = False, 0, 0
    while turns < MAX_TURNS:
        try:
            parts, text, keep, finish = _turn(c, contents, emit)
        except errors.APIError as e:
            return emit({"t": "error", "v": f"API error {e.code}: {e.message or e.status}"})
        _last_call = time.time()
        if finish == "MALFORMED_FUNCTION_CALL":  # nothing to answer, so re-issue the turn
            bad_json += 1
            if bad_json > 2:
                return emit({"t": "error", "v": "model kept sending malformed tool calls"})
            continue
        turns += 1
        calls = [p.function_call for p in parts if p.function_call]
        if finish in BLOCKED:
            return emit({"t": "error", "v": f"the model declined this one ({finish.lower()})"})
        if not calls:
            if keep and not ran:
                memory.store(prompt, text)
            return emit({"t": "done"})
        if finish == "MAX_TOKENS":
            return emit({"t": "error", "v": "response cut off (max_tokens)"})
        ran = True
        with ThreadPoolExecutor() as ex:
            results = list(ex.map(lambda call: _tool(call, session, emit, approve), calls))
        if all(r["ok"] and r["done"] for r in results):  # short-circuit: no second API call just to say "done"
            return emit({"t": "done", "v": "\n".join(r["done"] for r in results)})
        contents += [{"role": "model", "parts": parts}, {"role": "user", "parts": [r["part"] for r in results]}]
    emit({"t": "error", "v": f"gave up after {MAX_TURNS} attempts"})
