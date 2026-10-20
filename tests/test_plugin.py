"""CLI кладёт посты и команды в ящик плагина; процесс шлёт в Telegram один пост (telegram.post), следующий — после ❤️ / 👎,
закрытие колоды отдаёт ей по порядку, с повтором, пока она лежит, и не повторяет после перезапуска."""
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import journal, spool  # noqa: E402

import main  # noqa: E402

_loader = importlib.machinery.SourceFileLoader("tgposts_cli", os.path.join(ROOT, "plugins", "tg-posts", "bin", "tg-posts"))
_spec = importlib.util.spec_from_loader("tgposts_cli", _loader)
cli = importlib.util.module_from_spec(_spec)
_loader.exec_module(cli)

DRAFTS = """# Черновики

## 1. Первый
Text one.
Перевод.

---

## 2) Второй пост
Text two.

## 3. Третий
Text three.
"""


class Deck:
    """Поддельный сервер колоды: /api/deck, /media/<файл>, запросы POST — в calls."""
    def __init__(self, cards):
        self.calls, self.down, self.badges, self.badge = [], False, [], None   # badge — ответ /api/badge (None — колода его не знает)
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path == "/api/deck":
                    return self.reply(200, json.dumps({"cards": cards, "liked": 0}).encode())
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
        os.environ["HUB_STATE_DIR"] = self.dir   # не живой хаб — всегда, не setdefault
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
        self.assertEqual(self.taken(), [])   # в Telegram CLI сам не шлёт — только в очередь плагина
        first, second = cmd["items"]
        self.assertEqual(first["text"], "1. Первый\n\nText one.\nПеревод.")   # без хвостового «---»
        self.assertEqual((first["kind"], first["key"], first["n"], first["title"]), ("drafts", f"{md}#1", 1, "Первый"))
        self.assertEqual((first["ref"], first["session"], first["file"]), ("drafts-2026-10-19.md #1 Первый", "smm", md))
        self.assertNotIn("path", first)
        self.assertEqual((second["ref"], second["path"]), ("drafts-2026-10-19.md #2 Второй пост", pic))

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

        def plugin_state(items):   # как будто плагин уже разобрал ящик и держит пост открытым
            os.makedirs(os.path.join(self.dir, "tg-posts"), exist_ok=True)
            with open(os.path.join(self.dir, "tg-posts", "state.json"), "w", encoding="utf-8") as f:
                json.dump({"open": items[0], "posts": []}, f)

        write(b"one")
        self.assertIn("в очередь: 1 пост", run())
        first = self.inbox()[0]["items"][0]
        plugin_state([first])
        self.assertIn("без изменений", run())   # ничего не менялось — сессия узнаёт, что ушло нечего
        write(b"two")   # тот же путь, другая картинка
        out = run()
        self.assertIn("в очередь: 1 пост", out)
        self.assertNotIn("без изменений", out)
        second = self.inbox()[-1]["items"][0]
        self.assertEqual(first["path"], second["path"])
        self.assertNotEqual(first["img"], second["img"])
        self.assertEqual(second["img"], cli.fingerprint(pic))

    def test_deck_queues_unrated_cards_with_their_thumbs(self):
        deck = Deck([{"id": "reddit:t3_a/b", "caption": "Line\nmore", "caption_ru": "Строка", "thumb": "a.thumb.jpg"},
                     {"id": "arena:2", "caption": "Two"}])
        try:
            cli.main(["deck", "--url", deck.url, "--count", "5"])
        finally:
            deck.server.shutdown()
        (cmd,) = self.inbox()
        a, b = cmd["items"]
        self.assertEqual((a["kind"], a["key"], a["ref"], a["text"]), ("deck", "reddit:t3_a/b", "reddit:t3_a/b", "Line\nmore\n\nСтрока"))
        self.assertTrue(a["path"].startswith(self.dir))
        with open(a["path"], "rb") as f:
            self.assertTrue(f.read().endswith(b"thumb"))
        self.assertNotIn("path", b)
        self.assertNotIn("session", a)   # нажатия и реплаи — плагину, не сессии

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
        cli.VIDEO_MAX = 4   # скачанное больше предела Bot API — картинкой
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
            self.assertTrue(f.read().endswith(b"thumb"))   # картинка с сервера, а не старый файл из кэша

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
            cli.main(["status"])   # плагин ещё не запускался
        os.makedirs(os.path.join(self.dir, "tg-posts"))
        with open(os.path.join(self.dir, "tg-posts", "state.json"), "w", encoding="utf-8") as f:
            json.dump({"open": {"ref": "a #1 Один", "at": 1000.0}, "posts": [{"ref": "a #2 Два"}], "jobs": []}, f)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["status"])
        self.assertIn("открыт в Telegram: a #1 Один", buf.getvalue())
        self.assertIn("в очереди: 1\n  a #2 Два", buf.getvalue())
        cli.main(["clear"])
        self.assertEqual(self.inbox(), [{"op": "clear"}])

    def test_status_shows_posts_in_revision_and_the_cli_knows_them(self):
        os.makedirs(os.path.join(self.dir, "tg-posts"))
        rev = {"kind": "drafts", "key": "/w/d.md#1", "ref": "d.md #1 Пост", "text": "1. Пост\n\ntext 1", "session": "smm"}
        with open(os.path.join(self.dir, "tg-posts", "state.json"), "w", encoding="utf-8") as f:
            json.dump({"open": {"ref": "a #2 Два", "at": 1000.0}, "posts": [], "revising": {"/w/d.md#1": rev}}, f)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["status"])
        self.assertIn("в докрутке: 1\n  d.md #1 Пост", buf.getvalue())
        same = {"kind": "drafts", "key": "/w/d.md#1", "text": "1. Пост\n\ntext 1"}
        new = {"kind": "drafts", "key": "/w/d.md#1", "text": "1. Пост\n\nкоротко"}
        self.assertEqual(cli.verdicts(self.dir, [same, new]), ["same", "revised"])

    def test_deck_url_comes_from_hub_json_of_the_plugin(self):
        with open(os.environ["HUB_CONFIG"], "w", encoding="utf-8") as f:
            json.dump({"extensions": {"Eugenius-S:tg-posts": {"deck": "http://deck:1/"}}}, f)
        self.assertEqual(cli.deck_url(cli.argparse.Namespace(url=None), self.dir), "http://deck:1")


class Process(Base):
    def setUp(self):
        super().setUp()
        self.deck = Deck([])
        with open(os.environ["HUB_CONFIG"], "w", encoding="utf-8") as f:
            json.dump({"extensions": {"tg-posts": {"deck": self.deck.url}}}, f)
        self.now = 1000.0

    def tearDown(self):
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
        self.press("arena:1", "❤️", ext="other")   # чужое расширение
        self.j.append("telegram.replied", None, {"ext": "tg-posts", "ref": "arena:1", "text": " автора найти "},
                      src="telegram")
        p.poll()
        self.now += main.WHY_HOLD   # отказ ждёт причину — реплай на ту же карточку за ним
        p.poll()
        self.assertEqual(self.deck.calls, [
            ("/api/decide", {"id": "reddit:t3_x", "verdict": "like"}),
            ("/api/decide", {"id": "arena:1", "verdict": "reject"}),
            ("/api/direct", {"id": "arena:1", "text": "автора найти", "from": "tg-posts"})])
        self.plugin().poll()   # перезапуск — не повторяет
        self.assertEqual(len(self.deck.calls), 3)

    def test_deck_down_retries_later_and_keeps_the_order(self):
        p = self.plugin()
        self.deck.down = True
        self.press("a", "❤️")
        self.press("gone", "👎")   # 404 — карточку уже оценили: не повторять
        self.press("gone", "Дубль", ext="nobody")
        p.poll()
        self.now += main.WHY_HOLD
        p.poll()
        self.assertEqual(self.deck.calls, [])
        self.deck.down = False
        p.poll()
        self.assertEqual(self.deck.calls, [])   # пауза до повтора
        self.now += main.RETRY
        p.poll()
        self.assertEqual([b["id"] for _, b in self.deck.calls], ["a", "gone"])
        self.now += main.RETRY
        p.poll()
        self.assertEqual(len(self.deck.calls), 2)

    def queue(self, *items):
        cli.put(self.dir, {"op": "post", "items": list(items)})

    @staticmethod
    def card(i, **kw):
        return {"kind": "deck", "key": i, "ref": i, "text": f"card {i}", **kw}

    @staticmethod
    def draft(n, text=None, **kw):
        return {"kind": "drafts", "key": f"/w/d.md#{n}", "ref": f"d.md #{n} Пост", "text": text or f"{n}. Пост\n\ntext {n}",
                "session": "smm", "file": "/w/d.md", "n": n, "title": "Пост", **kw}

    def posts(self):
        return [x for x in self.sent() if not x["ref"].startswith("why:")]

    def sent(self):
        return [e["data"] for e in self.taken() if e["type"] == "telegram.post"]

    def whys(self):
        """Вопросы «Почему?» после 👎 (то, что бот покажет владельцу)."""
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
        self.assertEqual(self.posts(), [])   # a не закрыта — b ждёт
        self.press("a", "❤️")
        p.poll()
        (b,) = self.posts()
        self.assertEqual(b["ref"], "b")
        self.press("b", "👎")
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["c"])
        self.assertEqual(self.deck.calls, [("/api/decide", {"id": "a", "verdict": "like"})])   # отказ ждёт причину
        self.now += main.WHY_HOLD
        p.poll()
        self.assertEqual(self.deck.calls, [("/api/decide", {"id": "a", "verdict": "like"}),
                                           ("/api/decide", {"id": "b", "verdict": "reject"})])

    def test_deck_card_gets_the_batch_number_as_its_first_line(self):
        p = self.plugin()
        self.deck.badge = {"text": "7 из 21"}
        self.queue(self.card("a b"), self.card("b"))
        p.poll()
        (a,) = self.posts()
        self.assertEqual(a["text"], "7 из 21\n\ncard a b")
        self.assertEqual(self.deck.badges, ["/api/badge?id=a+b"])   # id закодирован
        self.assertEqual(p.st["open"]["text"], "card a b")   # в очереди текст прежний: номер ставится при отправке
        self.deck.badge = {"text": "8 из 21"}
        self.press("a b", "❤️")
        p.poll()
        (b,) = self.posts()
        self.assertEqual(b["text"], "8 из 21\n\ncard b")   # номер свежий для каждой карточки

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
        self.deck.server.shutdown()   # колода не отвечает совсем
        self.queue(self.card("z"))
        p.poll()
        (z,) = self.posts()
        self.assertEqual(z["text"], "card z")

    def test_drafts_do_not_ask_the_deck_for_a_badge(self):
        p = self.plugin()
        self.deck.badge = {"text": "7 из 21"}
        self.queue(self.draft(1))
        p.poll()
        (d,) = self.posts()
        self.assertEqual(d["text"], "1. Пост\n\ntext 1")
        self.assertEqual(self.deck.badges, [])

    def reply_to_card(self, ref, text="автора"):
        self.j.append("telegram.replied", None, {"ext": "tg-posts", "ref": ref, "text": text}, src="telegram")

    def test_reply_to_the_open_deck_card_sends_it_for_revision_and_the_next_card_goes_at_once(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"), self.card("c"))
        p.poll()
        self.posts()
        self.reply_to_card("a")
        p.poll()
        self.assertEqual(self.deck.calls, [("/api/direct", {"id": "a", "text": "автора", "from": "tg-posts"})])
        self.assertEqual([x["ref"] for x in self.posts()], ["b"])   # следующая ушла сразу, не ждёт кнопки
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
        cli.put(self.dir, {"op": "post", "items": [{**self.card(i, **({"text": text} if text else {})), "fresh": True}
                                                    for i in ids]})

    def test_revised_deck_card_goes_first_in_the_queue_but_not_over_the_open_one(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"), self.card("c"))
        p.poll()
        self.reply_to_card("a")
        p.poll()
        self.posts()
        self.deck_revised("a", text="card a v2")
        p.poll()
        self.assertEqual(self.posts(), [])   # открыта b — её не вытесняем
        self.assertEqual((p.st["open"]["ref"], [x["ref"] for x in p.st["posts"]], p.st["revising"]), ("b", ["a", "c"], {}))
        self.assertEqual(p.st["posts"][0]["text"], "card a v2")
        self.press("b", "❤️")
        p.poll()
        (a,) = self.posts()
        self.assertEqual((a["ref"], a["text"]), ("a", "card a v2"))   # a раньше c

    def test_revised_card_returns_even_if_the_text_did_not_change(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.reply_to_card("a")
        p.poll()
        self.posts()
        self.deck_revised("a", text="card a")   # правка ничего не изменила — карточка всё равно не должна пропасть
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
        self.queue(self.card("a"))   # deck без --id видит её среди неоценённых: старую версию не отдаём
        p.poll()
        self.assertEqual(([x["ref"] for x in p.st["posts"]], list(p.st["revising"])), ([], ["a"]))

    def test_close_op_closes_the_open_card_without_a_second_decision_and_sends_the_next(self):
        # 20.10: карточку уже оценили в колоде (другой сессией), в плагине она держала очередь
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.posts()
        cli.put(self.dir, {"op": "close", "ref": "a"})
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["b"])
        self.assertEqual((p.st["open"]["ref"], self.deck.calls), ("b", []))
        self.press("a", "❤️")   # кнопка под закрытым сообщением очередь не трогает
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
        # 20.10: smm-post-tinder сделал clear + deck — карточка из докрутки (владелец уже ответил на неё) ушла как новая
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.posts()
        self.reply_to_card("a")
        p.poll()
        self.posts()
        cli.put(self.dir, {"op": "clear"})
        p.poll()
        self.queue(self.card("a"), self.card("b"))   # deck без --id видит обе среди неоценённых
        p.poll()
        self.assertEqual((self.taken(), p.st["posts"], p.st["open"]), ([], [], None))
        self.plugin().poll()
        self.queue(self.card("a"))
        p = self.plugin()
        p.poll()
        self.assertEqual((self.taken(), p.st["posts"]), ([], []))
        self.assertEqual(cli.verdicts(self.dir, [{**self.card("a")}, {**self.card("c")}]), ["same", "new"])   # и CLI не врёт

    def test_card_decided_with_a_button_never_comes_again_but_its_new_version_does(self):
        p = self.plugin()
        self.queue(self.card("a"))
        p.poll()
        self.posts()
        self.press("a", "👎")
        p.poll()
        self.queue(self.card("a"))   # колода ещё держит её в списке (оценка не дошла или карточка вернулась)
        p.poll()
        self.assertEqual(self.posts(), [])
        self.now += main.WHY_HOLD
        p.poll()
        self.deck_revised("a", text="card a v2")   # новая версия по докрутке — единственное, что возвращает показанную
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
        del p.st["sent"]["a"]   # sent короткий (KEEP_SENT) и общий с вариантами постов — по нему «видела» надолго не узнать
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
        self.now += main.WHY_HOLD   # причину не дали — отказ ушёл
        p.poll()
        self.deck_revised("a", text="card a v2")   # уже отправлялась и закрыта — всё равно встаёт
        p.poll()
        (a,) = self.posts()
        self.assertEqual(a["text"], "card a v2")

    def test_revised_card_that_is_not_tracked_goes_first_not_to_the_tail(self):
        # реплай на карточку, которая не открыта (в revising её нет), → deck --id: возврат — первым, а не после десяти ждущих
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"), self.card("c"), self.card("d"))
        p.poll()
        self.posts()
        self.press("a", "❤️")
        p.poll()
        self.posts()   # открыта b; c, d ждут
        self.reply_to_card("a")   # a не открыта — докрутка не началась, но колода её правит
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
        self.j.append("telegram.replied", None, {"ext": "tg-posts", "ref": "a", "text": "точка", "quote": " хвост подписи "},
                      src="telegram")
        p.poll()
        self.assertEqual(self.deck.calls, [("/api/direct", {"id": "a", "text": "точка", "from": "tg-posts", "quote": "хвост подписи"})])

    def test_revised_card_already_open_or_waiting_is_not_duplicated(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.posts()
        self.deck_revised("a", "b")
        p.poll()
        self.assertEqual(([x["ref"] for x in p.st["posts"]], p.st["open"]["ref"]), (["b"], "a"))

    def test_new_version_of_the_open_deck_card_replaces_it_at_once_and_the_same_version_does_not(self):
        # 20.10 t3_1wgycpb: реплай → карточка вернулась старой (открыта) → райтер переписал → deck --id молчал
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.posts()
        self.deck_revised("a", text="card a")   # та же версия — открытая остаётся, второй раз не уходит
        p.poll()
        self.assertEqual((self.posts(), p.st["open"]["ref"]), ([], "a"))
        self.deck_revised("a", text="card a v2")
        p.poll()
        (a,) = self.posts()
        self.assertEqual((a["ref"], a["text"]), ("a", "card a v2"))
        self.assertEqual(([x["ref"] for x in p.st["posts"]], p.st["open"]["ref"], p.st["open"]["text"]),
                         (["b"], "a", "card a v2"))
        self.assertEqual((self.deck.calls, p.st["revising"]), ([], {}))   # старую закрыли без оценки колоде

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
        self.press("a", "❤️")   # ref карточки колоды один на все версии — кнопка под старым сообщением решает карточку
        p.poll()
        self.assertEqual(self.deck.calls, [("/api/decide", {"id": "a", "verdict": "like"})])
        self.assertEqual(p.st["posts"], [])   # новая версия в очереди больше не нужна: она оценена

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
        self.reply_to_card("a", text="ещё короче")
        p.poll()
        self.assertEqual([b["text"] for _, b in self.deck.calls], ["автора", "ещё короче"])
        self.assertEqual((self.posts(), list(p.st["revising"])), ([], ["a"]))

    def test_draft_button_tells_the_session_and_sends_the_next(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2), self.draft(3))
        p.poll()
        got = self.taken()
        (post,) = [e["data"] for e in got if e["type"] == "telegram.post"]
        self.assertEqual((post["ref"], post["session"], post["buttons"]), ("d.md #1 Пост", "smm", ["❤️", "👎"]))
        self.press("d.md #1 Пост", "❤️")
        p.poll()
        got = self.taken()
        (msg,) = [e for e in got if e["type"] == "message.received"]
        self.assertEqual((msg["src"], msg["data"]["to"], msg["data"]["channel"]), ("ext:tg-posts", "smm", "session"))
        for word in ("❤️", "1", "«Пост»", "/w/d.md", "принят", "ещё 2"):
            self.assertIn(word, msg["data"]["text"])
        self.assertEqual([e["data"]["ref"] for e in got if e["type"] == "telegram.post"], ["d.md #2 Пост"])
        self.press("d.md #2 Пост", "👎")
        p.poll()
        got = self.taken()
        (msg,) = [e for e in got if e["type"] == "message.received"]
        self.assertIn("слив", msg["data"]["text"])
        self.assertEqual(self.deck.calls, [])   # колода к вариантам постов не относится

    def test_new_version_replaces_the_open_post_that_was_not_replied_to(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2))
        p.poll()
        self.taken()
        self.queue(self.draft(1, text="1. Пост\n\nкоротко"))
        p.poll()
        (v2,) = self.posts()
        self.assertEqual((v2["ref"], v2["text"]), ("d.md #1 Пост (v2)", "1. Пост\n\nкоротко"))
        self.press("d.md #1 Пост", "❤️")   # кнопка под старой версией — не в счёт
        p.poll()
        self.assertEqual(self.taken(), [])
        self.press("d.md #1 Пост (v2)", "❤️")
        p.poll()
        self.assertEqual([e["data"]["ref"] for e in self.taken() if e["type"] == "telegram.post"], ["d.md #2 Пост"])

    def test_revision_of_a_waiting_post_replaces_it_in_place_and_same_text_is_a_noop(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2, path="/w/2.png"))
        p.poll()
        self.taken()
        self.queue(self.draft(2, text="2. Пост\n\nновый"), self.draft(1))   # у 2 текст другой, у 1 — тот же
        p.poll()
        self.assertEqual(self.taken(), [])
        self.press("d.md #1 Пост", "❤️")
        p.poll()
        (two,) = self.posts()
        self.assertEqual((two["text"], two["path"]), ("2. Пост\n\nновый", "/w/2.png"))   # картинка не потерялась

    def test_same_path_with_new_picture_is_a_new_version_and_goes_out_again(self):
        p = self.plugin()
        self.queue(self.draft(1, path="/w/1.png", img="aaa"), self.draft(2, path="/w/2.png", img="bbb"))
        p.poll()
        self.taken()
        self.queue(self.draft(1, path="/w/1.png", img="ccc"))   # открытый: файл перезаписан, путь тот же
        p.poll()
        (v2,) = self.posts()
        self.assertEqual((v2["ref"], v2["path"]), ("d.md #1 Пост (v2)", "/w/1.png"))
        self.queue(self.draft(1, path="/w/1.png", img="ccc"), self.draft(2, path="/w/2.png", img="ddd"))
        p.poll()   # открытая v2 не изменилась — молчим; ждущий пост 2 с новой картинкой заменён на месте
        self.assertEqual(self.taken(), [])
        self.press("d.md #1 Пост (v2)", "❤️")
        p.poll()
        (two,) = self.posts()
        self.assertEqual(two["ref"], "d.md #2 Пост")
        self.assertEqual(p.st["open"]["img"], "ddd")

    def test_text_edit_without_image_keeps_the_picture_and_its_print(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2, path="/w/2.png", img="bbb"))
        p.poll()
        self.taken()
        self.queue(self.draft(2, text="2. Пост\n\nновый"))
        p.poll()
        self.press("d.md #1 Пост", "❤️")
        p.poll()
        self.assertEqual((p.st["open"]["path"], p.st["open"]["img"]), ("/w/2.png", "bbb"))

    def test_old_item_without_print_is_replaced_by_one_with_it(self):
        p = self.plugin()
        self.queue(self.draft(1, path="/w/1.png"))
        p.poll()
        self.taken()
        self.queue(self.draft(1, path="/w/1.png", img="aaa"))
        p.poll()
        (v2,) = self.posts()
        self.assertEqual(v2["ref"], "d.md #1 Пост (v2)")

    def reply(self, ref, text="короче", to="smm"):
        """Реплай владельца на вариант постов так, как его пишет бот: сессии, с пометкой поста (telegram.replied для постов
        с сессией бот не пишет)."""
        self.j.append("message.received", None, {
            "text": f"{text}\n\n(в ответ на пост {ref}, файл `/w/d.md`)", "attachments": [],
            "tg": {"chat": 1, "id": 7}, "to": to}, src="telegram")

    def test_reply_to_the_open_draft_sends_the_next_one_and_keeps_the_revision(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2), self.draft(3))
        p.poll()
        self.taken()
        self.reply("d.md #1 Пост")
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["d.md #2 Пост"])
        self.assertEqual([o["ref"] for o in p.st["revising"].values()], ["d.md #1 Пост"])
        self.assertEqual(p.st["open"]["ref"], "d.md #2 Пост")
        self.assertEqual([x["ref"] for x in p.st["posts"]], ["d.md #3 Пост"])
        self.reply("d.md #1 Пост")   # второй реплай на то же — ничего не двигает
        p.poll()
        self.assertEqual(self.taken(), [])

    def test_reply_fact_for_a_draft_works_too(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2))
        p.poll()
        self.taken()
        self.j.append("telegram.replied", None, {"ext": "tg-posts", "ref": "d.md #1 Пост", "text": "короче"}, src="telegram")
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["d.md #2 Пост"])

    def test_reply_to_the_last_open_draft_leaves_the_queue_empty_until_the_new_version(self):
        p = self.plugin()
        self.queue(self.draft(1))
        p.poll()
        self.taken()
        self.reply("d.md #1 Пост")
        p.poll()
        self.assertEqual((self.taken(), p.st["open"]), ([], None))
        self.queue(self.draft(1, text="1. Пост\n\nкоротко"))
        p.poll()
        (v2,) = self.posts()
        self.assertEqual((v2["ref"], v2["text"]), ("d.md #1 Пост (v2)", "1. Пост\n\nкоротко"))
        self.assertEqual(p.st["revising"], {})

    def test_new_version_of_a_revised_post_goes_first_in_the_queue_but_not_over_the_open_one(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2), self.draft(3))
        p.poll()
        self.taken()
        self.reply("d.md #1 Пост")
        p.poll()
        self.taken()
        self.queue(self.draft(1, text="1. Пост\n\nкоротко"))
        p.poll()
        self.assertEqual(self.posts(), [])   # открыт #2 — он не вытеснен
        self.assertEqual((p.st["open"]["ref"], [x["ref"] for x in p.st["posts"]], p.st["revising"]),
                         ("d.md #2 Пост", ["d.md #1 Пост", "d.md #3 Пост"], {}))
        self.press("d.md #2 Пост", "❤️")
        p.poll()
        self.assertEqual([x["ref"] for x in self.posts()], ["d.md #1 Пост (v2)"])   # #1 встал раньше #3

    def test_reply_to_a_later_version_of_a_post_revises_it_again(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2))
        p.poll()
        self.reply("d.md #1 Пост")
        p.poll()
        self.queue(self.draft(1, text="1. Пост\n\nкоротко"))
        self.press("d.md #2 Пост", "❤️")
        p.poll()
        self.assertEqual(p.st["open"]["ref"], "d.md #1 Пост (v2)")
        self.taken()
        self.reply("d.md #1 Пост (v2)")
        p.poll()
        self.assertEqual((p.st["open"], [o["ref"] for o in p.st["revising"].values()]), (None, ["d.md #1 Пост (v2)"]))

    def test_same_text_again_for_a_revised_post_is_not_a_new_version(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2))
        p.poll()
        self.reply("d.md #1 Пост")
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
        self.queue(self.draft(3, text="3. Пост\n\nдругой"))
        p.poll()
        self.assertEqual([x["text"] for x in p.st["posts"]], ["2. Пост\n\ntext 2", "3. Пост\n\nдругой"])

    def test_button_under_a_post_in_revision_closes_it_without_touching_the_open_one(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2), self.draft(3))
        p.poll()
        self.reply("d.md #1 Пост")
        p.poll()
        self.taken()
        self.press("d.md #1 Пост", "👎")
        p.poll()
        got = self.taken()
        (msg,) = [e for e in got if e["type"] == "message.received"]
        self.assertIn("слив", msg["data"]["text"])
        self.assertIn("1", msg["data"]["text"])
        self.assertEqual([e for e in got if e["type"] == "telegram.post" and not e["data"]["ref"].startswith("why:")], [])
        self.assertEqual((p.st["revising"], p.st["open"]["ref"], len(p.st["posts"])), ({}, "d.md #2 Пост", 1))

    def test_button_under_the_old_version_after_the_new_one_is_queued_counts_for_nothing(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2))
        p.poll()
        self.reply("d.md #1 Пост")
        p.poll()
        self.queue(self.draft(1, text="1. Пост\n\nкоротко"))
        p.poll()
        self.taken()
        self.press("d.md #1 Пост", "❤️")
        p.poll()
        self.assertEqual(self.taken(), [])
        self.assertEqual(p.st["open"]["ref"], "d.md #2 Пост")

    def test_reply_to_a_post_that_is_not_open_moves_nothing(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2), self.draft(3))
        p.poll()
        self.taken()
        self.reply("d.md #3 Пост")   # ждёт в очереди
        self.reply("d.md #1 Пост (v2)")   # чужая версия
        self.reply("d.md #1 Пост", to="other")   # другой сессии
        self.j.append("message.received", None, {"text": "просто слова", "tg": {"chat": 1, "id": 8}, "to": "smm"}, src="telegram")
        self.j.append("message.received", None, {
            "text": "(в ответ на пост d.md #1 Пост, файл `/w/d.md`)", "to": "smm"}, src="web")   # не из Telegram
        p.poll()
        self.assertEqual((self.taken(), p.st["revising"], p.st["open"]["ref"]), ([], {}, "d.md #1 Пост"))

    def test_reply_to_a_deck_card_still_only_goes_to_the_deck(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.taken()
        self.j.append("message.received", None, {"text": "x\n\n(в ответ на пост a)", "tg": {"chat": 1, "id": 9}}, src="telegram")
        p.poll()
        self.assertEqual((self.taken(), p.st["revising"]), ([], {}))

    def test_revising_survives_restart_and_clear_drops_it(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2))
        p.poll()
        self.reply("d.md #1 Пост")
        p.poll()
        self.taken()
        p = self.plugin()
        self.assertEqual([o["ref"] for o in p.st["revising"].values()], ["d.md #1 Пост"])
        self.queue(self.draft(1, text="1. Пост\n\nкоротко"))
        p.poll()
        self.assertEqual([x["ref"] for x in p.st["posts"]], ["d.md #1 Пост"])   # после перезапуска — тоже первым
        self.reply("d.md #2 Пост")
        p.poll()
        cli.put(self.dir, {"op": "clear"})
        p.poll()
        self.assertEqual((p.st["revising"], p.st["open"], p.st["posts"]), ({}, None, []))

    def test_state_survives_restart_without_resending(self):
        self.queue(self.draft(1), self.draft(2))
        self.plugin().poll()
        self.taken()
        p = self.plugin()   # перезапуск: один открыт, второй в очереди
        p.poll()
        self.assertEqual(self.taken(), [])
        self.press("d.md #1 Пост", "❤️")
        p.poll()
        self.assertEqual([e["data"]["ref"] for e in self.taken() if e["type"] == "telegram.post"], ["d.md #2 Пост"])
        self.plugin().poll()
        self.assertEqual(self.taken(), [])

    def test_clear_drops_queue_and_open_post(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2))
        p.poll()
        self.taken()
        cli.put(self.dir, {"op": "clear"})
        p.poll()
        self.press("d.md #1 Пост", "❤️")
        p.poll()
        self.assertEqual(self.taken(), [])
        self.assertEqual((p.st["open"], p.st["posts"]), (None, []))

    def test_same_deck_card_is_not_queued_twice_even_while_its_rating_is_pending(self):
        p = self.plugin()
        self.deck.down = True
        self.queue(self.card("a"), self.card("a"))
        p.poll()
        self.assertEqual(len(self.posts()), 1)
        self.press("a", "❤️")
        p.poll()   # колода лежит — оценка ждёт повтора
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

    DECK_WHY = ["Баян", "Не наш голос", "Не дизайн", "Слабый кадр", "Мыльно", "Дубль"]
    DRAFTS_WHY = ["Скучно", "Не по теме", "Слабая картинка", "Длинно", "Не наш тон"]

    def test_thumb_down_on_a_deck_card_asks_why_and_the_next_card_does_not_wait(self):
        p = self.plugin()
        self.queue(self.card("a"), self.card("b"))
        p.poll()
        self.posts()
        self.press("a", "👎")
        p.poll()
        sent = self.sent()
        self.assertEqual([x["ref"] for x in sent if not x["ref"].startswith("why:")], ["b"])   # следующая ушла сразу, не ждёт причины
        self.assertEqual(self.deck.calls, [])   # отказ ждёт причину: колода принимает её вместе с отказом
        (why,) = [x for x in sent if x["ref"].startswith("why:")]
        self.assertEqual((why["buttons"], why["settle"]), (self.DECK_WHY, "Почему: {}"))
        self.assertTrue(why["ref"].startswith("why:a"))
        self.assertNotIn("path", why)
        self.assertNotIn("session", why)   # реплай на вопрос — плагину, не сессии
        self.press(why["ref"], "Баян")
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
        self.reply_to_card(why["ref"], "  шутка не смешная ")
        p.poll()
        self.assertEqual(self.deck.calls, [("/api/comment", {"id": "a", "comment": "шутка не смешная"}),
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
        self.press(why["ref"], "Баян")   # поздно: отказ уже ушёл — второго не будет
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
        self.press("old", "👎")   # карточка ушла до очереди — состояния о ней нет
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
        self.press(why["ref"], "Дубль")
        p.poll()
        self.assertEqual(self.deck.calls[-1], ("/api/decide", {"id": "a", "verdict": "reject", "reason": "dupe"}))

    def test_pending_rejection_and_question_survive_a_restart(self):
        p = self.plugin()
        self.queue(self.card("a"))
        p.poll()
        self.press("a", "👎")
        p.poll()
        (why,) = self.whys()
        p = self.plugin()
        self.press(why["ref"], "Мыльно")
        p.poll()
        self.assertEqual(self.deck.calls, [("/api/decide", {"id": "a", "verdict": "reject", "reason": "soft"})])

    def test_thumb_down_on_a_draft_asks_why_and_the_reason_goes_to_the_session(self):
        p = self.plugin()
        self.queue(self.draft(1), self.draft(2))
        p.poll()
        self.taken()
        self.press("d.md #1 Пост", "👎")
        p.poll()
        sent = self.sent()
        self.assertEqual([x["ref"] for x in sent if not x["ref"].startswith("why:")], ["d.md #2 Пост"])   # очередь идёт как шла
        (why,) = [x for x in sent if x["ref"].startswith("why:")]
        self.assertEqual((why["buttons"], why["settle"]), (self.DRAFTS_WHY, "Почему: {}"))
        self.assertNotIn("session", why)
        self.press(why["ref"], "Скучно")
        p.poll()
        (said,) = [t for t in self.session_said() if "причина" in t]
        self.assertIn("«Пост»", said)
        self.assertIn("Скучно", said)
        self.assertIn("/w/d.md", said)

    def test_reason_in_words_for_a_draft_goes_to_the_session_as_is(self):
        p = self.plugin()
        self.queue(self.draft(1))
        p.poll()
        self.taken()
        self.press("d.md #1 Пост", "👎")
        p.poll()
        (why,) = self.whys()
        self.reply_to_card(why["ref"], "это уже было в сентябре")
        p.poll()
        (said,) = [t for t in self.session_said() if "причина" in t]
        self.assertIn("это уже было в сентябре", said)
        self.reply_to_card(why["ref"], "и ещё")   # вопрос закрыт — второй раз не отдаём
        p.poll()
        self.assertEqual(self.session_said(), [])

    def test_thumb_up_does_not_ask_why(self):
        p = self.plugin()
        self.queue(self.card("a"), self.draft(1))
        p.poll()
        self.press("a", "❤️")
        p.poll()
        self.press("d.md #1 Пост", "❤️")
        p.poll()
        self.assertEqual(self.whys(), [])

    def test_first_run_starts_from_the_end_of_the_journal(self):
        self.press("old", "❤️")
        self.plugin().poll()
        self.assertEqual(self.deck.calls, [])


if __name__ == "__main__":
    unittest.main()
