"""Процесс плагина «Посты в Telegram»: читает журнал хаба; нажатие ❤️ / 👎 под карточкой колоды (telegram.pressed) и
реплай владельца на неё (telegram.replied) отдаёт серверу колоды — без хода сессии. Колода лежит — повторяет, пока не
примет (до часа). Ядро не правит: читает журнал своим смещением."""
import json
import os
import sys
import time
import urllib.error
import urllib.request

from core import catalog, journal

EXT = "tg-posts"   # так его факты подписывает CLI (bin/tg-posts) — по нему бот отдаёт нажатия сюда
NAME = os.environ.get("HUB_EXTENSION", EXT)
LIKE, NOPE = "❤️", "👎"
RETRY, GIVE_UP = 30, 3600   # с: повтор при лежащей колоде, сколько пытаться


def deck_url(state):
    ext = (catalog.load_config(state).get("extensions") or {})
    for k in (NAME, EXT):
        v = ext.get(k)
        if isinstance(v, dict) and v.get("deck"):
            return str(v["deck"]).rstrip("/")
    return "http://localhost:7777"


def request(ev):
    """Факт → (путь API, тело) или None (не наше, не карточка колоды)."""
    data = ev.get("data") or {}
    if data.get("ext") != EXT or not data.get("ref"):
        return None
    if ev.get("type") == "telegram.pressed":
        verdict = {LIKE: "like", NOPE: "reject"}.get(data.get("button"))
        return ("/api/decide", {"id": data["ref"], "verdict": verdict}) if verdict else None
    if ev.get("type") == "telegram.replied" and (data.get("text") or "").strip():
        return "/api/direct", {"id": data["ref"], "text": data["text"].strip()}
    return None


class Plugin:
    def __init__(self, state_dir, post=None, clock=time.time):
        self.dir, self.path = state_dir, os.path.join(state_dir, "journal.jsonl")
        self.mine = os.path.join(state_dir, EXT, "state.json")
        self.post, self.clock = post or self.http, clock
        try:
            with open(self.mine, encoding="utf-8") as f:
                self.st = json.load(f)
        except (OSError, ValueError):
            self.st = None
        self.ino, self.offset = journal.identity(self.path), self.size()
        if self.st is None:   # первый запуск — с конца журнала: старые нажатия не повторять
            last = journal.read_tail(self.path)
            self.st = {"seq": last[-1]["seq"] if last else 0, "queue": []}
            self.save()
        else:   # пока лежал — догнать по хвосту журнала
            self.handle(journal.read_tail(self.path))

    def size(self):
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def save(self):
        os.makedirs(os.path.dirname(self.mine), exist_ok=True)
        tmp = self.mine + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.st, f, ensure_ascii=False)
        os.replace(tmp, self.mine)

    def http(self, path, body):
        """→ True — колода ответила (принято или отказ по делу: 404 уже оценена, 422), False — повторить."""
        req = urllib.request.Request(deck_url(self.dir) + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=60):
                return True
        except urllib.error.HTTPError as e:
            why = e.read().decode("utf-8", "replace")[:200]
            e.close()
            sys.stderr.write(f"tg-posts: {path} {body.get('id')}: {e.code} {why}\n")
            return e.code < 500 or e.code == 502   # 502 — ошибка по делу (текст колоды), не сеть
        except OSError as e:
            sys.stderr.write(f"tg-posts: колода не отвечает ({e}) — повторю\n")
            return False

    def handle(self, events):
        changed = False
        for ev in events:
            if ev.get("seq", 0) <= self.st["seq"]:
                continue
            self.st["seq"], changed = ev["seq"], True
            req = request(ev)
            if req:
                self.st["queue"].append({"path": req[0], "body": req[1], "since": self.clock(), "next": 0})
        if (self.flush() if self.st["queue"] else False) or changed:
            self.save()

    def flush(self):
        """По порядку: не принятое ждёт повтора, а следующие — за ним (оценка и реплай к одной карточке не меняются местами)."""
        now, left, changed = self.clock(), [], False
        for job in self.st["queue"]:
            if left or job["next"] > now:
                left.append(job)
            elif self.post(job["path"], job["body"]):
                changed = True
            elif now - job["since"] < GIVE_UP:
                left.append({**job, "next": now + RETRY})
                changed = True
            else:
                sys.stderr.write(f"tg-posts: {job['path']} {job['body'].get('id')}: колода не ответила за час — бросаю\n")
                changed = True
        self.st["queue"] = left
        return changed

    def poll(self):
        ino = journal.identity(self.path)
        if ino != self.ino or self.size() < self.offset:   # журнал начат заново — читать новый с начала
            self.ino, self.offset = ino, 0
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read()
        except OSError:
            return
        end = chunk.rfind(b"\n")
        events = []
        if end >= 0:
            self.offset += end + 1
            for line in chunk[:end].split(b"\n"):
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
        self.handle(events)


def main():
    state = os.environ.get("HUB_STATE_DIR")
    if not state:
        sys.exit("нужен HUB_STATE_DIR (плагин запускает хаб)")
    p = Plugin(state)
    while True:
        p.poll()
        time.sleep(1)


if __name__ == "__main__":
    main()
