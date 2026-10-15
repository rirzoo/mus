"""Answer cache: repeated questions skip the API entirely."""
import re
import sqlite3
import time
from pathlib import Path

DB = Path.home() / ".local/share/mus/memory.db"
KEEP = 500
MATCH = 0.8  # token Jaccard; 0.8 keeps "volume up" vs "volume down" apart but forgives one extra word in 5
STOP = set("a an the to of for in on at by my me i do does did is are was be can could would should will how what whats "
           "which where when who why please pls tell show give us it this that there with and or you your have has get "
           "use using way".split())


def key(prompt):
    words = re.findall(r"[a-z0-9.+_-]+", prompt.lower())
    stem = lambda w: w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w  # noqa: E731
    return " ".join(sorted({stem(w.strip(".-")) for w in words if w not in STOP and (len(w) > 1 or w.isdigit())}))


def _db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS qa (key TEXT PRIMARY KEY, prompt TEXT, answer TEXT, hits INT, last_used REAL)")
    return con


def lookup(prompt):
    k = key(prompt)
    if not k:
        return None
    with _db() as con:
        row = con.execute("SELECT key, answer FROM qa WHERE key = ?", (k,)).fetchone()
        if not row:
            # ponytail: linear scan, fine to ~10k rows; embeddings if recall disappoints
            mine = set(k.split())
            best = max(((len(mine & set(kk.split())) / len(mine | set(kk.split())), kk, a)
                        for kk, a in con.execute("SELECT key, answer FROM qa")), default=None)
            row = best[1:] if best and best[0] >= MATCH else None
        if row:
            con.execute("UPDATE qa SET hits = hits + 1, last_used = ? WHERE key = ?", (time.time(), row[0]))
            return row[1]
    return None


def store(prompt, answer):
    k = key(prompt)
    if not k or not answer.strip():
        return
    with _db() as con:
        con.execute("INSERT INTO qa VALUES (?, ?, ?, 1, ?) ON CONFLICT(key) DO UPDATE SET "
                    "answer = excluded.answer, prompt = excluded.prompt, last_used = excluded.last_used",
                    (k, prompt, answer.strip(), time.time()))
        con.execute("DELETE FROM qa WHERE key NOT IN (SELECT key FROM qa ORDER BY hits DESC, last_used DESC LIMIT ?)", (KEEP,))


def rows():
    with _db() as con:
        return con.execute("SELECT rowid, hits, prompt, answer FROM qa ORDER BY hits DESC, last_used DESC").fetchall()


def forget(rowid=None):
    with _db() as con:
        con.execute("DELETE FROM qa" + ("" if rowid is None else " WHERE rowid = ?"), () if rowid is None else (rowid,))
