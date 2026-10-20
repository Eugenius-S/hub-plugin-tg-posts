"""Процесс плагина «Посты в Telegram»: ведёт очередь постов и шлёт владельцу ровно один неразобранный — следующий уходит,
когда этот закрыт кнопкой ❤️ или 👎 (telegram.pressed). Реплай владельца на карточку колоды (telegram.replied) идёт серверу
колоды и карточку не закрывает. Реплай на вариант постов (drafts) идёт сессии (это делает бот) — пост уходит в докрутку
(revising): место в Telegram освобождается, следующий пост уходит сразу, а новая версия докрученного встаёт в очереди
первой, сразу за открытым. Закрытие карточки колоды отдаётся серверу колоды
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
KEEP_SENT, KEEP_REVS, KEEP_REVISING = 300, 500, 100   # сколько ушедших постов, счётчиков версий и постов в докрутке помнить
REPLY_MARK = "(в ответ на пост "   # так бот помечает реплай на пост, ушедший сессии (modules/telegram/bot.py)


def replied_ref(ev, session):
    """ref поста, на который владелец ответил из Telegram, или None. Реплай на вариант постов бот отдаёт сессии сообщением
    с пометкой «(в ответ на пост <ref>, файл …)» — фактом telegram.replied он помечает только посты без сессии."""
    data = ev.get("data") or {}
    if ev.get("type") == "telegram.replied" and data.get("ext") == EXT:
        return data.get("ref")
    if ev.get("type") != "message.received" or not str(ev.get("src") or "").startswith("telegram") \
            or not data.get("tg") or data.get("to") != session:
        return None
    text = data.get("text") or ""
    at = text.rfind(REPLY_MARK)
    if at < 0:
        return None
    ref = text[at + len(REPLY_MARK):]
    if ", файл `" in ref:
        return ref.split(", файл `", 1)[0]
    return ref[:-1] if ref.endswith(")") else None   # ровно одна скобка — закрывающая пометку; ref сам может кончаться на «(v2)»


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
        return "/api/direct", {"id": data["ref"], "text": data["text"].strip(), "from": EXT}   # from — колода правит в фоне и сама вернёт карточку (tg-posts deck --id)
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
        st.setdefault("revising", {})   # ключ поста → пост, на который владелец ответил: ушёл сессии в докрутку, ждёт новой версии
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
        data, now_open = ev.get("data") or {}, self.st["open"]
        if now_open and now_open["kind"] == "drafts" and replied_ref(ev, now_open["session"]) == now_open["ref"]:
            return self.revise()
        if data.get("ext") != EXT or not data.get("ref"):
            return
        info = self.st["sent"].get(data["ref"]) or {}
        if info.get("kind") == "drafts":   # реплай на него бот отдаёт сессии сам; здесь — нажатие под открытым или докручиваемым
            if ev.get("type") == "telegram.pressed" and data.get("button") in (LIKE, NOPE):
                if now_open and now_open["ref"] == data["ref"]:
                    self.close(data["button"])
                else:
                    rev = next((o for o in self.st["revising"].values() if o["ref"] == data["ref"]), None)
                    if rev:
                        self.close(data["button"], rev)
            return
        if ev.get("type") == "telegram.pressed" and data.get("button") in (LIKE, NOPE):
            if now_open and now_open["ref"] == data["ref"]:
                return self.close(data["button"])
            rev = self.st["revising"].get(data["ref"])
            if rev and rev["kind"] == "deck":   # карточка в докрутке: кнопка под её сообщением закрывает карточку
                return self.close(data["button"], rev)
            # ref карточки колоды один на все версии: оценили — новая версия в очереди больше не нужна
            self.st["posts"] = [p for p in self.st["posts"] if not (p["kind"] == "deck" and p["ref"] == data["ref"])]
        req = request(ev)
        if req:
            self.job(*req)
            if req[0] == "/api/direct" and now_open and now_open["kind"] == "deck" and now_open["ref"] == data["ref"]:
                self.revise()   # реплай на открытую карточку — докрутка: она уходит колоде, владельцу сразу следующая

    def job(self, path, body):
        self.st["jobs"].append({"path": path, "body": body, "since": self.clock(), "next": 0})

    def revise(self):
        """Реплай на открытый вариант постов: он идёт в докрутку, а владельцу — следующий из очереди (пока сессия делает новую
        версию). Новая версия встанет в очереди первой (enqueue)."""
        o, self.st["open"] = self.st["open"], None
        rv = self.st["revising"]
        rv.pop(o["key"], None)
        rv[o["key"]] = o
        while len(rv) > KEEP_REVISING:
            del rv[next(iter(rv))]
        self.pump()

    def close(self, button, o=None):
        """❤️ / 👎 под открытым постом (или под постом в докрутке — o): закрыть, сказать кому надо; открытый — отправить следующий."""
        if o is None:
            o, self.st["open"] = self.st["open"], None
        else:
            self.st["revising"].pop(o["key"], None)
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
        постов — новая версия заменяет ждущую в очереди или открытую (тогда она уходит заново, с кнопками); версия
        докрученного (владелец ответил на него) встаёт в очереди первой, не вытесняя открытый. Карточка колоды в докрутке
        возвращается только от колоды (fresh — `tg-posts deck --id`), даже если не изменилась; обычный `deck` её не берёт."""
        key, st, fresh = item["key"], self.st, item.pop("fresh", False)
        o = st["open"]
        old = o if o and o["key"] == key else next((p for p in st["posts"] if p["key"] == key), None)
        if old is None and key in st["revising"] and item["kind"] == "deck":
            if not fresh:
                return "same"
            del st["revising"][key]
            st["posts"].insert(0, item)
            return "revised"
        if old is None and key in st["revising"] and item["kind"] == "drafts":   # докрученный: новая версия — первой за открытым
            old = st["revising"][key]
            if old.get("path") and not item.get("path"):
                item["path"], item["img"] = old["path"], old.get("img")
            if all(old.get(k) == item.get(k) for k in ("text", "path", "img")):
                return "same"
            del st["revising"][key]
            st["posts"].insert(0, item)
            return "revised"
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
                    self.st["posts"], self.st["open"], self.st["revising"] = [], None, {}
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
