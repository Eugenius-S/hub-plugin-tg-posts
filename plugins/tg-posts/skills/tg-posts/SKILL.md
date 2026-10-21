---
name: tg-posts
description: Send the owner draft posts or deck cards in Telegram, one at a time, with ❤️ 👎 buttons. Use it when the owner asks to send posts to Telegram to review them from a phone, and when a tg-posts message about ❤️ / 👎 or a reply to a post arrives.
---

# Posts in Telegram

Posts go to the owner **one at a time**. The commands put them into the plugin's queue; one unreviewed post hangs in
Telegram (a picture, a caption, ❤️ 👎 buttons), and the next one goes out when this one is closed with a button or the
owner replied to it. You wait for nothing and send nothing later: put the whole batch into the queue at once.

## Draft posts from a drafts file

Posts in the file are sections `## N. Title`; Telegram gets "N. Title" and the body of the section. You name the picture
for a post yourself:

```
python3 "${CLAUDE_PLUGIN_ROOT}/bin/tg-posts" drafts <full path to drafts.md> \
  --image 3=<full path to the picture> --image 5=<...> [--only 3,5,7]
```

- `--image N=<path>` - the picture of post N (png, jpg, webp up to 10 MB); without it the post goes as text.
- `--only` - only these numbers; without it - all sections of the file.
- Text longer than 1000 characters - the picture, then the text as a separate message.

What comes to you:

- **❤️ or 👎** - a message "tg-posts: the owner gave post N "Title" (file ...) a ❤️ / 👎: ...". ❤️ - approved: mark it in
  the file. 👎 - rejected outright: remove the post from the file and do not return to it. At the end - how many are
  still in the queue; the next post has already gone to the owner.
- **The reason for 👎** - a message "tg-posts: the reason the owner rejected post N "Title" (file ...): ...". Take it into
  account in the next posts; do not bring the rejected post back. The owner answers with a button or in words; they may
  not answer at all.
- **A reply to a post** (words or voice, with a note appended by the bot - today it is worded in Russian, meaning "in
  reply to post <file> #N Title, file <path>") - revision. The next post from the queue goes to the owner at once -
  **after a reply the next post has already gone out**. Rework the post and send the **new version as usual**: the same command with `--only N` (and `--image N=...` if the picture is
  new; without it the previous one stays) - the version stands **next** in the queue, right after the open post, and goes
  out when it is reviewed:

  ```
  python3 "${CLAUDE_PLUGIN_ROOT}/bin/tg-posts" drafts <the same drafts.md> --only N [--image N=<path>]
  ```

  The new version goes as a new message with buttons (ref "... (v2)"), the old version no longer closes anything; ❤️ / 👎
  under the old message, while there is no new version, closes the post as usual. The version of a post that was not
  replied to is replaced in place by the command (an open one goes out again).
  The picture is compared by content: the same path with an overwritten file is a new version. The same text and the same
  picture is not a duplicate: the command answers "no changes - ... already open or in the queue" and does not send it a
  second time.
- None of this requires an answer to the owner in the chat: they decide with a button.

## A deck with ❤️ 👎 buttons

```
python3 "${CLAUDE_PLUGIN_ROOT}/bin/tg-posts" deck [--count 10]
```

Takes the unrated cards of the deck server (`GET <deck>/api/deck`; the address is the plugin's `deck` in hub.json, by
default `http://localhost:7777`) and puts them into the queue; they go to Telegram one at a time. The plugin passes ❤️ / 👎
to the deck itself (`/api/decide`) and sends the next one at once; a reply in words too (`/api/direct`) - that is a
revision: the card goes to the deck, the next one goes to the owner at once, and the edited card comes back first in the
queue when the deck calls `deck --id <id>` (the deck server does that, not you). You have nothing to do, no turn is spent
on it. A card already standing in the queue does not stand a second time.

## The queue

```
python3 "${CLAUDE_PLUGIN_ROOT}/bin/tg-posts" status   # what is open and how many wait
python3 "${CLAUDE_PLUGIN_ROOT}/bin/tg-posts" clear    # reset the queue and the open post (only on the owner's word)
python3 "${CLAUDE_PLUGIN_ROOT}/bin/tg-posts" again    # remind: the open post - to Telegram once more, as a new message at the bottom (ref <ref> - if a specific one is needed)
```

The queue and the posts in revision live in the plugin and survive a hub restart. An open post without an answer for a
long time means the owner has not got to it yet: do not reset it. You reminded the owner about it (with a digest) - follow
with `again`: the post comes again with buttons, the queue and the version are the same.
