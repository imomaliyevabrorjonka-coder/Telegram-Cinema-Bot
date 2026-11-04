"""Uzbek cinema bot built with python-telegram-bot."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import InvalidToken, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


logger = logging.getLogger(__name__)

SEARCH_BUTTON = "🎬 Kino qidirish"
SUBSCRIBE_BUTTON = "📢 Kanalga obuna bo‘lish"
PREMIUM_BUTTON = "⭐ Premium obuna"
CONTACT_BUTTON = "📞 Admin bilan bog‘lanish"
ADMIN_BUTTON = "🔐 ADMIN"
SUBSCRIBED_BUTTON = "✅ Obuna bo‘ldim"
BACK_BUTTON = "⬅️ Asosiy menyu"
CANCEL_BUTTON = "❌ Bekor qilish"
STATE_KEY = "cinema_state"
DEFAULT_DB_PATH = Path(__file__).with_name("cinema.sqlite3")
DEFAULT_ORDER_DB_PATH = Path(__file__).with_name("orders.sqlite3")

START_MESSAGE = (
    "🎬 Kino botga xush kelibsiz!\n\n"
    "Kino qidirish yoki Premium xizmatlardan foydalanish uchun menyudan tanlang."
)


def get_bot_token() -> str:
    """Read a token from Replit Secrets without logging its raw value."""
    raw_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    token_match = re.search(r"(?<!\w)\d{5,}:[A-Za-z0-9_-]{20,}(?!\w)", raw_token)
    if token_match is None:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set or does not contain a valid Telegram token."
        )
    return token_match.group(0)


def admin_id() -> int | None:
    """Return the numeric admin ID, if configured."""
    raw_id = os.getenv("ADMIN_ID", "").strip()
    if not raw_id or not raw_id.lstrip("-").isdigit():
        return None
    return int(raw_id)


def is_admin(update: Update) -> bool:
    """Authorize admin tools by Telegram user ID only."""
    configured_id = admin_id()
    user = update.effective_user
    return configured_id is not None and user is not None and user.id == configured_id


def configured_channel() -> str:
    """Return the configured Telegram channel ID or username."""
    return (
        os.getenv("CHANNEL_ID", "").strip()
        or os.getenv("CHANNEL_USERNAME", "").strip()
    )


def main_keyboard(update: Update) -> ReplyKeyboardMarkup:
    """Build the main keyboard, hiding ADMIN from non-admin users."""
    rows = [
        [SEARCH_BUTTON, SUBSCRIBE_BUTTON],
        [PREMIUM_BUTTON, CONTACT_BUTTON],
    ]
    if is_admin(update):
        rows.append([ADMIN_BUTTON])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def admin_keyboard() -> ReplyKeyboardMarkup:
    """Build the administrator menu."""
    return ReplyKeyboardMarkup(
        [
            ["👥 Foydalanuvchilar", "📊 Statistika"],
            ["🎬 Kino qo‘shish", "🗑 Kino o‘chirish"],
            ["🔎 Kino qidirish", "⭐ Premium foydalanuvchilar"],
            ["📢 Kanal sozlamalari"],
            [BACK_BUTTON],
        ],
        resize_keyboard=True,
    )


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_url(value: str) -> str | None:
    """Keep only optional HTTP(S) links from admin input."""
    value = value.strip()
    if not value or value == "-":
        return None
    if not re.match(r"^https?://\S+$", value, flags=re.IGNORECASE):
        return None
    return value


class CinemaStore:
    """SQLite-backed users, movies, and premium status storage."""

    def __init__(self, database_path: str | os.PathLike[str] | None = None) -> None:
        configured_path = os.getenv("CINEMA_DB_PATH", "").strip()
        self.database_path = Path(database_path or configured_path or DEFAULT_DB_PATH)

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        self._ensure_schema(connection)
        return connection

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TEXT NOT NULL,
                is_premium INTEGER NOT NULL DEFAULT 0,
                premium_until TEXT
            );

            CREATE TABLE IF NOT EXISTS movies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL UNIQUE,
                year INTEGER NOT NULL,
                genre TEXT NOT NULL,
                rating REAL NOT NULL,
                description TEXT NOT NULL,
                poster_url TEXT,
                trailer_url TEXT,
                watch_url TEXT,
                created_at TEXT NOT NULL
            );
            """
        )

    def upsert_user(self, update: Update) -> None:
        """Track a user without changing their premium fields."""
        user = update.effective_user
        if user is None:
            return
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users (telegram_id, username, first_name, joined_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name
                """,
                (user.id, user.username, user.full_name, now_utc()),
            )

    def add_movie(self, movie: dict[str, Any]) -> int:
        """Add a movie and return its database ID."""
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO movies (
                        title, year, genre, rating, description,
                        poster_url, trailer_url, watch_url, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        movie["title"],
                        movie["year"],
                        movie["genre"],
                        movie["rating"],
                        movie["description"],
                        movie["poster_url"],
                        movie["trailer_url"],
                        movie["watch_url"],
                        now_utc(),
                    ),
                )
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ValueError("Bu nomdagi kino allaqachon mavjud.") from exc

    def delete_movie(self, movie_id: int) -> bool:
        """Delete a movie by its ID."""
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM movies WHERE id = ?", (movie_id,))
            return cursor.rowcount > 0

    def search_movies(self, query: str) -> list[dict[str, Any]]:
        """Search movie titles and genres case-insensitively."""
        search = f"%{query.strip().casefold()}%"
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, year, genre, rating, description,
                       poster_url, trailer_url, watch_url
                FROM movies
                WHERE lower(title) LIKE ? OR lower(genre) LIKE ?
                ORDER BY title COLLATE NOCASE
                LIMIT 20
                """,
                (search, search),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_users(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent users for the admin panel."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT telegram_id, username, first_name, joined_at,
                       is_premium, premium_until
                FROM users
                ORDER BY joined_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_premium_users(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return stored premium users for the admin panel."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT telegram_id, username, first_name, premium_until
                FROM users
                WHERE is_premium = 1
                ORDER BY premium_until DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def statistics(self) -> dict[str, int]:
        """Return counts for the admin dashboard."""
        with self._connect() as connection:
            users = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            movies = connection.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
            premium = connection.execute(
                "SELECT COUNT(*) FROM users WHERE is_premium = 1"
            ).fetchone()[0]
        return {"users": int(users), "movies": int(movies), "premium": int(premium)}


class OrderHistoryStore:
    """Read persisted shop orders without exposing the order table to users."""

    def __init__(self, database_path: str | os.PathLike[str] | None = None) -> None:
        configured_path = os.getenv("ORDER_DB_PATH", "").strip()
        self.database_path = Path(
            database_path or configured_path or DEFAULT_ORDER_DB_PATH
        )

    def find_by_id(self, order_id: str) -> dict[str, Any] | None:
        """Find one order by its stable ID using a parameterized query."""
        if not self.database_path.exists():
            return None
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT
                    order_id,
                    status,
                    submitted_at,
                    product_name,
                    product_price,
                    quantity,
                    customer_name,
                    phone,
                    telegram_user_id,
                    telegram_username,
                    telegram_name
                FROM orders
                WHERE order_id = ?
                LIMIT 1
                """,
                (order_id,),
            ).fetchone()
        return dict(row) if row is not None else None


def clear_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(STATE_KEY, None)


def movie_text(movie: dict[str, Any]) -> str:
    """Render movie metadata, excluding illegal download instructions."""
    return (
        f"🎬 {movie['title']}\n\n"
        f"📅 Yil: {movie['year']}\n"
        f"🎭 Janr: {movie['genre']}\n"
        f"⭐ Reyting: {movie['rating']}/10\n\n"
        f"{movie['description']}\n\n"
        "Faqat qonuniy treyler va tomosha qilish havolalari ko‘rsatiladi."
    )


def movie_links(movie: dict[str, Any]) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if movie.get("trailer_url"):
        rows.append([InlineKeyboardButton("🎞 Treyler", url=movie["trailer_url"])])
    if movie.get("watch_url"):
        rows.append(
            [InlineKeyboardButton("▶️ Qonuniy tomosha qilish", url=movie["watch_url"])]
        )
    return InlineKeyboardMarkup(rows) if rows else None


async def send_movie(message: Any, movie: dict[str, Any]) -> None:
    """Send a movie poster when possible, with a text fallback."""
    text = movie_text(movie)
    links = movie_links(movie)
    poster_url = movie.get("poster_url")
    if poster_url:
        try:
            await message.reply_photo(
                photo=poster_url,
                caption=text[:1024],
                reply_markup=links,
            )
            return
        except TelegramError:
            logger.warning("Could not send a movie poster; using text fallback")
    await message.reply_text(text, reply_markup=links)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start and show the Uzbek main menu."""
    if update.message is None:
        return
    CinemaStore().upsert_user(update)
    clear_state(context)
    await update.message.reply_text(
        START_MESSAGE, reply_markup=main_keyboard(update)
    )


async def start_movie_search(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.message is None:
        return
    CinemaStore().upsert_user(update)
    context.user_data[STATE_KEY] = {"type": "movie_search"}
    await update.message.reply_text(
        "🎬 Kino nomini yozing:",
        reply_markup=ReplyKeyboardMarkup([[CANCEL_BUTTON]], resize_keyboard=True),
    )


def channel_join_url() -> str | None:
    """Return a safe public/private channel join URL when configured."""
    configured_url = os.getenv("CHANNEL_JOIN_URL", "").strip()
    if configured_url.startswith(("https://", "http://")):
        return configured_url
    username = os.getenv("CHANNEL_USERNAME", "").strip().lstrip("@")
    if username:
        return f"https://t.me/{username}"
    return None


async def show_subscription(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.message is None:
        return
    CinemaStore().upsert_user(update)
    if not configured_channel():
        await update.message.reply_text(
            "Kanal sozlamalari hali kiritilmagan. Keyinroq qayta urinib ko‘ring.",
            reply_markup=main_keyboard(update),
        )
        return

    rows: list[list[InlineKeyboardButton]] = []
    join_url = channel_join_url()
    if join_url:
        rows.append([InlineKeyboardButton("📢 Kanalga qo‘shilish", url=join_url)])
    rows.append([InlineKeyboardButton(SUBSCRIBED_BUTTON, callback_data="channel:check")])
    await update.message.reply_text(
        "📢 Kanalga obuna bo‘ling, so‘ng “✅ Obuna bo‘ldim” tugmasini bosing.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def check_subscription(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    await query.answer()
    channel = configured_channel()
    if not channel:
        await query.edit_message_text("Kanal sozlamalari hali kiritilmagan.")
        return
    try:
        member = await context.bot.get_chat_member(chat_id=channel, user_id=user.id)
        is_member = member.status in {"member", "administrator", "creator"} or (
            member.status == "restricted" and bool(getattr(member, "is_member", False))
        )
    except TelegramError:
        logger.warning("Could not check channel membership")
        await query.edit_message_text(
            "Obunani tekshirib bo‘lmadi. Kanal username/ID va bot huquqlarini tekshiring."
        )
        return

    if not is_member:
        await query.edit_message_text("❌ Avval kanalga obuna bo‘ling.")
        return

    await query.edit_message_text("✅ Obuna tasdiqlandi.")
    if query.message is not None:
        await query.message.reply_text(
            "Asosiy menyu:", reply_markup=main_keyboard(update)
        )


async def show_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    CinemaStore().upsert_user(update)
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("1 oy", callback_data="premium:1"),
                InlineKeyboardButton("3 oy", callback_data="premium:3"),
            ],
            [
                InlineKeyboardButton("6 oy", callback_data="premium:6"),
                InlineKeyboardButton("1 yil", callback_data="premium:12"),
            ],
        ]
    )
    await update.message.reply_text(
        "⭐ Premium tarifini tanlang:", reply_markup=markup
    )


async def premium_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    months = (query.data or "").removeprefix("premium:")
    await query.edit_message_text(
        f"⭐ Premium — {months} oy\n\n"
        "To‘lov tizimi hozircha ulanmagan. To‘lov tasdiqlanmaguncha "
        "Premium status berilmaydi.\n\n"
        "Admin bilan bog‘lanish uchun asosiy menyudan kontakt tugmasini tanlang."
    )


def admin_contact_url() -> str | None:
    value = os.getenv("ADMIN_CONTACT", "").strip()
    if not value:
        return None
    if value.startswith(("https://", "http://")):
        return value
    return f"https://t.me/{value.lstrip('@')}"


async def contact_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    CinemaStore().upsert_user(update)
    url = admin_contact_url()
    if not url:
        await update.message.reply_text(
            "Admin kontakti hali sozlanmagan.", reply_markup=main_keyboard(update)
        )
        return
    await update.message.reply_text(
        "📞 Admin bilan bog‘lanish:",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Admin profilini ochish", url=url)]]
        ),
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    if not is_admin(update):
        await update.message.reply_text("Bu bo‘lim faqat admin uchun.")
        return
    clear_state(context)
    await update.message.reply_text(
        "🔐 Admin paneli:", reply_markup=admin_keyboard()
    )


async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not is_admin(update):
        return
    users = CinemaStore().list_users()
    if not users:
        text = "👥 Hozircha foydalanuvchilar yo‘q."
    else:
        lines = ["👥 Foydalanuvchilar:"]
        for user in users:
            username = f"@{user['username']}" if user["username"] else "username yo‘q"
            premium = " ⭐ Premium" if user["is_premium"] else ""
            lines.append(f"• {user['first_name']} — {username} — ID: {user['telegram_id']}{premium}")
        text = "\n".join(lines)
    await update.message.reply_text(text, reply_markup=admin_keyboard())


async def admin_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not is_admin(update):
        return
    stats = CinemaStore().statistics()
    await update.message.reply_text(
        "📊 Statistika:\n\n"
        f"👥 Foydalanuvchilar: {stats['users']}\n"
        f"🎬 Kinolar: {stats['movies']}\n"
        f"⭐ Premium foydalanuvchilar: {stats['premium']}",
        reply_markup=admin_keyboard(),
    )


async def admin_add_movie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not is_admin(update):
        return
    context.user_data[STATE_KEY] = {"type": "admin_add_movie"}
    await update.message.reply_text(
        "🎬 Kino ma’lumotlarini quyidagi 8 qatorda yuboring:\n\n"
        "1. Nomi\n2. Yili\n3. Janri\n4. Reytingi (0–10)\n"
        "5. Qisqacha tavsif\n6. Poster URL yoki -\n7. Treyler URL yoki -\n"
        "8. Qonuniy tomosha URL yoki -",
        reply_markup=ReplyKeyboardMarkup([[CANCEL_BUTTON]], resize_keyboard=True),
    )


def parse_movie_input(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in text.splitlines()]
    if len(lines) != 8 or any(not line for line in lines[:5]):
        raise ValueError("Aniq 8 ta qator yuboring; birinchi 5 qator bo‘sh bo‘lmasin.")
    try:
        year = int(lines[1])
        rating = float(lines[3].replace(",", "."))
    except ValueError as exc:
        raise ValueError("Yil butun son, reyting esa 0 dan 10 gacha bo‘lishi kerak.") from exc
    if not 1880 <= year <= 2100:
        raise ValueError("Yil 1880 va 2100 oralig‘ida bo‘lishi kerak.")
    if not 0 <= rating <= 10:
        raise ValueError("Reyting 0 dan 10 gacha bo‘lishi kerak.")
    urls = [normalize_url(value) for value in lines[5:8]]
    if any(value is None and raw != "-" for value, raw in zip(urls, lines[5:8])):
        raise ValueError("URL qatorlarida http:// yoki https:// havolasidan foydalaning.")
    return {
        "title": lines[0],
        "year": year,
        "genre": lines[2],
        "rating": rating,
        "description": lines[4],
        "poster_url": urls[0],
        "trailer_url": urls[1],
        "watch_url": urls[2],
    }


async def admin_delete_movie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not is_admin(update):
        return
    context.user_data[STATE_KEY] = {"type": "admin_delete_movie"}
    await update.message.reply_text(
        "🗑 O‘chirish uchun kino ID raqamini yuboring.",
        reply_markup=ReplyKeyboardMarkup([[CANCEL_BUTTON]], resize_keyboard=True),
    )


async def admin_search_movies(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.message is None or not is_admin(update):
        return
    context.user_data[STATE_KEY] = {"type": "admin_search_movies"}
    await update.message.reply_text(
        "🔎 Kino nomi yoki janrini yozing:",
        reply_markup=ReplyKeyboardMarkup([[CANCEL_BUTTON]], resize_keyboard=True),
    )


async def admin_premium_users(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.message is None or not is_admin(update):
        return
    users = CinemaStore().list_premium_users()
    if not users:
        text = "⭐ Hozircha Premium foydalanuvchilar yo‘q."
    else:
        lines = ["⭐ Premium foydalanuvchilar:"]
        for user in users:
            username = f"@{user['username']}" if user["username"] else "username yo‘q"
            until = user["premium_until"] or "muddat ko‘rsatilmagan"
            lines.append(f"• {user['first_name']} — {username} — {until}")
        text = "\n".join(lines)
    await update.message.reply_text(text, reply_markup=admin_keyboard())


async def admin_channel_settings(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.message is None or not is_admin(update):
        return
    channel = configured_channel() or "kiritilmagan"
    username = os.getenv("CHANNEL_USERNAME", "").strip() or "kiritilmagan"
    join_url = os.getenv("CHANNEL_JOIN_URL", "").strip() or "avtomatik/yo‘q"
    await update.message.reply_text(
        "📢 Kanal sozlamalari:\n\n"
        f"CHANNEL_ID: {channel}\n"
        f"CHANNEL_USERNAME: {username}\n"
        f"CHANNEL_JOIN_URL: {join_url}\n\n"
        "O‘zgartirish uchun Replit Environment Variables’dan foydalaning.",
        reply_markup=admin_keyboard(),
    )


def format_order_lookup(order: dict[str, Any]) -> str:
    """Render the same customer and submission details used in order history."""
    telegram_line = ""
    if order.get("telegram_username"):
        telegram_line = f"\nTelegram: @{order['telegram_username']}"
    elif order.get("telegram_user_id"):
        telegram_line = f"\nTelegram ID: {order['telegram_user_id']}"

    price_line = ""
    if order.get("product_price"):
        price_line = f"\n💵 Narx: {order['product_price']}"

    return (
        "🧾 Buyurtma topildi:\n\n"
        f"🆔 ID: {order['order_id']}\n"
        f"👤 Mijoz: {order['customer_name']}\n"
        f"📞 Telefon: {order['phone']}\n"
        f"📦 Mahsulot: {order['product_name']} × {order['quantity']}"
        f"{price_line}\n"
        f"📌 Holat: {order['status']}\n"
        f"🕒 Vaqt: {order['submitted_at']}"
        f"{telegram_line}"
    )


async def lookup_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Let only the configured admin look up one persisted order."""
    if update.message is None:
        return
    if not is_admin(update):
        await update.message.reply_text("Bu buyruq faqat admin uchun.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Foydalanish: /order <buyurtma_ID>")
        return

    order_id = context.args[0].strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", order_id):
        await update.message.reply_text("Buyurtma ID formati noto‘g‘ri.")
        return

    try:
        order = OrderHistoryStore().find_by_id(order_id)
    except (OSError, sqlite3.Error):
        logger.error("Could not read one order by ID")
        await update.message.reply_text(
            "Buyurtmani qidirishda xatolik yuz berdi."
        )
        return

    if order is None:
        await update.message.reply_text("Bu ID bilan buyurtma topilmadi.")
        return
    await update.message.reply_text(format_order_lookup(order))


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    clear_state(context)
    await update.message.reply_text(
        "Asosiy menyu:", reply_markup=main_keyboard(update)
    )


async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    clear_state(context)
    await update.message.reply_text(
        "❌ Amal bekor qilindi.", reply_markup=main_keyboard(update)
    )


async def send_search_results(
    message: Any, results: list[dict[str, Any]], empty_text: str
) -> None:
    if not results:
        await message.reply_text(empty_text)
        return
    for movie in results:
        await send_movie(message, movie)


async def handle_stateful_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.message is None:
        return
    state = context.user_data.get(STATE_KEY)
    if not state:
        await update.message.reply_text(
            "Menyudan kerakli bo‘limni tanlang yoki /start buyrug‘ini yuboring.",
            reply_markup=main_keyboard(update),
        )
        return

    text = update.message.text.strip()
    state_type = state.get("type")
    store = CinemaStore()

    if state_type == "movie_search":
        clear_state(context)
        await send_search_results(
            update.message,
            store.search_movies(text),
            "Bu nom bo‘yicha kino topilmadi.",
        )
        await update.message.reply_text(
            "Yana qidirish uchun 🎬 Kino qidirish tugmasini bosing.",
            reply_markup=main_keyboard(update),
        )
        return

    if not is_admin(update):
        clear_state(context)
        await update.message.reply_text("Bu amal faqat admin uchun.")
        return

    if state_type == "admin_add_movie":
        try:
            movie = parse_movie_input(text)
            movie_id = store.add_movie(movie)
        except (ValueError, sqlite3.Error) as exc:
            error_text = str(exc) if isinstance(exc, ValueError) else "Kino saqlanmadi."
            await update.message.reply_text(error_text)
            return
        clear_state(context)
        await update.message.reply_text(
            f"✅ Kino qo‘shildi. ID: {movie_id}", reply_markup=admin_keyboard()
        )
        return

    if state_type == "admin_delete_movie":
        if not text.isdigit():
            await update.message.reply_text("Kino ID raqamini yuboring.")
            return
        deleted = store.delete_movie(int(text))
        clear_state(context)
        await update.message.reply_text(
            "✅ Kino o‘chirildi." if deleted else "Bu ID bilan kino topilmadi.",
            reply_markup=admin_keyboard(),
        )
        return

    if state_type == "admin_search_movies":
        clear_state(context)
        await send_search_results(
            update.message,
            store.search_movies(text),
            "Bu so‘rov bo‘yicha kino topilmadi.",
        )
        await update.message.reply_text("🔐 Admin paneli:", reply_markup=admin_keyboard())
        return

    clear_state(context)
    await update.message.reply_text(
        "Menyudan kerakli bo‘limni tanlang.",
        reply_markup=main_keyboard(update),
    )


def build_application() -> Application:
    """Create the application and register user/admin handlers."""
    application = Application.builder().token(get_bot_token()).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("order", lookup_order))
    application.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(SEARCH_BUTTON)}$"), start_movie_search)
    )
    application.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(SUBSCRIBE_BUTTON)}$"), show_subscription)
    )
    application.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(PREMIUM_BUTTON)}$"), show_premium)
    )
    application.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(CONTACT_BUTTON)}$"), contact_admin)
    )
    application.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(ADMIN_BUTTON)}$"), admin_panel)
    )
    application.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(SUBSCRIBED_BUTTON)}$"), show_subscription)
    )
    application.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(BACK_BUTTON)}$"), back_to_menu)
    )
    application.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(CANCEL_BUTTON)}$"), cancel_flow)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^👥 Foydalanuvchilar$"), admin_users)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^📊 Statistika$"), admin_statistics)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^🎬 Kino qo‘shish$"), admin_add_movie)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^🗑 Kino o‘chirish$"), admin_delete_movie)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^🔎 Kino qidirish$"), admin_search_movies)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^⭐ Premium foydalanuvchilar$"), admin_premium_users)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^📢 Kanal sozlamalari$"), admin_channel_settings)
    )
    application.add_handler(
        CallbackQueryHandler(check_subscription, pattern=r"^channel:check$")
    )
    application.add_handler(
        CallbackQueryHandler(premium_plan, pattern=r"^premium:\d+$")
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_stateful_text)
    )
    return application


def main() -> None:
    """Initialize storage and start Telegram polling."""
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.WARNING)
    CinemaStore()._connect().close()
    try:
        build_application().run_polling()
    except InvalidToken:
        logger.error(
            "Telegram rejected TELEGRAM_BOT_TOKEN. "
            "Update the secret with only the token from @BotFather."
        )


if __name__ == "__main__":
    main()