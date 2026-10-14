import asyncio
import html
import logging
import os
import shutil
import time
import uuid

from pyrogram import Client, filters
from pyrogram.enums import MessageEntityType
from pyrogram.errors import FloodWait
from pyrogram.types import CallbackQuery, Message

import botapi
import db
import terabox
from botapi import btn, kb
from config import (
    API_HASH, API_ID, BOT_TOKEN, DOWNLOAD_DIR, MAX_PARALLEL, MAX_SIZE_MB,
    OWNER_ID, SUPPORT_URL, UPDATES_URL,
)
from ui import bar, e, eid, human, trunc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)      # httpx logs the bot token in URLs
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("terabox-bot")

app = Client("terabox_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)

# Only react to messages from real users. Without this the bot also receives its own
# messages (sent via Bot API) and answers them forever.
IN = filters.incoming & ~filters.bot

SEM = asyncio.Semaphore(MAX_PARALLEL)
BUSY: set[int] = set()          # users with a download running
PENDING: dict[str, dict] = {}   # multi-file pickers
VIDEO_EXT = (".mp4", ".mov", ".m4v", ".webm")
MAX_PICK = 8
MAX_ALL = 10


# ---------------- helpers ----------------
class Throttle:
    def __init__(self, gap=4.0):
        self.gap, self.last = gap, 0.0

    def ready(self):
        now = time.monotonic()
        if now - self.last >= self.gap:
            self.last = now
            return True
        return False


async def quiet_edit(chat_id, mid, text, markup=None):
    try:
        await botapi.edit(chat_id, mid, text, markup)
    except Exception as ex:
        log.debug("edit failed: %s", ex)


def progress_text(icon, title, name, done, total):
    line = f"{e(icon)} <b>{title}</b>\n\n<code>{html.escape(trunc(name, 40))}</code>\n"
    if total:
        b, pct = bar(done, total)
        return line + f"{b} {pct:.0f}%\n{human(done)} / {human(total)}"
    return line + human(done)


def welcome(name):
    return (
        f"{e('rocket')} <b>Welcome to Terabox Downloader, {html.escape(name)}!</b>\n\n"
        f"{e('link')} Send me any <b>Terabox link</b> and I'll download the video and send it right here.\n\n"
        f"{e('bolt')} Fast • Folders supported • Up to {MAX_SIZE_MB // 1000} GB"
    )


HELP = (
    f"{e('star')} <b>How to use</b>\n\n"
    f"1. Copy a Terabox share link\n"
    f"2. Send it to me (or in a group where I'm added)\n"
    f"3. Wait while I download &amp; upload it {e('video')}\n\n"
    f"{e('folder')} For folder links I show a list so you can pick files."
)


def home_markup():
    rows = [[btn("How to use", "help", style="primary", emoji_id=eid("bolt"))]]
    row = []
    if UPDATES_URL:
        row.append(btn("Updates", url=UPDATES_URL, style="success"))
    if SUPPORT_URL:
        row.append(btn("Support", url=SUPPORT_URL, style="danger"))
    return kb(*rows, row)


def result_markup():
    me = app.me.username
    row = [btn("Share bot", url=f"https://t.me/share/url?url=https://t.me/{me}", style="success",
               emoji_id=eid("star"))]
    if UPDATES_URL:
        row.append(btn("Updates", url=UPDATES_URL, style="primary"))
    return kb(row)


async def track(message: Message):
    await db.add("users" if message.chat.id > 0 else "chats", message.chat.id)


# ---------------- core: download + upload one file ----------------
async def send_file(anchor: Message, status_id: int, f: terabox.TeraFile, label: str = ""):
    chat_id = anchor.chat.id
    if f.size and f.size > MAX_SIZE_MB * 1024 * 1024:
        raise terabox.TeraboxError(f"File too large ({human(f.size)}). Limit: {MAX_SIZE_MB} MB.")

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4().hex[:8]}_{terabox.safe_name(f.name)}")
    thumb = None
    try:
        async with SEM:
            th = Throttle()

            async def on_dl(done, total):
                if th.ready():
                    await quiet_edit(chat_id, status_id,
                                     progress_text("download", f"{label}Downloading", f.name, done, total or f.size))

            await terabox.download(f, path, on_dl)
            size = os.path.getsize(path)
            if f.thumb:
                thumb = await terabox.fetch_thumb(f.thumb, path + ".jpg")

            await quiet_edit(chat_id, status_id, progress_text("upload", f"{label}Uploading", f.name, 0, size))
            tu = Throttle()

            async def on_up(done, total):
                if tu.ready():
                    await quiet_edit(chat_id, status_id, progress_text("upload", f"{label}Uploading", f.name, done, total))

            caption = f"<b>{html.escape(f.name)}</b>\n\n💾 {human(size)}"
            if f.name.lower().endswith(VIDEO_EXT):
                sent = await anchor.reply_video(path, caption=caption, thumb=thumb,
                                                supports_streaming=True, progress=on_up)
            else:
                sent = await anchor.reply_document(path, caption=caption, thumb=thumb, progress=on_up)

        # upgrade caption/buttons with premium emoji + colored buttons
        fancy = (
            f"{e('video')} <b>{html.escape(f.name)}</b>\n\n"
            f"{e('save')} <b>Size:</b> {human(size)}\n"
            f"{e('bolt')} <i>via @{app.me.username}</i>"
        )
        try:
            await botapi.edit_caption(chat_id, sent.id, fancy, result_markup())
        except Exception as ex:
            log.debug("caption upgrade failed: %s", ex)
    finally:
        for p in (path, path + ".jpg"):
            try:
                os.remove(p)
            except OSError:
                pass


async def fail(chat_id, status_id, ex):
    if isinstance(ex, terabox.TeraboxError):
        msg = html.escape(str(ex))
    else:
        log.exception("unexpected error", exc_info=ex)
        msg = "Something went wrong. Please try again later."
    await quiet_edit(chat_id, status_id, f"{e('cross')} <b>Failed</b>\n\n{msg}")


# ---------------- handlers ----------------
@app.on_message(filters.command("start") & IN)
async def start(client, message: Message):
    await track(message)
    name = message.from_user.first_name if message.from_user else "there"
    await botapi.send(message.chat.id, welcome(name), home_markup())


@app.on_callback_query(filters.regex(r"^(help|home)$"))
async def nav(client, cq: CallbackQuery):
    if cq.data == "help":
        text, markup = HELP, kb([btn("Back", "home", style="primary")])
    else:
        text, markup = welcome(cq.from_user.first_name), home_markup()
    await quiet_edit(cq.message.chat.id, cq.message.id, text, markup)
    await cq.answer()


@app.on_message(IN & filters.text & ~filters.regex(r"^/") & ~filters.via_bot)
async def on_text(client, message: Message):
    if message.outgoing or (message.from_user and message.from_user.is_bot):
        return
    m = terabox.LINK_RE.search(message.text)
    if not m:
        if message.chat.id > 0:
            try:
                await botapi.send(message.chat.id, f"{e('cross')} Please send a valid <b>Terabox link</b>.",
                                  reply_to=message.id)
            except Exception as ex:
                log.warning("reply failed: %s", ex)
        return
    await track(message)
    await handle_link(message, m.group(0))


async def handle_link(message: Message, url: str):
    chat_id = message.chat.id
    uid = message.from_user.id if message.from_user else chat_id
    if uid in BUSY:
        await botapi.send(chat_id, f"{e('clock')} One download at a time — wait for the current one to finish.",
                          reply_to=message.id)
        return
    BUSY.add(uid)
    status_id = None
    try:
        status = await botapi.send(chat_id, f"{e('search')} <b>Fetching link details…</b>", reply_to=message.id)
        status_id = status["message_id"]
        files = await terabox.resolve(url)

        if len(files) == 1:
            await send_file(message, status_id, files[0])
            await botapi.delete(chat_id, status_id)
            return

        # folder / multiple files -> picker
        for k in [k for k, v in PENDING.items() if time.time() - v["ts"] > 900]:
            PENDING.pop(k, None)
        key = uuid.uuid4().hex[:8]
        PENDING[key] = {"uid": uid, "files": files, "anchor": message, "ts": time.time()}
        rows = [[btn(f"{trunc(f.name, 28)} • {human(f.size)}", f"tb:{key}:{i}", style="primary")]
                for i, f in enumerate(files[:MAX_PICK])]
        rows.append([
            btn(f"Download all ({min(len(files), MAX_ALL)})", f"tb:{key}:all", style="success"),
            btn("Cancel", f"tb:{key}:x", style="danger"),
        ])
        extra = f"\n<i>Showing first {MAX_PICK} of {len(files)}.</i>" if len(files) > MAX_PICK else ""
        await quiet_edit(chat_id, status_id,
                         f"{e('folder')} <b>{len(files)} files found</b> — pick one:{extra}", kb(*rows))
    except Exception as ex:
        if status_id:
            await fail(chat_id, status_id, ex)
    finally:
        BUSY.discard(uid)


@app.on_callback_query(filters.regex(r"^tb:(\w+):(\w+)$"))
async def on_pick(client, cq: CallbackQuery):
    _, key, act = cq.data.split(":")
    job = PENDING.get(key)
    if not job:
        return await cq.answer("This list expired. Send the link again.", show_alert=True)
    if cq.from_user.id != job["uid"]:
        return await cq.answer("This isn't your request.", show_alert=True)
    chat_id, mid = cq.message.chat.id, cq.message.id
    if act == "x":
        PENDING.pop(key, None)
        await quiet_edit(chat_id, mid, f"{e('cross')} Cancelled.")
        return await cq.answer()
    uid = job["uid"]
    if uid in BUSY:
        return await cq.answer("Wait for your current download to finish.", show_alert=True)

    files = job["files"][:MAX_ALL] if act == "all" else [job["files"][int(act)]]
    await cq.answer()
    BUSY.add(uid)
    try:
        for i, f in enumerate(files, 1):
            label = f"[{i}/{len(files)}] " if len(files) > 1 else ""
            await send_file(job["anchor"], mid, f, label)
        await botapi.delete(chat_id, mid)
    except Exception as ex:
        await fail(chat_id, mid, ex)
    finally:
        BUSY.discard(uid)
        PENDING.pop(key, None)


# ---------------- owner tools ----------------
@app.on_message(filters.command("stats") & filters.user(OWNER_ID))
async def stats(client, message: Message):
    await message.reply_text(f"👤 Users: <b>{await db.count('users')}</b>\n👥 Groups: <b>{await db.count('chats')}</b>")


@app.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
async def broadcast(client, message: Message):
    if not message.reply_to_message:
        return await message.reply_text("Reply to a message with <code>/broadcast -user</code>, <code>-gc</code> or <code>-all</code>")
    flag = message.text.lower()
    targets = []
    if "-user" in flag or "-all" in flag:
        targets += await db.ids("users")
    if "-gc" in flag or "-all" in flag:
        targets += await db.ids("chats")
    ok = bad = 0
    for cid in targets:
        for _ in range(2):
            try:
                await message.reply_to_message.copy(cid)
                ok += 1
                break
            except FloodWait as fw:
                await asyncio.sleep(fw.value)
            except Exception:
                bad += 1
                break
        await asyncio.sleep(0.05)
    await message.reply_text(f"Broadcast done ✅ {ok}  ❌ {bad}")


@app.on_message(filters.command("emojiid") & filters.user(OWNER_ID))
async def emojiid(client, message: Message):
    """Reply to a message containing premium emoji -> get their IDs for ui.py"""
    target = message.reply_to_message or message
    ents = (target.entities or []) + (target.caption_entities or [])
    ids = [x.custom_emoji_id for x in ents if x.type == MessageEntityType.CUSTOM_EMOJI]
    await message.reply_text("\n".join(f"<code>{i}</code>" for i in ids) or
                             "No premium emoji found. Reply to a message that contains them.")


if __name__ == "__main__":
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    app.run()
