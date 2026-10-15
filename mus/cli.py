"""mus command line: one-shot prompts, the daemon the popup talks to, setup and memory."""
import argparse
import getpass
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

from . import agent, context, memory

SOCK = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "mus.sock"
EXT_UUID = "mus@rirzoo"
EXT_SRC = Path(__file__).resolve().parent.parent / "extension" / EXT_UUID
UNIT = Path.home() / ".config/systemd/user/mus.service"


# --- daemon ------------------------------------------------------------------

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            req = json.loads(self.rfile.readline() or b"{}")
        except ValueError:
            return
        if req.get("warm"):
            return agent.warm()
        lock = threading.Lock()

        def emit(ev):
            with lock:
                self.wfile.write(json.dumps(ev).encode() + b"\n")

        def approve(script):
            if not req.get("interactive"):
                return False
            emit({"t": "approve", "cmd": script})
            try:
                return bool(json.loads(self.rfile.readline()).get("approve"))
            except ValueError:  # client went away: treat as no
                return False

        try:
            agent.ask(req.get("prompt", ""), emit, approve, req.get("fresh", False))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # keep the daemon alive and tell the popup why
            try:
                emit({"t": "error", "why": "crash", "v": f"{type(e).__name__}: {e}"})
            except OSError:
                pass


def cmd_serve(a):
    context.get()
    try:
        agent.client()  # import the SDK and open the client before the first prompt arrives
    except agent.NoKey:
        print("mus: no API key yet; serving remembered answers only", file=sys.stderr)
    SOCK.unlink(missing_ok=True)
    socketserver.ThreadingUnixStreamServer.daemon_threads = True
    with socketserver.ThreadingUnixStreamServer(str(SOCK), Handler) as srv:
        os.chmod(SOCK, 0o600)
        print(f"mus: listening on {SOCK}", file=sys.stderr)
        srv.serve_forever()


# --- one-shot prompt -----------------------------------------------------------

def tty_approve(script):
    if not sys.stdin.isatty():
        return False
    print(f"\n\033[33mmus wants to run (root or irreversible):\033[0m\n{script}", file=sys.stderr)
    try:
        return input("allow for this prompt? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_ask(a):
    prompt = " ".join(a.prompt)
    failed = False

    def show(ev):
        nonlocal failed
        failed |= ev["t"] == "error"
        if a.json:
            print(json.dumps(ev), flush=True)
        elif ev["t"] == "text":
            print(ev["v"], end="", flush=True)
        elif ev["t"] == "run":
            lines = ev["cmd"].strip().splitlines() or [""]
            more = f"  (+{len(lines) - 1} lines)" if len(lines) > 1 else ""
            print(f"\033[2m$ {lines[0]}{more}\033[0m", file=sys.stderr, flush=True)
        elif ev["t"] == "wait" and ev.get("v"):  # why it's slow: network, model, quota or a retry
            print(f"\033[2m… {ev['v']}\033[0m", file=sys.stderr, flush=True)
        elif ev["t"] == "done":
            print(("⚡ " if ev.get("cached") else "") + ev["v"] if ev.get("v") else "")
        elif ev["t"] == "error":
            print(f"\nmus: {ev['v']}", file=sys.stderr)

    req = {"prompt": prompt, "fresh": a.fresh, "interactive": sys.stdin.isatty()}
    try:
        s = socket.socket(socket.AF_UNIX)
        s.connect(str(SOCK))
    except OSError:  # no daemon: run in-process (pays the SDK import)
        agent.ask(prompt, show, tty_approve, a.fresh)
        return int(failed)
    with s, s.makefile("rb") as r, s.makefile("wb", buffering=0) as w:
        w.write(json.dumps(req).encode() + b"\n")
        for line in r:
            ev = json.loads(line)
            show(ev)
            if ev["t"] == "approve":
                w.write(json.dumps({"approve": tty_approve(ev["cmd"])}).encode() + b"\n")
    return int(failed)


# --- housekeeping --------------------------------------------------------------

def cmd_refresh(a):
    text = context.refresh()
    print(f"snapshot rebuilt: {len(text)} chars (~{len(text) // 4} tokens) in {context.CACHE}")


def cmd_memory(a):
    if a.clear:
        memory.forget()
        return print("memory cleared")
    if a.forget is not None:
        memory.forget(a.forget)
        return print(f"forgot {a.forget}")
    for rowid, hits, prompt, answer in memory.rows():
        print(f"{rowid:4} {hits:3}x  {prompt[:50]:50}  {answer.splitlines()[0][:60]}")


def store_secret(kind, label, value):
    subprocess.run(["secret-tool", "store", "--label", label, "service", "mus", "key", kind], input=value, text=True,
                   check=True)


def run(*cmd):
    print("  $", " ".join(cmd))
    subprocess.run(cmd, check=False)


def cmd_setup(a):
    if not shutil.which("secret-tool"):
        sys.exit("mus: secret-tool not found; install libsecret (Fedora) or libsecret-tools (Ubuntu)")
    print("1. Gemini API keys (https://aistudio.google.com/apikey); with several, Mus moves on before one runs out")
    have = agent.api_keys()
    n = input(f"  {len(have)} stored. How many keys will you enter? (Enter keeps them) " if have
              else "  How many keys do you have? (Enter to skip) ").strip()
    if n.isdigit() and int(n) > 0:
        keys = [k for i in range(int(n)) if (k := getpass.getpass(f"  key {i + 1} of {n}: ").strip())]
        if keys:  # replaces the stored set, so include keys you already had
            store_secret("api", "mus Gemini API keys", "\n".join(keys))
            print(f"  stored {len(keys)} key(s)")

    print("2. sudo password for one-tap approvals")
    if agent.secret("sudo"):
        print("  already set")
    elif pw := getpass.getpass("  sudo password (Enter to skip): "):
        ok = subprocess.run(["sudo", "-k", "-S", "-p", "", "true"], input=pw + "\n", text=True,
                            capture_output=True).returncode == 0
        if ok:
            store_secret("sudo", "mus sudo password", pw)
        else:
            print("  sudo rejected that password; skipped")

    print("3. how Mus answers (Enter skips a question; edit ~/.config/mus/prefs.md any time)")
    old = context.PREFS.read_text().strip() if context.PREFS.exists() else ""
    if not old or input("  " + old.replace("\n", "\n  ") + "\n  change these? [y/N] ").strip().lower() == "y":
        qs = (("Length", "short = 1-2 lines, normal, detailed = up to 6"), ("Tone", "plain, friendly, playful"),
              ("Language", "e.g. English, Hindi"), ("Anything else", "e.g. call me R, I use fish, prefer flatpak"))
        prefs = [f"- {q}: {v}" for q, hint in qs if (v := input(f"  {q} ({hint}): ").strip())]
        context.PREFS.parent.mkdir(parents=True, exist_ok=True)
        if prefs:
            context.PREFS.write_text("\n".join(prefs) + "\n")
        else:
            context.PREFS.unlink(missing_ok=True)

    print("4. daemon (systemd user service)")
    UNIT.parent.mkdir(parents=True, exist_ok=True)
    UNIT.write_text(f"[Unit]\nDescription=Mus errand daemon\n\n[Service]\nExecStart={sys.executable} -m mus serve\n"
                    f"Environment=PYTHONPATH={Path(__file__).resolve().parent.parent}\nRestart=on-failure\nKillMode=process\n\n"
                    "[Install]\nWantedBy=default.target\n")
    run("systemctl", "--user", "daemon-reload")
    run("systemctl", "--user", "enable", "--now", "mus")
    run("systemctl", "--user", "restart", "mus")

    print("5. popup (GNOME Shell extension)")
    run("glib-compile-schemas", str(EXT_SRC / "schemas"))
    dest = Path.home() / ".local/share/gnome-shell/extensions" / EXT_UUID
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.symlink_to(EXT_SRC)
    enabled = subprocess.run(["gsettings", "get", "org.gnome.shell", "enabled-extensions"], capture_output=True,
                             text=True).stdout.strip()
    if EXT_UUID not in enabled:
        items = [] if enabled.startswith("@as") else json.loads(enabled.replace("'", '"'))
        run("gsettings", "set", "org.gnome.shell", "enabled-extensions", json.dumps(items + [EXT_UUID]))
    # Super+M is a duplicate of Super+V for the notification list; hand it to the mouse
    run("gsettings", "set", "org.gnome.shell.keybindings", "toggle-message-tray", "['<Super>v']")
    print("\ndone. Log out and back in once so GNOME loads the extension, then press Super+M.")


def main():
    argv = sys.argv[1:]
    p = argparse.ArgumentParser(prog="mus", description="one-prompt errands: mus \"move a.jpg from Downloads to tools\"")
    if argv and argv[0] in ("serve", "setup", "refresh", "memory"):
        sub = p.add_subparsers(dest="cmd", required=True)
        sub.add_parser("serve", help="run the daemon the popup talks to").set_defaults(fn=cmd_serve)
        sub.add_parser("setup", help="store keys, install the daemon and the popup").set_defaults(fn=cmd_setup)
        sub.add_parser("refresh", help="rebuild the machine snapshot").set_defaults(fn=cmd_refresh)
        m = sub.add_parser("memory", help="list remembered answers")
        m.add_argument("--forget", type=int, metavar="ID")
        m.add_argument("--clear", action="store_true")
        m.set_defaults(fn=cmd_memory)
    else:
        p.add_argument("prompt", nargs="+")
        p.add_argument("--fresh", action="store_true", help="skip memory and ask again")
        p.add_argument("--json", action="store_true", help="print raw events, one JSON object per line")
        p.set_defaults(fn=cmd_ask)
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except KeyboardInterrupt:
        return 130
