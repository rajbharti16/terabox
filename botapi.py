"""
Thin Bot API client. Used for every message that needs colored buttons
(`style`) and premium emoji (<tg-emoji>), so it works with any Pyrogram fork.
Big file uploads still go through Pyrogram (MTProto, up to 2 GB).
If Telegram rejects the fancy parts, the call is retried with plain emoji/buttons.
"""
import asyncio
import re

import httpx

from config import BOT_TOKEN

_API = f"https://api.telegram.org/bot{BOT_TOKEN}/"
_http = httpx.AsyncClient(timeout=30)
_EMOJI_TAG = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', re.S)


class TGError(Exception):
    def __init__(self, code, desc):
        super().__init__(f"{code}: {desc}")
        self.code, self.desc = code, desc


def btn(text, data=None, url=None, style=None, emoji_id=None):
    """style: 'primary' (blue) | 'success' (green) | 'danger' (red)"""
    b = {"text": text}
    if data:
        b["callback_data"] = data
    if url:
        b["url"] = url
    if style:
        b["style"] = style
    if emoji_id:
        b["icon_custom_emoji_id"] = emoji_id
    return b


def kb(*rows):
    return {"inline_keyboard": [list(r) for r in rows if r]}


def _plain(params):
    p = dict(params)
    for k in ("text", "caption"):
        if k in p:
            p[k] = _EMOJI_TAG.sub(r"\1", p[k])
    mk = p.get("reply_markup")
    if mk and mk.get("inline_keyboard"):
        p["reply_markup"] = {
            "inline_keyboard": [
                [{k: v for k, v in b.items() if k not in ("style", "icon_custom_emoji_id")} for b in row]
                for row in mk["inline_keyboard"]
            ]
        }
    return p


async def call(method, **params):
    plain_tried = False
    for _ in range(4):
        r = await _http.post(_API + method, json=params)
        data = r.json()
        if data.get("ok"):
            return data["result"]
        code = data.get("error_code")
        desc = data.get("description", "")
        if "not modified" in desc:
            return None
        if code == 429:  # flood limit -> wait and retry
            wait = (data.get("parameters") or {}).get("retry_after", 3)
            await asyncio.sleep(min(wait, 15))
            continue
        if code == 400 and not plain_tried:  # premium emoji / colored buttons rejected -> plain fallback
            plain_tried = True
            plain = _plain(params)
            if plain != params:
                params = plain
                continue
        raise TGError(code, desc)
    raise TGError(429, "Too many retries")


async def send(chat_id, text, markup=None, reply_to=None):
    p = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    if markup:
        p["reply_markup"] = markup
    if reply_to:
        p["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    return await call("sendMessage", **p)


async def edit(chat_id, message_id, text, markup=None):
    return await call(
        "editMessageText",
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        parse_mode="HTML",
        link_preview_options={"is_disabled": True},
        reply_markup=markup or {"inline_keyboard": []},
    )


async def edit_caption(chat_id, message_id, caption, markup=None):
    return await call(
        "editMessageCaption",
        chat_id=chat_id,
        message_id=message_id,
        caption=caption,
        parse_mode="HTML",
        reply_markup=markup or {"inline_keyboard": []},
    )


async def delete(chat_id, message_id):
    try:
        await call("deleteMessage", chat_id=chat_id, message_id=message_id)
    except TGError:
        pass
