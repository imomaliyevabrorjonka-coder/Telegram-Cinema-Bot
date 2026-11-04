# Uzbek Cinema Telegram Bot

An Uzbek-language cinema bot built with `python-telegram-bot`. It searches a
local movie database, shows legal trailer/watch links, checks channel
membership, provides a payment placeholder for Premium plans, and includes a
protected administrator panel.

## Features

- `/start` welcome message and dynamic main menu
- 🎬 Local movie search with title, year, genre, rating, description, poster,
  trailer URL, and optional legal watch URL
- 📢 Channel join link and Telegram membership verification
- ⭐ Premium plans for 1, 3, 6, and 12 months
- Payment placeholder that never grants Premium before payment confirmation
- 📞 Configurable admin contact link
- 🔐 Admin-only panel with users, statistics, movie add/delete/search, Premium
  users, and channel settings
- Admin order lookup with `/order <buyurtma_ID>` for persisted historical shop
  orders
- SQLite persistence for users, movies, and Premium fields
- No pirated downloads, copyright bypasses, or illegal streaming links

## Configuration

### Replit Secrets

- `TELEGRAM_BOT_TOKEN` — the token from @BotFather. Keep it in Replit Secrets.

### Environment variables

- `ADMIN_ID` — numeric Telegram user ID allowed to use the admin panel
- `ADMIN_CONTACT` — public admin username or URL, for example
  `https://t.me/shokhwins`
- `CHANNEL_ID` — channel username or numeric Telegram channel ID used for
  membership checks
- `CHANNEL_USERNAME` — public channel username, used to build the join link
- `CHANNEL_JOIN_URL` — optional invite or public join URL
- `CINEMA_DB_PATH` — optional SQLite path; defaults to
  `telegram-bot/cinema.sqlite3`

The bot never logs the token or credential-bearing HTTP URLs. If a BotFather
message was pasted into the secret, startup extracts only the token pattern.

## Admin movie management

Set `ADMIN_ID`, send `/start` from that Telegram account, and open `🔐 ADMIN`.
To add a movie, send exactly eight lines:

1. Title
2. Release year
3. Genre
4. Rating from 0 to 10
5. Short description
6. Poster URL or `-`
7. Trailer URL or `-`
8. Legal watch URL or `-`

The admin can delete a movie by its database ID and search the catalog from
the same panel.

Historical shop orders can be looked up directly with:

```text
/order order-123
```

Only the configured admin can use this command. Unknown IDs return a generic
not-found response, and the order ID is validated and queried as a parameter.

## Run

From the project root:

```bash
.pythonlibs/bin/python telegram-bot/bot.py
```

The Telegram Bot workflow uses the same command.