# tg-posts - posts in Telegram

An [Agents Hub](https://github.com/flagman/agents-hub-core) plugin: a session sends the owner posts in Telegram **one at
a time** - a picture with a caption. The commands put posts into the plugin's queue; one unreviewed post hangs in
Telegram, and the next one goes out when this one is closed with a ❤️ or 👎 button or when the owner replied to it
(revision).

- **Draft posts.** From a drafts file (`## N. Title`). ❤️ - approved, 👎 - rejected outright: both close the post, and
  the session gets a message with the number, the title, the file and the verdict. A reply by voice or text is a
  revision: it goes to the session, and the owner immediately gets the next post from the queue. After a reply the next
  post has already gone out; the session sends the new version as usual (`drafts --only N`) - it stands first in the
  queue, right after the open one, and goes out when that one is reviewed. ❤️ / 👎 under the old message of a revised post
  closes it as usual. A version of a post that was not replied to replaces it in place (an open one goes out again).
- **Deck.** Cards with ❤️ 👎 buttons - also one at a time. The plugin passes a press and a reply in words to the deck
  server itself (`/api/decide`, `/api/direct` with `from: "tg-posts"`) - without a session turn. A reply to a card is a
  revision, like with draft posts: the owner immediately gets the next one, the deck edits the card in the background and
  when ready calls `tg-posts deck --id <id>` - the new version stands first in the queue, right after the open one (both
  when the reply was to an unopened card and when it was already waiting in the queue). A quote selected in the reply
  (`quote`, from this hub's bot) goes to the deck together with the text. ❤️ / 👎 under a card in revision closes it.
  The address is in `hub.json`: `"extensions": {"<plugin name>": {..., "deck": "http://localhost:7777"}}`.
  The number in the batch: at the moment of sending the plugin asks the deck `GET /api/badge?id=<ref>` (no proxy, 3 s)
  and puts its `text` ("7 of 21") as the first line above the caption; the deck is silent or has no `text` - the card
  goes out as before.
- **Why 👎.** After 👎 the owner immediately gets a short question "Why 👎?" with reason buttons (deck: Stale, Not our
  voice, Not design, Weak visual, Blurry, Dupe; draft posts: Boring, Off-topic, Weak image, Too long, Not our tone) - or
  a reply in their own words. The next post does not wait for the answer. The reason goes to the deck together with the
  rating (`/api/decide` with `reason`; words - `/api/comment`) or to the posts session as a message. After the answer
  the question is replaced with a "Why: ..." line; unanswered it stays, and the deck rejection goes out after 20 minutes
  without a reason. Replacing the question with a line works with a bot that knows `settle`.
- **Video.** A deck card has a `local_video` field (a file name, the server serves it at `/media/<name>`) - in Telegram
  it goes as a video with a caption and buttons instead of the preview. A video over 50 MB or one that failed to
  download - the preview goes.
- **Stale cards.** Before a deck card goes out (the next one, or `again`), the plugin asks the deck for its unrated list: a card
  that is gone from it (rated on the deck page while it waited) is dropped quietly and the next one is taken; the deck is
  silent - nothing goes blind, the plugin tries again after 30 s.
- **Queue.** Lives in the plugin's state and survives a restart. `tg-posts status` - what is open, how many wait and what
  is in revision, `tg-posts clear` - reset the queue and the open post, `tg-posts again [<ref>]` - the open post once
  more in Telegram as a new message at the bottom (to remind about it after a digest: the same post, the ref and the
  queue do not change, the "N of M" number is fresh; buttons and replies on the old and the new message belong to one
  post; no open post or the ref is not open - it sends nothing and says so), `tg-posts close <ref>` - close one post
  without a rating to the deck (it is already rated in the deck or on the page): an open one - the owner immediately
  gets the next one.
- **Answer under a post.** `tg-posts reply <ref> "<text>"` - an answer to the owner's question about a post (a reply
  to a deck card goes to the deck server; its answer goes back this way): in Telegram as a reply to the post's latest
  message, and a reply to the answer comes back to the post. No post in the chat - a message with the post's caption
  first. The queue does not move. Needs a hub with `reply_text` in telegram.post; an older one sends the caption and the
  answer as a plain message.

Needs a hub with the `telegram.post` fact (the Telegram module sends a post, a press is `telegram.pressed`, a reply is
`telegram.replied`; the plugin recognizes a reply to a draft post by the bot's message to the session with a note that it
is a reply to a post - today the bot words that note in Russian).

For sessions - the `tg-posts` skill (how to send, how to live with the queue and revision).

## Language

The texts that go to the owner in Telegram (the "Why?" question and its buttons) are in two languages, English and
Russian, and follow the hub language (`hub.json` `language`, then the owner's language, then English), resolved at the
moment of sending. Any other language falls back to English. Messages to sessions and the CLI output are always
English. The manifest description is English, the Russian text is under `i18n.ru` in `hub-plugin.json`.

## Tests

```
PYTHONPATH=<hub code folder> HUB_STATE_DIR=<temp dir> HUB_CONFIG=<temp file> python3 -m unittest discover -s tests
```

Icon — [3D Fluency](https://icons8.com/icons/3d-fluency) by Icons8.
