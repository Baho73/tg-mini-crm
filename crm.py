# FILE: crm.py
# START_MODULE_CONTRACT
#   PURPOSE: Мини-CRM агентства: лиды из Telegram-бота, из подключенного личного Telegram
#     (Telegram Business), ручное добавление, теги и фильтр по тегу.
#   INPUTS: BOT_TOKEN, CRM_USER, CRM_PASSWORD (env); сообщения Telegram; формы веб-страницы
#   OUTPUTS: таблицы leads / tags в SQLite (DB_PATH), HTML-страница CRM за Basic-auth
#   RULES: один процесс = веб (FastAPI) + бот (aiogram polling). Лид из одного Telegram-чата
#     не дублируется: повторные сообщения дописываются в его историю.
# END_MODULE_CONTRACT

import asyncio
import html
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from urllib.parse import urlencode, urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

DB_PATH = os.getenv("DB_PATH", "crm.db")


# START_BLOCK_STORAGE
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            contact TEXT,
            request TEXT,
            source TEXT NOT NULL,              -- bot | telegram | manual
            tg_chat_id INTEGER,                -- для склейки повторных сообщений
            tg_conn TEXT,                      -- business_connection_id: чьему менеджеру писали
            created_at TEXT DEFAULT (datetime('now', '+3 hours'))
        );
        CREATE TABLE IF NOT EXISTS tags (
            lead_id INTEGER REFERENCES leads(id) ON DELETE CASCADE,
            tag TEXT NOT NULL,
            PRIMARY KEY (lead_id, tag)
        );
        """)
        if "tg_conn" not in [r[1] for r in c.execute("PRAGMA table_info(leads)")]:
            c.execute("ALTER TABLE leads ADD COLUMN tg_conn TEXT")  # база первой версии


def add_lead(name, contact, request, source, tg_chat_id=None) -> int:
    with db() as c:
        return c.execute(
            "INSERT INTO leads (name, contact, request, source, tg_chat_id) VALUES (?, ?, ?, ?, ?)",
            (name.strip() or "Без имени", contact, request, source, tg_chat_id)).lastrowid


def upsert_tg_message(chat_id, name, contact, text, source, conn_id=None) -> int:
    """Сообщение из личного Telegram: первое создает лида, следующие дописываются в запрос.
    Ключ склейки: чат клиента + подключенный аккаунт менеджера (два менеджера = два лида)."""
    with db() as c:
        row = c.execute("SELECT id, request FROM leads WHERE tg_chat_id = ? AND source = ? AND tg_conn IS ?",
                        (chat_id, source, conn_id)).fetchone()
        if row:
            c.execute("UPDATE leads SET request = ? WHERE id = ?",
                      ((row["request"] + "\n" if row["request"] else "") + text, row["id"]))
            return row["id"]
        return c.execute(
            "INSERT INTO leads (name, contact, request, source, tg_chat_id, tg_conn) VALUES (?, ?, ?, ?, ?, ?)",
            (name.strip() or "Без имени", contact, text, source, chat_id, conn_id)).lastrowid


def add_tag(lead_id, tag) -> bool:
    """False, если лида нет. Запятая запрещена: это разделитель в списке тегов."""
    tag = tag.strip().lower().replace(",", " ")
    with db() as c:
        if not c.execute("SELECT 1 FROM leads WHERE id = ?", (lead_id,)).fetchone():
            return False
        if tag:
            c.execute("INSERT OR IGNORE INTO tags (lead_id, tag) VALUES (?, ?)", (lead_id, tag))
    return True


def remove_tag(lead_id, tag):
    with db() as c:
        c.execute("DELETE FROM tags WHERE lead_id = ? AND tag = ?", (lead_id, tag))


def list_leads(tag=None):
    q = """SELECT l.*, COALESCE(GROUP_CONCAT(t.tag, ','), '') AS tags
           FROM leads l LEFT JOIN tags t ON t.lead_id = l.id"""
    params = ()
    if tag:
        q += " WHERE l.id IN (SELECT lead_id FROM tags WHERE tag = ?)"
        params = (tag,)
    with db() as c:
        return c.execute(q + " GROUP BY l.id ORDER BY l.id DESC", params).fetchall()


def all_tags():
    with db() as c:
        return [r[0] for r in c.execute("SELECT DISTINCT tag FROM tags ORDER BY tag")]
# END_BLOCK_STORAGE


# START_BLOCK_BOT
class Form_(StatesGroup):
    name = State()
    contact = State()
    request = State()


dp = Dispatcher(events_isolation=SimpleEventIsolation())  # сообщения одного чата по очереди, без гонок FSM


@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Form_.name)
    await m.answer("Здравствуйте! Оставьте заявку агентству, это 3 вопроса.\nКак вас зовут?",
                   reply_markup=ReplyKeyboardRemove())


@dp.message(Form_.name, F.text)
async def got_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text)
    await state.set_state(Form_.contact)
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, keyboard=[
        [KeyboardButton(text="Отправить мой телефон", request_contact=True)]])
    await m.answer("Как с вами связаться? Телефон, email или нажмите кнопку.", reply_markup=kb)


@dp.message(Form_.contact, F.contact | F.text)
async def got_contact(m: Message, state: FSMContext):
    contact = m.contact.phone_number if m.contact else m.text
    await state.update_data(contact=contact)
    await state.set_state(Form_.request)
    await m.answer("Что нужно сделать? Опишите задачу в свободной форме.", reply_markup=ReplyKeyboardRemove())


@dp.message(Form_.request, F.text | F.caption)
async def got_request(m: Message, state: FSMContext):
    d = await state.get_data()
    tg = f"@{m.from_user.username}" if m.from_user.username else f"tg id {m.from_user.id}"
    lead_id = add_lead(d["name"], f'{d["contact"]} ({tg})', m.text or m.caption, "bot", m.chat.id)
    await state.clear()
    await m.answer(f"Спасибо! Заявка №{lead_id} принята, менеджер свяжется с вами.")


@dp.message(Form_.name)
@dp.message(Form_.contact)
@dp.message(Form_.request)
async def need_text(m: Message):
    await m.answer("Пришлите, пожалуйста, ответ текстом.")


@dp.message()
async def fallback(m: Message):
    await m.answer("Чтобы оставить заявку, нажмите /start")


# Пункт 2: личный Telegram подключается к боту через Telegram Business
# (Настройки -> Telegram для бизнеса -> Чат-боты). Входящие клиентов приходят как business_message.
@dp.business_message(F.text | F.caption)
async def business_in(m: Message):
    if m.from_user.id == m.chat.id:  # пишет клиент, а не владелец аккаунта
        tg = f"@{m.from_user.username}" if m.from_user.username else f"tg id {m.from_user.id}"
        upsert_tg_message(m.chat.id, m.from_user.full_name, tg, m.text or m.caption, "telegram",
                          m.business_connection_id)
# END_BLOCK_BOT


# START_BLOCK_WEB
security = HTTPBasic()


def same_origin(request: Request):
    """CSRF: браузер сам прикладывает Basic-auth к чужой форме, поэтому POST только со своей страницы."""
    src = request.headers.get("origin") or request.headers.get("referer")
    if not src or urlparse(src).netloc != request.headers.get("host"):
        raise HTTPException(403, "cross-origin")


def auth(cred: HTTPBasicCredentials = Depends(security)):
    ok = secrets.compare_digest(cred.username, os.getenv("CRM_USER", "admin")) and \
        secrets.compare_digest(cred.password, os.getenv("CRM_PASSWORD", ""))
    if not ok or not os.getenv("CRM_PASSWORD"):
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


class RetryNetwork(BaseRequestMiddleware):
    """С российского сервера ~1 из 10 соединений с Telegram рвется (DPI): повторяем запрос,
    иначе ответ бота клиенту теряется."""

    async def __call__(self, make_request, bot, method):
        for attempt in range(3):
            try:
                return await make_request(bot, method)
            except TelegramNetworkError:
                if attempt == 2:
                    raise
                await asyncio.sleep(1 + attempt)


@asynccontextmanager
async def lifespan(_):
    init_db()
    task = None
    if os.getenv("BOT_TOKEN"):
        bot = Bot(os.environ["BOT_TOKEN"])
        bot.session.middleware(RetryNetwork())
        task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        # бот умер (например, отозван токен), а веб жив: роняем процесс, docker поднимет заново
        task.add_done_callback(lambda t: t.cancelled() or os._exit(1))
    yield
    if task:
        task.cancel()


app = FastAPI(lifespan=lifespan)
SOURCE = {"bot": "бот", "telegram": "личный Telegram", "manual": "вручную"}
e = html.escape


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
def index(tag: str = ""):
    rows = "".join(f"""<tr><td>{r['id']}</td><td>{r['created_at'][:16]}</td><td>{SOURCE[r['source']]}</td>
      <td>{e(r['name'])}</td><td>{e(r['contact'] or '')}</td><td class=req>{e(r['request'] or '')}</td>
      <td>{''.join(f'<form method=post action=/untag class=t><input type=hidden name=lead_id value={r["id"]}><input type=hidden name=tag value="{e(t)}"><a href="/?{e(urlencode({'tag': t}))}">#{e(t)}</a><button title="снять">×</button></form>' for t in r['tags'].split(',') if t)}
      <form method=post action=/tag><input type=hidden name=lead_id value={r['id']}><input name=tag placeholder="тег" size=8 required><button>+</button></form></td></tr>"""
                   for r in list_leads(tag or None))
    tags = " ".join(f'<a href="/?{e(urlencode({'tag': t}))}">#{e(t)}</a>' for t in all_tags())
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>Мини-CRM</title><style>body{{font:14px sans-serif;margin:16px}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccc;padding:4px;vertical-align:top}}.req{{white-space:pre-wrap;max-width:360px}}.t{{display:inline}}
form{{margin:2px 0}}</style>
<h2>Лиды{f' с тегом #{e(tag)} · <a href="/">все</a>' if tag else ''}</h2>
<p>Теги: {tags or 'пока нет'}</p>
<details><summary>Добавить лида вручную</summary><form method=post action=/lead>
<input name=name placeholder="Имя" required> <input name=contact placeholder="Контакт">
<input name=request placeholder="Запрос" size=40> <input name=tag placeholder="тег"> <button>Добавить</button></form></details>
<table><tr><th>№</th><th>Создан (МСК)</th><th>Источник</th><th>Имя</th><th>Контакт</th><th>Запрос</th><th>Теги</th></tr>{rows}</table>"""


POST_DEPS = [Depends(auth), Depends(same_origin)]


@app.post("/lead", dependencies=POST_DEPS)
def create_lead(name: str = Form(...), contact: str = Form(""), request: str = Form(""), tag: str = Form("")):
    add_tag(add_lead(name, contact, request, "manual"), tag)
    return RedirectResponse("/", 303)


@app.post("/tag", dependencies=POST_DEPS)
def tag_lead(lead_id: int = Form(...), tag: str = Form(...)):
    if not add_tag(lead_id, tag):
        raise HTTPException(404, "лид не найден")
    return RedirectResponse("/", 303)


@app.post("/untag", dependencies=POST_DEPS)
def untag_lead(lead_id: int = Form(...), tag: str = Form(...)):
    remove_tag(lead_id, tag)
    return RedirectResponse("/", 303)
# END_BLOCK_WEB
