"""Machine snapshot that rides in the cached system prompt, so most questions need no tool call."""
import ast
import os
import shutil
import subprocess
import time
from pathlib import Path

HOME = Path.home()
CACHE = HOME / ".cache/mus/snapshot.txt"
NOTES = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "mus/notes.md"  # personal hints, never in the repo
PREFS = NOTES.with_name("prefs.md")  # answer preferences, written by `mus setup`
MAX_AGE = 24 * 3600
SKIP_DIRS = {"node_modules", "__pycache__", "venv", "site-packages", "build", "dist", "target", "snap"}
MAX_DIRS = 1500
TOOLS = ("fd rg jq ffmpeg magick convert pandoc yt-dlp gio nmcli dnf flatpak git gh docker podman python3 node npm "
         "pip unzip 7z tar rsync curl wget xdg-open gtk-launch wl-copy wl-paste notify-send pdftotext qpdf tesseract "
         "code nvim claude exiftool trash-put ocrmypdf zip").split()
KEY_SCHEMAS = ("org.gnome.desktop.wm.keybindings", "org.gnome.shell.keybindings", "org.gnome.mutter.keybindings",
               "org.gnome.mutter.wayland.keybindings", "org.gnome.settings-daemon.plugins.media-keys")
MEDIA = "org.gnome.settings-daemon.plugins.media-keys"


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def keybindings():
    out = []
    for schema in KEY_SCHEMAS:
        for line in sh(f"gsettings list-recursively {schema}").splitlines():
            parts = line.split(" ", 2)
            if len(parts) < 3:
                continue
            _, key, val = parts
            if key == "custom-keybindings" or val in ("@as []", "['']", "''") or val[0] not in "['":
                continue
            out.append(f"{key}: {val}")
    try:
        paths = ast.literal_eval(sh(f"gsettings get {MEDIA} custom-keybindings"))
    except (ValueError, SyntaxError):
        paths = []
    for p in paths:
        get = lambda k: sh(f"gsettings get {MEDIA}.custom-keybinding:{p} {k}")  # noqa: E731
        out.append(f"custom {get('name')}: {get('binding')} runs {get('command')}")
    return out


def dirs():
    out = []
    for root, sub, _ in os.walk(HOME):
        depth = len(Path(root).relative_to(HOME).parts)
        sub[:] = sorted(d for d in sub if not d.startswith(".") and d not in SKIP_DIRS) if depth < 3 else []
        out += [f"~/{Path(root, d).relative_to(HOME)}" for d in sub]
        if len(out) >= MAX_DIRS:
            # ponytail: breadth is cut at MAX_DIRS; the model falls back to `fd` for anything deeper
            return out[:MAX_DIRS]
    return out


def apps():
    seen = {}
    for base in ("/usr/share/applications", "~/.local/share/applications", "/var/lib/flatpak/exports/share/applications",
                 "~/.local/share/flatpak/exports/share/applications"):
        for f in sorted(Path(base).expanduser().glob("*.desktop")):
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            if "NoDisplay=true" in text:
                continue
            name = next((l[5:] for l in text.splitlines() if l.startswith("Name=")), f.stem)
            seen[f.stem] = name
    return [f"{name} ({stem})" for stem, name in seen.items()]


def build():
    user_bins = sorted({p.name for d in ("~/.local/bin", "~/bin") for p in Path(d).expanduser().glob("*") if p.is_file()})
    xdg = {k: sh(f"xdg-user-dir {k}") for k in ("DESKTOP", "DOWNLOAD", "DOCUMENTS", "PICTURES", "MUSIC", "VIDEOS")}
    mime = {m: sh(f"xdg-mime query default {m}") for m in
            ("text/plain", "application/pdf", "image/jpeg", "video/mp4", "inode/directory", "x-scheme-handler/mailto")}
    sections = {
        "system": [sh(". /etc/os-release; echo $PRETTY_NAME"), f"kernel {os.uname().release}",
                   f"{sh('gnome-shell --version')} on {os.environ.get('XDG_SESSION_TYPE', 'wayland')}",
                   f"user {os.environ.get('USER', '')}, home {HOME}, shell {os.environ.get('SHELL', '')}"],
        "user dirs": [f"{k}: {v}" for k, v in xdg.items() if v],
        "default apps": [f"browser: {sh('xdg-settings get default-web-browser')}"] + [f"{m}: {v}" for m, v in mime.items() if v],
        "keybindings (gsettings)": keybindings(),
        "installed CLI tools": [" ".join(t for t in TOOLS if shutil.which(t))],
        "user scripts in ~/.local/bin, ~/bin": [" ".join(user_bins)],
        "desktop apps: Name (desktop id for gtk-launch)": apps(),
        f"directories under ~ (depth 3, hidden skipped)": dirs(),
    }
    for title, f in (("notes from the user (trust these over guesses)", NOTES), ("answer preferences (from mus setup)", PREFS)):
        try:
            sections[title] = [f.read_text().strip()]
        except OSError:
            pass
    return "\n\n".join(f"## {title}\n" + "\n".join(lines) for title, lines in sections.items())


def refresh():
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    text = build()
    CACHE.write_text(text)
    return text


_memo = (0.0, "")


def _mtime(f):
    try:
        return f.stat().st_mtime
    except OSError:
        return 0


def get():
    """Snapshot text, rebuilt when older than a day or when notes.md or prefs.md changed; memoized on mtime so the daemon pays three stats per prompt."""
    global _memo
    mtime = _mtime(CACHE)
    if max(_mtime(NOTES), _mtime(PREFS)) > mtime or time.time() - mtime > MAX_AGE:
        _memo = (time.time(), refresh())
    elif mtime != _memo[0]:
        _memo = (mtime, CACHE.read_text())
    return _memo[1]
