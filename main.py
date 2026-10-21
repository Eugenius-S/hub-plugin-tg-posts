"""Process of the "Posts in Telegram" plugin: keeps the post queue and sends the owner exactly one unreviewed post - the
next one goes out when this one is closed with the ❤️ or 👎 button (telegram.pressed). The owner's reply to a deck card
(telegram.replied) goes to the deck server and does not close the card. A reply to a draft post (drafts) goes to the
session (the bot does that) - the post moves to revision (revising): the place in Telegram is freed, the next post goes
out at once, and the new version of the revised post stands first in the queue, right after the open one. Closing a deck
card is passed to the deck server (/api/decide) - without a session turn; the deck is down - retried for up to an hour;
closing a draft is a message to the session. Posts and commands are put into the plugin's inbox by the CLI
(bin/tg-posts). It does not touch the core: it reads the journal with its own offset.

Texts that go to the owner in Telegram (the "Why?" question and its buttons) are in the hub language
(core.i18n.current(): hub.json language -> owner language -> en), resolved at the moment of sending; any language
without a table here falls back to English. Messages to sessions are always English."""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from core import catalog, journal, spool

EXT = "tg-posts"   # the CLI (bin/tg-posts) signs its facts with this name - the bot passes presses back here by it
NAME = os.environ.get("HUB_EXTENSION", EXT)
LIKE, NOPE = "❤️", "👎"
RETRY, GIVE_UP = 30, 3600   # s: retry while the deck is down, how long to keep trying
KEEP_SENT, KEEP_REVS, KEEP_REVISING, KEEP_SEEN = 300, 500, 100, 3000   # how many sent posts, version counters, posts in revision and shown cards to remember
BADGE_WAIT, BADGE_MAX = 3, 40   # s: how long to wait for the card number from the deck; characters in the number line
WHY_HOLD = 1200   # s: a deck card rejection waits this long for a reason, then goes out without one
WHY_DECK = ["stale", "off-brand", "not-design", "weak-visual", "soft", "dupe", "boring", "changed-mind"]   # reasons of the deck web UI: reason codes in /api/decide, STR key suffixes
WHY_DRAFTS = ["bored", "off-topic", "weak-image", "long", "off-tone"]   # reasons for a draft post: STR key suffixes
KEEP_WHY = 200
HEAD_MAX = 200   # the post caption's first line above an answer (tg-posts reply)


def _cp(*codes):
    return "".join(map(chr, codes))


# Protocol markers of the core (modules/telegram/bot.py), not UI text: when the owner replies to a post that went out
# for a session, the bot appends "(<in reply to> <post> <ref>, <file> `<path>`)" to the message in Russian. They are
# spelled by code points to keep this file English; the words are: "в ответ на", "пост", "файл".
REPLY_MARK = "(" + _cp(0x432, 0x20, 0x43e, 0x442, 0x432, 0x435, 0x442, 0x20, 0x43d, 0x430, 0x20,
                       0x43f, 0x43e, 0x441, 0x442) + " "
REPLY_FILE = ", " + _cp(0x444, 0x430, 0x439, 0x43b) + " `"

STR = {
    "en": {
        "why_ask": "Why 👎? Press a button - or reply in your own words.",
        "why_settle": "Why: {}",
        "why_deck_stale": "Stale",
        "why_deck_off-brand": "Not our voice",
        "why_deck_not-design": "Not design",
        "why_deck_weak-visual": "Weak visual",
        "why_deck_soft": "Blurry",
        "why_deck_dupe": "Dupe",
        "why_deck_boring": "Boring",
        "why_deck_changed-mind": "Changed my mind",
        "why_drafts_bored": "Boring",
        "why_drafts_off-topic": "Off-topic",
        "why_drafts_weak-image": "Weak image",
        "why_drafts_long": "Too long",
        "why_drafts_off-tone": "Not our tone",
    },
    "ru": {
        "why_ask": "Почему 👎? Кнопка — или реплаем своими словами.",
        "why_settle": "Почему: {}",
        "why_deck_stale": "Баян",
        "why_deck_off-brand": "Не наш голос",
        "why_deck_not-design": "Не дизайн",
        "why_deck_weak-visual": "Слабый кадр",
        "why_deck_soft": "Мыльно",
        "why_deck_dupe": "Дубль",
        "why_deck_boring": "Скучно",
        "why_deck_changed-mind": "Передумал",
        "why_drafts_bored": "Скучно",
        "why_drafts_off-topic": "Не по теме",
        "why_drafts_weak-image": "Слабая картинка",
        "why_drafts_long": "Длинно",
        "why_drafts_off-tone": "Не наш тон",
    },
}


def _lang():
    try:
        from core import i18n
        return i18n.current()      # hub language chain: hub.json language -> owner language -> en
    except Exception:
        return (os.environ.get("HUB_LANG") or "en").split("-")[0].lower()


def tr(lang, key, **params):
    """Text of a key in the language (any language without a table - English); {name} placeholders."""
    base = (lang or "en").split("-")[0].lower()
    text = (STR.get(base) or STR["en"]).get(key) or STR["en"][key]
    return text.format(**params) if params else text


def why_buttons(kind, lang):
    """Reason buttons of a "Why?" question, in the language of the moment."""
    keys = WHY_DECK if kind == "deck" else WHY_DRAFTS
    return [tr(lang, f"why_{kind}_{k}") for k in keys]


def why_key(kind, button):
    """STR key suffix of a pressed reason button, whatever language it was sent in (the hub language may have changed
    since); None - not a reason button."""
    for k in WHY_DECK if kind == "deck" else WHY_DRAFTS:
        if any(t.get(f"why_{kind}_{k}") == button for t in STR.values()):
            return k
    return None


def replied_ref(ev, session):
    """ref of the post the owner replied to from Telegram, or None. The bot hands a reply to a draft post to the session as
    a message with the note "(<in reply to> <post> <ref>, <file> `…`)" (see REPLY_MARK) - it marks only posts without a
    session with the telegram.replied fact."""
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
    if REPLY_FILE in ref:
        return ref.split(REPLY_FILE, 1)[0]
    return ref[:-1] if ref.endswith(")") else None   # exactly one parenthesis closes the note; the ref itself may end with "(v2)"


def emit(state, type, data):
    """A fact to the hub on behalf of the plugin (the core's spool): a post in Telegram or a message to a session."""
    spool.put(os.path.join(state, "spool"), type, None, data, src=f"ext:{EXT}")


def deck_url(state):
    ext = (catalog.load_config(state).get("extensions") or {})
    for k in (NAME, EXT):
        v = ext.get(k)
        if isinstance(v, dict) and v.get("deck"):
            return str(v["deck"]).rstrip("/")
    return "http://localhost:7777"


def request(ev):
    """Fact -> (API path, body) or None (not ours, not a deck card)."""
    data = ev.get("data") or {}
    if data.get("ext") != EXT or not data.get("ref"):
        return None
    if ev.get("type") == "telegram.pressed":
        verdict = {LIKE: "like", NOPE: "reject"}.get(data.get("button"))
        return ("/api/decide", {"id": data["ref"], "verdict": verdict}) if verdict else None
    if ev.get("type") == "telegram.replied" and (data.get("text") or "").strip():
        body = {"id": data["ref"], "text": data["text"].strip(), "from": EXT}   # from - the deck revises in the background and returns the card itself (tg-posts deck --id)
        if str(data.get("quote") or "").strip():
            body["quote"] = data["quote"].strip()   # what the owner selected as a quote: a "dot" on it cuts the caption here
        return "/api/direct", body
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
        if self.st is None:   # first start - from the end of the journal: do not repeat old presses
            last = journal.read_tail(self.path)
            self.st = {"seq": last[-1]["seq"] if last else 0}
            self.shape()
            self.save()
        else:   # while it was down - catch up from the journal tail
            self.shape()
            self.handle(journal.read_tail(self.path))

    def shape(self):
        """jobs - what waits for the deck (before 0.2.0 it lay in queue), posts - the post queue, open - the only post in Telegram."""
        st = self.st
        st.setdefault("jobs", st.pop("queue", None) or [])
        st.setdefault("posts", [])
        st.setdefault("open", None)
        st.setdefault("revising", {})   # post key -> the post the owner replied to: went to the session for revision, waits for a new version
        st.setdefault("sent", {})   # ref -> which post went out (draft or card): a press is recognized by it
        st.setdefault("why", {})   # ref of a "Why?" question -> what it is about (deck card or draft post): the reason is recognized by it
        st.setdefault("why_n", 0)
        st.setdefault("seen", {})   # deck card ref -> when it went to the owner: a shown card does not go twice (except a new version after revision)
        st.setdefault("revs", {})   # post key -> how many times it went out (a new version is a new ref, a press under the old one does not count)

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
        """-> True - the deck answered (accepted, or refused on the merits: 404 already rated, 422), False - retry."""
        req = urllib.request.Request(deck_url(self.dir) + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=60):
                return True
        except urllib.error.HTTPError as e:
            why = e.read().decode("utf-8", "replace")[:200]
            e.close()
            sys.stderr.write(f"tg-posts: {path} {body.get('id')}: {e.code} {why}\n")
            return e.code < 500 or e.code == 502   # 502 - an error on the merits (the deck's text), not the network
        except OSError as e:
            sys.stderr.write(f"tg-posts: the deck does not answer ({e}) - will retry\n")
            return False

    def badge(self, ref):
        """The card number in the current batch from the deck ("7 of 21") or "": the deck is silent, slower than BADGE_WAIT, an answer without text - no line."""
        url = deck_url(self.dir) + "/api/badge?" + urllib.parse.urlencode({"id": ref})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=BADGE_WAIT) as r:
                text = json.loads(r.read(4096).decode("utf-8", "replace")).get("text")
        except (OSError, ValueError, AttributeError):
            return ""
        return " ".join(text.split())[:BADGE_MAX] if isinstance(text, str) else ""

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
        """A press and a reply by ref: a draft post is known by `sent`, everything else (including deck cards that went out
        before the queue) - as before, to the deck."""
        data, now_open = ev.get("data") or {}, self.st["open"]
        if data.get("ext") == EXT and data.get("ref") in self.st["why"]:   # an answer to "Why?": a button or a reply in words
            if ev.get("type") == "telegram.pressed":
                self.answer_why(data["ref"], button=data.get("button"))
            elif ev.get("type") == "telegram.replied" and (data.get("text") or "").strip():
                self.answer_why(data["ref"], text=data["text"].strip())
            return
        if now_open and now_open["kind"] == "drafts" and replied_ref(ev, now_open["session"]) == now_open["ref"]:
            return self.revise()
        if data.get("ext") != EXT or not data.get("ref"):
            return
        info = self.st["sent"].get(data["ref"]) or {}
        if info.get("kind") == "drafts":   # the bot hands a reply to it to the session itself; here - a press under the open or revised one
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
            if rev and rev["kind"] == "deck":   # a card in revision: the button under its message closes the card
                return self.close(data["button"], rev)
            # a deck card's ref is one for all versions: rated - a new version in the queue is no longer needed
            self.st["posts"] = [p for p in self.st["posts"] if not (p["kind"] == "deck" and p["ref"] == data["ref"])]
            if data["button"] == NOPE:
                return self.reject_deck(data["ref"])
            self.cancel_held(data["ref"])   # ❤️ after 👎 on the same card: the waiting rejection is not needed
        req = request(ev)
        if req:
            self.job(*req)
            if req[0] == "/api/direct" and now_open and now_open["kind"] == "deck" and now_open["ref"] == data["ref"]:
                self.revise()   # a reply to the open card is a revision: it goes to the deck, the next one goes to the owner at once

    def job(self, path, body):
        self.st["jobs"].append({"path": path, "body": body, "since": self.clock(), "next": 0})

    def ask_why(self, kind, o):
        """After 👎 a "Why?" with reason buttons goes to the owner at once (a reply - in their own words); the queue does not
        wait for it. The bot replaces the question with a "Why: ..." line when answered (settle). The text is in the hub
        language of this moment. -> ref of the question."""
        n = self.st["why_n"] = self.st["why_n"] + 1
        ref = f"why:{o['ref'][:150]}#{n}"
        self.st["why"][ref] = {"kind": kind, "ref": o["ref"], **{k: o[k] for k in ("session", "n", "title", "file") if o.get(k)}}
        while len(self.st["why"]) > KEEP_WHY:
            del self.st["why"][next(iter(self.st["why"]))]
        lang = _lang()
        emit(self.dir, "telegram.post", {"text": tr(lang, "why_ask"), "ref": ref, "settle": tr(lang, "why_settle"),
                                         "buttons": why_buttons(kind, lang)})
        return ref

    def held(self, ref):
        return next((j for j in self.st["jobs"] if j.get("why") and j["body"].get("id") == ref), None)

    def reject_deck(self, ref):
        """👎 on a deck card: the rejection waits for a reason (the deck accepts it together with the rejection) for no longer
        than WHY_HOLD, then goes without it. A second 👎 on the same card while the question is open sends no second question."""
        if self.held(ref):
            return
        now = self.clock()
        self.st["jobs"].append({"path": "/api/decide", "body": {"id": ref, "verdict": "reject"}, "since": now,
                                "next": now + WHY_HOLD, "why": self.ask_why("deck", {"ref": ref})})

    def cancel_held(self, ref):
        job = self.held(ref)
        if job:
            self.st["jobs"].remove(job)
            self.st["why"].pop(job["why"], None)

    def answer_why(self, ref, button=None, text=None):
        """The reason for 👎: to a deck card - reason (button) or note via /api/comment (words) and the rejection goes; to a
        draft post - a message to the session that writes it (the reason in English). An unknown button does not close the
        question; a button is known in any language of the table (the language may have changed since it was sent)."""
        w = self.st["why"].get(ref)
        if not w:
            return
        deck = w["kind"] == "deck"
        code = why_key(w["kind"], button)   # deck: the reason code for /api/decide; draft: the key of its English text
        if not (text or code):
            return
        del self.st["why"][ref]
        if deck:
            job = next((j for j in self.st["jobs"] if j.get("why") == ref), None)
            if job is None:   # the rejection already went without a reason (the deck waited longer than WHY_HOLD)
                return
            if text:
                self.st["jobs"].insert(self.st["jobs"].index(job),
                                       {"path": "/api/comment", "body": {"id": w["ref"], "comment": text},
                                        "since": self.clock(), "next": 0})
            else:
                job["body"]["reason"] = code
            del job["why"]
            job["next"] = 0
            return
        reason = text or tr("en", f"why_drafts_{code}")
        emit(self.dir, "message.received", {
            "to": w["session"], "channel": "session", "owner": False, "sender": NAME,
            "text": f"tg-posts: the reason the owner rejected post {w['n']} \"{w['title']}\" (file {w['file']}): "
                    f"{reason}. Take it into account in the next posts; do not bring the post back."})

    def revise(self):
        """A reply to the open draft post: it goes to revision, and the next one from the queue goes to the owner (while the
        session makes a new version). The new version stands first in the queue (enqueue)."""
        o, self.st["open"] = self.st["open"], None
        rv = self.st["revising"]
        rv.pop(o["key"], None)
        rv[o["key"]] = o
        while len(rv) > KEEP_REVISING:
            del rv[next(iter(rv))]
        self.pump()

    def close(self, button, o=None):
        """❤️ / 👎 under the open post (or under a post in revision - o): close it, tell whoever needs to know; the open one - send the next."""
        if o is None:
            o, self.st["open"] = self.st["open"], None
        else:
            self.st["revising"].pop(o["key"], None)
        if o["kind"] == "deck":
            if button == LIKE:
                self.cancel_held(o["ref"])
                self.job("/api/decide", {"id": o["ref"], "verdict": "like"})
            else:
                self.reject_deck(o["ref"])
        else:
            left = len(self.st["posts"])
            tail = f" {left} more in the queue." if left else " The queue is empty - you can send the next one."
            what = ("approved - mark it in the file" if button == LIKE
                    else "rejected outright - remove the post from the file and do not return to it")
            emit(self.dir, "message.received", {
                "to": o["session"], "channel": "session", "owner": False, "sender": NAME,
                "text": f"tg-posts: the owner gave post {o['n']} \"{o['title']}\" (file {o['file']}) a {button}: {what}.{tail}"})
            if button == NOPE:
                self.ask_why("drafts", o)
        self.pump()

    def close_quiet(self, ref):
        """The post is already closed where it was rated (deck, page): remove it from the queue, the open one and revision without a rating to the deck;
        the open one - the next goes out on pump."""
        self.cancel_held(ref)
        self.st["posts"] = [p for p in self.st["posts"] if p["ref"] != ref]
        self.st["revising"] = {k: p for k, p in self.st["revising"].items() if p["ref"] != ref}
        if self.st["open"] and self.st["open"]["ref"] == ref:
            self.st["open"] = None

    def pump(self):
        """One unreviewed post hangs in Telegram: nothing open - send the next one from the queue."""
        if self.st["open"] is not None or not self.st["posts"]:
            return
        item = self.st["posts"].pop(0)
        rev = self.st["revs"][item["key"]] = self.st["revs"].get(item["key"], 0) + 1
        ref = item["ref"][:180] + (f" (v{rev})" if rev > 1 and item["kind"] == "drafts" else "")   # for the deck the ref is the card id
        self.emit_post(item, ref)
        self.st["open"] = {**item, "ref": ref, "at": self.clock()}
        self.st["sent"][ref] = {"kind": item["kind"], "key": item["key"]}
        if item["kind"] == "deck":
            self.st["seen"].pop(ref, None)
            self.st["seen"][ref] = self.clock()
        for d, keep in ((self.st["sent"], KEEP_SENT), (self.st["revs"], KEEP_REVS), (self.st["seen"], KEEP_SEEN)):
            while len(d) > keep:
                del d[next(iter(d))]

    def emit_post(self, item, ref):
        """A post in Telegram: caption, ❤️ 👎 buttons, file and session; a deck card's number comes from the deck at the moment of sending."""
        data = {"text": item["text"], "ref": ref, "buttons": [LIKE, NOPE]}
        if item["kind"] == "deck":   # the number is taken at the moment of sending: fresh for deck --id and after revision
            badge = self.badge(item["ref"])
            if badge:
                data["text"] = f"{badge}\n\n{item['text']}" if item["text"] else badge
        for k in ("path", "session"):
            if item.get(k):
                data[k] = item[k]
        emit(self.dir, "telegram.post", data)

    def again(self, ref=""):
        """`tg-posts again`: the open post goes to Telegram once more - a new message at the bottom, with the same ref. The bot recognizes buttons and
        replies by ref, so the old and the new message are one and the same post. The state (open, queue, seen, revs) does not
        change: the next one is not released, the version is not new. -> True - sent; nothing open or the ref is not its - False."""
        o = self.st["open"]
        if o is None or ref not in ("", o["ref"], o["key"]):
            return False
        self.emit_post(o, o["ref"])
        return True

    def answer(self, ref, text):
        """`tg-posts reply`: an answer to the owner's question about a post - to Telegram as a reply to the post's latest
        message (reply_text: the bot finds it by ref, a reply to the answer comes back here by the same ref). text - the whole
        answer with the post's caption first: the bot sends it when the post is not in the chat (or does not know reply_text).
        The queue does not move."""
        text = text.strip()
        if not ref or not text:
            return False
        o = next((p for p in (self.st["open"], *self.st["revising"].values(), *self.st["posts"])
                  if p and ref in (p["ref"], p["key"])), None)
        head = next((ln.strip() for ln in str((o or {}).get("text") or "").splitlines() if ln.strip()), "") or ref
        emit(self.dir, "telegram.post", {"text": f"{head[:HEAD_MAX]}\n\n{text}", "reply_text": text,
                                         "ref": o["ref"] if o else ref})
        return True

    def enqueue(self, item):
        """-> 'new' | 'same' | 'revised'. The same post (key) - not a second time: a deck card already here - skipped; a draft
        post - the new version replaces the one waiting in the queue or the open one (then it goes out again, with buttons); the
        version of a revised one (the owner replied to it) stands first in the queue, not displacing the open one. A deck card in revision
        returns only from the deck (fresh - `tg-posts deck --id`), even if unchanged; a plain `deck` does not take it."""
        key, st, fresh = item["key"], self.st, item.pop("fresh", False)
        o = st["open"]
        old = o if o and o["key"] == key else next((p for p in st["posts"] if p["key"] == key), None)
        if old is None and key in st["revising"] and item["kind"] == "deck":
            if not fresh:
                return "same"
            del st["revising"][key]
            st["posts"].insert(0, item)
            return "revised"
        if old is None and key in st["revising"] and item["kind"] == "drafts":   # a revised one: the new version - first after the open one
            old = st["revising"][key]
            if old.get("path") and not item.get("path"):
                item["path"], item["img"] = old["path"], old.get("img")
            if all(old.get(k) == item.get(k) for k in ("text", "path", "img")):
                return "same"
            del st["revising"][key]
            st["posts"].insert(0, item)
            return "revised"
        if fresh and item["kind"] == "deck" and old is not None and old is not o:   # waits in the queue - came back edited: first
            st["posts"].remove(old)
            st["posts"].insert(0, item)
            return "revised"
        if old is None:
            if item["kind"] == "deck" and any(j["body"].get("id") == item["ref"] for j in st["jobs"]):
                return "same"   # the rating has not reached the deck yet - the card is still in its list of unrated ones
            if item["kind"] == "deck" and not fresh and self.shown(item["ref"]):
                return "same"   # the owner has already seen it (rated, replied, clear reset the queue): not sent a second time
            if item["kind"] == "deck" and fresh:   # edited by the deck but not in revision (the reply was to an unopened card, a restart): first, not at the tail
                st["posts"].insert(0, item)
                return "revised"
            st["posts"].append(item)
            return "new"
        if item["kind"] == "deck":
            if fresh and old is o and any(old.get(k) != item.get(k) for k in ("text", "path", "img")):
                self.cancel_held(o["ref"])   # the open card came back edited from the deck: the old one is closed without a rating, the new one goes at once
                st["open"] = None
                st["posts"].insert(0, item)
                return "revised"
            return "same"
        if old.get("path") and not item.get("path"):
            item["path"], item["img"] = old["path"], old.get("img")   # do not lose the picture when only the text is edited
        if all(old.get(k) == item.get(k) for k in ("text", "path", "img")):   # img is the file fingerprint: the same path, another picture - a new version
            return "same"
        if old is o:
            st["open"] = None
            st["posts"].insert(0, item)
        else:
            st["posts"][st["posts"].index(old)] = item
        return "revised"

    def shown(self, ref):
        """The deck card has already gone to the owner: seen remembers longer, sent is a spare trail (the state before 0.4.1 did not keep seen)."""
        return ref in self.st["seen"] or (self.st["sent"].get(ref) or {}).get("kind") == "deck"

    def inbox(self):
        """Posts and commands from the CLI: files in <plugin>/inbox, in order; a parsed file is deleted after saving."""
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
                elif cmd.get("op") == "again":
                    self.again(str(cmd.get("ref") or ""))
                elif cmd.get("op") == "reply":
                    self.answer(str(cmd.get("ref") or ""), str(cmd.get("text") or ""))
                elif cmd.get("op") == "close":
                    self.close_quiet(str(cmd.get("ref") or ""))
                else:
                    for item in cmd["items"]:
                        self.enqueue(item)
            except (OSError, ValueError, KeyError, TypeError) as e:
                sys.stderr.write(f"tg-posts: {n}: could not parse ({e})\n")
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
        """In order: one not accepted waits for a retry, and the following ones wait behind it (a rating and a reply to one card do not swap places)."""
        now, left, behind, changed = self.clock(), [], set(), False
        for job in self.st["jobs"]:
            if job["body"].get("id") in behind or job["next"] > now:
                left.append(job)   # waits for a retry or a reason - the following ones to the same card behind it, the others go
                behind.add(job["body"].get("id"))
            elif self.post(job["path"], job["body"]):
                changed = True
                self.st["why"].pop(job.get("why"), None)   # went without a reason: the question can no longer change anything
            elif now - job["since"] < GIVE_UP:
                left.append({**job, "next": now + RETRY})
                behind.add(job["body"].get("id"))
                changed = True
            else:
                sys.stderr.write(f"tg-posts: {job['path']} {job['body'].get('id')}: the deck did not answer within an hour - giving up\n")
                changed = True
        self.st["jobs"] = left
        return changed

    def poll(self):
        self.inbox()
        ino = journal.identity(self.path)
        if ino != self.ino or self.size() < self.offset:   # the journal was started anew - read the new one from the start
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
        sys.exit("HUB_STATE_DIR is required (the hub sets it when it runs the plugin)")
    p = Plugin(state)
    while True:
        p.poll()
        time.sleep(1)


if __name__ == "__main__":
    main()
