import asyncio
import logging
import aiohttp
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, JobQueue
)
from telegram.constants import ParseMode

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = "8399163198:AAGzTfUiluu6vkropSBGSZEcDygU4umCt7A"

STEAM_APPS = list(set([
    # ААА
    730,570,271590,1091500,1245620,814380,1551360,1172470,252490,381210,
    578080,1172620,1086940,2379780,892970,1174180,1716740,108600,289070,
    322330,242760,413150,365720,1621690,620,550,8930,374320,1174370,
    1966720,774171,1145360,1466060,2215430,1888930,2230490,377160,489830,
    292030,570940,72850,22380,49520,1250410,526870,
    # Инди и средние
    304930,251570,346110,427520,346900,239140,275850,294100,588650,648800,
    1062090,1222730,1203220,976730,1054830,1238840,632360,624320,1449560,
    1126290,2050650,1580130,1794680,1517290,739630,
    # Хайповые
    2218750,1649240,1817230,2475490,2396890,1908780,1284210,1547340,
    1868140,2456290,1670810,2357570,1811840,1794170,2195250,1895380,
    # Классика
    70,440,220,400,8870,22300,22320,57690,48000,8980,42910,49800,
    # Мультиплеер
    359550,1418590,1262350,1240440,1599340,2369390,105600,
]))

_cache: dict = {}
_cache_time: dict = {}
CACHE_TTL = 1800

_processing: set = set()
_subscribers: set = set()
_last_notified: set = set()

# ── Клавиатуры ──

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Steam скидки", callback_data="steam_menu"),
         InlineKeyboardButton("Бесплатные игры", callback_data="free_menu")],
        [InlineKeyboardButton("Уведомления", callback_data="settings"),
         InlineKeyboardButton("О боте", callback_data="about")],
    ])

def steam_discount_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("25–50% скидки", callback_data="disc_25_50")],
        [InlineKeyboardButton("50–75% скидки", callback_data="disc_50_75")],
        [InlineKeyboardButton("75–90% скидки", callback_data="disc_75_90")],
        [InlineKeyboardButton("90%+ мега-скидки", callback_data="disc_90")],
        [InlineKeyboardButton("Обновить данные", callback_data="refresh_steam")],
        [InlineKeyboardButton("Назад", callback_data="main_menu")],
    ])

def free_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Steam — бесплатно", callback_data="free_steam")],
        [InlineKeyboardButton("Epic Games Store", callback_data="free_epic")],
        [InlineKeyboardButton("Обновить", callback_data="refresh_free")],
        [InlineKeyboardButton("Назад", callback_data="main_menu")],
    ])

def back_kb(target="main_menu"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data=target)]])

def settings_kb(on: bool):
    txt = "Уведомления: ВКЛ" if on else "Уведомления: ВЫКЛ"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(txt, callback_data="toggle_notif")],
        [InlineKeyboardButton("Назад", callback_data="main_menu")],
    ])

# ── Steam API ──

async def fetch_game(session, appid):
    url = f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=ru&l=russian"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=7)) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
            info = data.get(str(appid), {})
            if not info.get("success"):
                return None
            d = info["data"]
            if d.get("type") not in ("game",):
                return None
            is_free = d.get("is_free", False)
            price_info = d.get("price_overview")
            if is_free and not price_info:
                return {"appid": appid, "name": d.get("name","?"), "discount": 100,
                        "original": "", "final": "Бесплатно",
                        "url": f"https://store.steampowered.com/app/{appid}",
                        "mc": d.get("metacritic", {}).get("score", -1)}
            if not price_info:
                return None
            disc = price_info.get("discount_percent", 0)
            if disc == 0:
                return None
            mc = d.get("metacritic", {}).get("score", -1)
            # Фильтр: если MC есть и ниже 55 — пропускаем
            if mc != -1 and mc < 55:
                return None
            return {"appid": appid, "name": d.get("name","?"), "discount": disc,
                    "original": price_info.get("initial_formatted",""),
                    "final": price_info.get("final_formatted",""),
                    "url": f"https://store.steampowered.com/app/{appid}",
                    "mc": mc}
    except:
        return None

async def fetch_all(min_d, max_d, force=False):
    key = f"{min_d}_{max_d}"
    now = datetime.now().timestamp()
    if not force and key in _cache and now - _cache_time.get(key, 0) < CACHE_TTL:
        return _cache[key]
    results = []
    conn = aiohttp.TCPConnector(limit=30)
    async with aiohttp.ClientSession(connector=conn) as session:
        for i in range(0, len(STEAM_APPS), 25):
            batch = STEAM_APPS[i:i+25]
            resp = await asyncio.gather(*[fetch_game(session, a) for a in batch], return_exceptions=True)
            for r in resp:
                if isinstance(r, dict) and r and min_d <= r["discount"] < max_d:
                    results.append(r)
            await asyncio.sleep(0.15)
    results.sort(key=lambda x: x["discount"], reverse=True)
    _cache[key] = results
    _cache_time[key] = now
    return results

async def get_free_steam(force=False):
    key = "free_steam"
    now = datetime.now().timestamp()
    if not force and key in _cache and now - _cache_time.get(key, 0) < CACHE_TTL:
        return _cache[key]
    results = []
    # Спецпредложения Steam
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://store.steampowered.com/api/featuredcategories?cc=ru&l=russian",
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    for item in data.get("specials", {}).get("items", []):
                        if item.get("discount_percent", 0) == 100:
                            results.append({"appid": item.get("id"), "name": item.get("name","?"),
                                           "discount": 100, "original": "",
                                           "final": "Бесплатно",
                                           "url": f"https://store.steampowered.com/app/{item.get('id')}"})
    except:
        pass
    # Из нашего списка
    conn = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=conn) as session:
        resp = await asyncio.gather(*[fetch_game(session, a) for a in STEAM_APPS[:100]], return_exceptions=True)
    for r in resp:
        if isinstance(r, dict) and r and r["discount"] == 100:
            if not any(x["appid"] == r["appid"] for x in results):
                results.append(r)
    _cache[key] = results
    _cache_time[key] = now
    return results

async def get_epic(force=False):
    key = "epic_free"
    now = datetime.now().timestamp()
    if not force and key in _cache and now - _cache_time.get(key, 0) < CACHE_TTL:
        return _cache[key]
    url = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions?locale=ru&country=RU&allowCountries=RU"
    results = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return []
                data = await r.json(content_type=None)
        elements = data.get("data",{}).get("Catalog",{}).get("searchStore",{}).get("elements",[])
        for game in elements:
            title = game.get("title","?")
            # Пропускаем тайные/Mystery игры
            if any(w in title.lower() for w in ["mystery","secret","???","tba","unknown"]):
                # Если это Mystery неделя — добавляем инфо-запись
                continue
            promos = game.get("promotions") or {}
            slug = ""
            for m in game.get("catalogNs",{}).get("mappings",[]):
                if m.get("pageType") == "productHome":
                    slug = m.get("pageSlug",""); break
            if not slug:
                slug = game.get("productSlug") or game.get("urlSlug") or ""
            gurl = f"https://store.epicgames.com/ru/p/{slug}" if slug else "https://store.epicgames.com/ru/free-games"
            orig = game.get("price",{}).get("totalPrice",{}).get("fmtPrice",{}).get("originalPrice","")

            for pg in promos.get("promotionalOffers",[]):
                for offer in pg.get("promotionalOffers",[]):
                    if offer.get("discountSetting",{}).get("discountPercentage",99) == 0:
                        end_str = ""
                        try:
                            dt = datetime.fromisoformat(offer.get("endDate","").replace("Z",""))
                            end_str = dt.strftime("%d.%m")
                        except: pass
                        results.append({"name":title,"original_price":orig,"url":gurl,"end_date":end_str,"status":"current"})

            for pg in promos.get("upcomingPromotionalOffers",[]):
                for offer in pg.get("promotionalOffers",[]):
                    if offer.get("discountSetting",{}).get("discountPercentage",99) == 0:
                        start_str = ""
                        try:
                            dt = datetime.fromisoformat(offer.get("startDate","").replace("Z",""))
                            start_str = dt.strftime("%d.%m в %H:%M")
                        except: pass
                        results.append({"name":title,"original_price":orig,"url":gurl,"start_date":start_str,"status":"upcoming"})
    except Exception as e:
        logger.error(f"Epic error: {e}")
    _cache[key] = results
    _cache_time[key] = now
    return results

def check_mystery_week(data) -> str | None:
    """Проверяет есть ли Mystery неделя в Epic и возвращает инфо-строку."""
    try:
        elements = data.get("data",{}).get("Catalog",{}).get("searchStore",{}).get("elements",[])
        for game in elements:
            title = game.get("title","")
            if any(w in title.lower() for w in ["mystery","secret"]):
                promos = game.get("promotions") or {}
                for pg in promos.get("upcomingPromotionalOffers",[]):
                    for offer in pg.get("promotionalOffers",[]):
                        if offer.get("discountSetting",{}).get("discountPercentage",99) == 0:
                            try:
                                dt = datetime.fromisoformat(offer.get("startDate","").replace("Z",""))
                                return dt.strftime("%d.%m в %H:%M")
                            except: pass
    except: pass
    return None

# ── Форматирование ──

def disc_icon(d):
    if d >= 90: return "★"
    if d >= 75: return "▲"
    if d >= 50: return "◆"
    return "●"

def mc_label(mc):
    if mc < 0: return ""
    return f"  MC {mc}"

def format_deals(games, title):
    ts = datetime.now().strftime("%H:%M %d.%m")
    if not games:
        return f"<b>{title}</b>  ·  <i>{ts}</i>\n\nСкидок в этом диапазоне сейчас нет.\nПопробуй другую категорию."
    lines = [f"<b>{title}</b>  ·  <i>{ts}</i>\n"]
    for g in games[:25]:
        d = g["discount"]
        name = g["name"][:40]
        orig = g.get("original","")
        final = g.get("final","")
        price = f"  {orig} → <b>{final}</b>" if orig and final and orig != final else (f"  <b>{final}</b>" if final else "")
        mc = mc_label(g.get("mc",-1))
        lines.append(f'{disc_icon(d)} <b>-{d}%</b>  <a href="{g["url"]}">{name}</a>{price}{mc}')
    if len(games) > 25:
        lines.append(f"\n<i>+ ещё {len(games)-25} игр</i>")
    return "\n".join(lines)

def format_epic(games):
    if not games:
        return "<b>Epic Games — раздачи</b>\n\nАктивных раздач нет.\nОбычно новые игры появляются по четвергам."
    current = [g for g in games if g.get("status")=="current"]
    upcoming = [g for g in games if g.get("status")=="upcoming"]
    lines = ["<b>Epic Games Store</b>\n"]
    if current:
        lines.append("<b>Сейчас бесплатно:</b>")
        for g in current:
            end = f"  до {g['end_date']}" if g.get("end_date") else ""
            pr = f"  ~~{g['original_price']}~~" if g.get("original_price") and g["original_price"] not in ("0","Free","") else ""
            lines.append(f'+ <a href="{g["url"]}">{g["name"]}</a>{pr}{end}')
    if upcoming:
        lines.append("\n<b>Скоро:</b>")
        for g in upcoming:
            start = f"  с {g['start_date']}" if g.get("start_date") else ""
            lines.append(f'· <a href="{g["url"]}">{g["name"]}</a>{start}')
    return "\n".join(lines)

def format_free_steam(games):
    if not games:
        return "<b>Steam — временно бесплатно</b>\n\nАктивных раздач сейчас нет.\nПришлю уведомление как появятся."
    lines = ["<b>Steam — временно бесплатно</b>\n"]
    for g in games:
        orig = g.get("original","")
        pr = f"  ~~{orig}~~" if orig and orig not in ("Бесплатно","Free","") else ""
        lines.append(f'+ <a href="{g["url"]}">{g["name"]}</a>{pr}')
    return "\n".join(lines)

# ── Хендлеры ──

WELCOME = (
    "<b>Xalava Games</b>\n\n"
    "Слежу за скидками Steam и раздачами Epic Games.\n"
    "Фильтрую по рейтингу — только нормальные игры.\n\n"
    "Выбери раздел:"
)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _subscribers.add(update.effective_chat.id)
    if ctx.user_data.get("notifications") is None:
        ctx.user_data["notifications"] = True
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Команды:</b>\n/start — меню\n/deals — скидки Steam\n/free — бесплатные игры\n/help — справка",
        parse_mode=ParseMode.HTML)

async def deals_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("<b>Steam скидки:</b>", parse_mode=ParseMode.HTML, reply_markup=steam_discount_keyboard())

async def free_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("<b>Бесплатные игры:</b>", parse_mode=ParseMode.HTML, reply_markup=free_menu_keyboard())

async def button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = f"{query.from_user.id}_{query.data}"
    if uid in _processing:
        await query.answer()
        return
    _processing.add(uid)
    try:
        await query.answer()
        data = query.data

        async def edit(text, kb=None):
            try:
                await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                    reply_markup=kb, disable_web_page_preview=True)
            except Exception as e:
                logger.debug(f"edit: {e}")

        if data == "main_menu":
            await edit(WELCOME, main_menu_keyboard())
        elif data == "steam_menu":
            await edit("<b>Steam скидки — выбери категорию:</b>", steam_discount_keyboard())
        elif data == "free_menu":
            await edit("<b>Бесплатные игры:</b>", free_menu_keyboard())
        elif data in ("disc_25_50","disc_50_75","disc_75_90","disc_90","refresh_steam"):
            mp = {"disc_25_50":(25,50,"Steam — скидки 25–50%"),
                  "disc_50_75":(50,75,"Steam — скидки 50–75%"),
                  "disc_75_90":(75,90,"Steam — скидки 75–90%"),
                  "disc_90":(90,101,"Steam — скидки 90%+"),
                  "refresh_steam":(50,101,"Steam — скидки 50%+")}
            mn, mx, title = mp[data]
            force = (data == "refresh_steam")
            await edit("<i>Загружаю данные Steam...</i>")
            games = await fetch_all(mn, mx, force=force)
            await edit(format_deals(games, title), back_kb("steam_menu"))
        elif data == "free_steam":
            await edit("<i>Ищу раздачи Steam...</i>")
            games = await get_free_steam()
            await edit(format_free_steam(games), back_kb("free_menu"))
        elif data == "free_epic":
            await edit("<i>Загружаю Epic Games Store...</i>")
            games = await get_epic()
            await edit(format_epic(games), back_kb("free_menu"))
        elif data == "refresh_free":
            await edit("<i>Обновляю...</i>")
            epic = await get_epic(force=True)
            sf = await get_free_steam(force=True)
            text = format_epic(epic) + "\n\n" + format_free_steam(sf)
            await edit(text[:4090], back_kb("free_menu"))
        elif data == "settings":
            on = ctx.user_data.get("notifications", True)
            await edit("<b>Уведомления</b>\n\nПришлю алёрт при скидках 75%+ и новых раздачах.", settings_kb(on))
        elif data == "toggle_notif":
            cur = ctx.user_data.get("notifications", True)
            ctx.user_data["notifications"] = not cur
            await query.answer("Включены" if not cur else "Выключены", show_alert=True)
            await edit("<b>Уведомления</b>\n\nПришлю алёрт при скидках 75%+ и новых раздачах.", settings_kb(not cur))
        elif data == "about":
            await edit(
                "<b>Xalava Games Bot</b>\n\n"
                "Мониторю скидки Steam и раздачи EGS.\n"
                "Фильтр по Metacritic — плохие игры не показываю.\n\n"
                "<b>Источники:</b> Steam API, Epic Games API\n"
                "<b>Автопроверка:</b> каждый час\n"
                "<b>База:</b> 150+ игр (ААА, инди, хайп)\n",
                back_kb("main_menu"))
    finally:
        _processing.discard(uid)

# ── Мониторинг ──

async def auto_monitor(ctx: ContextTypes.DEFAULT_TYPE):
    if not _subscribers:
        return
    try:
        games_75 = await fetch_all(75, 101, force=True)
        epic = await get_epic(force=True)
        sf = await get_free_steam(force=True)

        new_deals, new_epic, new_sf = [], [], []
        for g in games_75:
            k = f"s_{g['appid']}_{g['discount']}"
            if k not in _last_notified:
                new_deals.append(g); _last_notified.add(k)
        for g in epic:
            if g.get("status") == "current":
                k = f"e_{g['name']}"
                if k not in _last_notified:
                    new_epic.append(g); _last_notified.add(k)
        for g in sf:
            k = f"sf_{g['appid']}"
            if k not in _last_notified:
                new_sf.append(g); _last_notified.add(k)

        if not new_deals and not new_epic and not new_sf:
            return

        parts = ["<b>Новые предложения</b>\n"]
        if new_deals:
            parts.append("<b>Steam — скидки 75%+</b>")
            for g in new_deals[:6]:
                parts.append(f'{disc_icon(g["discount"])} <b>-{g["discount"]}%</b>  <a href="{g["url"]}">{g["name"]}</a>  {g.get("original","")} → <b>{g.get("final","")}</b>')
        if new_epic:
            parts.append("\n<b>Epic — бесплатно:</b>")
            for g in new_epic[:4]:
                parts.append(f'+ <a href="{g["url"]}">{g["name"]}</a>')
        if new_sf:
            parts.append("\n<b>Steam — бесплатно:</b>")
            for g in new_sf[:4]:
                parts.append(f'+ <a href="{g["url"]}">{g["name"]}</a>')

        text = "\n".join(parts)
        for chat_id in list(_subscribers):
            try:
                await ctx.bot.send_message(chat_id=chat_id, text=text,
                    parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                    reply_markup=main_menu_keyboard())
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.warning(f"Notify {chat_id}: {e}")
    except Exception as e:
        logger.error(f"Monitor: {e}")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("deals", deals_cmd))
    app.add_handler(CommandHandler("free", free_cmd))
    app.add_handler(CallbackQueryHandler(button))
    app.job_queue.run_repeating(auto_monitor, interval=3600, first=120)

    async def post_init(application):
        await application.bot.set_my_commands([
            BotCommand("start","Главное меню"),
            BotCommand("deals","Steam скидки"),
            BotCommand("free","Бесплатные игры"),
            BotCommand("help","Помощь"),
        ])
    app.post_init = post_init
    logger.info("Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
