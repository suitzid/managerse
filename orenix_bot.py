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
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand, BotCommandScopeAllPrivateChats, CallbackQuery, ChatMemberUpdated, ChatPermissions, InlineKeyboardButton,
    InlineKeyboardMarkup, Message, ReplyParameters,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# ═════════════════════════ Конфиг ═════════════════════════
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET") or "change-me"
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
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS banned (user_id INTEGER PRIMARY KEY, reason TEXT, by_id INTEGER, ts INTEGER);
        CREATE TABLE IF NOT EXISTS reqsubs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER UNIQUE, title TEXT, username TEXT, link TEXT,
            added_by INTEGER, ts INTEGER);
        CREATE TABLE IF NOT EXISTS ubuttons (
            id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, url TEXT, style TEXT DEFAULT '',
            newrow INTEGER DEFAULT 1, pos INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS giveaways (
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, channel_id INTEGER, channel_username TEXT,
            channel_title TEXT, message_id INTEGER, text TEXT, winners INTEGER, ends_at INTEGER,
            status TEXT DEFAULT 'active', subs TEXT DEFAULT '[]', photo TEXT, has_photo INTEGER DEFAULT 0,
            last_edit INTEGER DEFAULT 0, last_count INTEGER DEFAULT 0, winner_ids TEXT DEFAULT '[]', created_at INTEGER);
        CREATE TABLE IF NOT EXISTS gparts (
            giveaway_id INTEGER, user_id INTEGER, name TEXT, ts INTEGER, PRIMARY KEY(giveaway_id, user_id));
        """
    )
    # миграция: права админов (старые админы получают статистику и розыгрыши)
    for col, ddl in (("perms", "TEXT DEFAULT 'stats,gv'"), ("added_by", "INTEGER"), ("added_at", "INTEGER")):
        try:
            DB.execute(f"ALTER TABLE gadmins ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass


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

<b>🎁 Розыгрыши</b> <i>(в ЛС с ботом)</i>
/giveaway — создать розыгрыш в своём канале

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
    except TelegramBadRequest as e:
        if "no text in the message" in str(e):  # сообщение-фото (экран /start) — заменяем новым
            try:
                await cb.message.delete()
            except TelegramAPIError:
                pass
            try:
                await cb.message.answer(text, reply_markup=markup)
            except TelegramAPIError:
                pass
    except AttributeError:
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


# ═════════════════════════ Настройки бота, права, доступ ═════════════════════════
HR = "━━━━━━━━━━━━━━━━━━"
DEF_START = ("👋 <b>Привет, {name}! Я Orenix Manager</b>\n\n"
             "🛡 Баны, муты, варны, роли, префиксы и награды\n📊 Статистика, топ и лог чата\n"
             "👋 Приветствия и капча · 🎮 Игры на монеты\n🎁 Розыгрыши в каналах с обязательной подпиской\n\n"
             "Добавь меня в чат и сделай админом 👇")
SDEF = {
    "bot_on": "1",
    "off_text": "⏻ <b>Бот временно отключён</b>\n\nИдут технические работы — загляните чуть позже!",
    "start_text": DEF_START,
    "start_photo": "",
    "sub_text": "📌 <b>Обязательная подписка</b>\n\nЧтобы пользоваться ботом, подпишись на каналы ниже, затем нажми «Проверить».",
    "sub_groups": "0",
    "gv_public": "1",
    "gv_limit": "5",
}


def sget(k):
    r = q1("SELECT value FROM settings WHERE key=?", (k,))
    return r[0] if r and r[0] is not None and r[0] != "" else SDEF.get(k, "")


def sset(k, v):
    ex("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, str(v)))


def sdel(k):
    ex("DELETE FROM settings WHERE key=?", (k,))


PERMS = {
    "stats": ("📊", "Статистика"),
    "gv": ("🎁", "Все розыгрыши"),
    "ban": ("🚫", "Блокировка пользователей"),
    "bc": ("📢", "Рассылка"),
    "subs": ("📌", "Обязательная подписка"),
    "ui": ("🎨", "Оформление и кнопки"),
    "chats": ("💬", "Чаты и каналы"),
    "power": ("⏻", "Вкл/выкл бота"),
    "admins": ("👮", "Управление админами"),
}
PRESETS = {
    "min": ("👀 Наблюдатель", ["stats"]),
    "mod": ("🛡 Модератор", ["stats", "gv", "ban", "chats"]),
    "full": ("⭐ Старший", [p for p in PERMS if p != "admins"]),
}


def perms_of(uid):
    if uid == OWNER_ID:
        return set(PERMS)
    r = q1("SELECT perms FROM gadmins WHERE user_id=?", (uid,))
    return {p for p in (r[0] or "").split(",") if p in PERMS} if r else set()


def has_perm(uid, p):
    return p in perms_of(uid)


def can_create(uid):
    return is_gadmin(uid) or sget("gv_public") == "1"


def can_manage(actor, target):
    """Может ли actor менять/снимать админа target."""
    if target == OWNER_ID or target == actor:
        return False
    return actor == OWNER_ID or ("admins" in perms_of(actor) and "admins" not in perms_of(target))


def can_grant(actor, perm):
    return perm in perms_of(actor) and (perm != "admins" or actor == OWNER_ID)


# ── стили кнопок
STYLES = ["", "primary", "success", "danger"]
STYLE_NAME = {"": "⬜ обычная", "primary": "🔵 синяя", "success": "🟢 зелёная", "danger": "🔴 красная"}


def next_style(s):
    return STYLES[(STYLES.index(s or "") + 1) % len(STYLES)]


BB_DEF = {"add": ("➕ Добавить в чат", "success"), "help": ("📖 Команды", "primary"), "gv": ("🎁 Розыгрыши", "danger")}
BB_NAME = {"add": "Добавить в чат", "help": "Команды", "gv": "Розыгрыши"}


def _bb_raw():
    r = q1("SELECT value FROM settings WHERE key='bb'")
    try:
        return json.loads(r[0]) if r else {}
    except ValueError:
        return {}


def bb_all():
    d, out = _bb_raw(), {}
    for k, (t, s) in BB_DEF.items():
        c = d.get(k, {})
        out[k] = {"t": c.get("t") or t, "s": c.get("s", s), "on": c.get("on", 1)}
    return out


def bb_save(k, reset=False, **kw):
    d = _bb_raw()
    if reset:
        d.pop(k, None)
    else:
        d.setdefault(k, {}).update(kw)
    sset("bb", json.dumps(d, ensure_ascii=False))


def add_url():
    return (f"https://t.me/{BOT_USERNAME}?startgroup=true&admin=delete_messages+restrict_members+"
            f"invite_users+pin_messages+promote_members+manage_chat")


def fill_start(tpl, u):
    vals = {"name": esc(u.first_name or u.full_name), "mention": mention(u.id, u.full_name), "id": str(u.id)}
    return re.sub(r"\{(\w+)\}", lambda mm: vals.get(mm.group(1), mm.group(0)), tpl)


def start_markup(uid):
    bb = bb_all()
    rows = []
    if bb["add"]["on"]:
        rows.append([B(bb["add"]["t"], style=bb["add"]["s"], url=add_url())])
    r2 = []
    if bb["help"]["on"]:
        r2.append(B(bb["help"]["t"], "pm:help", bb["help"]["s"]))
    if bb["gv"]["on"]:
        r2.append(B(bb["gv"]["t"], "gm:menu", bb["gv"]["s"]))
    if r2:
        rows.append(r2)
    cur = None
    for r in q("SELECT * FROM ubuttons ORDER BY pos, id"):
        btn = B(r["text"], url=r["url"], style=r["style"])
        if r["newrow"] or cur is None or len(cur) >= 3:
            cur = [btn]
            rows.append(cur)
        else:
            cur.append(btn)
    if is_gadmin(uid):
        rows.append([B("🛠 Админ-панель", "ap:main", "danger")])
    return kb(rows)


async def send_start(msg, user):
    text = fill_start(sget("start_text"), user)
    markup = start_markup(user.id)
    ph = sget("start_photo")
    if ph and len(text) <= 1024:
        try:
            return await msg.answer_photo(ph, caption=text, reply_markup=markup)
        except TelegramAPIError as e:
            logging.warning("start photo failed: %s", e)
    return await msg.answer(text, reply_markup=markup)


# ═════════════════════════ Фильтр: бан / выключен / обязательная подписка ═════════════════════════
_notice: dict = {}
_sub_ok: dict = {}


def throttled(key, sec):
    t = time.time()
    if t - _notice.get(key, 0) < sec:
        return True
    _notice[key] = t
    return False


async def sub_missing(uid):
    """Список обязательных каналов, на которые пользователь не подписан."""
    if time.time() - _sub_ok.get(uid, 0) < 90:
        return []
    out = []
    for s in q("SELECT * FROM reqsubs ORDER BY id"):
        try:
            cm = await bot.get_chat_member(s["chat_id"], uid)
            if cm.status in ("left", "kicked") or (cm.status == "restricted" and getattr(cm, "is_member", True) is False):
                out.append(dict(s))
        except TelegramAPIError as e:
            logging.warning("required sub check failed for %s: %s", s["chat_id"], e)
    if not out:
        _sub_ok[uid] = time.time()
    return out


def sub_kb(miss):
    rows = [[B(f"📢 {s['title'][:30]}", url=s["link"], style="primary")] for s in miss if s.get("link")]
    rows.append([B("✅ Я подписался — проверить", "sub:chk", "success")])
    return kb(rows)


class Gate(BaseMiddleware):
    """Блокировка пользователей, режим «бот выключен», обязательная подписка. Админы бота проходят всегда."""

    async def __call__(self, handler, event, data):
        u = event.from_user
        if u is None or u.is_bot:
            return await handler(event, data)
        is_cb = isinstance(event, CallbackQuery)
        msg = event.message if is_cb else event
        chat = getattr(msg, "chat", None)
        private = chat is None or chat.type == "private"
        if private:
            touch(None, u)
            ex("UPDATE users SET pm=1 WHERE user_id=?", (u.id,))
        if is_gadmin(u.id):
            return await handler(event, data)
        cdata = (event.data or "") if is_cb else ""
        cmd = (not is_cb and not private) and parse_cmd(event.text or event.caption)[0] is not None

        ban = q1("SELECT reason FROM banned WHERE user_id=?", (u.id,))
        if ban:
            if is_cb:
                await event.answer("🚫 Вы заблокированы в боте", show_alert=True)
            elif private and not throttled(("ban", u.id), 60):
                await event.answer("🚫 <b>Доступ ограничен</b>\nВы заблокированы администрацией бота."
                                   + (f"\n📝 {esc(ban[0])}" if ban[0] else ""))
            if is_cb or private or cmd:
                return
            return await handler(event, data)

        if sget("bot_on") != "1":
            if is_cb:
                return await event.answer("⏻ Бот временно отключён", show_alert=True)
            if private:
                if not throttled(("off", u.id), 30):
                    await event.answer(sget("off_text"))
            elif cmd and not throttled(("offg", chat.id), 60):
                await event.reply(sget("off_text"))
            return

        if cdata.startswith(("sub:", "gv:j:", "cap:")):
            return await handler(event, data)
        need_sub = private or (sget("sub_groups") == "1" and (is_cb or cmd))
        if need_sub and q1("SELECT 1 FROM reqsubs LIMIT 1"):
            miss = await sub_missing(u.id)
            if miss:
                if is_cb and not private:
                    return await event.answer("📌 Для работы с ботом нужна подписка на его каналы. Открой бота в ЛС.",
                                              show_alert=True)
                if is_cb:
                    await event.answer()
                if not throttled(("sub", u.id), 4):
                    try:
                        await bot.send_message(chat.id if not is_cb else u.id, sget("sub_text"), reply_markup=sub_kb(miss))
                    except TelegramAPIError:
                        pass
                return
        return await handler(event, data)


@cb_router.callback_query(F.data == "sub:chk")
async def cb_sub_chk(cb: CallbackQuery):
    _sub_ok.pop(cb.from_user.id, None)
    miss = await sub_missing(cb.from_user.id)
    if miss:
        names = ", ".join(s["title"] for s in miss)
        return await cb.answer(f"❌ Ты ещё не подписан: {names}"[:190], show_alert=True)
    await cb.answer("✅ Спасибо за подписку!")
    try:
        await cb.message.delete()
    except (TelegramAPIError, AttributeError):
        pass
    if cb.message.chat.type == "private":
        await send_start(cb.message, cb.from_user)


# ═════════════════════════ Состояния и общие хелперы панели ═════════════════════════
class GW(StatesGroup):
    channel = State()
    text = State()
    winners = State()
    ends = State()
    subs = State()
    photo = State()


class AP(StatesGroup):
    bc = State()
    ban = State()
    unban = State()
    find = State()
    sub_add = State()
    sub_link = State()
    edit = State()
    photo = State()
    btn_new = State()
    btn_edit = State()
    chat_add = State()
    add_admin = State()


PER = 8


def paged(items, page):
    pages = max(1, -(-len(items) // PER))
    page = max(0, min(page, pages - 1))
    return items[page * PER:(page + 1) * PER], page, pages


def nav_row(prefix, page, pages):
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(B("◀️", f"{prefix}{page - 1}"))
    row.append(B(f"{page + 1}/{pages}", "ap:noop"))
    if page < pages - 1:
        row.append(B("▶️", f"{prefix}{page + 1}"))
    return [row]


def cancel_kb(back=None):
    return kb([[B("✖️ Отмена", f"ap:cx:{back}" if back else "gm:cancel", "danger")]])


def bar(done, total, n=10):
    k = round(done / total * n) if total else n
    return "▰" * k + "▱" * (n - k) + f" {round(done / total * 100) if total else 100}%"


async def ask(state, st, back, text, extra=None, **data):
    """Переводит в режим ввода: возвращает (текст, клавиатура) для показа."""
    await state.clear()
    await state.set_state(st)
    await state.update_data(back=back, **data)
    return text, kb((extra or []) + [[B("✖️ Отмена", f"ap:cx:{back}", "danger")]])


async def parse_user_input(m):
    """→ (uid, name, остаток). Пересланное сообщение, ID или @username из базы."""
    fo = m.forward_origin
    if fo is not None:
        if getattr(fo, "type", "") == "user":
            return fo.sender_user.id, fo.sender_user.full_name, ""
        return None, None, "hidden"
    txt = (m.text or "").strip()
    tok, _, rest = txt.partition(" ")
    if re.fullmatch(r"\d{3,}", tok):
        return int(tok), uname(int(tok)), rest.strip()
    if tok.startswith("@") or tok.startswith("https://t.me/"):
        name = tok.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
        r = q1("SELECT user_id,name FROM users WHERE lower(username)=lower(?)", (name,))
        if r:
            return r[0], r[1], rest.strip()
    return None, None, txt


async def target_chat(m):
    """Чат/канал из пересланного поста, @username, t.me-ссылки или ID → (chat, участник-бот)."""
    key = None
    fo = m.forward_origin
    if fo is not None:
        c = getattr(fo, "chat", None) or getattr(fo, "sender_chat", None)
        if c:
            key = c.id
    if key is None:
        s = (m.text or "").strip().split(None, 1)[0] if (m.text or "").strip() else ""
        if not s:
            raise ValueError("Отправь @username, ID или перешли пост из канала.")
        if "t.me/+" in s or "joinchat" in s:
            raise ValueError("По invite-ссылке канал не определить — перешли сюда любой пост из него или отправь ID.")
        if s.startswith(("https://t.me/", "t.me/")):
            s = "@" + s.rstrip("/").rsplit("/", 1)[1]
        key = int(s) if re.fullmatch(r"-?\d+", s) else "@" + s.lstrip("@")
    chat = await bot.get_chat(key)
    me = await bot.get_chat_member(chat.id, bot.id)
    return chat, me


def is_admin_member(me):
    return me.status in ("administrator", "creator")


def norm_url(s):
    s = s.strip()
    if s.startswith("@") and len(s) > 1:
        return "https://t.me/" + s[1:]
    if s.startswith(("t.me/", "telegram.me/")):
        return "https://" + s
    return s if re.match(r"^(https?://|tg://)\S+$", s) else None


def block_user(uid, reason, by):
    ex("INSERT OR REPLACE INTO banned VALUES(?,?,?,?)", (uid, reason or "", by, now()))


def unblock_user(uid):
    ex("DELETE FROM banned WHERE user_id=?", (uid,))


# ═════════════════════════ ЛС: старт, меню розыгрышей (для всех) ═════════════════════════
@pm_router.message(CommandStart())
async def pm_start(m: Message, state: FSMContext):
    await state.clear()
    touch(None, m.from_user)
    ex("UPDATE users SET pm=1 WHERE user_id=?", (m.from_user.id,))
    await send_start(m, m.from_user)


@pm_router.message(Command("help"))
async def pm_help(m: Message):
    await m.answer(HELP)


@pm_router.message(Command("cancel"))
async def pm_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("✖️ Отменено.")


@pm_router.message(Command("admin", "panel"))
async def pm_admin(m: Message, state: FSMContext):
    await state.clear()
    if not is_gadmin(m.from_user.id):
        return await m.answer("⛔ Доступ только для админов бота.")
    await m.answer(*ap_main(m.from_user.id))


@pm_router.message(Command("giveaway"))
async def pm_giveaway(m: Message, state: FSMContext):
    if not can_create(m.from_user.id):
        return await m.answer("⛔ Создание розыгрышей сейчас отключено администрацией.")
    await gw_begin(m, state)


@cb_router.callback_query(F.data.startswith("pm:"))
async def cb_pm(cb: CallbackQuery):
    await cb.answer()
    if cb.data == "pm:help":
        await cb.message.answer(HELP)
    elif cb.data == "pm:home":
        await send_start(cb.message, cb.from_user)
        try:
            await cb.message.delete()
        except TelegramAPIError:
            pass


def gm_menu(uid):
    mine = q1("SELECT COUNT(*), COALESCE(SUM(status='active'),0) FROM giveaways WHERE owner_id=?", (uid,))
    lim = int(sget("gv_limit"))
    text = (f"🎁 <b>Розыгрыши</b>\n{HR}\n"
            "Создай розыгрыш в своём канале: я опубликую пост с кнопкой «Участвовать», проверю подписки "
            "и честно выберу победителей.\n\n"
            f"📋 Твои розыгрыши: <b>{mine[0]}</b> (активных: <b>{mine[1]}</b>"
            + ("" if is_gadmin(uid) else f" из {lim}") + ")")
    rows = []
    if can_create(uid):
        rows.append([B("➕ Создать розыгрыш", "gm:new", "success")])
    else:
        text += "\n\n⛔ Создание новых розыгрышей временно отключено."
    rows.append([B("📋 Мои розыгрыши", "gm:l:mine:0", "primary")])
    rows.append([B("⬅️ На главную", "pm:home")])
    return text, kb(rows)


def gl_screen(uid, scope, page=0):
    if scope == "all":
        rows_db = q("SELECT * FROM giveaways ORDER BY id DESC")
    else:
        rows_db = q("SELECT * FROM giveaways WHERE owner_id=? ORDER BY id DESC", (uid,))
    chunk, page, pages = paged(rows_db, page)
    icon = {"active": "🟢", "ended": "🏁", "cancelled": "⛔"}
    rows = [[B(f"{icon.get(r['status'], '•')} #{r['id']} · {(r['channel_title'] or '')[:18]} · {gcount(r['id'])} уч.",
               f"gm:g:{r['id']}:{scope}", "success" if r["status"] == "active" else None)] for r in chunk]
    rows += nav_row(f"gm:l:{scope}:", page, pages)
    if can_create(uid):
        rows.append([B("➕ Создать розыгрыш", "gm:new", "success")])
    rows.append([B("⬅️ Назад", "ap:main" if scope == "all" else "gm:menu")])
    title = "🎁 <b>Все розыгрыши бота</b>" if scope == "all" else "📋 <b>Мои розыгрыши</b>"
    return f"{title}\n{HR}" + ("" if rows_db else "\n\nПока пусто."), kb(rows)


def g_screen(gid, scope):
    g = gget(gid)
    st = {"active": "🟢 идёт", "ended": "🏁 завершён", "cancelled": "⛔ отменён"}[g["status"]]
    text = (f"🎁 <b>Розыгрыш #{gid}</b>\n{HR}\n📣 Канал: <b>{esc(g['channel_title'])}</b>\nСтатус: {st}\n"
            f"🏆 Победителей: {g['winners']}\n⏰ Итоги: {fmt_ts(g['ends_at'])}\n👥 Участников: <b>{gcount(gid)}</b>")
    if scope == "all":
        text += f"\n👤 Создатель: {mention(g['owner_id'], uname(g['owner_id']))}"
    rows = []
    if g["message_id"]:
        rows.append([B("🔗 Открыть пост", url=post_link(g))])
    if g["status"] == "active":
        rows.append([B("🏁 Завершить сейчас", f"gm:end:{gid}:{scope}", "success"), B("⛔ Отменить", f"gm:can:{gid}:{scope}", "danger")])
    elif g["status"] == "ended":
        rows.append([B("🔄 Переиграть", f"gm:rr:{gid}:{scope}", "primary")])
    rows.append([B("⬅️ Назад", f"gm:l:{scope}:0")])
    return text, kb(rows)


def owner_of(uid, gid):
    g = gget(gid)
    return g if g and (uid == OWNER_ID or g["owner_id"] == uid or has_perm(uid, "gv")) else None


@cb_router.callback_query(F.data.startswith("gm:"))
async def cb_gm(cb: CallbackQuery, state: FSMContext):
    p = cb.data.split(":")
    act, uid = p[1], cb.from_user.id
    if act == "new" and not can_create(uid):
        return await cb.answer("⛔ Создание розыгрышей отключено", show_alert=True)
    if (act == "l" and p[2] == "all") and not has_perm(uid, "gv"):
        return await cb.answer("⛔ Нет доступа", show_alert=True)
    await cb.answer()
    if act in ("menu", "cancel"):
        await state.clear()
        await edit(cb, *gm_menu(uid))
    elif act == "new":
        await gw_begin(cb, state)
    elif act == "l":
        await edit(cb, *gl_screen(uid, p[2], int(p[3])))
    elif act == "g":
        if owner_of(uid, int(p[2])):
            await edit(cb, *g_screen(int(p[2]), p[3]))
    elif act in ("end", "can", "rr"):
        gid, scope = int(p[2]), p[3]
        g = owner_of(uid, gid)
        if not g:
            return
        if act == "end":
            await finish_giveaway(gid)
        elif act == "rr":
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
        await edit(cb, *g_screen(gid, scope))


# ═════════════════════════ Мастер создания розыгрыша ═════════════════════════
async def gw_begin(target, state):
    await state.clear()
    await state.set_state(GW.channel)
    msg = target.message if isinstance(target, CallbackQuery) else target
    await msg.answer("🎁 <b>Новый розыгрыш — шаг 1/6</b>\n\nОтправь @username или ID канала, где опубликовать пост.\n"
                     "Бот должен быть <b>админом</b> этого канала с правом публикации, и ты тоже.", reply_markup=cancel_kb())


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
    if not is_gadmin(m.from_user.id):
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
    markup = kb([[B("🚀 Опубликовать", "gw:ok", "success"), B("✖️ Отмена", "gm:cancel", "danger")]])
    await m.answer("👀 <b>Предпросмотр поста:</b>")
    if photo and len(text) <= 1000:
        await m.answer_photo(photo, caption=text)
    else:
        await m.answer(text)
    await m.answer(f"Канал: <b>{esc(d['channel_title'])}</b>. Публикуем?", reply_markup=markup)


@cb_router.callback_query(F.data == "gw:ok")
async def gw_confirm(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    uid = cb.from_user.id
    if not d.get("channel_id") or not can_create(uid):
        return await cb.answer("Сессия устарела, начните заново: /giveaway", show_alert=True)
    if not is_gadmin(uid):
        lim = int(sget("gv_limit"))
        if q1("SELECT COUNT(*) FROM giveaways WHERE owner_id=? AND status='active'", (uid,))[0] >= lim:
            return await cb.answer(f"⛔ Лимит: не больше {lim} активных розыгрышей одновременно.", show_alert=True)
    cur = ex("INSERT INTO giveaways(owner_id,channel_id,channel_username,channel_title,text,winners,ends_at,subs,photo,created_at) "
             "VALUES(?,?,?,?,?,?,?,?,?,?)",
             (uid, d["channel_id"], d["channel_username"], d["channel_title"], d["text"], d["winners"],
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
                               f"Итоги: {fmt_ts(d['ends_at'])}",
                               reply_markup=kb([[B("📋 Мои розыгрыши", "gm:l:mine:0", "primary")]]))


# ═════════════════════════ АДМИН-ПАНЕЛЬ ═════════════════════════
SEC_PERM = {"st": "stats", "us": "ban", "bc": "bc", "sb": "subs", "ui": "ui", "bt": "ui", "pf": "ui",
            "ch": "chats", "pw": "power", "ad": "admins"}


def ap_main(uid):
    one = lambda s, p=(): q1(s, p)[0]  # noqa: E731
    on = sget("bot_on") == "1"
    P = perms_of(uid)
    text = (f"🛠 <b>ORENIX · Панель управления</b>\n{HR}\n"
            f"{'🟢' if on else '🔴'} Бот: <b>{'работает' if on else 'выключен'}</b>\n"
            f"👤 Пользователей: <b>{one('SELECT COUNT(*) FROM users')}</b> · 🚫 в бане: <b>{one('SELECT COUNT(*) FROM banned')}</b>\n"
            f"👥 Групп: <b>{one('SELECT COUNT(*) FROM chats WHERE active=1 AND type IN (?,?)', ('group', 'supergroup'))}</b> · "
            f"📣 Каналов: <b>{one('SELECT COUNT(*) FROM chats WHERE active=1 AND type=?', ('channel',))}</b>\n"
            f"🎁 Активных розыгрышей: <b>{one('SELECT COUNT(*) FROM giveaways WHERE status=?', ('active',))}</b>\n"
            f"📌 Обязательных подписок: <b>{one('SELECT COUNT(*) FROM reqsubs')}</b>\n{HR}\n"
            f"Твой доступ: <b>{'👑 Владелец' if uid == OWNER_ID else '👮 Админ · ' + str(len(P)) + ' из ' + str(len(PERMS)) + ' разделов'}</b>")
    items = [("stats", "📊 Статистика", "ap:st", "primary"), ("gv", "🎁 Розыгрыши", "gm:l:all:0", "success"),
             ("ban", "🚫 Пользователи", "ap:us", "danger"), ("bc", "📢 Рассылка", "ap:bc", "primary"),
             ("subs", "📌 Подписка", "ap:sb", "success"), ("ui", "🎨 Оформление", "ap:ui", "primary"),
             ("chats", "💬 Чаты и каналы", "ap:ch", "primary"), ("admins", "👮 Админы", "ap:ad", "danger")]
    btns = [B(t, c, s) for p, t, c, s in items if p in P]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    if "power" in P:
        rows.insert(0, [B("🔴 Выключить бота" if on else "🟢 Включить бота", "ap:pw", "danger" if on else "success")])
    rows.append([B("🎁 Мои розыгрыши", "gm:menu"), B("🏠 На главную", "ap:home")])
    return text, kb(rows)


# ── питание
async def sec_pw(uid, p, state, cb):
    act = p[2] if len(p) > 2 else ""
    if act == "txt":
        return await ask_edit(state, "off_text", "pw")
    if act == "t":
        sset("bot_on", "0" if sget("bot_on") == "1" else "1")
        logging.warning("bot_on=%s by %s", sget("bot_on"), uid)
    on = sget("bot_on") == "1"
    text = (f"⏻ <b>Питание бота</b>\n{HR}\nСтатус: {'🟢 <b>работает</b>' if on else '🔴 <b>выключен</b>'}\n\n"
            "Пока бот выключен, он молчит для всех, <b>кроме админов бота</b>. Уже идущие розыгрыши завершатся по расписанию.\n\n"
            f"💬 <b>Что видят пользователи:</b>\n<blockquote>{sget('off_text')}</blockquote>")
    return text, kb([[B("🔴 Выключить" if on else "🟢 Включить", "ap:pw:t", "danger" if on else "success")],
                     [B("✏️ Изменить сообщение", "ap:pw:txt", "primary")], [B("⬅️ Назад", "ap:main")]])


# ── универсальное редактирование текстовых настроек
EDITABLE = {
    "start_text": ("📝 <b>Текст /start</b>", "Форматирование поддерживается. Переменные: <code>{name}</code> <code>{mention}</code> <code>{id}</code>.", 1000, "ui", "ui"),
    "off_text": ("💬 <b>Сообщение при выключении</b>", "Его увидят все, кроме админов, пока бот выключен.", 1000, "power", "pw"),
    "sub_text": ("📌 <b>Текст обязательной подписки</b>", "Показывается над кнопками каналов.", 1000, "subs", "sb"),
    "bot_name": ("🏷 <b>Имя бота</b>", "До 64 символов. Меняется в профиле Telegram.", 64, "ui", "pf"),
    "bot_desc": ("📄 <b>Описание бота</b>", "Текст над кнопкой «Начать» в пустом чате. До 512 символов.", 512, "ui", "pf"),
    "bot_short": ("✍️ <b>Короткое описание</b>", "Показывается в профиле бота. До 120 символов.", 120, "ui", "pf"),
}


async def cur_value(key):
    try:
        if key == "bot_name":
            return (await bot.get_my_name()).name
        if key == "bot_desc":
            return (await bot.get_my_description()).description
        if key == "bot_short":
            return (await bot.get_my_short_description()).short_description
    except TelegramAPIError:
        return "—"
    return sget(key)


async def ask_edit(state, key, back):
    title, hint, lim, perm, _ = EDITABLE[key]
    extra = [[B("↩️ По умолчанию", f"ap:rs:{key}", "primary")]] if key in SDEF else None
    cur = await cur_value(key)
    return await ask(state, AP.edit, back, f"{title}\n{HR}\n{hint}\n\n<b>Сейчас:</b>\n<pre>{esc(cur)[:900]}</pre>\n"
                     "✏️ Отправь новое значение сообщением.", extra, ekey=key)


async def sec_rs(uid, p, state, cb):
    key = p[2]
    if key in EDITABLE and key in SDEF and has_perm(uid, EDITABLE[key][3]):
        sdel(key)
        await state.clear()
        return await SECTIONS[EDITABLE[key][4]](uid, ["ap", EDITABLE[key][4]], state, cb)


@pm_router.message(AP.edit)
async def ap_edit_msg(m: Message, state: FSMContext):
    d = await state.get_data()
    key = d.get("ekey")
    if key not in EDITABLE or not has_perm(m.from_user.id, EDITABLE[key][3]):
        await state.clear()
        return await m.answer("⛔ Нет доступа.")
    if not m.text:
        return await m.answer("❌ Нужен текст.")
    lim, back = EDITABLE[key][2], EDITABLE[key][4]
    if len(m.text) > lim:
        return await m.answer(f"❌ Слишком длинно: {len(m.text)} из {lim}.")
    try:
        if key == "bot_name":
            await bot.set_my_name(name=m.text)
        elif key == "bot_desc":
            await bot.set_my_description(description=m.text)
        elif key == "bot_short":
            await bot.set_my_short_description(short_description=m.text)
        else:
            sset(key, m.html_text)
    except TelegramAPIError as e:
        return await m.answer(f"❌ Telegram отклонил: <code>{esc(e)}</code>")
    await state.clear()
    await m.answer("✅ <b>Сохранено</b>")
    await m.answer(*(await SECTIONS[back](m.from_user.id, ["ap", back], state, None)))


# ── статистика
async def sec_st(uid, p, state, cb):
    one = lambda s, pr=(): q1(s, pr)[0]  # noqa: E731
    st = day_start_ts(0)
    days = []
    for i in range(6, -1, -1):
        lo, hi = day_start_ts(i), day_start_ts(i - 1)
        days.append((datetime.fromtimestamp(lo, TZ).strftime("%d.%m"),
                     one("SELECT COUNT(*) FROM users WHERE first_seen>=? AND first_seen<?", (lo, hi))))
    mx = max([n for _, n in days] + [1])
    chart = "\n".join(f"{d} {'▇' * round(n / mx * 10) or '·'} {n}" for d, n in days)
    text = (
        f"📊 <b>Статистика бота</b>\n{HR}\n"
        f"👤 Пользователей: <b>{one('SELECT COUNT(*) FROM users')}</b> (в ЛС: {one('SELECT COUNT(*) FROM users WHERE pm=1')})\n"
        f"🆕 Новых сегодня: <b>{one('SELECT COUNT(*) FROM users WHERE first_seen>=?', (st,))}</b>\n"
        f"🚫 Заблокировано: <b>{one('SELECT COUNT(*) FROM banned')}</b>\n\n"
        f"<b>Новые пользователи за 7 дней</b>\n<pre>{chart}</pre>\n"
        f"👥 Групп: <b>{one('SELECT COUNT(*) FROM chats WHERE active=1 AND type IN (?,?)', ('group', 'supergroup'))}</b> · "
        f"📣 Каналов: <b>{one('SELECT COUNT(*) FROM chats WHERE active=1 AND type=?', ('channel',))}</b>\n"
        f"✉️ Сообщений: <b>{one('SELECT COALESCE(SUM(cnt),0) FROM msg_stats')}</b> "
        f"(сегодня {one('SELECT COALESCE(SUM(cnt),0) FROM msg_stats WHERE day=?', (today(),))})\n"
        f"🔨 Банов сегодня: <b>{one('SELECT COUNT(*) FROM chat_log WHERE action=? AND ts>=?', ('ban', st))}</b> · "
        f"🔇 мутов: <b>{one('SELECT COUNT(*) FROM chat_log WHERE action=? AND ts>=?', ('mute', st))}</b> · "
        f"⚠️ варнов: <b>{one('SELECT COUNT(*) FROM chat_log WHERE action=? AND ts>=?', ('warn', st))}</b>\n\n"
        f"🎁 Розыгрышей: активных <b>{one('SELECT COUNT(*) FROM giveaways WHERE status=?', ('active',))}</b>, "
        f"завершённых <b>{one('SELECT COUNT(*) FROM giveaways WHERE status=?', ('ended',))}</b>\n"
        f"🙋 Участий всего: <b>{one('SELECT COUNT(*) FROM gparts')}</b>")
    return text, kb([[B("🔄 Обновить", "ap:st", "primary")], [B("⬅️ Назад", "ap:main")]])


# ── пользователи: блок / разбан
def user_card(uid, note=""):
    r = q1("SELECT * FROM users WHERE user_id=?", (uid,))
    b = q1("SELECT * FROM banned WHERE user_id=?", (uid,))
    name = r["name"] if r else "неизвестен"
    text = (f"{note}👤 <b>{esc(name)}</b>\n🆔 <code>{uid}</code>\n"
            + (f"🔗 @{esc(r['username'])}\n" if r and r["username"] else "")
            + (f"🪙 Монет: {r['coins']}\n🕐 Первый визит: {fmt_ts(r['first_seen'])}\n👀 Был: {fmt_ts(r['last_seen'])}\n" if r and r["first_seen"] else "")
            + f"{HR}\n" + (f"Статус: 🚫 <b>заблокирован</b>\n📝 {esc(b['reason']) or '—'}\n🕐 {fmt_ts(b['ts'])}" if b else "Статус: ✅ <b>активен</b>"))
    btn = B("♻️ Разбанить", f"ap:us:u:{uid}", "success") if b else B("🚫 Заблокировать", f"ap:us:b:{uid}", "danger")
    return text, kb([[btn], [B("⬅️ Назад", "ap:us")]])


async def sec_us(uid, p, state, cb):
    act = p[2] if len(p) > 2 else ""
    if act == "ban":
        return await ask(state, AP.ban, "us", "🚫 <b>Блокировка пользователя</b>\n\nОтправь <b>ID</b>, <b>@username</b> (если он уже писал боту) "
                         "или <b>перешли сообщение</b> пользователя. Причину можно дописать после ID.\n\n<i>Пример:</i> <code>123456789 спам</code>")
    if act == "unid":
        return await ask(state, AP.unban, "us", "♻️ <b>Разбан по ID</b>\n\nОтправь ID, @username или перешли сообщение пользователя.")
    if act == "find":
        return await ask(state, AP.find, "us", "🔎 <b>Поиск пользователя</b>\n\nОтправь ID, @username или перешли сообщение.")
    if act == "i":
        return user_card(int(p[3]))
    if act == "b":
        t = int(p[3])
        if t == OWNER_ID or is_gadmin(t):
            return user_card(t, "⛔ Админов бота блокировать нельзя.\n\n")
        block_user(t, "", uid)
        return user_card(t, "✅ Заблокирован.\n\n")
    if act == "u":
        unblock_user(int(p[3]))
        return user_card(int(p[3]), "✅ Разблокирован.\n\n")
    if act == "unb":
        rows_db = q("SELECT b.user_id, b.reason, COALESCE(u.name,'') n FROM banned b LEFT JOIN users u ON u.user_id=b.user_id ORDER BY b.ts DESC")
        chunk, page, pages = paged(rows_db, int(p[3]) if len(p) > 3 else 0)
        rows = [[B(f"♻️ {(r['n'] or 'без имени')[:20]} · {r['user_id']}", f"ap:us:i:{r['user_id']}", "success")] for r in chunk]
        rows += nav_row("ap:us:unb:", page, pages)
        rows.append([B("⌨️ Разбан по ID", "ap:us:unid", "primary")])
        rows.append([B("⬅️ Назад", "ap:us")])
        return f"♻️ <b>Заблокированные</b> ({len(rows_db)})\n{HR}\nНажми на пользователя, чтобы открыть карточку и разбанить." + ("" if rows_db else "\n\nСписок пуст."), kb(rows)
    n = q1("SELECT COUNT(*) FROM banned")[0]
    return (f"🚫 <b>Пользователи</b>\n{HR}\n👤 Всего: <b>{q1('SELECT COUNT(*) FROM users')[0]}</b>\n🚫 Заблокировано: <b>{n}</b>\n\n"
            "Заблокированный пользователь не может пользоваться ботом (ЛС, кнопки, розыгрыши)."), kb([
        [B("🚫 Заблокировать", "ap:us:ban", "danger"), B(f"♻️ Разбан ({n})", "ap:us:unb:0", "success")],
        [B("🔎 Найти пользователя", "ap:us:find", "primary")], [B("⬅️ Назад", "ap:main")]])


async def _user_input(m, state, perm, mode):
    if not has_perm(m.from_user.id, perm):
        await state.clear()
        return await m.answer("⛔ Нет доступа.")
    uid, name, rest = await parse_user_input(m)
    if not uid:
        return await m.answer("❌ Не нашёл пользователя. Отправь числовой ID, @username (он должен был писать боту) "
                              "или перешли его сообщение (если профиль не скрыт).", reply_markup=cancel_kb("us"))
    await state.clear()
    if mode == "ban":
        if uid == OWNER_ID or is_gadmin(uid):
            return await m.answer("⛔ Админов бота блокировать нельзя.", reply_markup=kb([[B("⬅️ Назад", "ap:us")]]))
        block_user(uid, rest, m.from_user.id)
        try:
            await bot.send_message(uid, "🚫 <b>Вы заблокированы в боте.</b>" + (f"\n📝 {esc(rest)}" if rest else ""))
        except TelegramAPIError:
            pass
        return await m.answer(*user_card(uid, "✅ Заблокирован.\n\n"))
    if mode == "unban":
        unblock_user(uid)
        return await m.answer(*user_card(uid, "✅ Разблокирован.\n\n"))
    await m.answer(*user_card(uid))


@pm_router.message(AP.ban)
async def ap_ban(m: Message, state: FSMContext):
    await _user_input(m, state, "ban", "ban")


@pm_router.message(AP.unban)
async def ap_unban(m: Message, state: FSMContext):
    await _user_input(m, state, "ban", "unban")


@pm_router.message(AP.find)
async def ap_find(m: Message, state: FSMContext):
    await _user_input(m, state, "ban", "find")


# ── рассылка
BC = {"on": False, "stop": False}


def bc_targets(kind):
    out = []
    if kind in ("users", "all"):
        out += [(r[0], "u") for r in q("SELECT user_id FROM users WHERE pm=1 AND user_id NOT IN (SELECT user_id FROM banned)")]
    if kind in ("groups", "all"):
        out += [(r[0], "c") for r in q("SELECT chat_id FROM chats WHERE active=1 AND type IN ('group','supergroup')")]
    if kind in ("channels", "all"):
        out += [(r[0], "c") for r in q("SELECT chat_id FROM chats WHERE active=1 AND type='channel'")]
    return out


KIND_NAME = {"users": "👤 Пользователям", "groups": "👥 Группам", "channels": "📣 Каналам", "all": "🌐 Всем"}


async def send_copy(tid, src, mid):
    err = None
    for _ in range(3):
        try:
            return await bot.copy_message(tid, src, mid)
        except TelegramRetryAfter as e:
            err = e
            await asyncio.sleep(e.retry_after + 1)
    raise err


async def run_broadcast(msg_chat, msg_id, src, mid, targets):
    BC.update(on=True, stop=False)
    ok = blocked = failed = 0
    last, total = 0, len(targets)
    stop_kb = kb([[B("⏹ Остановить", "ap:bc:stop", "danger")]])
    for i, (tid, kind) in enumerate(targets, 1):
        if BC["stop"]:
            break
        try:
            await send_copy(tid, src, mid)
            ok += 1
        except TelegramForbiddenError:
            blocked += 1
            ex("UPDATE users SET pm=0 WHERE user_id=?", (tid,)) if kind == "u" else ex("UPDATE chats SET active=0 WHERE chat_id=?", (tid,))
        except TelegramAPIError as e:
            failed += 1
            if kind == "c" and "chat not found" in str(e).lower():
                ex("UPDATE chats SET active=0 WHERE chat_id=?", (tid,))
        except Exception:  # noqa: BLE001
            failed += 1
        if time.time() - last > 3:
            last = time.time()
            try:
                await bot.edit_message_text(f"📢 <b>Рассылка идёт…</b>\n{bar(i, total)}\n✅ {ok} · 🚫 {blocked} · ❌ {failed}",
                                            chat_id=msg_chat, message_id=msg_id, reply_markup=stop_kb)
            except TelegramAPIError:
                pass
        await asyncio.sleep(0.05)
    stopped = BC["stop"]
    BC.update(on=False, stop=False)
    try:
        await bot.edit_message_text(
            f"{'⏹ <b>Рассылка остановлена</b>' if stopped else '✅ <b>Рассылка завершена</b>'}\n{HR}\n"
            f"📬 Доставлено: <b>{ok}</b> из {total}\n🚫 Заблокировали бота / удалили: <b>{blocked}</b>\n❌ Ошибки: <b>{failed}</b>",
            chat_id=msg_chat, message_id=msg_id, reply_markup=kb([[B("⬅️ В панель", "ap:main")]]))
    except TelegramAPIError:
        pass


async def sec_bc(uid, p, state, cb):
    act = p[2] if len(p) > 2 else ""
    if act == "stop":
        BC["stop"] = True
        return None
    if BC["on"]:
        return "📢 <b>Рассылка уже идёт</b>\nДождись завершения или останови её.", kb([[B("⏹ Остановить", "ap:bc:stop", "danger")], [B("⬅️ Назад", "ap:main")]])
    d = await state.get_data()
    if act in ("pick", "go", "ok") and not d.get("bc_mid"):
        act = ""
    if act == "pick":
        c = {k: len(bc_targets(k)) for k in KIND_NAME}
        return (f"📢 <b>Кому отправить?</b>\n{HR}\nСообщение принято (предпросмотр выше).\n\n"
                f"👤 Пользователей в ЛС: <b>{c['users']}</b>\n👥 Групп: <b>{c['groups']}</b>\n📣 Каналов: <b>{c['channels']}</b>"), kb([
            [B(f"👤 Пользователям ({c['users']})", "ap:bc:go:users", "primary"), B(f"👥 Группам ({c['groups']})", "ap:bc:go:groups", "primary")],
            [B(f"📣 Каналам ({c['channels']})", "ap:bc:go:channels", "primary")],
            [B(f"🌐 Всем сразу ({c['all']})", "ap:bc:go:all", "success")],
            [B("✖️ Отмена", "ap:cx:main", "danger")]])
    if act == "go":
        kind = p[3]
        n = len(bc_targets(kind))
        return (f"⚠️ <b>Подтверди рассылку</b>\n{HR}\nПолучатели: <b>{KIND_NAME[kind]}</b>\nАдресов: <b>{n}</b>\n\n"
                "Отправить сообщение сейчас?"), kb([[B("🚀 Отправить", f"ap:bc:ok:{kind}", "success"), B("⬅️ Назад", "ap:bc:pick")]])
    if act == "ok":
        targets = bc_targets(p[3])
        if not targets:
            return "📭 Нет адресатов для этой категории.", kb([[B("⬅️ Назад", "ap:bc:pick")]])
        spawn(run_broadcast(cb.message.chat.id, cb.message.message_id, d["bc_src"], d["bc_mid"], targets))
        await state.clear()
        return f"📢 <b>Рассылка запущена</b>\n{bar(0, len(targets))}", kb([[B("⏹ Остановить", "ap:bc:stop", "danger")]])
    return await ask(state, AP.bc, "main", "📢 <b>Рассылка</b>\n\nОтправь сообщение, которое нужно разослать. Подойдёт <b>любое</b>: "
                     "текст с форматированием, фото, видео, документ — я скопирую его как есть.\n\n"
                     "Можно отправлять пользователям, группам и каналам, где я состою.")


@pm_router.message(AP.bc)
async def ap_bc_msg(m: Message, state: FSMContext):
    if not has_perm(m.from_user.id, "bc"):
        await state.clear()
        return await m.answer("⛔ Нет доступа.")
    await state.set_state(None)
    await state.update_data(bc_src=m.chat.id, bc_mid=m.message_id)
    await m.answer(*(await sec_bc(m.from_user.id, ["ap", "bc", "pick"], state, None)))


# ── обязательная подписка
async def sec_sb(uid, p, state, cb):
    act = p[2] if len(p) > 2 else ""
    if act == "add":
        return await ask(state, AP.sub_add, "sb",
                         "📌 <b>Добавить канал в обязательную подписку</b>\n\nОтправь <b>@username</b>, <b>ID</b> или <b>перешли любой пост</b> из канала "
                         "(так добавляются и <b>приватные</b> каналы).\n\n⚠️ Бот должен быть <b>админом</b> этого канала — иначе он не сможет проверять подписку.\n"
                         "Для приватного канала бот создаст invite-ссылку сам (нужно право «Приглашать»).")
    if act == "txt":
        return await ask_edit(state, "sub_text", "sb")
    if act == "grp":
        sset("sub_groups", "0" if sget("sub_groups") == "1" else "1")
    if act == "d":
        ex("DELETE FROM reqsubs WHERE id=?", (int(p[3]),))
        _sub_ok.clear()
    subs = q("SELECT * FROM reqsubs ORDER BY id")
    lines = []
    for s in subs:
        try:
            me = await bot.get_chat_member(s["chat_id"], bot.id)
            okb = "✅" if is_admin_member(me) else "⚠️ бот не админ"
        except TelegramAPIError:
            okb = "⚠️ нет доступа"
        kind = f"@{s['username']}" if s["username"] else "🔒 приватный"
        lines.append(f"• <b>{esc(s['title'])}</b> · {esc(kind)} · {okb}")
    grp = sget("sub_groups") == "1"
    text = (f"📌 <b>Обязательная подписка</b>\n{HR}\nПользователь не сможет пользоваться ботом, пока не подпишется на <b>все</b> каналы ниже. "
            f"Админы бота освобождены.\n\n" + ("\n".join(lines) if lines else "<i>Каналов нет — подписка не требуется.</i>")
            + f"\n\n👥 Проверять и в группах (команды): <b>{'ВКЛ' if grp else 'ВЫКЛ'}</b>")
    rows = [[B(f"🗑 {s['title'][:28]}", f"ap:sb:d:{s['id']}", "danger")] for s in subs]
    rows.append([B("➕ Добавить канал", "ap:sb:add", "success")])
    rows.append([B(f"👥 В группах: {'ВКЛ ✅' if grp else 'ВЫКЛ ❌'}", "ap:sb:grp", "success" if grp else "danger"), B("✏️ Текст", "ap:sb:txt", "primary")])
    rows.append([B("⬅️ Назад", "ap:main")])
    return text, kb(rows)


def save_sub(chat, link, by):
    ex("INSERT OR REPLACE INTO reqsubs(chat_id,title,username,link,added_by,ts) VALUES(?,?,?,?,?,?)",
       (chat["id"], chat["title"], chat["username"], link, by, now()))
    _sub_ok.clear()


@pm_router.message(AP.sub_add)
async def ap_sub_add(m: Message, state: FSMContext):
    if not has_perm(m.from_user.id, "subs"):
        await state.clear()
        return await m.answer("⛔ Нет доступа.")
    try:
        chat, me = await target_chat(m)
    except (TelegramAPIError, ValueError, IndexError) as e:
        return await m.answer(f"❌ {esc(e)}", reply_markup=cancel_kb("sb"))
    if not is_admin_member(me):
        return await m.answer("❌ Бот не админ в этом канале — он не сможет проверять подписку. Сделай его админом и отправь снова.",
                              reply_markup=cancel_kb("sb"))
    info = {"id": chat.id, "title": chat.title or str(chat.id), "username": chat.username}
    link = await chat_link(chat)
    if not link:
        await state.set_state(AP.sub_link)
        await state.update_data(sc=info)
        return await m.answer("🔗 Не смог создать ссылку-приглашение (нет права «Приглашать»). Отправь invite-ссылку вручную "
                              "(<code>https://t.me/+…</code>).", reply_markup=cancel_kb("sb"))
    save_sub(info, link, m.from_user.id)
    await state.clear()
    await m.answer(f"✅ Канал <b>{esc(info['title'])}</b> добавлен в обязательную подписку.")
    await m.answer(*(await sec_sb(m.from_user.id, ["ap", "sb"], state, None)))


@pm_router.message(AP.sub_link)
async def ap_sub_link(m: Message, state: FSMContext):
    link = norm_url(m.text or "")
    if not has_perm(m.from_user.id, "subs") or not link:
        return await m.answer("❌ Нужна ссылка вида <code>https://t.me/+…</code>", reply_markup=cancel_kb("sb"))
    d = await state.get_data()
    save_sub(d["sc"], link, m.from_user.id)
    await state.clear()
    await m.answer(f"✅ Канал <b>{esc(d['sc']['title'])}</b> добавлен.")
    await m.answer(*(await sec_sb(m.from_user.id, ["ap", "sb"], state, None)))


# ── оформление
async def sec_ui(uid, p, state, cb):
    act = p[2] if len(p) > 2 else ""
    if act == "st":
        return await ask_edit(state, "start_text", "ui")
    if act == "ph":
        extra = [[B("🗑 Убрать фото", "ap:ui:phx", "danger")]] if sget("start_photo") else None
        return await ask(state, AP.photo, "ui", "🖼 <b>Фото для /start</b>\n\nОтправь картинку (как фото, не файлом). Подпись /start должна быть до 1000 символов.", extra)
    if act == "phx":
        sdel("start_photo")
        await state.clear()
    if act == "gvp":
        sset("gv_public", "0" if sget("gv_public") == "1" else "1")
    if act == "lim":
        steps = [1, 2, 3, 5, 10, 20, 50]
        cur = int(sget("gv_limit"))
        i = steps.index(cur) if cur in steps else 3
        sset("gv_limit", steps[max(0, min(len(steps) - 1, i + int(p[3])))])
    if act == "pv" and cb:
        await send_start(cb.message, cb.from_user)
        return None
    pub = sget("gv_public") == "1"
    nb = q1("SELECT COUNT(*) FROM ubuttons")[0]
    text = (f"🎨 <b>Оформление бота</b>\n{HR}\n📝 Текст /start: <b>{'свой' if q1('SELECT 1 FROM settings WHERE key=?', ('start_text',)) else 'по умолчанию'}</b>\n"
            f"🖼 Фото: <b>{'есть' if sget('start_photo') else 'нет'}</b>\n🔘 Своих кнопок: <b>{nb}</b>\n"
            f"🎁 Розыгрыши для всех: <b>{'ВКЛ' if pub else 'ВЫКЛ'}</b> (лимит активных на человека: <b>{sget('gv_limit')}</b>)")
    return text, kb([
        [B("📝 Текст /start", "ap:ui:st", "primary"), B("🖼 Фото /start", "ap:ui:ph", "primary")],
        [B("🔘 Кнопки", "ap:bt", "success"), B("🤖 Профиль бота", "ap:pf", "primary")],
        [B(f"🎁 Розыгрыши для всех: {'ВКЛ ✅' if pub else 'ВЫКЛ ❌'}", "ap:ui:gvp", "success" if pub else "danger")],
        [B("➖", "ap:ui:lim:-1"), B(f"Лимит: {sget('gv_limit')}", "ap:noop"), B("➕", "ap:ui:lim:1")],
        [B("👁 Предпросмотр /start", "ap:ui:pv", "success")], [B("⬅️ Назад", "ap:main")]])


@pm_router.message(AP.photo)
async def ap_photo(m: Message, state: FSMContext):
    if not has_perm(m.from_user.id, "ui"):
        await state.clear()
        return await m.answer("⛔ Нет доступа.")
    if not m.photo:
        return await m.answer("❌ Отправь картинку как фото.", reply_markup=cancel_kb("ui"))
    sset("start_photo", m.photo[-1].file_id)
    await state.clear()
    await m.answer("✅ Фото сохранено")
    await m.answer(*(await sec_ui(m.from_user.id, ["ap", "ui"], state, None)))


async def sec_pf(uid, p, state, cb):
    if len(p) > 2 and p[2] in ("name", "desc", "short"):
        return await ask_edit(state, "bot_" + p[2], "pf")
    n, d, s = await cur_value("bot_name"), await cur_value("bot_desc"), await cur_value("bot_short")
    text = (f"🤖 <b>Профиль бота</b>\n{HR}\n🏷 <b>Имя:</b> {esc(n)}\n\n📄 <b>Описание:</b>\n{esc(d) or '—'}\n\n✍️ <b>Короткое:</b> {esc(s) or '—'}\n\n"
            "<i>Изменения применяются сразу в профиле бота в Telegram.</i>")
    return text, kb([[B("🏷 Имя", "ap:pf:name", "primary"), B("📄 Описание", "ap:pf:desc", "primary")],
                     [B("✍️ Короткое описание", "ap:pf:short", "primary")], [B("⬅️ Назад", "ap:ui")]])


# ── кнопки
def cbtn_card(bid, note=""):
    r = q1("SELECT * FROM ubuttons WHERE id=?", (bid,))
    if not r:
        return None
    text = (f"{note}🔘 <b>Кнопка</b>\n{HR}\n🏷 Текст: <b>{esc(r['text'])}</b>\n🔗 Ссылка: {esc(r['url'])}\n"
            f"🎨 Цвет: {STYLE_NAME[r['style'] or '']}\n📐 Положение: {'с новой строки' if r['newrow'] else 'в ряд с предыдущей'}")
    return text, kb([
        [B("✏️ Текст", f"ap:bt:ct:{bid}", "primary"), B("🔗 Ссылка", f"ap:bt:cl:{bid}", "primary")],
        [B("🎨 Цвет", f"ap:bt:cs:{bid}", "primary"), B("↔️ Положение", f"ap:bt:cr:{bid}", "primary")],
        [B("⬆️", f"ap:bt:cu:{bid}"), B("⬇️", f"ap:bt:cd:{bid}"), B("🗑 Удалить", f"ap:bt:cx:{bid}", "danger")],
        [B("⬅️ К списку", "ap:bt")]])


def bbtn_card(k, note=""):
    c = bb_all()[k]
    text = (f"{note}🔘 <b>Кнопка «{BB_NAME[k]}»</b>\n{HR}\n🏷 Текст: <b>{esc(c['t'])}</b>\n🎨 Цвет: {STYLE_NAME[c['s'] or '']}\n"
            f"👁 Показывается: <b>{'да' if c['on'] else 'нет'}</b>")
    return text, kb([
        [B("✏️ Текст", f"ap:bt:bt:{k}", "primary"), B("🎨 Цвет", f"ap:bt:bs:{k}", "primary")],
        [B("🙈 Скрыть" if c["on"] else "👁 Показать", f"ap:bt:bv:{k}", "danger" if c["on"] else "success"), B("↩️ Сбросить", f"ap:bt:br:{k}")],
        [B("⬅️ К списку", "ap:bt")]])


def move_btn(bid, d):
    ids = [r[0] for r in q("SELECT id FROM ubuttons ORDER BY pos, id")]
    for i, rid in enumerate(ids):
        ex("UPDATE ubuttons SET pos=? WHERE id=?", (i, rid))
    if bid in ids:
        i = ids.index(bid)
        j = i + d
        if 0 <= j < len(ids):
            ex("UPDATE ubuttons SET pos=? WHERE id=?", (j, bid))
            ex("UPDATE ubuttons SET pos=? WHERE id=?", (i, ids[j]))


async def sec_bt(uid, p, state, cb):
    act = p[2] if len(p) > 2 else ""
    arg = p[3] if len(p) > 3 else ""
    if act == "pv" and cb:
        await send_start(cb.message, cb.from_user)
        return None
    if act == "new":
        return await ask(state, AP.btn_new, "bt", "➕ <b>Новая кнопка</b>\n\nОтправь одним сообщением:\n<code>Текст кнопки | ссылка</code>\n\n"
                         "<i>Пример:</i> <code>📢 Наш канал | https://t.me/orenix</code>\nЦвет и положение настроишь после создания. До 8 своих кнопок.")
    if act in ("b", "bs", "bv", "bt", "br") and arg in BB_DEF:
        if act == "bt":
            return await ask(state, AP.btn_edit, "bt", f"✏️ Отправь новый текст для кнопки «{BB_NAME[arg]}» (до 30 символов, можно эмодзи).", field="bb", key=arg)
        if act == "bs":
            bb_save(arg, s=next_style(bb_all()[arg]["s"]))
        elif act == "bv":
            bb_save(arg, on=0 if bb_all()[arg]["on"] else 1)
        elif act == "br":
            bb_save(arg, reset=True)
        return bbtn_card(arg)
    if act in ("c", "cs", "cr", "cu", "cd", "cx", "ct", "cl") and arg.isdigit():
        bid = int(arg)
        r = q1("SELECT * FROM ubuttons WHERE id=?", (bid,))
        if not r:
            act = ""
        elif act == "ct":
            return await ask(state, AP.btn_edit, "bt", "✏️ Отправь новый текст кнопки (до 30 символов).", field="text", key=bid)
        elif act == "cl":
            return await ask(state, AP.btn_edit, "bt", "🔗 Отправь новую ссылку (https://…, t.me/… или @username).", field="url", key=bid)
        elif act == "cs":
            ex("UPDATE ubuttons SET style=? WHERE id=?", (next_style(r["style"]), bid))
        elif act == "cr":
            ex("UPDATE ubuttons SET newrow=? WHERE id=?", (0 if r["newrow"] else 1, bid))
        elif act in ("cu", "cd"):
            move_btn(bid, -1 if act == "cu" else 1)
        elif act == "cx":
            ex("DELETE FROM ubuttons WHERE id=?", (bid,))
            act = ""
        if act:
            return cbtn_card(bid)
    bb = bb_all()
    rows = [[B(f"{'✅' if bb[k]['on'] else '🙈'} {bb[k]['t']}", f"ap:bt:b:{k}", bb[k]["s"])] for k in BB_DEF]
    cs = q("SELECT * FROM ubuttons ORDER BY pos, id")
    rows += [[B(f"✏️ {r['text'][:26]}", f"ap:bt:c:{r['id']}", r["style"])] for r in cs]
    if len(cs) < 8:
        rows.append([B("➕ Добавить кнопку", "ap:bt:new", "success")])
    rows.append([B("👁 Предпросмотр /start", "ap:bt:pv", "primary")])
    rows.append([B("⬅️ Назад", "ap:ui")])
    return (f"🔘 <b>Кнопки главного экрана</b>\n{HR}\nСверху — встроенные (можно переименовать, перекрасить, скрыть), ниже — твои ссылки. "
            "Нажми на кнопку, чтобы настроить."), kb(rows)


@pm_router.message(AP.btn_new)
async def ap_btn_new(m: Message, state: FSMContext):
    if not has_perm(m.from_user.id, "ui"):
        await state.clear()
        return await m.answer("⛔ Нет доступа.")
    parts = [x.strip() for x in (m.text or "").split("|")]
    url = norm_url(parts[1]) if len(parts) > 1 else None
    if len(parts) < 2 or not parts[0] or len(parts[0]) > 30 or not url:
        return await m.answer("❌ Формат: <code>Текст | https://ссылка</code> (текст до 30 символов).", reply_markup=cancel_kb("bt"))
    if q1("SELECT COUNT(*) FROM ubuttons")[0] >= 8:
        await state.clear()
        return await m.answer("❌ Уже 8 кнопок — это максимум.")
    pos = (q1("SELECT COALESCE(MAX(pos),-1) FROM ubuttons")[0]) + 1
    cur = ex("INSERT INTO ubuttons(text,url,style,newrow,pos) VALUES(?,?,?,?,?)", (parts[0], url, "", 1, pos))
    await state.clear()
    await m.answer(*cbtn_card(cur.lastrowid, "✅ Кнопка добавлена.\n\n"))


@pm_router.message(AP.btn_edit)
async def ap_btn_edit(m: Message, state: FSMContext):
    if not has_perm(m.from_user.id, "ui"):
        await state.clear()
        return await m.answer("⛔ Нет доступа.")
    d = await state.get_data()
    field, key, val = d.get("field"), d.get("key"), (m.text or "").strip()
    if field in ("bb", "text"):
        if not val or len(val) > 30:
            return await m.answer("❌ Текст от 1 до 30 символов.", reply_markup=cancel_kb("bt"))
    if field == "bb":
        bb_save(key, t=val)
        await state.clear()
        return await m.answer(*bbtn_card(key, "✅ Сохранено.\n\n"))
    if field == "text":
        ex("UPDATE ubuttons SET text=? WHERE id=?", (val, key))
    elif field == "url":
        url = norm_url(val)
        if not url:
            return await m.answer("❌ Нужна ссылка: https://…, t.me/… или @username.", reply_markup=cancel_kb("bt"))
        ex("UPDATE ubuttons SET url=? WHERE id=?", (url, key))
    await state.clear()
    card = cbtn_card(key, "✅ Сохранено.\n\n")
    await m.answer(*(card or await sec_bt(m.from_user.id, ["ap", "bt"], state, None)))


# ── чаты и каналы
ICON_CT = {"group": "👥", "supergroup": "👥", "channel": "📣"}


async def sec_ch(uid, p, state, cb):
    act = p[2] if len(p) > 2 else ""
    if act == "add":
        return await ask(state, AP.chat_add, "ch", "➕ <b>Добавить чат или канал вручную</b>\n\nОтправь @username, ID или перешли пост из канала. "
                         "Бот должен уже быть там — так он попадёт в список рассылки.")
    if act == "v":
        cid = int(p[3])
        r = q1("SELECT * FROM chats WHERE chat_id=?", (cid,))
        if not r:
            return await sec_ch(uid, ["ap", "ch"], state, cb)
        try:
            mc = await bot.get_chat_member_count(cid)
        except TelegramAPIError:
            mc = "—"
        n = q1("SELECT COALESCE(SUM(cnt),0) FROM msg_stats WHERE chat_id=?", (cid,))[0]
        return (f"{ICON_CT.get(r['type'], '💬')} <b>{esc(r['title'])}</b>\n{HR}\n🆔 <code>{cid}</code>\n📂 Тип: {r['type']}\n"
                f"👥 Участников: <b>{mc}</b>\n✉️ Сообщений: <b>{n}</b>\n📅 Добавлен: {fmt_ts(r['added_at'])}"), kb([
            [B("🚪 Покинуть", f"ap:ch:leave:{cid}", "danger")], [B("⬅️ К списку", "ap:ch")]])
    if act == "leave":
        cid = int(p[3])
        try:
            await bot.leave_chat(cid)
        except TelegramAPIError:
            pass
        ex("UPDATE chats SET active=0 WHERE chat_id=?", (cid,))
        return "🚪 Готово — бот вышел из чата.", kb([[B("⬅️ К списку", "ap:ch")]])
    flt = p[3] if act == "l" else "all"
    page = int(p[4]) if act == "l" else 0
    cond = {"all": "", "g": "AND type IN ('group','supergroup')", "c": "AND type='channel'"}.get(flt, "")
    rows_db = q(f"SELECT c.chat_id, c.title, c.type, COALESCE((SELECT SUM(cnt) FROM msg_stats s WHERE s.chat_id=c.chat_id),0) n "
                f"FROM chats c WHERE c.active=1 {cond} ORDER BY n DESC, c.added_at DESC")
    chunk, page, pages = paged(rows_db, page)
    tabs = [B(("• " if flt == k else "") + t, f"ap:ch:l:{k}:0", "primary" if flt == k else None)
            for k, t in (("all", "Все"), ("g", "Группы"), ("c", "Каналы"))]
    rows = [tabs]
    rows += [[B(f"{ICON_CT.get(r['type'], '💬')} {(r['title'] or str(r['chat_id']))[:26]}", f"ap:ch:v:{r['chat_id']}")] for r in chunk]
    rows += nav_row(f"ap:ch:l:{flt}:", page, pages)
    rows.append([B("➕ Добавить вручную", "ap:ch:add", "success")])
    rows.append([B("⬅️ Назад", "ap:main")])
    return f"💬 <b>Чаты и каналы бота</b> ({len(rows_db)})\n{HR}" + ("" if rows_db else "\n\nПока пусто."), kb(rows)


@pm_router.message(AP.chat_add)
async def ap_chat_add(m: Message, state: FSMContext):
    if not has_perm(m.from_user.id, "chats"):
        await state.clear()
        return await m.answer("⛔ Нет доступа.")
    try:
        chat, me = await target_chat(m)
    except (TelegramAPIError, ValueError, IndexError) as e:
        return await m.answer(f"❌ {esc(e)}", reply_markup=cancel_kb("ch"))
    if me.status in ("left", "kicked"):
        return await m.answer("❌ Бота нет в этом чате.", reply_markup=cancel_kb("ch"))
    ex("INSERT INTO chats(chat_id,title,type,added_at,active) VALUES(?,?,?,?,1) "
       "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, type=excluded.type, active=1", (chat.id, chat.title, chat.type, now()))
    await state.clear()
    await m.answer(f"✅ <b>{esc(chat.title)}</b> добавлен в список.")
    await m.answer(*(await sec_ch(m.from_user.id, ["ap", "ch"], state, None)))


# ── админы и права
def ad_list(actor, note=""):
    rows_db = q("SELECT user_id FROM gadmins ORDER BY added_at")
    rows = [[B(f"👮 {uname(r['user_id'])[:22]} · {len(perms_of(r['user_id']))} прав", f"ap:ad:v:{r['user_id']}", "primary")] for r in rows_db]
    rows.append([B("➕ Назначить админа", "ap:ad:add", "success")])
    rows.append([B("⬅️ Назад", "ap:main")])
    return (f"{note}👮 <b>Админы бота</b>\n{HR}\n👑 Владелец: {mention(OWNER_ID, uname(OWNER_ID))} — полный доступ\n\n"
            + (f"Назначено: <b>{len(rows_db)}</b>. Нажми на админа, чтобы настроить доступ или снять." if rows_db else "<i>Других админов пока нет.</i>")), kb(rows)


def ad_card(target, actor, note=""):
    r = q1("SELECT * FROM gadmins WHERE user_id=?", (target,))
    if not r:
        return ad_list(actor, "⚠️ Такого админа нет.\n\n")
    P = perms_of(target)
    manage = can_manage(actor, target)
    lines = [f"{'✅' if k in P else '▫️'} {ic} {nm}" for k, (ic, nm) in PERMS.items()]
    text = (f"{note}👮 <b>{esc(uname(target))}</b>\n🆔 <code>{target}</code>\n"
            f"📅 Назначен: {fmt_ts(r['added_at']) if r['added_at'] else '—'}"
            + (f" · {esc(uname(r['added_by']))}" if r["added_by"] else "") + f"\n{HR}\n<b>Доступ:</b>\n" + "\n".join(lines))
    rows = []
    if manage:
        for k, (ic, nm) in PERMS.items():
            if can_grant(actor, k):
                rows.append([B(f"{'✅' if k in P else '▫️'} {ic} {nm}", f"ap:ad:t:{target}:{k}", "success" if k in P else None)])
        rows.append([B(t, f"ap:ad:p:{target}:{k}", "primary") for k, (t, _) in PRESETS.items()])
        rows.append([B("🗑 Снять с должности", f"ap:ad:d:{target}", "danger")])
    else:
        text += "\n\n🔒 <i>Ты не можешь менять этого админа.</i>"
    rows.append([B("⬅️ К списку", "ap:ad")])
    return text, kb(rows)


def set_perms(target, perms):
    ex("UPDATE gadmins SET perms=? WHERE user_id=?", (",".join(k for k in PERMS if k in perms), target))


async def sec_ad(uid, p, state, cb):
    act = p[2] if len(p) > 2 else ""
    t = int(p[3]) if len(p) > 3 and p[3].lstrip("-").isdigit() else 0
    if act == "add":
        return await ask(state, AP.add_admin, "ad", "👮 <b>Назначить админа</b>\n\nОтправь <b>ID</b>, <b>@username</b> (если он писал боту) или <b>перешли сообщение</b> человека. "
                         "Права настроишь на следующем шаге.")
    if act == "v":
        return ad_card(t, uid)
    if act in ("t", "p", "d", "dd") and not can_manage(uid, t):
        return ad_card(t, uid, "⛔ Нельзя менять этого админа.\n\n")
    if act == "t" and p[4] in PERMS and can_grant(uid, p[4]):
        P = perms_of(t) ^ {p[4]}
        set_perms(t, P)
        return ad_card(t, uid)
    if act == "p" and p[4] in PRESETS:
        keep = {k for k in PRESETS[p[4]][1] if can_grant(uid, k)}
        set_perms(t, keep)
        return ad_card(t, uid, f"✅ Применён набор «{PRESETS[p[4]][0]}».\n\n")
    if act == "d":
        return (f"⚠️ <b>Снять {esc(uname(t))} с должности админа?</b>\n\nОн потеряет доступ к панели сразу."), kb([
            [B("🗑 Да, снять", f"ap:ad:dd:{t}", "danger"), B("⬅️ Нет", f"ap:ad:v:{t}")]])
    if act == "dd":
        ex("DELETE FROM gadmins WHERE user_id=?", (t,))
        try:
            await bot.send_message(t, "ℹ️ Вас сняли с должности админа бота.")
        except TelegramAPIError:
            pass
        return ad_list(uid, f"✅ {esc(uname(t))} больше не админ.\n\n")
    return ad_list(uid)


@pm_router.message(AP.add_admin)
async def ap_add_admin(m: Message, state: FSMContext):
    uid = m.from_user.id
    if not has_perm(uid, "admins"):
        await state.clear()
        return await m.answer("⛔ Нет доступа.")
    t, name, _ = await parse_user_input(m)
    if not t:
        return await m.answer("❌ Не нашёл пользователя. Отправь числовой ID, @username или перешли его сообщение.", reply_markup=cancel_kb("ad"))
    if t == OWNER_ID or is_gadmin(t):
        await state.clear()
        return await m.answer("ℹ️ Он уже админ.", reply_markup=kb([[B("⬅️ К списку", "ap:ad")]]))
    keep = {k for k in PRESETS["mod"][1] if can_grant(uid, k)}
    ex("INSERT OR REPLACE INTO gadmins(user_id,perms,added_by,added_at) VALUES(?,?,?,?)", (t, ",".join(sorted(keep)), uid, now()))
    unblock_user(t)
    await state.clear()
    try:
        await bot.send_message(t, "👮 <b>Тебя назначили админом бота!</b>\nОткрой панель управления 👇",
                               reply_markup=kb([[B("🛠 Админ-панель", "ap:main", "success")]]))
    except TelegramAPIError:
        pass
    await m.answer(*ad_card(t, uid, "✅ Админ назначен — настрой ему доступ.\n\n"))


# ── диспетчер
SECTIONS = {"pw": sec_pw, "st": sec_st, "us": sec_us, "bc": sec_bc, "sb": sec_sb, "ui": sec_ui, "pf": sec_pf,
            "bt": sec_bt, "ch": sec_ch, "ad": sec_ad, "rs": sec_rs}


async def sec_main(uid, p, state, cb):
    await state.clear()
    return ap_main(uid)


SECTIONS["main"] = sec_main


@cb_router.callback_query(F.data.startswith("ap:"))
async def cb_ap(cb: CallbackQuery, state: FSMContext):
    p = cb.data.split(":")
    sec, uid = p[1], cb.from_user.id
    if not is_gadmin(uid):
        return await cb.answer("⛔ Только для админов бота", show_alert=True)
    if sec == "cx":  # отмена ввода → возврат в раздел
        sec, p = p[2], ["ap", p[2]]
        await state.clear()
    need = SEC_PERM.get(sec)
    if need and not has_perm(uid, need):
        return await cb.answer("⛔ У тебя нет доступа к этому разделу", show_alert=True)
    await cb.answer()
    if sec == "home":
        await state.clear()
        await send_start(cb.message, cb.from_user)
        try:
            await cb.message.delete()
        except TelegramAPIError:
            pass
        return
    fn = SECTIONS.get(sec)
    if fn:
        res = await fn(uid, p, state, cb)
        if res:
            await edit(cb, *res)


# ═════════════════════════ Запуск ═════════════════════════
def _base_url() -> str:
    u = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if u and not u.startswith(("http://", "https://")):
        u = "https://" + u
    if u.endswith(WEBHOOK_PATH):  # если в переменную вписали полный путь
        u = u[: -len(WEBHOOK_PATH)]
    return u


async def on_startup(bot: Bot):
    global BOT_USERNAME
    base = _base_url()
    if not base:
        raise RuntimeError("Задайте WEBHOOK_URL или запустите на Render (RENDER_EXTERNAL_URL).")
    me = await bot.get_me()
    BOT_USERNAME = me.username or BOT_USERNAME
    url = f"{base}{WEBHOOK_PATH}"
    for attempt in range(1, 6):
        try:
            await bot.set_webhook(url, secret_token=WEBHOOK_SECRET, drop_pending_updates=True,
                                  allowed_updates=dp.resolve_used_update_types())
            break
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Exception:
            logging.exception("set_webhook попытка %d/5 не удалась (url=%s)", attempt, url)
            if attempt == 5:
                raise
            await asyncio.sleep(3 * attempt)
    try:
        wi = await bot.get_webhook_info()
        logging.info("Webhook: url=%s pending=%s last_error=%s", wi.url, wi.pending_update_count, wi.last_error_message)
    except Exception:
        pass
    try:
        await bot.set_my_commands([
            BotCommand(command="help", description="Список команд"), BotCommand(command="stats", description="Статистика чата"),
            BotCommand(command="top", description="Топ чата"), BotCommand(command="me", description="Мой профиль"),
            BotCommand(command="daily", description="Ежедневный бонус"), BotCommand(command="settings", description="Настройки чата"),
        ])
        await bot.set_my_commands([
            BotCommand(command="start", description="Главное меню"), BotCommand(command="giveaway", description="Создать розыгрыш"),
            BotCommand(command="help", description="Список команд"),
        ], scope=BotCommandScopeAllPrivateChats())
    except Exception:
        logging.exception("set_my_commands не удался")
    spawn(scheduler())
    logging.info("Запущен как @%s | webhook %s", BOT_USERNAME, url)


async def on_shutdown(bot: Bot):
    # ВАЖНО: webhook здесь НЕ удаляем. На Render новый инстанс стартует до остановки старого,
    # и delete_webhook из старого убивал бота после каждого деплоя/усыпления.
    await bot.session.close()


async def keepalive(app: web.Application):
    """Free-план Render усыпляет сервис через 15 минут тишины — пингуем сами себя."""
    async def loop():
        import aiohttp
        await asyncio.sleep(60)
        while True:
            try:
                async with aiohttp.ClientSession() as cs:
                    async with cs.get(_base_url() + "/", timeout=aiohttp.ClientTimeout(total=20)) as r:
                        logging.info("keepalive %s", r.status)
            except Exception:
                logging.warning("keepalive не удался")
            await asyncio.sleep(600)

    task = asyncio.create_task(loop())
    yield
    task.cancel()


def main():
    init_db()
    dp.message.outer_middleware(Gate())
    dp.callback_query.outer_middleware(Gate())
    dp.include_router(pm_router)
    dp.include_router(group_router)
    dp.include_router(cb_router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    app = web.Application()

    async def health(_):
        return web.Response(text="ok")

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    if os.environ.get("RENDER_EXTERNAL_URL") and os.environ.get("KEEPALIVE", "1") == "1":
        app.cleanup_ctx.append(keepalive)
    logging.info("Старт: base=%s port=%s db=%s", _base_url(), PORT, DB_PATH)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
