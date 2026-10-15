"""Offline checks for mus: memory matching, the approval gate, § handling and the done short-circuit."""
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from mus import agent, context, memory

memory.DB = Path(tempfile.mkdtemp()) / "memory.db"
context.get = lambda: "(snapshot)"


def test_memory():
    assert memory.key("What keyboard shortcut do I have to open the browser?") == "browser keyboard open shortcut"
    assert memory.key("shortcut, open browser keyboard") == memory.key("What's the keyboard shortcut to open browsers")
    memory.store("what keyboard shortcut do I have to open the browser?", "`Super+1`")
    assert memory.lookup("keyboard shortcut to open the browser") == "`Super+1`"
    assert memory.lookup("keyboard shortcut to open the browser quickly now") is None  # 4/6 overlap: too far
    memory.store("volume up shortcut", "up")
    assert memory.lookup("volume down shortcut") is None
    assert memory.lookup("shortcut for volume up") == "up"
    memory.forget()
    assert memory.lookup("volume up shortcut") is None


def test_gate():
    for s in ("sudo dnf install -y cowsay", "rm -rf ~/x", "dd if=a of=/dev/sda", "pkexec true", "mkfs.ext4 /dev/sdb1",
              "chmod -R 777 /", "echo x > /dev/sda"):
        assert agent.GATED.search(s), s
    for s in ("mv -n ~/Downloads/a.jpg ~/projects/", "gio trash old.txt", "rmdir empty", "ls -l /", "dnf list cowsay",
              "chmod -R 755 ~/proj"):
        assert not agent.GATED.search(s), s


class Stub:
    """Fake genai client: each turn is (text chunks, tool args); it streams one chunk per text piece."""

    def __init__(self, turns, finish=None):
        self.turns, self.calls, self.finish = list(turns), 0, finish
        self.models = self

    def generate_content_stream(self, model, contents, config):
        chunks, tools = self.turns[self.calls]
        self.calls += 1
        part = lambda **kw: NS(**{"text": None, "thought": None, "function_call": None, **kw})  # noqa: E731
        parts = [part(text=c) for c in chunks]
        parts += [part(function_call=NS(name="run", args=t, id=f"t{i}")) for i, t in enumerate(tools)]
        finish = self.finish or "STOP"
        return [NS(candidates=[NS(content=NS(parts=[p]), finish_reason=finish if p is parts[-1] else None)])
                for p in parts]


def run(turns, prompt="q", approve=lambda s: False, finish=None):
    stub = Stub(turns, finish)
    agent._client = stub
    events = []
    agent.ask(prompt, events.append, approve, fresh=True)
    return stub.calls, events


def test_loop():
    # § marker split across chunks is stripped and the answer remembered
    calls, ev = run([(["§", " Press `Super+T`"], [])], prompt="terminal shortcut")
    assert "".join(e["v"] for e in ev if e["t"] == "text") == "Press `Super+T`"
    assert memory.lookup("terminal shortcut") == "Press `Super+T`"
    # a successful script with `done` ends the prompt without a second API call; {out} is filled in
    calls, ev = run([([], [{"script": "echo 42", "done": "answer: {out}"}])])
    assert calls == 1 and ev[-1] == {"t": "done", "v": "answer: 42"}
    # a failing script goes back to the model, which then finishes
    calls, ev = run([([], [{"script": "exit 3", "done": "ok"}]), (["fixed"], [])])
    assert calls == 2 and ev[-1] == {"t": "done"}
    # gated script: declined means it never runs
    marker = Path(tempfile.mkdtemp()) / "ran"
    calls, ev = run([([], [{"script": f"sudo true; touch {marker}", "done": "ok"}]), (["run it yourself"], [])])
    assert calls == 2 and not marker.exists()
    # malformed tool input is reported, not executed
    calls, ev = run([([], [{"script": 5}]), (["sorry"], [])])
    assert calls == 2 and not any(e["t"] == "run" for e in ev)
    # a safety block ends the prompt with an error instead of looping
    calls, ev = run([(["no"], [])], finish="SAFETY")
    assert calls == 1 and ev[-1]["t"] == "error"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
