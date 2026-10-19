"""Процесс плагина «Посты в Telegram»: ведёт очередь постов и шлёт владельцу ровно один неразобранный — следующий уходит,
когда этот закрыт кнопкой ❤️ или 👎 (telegram.pressed). Реплай владельца пост не закрывает: у варианта постов (drafts)
он идёт сессии, у карточки колоды (telegram.replied) — серверу колоды. Закрытие карточки колоды отдаётся серверу колоды
(/api/decide) — без хода сессии, колода лежит — повторяет до часа; закрытие варианта — сообщением сессии. Посты и
команды кладёт CLI (bin/tg-posts) в ящик плагина. Ядро не правит: читает журнал своим смещением."""
import json
import os
import sys
import time
import urllib.error
import urllib.request

from core import catalog, journal, spool

EXT = "tg-posts"   # так его факты подписывает CLI (bin/tg-posts) — по нему бот отдаёт нажатия сюда
NAME = os.environ.get("HUB_EXTENSION", EXT)
LIKE, NOPE = "❤️", "👎"
RETRY, GIVE_UP = 30, 3600   # с: повтор при лежащей колоде, сколько пытаться
KEEP_SENT, KEEP_REVS = 300, 500   # сколько ушедших постов и счётчиков версий помнить


def emit(state, type, data):
    """Факт хабу от имени плагина (ящик ядра): пост в Telegram или сообщение сессии."""
    spool.put(os.path.join(state, "spool"), type, None, data, src=f"ext:{EXT}")


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
            self.st = {"seq": last[-1]["seq"] if last else 0}
            self.shape()
            self.save()
        else:   # пока лежал — догнать по хвосту журнала
            self.shape()
            self.handle(journal.read_tail(self.path))

    def shape(self):
        """jobs — что ждёт колоду (до 0.2.0 лежало в queue), posts — очередь постов, open — единственный пост в Telegram."""
        st = self.st
        st.setdefault("jobs", st.pop("queue", None) or [])
        st.setdefault("posts", [])
        st.setdefault("open", None)
        st.setdefault("sent", {})   # ref → что за пост ушёл (вариант или карточка): по нему узнаём нажатие
        st.setdefault("revs", {})   # ключ поста → сколько раз он уходил (новая версия — новый ref, старая кнопка не в счёт)

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
            self.react(ev)
        if (self.flush() if self.st["jobs"] else False) or changed:
            self.save()

    def react(self, ev):
        """Нажатие и реплай по ref: вариант постов знаем по `sent`, всё остальное (в том числе карточки колоды, ушедшие
        до очереди) — как раньше, колоде."""
        data = ev.get("data") or {}
        if data.get("ext") != EXT or not data.get("ref"):
            return
        info, now_open = self.st["sent"].get(data["ref"]) or {}, self.st["open"]
        if info.get("kind") == "drafts":   # реплай на него бот отдаёт сессии сам; сюда доходит только нажатие
            if ev.get("type") == "telegram.pressed" and now_open and now_open["ref"] == data["ref"] \
                    and data.get("button") in (LIKE, NOPE):
                self.close(data["button"])
            return
        if ev.get("type") == "telegram.pressed" and now_open and now_open["ref"] == data["ref"] \
                and data.get("button") in (LIKE, NOPE):
            return self.close(data["button"])
        req = request(ev)
        if req:
            self.job(*req)

    def job(self, path, body):
        self.st["jobs"].append({"path": path, "body": body, "since": self.clock(), "next": 0})

    def close(self, button):
        """❤️ / 👎 под открытым постом: закрыть, сказать кому надо и отправить следующий."""
        o, self.st["open"] = self.st["open"], None
        if o["kind"] == "deck":
            self.job("/api/decide", {"id": o["ref"], "verdict": "like" if button == LIKE else "reject"})
        else:
            left = len(self.st["posts"])
            tail = f" В очереди ещё {left}." if left else " Очередь пуста — можно присылать следующую."
            what = ("принят — отметь это в файле" if button == LIKE
                    else "категорический слив — убери пост из файла и не возвращайся к нему")
            emit(self.dir, "message.received", {
                "to": o["session"], "channel": "session", "owner": False, "sender": NAME,
                "text": f"tg-posts: владелец поставил {button} посту {o['n']} «{o['title']}» (файл {o['file']}): {what}.{tail}"})
        self.pump()

    def pump(self):
        """В Telegram висит один неразобранный пост: открытого нет — отправить следующий из очереди."""
        if self.st["open"] is not None or not self.st["posts"]:
            return
        item = self.st["posts"].pop(0)
        rev = self.st["revs"][item["key"]] = self.st["revs"].get(item["key"], 0) + 1
        ref = item["ref"][:180] + (f" (v{rev})" if rev > 1 and item["kind"] == "drafts" else "")   # у колоды ref — id карточки
        data = {"text": item["text"], "ref": ref, "buttons": [LIKE, NOPE]}
        for k in ("path", "session"):
            if item.get(k):
                data[k] = item[k]
        emit(self.dir, "telegram.post", data)
        self.st["open"] = {**item, "ref": ref, "at": self.clock()}
        self.st["sent"][ref] = {"kind": item["kind"], "key": item["key"]}
        for d, keep in ((self.st["sent"], KEEP_SENT), (self.st["revs"], KEEP_REVS)):
            while len(d) > keep:
                del d[next(iter(d))]

    def enqueue(self, item):
        """→ 'new' | 'same' | 'revised'. Тот же пост (ключ) — не второй раз: карточка колоды уже тут — пропуск; вариант
        постов — новая версия заменяет ждущую в очереди или открытую (тогда она уходит заново, с кнопками)."""
        key, st = item["key"], self.st
        o = st["open"]
        old = o if o and o["key"] == key else next((p for p in st["posts"] if p["key"] == key), None)
        if old is None:
            if item["kind"] == "deck" and any(j["body"].get("id") == item["ref"] for j in st["jobs"]):
                return "same"   # оценка ещё не дошла до колоды — карточка у неё в списке неоценённых
            st["posts"].append(item)
            return "new"
        if item["kind"] == "deck":
            return "same"
        if old.get("path") and not item.get("path"):
            item["path"], item["img"] = old["path"], old.get("img")   # картинку при правке текста не теряем
        if all(old.get(k) == item.get(k) for k in ("text", "path", "img")):   # img — отпечаток файла: тот же путь, другая картинка — новая версия
            return "same"
        if old is o:
            st["open"] = None
            st["posts"].insert(0, item)
        else:
            st["posts"][st["posts"].index(old)] = item
        return "revised"

    def inbox(self):
        """Посты и команды от CLI: файлы в <плагин>/inbox, по порядку; разобранный файл удаляется после сохранения."""
        d = os.path.join(self.dir, EXT, "inbox")
        try:
            names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
        except OSError:
            return
        for n in names:
            try:
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    cmd = json.load(f)
                if cmd.get("op") == "clear":
                    self.st["posts"], self.st["open"] = [], None
                else:
                    for item in cmd["items"]:
                        self.enqueue(item)
            except (OSError, ValueError, KeyError, TypeError) as e:
                sys.stderr.write(f"tg-posts: {n}: не разобрал ({e})\n")
                os.replace(os.path.join(d, n), os.path.join(d, n + ".bad"))
        if names:
            self.pump()
            self.save()
            for n in names:
                try:
                    os.unlink(os.path.join(d, n))
                except OSError:
                    pass

    def flush(self):
        """По порядку: не принятое ждёт повтора, а следующие — за ним (оценка и реплай к одной карточке не меняются местами)."""
        now, left, changed = self.clock(), [], False
        for job in self.st["jobs"]:
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
        self.st["jobs"] = left
        return changed

    def poll(self):
        self.inbox()
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
