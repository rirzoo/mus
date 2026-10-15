"""Offline checks for mus: memory matching, the approval gate, the <run> loop, § handling and key rotation."""
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from mus import agent, context, memory, quota

memory.DB = Path(tempfile.mkdtemp()) / "memory.db"
quota.STATE = Path(tempfile.mkdtemp()) / "quota.json"
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
    """Fake genai client: each turn is a list of text chunks (one stream chunk each) or an exception to raise."""

    def __init__(self, turns, finish=None):
        self.turns, self.calls, self.finish, self.keys = list(turns), 0, finish, []
        self.models = self

    def generate_content_stream(self, model, contents, config):
        turn = self.turns[self.calls]
        self.calls += 1
        if isinstance(turn, Exception):
            raise turn
        parts = [NS(text=c, thought=None) for c in turn]
        finish = self.finish or "STOP"
        return [NS(candidates=[NS(content=NS(parts=[p]), finish_reason=finish if p is parts[-1] else None)])
                for p in parts]


def run(turns, prompt="q", approve=lambda s: False, finish=None, keys=("k1",)):
    stub = Stub(turns, finish)
    agent.api_keys = lambda: list(keys)
    agent.client = lambda key=None: stub.keys.append(key) or stub
    events = []
    agent.ask(prompt, events.append, approve, fresh=True)
    return stub, events


def shown(ev):
    return "".join(e["v"] for e in ev if e["t"] == "text")


def test_loop():
    # § marker split across chunks is stripped and the answer remembered
    stub, ev = run([["§", " Press `Super+T`"]], prompt="terminal shortcut")
    assert shown(ev) == "Press `Super+T`"
    assert memory.lookup("terminal shortcut") == "Press `Super+T`"
    # a § the model repeats on later lines never reaches the screen
    stub, ev = run([["§ UI: `Shift+Super+S`\n§ Window: `Alt+Print`"]])
    assert shown(ev) == "UI: `Shift+Super+S`\nWindow: `Alt+Print`"
    # a successful script with <done> ends the prompt without a second call; a tag split across chunks stays hidden
    stub, ev = run([["Checking.\n<r", "un>\necho 42\n</run>\n<done>answer: {out}</done>"]])
    assert stub.calls == 1 and ev[-1] == {"t": "done", "v": "answer: 42"} and shown(ev) == "Checking.\n"
    # a fenced script still runs
    stub, ev = run([["<run>\n```bash\necho 7\n```\n</run><done>{out}</done>"]])
    assert ev[-1] == {"t": "done", "v": "7"}
    # a failing script goes back to the model, which then finishes
    stub, ev = run([["<run>exit 3</run><done>ok</done>"], ["fixed"]])
    assert stub.calls == 2 and ev[-1] == {"t": "done"}
    # gated script: declined means it never runs
    marker = Path(tempfile.mkdtemp()) / "ran"
    stub, ev = run([[f"<run>sudo true; touch {marker}</run><done>ok</done>"], ["run it yourself"]])
    assert stub.calls == 2 and not marker.exists()
    # an unclosed <run> is reported back, not executed
    stub, ev = run([["<run>\necho hi"], ["sorry"]])
    assert stub.calls == 2 and not any(e["t"] == "run" for e in ev)
    # a safety block ends the prompt with an error instead of looping
    stub, ev = run([["no"]], finish="SAFETY")
    assert stub.calls == 1 and ev[-1]["t"] == "error"


def quota_error(kind, value, delay="30s"):
    from google.genai import errors
    return errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": f"GenerateRequests{kind}PerProjectPerModel-FreeTier", "quotaValue": str(value)}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]}})


def test_quota():
    quota.STATE = Path(tempfile.mkdtemp()) / "quota.json"
    quota.GUESS = {"minute": 2, "day": 100}
    # preemptive: the first key until its minute budget is used, then the next
    assert [quota.take(["x", "y"]) for _ in range(3)] == ["x", "x", "y"]
    # a 429 benches the key, the same turn moves on to the next key, and the real daily limit is learned
    stub, ev = run([quota_error("PerDay", 20), ["hi"]], keys=("k1", "k2"))
    assert stub.keys == ["k1", "k2"] and ev[-1] == {"t": "done"}
    assert quota._load()["limits"]["day"] == 20 and quota.take(["k1", "k2"], record=False) == "k2"
    # every key out of quota: one clear error
    stub, ev = run([quota_error("PerMinute", 5), quota_error("PerMinute", 5)], keys=("a", "b"))
    assert ev[-1]["t"] == "error" and "quota" in ev[-1]["v"]


def waits(ev):
    return [e["why"] for e in ev if e["t"] == "wait" and e["why"] != "work"]


def test_wait():
    import time
    # each turn's first chunk says the model has started; a failed script says it's retrying, a 429 that it switched keys
    stub, ev = run([["<run>exit 3</run><done>ok</done>"], ["fixed"]])
    assert [e["why"] for e in ev if e["t"] == "wait"] == ["work", "retry", "work"]
    stub, ev = run([quota_error("PerMinute", 5), ["hi"]], keys=("q1", "q2"))
    assert waits(ev) == ["quota"]
    # a declined approval isn't a retry
    stub, ev = run([["<run>sudo true</run><done>ok</done>"], ["run it yourself"]])
    assert waits(ev) == []
    # a late first token: the ping decides whether the model, the network or the connection is the problem
    import httpx

    def ping(err):
        def get(model, config):
            if err:
                raise err
        return get
    agent.SLOW_FIRST = 0.05
    for err, why in ((None, "model"), (httpx.ReadTimeout("slow"), "network"), (httpx.ConnectError("down"), "offline")):
        stub = Stub([["hi"]])
        stub.get = ping(err)
        gen = stub.generate_content_stream
        stub.generate_content_stream = lambda **kw: (time.sleep(0.4), gen(**kw))[1]
        agent.client = lambda key=None: stub
        ev = []
        agent.ask("q", ev.append, lambda s: False, fresh=True)
        assert waits(ev) == [why] and ev[-1] == {"t": "done"}, ev
    agent.SLOW_FIRST = 2.5


def test_prompt():
    # who made Mus and which model it runs on come from about.md, filled in once at import
    assert "{about}" not in agent.SYSTEM and "{model}" not in agent.SYSTEM
    assert "Romir made Mus" in agent.SYSTEM and agent.MODEL in agent.SYSTEM


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
