"""
Orenix Manager (@OrenixMnBot) — менеджер чатов + розыгрыши в каналах.

Переменные окружения: BOT_TOKEN, OWNER_ID, WEBHOOK_URL (или RENDER_EXTERNAL_URL),
опционально: WEBHOOK_SECRET, PORT, DB_PATH, TZ_OFFSET.
"""
import asyncio
import html
import json
import logging
import os
import random
import re
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand, CallbackQuery, ChatMemberUpdated, ChatPermissions, InlineKeyboardButton,
    InlineKeyboardMarkup, Message, ReplyParameters,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# ═════════════════════════ Конфиг ═════════════════════════
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")
PORT = int(os.environ.get("PORT", 8080))
DB_PATH = os.environ.get("DB_PATH", "orenix.db")
TZ_OFFSET = int(os.environ.get("TZ_OFFSET", 3))
TZ = timezone(timedelta(hours=TZ_OFFSET))

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
dp = Dispatcher()
BOT_USERNAME = "OrenixMnBot"
BG: set = set()  # ссылки на фоновые задачи


def spawn(coro):
    t = asyncio.create_task(coro)
    BG.add(t)
    t.add_done_callback(BG.discard)


# ═════════════════════════ База данных ═════════════════════════
DB = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
DB.row_factory = sqlite3.Row


def q(sql, p=()):
    return DB.execute(sql, p).fetchall()


def q1(sql, p=()):
    return DB.execute(sql, p).fetchone()


def ex(sql, p=()):
    return DB.execute(sql, p)


def init_db():
    DB.execute("PRAGMA journal_mode=WAL")
    DB.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, name TEXT, coins INTEGER DEFAULT 1000,
            last_daily INTEGER DEFAULT 0, pm INTEGER DEFAULT 0, first_seen INTEGER, last_seen INTEGER);
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY, title TEXT, type TEXT, added_at INTEGER, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS cset (chat_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(chat_id, key));
        CREATE TABLE IF NOT EXISTS members (
            chat_id INTEGER, user_id INTEGER, prefix TEXT DEFAULT '', rank INTEGER DEFAULT 0,
            warns INTEGER DEFAULT 0, joined_at INTEGER, PRIMARY KEY(chat_id, user_id));
        CREATE TABLE IF NOT EXISTS msg_stats (
            chat_id INTEGER, user_id INTEGER, day TEXT, cnt INTEGER DEFAULT 0, PRIMARY KEY(chat_id, user_id, day));
        CREATE TABLE IF NOT EXISTS awards (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER, title TEXT, by_id INTEGER, ts INTEGER);
        CREATE TABLE IF NOT EXISTS chat_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, ts INTEGER, action TEXT, actor_id INTEGER,
            actor_name TEXT, target_id INTEGER, target_name TEXT, details TEXT);
        CREATE INDEX IF NOT EXISTS idx_log ON chat_log(chat_id, ts);
        CREATE TABLE IF NOT EXISTS gadmins (user_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS giveaways (
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, channel_id INTEGER, channel_username TEXT,
            channel_title TEXT, message_id INTEGER, text TEXT, winners INTEGER, ends_at INTEGER,
            status TEXT DEFAULT 'active', subs TEXT DEFAULT '[]', photo TEXT, has_photo INTEGER DEFAULT 0,
            last_edit INTEGER DEFAULT 0, last_count INTEGER DEFAULT 0, winner_ids TEXT DEFAULT '[]', created_at INTEGER);
        CREATE TABLE IF NOT EXISTS gparts (
            giveaway_id INTEGER, user_id INTEGER, name TEXT, ts INTEGER, PRIMARY KEY(giveaway_id, user_id));
        """
    )


# ═════════════════════════ Утилиты ═════════════════════════
def now():
    return int(time.time())


def today():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def day_ago(n):
    return (datetime.now(TZ) - timedelta(days=n)).strftime("%Y-%m-%d")


def day_start_ts(n=0):
    d = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=n)
    return int(d.timestamp())


def fmt_ts(ts, short=False):
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M" if short else "%d.%m.%Y %H:%M")


def esc(s):
    return html.escape(str(s or ""))


def mention(uid, name):
    return f'<a href="tg://user?id={uid}">{esc(name) or uid}</a>'


def B(text, cb=None, style=None, url=None):
    """Кнопка. style: primary (синяя) / success (зелёная) / danger (красная)."""
    kw = {"style": style} if style in ("primary", "success", "danger") else {}
    return InlineKeyboardButton(text=text, callback_data=cb, url=url, **kw)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


UNITS = {"s": 1, "с": 1, "сек": 1, "m": 60, "м": 60, "мин": 60, "h": 3600, "ч": 3600, "час": 3600,
         "d": 86400, "д": 86400, "дн": 86400, "w": 604800, "н": 604800, "нед": 604800}
FOREVER = {"навсегда", "forever", "perm", "inf"}


def parse_dur(s):
    m = re.fullmatch(r"(\d+)\s*([a-zA-Zа-яА-Я]*)", s.strip())
    if not m:
        return None
    u = m.group(2).lower()
    if not u:
        return int(m.group(1)) * 60
    for k in sorted(UNITS, key=len, reverse=True):
        if u.startswith(k):
            return int(m.group(1)) * UNITS[k]
    return None


def take_dur(rest):
    """'1d причина' -> (86400, 'причина'); 'навсегда ...' -> (0, ...); иначе (None, rest)."""
    parts = rest.split(None, 1)
    if not parts:
        return None, ""
    if parts[0].lower() in FOREVER:
        return 0, parts[1] if len(parts) > 1 else ""
    d = parse_dur(parts[0])
    if d:
        return d, parts[1] if len(parts) > 1 else ""
    return None, rest


def fmt_dur(sec):
    if not sec:
        return "навсегда"
    out = []
    for n, u in ((604800, "н"), (86400, "д"), (3600, "ч"), (60, "м")):
        if sec >= n:
            out.append(f"{sec // n}{u}")
            sec %= n
    return " ".join(out) or "1м"


def until(sec):
    return datetime.now(timezone.utc) + timedelta(seconds=sec) if sec else None


MUTED = ChatPermissions(
    can_send_messages=False, can_send_audios=False, can_send_documents=False, can_send_photos=False,
    can_send_videos=False, can_send_video_notes=False, can_send_voice_notes=False, can_send_polls=False,
    can_send_other_messages=False, can_add_web_page_previews=False)
FULL = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True,
    can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True,
    can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True)


# ═════════════════════════ Настройки чата / пользователи ═════════════════════════
CDEF = {
    "welcome": "1", "captcha": "0", "antilink": "0", "antiflood": "0", "games": "1",
    "warn_limit": "3", "warn_action": "mute", "logchat": "",
    "rules": "Правила пока не заданы. Админ: /setrules текст",
    "welcome_text": "👋 Добро пожаловать, {mention}!\nТы в чате <b>{chat}</b>. Правила — /rules",
}


def cget(chat, key):
    r = q1("SELECT value FROM cset WHERE chat_id=? AND key=?", (chat, key))
    return r[0] if r else CDEF[key]


def cset(chat, key, val):
    ex("INSERT OR REPLACE INTO cset VALUES (?,?,?)", (chat, key, str(val)))


def touch(chat, u):
    t = now()
    ex("INSERT INTO users(user_id,username,name,first_seen,last_seen) VALUES(?,?,?,?,?) "
       "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, name=excluded.name, last_seen=excluded.last_seen",
       (u.id, u.username, u.full_name, t, t))
    if chat is not None:
        ex("INSERT INTO chats(chat_id,title,type,added_at,active) VALUES(?,?,?,?,1) "
           "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, active=1", (chat.id, chat.title, chat.type, t))
        ex("INSERT OR IGNORE INTO members(chat_id,user_id,joined_at) VALUES(?,?,?)", (chat.id, u.id, t))


def uname(uid):
    r = q1("SELECT name FROM users WHERE user_id=?", (uid,))
    return r[0] if r else str(uid)


def member(chat, uid):
    ex("INSERT OR IGNORE INTO members(chat_id,user_id,joined_at) VALUES(?,?,?)", (chat, uid, now()))
    return q1("SELECT * FROM members WHERE chat_id=? AND user_id=?", (chat, uid))


def shown(chat, uid, name=None):
    r = q1("SELECT prefix FROM members WHERE chat_id=? AND user_id=?", (chat, uid))
    pre = f"<b>[{esc(r[0])}]</b> " if r and r[0] else ""
    return pre + mention(uid, name or uname(uid))


def is_gadmin(uid):
    return uid == OWNER_ID or q1("SELECT 1 FROM gadmins WHERE user_id=?", (uid,)) is not None


# ═════════════════════════ Права ═════════════════════════
_st: dict = {}


async def tg_status(chat_id, uid):
    c = _st.get((chat_id, uid))
    if c and time.time() - c[0] < 120:
        return c[1]
    try:
        s = (await bot.get_chat_member(chat_id, uid)).status
    except TelegramAPIError:
        s = "member"
    _st[(chat_id, uid)] = (time.time(), s)
    return s


async def get_level(chat_id, uid):
    """0 — участник, 1–4 — ранги бота, 3 — админ Telegram, 5 — создатель чата, 9 — владелец бота."""
    if uid == OWNER_ID:
        return 9
    s = await tg_status(chat_id, uid)
    base = 5 if s == "creator" else 3 if s == "administrator" else 0
    r = q1("SELECT rank FROM members WHERE chat_id=? AND user_id=?", (chat_id, uid))
    return max(base, r[0] if r else 0)


# ═════════════════════════ Лог ═════════════════════════
ICON = {"ban": "🔨", "unban": "♻️", "kick": "👢", "mute": "🔇", "unmute": "🔊", "warn": "⚠️", "unwarn": "✅",
        "join": "📥", "leave": "📤", "promote": "👑", "demote": "⬇️", "prefix": "🏷", "award": "🏆",
        "rank": "🎖", "captcha": "🤖", "antilink": "🔗", "flood": "💥", "settings": "⚙️"}


async def log_action(chat_id, action, actor, target_id=None, target_name="", details=""):
    aid = actor.id if actor else None
    aname = actor.full_name if actor else "бот"
    ex("INSERT INTO chat_log(chat_id,ts,action,actor_id,actor_name,target_id,target_name,details) VALUES(?,?,?,?,?,?,?,?)",
       (chat_id, now(), action, aid, aname, target_id, target_name, details))
    dest = cget(chat_id, "logchat")
    if dest:
        r = q1("SELECT title FROM chats WHERE chat_id=?", (chat_id,))
        text = f"{ICON.get(action, '•')} <b>{action}</b> · {esc(r[0] if r else chat_id)}\n"
        if aid:
            text += f"👮 {mention(aid, aname)}\n"
        if target_id:
            text += f"👤 {mention(target_id, target_name)}\n"
        if details:
            text += f"📝 {esc(details)}"
        try:
            await bot.send_message(int(dest), text)
        except (TelegramAPIError, ValueError):
            pass


# ═════════════════════════ Команды в группах ═════════════════════════
CMDS: dict = {}
ALIAS: dict = {}


def command(key, *aliases):
    def deco(f):
        CMDS[key] = f
        for a in (key, *aliases):
            ALIAS[a] = key
        return f
    return deco


def parse_cmd(text):
    parts = (text or "").strip().split(None, 1)
    if not parts:
        return None, ""
    w = parts[0].lstrip("/!.").lower()
    if "@" in w:
        w, _, suf = w.partition("@")
        if suf != BOT_USERNAME.lower():
            return None, ""
    key = ALIAS.get(w)
    if not key:
        return None, ""
    return key, parts[1] if len(parts) > 1 else ""


async def split_target(m, args):
    """→ (uid, name, остаток текста). Цель: ответ на сообщение, text_mention, @username (из базы) или ID."""
    r = m.reply_to_message
    if r and r.from_user:
        return r.from_user.id, r.from_user.full_name, args
    txt = m.text or m.caption or ""
    for e in m.entities or []:
        if e.type == "text_mention" and e.user:
            return e.user.id, e.user.full_name, txt[e.offset + e.length:].strip()
    tok, _, rest = args.strip().partition(" ")
    if tok.lstrip("-").isdigit():
        return int(tok), uname(int(tok)), rest.strip()
    if tok.startswith("@"):
        r2 = q1("SELECT user_id,name FROM users WHERE lower(username)=lower(?)", (tok[1:],))
        if r2:
            return r2[0], r2[1], rest.strip()
    return None, None, args


async def need(m, lvl):
    a = await get_level(m.chat.id, m.from_user.id)
    if a < lvl:
        await m.reply(f"⛔ Нужен ранг <b>{lvl}+</b> (у вас {a}).")
        return None
    return a


async def mod_target(m, args, lvl):
    a = await need(m, lvl)
    if a is None:
        return None
    uid, name, rest = await split_target(m, args)
    if not uid:
        await m.reply("❗ Укажите пользователя: ответом на сообщение, @username или ID.")
        return None
    if uid in (bot.id, m.from_user.id):
        await m.reply("🙃 Так нельзя.")
        return None
    if await get_level(m.chat.id, uid) >= a and m.from_user.id != OWNER_ID:
        await m.reply("⛔ У цели ранг не ниже вашего.")
        return None
    return uid, name, rest


def qa_kb(kind, chat, uid, label):
    return kb([[B(label, f"qa:{kind}:{chat}:{uid}", "success")]])


def who(m):
    return mention(m.from_user.id, m.from_user.full_name)


async def fail(m, e):
    await m.reply(f"❌ Не удалось: <code>{esc(e)}</code>\nПроверьте, что бот — админ с нужными правами.")


# ── модерация
@command("ban", "бан")
async def c_ban(m, args):
    r = await mod_target(m, args, 2)
    if not r:
        return
    uid, name, rest = r
    sec, reason = take_dur(rest)
    try:
        await bot.ban_chat_member(m.chat.id, uid, until_date=until(sec))
    except TelegramAPIError as e:
        return await fail(m, e)
    await log_action(m.chat.id, "ban", m.from_user, uid, name, f"{fmt_dur(sec)} · {reason or '—'}")
    await m.reply(f"🔨 {shown(m.chat.id, uid, name)} <b>забанен</b> ({fmt_dur(sec)})\n👮 {who(m)}\n📝 {esc(reason) or '—'}",
                  reply_markup=qa_kb("unban", m.chat.id, uid, "♻️ Разбанить"))


@command("unban", "разбан")
async def c_unban(m, args):
    r = await mod_target(m, args, 2)
    if not r:
        return
    uid, name, _ = r
    try:
        await bot.unban_chat_member(m.chat.id, uid, only_if_banned=True)
    except TelegramAPIError as e:
        return await fail(m, e)
    await log_action(m.chat.id, "unban", m.from_user, uid, name)
    await m.reply(f"♻️ {mention(uid, name)} <b>разбанен</b>\n👮 {who(m)}")


@command("kick", "кик")
async def c_kick(m, args):
    r = await mod_target(m, args, 2)
    if not r:
        return
    uid, name, rest = r
    try:
        await bot.ban_chat_member(m.chat.id, uid)
        await bot.unban_chat_member(m.chat.id, uid, only_if_banned=True)
    except TelegramAPIError as e:
        return await fail(m, e)
    await log_action(m.chat.id, "kick", m.from_user, uid, name, rest)
    await m.reply(f"👢 {mention(uid, name)} <b>исключён</b>\n👮 {who(m)}\n📝 {esc(rest) or '—'}")


async def do_mute(chat_id, uid, sec):
    await bot.restrict_chat_member(chat_id, uid, permissions=MUTED, until_date=until(sec))


@command("mute", "мут")
async def c_mute(m, args):
    r = await mod_target(m, args, 1)
    if not r:
        return
    uid, name, rest = r
    sec, reason = take_dur(rest)
    if sec is None:
        sec = 3600
    try:
        await do_mute(m.chat.id, uid, sec)
    except TelegramAPIError as e:
        return await fail(m, e)
    await log_action(m.chat.id, "mute", m.from_user, uid, name, f"{fmt_dur(sec)} · {reason or '—'}")
    await m.reply(f"🔇 {shown(m.chat.id, uid, name)} <b>в муте</b> на {fmt_dur(sec)}\n👮 {who(m)}\n📝 {esc(reason) or '—'}",
                  reply_markup=qa_kb("unmute", m.chat.id, uid, "🔊 Размутить"))


@command("unmute", "размут")
async def c_unmute(m, args):
    r = await mod_target(m, args, 1)
    if not r:
        return
    uid, name, _ = r
    try:
        await bot.restrict_chat_member(m.chat.id, uid, permissions=FULL)
    except TelegramAPIError as e:
        return await fail(m, e)
    await log_action(m.chat.id, "unmute", m.from_user, uid, name)
    await m.reply(f"🔊 {mention(uid, name)} <b>размучен</b>\n👮 {who(m)}")


@command("warn", "варн", "пред")
async def c_warn(m, args):
    r = await mod_target(m, args, 1)
    if not r:
        return
    uid, name, reason = r
    c = m.chat.id
    member(c, uid)
    ex("UPDATE members SET warns=warns+1 WHERE chat_id=? AND user_id=?", (c, uid))
    w = q1("SELECT warns FROM members WHERE chat_id=? AND user_id=?", (c, uid))[0]
    lim = int(cget(c, "warn_limit"))
    await log_action(c, "warn", m.from_user, uid, name, f"{w}/{lim} · {reason or '—'}")
    text = f"⚠️ {shown(c, uid, name)} получил <b>варн {w}/{lim}</b>\n👮 {who(m)}\n📝 {esc(reason) or '—'}"
    if w >= lim:
        act = cget(c, "warn_action")
        ex("UPDATE members SET warns=0 WHERE chat_id=? AND user_id=?", (c, uid))
        try:
            if act == "ban":
                await bot.ban_chat_member(c, uid)
            elif act == "kick":
                await bot.ban_chat_member(c, uid)
                await bot.unban_chat_member(c, uid, only_if_banned=True)
            else:
                await do_mute(c, uid, 86400)
            text += f"\n\n🚨 Лимит варнов! Наказание: <b>{ {'ban': 'бан', 'kick': 'кик', 'mute': 'мут на 1д'}.get(act, act) }</b>"
        except TelegramAPIError as e:
            text += f"\n\n❌ Не удалось применить наказание: {esc(e)}"
        return await m.reply(text)
    await m.reply(text, reply_markup=qa_kb("unwarn", c, uid, "↩️ Снять варн"))


@command("unwarn", "анварн", "снятьварн")
async def c_unwarn(m, args):
    r = await mod_target(m, args, 1)
    if not r:
        return
    uid, name, _ = r
    member(m.chat.id, uid)
    ex("UPDATE members SET warns=MAX(warns-1,0) WHERE chat_id=? AND user_id=?", (m.chat.id, uid))
    await log_action(m.chat.id, "unwarn", m.from_user, uid, name)
    await m.reply(f"✅ С {mention(uid, name)} снят варн\n👮 {who(m)}")


@command("warns", "варны")
async def c_warns(m, args):
    uid, name, _ = await split_target(m, args)
    if not uid:
        uid, name = m.from_user.id, m.from_user.full_name
    r = member(m.chat.id, uid)
    await m.reply(f"⚠️ У {mention(uid, name)} варнов: <b>{r['warns']}/{cget(m.chat.id, 'warn_limit')}</b>")


# ── роли
@command("promote", "админ")
async def c_promote(m, args):
    r = await mod_target(m, args, 4)
    if not r:
        return
    uid, name, title = r
    try:
        await bot.promote_chat_member(m.chat.id, uid, can_manage_chat=True, can_delete_messages=True,
                                      can_restrict_members=True, can_invite_users=True, can_pin_messages=True)
        if title:
            await bot.set_chat_administrator_custom_title(m.chat.id, uid, title[:16])
    except TelegramAPIError as e:
        return await fail(m, e)
    _st.pop((m.chat.id, uid), None)
    if (await get_level(m.chat.id, uid)) < 2:
        ex("UPDATE members SET rank=2 WHERE chat_id=? AND user_id=?", (m.chat.id, uid))
    await log_action(m.chat.id, "promote", m.from_user, uid, name, title)
    await m.reply(f"👑 {mention(uid, name)} <b>назначен админом</b>\n👮 {who(m)}" + (f"\n🏷 Титул: {esc(title[:16])}" if title else ""))


@command("demote", "разжаловать")
async def c_demote(m, args):
    r = await mod_target(m, args, 4)
    if not r:
        return
    uid, name, _ = r
    try:
        await bot.promote_chat_member(m.chat.id, uid, can_manage_chat=False, can_delete_messages=False,
                                      can_restrict_members=False, can_invite_users=False, can_pin_messages=False,
                                      can_promote_members=False, can_change_info=False)
    except TelegramAPIError as e:
        await m.reply(f"⚠️ Права в Telegram не сняты: <code>{esc(e)}</code>")
    _st.pop((m.chat.id, uid), None)
    member(m.chat.id, uid)
    ex("UPDATE members SET rank=0 WHERE chat_id=? AND user_id=?", (m.chat.id, uid))
    await log_action(m.chat.id, "demote", m.from_user, uid, name)
    await m.reply(f"⬇️ {mention(uid, name)} <b>разжалован</b>\n👮 {who(m)}")


@command("rank", "ранг")
async def c_rank(m, args):
    r = await mod_target(m, args, 4)
    if not r:
        return
    uid, name, rest = r
    a = await get_level(m.chat.id, m.from_user.id)
    if not rest.strip().isdigit() or not 0 <= int(rest.strip()) <= 4:
        return await m.reply("❗ Формат: <code>/rank @юзер 0-4</code>\n1 — варн/мут · 2 — +бан/кик · 3 — +префиксы/награды · 4 — +настройки/роли")
    n = int(rest.strip())
    if n >= a:
        return await m.reply("⛔ Нельзя выдать ранг не ниже своего.")
    member(m.chat.id, uid)
    ex("UPDATE members SET rank=? WHERE chat_id=? AND user_id=?", (n, m.chat.id, uid))
    await log_action(m.chat.id, "rank", m.from_user, uid, name, f"ранг {n}")
    await m.reply(f"🎖 {mention(uid, name)} теперь имеет ранг <b>{n}</b>\n👮 {who(m)}")


@command("admins", "админы", "стафф")
async def c_admins(m, args):
    lines = ["👑 <b>Команда чата</b>\n"]
    try:
        for a in await bot.get_chat_administrators(m.chat.id):
            if a.user.is_bot:
                continue
            tag = "👑" if a.status == "creator" else "🛡"
            t = f" · <i>{esc(getattr(a, 'custom_title', '') or '')}</i>" if getattr(a, "custom_title", None) else ""
            lines.append(f"{tag} {shown(m.chat.id, a.user.id, a.user.full_name)}{t}")
    except TelegramAPIError:
        pass
    ranked = q("SELECT user_id, rank FROM members WHERE chat_id=? AND rank>0 ORDER BY rank DESC", (m.chat.id,))
    if ranked:
        lines.append("\n🎖 <b>Ранги бота</b>")
        lines += [f"• {mention(r['user_id'], uname(r['user_id']))} — ранг {r['rank']}" for r in ranked]
    await m.reply("\n".join(lines))


# ── префиксы и награды
@command("prefix", "префикс")
async def c_prefix(m, args):
    r = await mod_target(m, args, 3)
    if not r:
        return
    uid, name, text = r
    text = text.strip()[:24]
    if not text:
        return await m.reply("❗ Формат: <code>/prefix @юзер текст</code> (до 24 символов)")
    member(m.chat.id, uid)
    ex("UPDATE members SET prefix=? WHERE chat_id=? AND user_id=?", (text, m.chat.id, uid))
    try:  # если цель — админ, ставим ещё и титул в Telegram
        await bot.set_chat_administrator_custom_title(m.chat.id, uid, text[:16])
    except TelegramAPIError:
        pass
    await log_action(m.chat.id, "prefix", m.from_user, uid, name, text)
    await m.reply(f"🏷 {mention(uid, name)} получил префикс <b>[{esc(text)}]</b>\n👮 {who(m)}")


@command("unprefix", "безпрефикса")
async def c_unprefix(m, args):
    r = await mod_target(m, args, 3)
    if not r:
        return
    uid, name, _ = r
    member(m.chat.id, uid)
    ex("UPDATE members SET prefix='' WHERE chat_id=? AND user_id=?", (m.chat.id, uid))
    await m.reply(f"🏷 Префикс {mention(uid, name)} удалён")


@command("award", "наградить")
async def c_award(m, args):
    r = await mod_target(m, args, 3)
    if not r:
        return
    uid, name, text = r
    if not text.strip():
        return await m.reply("❗ Формат: <code>/award @юзер 🏆 Лучший модератор</code>")
    ex("INSERT INTO awards(chat_id,user_id,title,by_id,ts) VALUES(?,?,?,?,?)",
       (m.chat.id, uid, text.strip()[:60], m.from_user.id, now()))
    await log_action(m.chat.id, "award", m.from_user, uid, name, text)
    await m.reply(f"🏆 {mention(uid, name)} получил награду: <b>{esc(text.strip()[:60])}</b>\n👮 {who(m)}")


@command("unaward", "забратьнаграду")
async def c_unaward(m, args):
    r = await mod_target(m, args, 3)
    if not r:
        return
    uid, name, _ = r
    ex("DELETE FROM awards WHERE id=(SELECT MAX(id) FROM awards WHERE chat_id=? AND user_id=?)", (m.chat.id, uid))
    await m.reply(f"🗑 Последняя награда {mention(uid, name)} удалена")


@command("me", "профиль", "инфо")
async def c_me(m, args):
    uid, name, _ = await split_target(m, args)
    if not uid:
        uid, name = m.from_user.id, m.from_user.full_name
    c = m.chat.id
    mem = member(c, uid)
    lvl = await get_level(c, uid)
    total = q1("SELECT COALESCE(SUM(cnt),0) FROM msg_stats WHERE chat_id=? AND user_id=?", (c, uid))[0]
    tod = q1("SELECT COALESCE(SUM(cnt),0) FROM msg_stats WHERE chat_id=? AND user_id=? AND day=?", (c, uid, today()))[0]
    place = q1("SELECT COUNT(*)+1 FROM (SELECT SUM(cnt) s FROM msg_stats WHERE chat_id=? GROUP BY user_id) WHERE s>?", (c, total))[0]
    coins = q1("SELECT coins FROM users WHERE user_id=?", (uid,))
    aw = q("SELECT title FROM awards WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT 10", (c, uid))
    text = (f"👤 <b>Профиль</b>\n\n{shown(c, uid, name)}\n🆔 <code>{uid}</code>\n"
            f"🎖 Ранг: <b>{lvl}</b>\n🏷 Префикс: {esc(mem['prefix']) or '—'}\n"
            f"⚠️ Варны: <b>{mem['warns']}/{cget(c, 'warn_limit')}</b>\n\n"
            f"💬 Сообщений: <b>{total}</b> (сегодня {tod})\n🏅 Место в топе: <b>#{place}</b>\n"
            f"🪙 Монеты: <b>{coins[0] if coins else 0}</b>")
    if aw:
        text += "\n\n🏆 <b>Награды</b>\n" + "\n".join(f"• {esc(a['title'])}" for a in aw)
    await m.reply(text)


# ── статистика, топ, лог
@command("stats", "статистика", "стата")
async def c_stats(m, args):
    c = m.chat.id

    def s(since):
        r = q1("SELECT COALESCE(SUM(cnt),0), COUNT(DISTINCT user_id) FROM msg_stats WHERE chat_id=? AND day>=?", (c, since))
        return r[0], r[1]

    t, tu = s(today())
    w, wu = s(day_ago(6))
    mo, _ = s(day_ago(29))
    a, au = s("0000")
    try:
        members = await bot.get_chat_member_count(c)
    except TelegramAPIError:
        members = "—"

    def lg(action, since):
        return q1("SELECT COUNT(*) FROM chat_log WHERE chat_id=? AND action=? AND ts>=?", (c, action, since))[0]

    st, sw = day_start_ts(0), day_start_ts(6)
    days = []
    for i in range(6, -1, -1):
        d = day_ago(i)
        n = q1("SELECT COALESCE(SUM(cnt),0) FROM msg_stats WHERE chat_id=? AND day=?", (c, d))[0]
        days.append((d[5:].replace("-", "."), n))
    mx = max([n for _, n in days] + [1])
    chart = "\n".join(f"{d} {'▇' * round(n / mx * 10) or '·'} {n}" for d, n in days)
    await m.reply(
        f"📊 <b>Статистика чата</b>\n\n👥 Участников: <b>{members}</b>\n\n"
        f"💬 <b>Сообщения</b>\n• Сегодня: <b>{t}</b> ({tu} чел.)\n• 7 дней: <b>{w}</b> ({wu} чел.)\n"
        f"• 30 дней: <b>{mo}</b>\n• Всего: <b>{a}</b> ({au} чел.)\n\n<pre>{chart}</pre>\n"
        f"🛡 <b>Модерация (сегодня / 7 дней)</b>\n"
        f"🔨 Баны: {lg('ban', st)} / {lg('ban', sw)}\n🔇 Муты: {lg('mute', st)} / {lg('mute', sw)}\n"
        f"⚠️ Варны: {lg('warn', st)} / {lg('warn', sw)}\n📥 Вошло: {lg('join', st)} / {lg('join', sw)} · "
        f"📤 Вышло: {lg('leave', st)} / {lg('leave', sw)}")


PERIODS = {"day": ("сегодня", 0), "d": ("сегодня", 0), "день": ("сегодня", 0), "сегодня": ("сегодня", 0),
           "week": ("за 7 дней", 6), "w": ("за 7 дней", 6), "неделя": ("за 7 дней", 6), "неделю": ("за 7 дней", 6),
           "month": ("за 30 дней", 29), "m": ("за 30 дней", 29), "месяц": ("за 30 дней", 29),
           "all": ("за всё время", None), "все": ("за всё время", None), "всё": ("за всё время", None)}


@command("top", "топ")
async def c_top(m, args):
    label, n = PERIODS.get(args.strip().lower(), ("сегодня", 0))
    since = "0000" if n is None else day_ago(n)
    rows = q("SELECT user_id, SUM(cnt) c FROM msg_stats WHERE chat_id=? AND day>=? GROUP BY user_id ORDER BY c DESC LIMIT 10",
             (m.chat.id, since))
    if not rows:
        return await m.reply("📭 Пока нет данных.")
    medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
    lines = [f"🏆 <b>Топ чата по сообщениям — {label}</b>\n"]
    for i, r in enumerate(rows):
        lines.append(f"{medals[i]} {shown(m.chat.id, r['user_id'])} — <b>{r['c']}</b>")
    await m.reply("\n".join(lines))


@command("log", "лог")
async def c_log(m, args):
    if await need(m, 2) is None:
        return
    rows = q("SELECT * FROM chat_log WHERE chat_id=? ORDER BY id DESC LIMIT 15", (m.chat.id,))
    if not rows:
        return await m.reply("📭 Лог пуст.")
    lines = ["📜 <b>Лог чата (последние 15)</b>\n"]
    for r in rows:
        t = f"<code>{fmt_ts(r['ts'], True)}</code> {ICON.get(r['action'], '•')} <b>{r['action']}</b>"
        if r["actor_id"]:
            t += f" {esc(r['actor_name'])} →"
        if r["target_id"]:
            t += f" {esc(r['target_name'])}"
        if r["details"]:
            t += f" <i>({esc(r['details'])[:60]})</i>"
        lines.append(t)
    await m.reply("\n".join(lines))


@command("setlog", "логчат")
async def c_setlog(m, args):
    if await need(m, 4) is None:
        return
    a = args.strip()
    if a.lower() in ("off", "выкл", ""):
        cset(m.chat.id, "logchat", "")
        return await m.reply("📜 Пересылка лога отключена.")
    try:
        ch = await bot.get_chat(int(a) if a.lstrip("-").isdigit() else a)
        await bot.send_message(ch.id, f"✅ Сюда будет приходить лог чата <b>{esc(m.chat.title)}</b>")
    except (TelegramAPIError, ValueError) as e:
        return await m.reply(f"❌ Не могу писать туда: <code>{esc(e)}</code>\nДобавьте бота в чат/канал и повторите.")
    cset(m.chat.id, "logchat", ch.id)
    await m.reply(f"📜 Лог пересылается в <b>{esc(ch.title)}</b>")


# ── правила, приветствие, настройки
@command("rules", "правила")
async def c_rules(m, args):
    await m.reply(f"📜 <b>Правила чата</b>\n\n{cget(m.chat.id, 'rules')}")


def rest_html(m):
    parts = (m.html_text or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


@command("setrules", "задатьправила")
async def c_setrules(m, args):
    if await need(m, 4) is None:
        return
    t = rest_html(m)
    if not t:
        return await m.reply("❗ Формат: <code>/setrules текст</code>")
    cset(m.chat.id, "rules", t)
    await m.reply("✅ Правила сохранены.")


def fill(tpl, u, chat, count="—"):
    vals = {"name": esc(u.full_name), "mention": mention(u.id, u.full_name), "chat": esc(chat.title),
            "id": str(u.id), "count": str(count)}
    return re.sub(r"\{(\w+)\}", lambda mm: vals.get(mm.group(1), mm.group(0)), tpl)


@command("setwelcome", "приветствие")
async def c_setwelcome(m, args):
    if await need(m, 4) is None:
        return
    t = rest_html(m)
    if not t:
        return await m.reply("❗ Формат: <code>/setwelcome текст</code>\nПеременные: {mention} {name} {chat} {id} {count}\n"
                             f"Сейчас:\n{cget(m.chat.id, 'welcome_text')}")
    cset(m.chat.id, "welcome_text", t)
    await m.reply("✅ Приветствие сохранено. Предпросмотр:\n\n" + fill(t, m.from_user, m.chat))


def settings_screen(chat):
    def tog(key, label):
        on = cget(chat, key) == "1"
        return [B(f"{label}: {'ВКЛ ✅' if on else 'ВЫКЛ ❌'}", f"cs:{chat}:{key}", "success" if on else "danger")]

    lim = int(cget(chat, "warn_limit"))
    act = {"mute": "🔇 мут 1д", "ban": "🔨 бан", "kick": "👢 кик"}[cget(chat, "warn_action")]
    rows = [tog("welcome", "👋 Приветствие"), tog("captcha", "🤖 Капча при входе"), tog("antilink", "🔗 Анти-ссылки"),
            tog("antiflood", "💥 Анти-флуд"), tog("games", "🎮 Игры"),
            [B("➖", f"cs:{chat}:wl:-1", "primary"), B(f"⚠️ Лимит варнов: {lim}", f"cs:{chat}:noop"), B("➕", f"cs:{chat}:wl:1", "primary")],
            [B(f"При лимите: {act}", f"cs:{chat}:wa", "primary")],
            [B("✖️ Закрыть", f"cs:{chat}:close", "danger")]]
    return "⚙️ <b>Настройки чата</b>\n\nТекст приветствия: /setwelcome · правила: /setrules · лог-канал: /setlog", kb(rows)


@command("settings", "настройки")
async def c_settings(m, args):
    if await need(m, 4) is None:
        return
    text, markup = settings_screen(m.chat.id)
    await m.reply(text, reply_markup=markup)


# ── игры
LAST_GAME: dict = {}


def coins_of(uid):
    r = q1("SELECT coins FROM users WHERE user_id=?", (uid,))
    return r[0] if r else 0


def take_coins(uid, amt):
    return ex("UPDATE users SET coins=coins-? WHERE user_id=? AND coins>=?", (amt, uid, amt)).rowcount > 0


def add_coins(uid, amt):
    ex("UPDATE users SET coins=coins+? WHERE user_id=?", (amt, uid))


async def game_ok(m):
    if cget(m.chat.id, "games") != "1":
        await m.reply("🎮 Игры в этом чате выключены.")
        return False
    if time.time() - LAST_GAME.get(m.from_user.id, 0) < 4:
        await m.reply("⏳ Не так быстро.")
        return False
    LAST_GAME[m.from_user.id] = time.time()
    return True


def parse_bet(tok, uid):
    bal = coins_of(uid)
    if tok.lower() in ("все", "всё", "all"):
        return bal if bal >= 10 else None
    return int(tok) if tok.isdigit() and 10 <= int(tok) <= bal else None


BET_HELP = "❗ Укажите ставку: от 10 до вашего баланса (или «все»)."


@command("bal", "баланс", "монеты")
async def c_bal(m, args):
    uid, name, _ = await split_target(m, args)
    if not uid:
        uid, name = m.from_user.id, m.from_user.full_name
    await m.reply(f"🪙 Баланс {mention(uid, name)}: <b>{coins_of(uid)}</b>")


@command("daily", "бонус")
async def c_daily(m, args):
    last = q1("SELECT last_daily FROM users WHERE user_id=?", (m.from_user.id,))[0]
    if now() - last < 20 * 3600:
        left = 20 * 3600 - (now() - last)
        return await m.reply(f"⏳ Бонус уже получен. Следующий через <b>{fmt_dur(left)}</b>")
    amt = random.randint(300, 700)
    add_coins(m.from_user.id, amt)
    ex("UPDATE users SET last_daily=? WHERE user_id=?", (now(), m.from_user.id))
    await m.reply(f"🎁 Ежедневный бонус: <b>+{amt}</b> 🪙\nБаланс: <b>{coins_of(m.from_user.id)}</b>")


@command("coin", "монета")
async def c_coin(m, args):
    t = args.split()
    if len(t) < 2:
        return await m.reply("🪙 Формат: <code>/coin ставка орёл|решка</code>")
    bet = parse_bet(t[0], m.from_user.id)
    side = t[1].lower()[:1]
    if bet is None:
        return await m.reply(BET_HELP)
    if side not in ("о", "р", "h", "t"):
        return await m.reply("❗ Выберите: орёл или решка.")
    if not await game_ok(m) or not take_coins(m.from_user.id, bet):
        return
    pick = "о" if side in ("о", "h") else "р"
    res = random.choice(["о", "р"])
    name = {"о": "🦅 Орёл", "р": "🪙 Решка"}[res]
    if pick == res:
        add_coins(m.from_user.id, bet * 2)
        await m.reply(f"{name}! Вы выиграли <b>+{bet}</b> 🪙\nБаланс: <b>{coins_of(m.from_user.id)}</b>")
    else:
        await m.reply(f"{name}. Вы проиграли <b>-{bet}</b> 😢\nБаланс: <b>{coins_of(m.from_user.id)}</b>")


@command("dice", "кости")
async def c_dice(m, args):
    bet = parse_bet(args.strip(), m.from_user.id) if args.strip() else None
    if bet is None:
        return await m.reply("🎲 Формат: <code>/dice ставка</code>\n1–3 проигрыш · 4 возврат · 5 ×2 · 6 ×3")
    if not await game_ok(m) or not take_coins(m.from_user.id, bet):
        return
    d = await m.reply_dice(emoji="🎲")
    await asyncio.sleep(3.5)
    v = d.dice.value
    mult = {1: 0, 2: 0, 3: 0, 4: 1, 5: 2, 6: 3}[v]
    add_coins(m.from_user.id, bet * mult)
    res = "😢 проигрыш" if mult == 0 else "↩️ возврат ставки" if mult == 1 else f"🎉 выигрыш ×{mult}"
    await m.reply(f"🎲 Выпало <b>{v}</b> — {res}\nБаланс: <b>{coins_of(m.from_user.id)}</b>")


@command("slots", "слоты")
async def c_slots(m, args):
    bet = parse_bet(args.strip(), m.from_user.id) if args.strip() else None
    if bet is None:
        return await m.reply("🎰 Формат: <code>/slots ставка</code>\n777 — ×20 · три в ряд — ×10")
    if not await game_ok(m) or not take_coins(m.from_user.id, bet):
        return
    d = await m.reply_dice(emoji="🎰")
    await asyncio.sleep(3.5)
    v = d.dice.value
    mult = 20 if v == 64 else 10 if v in (1, 22, 43) else 0
    add_coins(m.from_user.id, bet * mult)
    res = "😢 не повезло" if not mult else ("💎 ДЖЕКПОТ" if v == 64 else "🎉 три в ряд") + f" ×{mult}"
    await m.reply(f"🎰 {res}\nБаланс: <b>{coins_of(m.from_user.id)}</b>")


@command("give", "перевести", "передать")
async def c_give(m, args):
    uid, name, rest = await split_target(m, args)
    if not uid or not rest.strip().isdigit() or int(rest.strip()) < 1:
        return await m.reply("💸 Формат: <code>/give @юзер сумма</code> (ответом или по @)")
    amt = int(rest.strip())
    if uid in (m.from_user.id, bot.id):
        return await m.reply("🙃 Так нельзя.")
    if not q1("SELECT 1 FROM users WHERE user_id=?", (uid,)):
        return await m.reply("❗ Я ещё не знаю этого пользователя.")
    if not take_coins(m.from_user.id, amt):
        return await m.reply("❌ Недостаточно монет.")
    add_coins(uid, amt)
    await m.reply(f"💸 {who(m)} передал {mention(uid, name)} <b>{amt}</b> 🪙")


@command("rich", "богачи")
async def c_rich(m, args):
    rows = q("SELECT u.user_id, u.name, u.coins FROM users u JOIN members mb ON mb.user_id=u.user_id AND mb.chat_id=? "
             "ORDER BY u.coins DESC LIMIT 10", (m.chat.id,))
    medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
    await m.reply("💰 <b>Самые богатые</b>\n\n" + "\n".join(
        f"{medals[i]} {shown(m.chat.id, r['user_id'], r['name'])} — <b>{r['coins']}</b> 🪙" for i, r in enumerate(rows)))


HELP = """<b>🤖 Orenix Manager — команды</b>

<b>🛡 Модерация</b> <i>(ответом / @юзер / ID)</i>
/ban [время] [причина] · /unban · /kick
/mute [время] [причина] · /unmute
/warn [причина] · /unwarn · /warns
<i>Время: 30m, 2h, 1d, 1w, навсегда</i>

<b>👑 Роли</b>
/promote [титул] · /demote · /rank @юзер 0-4 · /admins

<b>🏷 Профиль</b>
/prefix @юзер текст · /unprefix
/award @юзер 🏆 текст · /unaward · /me

<b>📊 Статистика</b>
/stats · /top [day|week|month|all] · /log

<b>⚙️ Чат</b>
/settings · /setwelcome · /setrules · /rules · /setlog

<b>🎮 Игры</b>
/bal · /daily · /dice 100 · /slots 100
/coin 100 орёл · /give @юзер 50 · /rich

<i>Работают и слова без слэша: «бан», «мут 1h», «топ week», «баланс».</i>"""


@command("help", "помощь", "команды")
async def c_help(m, args):
    await m.reply(HELP)


@command("start")
async def c_start(m, args):
    await m.reply("👋 Я <b>Orenix Manager</b>. Дайте мне права админа — и я займусь модерацией.\nКоманды: /help")


# ═════════════════════════ Роутеры ═════════════════════════
group_router = Router()
group_router.message.filter(F.chat.type.in_({"group", "supergroup"}))
pm_router = Router()
pm_router.message.filter(F.chat.type == "private")
cb_router = Router()

PENDING: set = set()
FLOOD: dict = defaultdict(deque)


@group_router.message(F.new_chat_members)
async def on_join(m: Message):
    c = m.chat
    for u in m.new_chat_members:
        if u.id == bot.id:
            ex("INSERT INTO chats(chat_id,title,type,added_at,active) VALUES(?,?,?,?,1) "
               "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, active=1", (c.id, c.title, c.type, now()))
            await m.answer("👋 Всем привет! Я <b>Orenix Manager</b>.\nСделайте меня админом (удаление сообщений, "
                           "блокировка, приглашения) — и пишите /help")
            continue
        if u.is_bot:
            continue
        touch(c, u)
        await log_action(c.id, "join", None, u.id, u.full_name)
        if cget(c.id, "captcha") == "1":
            try:
                await do_mute(c.id, u.id, 0)
            except TelegramAPIError:
                pass
            else:
                msg = await m.answer(fill(cget(c.id, "welcome_text"), u, c) + "\n\n🤖 <b>Нажми кнопку, чтобы писать в чате</b> (2 минуты)",
                                     reply_markup=kb([[B("✅ Я не бот", f"cap:{c.id}:{u.id}", "success")]]))
                PENDING.add((c.id, u.id))
                spawn(captcha_timeout(c.id, u.id, msg.message_id))
                continue
        if cget(c.id, "welcome") == "1":
            try:
                n = await bot.get_chat_member_count(c.id)
            except TelegramAPIError:
                n = "—"
            await m.answer(fill(cget(c.id, "welcome_text"), u, c, n))


async def captcha_timeout(chat_id, uid, msg_id):
    await asyncio.sleep(120)
    if (chat_id, uid) not in PENDING:
        return
    PENDING.discard((chat_id, uid))
    try:
        await bot.ban_chat_member(chat_id, uid)
        await bot.unban_chat_member(chat_id, uid, only_if_banned=True)
        await bot.delete_message(chat_id, msg_id)
    except TelegramAPIError:
        pass
    await log_action(chat_id, "captcha", None, uid, uname(uid), "не прошёл капчу — кик")


@group_router.message(F.left_chat_member)
async def on_leave(m: Message):
    u = m.left_chat_member
    if u.id != bot.id and not u.is_bot:
        await log_action(m.chat.id, "leave", None, u.id, u.full_name)


@group_router.message()
async def on_group_message(m: Message):
    u = m.from_user
    if u is None or u.is_bot:
        return
    c = m.chat.id
    touch(m.chat, u)
    ex("INSERT INTO msg_stats VALUES(?,?,?,1) ON CONFLICT(chat_id,user_id,day) DO UPDATE SET cnt=cnt+1", (c, u.id, today()))

    key, args = parse_cmd(m.text or m.caption)
    if key:
        try:
            await CMDS[key](m, args)
        except Exception:
            logging.exception("command %s failed", key)
            await m.reply("⚠️ Что-то пошло не так.")
        return

    # фильтры
    if cget(c, "antilink") == "1" or cget(c, "antiflood") == "1":
        if await get_level(c, u.id) >= 1:
            return
    if cget(c, "antilink") == "1":
        ents = (m.entities or []) + (m.caption_entities or [])
        if any(e.type in ("url", "text_link") for e in ents) or "t.me/" in (m.text or m.caption or ""):
            try:
                await m.delete()
                note = await m.answer(f"🔗 {mention(u.id, u.full_name)}, ссылки в этом чате запрещены.")
                spawn(delete_later(note, 6))
                await log_action(c, "antilink", None, u.id, u.full_name)
            except TelegramAPIError:
                pass
            return
    if cget(c, "antiflood") == "1":
        dq = FLOOD[(c, u.id)]
        t = time.time()
        dq.append(t)
        while dq and t - dq[0] > 6:
            dq.popleft()
        if len(dq) >= 7:
            dq.clear()
            try:
                await do_mute(c, u.id, 300)
                await m.answer(f"💥 {mention(u.id, u.full_name)} в муте на 5 минут за флуд.")
                await log_action(c, "flood", None, u.id, u.full_name, "мут 5м")
            except TelegramAPIError:
                pass


async def delete_later(msg, sec):
    await asyncio.sleep(sec)
    try:
        await msg.delete()
    except TelegramAPIError:
        pass


@dp.my_chat_member()
async def on_my_chat_member(ev: ChatMemberUpdated):
    if ev.chat.type == "private":
        return
    st = ev.new_chat_member.status
    if st in ("left", "kicked"):
        ex("UPDATE chats SET active=0 WHERE chat_id=?", (ev.chat.id,))
    elif st in ("member", "administrator"):
        ex("INSERT INTO chats(chat_id,title,type,added_at,active) VALUES(?,?,?,?,1) "
           "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, active=1", (ev.chat.id, ev.chat.title, ev.chat.type, now()))
        try:
            await bot.send_message(OWNER_ID, f"➕ Меня добавили в <b>{esc(ev.chat.title)}</b> (<code>{ev.chat.id}</code>) — {esc(ev.from_user.full_name)}")
        except TelegramAPIError:
            pass


# ═════════════════════════ Callback'и (группы) ═════════════════════════
async def edit(cb: CallbackQuery, text, markup=None):
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except (TelegramBadRequest, AttributeError):
        pass


@cb_router.callback_query(F.data.startswith("qa:"))
async def cb_qa(cb: CallbackQuery):
    _, kind, chat, uid = cb.data.split(":")
    chat, uid = int(chat), int(uid)
    need_lvl = {"unwarn": 1, "unmute": 1, "unban": 2}[kind]
    if await get_level(chat, cb.from_user.id) < need_lvl:
        return await cb.answer("⛔ Недостаточно прав", show_alert=True)
    try:
        if kind == "unban":
            await bot.unban_chat_member(chat, uid, only_if_banned=True)
        elif kind == "unmute":
            await bot.restrict_chat_member(chat, uid, permissions=FULL)
        else:
            member(chat, uid)
            ex("UPDATE members SET warns=MAX(warns-1,0) WHERE chat_id=? AND user_id=?", (chat, uid))
    except TelegramAPIError as e:
        return await cb.answer(f"Ошибка: {e}"[:190], show_alert=True)
    await log_action(chat, kind, cb.from_user, uid, uname(uid), "кнопкой")
    await cb.answer("Готово ✅")
    await edit(cb, (cb.message.html_text or "") + f"\n\n✅ Отменено — {esc(cb.from_user.full_name)}")


@cb_router.callback_query(F.data.startswith("cap:"))
async def cb_captcha(cb: CallbackQuery):
    _, chat, uid = cb.data.split(":")
    chat, uid = int(chat), int(uid)
    if cb.from_user.id != uid:
        return await cb.answer("Эта кнопка не для тебя 🙂", show_alert=True)
    PENDING.discard((chat, uid))
    try:
        await bot.restrict_chat_member(chat, uid, permissions=FULL)
    except TelegramAPIError:
        pass
    await cb.answer("Добро пожаловать! ✅")
    txt = re.sub(r"\n\n🤖.*$", "", cb.message.html_text or "", flags=re.S)
    await edit(cb, txt)


@cb_router.callback_query(F.data.startswith("cs:"))
async def cb_settings(cb: CallbackQuery):
    p = cb.data.split(":")
    chat, act = int(p[1]), p[2]
    if await get_level(chat, cb.from_user.id) < 4:
        return await cb.answer("⛔ Нужен ранг 4+", show_alert=True)
    if act == "close":
        await cb.answer()
        try:
            await cb.message.delete()
        except (TelegramAPIError, AttributeError):
            pass
        return
    if act in ("welcome", "captcha", "antilink", "antiflood", "games"):
        cset(chat, act, "0" if cget(chat, act) == "1" else "1")
        await log_action(chat, "settings", cb.from_user, None, "", f"{act}={cget(chat, act)}")
    elif act == "wl":
        cset(chat, "warn_limit", max(1, min(10, int(cget(chat, "warn_limit")) + int(p[3]))))
    elif act == "wa":
        order = ["mute", "ban", "kick"]
        cset(chat, "warn_action", order[(order.index(cget(chat, "warn_action")) + 1) % 3])
    await cb.answer()
    text, markup = settings_screen(chat)
    await edit(cb, text, markup)


# ═════════════════════════ Розыгрыши ═════════════════════════
def gget(gid):
    r = q1("SELECT * FROM giveaways WHERE id=?", (gid,))
    return dict(r) if r else None


def gcount(gid):
    return q1("SELECT COUNT(*) FROM gparts WHERE giveaway_id=?", (gid,))[0]


def gv_text(g):
    subs = json.loads(g["subs"])
    lines = ["🎁 <b>РОЗЫГРЫШ</b>", "", g["text"], "", f"🏆 Победителей: <b>{g['winners']}</b>",
             f"⏰ Итоги: <b>{fmt_ts(g['ends_at'])}</b> (UTC{TZ_OFFSET:+d})"]
    if subs:
        lines.append("📌 Условие: подписаться на " + ", ".join(
            f'<a href="{s["link"]}">{esc(s["title"])}</a>' if s.get("link") else esc(s["title"]) for s in subs))
    lines.append("\nЖми кнопку ниже, чтобы участвовать 👇")
    return "\n".join(lines)


def gv_kb(g, count):
    rows = [[B(f"🎉 Участвовать ({count})", f"gv:j:{g['id']}", "success")]]
    subs = [B(f"📢 {s['title'][:22]}", url=s["link"], style="primary") for s in json.loads(g["subs"]) if s.get("link")]
    rows += [subs[i:i + 2] for i in range(0, len(subs), 2)]
    return kb(rows)


def post_link(g):
    if g["channel_username"]:
        return f"https://t.me/{g['channel_username']}/{g['message_id']}"
    return f"https://t.me/c/{str(g['channel_id']).replace('-100', '', 1)}/{g['message_id']}"


async def missing_subs(subs, uid):
    out = []
    for s in subs:
        try:
            st = (await bot.get_chat_member(s["id"], uid)).status
            if st in ("left", "kicked"):
                out.append(s)
        except TelegramAPIError as e:
            logging.warning("sub check failed for %s: %s", s["id"], e)
    return out


async def gv_publish(g):
    text, markup = gv_text(g), gv_kb(g, 0)
    ch = g["channel_id"]
    has_photo = 0
    if g["photo"] and len(text) <= 1000:
        msg = await bot.send_photo(ch, g["photo"], caption=text, reply_markup=markup)
        has_photo = 1
    else:
        if g["photo"]:
            await bot.send_photo(ch, g["photo"])
        msg = await bot.send_message(ch, text, reply_markup=markup)
    ex("UPDATE giveaways SET message_id=?, has_photo=? WHERE id=?", (msg.message_id, has_photo, g["id"]))


async def gv_refresh(gid):
    g = gget(gid)
    if not g or g["status"] != "active":
        return
    n = gcount(gid)
    try:
        await bot.edit_message_reply_markup(chat_id=g["channel_id"], message_id=g["message_id"], reply_markup=gv_kb(g, n))
    except TelegramBadRequest:
        pass
    except TelegramAPIError as e:
        logging.warning("refresh failed: %s", e)
    ex("UPDATE giveaways SET last_count=?, last_edit=? WHERE id=?", (n, now(), gid))


@cb_router.callback_query(F.data.startswith("gv:j:"))
async def gv_join(cb: CallbackQuery):
    gid = int(cb.data.split(":")[2])
    g = gget(gid)
    if not g or g["status"] != "active":
        return await cb.answer("🏁 Розыгрыш уже завершён", show_alert=True)
    u = cb.from_user
    if q1("SELECT 1 FROM gparts WHERE giveaway_id=? AND user_id=?", (gid, u.id)):
        return await cb.answer("✅ Ты уже участвуешь! Жди итогов.", show_alert=True)
    miss = await missing_subs(json.loads(g["subs"]), u.id)
    if miss:
        names = ", ".join(("@" + s["username"]) if s.get("username") else s["title"] for s in miss)
        return await cb.answer(f"❌ Сначала подпишись: {names}\nПотом нажми «Участвовать» ещё раз.", show_alert=True)
    touch(None, u)
    ex("INSERT OR IGNORE INTO gparts VALUES(?,?,?,?)", (gid, u.id, u.full_name, now()))
    await cb.answer("🎉 Ты участвуешь! Удачи!", show_alert=True)
    if now() - g["last_edit"] >= 5:
        await gv_refresh(gid)


async def finish_giveaway(gid, reroll=False):
    g = gget(gid)
    if not g or (g["status"] != "active" and not reroll):
        return
    subs = json.loads(g["subs"])
    parts = [dict(r) for r in q("SELECT user_id, name FROM gparts WHERE giveaway_id=?", (gid,))]
    random.shuffle(parts)
    winners = []
    for p in parts:
        if len(winners) >= g["winners"]:
            break
        if subs and await missing_subs(subs, p["user_id"]):
            continue
        winners.append(p)
    ex("UPDATE giveaways SET status='ended', winner_ids=? WHERE id=?", (json.dumps([w["user_id"] for w in winners]), gid))
    ch, mid = g["channel_id"], g["message_id"]

    if not reroll:
        orig = g["text"]
        tail = (f"\n\n🏁 <b>РОЗЫГРЫШ ЗАВЕРШЁН</b>\n🏆 Победителей: {len(winners)} · 👥 Участников: {len(parts)}\n⬇️ Итоги — в сообщении ниже")
        limit = 1000 if g["has_photo"] else 4000
        body = f"🎁 {orig}"
        if len(body) + len(tail) > limit:
            body = "🎁 Розыгрыш"
        try:
            if g["has_photo"]:
                await bot.edit_message_caption(chat_id=ch, message_id=mid, caption=body + tail)
            else:
                await bot.edit_message_text(body + tail, chat_id=ch, message_id=mid)
        except TelegramAPIError as e:
            logging.warning("edit post failed: %s", e)

    if winners:
        lst = "\n".join(f"{i}. {mention(w['user_id'], w['name'])}" for i, w in enumerate(winners, 1))
        title = "🔄 <b>ПЕРЕИГРОВКА — новые победители!</b>" if reroll else "🎊 <b>ИТОГИ РОЗЫГРЫША</b>"
        res = f"{title}\n\n🏆 <b>Победители:</b>\n{lst}\n\n👥 Участников: {len(parts)}\nПоздравляем! 🥳"
    else:
        res = "😔 <b>Розыгрыш завершён</b>\n\nНет подходящих участников — победителей нет."
    try:
        await bot.send_message(ch, res, reply_parameters=ReplyParameters(message_id=mid, allow_sending_without_reply=True))
    except TelegramAPIError as e:
        logging.warning("announce failed: %s", e)
    for w in winners:
        try:
            await bot.send_message(w["user_id"], f"🎉 <b>Поздравляем!</b> Ты выиграл в розыгрыше!\n👉 <a href=\"{post_link(g)}\">Перейти к посту</a>")
        except TelegramAPIError:
            pass
    try:
        await bot.send_message(g["owner_id"], f"🏁 Розыгрыш #{gid} в <b>{esc(g['channel_title'])}</b> завершён.\n"
                                              f"👥 Участников: {len(parts)} · 🏆 Победителей: {len(winners)}")
    except TelegramAPIError:
        pass


async def scheduler():
    await asyncio.sleep(5)
    while True:
        try:
            for r in q("SELECT id FROM giveaways WHERE status='active' AND ends_at<=?", (now(),)):
                await finish_giveaway(r["id"])
            for r in q("SELECT id, last_count FROM giveaways WHERE status='active'"):
                if gcount(r["id"]) != r["last_count"]:
                    await gv_refresh(r["id"])
        except Exception:
            logging.exception("scheduler error")
        await asyncio.sleep(15)


# ═════════════════════════ ЛС: старт, админ-панель, мастер розыгрыша ═════════════════════════
class GW(StatesGroup):
    channel = State()
    text = State()
    winners = State()
    ends = State()
    subs = State()
    photo = State()


class AP(StatesGroup):
    bc = State()
    add_admin = State()


def pm_start_screen():
    url = (f"https://t.me/{BOT_USERNAME}?startgroup=true&admin=delete_messages+restrict_members+"
           f"invite_users+pin_messages+promote_members+manage_chat")
    text = ("👋 <b>Привет! Я Orenix Manager</b>\n\n"
            "🛡 Баны, муты, варны, роли, префиксы и награды\n📊 Статистика, топ и лог чата\n"
            "👋 Приветствия и капча · 🎮 Игры на монеты\n🎁 Розыгрыши в каналах с обязательной подпиской\n\n"
            "Добавь меня в чат и сделай админом 👇")
    return text, kb([[B("➕ Добавить в чат", style="success", url=url)],
                     [B("📖 Команды", "pm:help", "primary"), B("🎁 Розыгрыши", "ap:main", "danger")]])


@pm_router.message(CommandStart())
async def pm_start(m: Message, state: FSMContext):
    await state.clear()
    touch(None, m.from_user)
    ex("UPDATE users SET pm=1 WHERE user_id=?", (m.from_user.id,))
    text, markup = pm_start_screen()
    await m.answer(text, reply_markup=markup)


@pm_router.message(Command("help"))
async def pm_help(m: Message):
    await m.answer(HELP)


@pm_router.message(Command("cancel"))
async def pm_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("✖️ Отменено.")


@pm_router.message(Command("admin"))
async def pm_admin(m: Message, state: FSMContext):
    await state.clear()
    if not is_gadmin(m.from_user.id):
        return await m.answer("⛔ Доступ только для админов бота.")
    text, markup = ap_main(m.from_user.id)
    await m.answer(text, reply_markup=markup)


@pm_router.message(Command("giveaway"))
async def pm_giveaway(m: Message, state: FSMContext):
    if not is_gadmin(m.from_user.id):
        return await m.answer("⛔ Создавать розыгрыши могут только админы бота.")
    await gw_begin(m, state)


def cancel_kb():
    return kb([[B("✖️ Отмена", "ap:cancel", "danger")]])


async def gw_begin(target, state):
    await state.clear()
    await state.set_state(GW.channel)
    msg = target.message if isinstance(target, CallbackQuery) else target
    await msg.answer("🎁 <b>Новый розыгрыш — шаг 1/6</b>\n\nОтправь @username или ID канала, где опубликовать пост.\n"
                     "Бот должен быть <b>админом</b> этого канала с правом публикации.", reply_markup=cancel_kb())


async def resolve_chat(s):
    s = s.strip()
    if s.startswith("https://t.me/"):
        s = "@" + s.rsplit("/", 1)[1]
    key = int(s) if re.fullmatch(r"-?\d+", s) else "@" + s.lstrip("@")
    chat = await bot.get_chat(key)
    me = await bot.get_chat_member(chat.id, bot.id)
    return chat, me


async def chat_link(chat):
    if chat.username:
        return f"https://t.me/{chat.username}"
    if getattr(chat, "invite_link", None):
        return chat.invite_link
    try:
        return (await bot.create_chat_invite_link(chat.id)).invite_link
    except TelegramAPIError:
        return None


@pm_router.message(GW.channel)
async def gw_channel(m: Message, state: FSMContext):
    try:
        chat, me = await resolve_chat(m.text or "")
    except (TelegramAPIError, ValueError, IndexError) as e:
        return await m.answer(f"❌ Не нашёл канал: <code>{esc(e)}</code>\nПроверь @username/ID и что бот добавлен.")
    if me.status not in ("administrator", "creator") or (chat.type == "channel" and not getattr(me, "can_post_messages", True)):
        return await m.answer("❌ Бот не админ в этом канале или нет права публикации. Исправь и отправь снова.")
    if m.from_user.id != OWNER_ID:
        try:
            st = (await bot.get_chat_member(chat.id, m.from_user.id)).status
        except TelegramAPIError:
            st = "member"
        if st not in ("administrator", "creator"):
            return await m.answer("⛔ Ты не админ этого канала.")
    await state.update_data(channel_id=chat.id, channel_title=chat.title, channel_username=chat.username)
    await state.set_state(GW.text)
    await m.answer(f"✅ Канал: <b>{esc(chat.title)}</b>\n\n<b>Шаг 2/6.</b> Отправь текст розыгрыша: приз и условия "
                   f"(до 700 символов, можно форматирование).", reply_markup=cancel_kb())


@pm_router.message(GW.text)
async def gw_text(m: Message, state: FSMContext):
    if not m.text or len(m.text) > 700:
        return await m.answer("❌ Нужен текст до 700 символов.")
    await state.update_data(text=m.html_text)
    await state.set_state(GW.winners)
    await m.answer("<b>Шаг 3/6.</b> Сколько победителей? (1–50)", reply_markup=cancel_kb())


@pm_router.message(GW.winners)
async def gw_winners(m: Message, state: FSMContext):
    if not (m.text or "").strip().isdigit() or not 1 <= int(m.text.strip()) <= 50:
        return await m.answer("❌ Число от 1 до 50.")
    await state.update_data(winners=int(m.text.strip()))
    await state.set_state(GW.ends)
    await m.answer(f"<b>Шаг 4/6.</b> Когда подвести итоги?\n• Через: <code>30m</code>, <code>2h</code>, <code>1d</code>\n"
                   f"• Дата: <code>25.12 18:00</code> или <code>25.12.2026 18:00</code> (UTC{TZ_OFFSET:+d})", reply_markup=cancel_kb())


def parse_end(s):
    s = s.strip()
    d = parse_dur(s)
    if d:
        return now() + d
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt == "%d.%m %H:%M":
                dt = dt.replace(year=datetime.now(TZ).year)
            return int(dt.replace(tzinfo=TZ).timestamp())
        except ValueError:
            pass
    return None


@pm_router.message(GW.ends)
async def gw_ends(m: Message, state: FSMContext):
    ts = parse_end(m.text or "")
    if not ts or ts < now() + 60:
        return await m.answer("❌ Не понял время или оно в прошлом (минимум +1 минута).")
    await state.update_data(ends_at=ts)
    await state.set_state(GW.subs)
    await m.answer("<b>Шаг 5/6.</b> Обязательная подписка: отправь @каналы через пробел (до 5) или <code>нет</code>.\n"
                   "Бот должен быть <b>админом</b> в каждом, чтобы проверять подписку.", reply_markup=cancel_kb())


@pm_router.message(GW.subs)
async def gw_subs(m: Message, state: FSMContext):
    raw = (m.text or "").strip()
    subs = []
    if raw.lower() not in ("нет", "no", "-"):
        for tok in re.split(r"[\s,]+", raw)[:5]:
            try:
                chat, me = await resolve_chat(tok)
            except (TelegramAPIError, ValueError, IndexError):
                return await m.answer(f"❌ Не нашёл <code>{esc(tok)}</code>. Бот добавлен туда?")
            if me.status not in ("administrator", "creator"):
                return await m.answer(f"❌ Бот не админ в <b>{esc(chat.title)}</b> — не сможет проверять подписку.")
            subs.append({"id": chat.id, "title": chat.title, "username": chat.username, "link": await chat_link(chat)})
    await state.update_data(subs=subs)
    await state.set_state(GW.photo)
    await m.answer("<b>Шаг 6/6.</b> Отправь фото для поста или <code>нет</code>.", reply_markup=cancel_kb())


@pm_router.message(GW.photo)
async def gw_photo(m: Message, state: FSMContext):
    photo = m.photo[-1].file_id if m.photo else None
    if not photo and (m.text or "").strip().lower() not in ("нет", "no", "-"):
        return await m.answer("❌ Отправь фото или напиши «нет».")
    d = await state.get_data()
    g = {"id": 0, "text": d["text"], "winners": d["winners"], "ends_at": d["ends_at"], "subs": json.dumps(d["subs"])}
    await state.update_data(photo=photo)
    text = gv_text(g)
    markup = kb([[B("🚀 Опубликовать", "gw:ok", "success"), B("✖️ Отмена", "ap:cancel", "danger")]])
    await m.answer("👀 <b>Предпросмотр поста:</b>")
    if photo and len(text) <= 1000:
        await m.answer_photo(photo, caption=text)
    else:
        await m.answer(text)
    await m.answer(f"Канал: <b>{esc(d['channel_title'])}</b>. Публикуем?", reply_markup=markup)


@cb_router.callback_query(F.data == "gw:ok")
async def gw_confirm(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if not d.get("channel_id") or not is_gadmin(cb.from_user.id):
        return await cb.answer("Сессия устарела, начните заново: /giveaway", show_alert=True)
    cur = ex("INSERT INTO giveaways(owner_id,channel_id,channel_username,channel_title,text,winners,ends_at,subs,photo,created_at) "
             "VALUES(?,?,?,?,?,?,?,?,?,?)",
             (cb.from_user.id, d["channel_id"], d["channel_username"], d["channel_title"], d["text"], d["winners"],
              d["ends_at"], json.dumps(d["subs"]), d.get("photo"), now()))
    gid = cur.lastrowid
    try:
        await gv_publish(gget(gid))
    except TelegramAPIError as e:
        ex("DELETE FROM giveaways WHERE id=?", (gid,))
        return await cb.answer(f"Не удалось опубликовать: {e}"[:190], show_alert=True)
    await state.clear()
    await cb.answer("Опубликовано! 🎉")
    await cb.message.edit_text(f"🚀 Розыгрыш <b>#{gid}</b> опубликован в <b>{esc(d['channel_title'])}</b>.\n"
                               f"Итоги: {fmt_ts(d['ends_at'])}\nУправление: /admin → 🎁 Розыгрыши",
                               reply_markup=kb([[B("🎁 Мои розыгрыши", "ap:gl", "primary")]]))


# ── админ-панель
def ap_main(uid):
    rows = [[B("📊 Статистика бота", "ap:stats", "primary")],
            [B("➕ Создать розыгрыш", "ap:gnew", "success"), B("🎁 Розыгрыши", "ap:gl", "success")],
            [B("💬 Чаты", "ap:chats", "primary")]]
    if uid == OWNER_ID:
        rows.append([B("📢 Рассылка", "ap:bc", "danger"), B("👮 Админы бота", "ap:adm", "danger")])
    return "🛠 <b>Админ-панель Orenix</b>\n\nВыбери раздел:", kb(rows)


def back(cbd="ap:main"):
    return [B("⬅️ Назад", cbd)]


def ap_stats():
    one = lambda s, p=(): q1(s, p)[0]  # noqa: E731
    st = day_start_ts(0)
    text = (
        "📊 <b>Статистика бота</b>\n\n"
        f"👤 Пользователей: <b>{one('SELECT COUNT(*) FROM users')}</b> (в ЛС: {one('SELECT COUNT(*) FROM users WHERE pm=1')})\n"
        f"🆕 Новых сегодня: <b>{one('SELECT COUNT(*) FROM users WHERE first_seen>=?', (st,))}</b>\n"
        f"💬 Чатов: <b>{one('SELECT COUNT(*) FROM chats WHERE active=1')}</b>\n\n"
        f"✉️ Сообщений: <b>{one('SELECT COALESCE(SUM(cnt),0) FROM msg_stats')}</b> "
        f"(сегодня {one('SELECT COALESCE(SUM(cnt),0) FROM msg_stats WHERE day=?', (today(),))})\n"
        f"🔨 Банов сегодня: <b>{one('SELECT COUNT(*) FROM chat_log WHERE action=? AND ts>=?', ('ban', st))}</b> · "
        f"🔇 мутов: <b>{one('SELECT COUNT(*) FROM chat_log WHERE action=? AND ts>=?', ('mute', st))}</b> · "
        f"⚠️ варнов: <b>{one('SELECT COUNT(*) FROM chat_log WHERE action=? AND ts>=?', ('warn', st))}</b>\n\n"
        f"🎁 Розыгрышей: активных <b>{one('SELECT COUNT(*) FROM giveaways WHERE status=?', ('active',))}</b>, "
        f"завершённых <b>{one('SELECT COUNT(*) FROM giveaways WHERE status=?', ('ended',))}</b>\n"
        f"🙋 Участий всего: <b>{one('SELECT COUNT(*) FROM gparts')}</b>"
    )
    return text, kb([[B("🔄 Обновить", "ap:stats", "primary")], back()])


def adm_screen():
    rows_db = q("SELECT user_id FROM gadmins")
    rows = [[B(f"🗑 {uname(r['user_id'])} · {r['user_id']}", f"ap:admdel:{r['user_id']}", "danger")] for r in rows_db]
    rows.append([B("➕ Добавить", "ap:admadd", "success")])
    rows.append(back())
    return "👮 <b>Админы бота</b>\nМогут создавать розыгрыши и видеть статистику.", kb(rows)


def gl_screen(uid):
    rows_db = q("SELECT * FROM giveaways ORDER BY id DESC LIMIT 15") if uid == OWNER_ID else \
        q("SELECT * FROM giveaways WHERE owner_id=? ORDER BY id DESC LIMIT 15", (uid,))
    icon = {"active": "🟢", "ended": "🏁", "cancelled": "⛔"}
    rows = [[B(f"{icon.get(r['status'], '•')} #{r['id']} · {r['channel_title'][:18]} · {gcount(r['id'])} уч.", f"ap:g:{r['id']}",
               "success" if r["status"] == "active" else None)] for r in rows_db]
    rows.append([B("➕ Создать розыгрыш", "ap:gnew", "success")])
    rows.append(back())
    return "🎁 <b>Розыгрыши</b>" + ("" if rows_db else "\n\nПока пусто."), kb(rows)


def g_screen(gid):
    g = gget(gid)
    st = {"active": "🟢 идёт", "ended": "🏁 завершён", "cancelled": "⛔ отменён"}[g["status"]]
    text = (f"🎁 <b>Розыгрыш #{gid}</b>\n\n📣 Канал: <b>{esc(g['channel_title'])}</b>\nСтатус: {st}\n"
            f"🏆 Победителей: {g['winners']}\n⏰ Итоги: {fmt_ts(g['ends_at'])}\n👥 Участников: <b>{gcount(gid)}</b>\n"
            f"🔗 <a href=\"{post_link(g)}\">Пост</a>")
    rows = []
    if g["status"] == "active":
        rows.append([B("🏁 Завершить сейчас", f"ap:gend:{gid}", "success"), B("⛔ Отменить", f"ap:gcan:{gid}", "danger")])
    elif g["status"] == "ended":
        rows.append([B("🔄 Переиграть", f"ap:grr:{gid}", "primary")])
    rows.append(back("ap:gl"))
    return text, kb(rows)


async def owner_of(uid, gid):
    g = gget(gid)
    return g if g and (uid == OWNER_ID or g["owner_id"] == uid) else None


@cb_router.callback_query(F.data.startswith("pm:"))
async def cb_pm(cb: CallbackQuery):
    await cb.answer()
    if cb.data == "pm:help":
        await cb.message.answer(HELP)


@cb_router.callback_query(F.data.startswith("ap:"))
async def cb_ap(cb: CallbackQuery, state: FSMContext):
    p = cb.data.split(":")
    act, uid = p[1], cb.from_user.id
    if not is_gadmin(uid):
        return await cb.answer("⛔ Только для админов бота", show_alert=True)
    if act in ("adm", "admadd", "admdel", "bc", "bcgo") and uid != OWNER_ID:
        return await cb.answer("⛔ Только владелец", show_alert=True)
    await cb.answer()

    if act in ("main", "cancel"):
        await state.clear()
        t, k = ap_main(uid)
        await edit(cb, t, k)
    elif act == "stats":
        await edit(cb, *ap_stats())
    elif act == "gnew":
        await gw_begin(cb, state)
    elif act == "gl":
        await edit(cb, *gl_screen(uid))
    elif act == "g":
        if await owner_of(uid, int(p[2])):
            await edit(cb, *g_screen(int(p[2])))
    elif act in ("gend", "gcan", "grr"):
        gid = int(p[2])
        g = await owner_of(uid, gid)
        if not g:
            return
        if act == "gend":
            await finish_giveaway(gid)
        elif act == "grr":
            await finish_giveaway(gid, reroll=True)
        else:
            ex("UPDATE giveaways SET status='cancelled' WHERE id=?", (gid,))
            try:
                body = "⛔ <b>Розыгрыш отменён</b>"
                if g["has_photo"]:
                    await bot.edit_message_caption(chat_id=g["channel_id"], message_id=g["message_id"], caption=body)
                else:
                    await bot.edit_message_text(body, chat_id=g["channel_id"], message_id=g["message_id"])
            except TelegramAPIError:
                pass
        await edit(cb, *g_screen(gid))
    elif act == "chats":
        rows_db = q("SELECT c.chat_id, c.title, COALESCE((SELECT SUM(cnt) FROM msg_stats s WHERE s.chat_id=c.chat_id),0) n "
                    "FROM chats c WHERE c.active=1 ORDER BY n DESC LIMIT 15")
        rows = [[B(f"{(r['title'] or r['chat_id'])[:26]} · {r['n']} сообщ.", f"ap:chat:{r['chat_id']}", "primary")] for r in rows_db]
        rows.append(back())
        await edit(cb, "💬 <b>Чаты, где я работаю</b>" + ("" if rows_db else "\n\nПока нет."), kb(rows))
    elif act == "chat":
        cid = int(p[2])
        r = q1("SELECT * FROM chats WHERE chat_id=?", (cid,))
        if not r:
            return
        n = q1("SELECT COALESCE(SUM(cnt),0) FROM msg_stats WHERE chat_id=?", (cid,))[0]
        try:
            mc = await bot.get_chat_member_count(cid)
        except TelegramAPIError:
            mc = "—"
        rows = []
        if uid == OWNER_ID:
            rows.append([B("🚪 Покинуть чат", f"ap:leave:{cid}", "danger")])
        rows.append(back("ap:chats"))
        await edit(cb, f"💬 <b>{esc(r['title'])}</b>\n🆔 <code>{cid}</code>\n👥 Участников: {mc}\n✉️ Сообщений: {n}", kb(rows))
    elif act == "leave":
        try:
            await bot.leave_chat(int(p[2]))
            ex("UPDATE chats SET active=0 WHERE chat_id=?", (int(p[2]),))
        except TelegramAPIError:
            pass
        await edit(cb, "🚪 Готово.", kb([back("ap:chats")]))
    elif act == "adm":
        await edit(cb, *adm_screen())
    elif act == "admadd":
        await state.set_state(AP.add_admin)
        await edit(cb, "👤 Отправь ID нового админа (число).", cancel_kb())
    elif act == "admdel":
        ex("DELETE FROM gadmins WHERE user_id=?", (int(p[2]),))
        await edit(cb, *adm_screen())
    elif act == "bc":
        await state.set_state(AP.bc)
        await edit(cb, "📢 Отправь текст рассылки (можно форматирование).", cancel_kb())
    elif act == "bcgo":
        d = await state.get_data()
        text = d.get("bc_text")
        if not text:
            return await edit(cb, "Сессия устарела.", kb([back()]))
        await state.clear()
        targets = []
        if p[2] in ("users", "all"):
            targets += [r[0] for r in q("SELECT user_id FROM users WHERE pm=1")]
        if p[2] in ("chats", "all"):
            targets += [r[0] for r in q("SELECT chat_id FROM chats WHERE active=1")]
        await edit(cb, f"📢 Отправляю в {len(targets)} адресов…")
        ok = 0
        for t in targets:
            try:
                await bot.send_message(t, text)
                ok += 1
            except TelegramAPIError:
                pass
            await asyncio.sleep(0.05)
        await cb.message.answer(f"✅ Рассылка завершена: доставлено <b>{ok}</b> из {len(targets)}")


@pm_router.message(AP.add_admin)
async def ap_add_admin(m: Message, state: FSMContext):
    if m.from_user.id != OWNER_ID or not (m.text or "").strip().isdigit():
        return await m.answer("❌ Нужен числовой ID.")
    ex("INSERT OR IGNORE INTO gadmins VALUES(?)", (int(m.text.strip()),))
    await state.clear()
    await m.answer("✅ Админ добавлен. /admin")


@pm_router.message(AP.bc)
async def ap_bc(m: Message, state: FSMContext):
    if m.from_user.id != OWNER_ID or not m.text:
        return
    await state.update_data(bc_text=m.html_text)
    await m.answer("Кому отправить?", reply_markup=kb([
        [B("👤 Пользователям", "ap:bcgo:users", "primary"), B("💬 Чатам", "ap:bcgo:chats", "primary")],
        [B("🌐 Всем", "ap:bcgo:all", "success")], [B("✖️ Отмена", "ap:cancel", "danger")]]))


# ═════════════════════════ Запуск ═════════════════════════
async def on_startup(bot: Bot):
    global BOT_USERNAME
    base = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if not base:
        raise RuntimeError("Задайте WEBHOOK_URL или запустите на Render (RENDER_EXTERNAL_URL).")
    me = await bot.get_me()
    BOT_USERNAME = me.username or BOT_USERNAME
    await bot.set_webhook(f"{base.rstrip('/')}{WEBHOOK_PATH}", secret_token=WEBHOOK_SECRET,
                          drop_pending_updates=True, allowed_updates=dp.resolve_used_update_types())
    await bot.set_my_commands([
        BotCommand(command="help", description="Список команд"), BotCommand(command="stats", description="Статистика чата"),
        BotCommand(command="top", description="Топ чата"), BotCommand(command="me", description="Мой профиль"),
        BotCommand(command="daily", description="Ежедневный бонус"), BotCommand(command="settings", description="Настройки чата"),
    ])
    spawn(scheduler())
    logging.info("Запущен как @%s", BOT_USERNAME)


async def on_shutdown(bot: Bot):
    await bot.delete_webhook()


def main():
    init_db()
    dp.include_router(pm_router)
    dp.include_router(group_router)
    dp.include_router(cb_router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
