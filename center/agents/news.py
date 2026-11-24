"""Чтение новостей публичных Telegram-каналов без входа в аккаунт: веб-превью https://t.me/s/<канал>.

Отдаёт последние посты: id, ссылка, время, текст. Каналы без публичного превью (закрытые, с отключённым
предпросмотром) просто возвращают пустой список.
"""
from __future__ import annotations

import datetime as dt
import html
import re

import aiohttp

POST_RE = re.compile(r'data-post="([A-Za-z0-9_]+/\d+)"')
TEXT_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')


def channel_name(src: str) -> str:
    """@name, t.me/name, https://t.me/s/name → name."""
    s = str(src).strip()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^(?:t\.me|telegram\.me)/(?:s/)?", "", s)
    return s.lstrip("@").split("/")[0].split("?")[0]


def _plain(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    return html.unescape(fragment).strip()


def parse(page: str) -> list[dict]:
    """Разобрать HTML превью канала в список постов (старые → новые)."""
    out = []
    chunks = re.split(r'(?=<div class="tgme_widget_message_wrap)', page)
    for ch in chunks:
        m = POST_RE.search(ch)
        if not m:
            continue
        t = TEXT_RE.search(ch)
        text = _plain(t.group(1)) if t else ""
        if not text:
            continue                                    # фото без подписи, опросы — пересказывать нечего
        tm = TIME_RE.search(ch)
        ts = None
        if tm:
            try:
                ts = dt.datetime.fromisoformat(tm.group(1).replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = None
        out.append({"post": m.group(1), "url": f"https://t.me/{m.group(1)}", "ts": ts, "text": text})
    return out


async def fetch(channel: str, timeout: float = 20) -> list[dict]:
    name = channel_name(channel)
    if not re.fullmatch(r"[A-Za-z0-9_]{4,64}", name):
        raise ValueError(f"не похоже на публичный канал: {channel}")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout),
                                     headers={"User-Agent": "Mozilla/5.0 (channel-agent)"}) as s:
        async with s.get(f"https://t.me/s/{name}") as r:
            if r.status != 200:
                raise RuntimeError(f"t.me ответил {r.status}")
            return parse(await r.text())
