"""CLI кладёт посты фактом telegram.post в ящик; процесс отдаёт нажатия и реплаи колоде по порядку, с повтором, пока
она лежит, и не повторяет их после перезапуска."""
import importlib.machinery
import importlib.util
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
        self.calls, self.down = [], False
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path == "/api/deck":
                    return self.reply(200, json.dumps({"cards": cards, "liked": 0}).encode())
                if self.path.startswith("/media/"):
                    return self.reply(200, b"\x89PNG\r\n\x1a\nthumb")
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
    def test_drafts_become_one_post_per_section_for_this_session(self):
        md = os.path.join(self.dir, "drafts-2026-10-19.md")
        with open(md, "w", encoding="utf-8") as f:
            f.write(DRAFTS)
        pic = os.path.join(self.dir, "card.png")
        with open(pic, "wb") as f:
            f.write(b"png")
        cli.main(["drafts", md, "--image", f"2={pic}", "--only", "1,2"])
        got = self.taken()
        self.assertEqual([(e["type"], e["src"]) for e in got], [("telegram.post", "ext:tg-posts")] * 2)
        first, second = (e["data"] for e in got)
        self.assertEqual(first["text"], "1. Первый\n\nText one.\nПеревод.")   # без хвостового «---»
        self.assertEqual(first["ref"], "drafts-2026-10-19.md #1 Первый")
        self.assertEqual(first["session"], "smm")
        self.assertNotIn("path", first)
        self.assertEqual((second["ref"], second["path"]), ("drafts-2026-10-19.md #2 Второй пост", pic))

    def test_deck_sends_unrated_cards_with_buttons_and_their_thumbs(self):
        deck = Deck([{"id": "reddit:t3_a/b", "caption": "Line\nmore", "caption_ru": "Строка", "thumb": "a.thumb.jpg"},
                     {"id": "arena:2", "caption": "Two"}])
        try:
            cli.main(["deck", "--url", deck.url, "--count", "5"])
        finally:
            deck.server.shutdown()
        a, b = (e["data"] for e in self.taken())
        self.assertEqual((a["ref"], a["text"], a["buttons"]), ("reddit:t3_a/b", "Line\nmore\n\nСтрока", ["❤️", "👎"]))
        self.assertTrue(a["path"].startswith(self.dir))
        with open(a["path"], "rb") as f:
            self.assertTrue(f.read().endswith(b"thumb"))
        self.assertNotIn("path", b)
        self.assertNotIn("session", a)   # нажатия и реплаи — плагину, не сессии

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
        self.assertEqual(self.deck.calls, [
            ("/api/decide", {"id": "reddit:t3_x", "verdict": "like"}),
            ("/api/decide", {"id": "arena:1", "verdict": "reject"}),
            ("/api/direct", {"id": "arena:1", "text": "автора найти"})])
        self.plugin().poll()   # перезапуск — не повторяет
        self.assertEqual(len(self.deck.calls), 3)

    def test_deck_down_retries_later_and_keeps_the_order(self):
        p = self.plugin()
        self.deck.down = True
        self.press("a", "❤️")
        self.press("gone", "👎")   # 404 — карточку уже оценили: не повторять
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

    def test_first_run_starts_from_the_end_of_the_journal(self):
        self.press("old", "❤️")
        self.plugin().poll()
        self.assertEqual(self.deck.calls, [])


if __name__ == "__main__":
    unittest.main()
