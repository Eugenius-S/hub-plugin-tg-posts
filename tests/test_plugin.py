"""The CLI puts posts and commands into the plugin's inbox; the process sends one post to Telegram (telegram.post), the next one after ❤️ / 👎,
passes closing to the deck in order, retrying while it is down, and does not repeat after a restart. Texts to the owner are en/ru."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import journal, spool  # noqa: E402

import main  # noqa: E402

_loader = importlib.machinery.SourceFileLoader("tgposts_cli", os.path.join(ROOT, "plugins", "tg-posts", "bin", "tg-posts"))
_spec = importlib.util.spec_from_loader("tgposts_cli", _loader)
cli = importlib.util.module_from_spec(_spec)
_loader.exec_module(cli)

DRAFTS = """# Drafts

## 1. First
Text one.
Translation.

---

## 2) Second post
Text two.

## 3. Third
Text three.
"""


# The note the core's Telegram bot (modules/telegram/bot.py) appends to a reply to a post that went out for a session. It is
# a protocol marker the plugin recognizes, so the tests keep it byte for byte (Russian, deliberately).
REPLY_NOTE = "(в ответ на пост {ref}, файл `{file}`)"
REPLY_SHORT = "(в ответ на пост {ref})"


class Deck:
    """A fake deck server: /api/deck, /media/<file>, POST requests go to calls."""
    def __init__(self, cards):
        self.calls, self.down, self.badges, self.badge = [], False, [], None   # badge - the /api/badge answer (None - the deck does not know it)
        self.cards, self.silent = cards, False   # cards - the unrated list /api/deck answers with; silent - /api/deck does not answer (503)
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path == "/api/deck":
                    if me.silent:
                        return self.reply(503, b"{}")
                    return self.reply(200, json.dumps({"cards": me.cards, "liked": 0}).encode())
                if self.path.startswith("/media/"):
                    if self.path.endswith(".mp4"):
                        return self.reply(200, b"\x00\x00\x00 ftypmp4video")
                    return self.reply(200, b"\x89PNG\r\n\x1a\nthumb")
                if self.path.startswith("/api/badge"):
                    me.badges.append(self.path)
                    if me.badge is None:
                        return self.reply(404, b"{}")
                    return self.reply(200, json.dumps(me.badge).encode())
                self.reply(404, b"{}")

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                if me.down:
                    return self.reply(503, b"{}")
                me.calls.append((self.path, body))
                self.reply(404 if body.get("id") == "gone" else 200, b'{"ok": true}')

            def reply(self, code, raw):
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tgposts-")
        self.env = {k: os.environ.get(k) for k in ("HUB_STATE_DIR", "HUB_CONFIG", "HUB_SESSION")}
        os.environ["HUB_STATE_DIR"] = self.dir   # never the live hub - always, not setdefault
        os.environ["HUB_CONFIG"] = os.path.join(self.dir, "hub.json")
        os.environ["HUB_SESSION"] = "smm"
        self.j = journal.Journal(os.path.join(self.dir, "journal.jsonl"))
        self.j.append("session.requested", "smm", {"cwd": "/w", "session_id": "s"})

    def tearDown(self):
        for k, v in self.env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        shutil.rmtree(self.dir, ignore_errors=True)

    def taken(self):
        d = os.path.join(self.dir, "spool")
        out = [e for _, e in spool.take(d)]
        for n in os.listdir(d) if os.path.isdir(d) else []:
            os.unlink(os.path.join(d, n))
        return out


class Cli(Base):
    def inbox(self):
        d = os.path.join(self.dir, "tg-posts", "inbox")
        out = []
        for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            with open(os.path.join(d, n), encoding="utf-8") as f:
                out.append(json.load(f))
        return out

    def test_drafts_become_one_queued_post_per_section_for_this_session(self):
        md = os.path.join(self.dir, "drafts-2026-10-19.md")
        with open(md, "w", encoding="utf-8") as f:
            f.write(DRAFTS)
        pic = os.path.join(self.dir, "card.png")
        with open(pic, "wb") as f:
            f.write(b"png")
        cli.main(["drafts", md, "--image", f"2={pic}", "--only", "1,2"])
        (cmd,) = self.inbox()
        self.assertEqual(self.taken(), [])   # the CLI does not send to Telegram itself - only to the plugin's queue
        first, second = cmd["items"]
        self.assertEqual(first["text"], "1. First\n\nText one.\nTranslation.")   # without the trailing "---"
        self.assertEqual((first["kind"], first["key"], first["n"], first["title"]), ("drafts", f"{md}#1", 1, "First"))
        self.assertEqual((first["ref"], first["session"], first["file"]), ("drafts-2026-10-19.md #1 First", "smm", md))
        self.assertNotIn("path", first)
        self.assertEqual((second["ref"], second["path"]), ("drafts-2026-10-19.md #2 Second post", pic))

    def test_overwritten_picture_is_a_revision_and_the_cli_does_not_lie_about_unchanged(self):
        md = os.path.join(self.dir, "d.md")
        with open(md, "w", encoding="utf-8") as f:
            f.write(DRAFTS)
        pic = os.path.join(self.dir, "card.png")

        def run():
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.main(["drafts", md, "--only", "2", "--image", f"2={pic}"])
            return buf.getvalue()

        def write(raw):
            with open(pic, "wb") as f:
                f.write(raw)

        def plugin_state(items):   # as if the plugin had already parsed the inbox and held the post open
            os.makedirs(os.path.join(self.dir, "tg-posts"), exist_ok=True)
            with open(os.path.join(self.dir, "tg-posts", "state.json"), "w", encoding="utf-8") as f:
                json.dump({"open": items[0], "posts": []}, f)

        write(b"one")
        self.assertIn("queued: 1 post", run())
        first = self.inbox()[0]["items"][0]
        plugin_state([first])
        self.assertIn("no changes", run())   # nothing changed - the session learns that there was nothing to send
        write(b"two")   # the same path, another picture
        out = run()
        self.assertIn("queued: 1 post", out)
        self.assertNotIn("no changes", out)
        second = self.inbox()[-1]["items"][0]
        self.assertEqual(first["path"], second["path"])
        self.assertNotEqual(first["img"], second["img"])
        self.assertEqual(second["img"], cli.fingerprint(pic))

    def test_deck_queues_unrated_cards_with_their_thumbs(self):
        deck = Deck([{"id": "reddit:t3_a/b", "caption": "Line\nmore", "caption_ru": "Translated line", "thumb": "a.thumb.jpg"},
                     {"id": "arena:2", "caption": "Two"}])
        try:
            cli.main(["deck", "--url", deck.url, "--count", "5"])
        finally:
            deck.server.shutdown()
        (cmd,) = self.inbox()
        a, b = cmd["items"]
        self.assertEqual((a["kind"], a["key"], a["ref"], a["text"]), ("deck", "reddit:t3_a/b", "reddit:t3_a/b", "Line\nmore\n\nTranslated line"))
        self.assertTrue(a["path"].startswith(self.dir))
        with open(a["path"], "rb") as f:
            self.assertTrue(f.read().endswith(b"thumb"))
        self.assertNotIn("path", b)
        self.assertNotIn("session", a)   # presses and replies - to the plugin, not the session

    def test_deck_card_with_local_video_goes_as_video_not_thumb(self):
        deck = Deck([{"id": "c:1", "caption": "Roof", "thumb": "c.thumb.jpg", "local_video": "c.roof.mp4"},
                     {"id": "c:2", "caption": "Pic", "thumb": "d.thumb.jpg"}])
        try:
            cli.main(["deck", "--url", deck.url, "--count", "5"])
        finally:
            deck.server.shutdown()
        (cmd,) = self.inbox()
        a, b = cmd["items"]
        self.assertTrue(a["path"].endswith("c.roof.mp4"))
        with open(a["path"], "rb") as f:
            self.assertTrue(f.read().endswith(b"mp4video"))
        self.assertTrue(b["path"].endswith("d.thumb.jpg"))

    def test_deck_card_whose_video_does_not_download_falls_back_to_thumb(self):
        deck = Deck([{"id": "c:1", "caption": "Roof", "thumb": "c.thumb.jpg", "local_video": "c.roof.mp4"}])
        cli.VIDEO_MAX = 4   # the downloaded one is over the Bot API limit - as a picture
        try:
            cli.main(["deck", "--url", deck.url])
        finally:
            deck.server.shutdown()
            cli.VIDEO_MAX = 50 * 1024 * 1024
        (cmd,) = self.inbox()
        self.assertTrue(cmd["items"][0]["path"].endswith("c.thumb.jpg"))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "tg-posts", "media", "c.roof.mp4")))

    def test_deck_by_id_queues_those_cards_as_fresh_even_if_not_among_the_first_count(self):
        cards = [{"id": f"c:{i}", "caption": f"Card {i}", "thumb": f"{i}.jpg"} for i in range(5)]
        deck = Deck(cards)
        try:
            cli.main(["deck", "--url", deck.url, "--count", "1", "--id", "c:3", "c:1"])
        finally:
            deck.server.shutdown()
        (cmd,) = self.inbox()
        self.assertEqual([it["ref"] for it in cmd["items"]], ["c:3", "c:1"])
        self.assertTrue(all(it["fresh"] and it["kind"] == "deck" for it in cmd["items"]))
        self.assertTrue(cmd["items"][0]["path"].startswith(self.dir))

    def test_deck_by_id_takes_a_new_picture_under_the_same_name(self):
        deck = Deck([{"id": "c:1", "caption": "Card", "thumb": "x.jpg"}])
        media = os.path.join(self.dir, "tg-posts", "media")
        os.makedirs(media)
        with open(os.path.join(media, "x.jpg"), "wb") as f:
            f.write(b"stale")
        try:
            cli.main(["deck", "--url", deck.url, "--id", "c:1"])
        finally:
            deck.server.shutdown()
        (cmd,) = self.inbox()
        with open(cmd["items"][0]["path"], "rb") as f:
            self.assertTrue(f.read().endswith(b"thumb"))   # the picture from the server, not the old file from the cache

    def test_deck_by_id_with_unknown_id_says_so_and_queues_the_rest(self):
        deck = Deck([{"id": "c:1", "caption": "Card"}])
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                cli.main(["deck", "--url", deck.url, "--id", "c:1", "nope"])
            self.assertIn("nope", buf.getvalue())
            with self.assertRaises(SystemExit):
                cli.main(["deck", "--url", deck.url, "--id", "nope"])
        finally:
            deck.server.shutdown()
        self.assertEqual([it["ref"] for it in self.inbox()[0]["items"]], ["c:1"])

    def test_status_and_clear(self):
        with self.assertRaises(SystemExit):
            cli.main(["status"])   # the plugin has not been started yet
        os.makedirs(os.path.join(self.dir, "tg-posts"))
        with open(os.path.join(self.dir, "tg-posts", "state.json"), "w", encoding="utf-8") as f:
            json.dump({"open": {"ref": "a #1 One", "at": 1000.0}, "posts": [{"ref": "a #2 Two"}], "jobs": []}, f)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["status"])
        self.assertIn("open session post: a #1 One", buf.getvalue())
        self.assertIn("in the queue: 1\n  a #2 Two", buf.getvalue())
        cli.main(["clear"])
        self.assertEqual(self.inbox(), [{"op": "clear"}])

    def test_status_shows_posts_in_revision_and_the_cli_knows_them(self):
        os.makedirs(os.path.join(self.dir, "tg-posts"))
        rev = {"kind": "drafts", "key": "/w/d.md#1", "ref": "d.md #1 Post", "text": "1. Post\n\ntext 1", "session": "smm"}
        with open(os.path.join(self.dir, "tg-posts", "state.json"), "w", encoding="utf-8") as f:
            json.dump({"open": {"ref": "a #2 Two", "at": 1000.0}, "posts": [], "revising": {"/w/d.md#1": rev}}, f)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["status"])
        self.assertIn("in revision: 1\n  d.md #1 Post", buf.getvalue())
        same = {"kind": "drafts", "key": "/w/d.md#1", "text": "1. Post\n\ntext 1"}
        new = {"kind": "drafts", "key": "/w/d.md#1", "text": "1. Post\n\nshort"}
        self.assertEqual(cli.verdicts(self.dir, [same, new]), ["same", "revised"])

    def test_deck_url_comes_from_hub_json_of_the_plugin(self):
        with open(os.environ["HUB_CONFIG"], "w", encoding="utf-8") as f:
            json.dump({"extensions": {"Eugenius-S:tg-posts": {"deck": "http://deck:1/"}}}, f)
        self.assertEqual(cli.deck_url(cli.argparse.Namespace(url=None), self.dir), "http://deck:1")


# 2026-10-28 is a Wednesday; the hub zone is UTC (hub.json has no timezone)
WED_NOON, WED_23, THU_0829, THU_0830 = 1793188800.0, 1793228400.0, 1793262540.0, 1793262600.0
WED_1959, WED_2000 = 1793217540.0, 1793217600.0
FRI_19, FRI_21 = 1793386800.0, 1793394000.0
SAT_10, SAT_12, SAT_1230, SAT_20 = 1793440800.0, 1793448000.0, 1793449800.0, 1793476800.0
SUN_1159, SUN_12, SUN_13 = 1793534340.0, 1793534400.0, 1793538000.0
MON_0829, MON_0830 = 1793608140.0, 1793608200.0


class Process(Base):
    def setUp(self):
        super().setUp()
        self.deck = Deck([])
        with open(os.environ["HUB_CONFIG"], "w", encoding="utf-8") as f:
            json.dump({"extensions": {"tg-posts": {"deck": self.deck.url}}}, f)
        self.now = WED_NOON   # a weekday daytime: no quiet time
        self.lang = mock.patch.object(main, "_lang", lambda: "en")   # the hub language is pinned: the tests do not read a live hub
        self.lang.start()

    def tearDown(self):
        self.lang.stop()
        self.deck.server.shutdown()
        super().tearDown()

    def plugin(self):
        return main.Plugin(self.dir, clock=lambda: self.now)

    def press(self, ref, button, ext="tg-posts"):
        self.j.append("telegram.pressed", None, {"ext": ext, "ref": ref, "button": button}, src="telegram")

    def test_presses_and_replies_go_to_the_deck_in_order_once(self):
        p = self.plugin()
        self.press("reddit:t3_x", "❤️")
        self.press("arena:1", "👎")
        self.press("arena:1", "❤️", ext="other")   # another extension
        self.j.append("telegram.replied", None, {"ext": "tg-posts", "ref": "arena:1", "text": " find the author "},
                      src="telegram")
        p.poll()
        self.now += main.WHY_HOLD   # the rejection waits for a reason - a reply to the same card goes behind it
        p.poll()
        self.assertEqual(self.deck.calls, [
            ("/api/decide", {"id": "reddit:t3_x", "verdict": "like"}),
            ("/api/decide", {"id": "arena:1", "verdict": "reject"}),
            ("/api/direct", {"id": "arena:1", "text": "find the author", "from": "tg-posts"})])
        self.plugin().poll()   # a restart does not repeat it
        self.assertEqual(len(self.deck.calls), 3)

    def test_deck_down_retries_later_and_keeps_the_order(self):
        p = self.plugin()
        self.deck.down = True
        self.press("a", "❤️")
        self.press("gone", "👎")   # 404 - the card is already rated: do not retry
        self.press("gone", "Dupe", ext="nobody")
        p.poll()
        self.now += main.WHY_HOLD
        p.poll()
        self.assertEqual(self.deck.calls, [])
        self.deck.down = False
        p.poll()
        self.assertEqual(self.deck.calls, [])   # a pause before the retry
        self.now += main.RETRY
        p.poll()
        self.assertEqual([b["id"] for _, b in self.deck.calls], ["a", "gone"])
        self.now += main.RETRY
        p.poll()
        self.assertEqual(len(self.deck.calls), 2)

    def queue(self, *items):
        self.listed(*items)
        cli.put(self.dir, {"op": "post", "items": list(items)})

    def listed(self, *items):
        """The deck lists the queued cards as unrated (as it does for a card the CLI has just taken from it)."""
        have = {c["id"] for c in self.deck.cards}
        self.deck.cards += [{"id": i["ref"]} for i in items if i["kind"] == "deck" and i["ref"] not in have]

    @staticmethod
    def card(i, **kw):
        return {"kind": "deck", "key": i, "ref": i, "text": f"card {i}", **kw}

    @staticmethod
    def draft(n, text=None, **kw):
        return {"kind": "drafts", "key": f"/w/d.md#{n}", "ref": f"d.md #{n} Post", "text": text or f"{n}. Post\n\ntext {n}",
                "session": "smm", "file": "/w/d.md", "n": n, "title": "Post", **kw}

    def posts(self):
        return [x for x in self.sent() if not x["ref"].startswith("why:")]

    def sent(self):
        return [e["data"] for e in self.taken() if e["type"] == "telegram.post"]

    def whys(self):
        """The "Why?" questions after 👎 (what the bot will show the owner)."""
        return [x for x in self.sent() if x["ref"].startswith("why:")]

    def session_said(self):
        return [e["data"]["text"] for e in self.taken() if e["type"] == "message.received"]

    def test_queue_shows_one_card_and_the_next_only_after_the_button(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"), self.card("c"))
        p.poll()
        (a,) = self.posts()
        self.assertEqual((a["ref"], a["text"], a["buttons"]), ("a", "card a", ["❤️", "👎"]))
        p.poll()
        self.assertEqual(self.posts(), [])   # a is not closed - b waits
        self.press("a", "❤️")
        p.poll()
        (b,) = self.posts()
        self.assertEqual(b["ref"], "b")
        self.press("b", "👎")
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["c"])
        self.assertEqual(self.deck.calls, [("/api/decide", {"id": "a", "verdict": "like"})])   # the rejection waits for a reason
        self.now += main.WHY_HOLD
        p.poll()
        self.assertEqual(self.deck.calls, [("/api/decide", {"id": "a", "verdict": "like"}),
                                           ("/api/decide", {"id": "b", "verdict": "reject"})])

    def test_session_post_goes_at_once_even_with_a_deck_card_open_and_the_next_one_waits_for_it(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"), self.card("c"))
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["a"])
        self.queue(self.draft(1), self.draft(2))   # a news post from a session while the deck card hangs
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["d.md #1 Post"])   # at once, the card stays open; the second post waits for the first
        for ref, want in (("d.md #1 Post", "d.md #2 Post"), ("a", "b")):
            self.press(ref, "❤️")
            p.poll()
            self.assertEqual([x["ref"] for x in self.posts()], [want])

    def test_deck_card_gets_the_batch_number_as_its_first_line(self):
        p = self.plugin()
        self.deck.badge = {"text": "7 of 21"}
        self.queue(self.card("a b"), self.card("b"))
        p.poll()
        (a,) = self.posts()
        self.assertEqual(a["text"], "7 of 21\n\ncard a b")
        self.assertEqual(self.deck.badges, ["/api/badge?id=a+b"])   # the id is encoded
        self.assertEqual(p.st["open"]["text"], "card a b")   # the text in the queue is as before: the number is added when sending
        self.deck.badge = {"text": "8 of 21"}
        self.press("a b", "❤️")
        p.poll()
        (b,) = self.posts()
        self.assertEqual(b["text"], "8 of 21\n\ncard b")   # the number is fresh for each card

    def test_no_badge_line_when_the_deck_is_silent_or_has_no_text(self):
        p = self.plugin()
        for i, answer in enumerate((None, {}, {"text": None}, {"text": 7}, {"text": "  "})):
            self.deck.badge = answer
            ref = f"c{i}"
            self.queue(self.card(ref))
            p.poll()
            (x,) = self.posts()
            self.assertEqual(x["text"], f"card {ref}", answer)
            self.press(ref, "❤️")
        self.deck.server.shutdown()   # the deck does not answer at all: the card is not sent blind (it waits for the deck)
        self.queue(self.card("z"))
        p.poll()
        self.assertEqual(self.posts(), [])

    def test_drafts_do_not_ask_the_deck_for_a_badge(self):
        p = self.plugin()
        self.deck.badge = {"text": "7 of 21"}
        self.queue(self.draft(1))
        p.poll()
        (d,) = self.posts()
        self.assertEqual(d["text"], "1. Post\n\ntext 1")
        self.assertEqual(self.deck.badges, [])

    def reply_to_card(self, ref, text="the author"):
        self.j.append("telegram.replied", None, {"ext": "tg-posts", "ref": ref, "text": text}, src="telegram")

    def test_reply_to_the_open_deck_card_sends_it_for_revision_and_the_next_card_goes_at_once(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"), self.card("c"))
        p.poll()
        self.posts()
        self.reply_to_card("a")
        p.poll()
        self.assertEqual(self.deck.calls, [("/api/direct", {"id": "a", "text": "the author", "from": "tg-posts"})])
        self.assertEqual([x["ref"] for x in self.posts()], ["b"])   # the next one went out at once, it does not wait for a button
        self.assertEqual(list(p.st["revising"]), ["a"])
        self.assertEqual([x["ref"] for x in p.st["posts"]], ["c"])

    def test_empty_reply_to_a_deck_card_changes_nothing(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.posts()
        self.reply_to_card("a", text="  ")
        p.poll()
        self.assertEqual((self.deck.calls, self.posts(), p.st["revising"]), ([], [], {}))

    def deck_revised(self, *ids, text=None):
        items = [{**self.card(i, **({"text": text} if text else {})), "fresh": True} for i in ids]
        self.listed(*items)
        cli.put(self.dir, {"op": "post", "items": items})

    def test_revised_deck_card_goes_first_in_the_queue_but_not_over_the_open_one(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"), self.card("c"))
        p.poll()
        self.reply_to_card("a")
        p.poll()
        self.posts()
        self.deck_revised("a", text="card a v2")
        p.poll()
        self.assertEqual(self.posts(), [])   # b is open - we do not displace it
        self.assertEqual((p.st["open"]["ref"], [x["ref"] for x in p.st["posts"]], p.st["revising"]), ("b", ["a", "c"], {}))
        self.assertEqual(p.st["posts"][0]["text"], "card a v2")
        self.press("b", "❤️")
        p.poll()
        (a,) = self.posts()
        self.assertEqual((a["ref"], a["text"]), ("a", "card a v2"))   # a before c

    def test_revised_card_returns_even_if_the_text_did_not_change(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.reply_to_card("a")
        p.poll()
        self.posts()
        self.deck_revised("a", text="card a")   # the edit changed nothing - the card must not vanish anyway
        p.poll()
        self.assertEqual(([x["ref"] for x in p.st["posts"]], p.st["revising"]), (["a"], {}))

    def test_revised_card_when_nothing_is_open_goes_out_at_once(self):
        p = self.plugin()
        self.queue(self.card("a"))
        p.poll()
        self.posts()
        self.reply_to_card("a")
        p.poll()
        self.assertEqual((p.st["open"], self.taken()), (None, []))
        self.deck_revised("a", text="card a v2")
        p.poll()
        (a,) = self.posts()
        self.assertEqual((a["ref"], a["text"]), ("a", "card a v2"))

    def test_plain_deck_run_does_not_bring_back_a_card_that_is_being_revised(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.reply_to_card("a")
        p.poll()
        self.posts()
        self.queue(self.card("a"))   # deck without --id sees it among the unrated: the old version is not handed out
        p.poll()
        self.assertEqual(([x["ref"] for x in p.st["posts"]], list(p.st["revising"])), ([], ["a"]))

    def test_close_op_closes_the_open_card_without_a_second_decision_and_sends_the_next(self):
        # regression: the card was already rated in the deck (by another session), in the plugin it held the queue
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.posts()
        cli.put(self.dir, {"op": "close", "ref": "a"})
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["b"])
        self.assertEqual((p.st["open"]["ref"], self.deck.calls), ("b", []))
        self.press("a", "❤️")   # a button under a closed message does not touch the queue
        p.poll()
        self.assertEqual(p.st["open"]["ref"], "b")

    def test_close_op_for_a_card_that_is_not_open_drops_it_from_the_queue_and_the_rest_stays(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"), self.card("c"))
        p.poll()
        self.posts()
        cli.put(self.dir, {"op": "close", "ref": "b"})
        cli.put(self.dir, {"op": "close", "ref": "nope"})
        p.poll()
        self.assertEqual(([x["key"] for x in p.st["posts"]], p.st["open"]["ref"], self.deck.calls), (["c"], "a", []))

    def test_card_already_shown_never_comes_again_not_after_clear_or_restart(self):
        # regression: a session did clear + deck - a card from revision (the owner had already replied to it) went out as new
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.posts()
        self.reply_to_card("a")
        p.poll()
        self.posts()
        cli.put(self.dir, {"op": "clear"})
        p.poll()
        self.queue(self.card("a"), self.card("b"))   # deck without --id sees both among the unrated
        p.poll()
        self.assertEqual((self.taken(), p.st["posts"], p.st["open"]), ([], [], None))
        self.plugin().poll()
        self.queue(self.card("a"))
        p = self.plugin()
        p.poll()
        self.assertEqual((self.taken(), p.st["posts"]), ([], []))
        self.assertEqual(cli.verdicts(self.dir, [{**self.card("a")}, {**self.card("c")}]), ["same", "new"])   # and the CLI does not lie

    def test_card_decided_with_a_button_never_comes_again_but_its_new_version_does(self):
        p = self.plugin()
        self.queue(self.card("a"))
        p.poll()
        self.posts()
        self.press("a", "👎")
        p.poll()
        self.queue(self.card("a"))   # the deck still holds it in its list (the rating has not arrived or the card came back)
        p.poll()
        self.assertEqual(self.posts(), [])
        self.now += main.WHY_HOLD
        p.poll()
        self.deck_revised("a", text="card a v2")   # a new version after revision is the only thing that brings back a shown one
        p.poll()
        (a,) = self.posts()
        self.assertEqual(a["text"], "card a v2")

    def test_seen_cards_are_remembered_beyond_the_sent_window(self):
        p = self.plugin()
        self.queue(self.card("a"))
        p.poll()
        self.posts()
        self.press("a", "❤️")
        p.poll()
        del p.st["sent"]["a"]   # sent is short (KEEP_SENT) and shared with draft posts - "seen" cannot be known from it for long
        p.save()
        self.queue(self.card("a"))
        p = self.plugin()
        p.poll()
        self.assertEqual((self.taken(), p.st["posts"]), ([], []))

    def test_old_state_without_seen_still_does_not_resend_cards_known_by_sent(self):
        os.makedirs(os.path.join(self.dir, "tg-posts"))
        with open(os.path.join(self.dir, "tg-posts", "state.json"), "w", encoding="utf-8") as f:
            json.dump({"seq": 0, "posts": [], "open": None, "sent": {"a": {"kind": "deck", "key": "a"}}}, f)
        p = self.plugin()
        self.queue(self.card("a"))
        p.poll()
        self.assertEqual((self.taken(), p.st["posts"]), ([], []))

    def test_revised_card_that_was_sent_before_and_is_not_tracked_is_queued_anew(self):
        p = self.plugin()
        self.queue(self.card("a"))
        p.poll()
        self.posts()
        self.press("a", "👎")
        p.poll()
        self.now += main.WHY_HOLD   # no reason was given - the rejection went out
        p.poll()
        self.deck_revised("a", text="card a v2")   # it was already sent and closed - it stands anyway
        p.poll()
        (a,) = self.posts()
        self.assertEqual(a["text"], "card a v2")

    def test_revised_card_that_is_not_tracked_goes_first_not_to_the_tail(self):
        # a reply to a card that is not open (it is not in revising), then deck --id: the return is first, not after ten waiting ones
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"), self.card("c"), self.card("d"))
        p.poll()
        self.posts()
        self.press("a", "❤️")
        p.poll()
        self.posts()   # b is open; c, d wait
        self.reply_to_card("a")   # a is not open - revision has not started, but the deck edits it
        p.poll()
        self.assertEqual(p.st["revising"], {})
        self.deck_revised("a", text="card a v2")
        p.poll()
        self.assertEqual([(x["ref"], x["text"]) for x in p.st["posts"]], [("a", "card a v2"), ("c", "card c"), ("d", "card d")])

    def test_revised_card_that_waits_in_the_queue_moves_to_the_front(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"), self.card("c"))
        p.poll()
        self.posts()
        self.deck_revised("c", text="card c v2")
        p.poll()
        self.assertEqual([(x["ref"], x["text"]) for x in p.st["posts"]], [("c", "card c v2"), ("b", "card b")])

    def test_cli_calls_a_waiting_or_forgotten_revised_card_revised(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"), self.card("c"))
        p.poll()
        self.posts()
        st = main.json.load(open(os.path.join(self.dir, "tg-posts", "state.json"), encoding="utf-8"))
        got = cli.verdicts(self.dir, [{**self.card("c"), "fresh": True}, {**self.card("zz"), "fresh": True}])
        self.assertEqual((got, st["open"]["ref"]), (["revised", "new"], "a"))

    def test_reply_to_a_deck_card_passes_the_selected_quote_to_the_deck(self):
        p = self.plugin()
        self.queue(self.card("a"))
        p.poll()
        self.posts()
        self.j.append("telegram.replied", None, {"ext": "tg-posts", "ref": "a", "text": "dot", "quote": " caption tail "},
                      src="telegram")
        p.poll()
        self.assertEqual(self.deck.calls, [("/api/direct", {"id": "a", "text": "dot", "from": "tg-posts", "quote": "caption tail"})])

    def test_revised_card_already_open_or_waiting_is_not_duplicated(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.posts()
        self.deck_revised("a", "b")
        p.poll()
        self.assertEqual(([x["ref"] for x in p.st["posts"]], p.st["open"]["ref"]), (["b"], "a"))

    def test_new_version_of_the_open_deck_card_replaces_it_at_once_and_the_same_version_does_not(self):
        # regression: a reply, the card came back old (open), the writer rewrote it, deck --id was silent
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.posts()
        self.deck_revised("a", text="card a")   # the same version - the open one stays, it does not go out a second time
        p.poll()
        self.assertEqual((self.posts(), p.st["open"]["ref"]), ([], "a"))
        self.deck_revised("a", text="card a v2")
        p.poll()
        (a,) = self.posts()
        self.assertEqual((a["ref"], a["text"]), ("a", "card a v2"))
        self.assertEqual(([x["ref"] for x in p.st["posts"]], p.st["open"]["ref"], p.st["open"]["text"]),
                         (["b"], "a", "card a v2"))
        self.assertEqual((self.deck.calls, p.st["revising"]), ([], {}))   # the old one was closed without a rating to the deck

    def test_button_under_a_deck_card_in_revision_closes_it_without_touching_the_open_one(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"), self.card("c"))
        p.poll()
        self.reply_to_card("a")
        p.poll()
        self.posts()
        self.deck.calls.clear()
        self.press("a", "👎")
        p.poll()
        self.now += main.WHY_HOLD
        p.poll()
        self.assertEqual(self.deck.calls, [("/api/decide", {"id": "a", "verdict": "reject"})])
        self.assertEqual(([x for x in self.posts()], p.st["revising"], p.st["open"]["ref"], len(p.st["posts"])), ([], {}, "b", 1))

    def test_button_under_the_old_message_of_a_card_whose_new_version_is_queued_closes_the_card(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.reply_to_card("a")
        p.poll()
        self.posts()
        self.deck_revised("a", text="card a v2")
        p.poll()
        self.deck.calls.clear()
        self.press("a", "❤️")   # a deck card's ref is one for all versions - a button under the old message decides the card
        p.poll()
        self.assertEqual(self.deck.calls, [("/api/decide", {"id": "a", "verdict": "like"})])
        self.assertEqual(p.st["posts"], [])   # the new version in the queue is no longer needed: it is rated

    def test_revising_deck_card_survives_restart(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.reply_to_card("a")
        p.poll()
        self.taken()
        p = self.plugin()
        self.assertEqual(list(p.st["revising"]), ["a"])
        self.deck_revised("a", text="card a v2")
        p.poll()
        self.assertEqual(([x["ref"] for x in p.st["posts"]], p.st["revising"]), (["a"], {}))

    def test_second_reply_to_a_card_in_revision_goes_to_the_deck_and_keeps_it_in_revision(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.reply_to_card("a")
        p.poll()
        self.posts()
        self.reply_to_card("a", text="even shorter")
        p.poll()
        self.assertEqual([b["text"] for _, b in self.deck.calls], ["the author", "even shorter"])
        self.assertEqual((self.posts(), list(p.st["revising"])), ([], ["a"]))

    def test_draft_button_tells_the_session_and_sends_the_next(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2), self.draft(3))
        p.poll()
        got = self.taken()
        (post,) = [e["data"] for e in got if e["type"] == "telegram.post"]
        self.assertEqual((post["ref"], post["session"], post["buttons"]), ("d.md #1 Post", "smm", ["❤️", "👎"]))
        self.press("d.md #1 Post", "❤️")
        p.poll()
        got = self.taken()
        (msg,) = [e for e in got if e["type"] == "message.received"]
        self.assertEqual((msg["src"], msg["data"]["to"], msg["data"]["channel"]), ("ext:tg-posts", "smm", "session"))
        for word in ("❤️", "1", '"Post"', "/w/d.md", "approved", "2 more"):
            self.assertIn(word, msg["data"]["text"])
        self.assertEqual([e["data"]["ref"] for e in got if e["type"] == "telegram.post"], ["d.md #2 Post"])
        self.press("d.md #2 Post", "👎")
        p.poll()
        got = self.taken()
        (msg,) = [e for e in got if e["type"] == "message.received"]
        self.assertIn("rejected outright", msg["data"]["text"])
        self.assertEqual(self.deck.calls, [])   # the deck has nothing to do with draft posts

    def test_new_version_replaces_the_open_post_that_was_not_replied_to(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2))
        p.poll()
        self.taken()
        self.queue(self.draft(1, text="1. Post\n\nshort"))
        p.poll()
        (v2,) = self.posts()
        self.assertEqual((v2["ref"], v2["text"]), ("d.md #1 Post (v2)", "1. Post\n\nshort"))
        self.press("d.md #1 Post", "❤️")   # a button under the old version does not count
        p.poll()
        self.assertEqual(self.taken(), [])
        self.press("d.md #1 Post (v2)", "❤️")
        p.poll()
        self.assertEqual([e["data"]["ref"] for e in self.taken() if e["type"] == "telegram.post"], ["d.md #2 Post"])

    def test_revision_of_a_waiting_post_replaces_it_in_place_and_same_text_is_a_noop(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2, path="/w/2.png"))
        p.poll()
        self.taken()
        self.queue(self.draft(2, text="2. Post\n\nnew"), self.draft(1))   # post 2 has another text, post 1 the same
        p.poll()
        self.assertEqual(self.taken(), [])
        self.press("d.md #1 Post", "❤️")
        p.poll()
        (two,) = self.posts()
        self.assertEqual((two["text"], two["path"]), ("2. Post\n\nnew", "/w/2.png"))   # the picture was not lost

    def test_same_path_with_new_picture_is_a_new_version_and_goes_out_again(self):
        p = self.plugin()
        self.queue(self.draft(1, path="/w/1.png", img="aaa"), self.draft(2, path="/w/2.png", img="bbb"))
        p.poll()
        self.taken()
        self.queue(self.draft(1, path="/w/1.png", img="ccc"))   # the open one: the file is overwritten, the path is the same
        p.poll()
        (v2,) = self.posts()
        self.assertEqual((v2["ref"], v2["path"]), ("d.md #1 Post (v2)", "/w/1.png"))
        self.queue(self.draft(1, path="/w/1.png", img="ccc"), self.draft(2, path="/w/2.png", img="ddd"))
        p.poll()   # the open v2 did not change - silence; the waiting post 2 with a new picture is replaced in place
        self.assertEqual(self.taken(), [])
        self.press("d.md #1 Post (v2)", "❤️")
        p.poll()
        (two,) = self.posts()
        self.assertEqual(two["ref"], "d.md #2 Post")
        self.assertEqual(p.st["open_drafts"]["img"], "ddd")

    def test_text_edit_without_image_keeps_the_picture_and_its_print(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2, path="/w/2.png", img="bbb"))
        p.poll()
        self.taken()
        self.queue(self.draft(2, text="2. Post\n\nnew"))
        p.poll()
        self.press("d.md #1 Post", "❤️")
        p.poll()
        self.assertEqual((p.st["open_drafts"]["path"], p.st["open_drafts"]["img"]), ("/w/2.png", "bbb"))

    def test_old_item_without_print_is_replaced_by_one_with_it(self):
        p = self.plugin()
        self.queue(self.draft(1, path="/w/1.png"))
        p.poll()
        self.taken()
        self.queue(self.draft(1, path="/w/1.png", img="aaa"))
        p.poll()
        (v2,) = self.posts()
        self.assertEqual(v2["ref"], "d.md #1 Post (v2)")

    def reply(self, ref, text="shorter", to="smm"):
        """The owner's reply to a draft post as the bot writes it: to the session, with the post note (the bot does not write
        telegram.replied for posts with a session)."""
        self.j.append("message.received", None, {
            "text": f"{text}\n\n{REPLY_NOTE.format(ref=ref, file='/w/d.md')}", "attachments": [],
            "tg": {"chat": 1, "id": 7}, "to": to}, src="telegram")

    def test_reply_to_the_open_draft_sends_the_next_one_and_keeps_the_revision(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2), self.draft(3))
        p.poll()
        self.taken()
        self.reply("d.md #1 Post")
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["d.md #2 Post"])
        self.assertEqual([o["ref"] for o in p.st["revising"].values()], ["d.md #1 Post"])
        self.assertEqual(p.st["open_drafts"]["ref"], "d.md #2 Post")
        self.assertEqual([x["ref"] for x in p.st["posts"]], ["d.md #3 Post"])
        self.reply("d.md #1 Post")   # a second reply to the same one moves nothing
        p.poll()
        self.assertEqual(self.taken(), [])

    def test_reply_fact_for_a_draft_works_too(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2))
        p.poll()
        self.taken()
        self.j.append("telegram.replied", None, {"ext": "tg-posts", "ref": "d.md #1 Post", "text": "shorter"}, src="telegram")
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["d.md #2 Post"])

    def test_reply_to_the_last_open_draft_leaves_the_queue_empty_until_the_new_version(self):
        p = self.plugin()
        self.queue(self.draft(1))
        p.poll()
        self.taken()
        self.reply("d.md #1 Post")
        p.poll()
        self.assertEqual((self.taken(), p.st["open_drafts"]), ([], None))
        self.queue(self.draft(1, text="1. Post\n\nshort"))
        p.poll()
        (v2,) = self.posts()
        self.assertEqual((v2["ref"], v2["text"]), ("d.md #1 Post (v2)", "1. Post\n\nshort"))
        self.assertEqual(p.st["revising"], {})

    def test_new_version_of_a_revised_post_goes_first_in_the_queue_but_not_over_the_open_one(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2), self.draft(3))
        p.poll()
        self.taken()
        self.reply("d.md #1 Post")
        p.poll()
        self.taken()
        self.queue(self.draft(1, text="1. Post\n\nshort"))
        p.poll()
        self.assertEqual(self.posts(), [])   # #2 is open - it is not displaced
        self.assertEqual((p.st["open_drafts"]["ref"], [x["ref"] for x in p.st["posts"]], p.st["revising"]),
                         ("d.md #2 Post", ["d.md #1 Post", "d.md #3 Post"], {}))
        self.press("d.md #2 Post", "❤️")
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["d.md #1 Post (v2)"])   # #1 stood before #3

    def test_reply_to_a_later_version_of_a_post_revises_it_again(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2))
        p.poll()
        self.reply("d.md #1 Post")
        p.poll()
        self.queue(self.draft(1, text="1. Post\n\nshort"))
        self.press("d.md #2 Post", "❤️")
        p.poll()
        self.assertEqual(p.st["open_drafts"]["ref"], "d.md #1 Post (v2)")
        self.taken()
        self.reply("d.md #1 Post (v2)")
        p.poll()
        self.assertEqual((p.st["open_drafts"], [o["ref"] for o in p.st["revising"].values()]), (None, ["d.md #1 Post (v2)"]))

    def test_same_text_again_for_a_revised_post_is_not_a_new_version(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2))
        p.poll()
        self.reply("d.md #1 Post")
        p.poll()
        self.taken()
        self.queue(self.draft(1))
        p.poll()
        self.assertEqual((self.taken(), p.st["posts"]), ([], []))
        self.assertEqual(len(p.st["revising"]), 1)

    def test_new_version_of_a_post_that_is_not_revised_replaces_it_as_before(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2), self.draft(3))
        p.poll()
        self.taken()
        self.queue(self.draft(3, text="3. Post\n\nanother"))
        p.poll()
        self.assertEqual([x["text"] for x in p.st["posts"]], ["2. Post\n\ntext 2", "3. Post\n\nanother"])

    def test_button_under_a_post_in_revision_closes_it_without_touching_the_open_one(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2), self.draft(3))
        p.poll()
        self.reply("d.md #1 Post")
        p.poll()
        self.taken()
        self.press("d.md #1 Post", "👎")
        p.poll()
        got = self.taken()
        (msg,) = [e for e in got if e["type"] == "message.received"]
        self.assertIn("rejected outright", msg["data"]["text"])
        self.assertIn("1", msg["data"]["text"])
        self.assertEqual([e for e in got if e["type"] == "telegram.post" and not e["data"]["ref"].startswith("why:")], [])
        self.assertEqual((p.st["revising"], p.st["open_drafts"]["ref"], len(p.st["posts"])), ({}, "d.md #2 Post", 1))

    def test_button_under_the_old_version_after_the_new_one_is_queued_counts_for_nothing(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2))
        p.poll()
        self.reply("d.md #1 Post")
        p.poll()
        self.queue(self.draft(1, text="1. Post\n\nshort"))
        p.poll()
        self.taken()
        self.press("d.md #1 Post", "❤️")
        p.poll()
        self.assertEqual(self.taken(), [])
        self.assertEqual(p.st["open_drafts"]["ref"], "d.md #2 Post")

    def test_reply_to_a_post_that_is_not_open_moves_nothing(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2), self.draft(3))
        p.poll()
        self.taken()
        self.reply("d.md #3 Post")   # waits in the queue
        self.reply("d.md #1 Post (v2)")   # another version
        self.reply("d.md #1 Post", to="other")   # another session
        self.j.append("message.received", None, {"text": "just words", "tg": {"chat": 1, "id": 8}, "to": "smm"}, src="telegram")
        self.j.append("message.received", None, {
            "text": REPLY_NOTE.format(ref="d.md #1 Post", file="/w/d.md"), "to": "smm"}, src="web")   # not from Telegram
        p.poll()
        self.assertEqual((self.taken(), p.st["revising"], p.st["open_drafts"]["ref"]), ([], {}, "d.md #1 Post"))

    def test_reply_to_a_deck_card_still_only_goes_to_the_deck(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.taken()
        self.j.append("message.received", None, {"text": "x\n\n" + REPLY_SHORT.format(ref="a"), "tg": {"chat": 1, "id": 9}}, src="telegram")
        p.poll()
        self.assertEqual((self.taken(), p.st["revising"]), ([], {}))

    def test_revising_survives_restart_and_clear_drops_it(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2))
        p.poll()
        self.reply("d.md #1 Post")
        p.poll()
        self.taken()
        p = self.plugin()
        self.assertEqual([o["ref"] for o in p.st["revising"].values()], ["d.md #1 Post"])
        self.queue(self.draft(1, text="1. Post\n\nshort"))
        p.poll()
        self.assertEqual([x["ref"] for x in p.st["posts"]], ["d.md #1 Post"])   # after a restart - also first
        self.reply("d.md #2 Post")
        p.poll()
        cli.put(self.dir, {"op": "clear"})
        p.poll()
        self.assertEqual((p.st["revising"], p.st["open_drafts"], p.st["posts"]), ({}, None, []))

    def test_state_survives_restart_without_resending(self):
        self.queue(self.draft(1), self.draft(2))
        self.plugin().poll()
        self.taken()
        p = self.plugin()   # a restart: one is open, the second in the queue
        p.poll()
        self.assertEqual(self.taken(), [])
        self.press("d.md #1 Post", "❤️")
        p.poll()
        self.assertEqual([e["data"]["ref"] for e in self.taken() if e["type"] == "telegram.post"], ["d.md #2 Post"])
        self.plugin().poll()
        self.assertEqual(self.taken(), [])

    def test_clear_drops_queue_and_open_post(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2))
        p.poll()
        self.taken()
        cli.put(self.dir, {"op": "clear"})
        p.poll()
        self.press("d.md #1 Post", "❤️")
        p.poll()
        self.assertEqual(self.taken(), [])
        self.assertEqual((p.st["open_drafts"], p.st["posts"]), (None, []))

    def test_same_deck_card_is_not_queued_twice_even_while_its_rating_is_pending(self):
        p = self.plugin()
        self.deck.down = True
        self.queue(self.card("a"), self.card("a"))
        p.poll()
        self.assertEqual(len(self.posts()), 1)
        self.press("a", "❤️")
        p.poll()   # the deck is down - the rating waits for a retry
        self.queue(self.card("a"))
        p.poll()
        self.assertEqual(self.taken(), [])

    def test_old_state_with_queue_key_keeps_its_pending_ratings(self):
        os.makedirs(os.path.join(self.dir, "tg-posts"))
        job = {"path": "/api/decide", "body": {"id": "x", "verdict": "like"}, "since": 1000.0, "next": 0}
        with open(os.path.join(self.dir, "tg-posts", "state.json"), "w", encoding="utf-8") as f:
            json.dump({"seq": 0, "queue": [job]}, f)
        self.plugin()
        self.assertEqual(self.deck.calls, [("/api/decide", {"id": "x", "verdict": "like"})])

    DECK_WHY = ["Stale", "Not our voice", "Not design", "Weak visual", "Blurry", "Dupe", "Boring", "Changed my mind"]
    DRAFTS_WHY = ["Boring", "Off-topic", "Weak image", "Too long", "Not our tone"]

    def test_thumb_down_on_a_deck_card_asks_why_and_the_next_card_does_not_wait(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.posts()
        self.press("a", "👎")
        p.poll()
        sent = self.sent()
        self.assertEqual([x["ref"] for x in sent if not x["ref"].startswith("why:")], ["b"])   # the next one went out at once, it does not wait for a reason
        self.assertEqual(self.deck.calls, [])   # the rejection waits for a reason: the deck accepts it together with the rejection
        (why,) = [x for x in sent if x["ref"].startswith("why:")]
        self.assertEqual((why["buttons"], why["settle"]), (self.DECK_WHY, "Why: {}"))
        self.assertTrue(why["ref"].startswith("why:a"))
        self.assertNotIn("path", why)
        self.assertNotIn("session", why)   # a reply to the question - to the plugin, not the session
        self.press(why["ref"], "Stale")
        p.poll()
        self.assertEqual(self.deck.calls, [("/api/decide", {"id": "a", "verdict": "reject", "reason": "stale"})])
        self.assertEqual(p.st["why"], {})

    def test_reason_in_words_goes_to_the_deck_as_a_note_and_then_the_rejection(self):
        p = self.plugin()
        self.queue(self.card("a"))
        p.poll()
        self.posts()
        self.press("a", "👎")
        p.poll()
        (why,) = self.whys()
        self.reply_to_card(why["ref"], "  the joke is not funny ")
        p.poll()
        self.assertEqual(self.deck.calls, [("/api/comment", {"id": "a", "comment": "the joke is not funny"}),
                                           ("/api/decide", {"id": "a", "verdict": "reject"})])

    def test_unanswered_why_lets_the_rejection_go_without_a_reason_after_the_wait(self):
        p = self.plugin()
        self.queue(self.card("a"))
        p.poll()
        self.press("a", "👎")
        p.poll()
        (why,) = self.whys()
        self.now += main.WHY_HOLD - 1
        p.poll()
        self.assertEqual(self.deck.calls, [])
        self.now += 2
        p.poll()
        self.assertEqual(self.deck.calls, [("/api/decide", {"id": "a", "verdict": "reject"})])
        self.press(why["ref"], "Stale")   # too late: the rejection already went out - there will be no second one
        p.poll()
        self.assertEqual(len(self.deck.calls), 1)

    def test_like_after_thumb_down_on_the_same_card_replaces_the_pending_rejection(self):
        p = self.plugin()
        self.queue(self.card("a"))
        p.poll()
        self.press("a", "👎")
        p.poll()
        self.whys()
        self.press("a", "❤️")
        p.poll()
        self.assertEqual(self.deck.calls, [("/api/decide", {"id": "a", "verdict": "like"})])
        self.now += main.WHY_HOLD + 1
        p.poll()
        self.assertEqual(len(self.deck.calls), 1)

    def test_second_thumb_down_does_not_ask_twice_and_other_cards_do_not_wait_behind_the_pending_one(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.press("a", "👎")
        p.poll()
        self.assertEqual(len(self.whys()), 1)
        self.press("a", "👎")
        self.press("b", "❤️")
        p.poll()
        self.assertEqual((self.whys(), self.deck.calls), ([], [("/api/decide", {"id": "b", "verdict": "like"})]))

    def test_thumb_down_on_a_card_not_tracked_by_the_queue_also_asks_why(self):
        p = self.plugin()
        self.press("old", "👎")   # the card went out before the queue - there is no state about it
        p.poll()
        (why,) = self.whys()
        self.assertEqual(why["buttons"], self.DECK_WHY)
        self.assertEqual(self.deck.calls, [])

    def test_thumb_down_under_a_card_in_revision_asks_why_too(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.reply_to_card("a")
        p.poll()
        self.taken()
        self.press("a", "👎")
        p.poll()
        (why,) = self.whys()
        self.press(why["ref"], "Dupe")
        p.poll()
        self.assertEqual(self.deck.calls[-1], ("/api/decide", {"id": "a", "verdict": "reject", "reason": "dupe"}))

    def test_boring_and_changed_mind_go_to_the_deck_as_reason_codes(self):
        self.assertEqual(main.WHY_DECK[-2:], ["boring", "changed-mind"])
        self.assertEqual(len(main.WHY_DECK), 8)   # the bot shows at most 8 buttons, three in a row
        for lang, boring, changed in (("en", "Boring", "Changed my mind"), ("ru", "Скучно", "Передумал")):
            self.assertEqual(main.why_buttons("deck", lang)[-2:], [boring, changed])
        for button, code in (("Boring", "boring"), ("Передумал", "changed-mind")):
            self.deck.calls.clear()
            p = self.plugin()
            self.queue(self.card("a"))
            p.poll()
            self.press("a", "👎")
            p.poll()
            (why,) = self.whys()
            self.press(why["ref"], button)
            p.poll()
            self.assertEqual(self.deck.calls[-1], ("/api/decide", {"id": "a", "verdict": "reject", "reason": code}))

    def test_pending_rejection_and_question_survive_a_restart(self):
        p = self.plugin()
        self.queue(self.card("a"))
        p.poll()
        self.press("a", "👎")
        p.poll()
        (why,) = self.whys()
        p = self.plugin()
        self.press(why["ref"], "Blurry")
        p.poll()
        self.assertEqual(self.deck.calls, [("/api/decide", {"id": "a", "verdict": "reject", "reason": "soft"})])

    def test_thumb_down_on_a_draft_asks_why_and_the_reason_goes_to_the_session(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2))
        p.poll()
        self.taken()
        self.press("d.md #1 Post", "👎")
        p.poll()
        sent = self.sent()
        self.assertEqual([x["ref"] for x in sent if not x["ref"].startswith("why:")], ["d.md #2 Post"])   # the queue goes as it went
        (why,) = [x for x in sent if x["ref"].startswith("why:")]
        self.assertEqual((why["buttons"], why["settle"]), (self.DRAFTS_WHY, "Why: {}"))
        self.assertNotIn("session", why)
        self.press(why["ref"], "Boring")
        p.poll()
        (said,) = [t for t in self.session_said() if "the reason" in t]
        self.assertIn('"Post"', said)
        self.assertIn("Boring", said)
        self.assertIn("/w/d.md", said)

    def test_reason_in_words_for_a_draft_goes_to_the_session_as_is(self):
        p = self.plugin()
        self.queue(self.draft(1))
        p.poll()
        self.taken()
        self.press("d.md #1 Post", "👎")
        p.poll()
        (why,) = self.whys()
        self.reply_to_card(why["ref"], "this was already in September")
        p.poll()
        (said,) = [t for t in self.session_said() if "the reason" in t]
        self.assertIn("this was already in September", said)
        self.reply_to_card(why["ref"], "and more")   # the question is closed - we do not hand it over a second time
        p.poll()
        self.assertEqual(self.session_said(), [])

    RU_DECK_WHY = ["Баян", "Не наш голос", "Не дизайн", "Слабый кадр", "Мыльно", "Дубль", "Скучно", "Передумал"]
    RU_DRAFTS_WHY = ["Скучно", "Не по теме", "Слабая картинка", "Длинно", "Не наш тон"]

    @staticmethod
    def speaking(lang):
        """Pin the hub language the plugin sees (it resolves it at the moment of sending)."""
        return mock.patch.object(main, "_lang", lambda: lang)

    def test_the_why_question_goes_in_russian_when_the_hub_speaks_russian(self):
        p = self.plugin()
        self.queue(self.card("a"), self.draft(1))
        p.poll()
        self.taken()
        with self.speaking("ru"):
            self.press("a", "👎")
            p.poll()   # the draft becomes the open post
            self.press("d.md #1 Post", "👎")
            p.poll()
        deck_why, drafts_why = self.whys()
        self.assertEqual((deck_why["text"], deck_why["settle"], deck_why["buttons"]),
                         ("Почему 👎? Нажмите кнопку или ответьте своими словами.", "Почему: {}", self.RU_DECK_WHY))
        self.assertEqual((drafts_why["text"], drafts_why["settle"], drafts_why["buttons"]),
                         ("Почему 👎? Нажмите кнопку или ответьте своими словами.", "Почему: {}", self.RU_DRAFTS_WHY))

    def test_the_why_question_is_english_by_default_and_for_a_language_without_a_table(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.taken()
        self.press("a", "👎")
        p.poll()
        (why,) = self.whys()
        self.assertEqual((why["text"], why["settle"], why["buttons"]),
                         ("Why 👎? Press a button or reply in your own words.", "Why: {}", self.DECK_WHY))
        with self.speaking("de"):
            self.press("b", "👎")
            p.poll()
        (why,) = self.whys()
        self.assertEqual((why["text"], why["buttons"]), ("Why 👎? Press a button or reply in your own words.", self.DECK_WHY))
        with self.speaking("ru-RU"):   # a regional variant of a known language
            self.assertEqual(main.tr(main._lang(), "why_settle"), "Почему: {}")

    def test_the_language_is_resolved_at_the_moment_of_sending(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.taken()
        self.press("a", "👎")
        p.poll()
        (first,) = self.whys()
        with self.speaking("ru"):   # the hub language changed while the plugin runs
            self.press("b", "👎")
            p.poll()
            (second,) = self.whys()
        self.assertEqual((first["buttons"], second["buttons"]), (self.DECK_WHY, self.RU_DECK_WHY))

    def test_a_reason_button_is_recognized_in_either_language(self):
        p = self.plugin()
        self.queue(self.card("a"), self.draft(1))
        p.poll()
        self.taken()
        with self.speaking("ru"):
            self.press("a", "👎")
            p.poll()   # the draft becomes the open post
            self.press("d.md #1 Post", "👎")
            p.poll()
        deck_why, drafts_why = self.whys()
        self.taken()
        self.press(deck_why["ref"], "Мыльно")   # sent in Russian, pressed after the hub went back to English
        self.press(drafts_why["ref"], "Скучно")
        p.poll()
        self.assertEqual(self.deck.calls, [("/api/decide", {"id": "a", "verdict": "reject", "reason": "soft"})])
        (said,) = [t for t in self.session_said() if "the reason" in t]
        self.assertIn("Boring", said)   # the session always gets English
        self.assertNotIn("Скучно", said)

    def test_messages_to_sessions_are_english_whatever_the_hub_language(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2))
        p.poll()
        self.taken()
        with self.speaking("ru"):
            self.press("d.md #1 Post", "👎")
            p.poll()
            got = self.taken()
            (why,) = [e["data"] for e in got if e["type"] == "telegram.post" and e["data"]["ref"].startswith("why:")]
            closed = [e["data"]["text"] for e in got if e["type"] == "message.received"]
            self.press(why["ref"], "Скучно")
            p.poll()
            reason = self.session_said()
        self.assertEqual(len(closed), 1)
        self.assertIn("the owner gave post 1", closed[0])
        self.assertIn("rejected outright", closed[0])
        self.assertIn("the reason the owner rejected post 1", reason[0])
        self.assertFalse(any("а" <= c <= "я" for t in closed + reason for c in t))   # no Russian letters

    def test_both_language_tables_have_the_same_keys_and_placeholders(self):
        import re
        self.assertEqual(set(main.STR["en"]), set(main.STR["ru"]))
        for k, v in main.STR["en"].items():
            self.assertEqual(set(re.findall(r"\{(\w+)\}", v)), set(re.findall(r"\{(\w+)\}", main.STR["ru"][k])), k)
        for kind, keys in (("deck", main.WHY_DECK), ("drafts", main.WHY_DRAFTS)):   # every reason has a label in each language
            for k in keys:
                self.assertIn(f"why_{kind}_{k}", main.STR["en"])

    def test_the_hub_language_comes_from_the_core_then_the_environment_then_english(self):
        self.lang.stop()
        try:
            with mock.patch("core.i18n.current", return_value="ru"):
                self.assertEqual(main._lang(), "ru")
            with mock.patch("core.i18n.current", side_effect=RuntimeError("no hub language")):
                with mock.patch.dict(os.environ, {"HUB_LANG": ""}):
                    self.assertEqual(main._lang(), "en")
                with mock.patch.dict(os.environ, {"HUB_LANG": "ru-RU"}):
                    self.assertEqual(main._lang(), "ru")
        finally:
            self.lang.start()

    def test_the_reply_note_of_the_core_bot_is_recognized_byte_for_byte(self):
        """A protocol marker, not UI text: the bot words it in Russian whatever the hub language (modules/telegram/bot.py)."""
        self.assertEqual(main.REPLY_MARK, "(в ответ на пост ")
        self.assertEqual(main.REPLY_FILE, ", файл `")
        ev = {"type": "message.received", "src": "telegram", "data": {
            "tg": {"chat": 1, "id": 7}, "to": "smm", "text": "shorter\n\n" + REPLY_NOTE.format(ref="d.md #1 Post (v2)", file="/w/d.md")}}
        self.assertEqual(main.replied_ref(ev, "smm"), "d.md #1 Post (v2)")
        ev["data"]["text"] = "shorter\n\n" + REPLY_SHORT.format(ref="d.md #1 Post (v2)")
        self.assertEqual(main.replied_ref(ev, "smm"), "d.md #1 Post (v2)")

    def test_thumb_up_does_not_ask_why(self):
        p = self.plugin()
        self.queue(self.card("a"), self.draft(1))
        p.poll()
        self.press("a", "❤️")
        p.poll()
        self.press("d.md #1 Post", "❤️")
        p.poll()
        self.assertEqual(self.whys(), [])

    def again(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["again", *args])
        return out.getvalue()

    def test_again_sends_the_open_deck_card_once_more_with_a_fresh_badge_and_changes_nothing_else(self):
        p = self.plugin()
        self.deck.badge = {"text": "7 of 21"}
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        (first,) = self.posts()
        before = json.loads(json.dumps(p.st))
        self.deck.badge = {"text": "9 of 21"}
        self.assertIn("sending it once more", self.again())
        p.poll()
        (again,) = self.posts()
        self.assertEqual((again["ref"], again["text"], again["buttons"]), (first["ref"], "9 of 21\n\ncard a", ["❤️", "👎"]))
        self.assertEqual(first["text"], "7 of 21\n\ncard a")
        self.assertEqual(p.st, before)   # open, queue, seen, revs, sent - as they were: the next one did not come out
        self.assertEqual(self.deck.calls, [])
        self.assertEqual(self.again("a")[:10], "a: sending")   # by ref too, any number of times
        p.poll()
        self.assertEqual(len(self.posts()), 1)

    def test_queued_card_already_rated_in_the_deck_is_dropped_quietly_and_the_next_one_goes_out(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"), self.card("c"))
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["a"])
        self.deck.cards = [c for c in self.deck.cards if c["id"] != "b"]   # b was liked on the deck page while it waited
        self.press("a", "❤️")
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["c"])   # b is skipped: not sent, not rated again
        self.assertEqual([(path, b["id"]) for path, b in self.deck.calls], [("/api/decide", "a")])
        self.assertEqual([q["ref"] for q in p.st["posts"]], [])
        self.assertEqual(p.st["open"]["ref"], "c")
        self.assertNotIn("b", p.st["seen"])

    def test_every_queued_card_rated_in_the_deck_leaves_nothing_open(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"), self.card("c"))
        p.poll()
        self.taken()
        self.deck.cards = []
        self.press("a", "👎")
        p.poll()
        self.assertEqual((self.posts(), p.st["posts"], p.st["open"]), ([], [], None))

    def test_silent_deck_holds_the_next_card_back_and_it_goes_after_the_pause(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.taken()
        self.deck.silent = True
        self.press("a", "❤️")
        p.poll()
        self.assertEqual((self.posts(), [q["ref"] for q in p.st["posts"]], p.st["open"]), ([], ["b"], None))   # not blind: b waits
        self.deck.silent = False
        p.poll()
        self.assertEqual(self.posts(), [])   # the pause before the next attempt
        self.now += main.RETRY
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["b"])
        self.now += main.RETRY
        p.poll()
        self.assertEqual(self.posts(), [])   # once

    def test_drafts_do_not_wait_for_the_deck(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2))
        p.poll()
        self.taken()
        self.deck.silent = True
        self.press("d.md #1 Post", "❤️")
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["d.md #2 Post"])

    def test_again_of_a_card_rated_in_the_deck_closes_it_quietly_and_sends_the_next(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.taken()
        self.deck.cards = [c for c in self.deck.cards if c["id"] != "a"]   # liked on the page: the open card is already rated
        self.again()
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["b"])   # not a reminder about a, but the next card
        self.assertEqual((p.st["open"]["ref"], self.deck.calls), ("b", []))

    def test_again_with_a_silent_deck_is_not_sent_blind_but_waits_for_it(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.taken()
        self.deck.silent = True
        self.again()
        p.poll()
        self.assertEqual((self.posts(), p.st["open"]["ref"]), ([], "a"))
        self.deck.silent = False
        self.now += main.RETRY
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["a"])   # the reminder still goes, once
        self.now += main.RETRY
        p.poll()
        self.assertEqual(self.posts(), [])

    def test_again_of_a_draft_does_not_ask_the_deck(self):
        p = self.plugin()
        self.queue(self.draft(1))
        p.poll()
        self.taken()
        self.deck.silent = True
        self.again()
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["d.md #1 Post"])

    def test_again_of_a_card_rated_in_the_deck_with_nothing_queued_just_closes_it(self):
        p = self.plugin()
        self.queue(self.card("a"))
        p.poll()
        self.taken()
        self.deck.cards = []
        self.again()
        p.poll()
        self.assertEqual((self.posts(), p.st["open"]), ([], None))

    def test_again_keeps_picture_video_and_session_of_a_draft_and_its_versioned_ref(self):
        p = self.plugin()
        self.queue(self.draft(1, path="/w/p.png", img="x"), self.draft(2))
        p.poll()
        self.taken()
        self.queue(self.draft(1, "1. Post\n\nnew", path="/w/p.png", img="y"))   # a new version of the open one - v2
        p.poll()
        (v2,) = self.posts()
        self.assertEqual(v2["ref"], "d.md #1 Post (v2)")
        self.again("d.md #1 Post")   # the ref without "(v2)" is not the open one: nothing to send
        p.poll()
        self.assertEqual(self.posts(), [])
        self.again("/w/d.md#1")   # the post key - the open one
        p.poll()
        (again,) = self.posts()
        self.assertEqual((again["ref"], again["path"], again["session"], again["text"]),
                         ("d.md #1 Post (v2)", "/w/p.png", "smm", "1. Post\n\nnew"))
        self.assertEqual((p.st["open_drafts"]["ref"], p.st["revs"]["/w/d.md#1"]), ("d.md #1 Post (v2)", 2))

    def test_again_does_not_count_as_the_same_version_nothing(self):
        # the same version from drafts/deck - "no changes", while again always goes to Telegram
        p = self.plugin()
        self.queue(self.draft(1))
        p.poll()
        self.taken()
        self.queue(self.draft(1))
        p.poll()
        self.assertEqual(self.posts(), [])
        self.again()
        p.poll()
        self.assertEqual(len(self.posts()), 1)

    def test_reply_and_button_work_on_the_old_and_the_new_message_of_the_same_post(self):
        # the bot recognizes both messages by ref, the plugin by the same ref: a reply and a button on the old one are no different from the new one
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.posts()
        self.again()
        p.poll()
        self.assertEqual(len(self.posts()), 1)
        self.reply_to_card("a")   # a reply is a revision: the open one goes to the deck, the next one to the owner, as without again
        p.poll()
        self.assertEqual(self.deck.calls, [("/api/direct", {"id": "a", "text": "the author", "from": "tg-posts"})])
        self.assertEqual([x["ref"] for x in self.posts()], ["b"])
        self.deck.calls.clear()
        self.press("a", "❤️")   # a button under either of the two messages closes the card in revision
        p.poll()
        self.assertEqual(self.deck.calls, [("/api/decide", {"id": "a", "verdict": "like"})])
        self.assertEqual(p.st["open"]["ref"], "b")

    def test_button_on_the_resent_draft_closes_it_and_sends_the_next(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2))
        p.poll()
        self.taken()
        self.again()
        p.poll()
        self.taken()
        self.press("d.md #1 Post", "❤️")
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["d.md #2 Post"])

    def test_again_without_an_open_post_or_for_another_ref_sends_nothing_and_says_so(self):
        self.assertIn("no plugin state", self.exit_text(lambda: self.again()))   # the plugin has not kept any state yet
        p = self.plugin()
        self.assertIn("no open post", self.again())
        self.queue(self.card("a"))
        p.poll()
        self.posts()
        self.assertIn("b is not open", self.again("b"))
        self.assertEqual(self.inbox_names(), [])   # nothing went into the inbox
        p.poll()
        self.assertEqual(self.taken(), [])
        cli.put(self.dir, {"op": "again", "ref": "b"})   # a race: another one is already open - the plugin is silent too
        cli.put(self.dir, {"op": "clear"})
        cli.put(self.dir, {"op": "again", "ref": ""})
        p.poll()
        self.assertEqual((self.taken(), p.st["open"]), ([], None))

    def answer(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["reply", *args])
        return out.getvalue()

    def test_reply_answers_the_owner_under_the_card_and_moves_nothing(self):
        # the deck answers the owner's question about a card: to Telegram as a reply to the card (reply_text - the bot finds its
        # latest message by ref), the full text with the card's caption first - for a bot that does not know reply_text
        p = self.plugin()
        self.queue(self.card("a", text="Phone of the post office\n\nlong story"), self.card("b"))
        p.poll()
        self.posts()
        self.reply_to_card("a", "why is it so?")
        p.poll()
        self.posts()   # a reply is a revision: b goes out
        before = json.loads(json.dumps(p.st))
        self.assertIn("a: the answer goes", self.answer("a", "The source does not say."))
        p.poll()
        (answer,) = self.posts()
        self.assertEqual(answer, {"ref": "a", "reply_text": "The source does not say.",
                                  "text": "Phone of the post office\n\nThe source does not say."})
        self.assertEqual(p.st, before)   # open, queue, revision - as they were
        self.answer("gone", "unknown card")   # not in the plugin any more - the ref is the caption
        p.poll()
        (alone,) = self.posts()
        self.assertEqual((alone["ref"], alone["text"]), ("gone", "gone\n\nunknown card"))
        self.assertIn("empty", self.exit_text(lambda: self.answer("a", "  ")))

    # --- two slots (0.13.0): a deck card and a session's post hang in Telegram independently

    def test_session_post_goes_at_once_while_a_deck_card_is_open(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["a"])
        self.queue(self.draft(1))
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["d.md #1 Post"])   # the open card does not hold it
        self.assertEqual((p.st["open"]["ref"], p.st["open_drafts"]["ref"], [x["ref"] for x in p.st["posts"]]),
                         ("a", "d.md #1 Post", ["b"]))
        p.poll()
        self.assertEqual(self.posts(), [])   # a card still goes one at a time

    def test_deck_card_goes_at_once_while_a_session_post_is_open(self):
        p = self.plugin()
        self.queue(self.draft(1))
        p.poll()
        self.taken()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["a"])   # the open post does not hold the deck
        self.assertEqual((p.st["open"]["ref"], p.st["open_drafts"]["ref"]), ("a", "d.md #1 Post"))

    def test_second_session_post_waits_for_the_first_and_each_answer_closes_its_own(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"), self.draft(1), self.draft(2))
        p.poll()
        self.assertEqual(sorted(x["ref"] for x in self.posts()), ["a", "d.md #1 Post"])
        self.assertEqual([x["ref"] for x in p.st["posts"]], ["d.md #2 Post", "b"])
        p.poll()
        self.assertEqual(self.posts(), [])   # both slots are busy
        self.press("d.md #1 Post", "❤️")
        p.poll()
        got = self.taken()
        self.assertEqual([e["data"]["ref"] for e in got if e["type"] == "telegram.post"], ["d.md #2 Post"])   # only the session slot moves
        self.assertEqual(p.st["open"]["ref"], "a")
        self.assertEqual(self.deck.calls, [])
        self.assertEqual(len([e for e in got if e["type"] == "message.received"]), 1)
        self.press("a", "❤️")
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["b"])   # only the deck slot moves
        self.assertEqual(self.deck.calls, [("/api/decide", {"id": "a", "verdict": "like"})])
        self.assertEqual(p.st["open_drafts"]["ref"], "d.md #2 Post")

    def test_reply_to_the_open_session_post_does_not_touch_the_open_card(self):
        p = self.plugin()
        self.queue(self.card("a"), self.draft(1), self.draft(2))
        p.poll()
        self.taken()
        self.reply("d.md #1 Post")
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["d.md #2 Post"])   # revision frees the session slot
        self.assertEqual((p.st["open"]["ref"], list(p.st["revising"])), ("a", ["/w/d.md#1"]))
        self.assertEqual(self.deck.calls, [])

    def test_reply_to_the_open_card_does_not_touch_the_open_session_post(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"), self.draft(1))
        p.poll()
        self.taken()
        self.reply_to_card("a")
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["b"])
        self.assertEqual((p.st["open"]["ref"], p.st["open_drafts"]["ref"]), ("b", "d.md #1 Post"))

    def test_new_version_of_the_open_session_post_replaces_it_in_its_own_slot(self):
        p = self.plugin()
        self.queue(self.card("a"), self.draft(1))
        p.poll()
        self.taken()
        self.queue(self.draft(1, text="1. Post\n\nshort"))
        p.poll()
        (v2,) = self.posts()
        self.assertEqual((v2["ref"], p.st["open"]["ref"]), ("d.md #1 Post (v2)", "a"))

    def test_clear_and_close_reach_both_slots(self):
        p = self.plugin()
        self.queue(self.card("a"), self.draft(1))
        p.poll()
        self.taken()
        cli.put(self.dir, {"op": "close", "ref": "d.md #1 Post"})
        p.poll()
        self.assertEqual((p.st["open"]["ref"], p.st["open_drafts"]), ("a", None))
        self.queue(self.draft(2))
        p.poll()
        self.taken()
        cli.put(self.dir, {"op": "clear"})
        p.poll()
        self.assertEqual((p.st["open"], p.st["open_drafts"], p.st["posts"]), (None, None, []))

    def test_again_without_a_ref_reminds_about_both_open_posts_and_with_a_ref_about_one(self):
        p = self.plugin()
        self.queue(self.card("a"), self.draft(1))
        p.poll()
        self.taken()
        self.assertTrue(p.again("d.md #1 Post"))
        self.assertEqual([x["ref"] for x in self.posts()], ["d.md #1 Post"])
        self.assertTrue(p.again(""))
        self.assertEqual(sorted(x["ref"] for x in self.posts()), ["a", "d.md #1 Post"])

    def test_answer_finds_a_post_in_either_slot(self):
        p = self.plugin()
        self.queue(self.card("a"), self.draft(1))
        p.poll()
        self.taken()
        p.answer("d.md #1 Post", "ok")
        (x,) = self.posts()
        self.assertEqual((x["ref"], x["text"].splitlines()[0]), ("d.md #1 Post", "1. Post"))

    def test_deck_silent_does_not_hold_a_session_post(self):
        p = self.plugin()
        self.deck.silent = True
        self.queue(self.card("a"), self.draft(1))
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["d.md #1 Post"])
        self.assertEqual([x["ref"] for x in p.st["posts"]], ["a"])

    def test_old_state_open_session_post_moves_to_the_session_slot_and_a_card_stays(self):
        os.makedirs(os.path.join(self.dir, "tg-posts"))
        post = {**self.draft(1), "ref": "d.md #1 Post", "at": 1.0}
        for old, deck_open, drafts_open in (({"open": post}, None, "d.md #1 Post"),
                                            ({"open": self.card("a")}, "a", None),
                                            ({"open": None}, None, None), ({}, None, None)):
            with open(os.path.join(self.dir, "tg-posts", "state.json"), "w", encoding="utf-8") as f:
                json.dump({"seq": 0, "posts": [], **old}, f)
            p = self.plugin()
            got = (p.st["open"] and p.st["open"]["ref"], p.st["open_drafts"] and p.st["open_drafts"]["ref"])
            self.assertEqual(got, (deck_open, drafts_open), old)
        # a migrated open post is still answered: a press closes it in its own slot
        with open(os.path.join(self.dir, "tg-posts", "state.json"), "w", encoding="utf-8") as f:
            json.dump({"seq": 0, "posts": [], "open": post, "sent": {"d.md #1 Post": {"kind": "drafts", "key": post["key"]}}}, f)
        p = self.plugin()
        self.press("d.md #1 Post", "❤️")
        p.poll()
        self.assertEqual(p.st["open_drafts"], None)
        self.assertEqual(len(self.session_said()), 1)

    def test_status_and_again_cli_see_both_slots(self):
        os.makedirs(os.path.join(self.dir, "tg-posts"))
        with open(os.path.join(self.dir, "tg-posts", "state.json"), "w", encoding="utf-8") as f:
            json.dump({"open": {"ref": "a", "key": "a", "kind": "deck", "at": 1000.0},
                       "open_drafts": {"ref": "d.md #1 Post", "key": "/w/d.md#1", "kind": "drafts", "at": 1000.0},
                       "posts": [], "jobs": []}, f)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["status"])
        self.assertIn("open in Telegram: a", buf.getvalue())
        self.assertIn("open session post: d.md #1 Post", buf.getvalue())
        self.assertIn("d.md #1 Post", self.again("d.md #1 Post"))
        self.assertIn("a: sending", self.again("a"))
        self.assertIn("is not open", self.again("zzz"))
        self.assertEqual(len(self.inbox_names()), 2)
        self.assertEqual(cli.verdicts(self.dir, [{"kind": "drafts", "key": "/w/d.md#1", "text": "x"}]), ["revised"])

    def exit_text(self, fn):
        try:
            fn()
        except SystemExit as e:
            return str(e)
        return ""

    def inbox_names(self):
        d = os.path.join(self.dir, "tg-posts", "inbox")
        return [n for n in os.listdir(d) if n.endswith(".json")] if os.path.isdir(d) else []

    def test_first_run_starts_from_the_end_of_the_journal(self):
        self.press("old", "❤️")
        self.plugin().poll()
        self.assertEqual(self.deck.calls, [])


    def settings(self, **kw):
        with open(os.environ["HUB_CONFIG"], "w", encoding="utf-8") as f:
            json.dump({"extensions": {"tg-posts": {"deck": self.deck.url, **kw}}}, f)

    def go(self, p, ts):
        self.now = ts
        p.poll()
        return self.posts()

    def test_quiet_night_holds_a_session_post_until_half_past_eight(self):
        p = self.plugin()
        self.now = WED_1959
        self.queue(self.draft(1))
        self.assertEqual(len(self.go(p, WED_1959)), 1)   # 19:59 - the evening is still open
        self.press("d.md #1 Post", "\u2764\ufe0f")
        self.queue(self.draft(2))
        self.assertEqual(self.go(p, WED_2000), [])   # 20:00 - the quiet time
        self.assertEqual(self.go(p, WED_23), [])
        self.assertEqual(self.go(p, THU_0829), [])
        sent = self.go(p, THU_0830)
        self.assertEqual([x["ref"] for x in sent], ["d.md #2 Post"])
        self.assertNotIn("silent", sent[0])   # a weekday - with a sound

    def test_quiet_time_does_not_hold_deck_cards(self):
        p = self.plugin()
        self.queue(self.card("a"))
        self.assertEqual([x["ref"] for x in self.go(p, WED_23)], ["a"])

    def test_weekday_daytime_post_goes_at_once(self):
        p = self.plugin()
        self.queue(self.draft(1))
        sent = self.go(p, WED_NOON)
        self.assertEqual([x["ref"] for x in sent], ["d.md #1 Post"])
        self.assertNotIn("silent", sent[0])

    def test_saturday_posts_gather_and_go_at_noon_the_first_with_a_sound(self):
        p = self.plugin()
        self.now = SAT_10
        self.queue(self.draft(1), self.draft(2))
        self.assertEqual(self.go(p, SAT_10), [])
        sent = self.go(p, SAT_12)
        self.assertEqual([x["ref"] for x in sent], ["d.md #1 Post"])
        self.assertNotIn("silent", sent[0])
        self.press("d.md #1 Post", "\u2764\ufe0f")
        self.now += 5
        p.poll()
        sent = self.posts()
        self.assertEqual([x["ref"] for x in sent], ["d.md #2 Post"])
        self.assertTrue(sent[0]["silent"])   # the rest of the digest - no sound

    def test_a_post_after_saturdays_digest_waits_for_sunday_noon(self):
        p = self.plugin()
        self.now = SAT_10
        self.queue(self.draft(1))
        self.assertEqual(len(self.go(p, SAT_12)), 1)
        self.press("d.md #1 Post", "\u2764\ufe0f")
        self.now = SAT_1230
        self.queue(self.draft(2))
        self.assertEqual(self.go(p, SAT_1230), [])
        self.assertEqual(self.go(p, SAT_20), [])
        self.assertEqual(self.go(p, SUN_1159), [])
        sent = self.go(p, SUN_12)
        self.assertEqual([x["ref"] for x in sent], ["d.md #2 Post"])
        self.assertNotIn("silent", sent[0])   # the Sunday digest has its own sound

    def test_a_post_after_sundays_digest_waits_for_monday_half_past_eight(self):
        p = self.plugin()
        self.now = SUN_12
        self.queue(self.draft(1))
        self.assertEqual(len(self.go(p, SUN_12)), 1)
        self.press("d.md #1 Post", "\u2764\ufe0f")
        self.now = SUN_13
        self.queue(self.draft(2))
        self.assertEqual(self.go(p, SUN_13), [])
        self.assertEqual(self.go(p, MON_0829), [])
        sent = self.go(p, MON_0830)
        self.assertEqual([x["ref"] for x in sent], ["d.md #2 Post"])
        self.assertNotIn("silent", sent[0])

    def test_friday_evening_starts_the_weekend_quiet(self):
        p = self.plugin()
        self.now = FRI_19
        self.queue(self.draft(1))
        self.assertEqual(len(self.go(p, FRI_19)), 1)   # Friday before 20:00 - a weekday
        self.press("d.md #1 Post", "\u2764\ufe0f")
        self.now = FRI_21
        self.queue(self.draft(2))
        self.assertEqual(self.go(p, FRI_21), [])
        self.assertEqual(self.go(p, SAT_10), [])
        self.assertEqual([x["ref"] for x in self.go(p, SAT_12)], ["d.md #2 Post"])

    def test_a_revision_on_a_weekend_is_not_held_for_the_digest_but_for_the_night(self):
        p = self.plugin()
        self.now = SAT_12
        self.queue(self.draft(1))
        self.assertEqual(len(self.go(p, SAT_12)), 1)
        self.reply("d.md #1 Post")
        p.poll()
        self.posts()
        self.queue(self.draft(1, text="1. Post\n\nshorter"))   # the session answers the reply
        self.now = SAT_20 - 60
        p.poll()
        self.assertEqual([x["text"] for x in self.posts()], ["1. Post\n\nshorter"])   # a revision - no wait for Sunday
        self.press("d.md #1 Post", "\u2764\ufe0f")
        self.reply("d.md #1 Post", text="again")
        p.poll()
        self.queue(self.draft(1, text="1. Post\n\nshortest"))
        self.now = SAT_20
        p.poll()
        self.assertEqual(self.posts(), [])   # at night - waits

    def test_quiet_false_keeps_the_old_behavior(self):
        self.settings(quiet=False)
        p = self.plugin()
        self.queue(self.draft(1))
        sent = self.go(p, WED_23)
        self.assertEqual([x["ref"] for x in sent], ["d.md #1 Post"])
        self.press("d.md #1 Post", "\u2764\ufe0f")
        self.queue(self.draft(2))
        sent = self.go(p, SAT_10)
        self.assertEqual([x["ref"] for x in sent], ["d.md #2 Post"])
        self.assertNotIn("silent", sent[0])

    def test_the_digest_hour_and_the_zone_come_from_the_settings(self):
        with open(os.environ["HUB_CONFIG"], "w", encoding="utf-8") as f:
            json.dump({"timezone": "Asia/Novosibirsk", "extensions": {"tg-posts": {"deck": self.deck.url, "weekend_digest_at": "10:00"}}}, f)
        p = self.plugin()
        self.now = SAT_10 - 7 * 3600   # Saturday 03:00 UTC = 10:00 in Novosibirsk
        self.queue(self.draft(1))
        self.assertEqual(len(self.go(p, SAT_10 - 7 * 3600)), 1)

    def test_a_reminder_waits_out_the_quiet_time(self):
        p = self.plugin()
        self.queue(self.draft(1))
        self.assertEqual(len(self.go(p, WED_NOON)), 1)
        self.now = WED_23
        self.again("d.md #1 Post")
        self.assertEqual(self.go(p, WED_23), [])
        self.assertEqual([x["ref"] for x in self.go(p, THU_0830)], ["d.md #1 Post"])

if __name__ == "__main__":
    unittest.main()
