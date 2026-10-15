# Mus

Mus is a small mouse that pops out from a random edge of your GNOME screen and does one errand per prompt. You can ask it something like "what's the shortcut to open the browser?" or "move file1.jpg and file2.jpg from Downloads to tools". It never asks you a follow-up question. If your prompt could mean more than one thing, it either answers each reading or does the safe, reversible one and tells you what it assumed.

```
$ mus "what shortcut opens the terminal"
⚡ Press `Super+T` (your custom Terminal shortcut).
$ mus "move a.jpg b.jpg from Downloads to tools"
$ set -e  (+3 lines)
Moved a.jpg and b.jpg to ~/projects/tools.
```

There are three parts:

- **A popup.** This is a GNOME Shell extension. Press `Super+M` or click the mouse icon in the top bar to open it.
- **A daemon (`mus serve`).** It runs as a systemd user service and does the actual work. The popup and the CLI both talk to it over `$XDG_RUNTIME_DIR/mus.sock`.
- **The `mus` CLI.** You can use it from a terminal, a script or Claude Code. If the daemon isn't running, the CLI does the work itself.

## Why it's fast

- **It remembers answers.** Answers to stable questions like "which shortcut does X" or "how do I Y" are saved in `~/.local/share/mus/memory.db`. If you ask the same thing again, even worded differently, the answer comes back in about 40 ms with no API call. Press `Ctrl+R` in the popup, or pass `--fresh`, to ask the model again.
- **The model already knows your machine.** Every request carries a snapshot of your machine: your GNOME keybindings, default apps, installed tools, desktop apps and the folders under `~`. The snapshot is the same from one prompt to the next, so Gemini can cache it. With it, Mus answers most questions without running anything, and it knows "tools" means your `~/projects/tools` folder.
- **Most errands take one API call.** The model writes the whole errand as a single script and includes the line to show you if it succeeds. If the script works, Mus shows that line and stops. If it fails, the model sees the error and tries again, up to 6 times.
- **The model is the fastest one.** Mus uses Gemini 3.5 Flash-Lite with minimal thinking, and the popup opens the connection to Google while you're still typing.

## Root and irreversible commands

Commands that use `sudo`, `pkexec`, `rm`, `dd`, `mkfs` and similar wait for your approval. In the popup, press Enter or click **Allow**. In a terminal, type `y`. One approval covers the rest of that prompt: every `sudo` in it gets your password from the GNOME keyring through `SUDO_ASKPASS`. The model never sees the password. If you deny, or the CLI isn't attached to a terminal (Claude Code, for example), nothing runs and Mus gives you the commands to run yourself.

The approval protects you from the model. It doesn't protect you from other programs running as you: anything running as your user can read the keyring, including the password Mus keeps there.

## Install

```sh
git clone https://github.com/rirzoo/mus && cd mus
pip install --user -e .
mus setup        # Gemini API key, sudo password, systemd service, extension, frees Super+M
```

Then log out and back in once. On Wayland, GNOME only loads a new extension at login. `mus setup` moves `<Super>m` off the notification list, which `Super+V` still opens.

If you don't have a Gemini API key yet, skip that step and Mus still answers from memory. You can get one at https://aistudio.google.com/apikey and store it with `mus setup`, or put it in `GEMINI_API_KEY`.

## Usage

| Command | What it does |
|---|---|
| `mus "<prompt>"` | Runs one errand and streams the answer |
| `mus --fresh "<prompt>"` | Skips memory and asks again |
| `mus --json "<prompt>"` | Prints raw events, one JSON object per line |
| `mus memory [--forget ID \| --clear]` | Lists or removes remembered answers |
| `mus refresh` | Rebuilds the machine snapshot (it also refreshes itself daily) |
| `mus serve` | Runs the daemon (systemd normally does this) |

These are the popup keys:

| Key | What it does |
|---|---|
| `Enter` | Send the prompt, or allow a pending approval |
| `Esc` | Close the popup, or deny a pending approval |
| `Ctrl+R` | Ask the model again instead of using memory |
| Click the answer | Copy the first `code` part, or the whole answer if there isn't one |

If you close the popup while an errand is still running, the result arrives as a notification.

## Development

```sh
python3 test_mus.py                                    # offline: memory, approval gate, loop, short-circuit
journalctl --user -u mus -f                            # daemon log
journalctl --user -f /usr/bin/gnome-shell | grep -i mus  # extension errors
```

To change the model, edit `MODEL` in `mus/agent.py`. The system prompt is in `mus/system.md`.
