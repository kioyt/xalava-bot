import asyncio
import logging
import json
import aiohttp
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, JobQueue
)
from telegram.constants import ParseMode

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = "8399163198:AAGzTfUiluu6vkropSBGSZEcDygU4umCt7A"

# Популярные Steam AppID'ы для мониторинга
POPULAR_STEAM_APPS = [
    730,    # CS2
    570,    # Dota 2
    271590, # GTA V
    1091500,# Cyberpunk 2077
    1245620,# Elden Ring
    814380, # Sekiro
    1551360,# Forza Horizon 5
    1172470,# Apex Legends
    252490, # Rust
    381210, # Dead by Daylight
    578080, # PUBG
    1172620,# Sea of Thieves
    1086940,# Baldur's Gate 3
    2379780,# Hogwarts Legacy
    892970, # Valheim
    1203220,# NARAKA: BLADEPOINT
    1174180,# Red Dead Redemption 2
    1716740,# HITMAN World of Assassination
    1250410,# Satisfactory
    108600, # Project Zomboid
    289070, # Civ VI
    49520,  # Borderlands 2
    322330, # Don't Starve Together
    242760, # The Forest
    413150, # Stardew Valley
    365720, # Subnautica
    1621690,# Subnautica: Below Zero
    620,    # Portal 2
    70,     # Half-Life
    440,    # Team Fortress 2
    550,    # Left 4 Dead 2
    8930,   # Sid Meier's Civilization V
    374320, # Dark Souls III
    1517290,# Battlefield 2042
    1938090,# Call of Duty
    1174370,# DayZ
    1966720,# Lethal Company
    2379780,# Hogwarts Legacy
    526870, # Satisfactory
    774171, # Slay the Spire
    1145360,# Hades
    1466060,# Hades II
    2215430,# Palworld
    1888930,# The Last of Us
    2230490,# Spider-Man
    990080, # Hogwarts Legacy
    1517290,# BF2042
    377160, # Fallout 4
    489830, # Skyrim SE
    292030, # The Witcher 3
    570940, # Dark Souls Remastered
    72850,  # Skyrim
    22380,  # Fallout: NV
]

POPULAR_STEAM_APPS = list(set(POPULAR_STEAM_APPS))  # dedupe

# Эмодзи для категорий скидок
DISCOUNT_EMOJI = {
    "25-50": "🟡",
    "50-75": "🟠",
    "75-90": "🔴",
    "90+":   "💥",
    "free":  "🎁",
}

# ─── Клавиатуры ────────────────────────────────────────────────────────────────

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎮 Steam скидки", callback_data="steam_menu"),
            InlineKeyboardButton("🎁 Бесплатные игры", callback_data="free_menu"),
        ],
        [
            InlineKeyboardButton("⚙️ Настройки уведомлений", callback_data="settings"),
            InlineKeyboardButton("ℹ️ О боте", callback_data="about"),
        ],
    ])

def steam_discount_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟡 25–50% скидки",  callback_data="disc_25_50")],
        [InlineKeyboardButton("🟠 50–75% скидки",  callback_data="disc_50_75")],
        [InlineKeyboardButton("🔴 75–90% скидки",  callback_data="disc_75_90")],
        [InlineKeyboardButton("💥 90%+ мега-скидки", callback_data="disc_90")],
        [InlineKeyboardButton("🔄 Обновить всё",   callback_data="refresh_steam")],
        [InlineKeyboardButton("◀️ Назад",           callback_data="main_menu")],
    ])

def free_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Steam — бесплатно",  callback_data="free_steam")],
        [InlineKeyboardButton("🟣 Epic Games Store",   callback_data="free_epic")],
        [InlineKeyboardButton("🔄 Обновить",            callback_data="refresh_free")],
        [InlineKeyboardButton("◀️ Назад",               callback_data="main_menu")],
    ])

def back_keyboard(target="main_menu"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=target)]])

def settings_keyboard(ctx_data: dict):
    notif_on = ctx_data.get("notifications", True)
    notif_text = "🔔 Уведомления: ВКЛ" if notif_on else "🔕 Уведомления: ВЫКЛ"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(notif_text, callback_data="toggle_notif")],
        [InlineKeyboardButton("◀️ Назад", callback_data="main_menu")],
    ])

# ─── Steam API ──────────────────────────────────────────────────────────────────

async def fetch_steam_game(session: aiohttp.ClientSession, appid: int) -> dict | None:
    url = f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=us&l=russian"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return None
            data = await r.json()
            info = data.get(str(appid), {})
            if not info.get("success"):
                return None
            d = info["data"]
            if d.get("type") != "game":
                return None
            price_info = d.get("price_overview")
            if not price_info:
                return None
            discount = price_info.get("discount_percent", 0)
            if discount == 0:
                return None
            return {
                "appid": appid,
                "name": d.get("name", "Unknown"),
                "discount": discount,
                "original": price_info.get("initial_formatted", ""),
                "final": price_info.get("final_formatted", ""),
                "url": f"https://store.steampowered.com/app/{appid}",
                "header_image": d.get("header_image", ""),
            }
    except Exception as e:
        logger.debug(f"Error fetching {appid}: {e}")
        return None

async def get_steam_deals(min_disc: int, max_disc: int) -> list[dict]:
    """Возвращает игры с скидкой в диапазоне [min_disc, max_disc)."""
    results = []
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_steam_game(session, appid) for appid in POPULAR_STEAM_APPS]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
    for r in responses:
        if isinstance(r, dict) and r:
            disc = r["discount"]
            if min_disc <= disc < max_disc:
                results.append(r)
    results.sort(key=lambda x: x["discount"], reverse=True)
    return results

async def get_free_steam_games() -> list[dict]:
    """Игры с 100% скидкой в Steam."""
    results = []
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_steam_game(session, appid) for appid in POPULAR_STEAM_APPS]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
    for r in responses:
        if isinstance(r, dict) and r and r["discount"] == 100:
            results.append(r)
    return results

async def get_epic_free_games() -> list[dict]:
    """Бесплатные игры из Epic Games Store через их публичный API."""
    url = (
        "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
        "?locale=ru&country=RU&allowCountries=RU"
    )
    results = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return []
                data = await r.json()
        elements = (
            data.get("data", {})
                .get("Catalog", {})
                .get("searchStore", {})
                .get("elements", [])
        )
        for game in elements:
            promos = game.get("promotions") or {}
            offers = promos.get("promotionalOffers", [])
            upcoming = promos.get("upcomingPromotionalOffers", [])
            
            # Текущие бесплатные
            for promo in offers:
                for offer in promo.get("promotionalOffers", []):
                    if offer.get("discountSetting", {}).get("discountPercentage", 99) == 0:
                        price = game.get("price", {}).get("totalPrice", {})
                        original = price.get("fmtPrice", {}).get("originalPrice", "")
                        slug = ""
                        for mapping in game.get("catalogNs", {}).get("mappings", []):
                            if mapping.get("pageType") == "productHome":
                                slug = mapping.get("pageSlug", "")
                                break
                        if not slug:
                            slug = game.get("productSlug", "") or game.get("urlSlug", "")
                        results.append({
                            "name": game.get("title", "Unknown"),
                            "original_price": original,
                            "url": f"https://store.epicgames.com/ru/p/{slug}" if slug else "https://store.epicgames.com/ru/free-games",
                            "end_date": offer.get("endDate", ""),
                            "status": "current",
                        })
            
            # Скоро бесплатные
            for promo in upcoming:
                for offer in promo.get("promotionalOffers", []):
                    if offer.get("discountSetting", {}).get("discountPercentage", 99) == 0:
                        slug = ""
                        for mapping in game.get("catalogNs", {}).get("mappings", []):
                            if mapping.get("pageType") == "productHome":
                                slug = mapping.get("pageSlug", "")
                                break
                        if not slug:
                            slug = game.get("productSlug", "") or game.get("urlSlug", "")
                        results.append({
                            "name": game.get("title", "Unknown"),
                            "original_price": "",
                            "url": f"https://store.epicgames.com/ru/p/{slug}" if slug else "https://store.epicgames.com/ru/free-games",
                            "start_date": offer.get("startDate", ""),
                            "end_date": offer.get("endDate", ""),
                            "status": "upcoming",
                        })
    except Exception as e:
        logger.error(f"Epic API error: {e}")
    return results

# ─── Форматирование ─────────────────────────────────────────────────────────────

def format_steam_deals(games: list[dict], emoji: str, title: str) -> str:
    if not games:
        return f"{emoji} <b>{title}</b>\n\n😔 Сейчас нет подходящих скидок.\nПроверьте позже!"
    
    lines = [f"{emoji} <b>{title}</b>\n<i>Обновлено: {datetime.now().strftime('%H:%M %d.%m')}</i>\n"]
    for g in games[:20]:  # max 20 игр
        disc = g['discount']
        name = g['name'][:35]
        original = g.get('original', '')
        final = g.get('final', '')
        price_str = f" {original} → <b>{final}</b>" if original and final else ""
        lines.append(
            f"{'🔥' if disc >= 80 else '🎮'} <b>-{disc}%</b> <a href=\"{g['url']}\">{name}</a>{price_str}"
        )
    
    if len(games) > 20:
        lines.append(f"\n<i>...и ещё {len(games)-20} игр</i>")
    return "\n".join(lines)

def format_epic_games(games: list[dict]) -> str:
    if not games:
        return "🟣 <b>Epic Games — бесплатно</b>\n\n😔 Сейчас нет бесплатных раздач.\nСледите за обновлениями!"
    
    lines = ["🟣 <b>Epic Games — бесплатные раздачи</b>\n"]
    current = [g for g in games if g.get("status") == "current"]
    upcoming = [g for g in games if g.get("status") == "upcoming"]
    
    if current:
        lines.append("✅ <b>Сейчас бесплатно:</b>")
        for g in current:
            end = ""
            if g.get("end_date"):
                try:
                    dt = datetime.fromisoformat(g["end_date"].replace("Z",""))
                    end = f" (до {dt.strftime('%d.%m')})"
                except:
                    pass
            price = f" ~~{g['original_price']}~~" if g.get("original_price") else ""
            lines.append(f"🎁 <a href=\"{g['url']}\">{g['name']}</a>{price}{end}")
    
    if upcoming:
        lines.append("\n⏳ <b>Скоро бесплатно:</b>")
        for g in upcoming:
            start = ""
            if g.get("start_date"):
                try:
                    dt = datetime.fromisoformat(g["start_date"].replace("Z",""))
                    start = f" (с {dt.strftime('%d.%m')})"
                except:
                    pass
            lines.append(f"🔜 <a href=\"{g['url']}\">{g['name']}</a>{start}")
    
    return "\n".join(lines)

def format_free_steam(games: list[dict]) -> str:
    if not games:
        return "🟢 <b>Steam — бесплатные игры</b>\n\n😔 Сейчас нет бесплатных раздач из отслеживаемого списка."
    lines = ["🟢 <b>Steam — бесплатно (100% скидка):</b>\n"]
    for g in games:
        lines.append(f"🎁 <a href=\"{g['url']}\">{g['name']}</a> ~~{g.get('original','')}~~")
    return "\n".join(lines)

# ─── Хендлеры ──────────────────────────────────────────────────────────────────

WELCOME = (
    "👋 <b>Добро пожаловать в Xalava Games Bot!</b>\n\n"
    "🎮 Мониторю скидки на Steam и бесплатные раздачи в EGS и Steam.\n\n"
    "Выбери раздел:"
)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("notifications_set"):
        ctx.user_data["notifications"] = True
    await update.message.reply_text(
        WELCOME,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "ℹ️ <b>Команды бота:</b>\n\n"
        "/start — главное меню\n"
        "/deals — скидки Steam\n"
        "/free — бесплатные игры\n"
        "/help — эта справка\n\n"
        "Бот автоматически проверяет скидки каждые <b>2 часа</b> и присылает уведомления "
        "о новых крупных скидках и бесплатных раздачах."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def deals_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎮 <b>Steam скидки — выбери категорию:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=steam_discount_keyboard(),
    )

async def free_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎁 <b>Бесплатные игры — выбери источник:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=free_menu_keyboard(),
    )

async def button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    async def edit(text, kb=None):
        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.debug(f"edit_message_text error: {e}")

    if data == "main_menu":
        await edit(WELCOME, main_menu_keyboard())

    elif data == "steam_menu":
        await edit("🎮 <b>Steam скидки — выбери категорию:</b>", steam_discount_keyboard())

    elif data == "free_menu":
        await edit("🎁 <b>Бесплатные игры — выбери источник:</b>", free_menu_keyboard())

    elif data in ("disc_25_50", "disc_50_75", "disc_75_90", "disc_90", "refresh_steam"):
        mapping = {
            "disc_25_50": (25, 50,  "🟡 Скидки 25–50%"),
            "disc_50_75": (50, 75,  "🟠 Скидки 50–75%"),
            "disc_75_90": (75, 90,  "🔴 Скидки 75–90%"),
            "disc_90":    (90, 101, "💥 Скидки 90%+"),
            "refresh_steam": (50, 101, "🔄 Скидки 50%+"),
        }
        mn, mx, title = mapping[data]
        await edit(f"⏳ <b>Загружаю данные из Steam...</b>\n<i>Это займёт ~10 секунд</i>")
        games = await get_steam_deals(mn, mx)
        kb = back_keyboard("steam_menu")
        await edit(format_steam_deals(games, title.split()[0], title), kb)

    elif data == "free_steam":
        await edit("⏳ <b>Ищу бесплатные игры в Steam...</b>")
        games = await get_free_steam_games()
        await edit(format_free_steam(games), back_keyboard("free_menu"))

    elif data == "free_epic":
        await edit("⏳ <b>Загружаю раздачи Epic Games Store...</b>")
        games = await get_epic_free_games()
        await edit(format_epic_games(games), back_keyboard("free_menu"))

    elif data in ("refresh_free",):
        await edit("⏳ <b>Обновляю данные...</b>")
        epic = await get_epic_free_games()
        steam_free = await get_free_steam_games()
        text = format_epic_games(epic) + "\n\n" + format_free_steam(steam_free)
        await edit(text, back_keyboard("free_menu"))

    elif data == "settings":
        await edit(
            "⚙️ <b>Настройки уведомлений</b>\n\n"
            "Включи уведомления, чтобы получать алёрты о:\n"
            "• Новых скидках 75%+ на популярные игры\n"
            "• Бесплатных раздачах Steam и Epic\n",
            settings_keyboard(ctx.user_data),
        )

    elif data == "toggle_notif":
        cur = ctx.user_data.get("notifications", True)
        ctx.user_data["notifications"] = not cur
        ctx.user_data["notifications_set"] = True
        status = "включены 🔔" if not cur else "выключены 🔕"
        await query.answer(f"Уведомления {status}", show_alert=True)
        await edit(
            "⚙️ <b>Настройки уведомлений</b>\n\n"
            "Включи уведомления, чтобы получать алёрты о:\n"
            "• Новых скидках 75%+ на популярные игры\n"
            "• Бесплатных раздачах Steam и Epic\n",
            settings_keyboard(ctx.user_data),
        )

    elif data == "about":
        await edit(
            "ℹ️ <b>Xalava Games Bot</b>\n\n"
            "🤖 Мониторю скидки в Steam и бесплатные игры в EGS/Steam.\n\n"
            "📊 <b>Источники данных:</b>\n"
            "• Steam Store API (официальный)\n"
            "• Epic Games Store API (официальный)\n\n"
            "🔄 <b>Автообновление:</b> каждые 2 часа\n"
            "📬 <b>Уведомления:</b> при появлении новых скидок 75%+ и раздач\n\n"
            "👨‍💻 Бот создан специально для @xalava_games_bot",
            back_keyboard("main_menu"),
        )

# ─── Авто-мониторинг ────────────────────────────────────────────────────────────

# Хранилище последних найденных скидок, чтобы не дублировать уведомления
_last_notified: set[str] = set()
_subscribers: set[int] = set()  # chat_id'ы подписчиков

async def auto_monitor(ctx: ContextTypes.DEFAULT_TYPE):
    """Запускается каждые 2 часа. Проверяет скидки и раздачи."""
    global _last_notified, _subscribers
    
    if not _subscribers:
        return
    
    try:
        # Скидки 75%+
        games_75 = await get_steam_deals(75, 101)
        epic = await get_epic_free_games()
        steam_free = await get_free_steam_games()
        
        new_deals = []
        for g in games_75:
            key = f"steam_{g['appid']}_{g['discount']}"
            if key not in _last_notified:
                new_deals.append(g)
                _last_notified.add(key)
        
        new_free = []
        for g in epic:
            key = f"epic_{g['name']}_{g.get('status')}"
            if key not in _last_notified:
                new_free.append(g)
                _last_notified.add(key)
        for g in steam_free:
            key = f"steamfree_{g['appid']}"
            if key not in _last_notified:
                new_free.append(g)
                _last_notified.add(key)
        
        if not new_deals and not new_free:
            return
        
        # Шлём уведомление всем подписчикам
        msg_parts = ["🔔 <b>Новые игровые предложения!</b>\n"]
        
        if new_deals:
            msg_parts.append("🎮 <b>Скидки 75%+ в Steam:</b>")
            for g in new_deals[:8]:
                msg_parts.append(
                    f"🔴 <b>-{g['discount']}%</b> <a href=\"{g['url']}\">{g['name']}</a> "
                    f"{g.get('original','')} → <b>{g.get('final','')}</b>"
                )
        
        if new_free:
            msg_parts.append("\n🎁 <b>Новые бесплатные раздачи:</b>")
            for g in new_free[:5]:
                url = g.get('url', '')
                name = g.get('name', '')
                msg_parts.append(f"🆓 <a href=\"{url}\">{name}</a>")
        
        text = "\n".join(msg_parts)
        
        for chat_id in list(_subscribers):
            try:
                await ctx.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=main_menu_keyboard(),
                )
            except Exception as e:
                logger.warning(f"Could not send to {chat_id}: {e}")
    
    except Exception as e:
        logger.error(f"Auto-monitor error: {e}")

# Регистрируем подписчиков при /start
async def start_register(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _subscribers.add(update.effective_chat.id)
    await start(update, ctx)

# ─── Запуск ─────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_register))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("deals", deals_cmd))
    app.add_handler(CommandHandler("free", free_cmd))
    app.add_handler(CallbackQueryHandler(button))

    # Авто-мониторинг каждые 2 часа
    job_queue: JobQueue = app.job_queue
    job_queue.run_repeating(auto_monitor, interval=7200, first=60)

    # Команды в меню бота
    async def post_init(application):
        await application.bot.set_my_commands([
            BotCommand("start", "🏠 Главное меню"),
            BotCommand("deals", "🎮 Steam скидки"),
            BotCommand("free", "🎁 Бесплатные игры"),
            BotCommand("help", "ℹ️ Помощь"),
        ])
    app.post_init = post_init

    logger.info("Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
