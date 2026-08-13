import os
import re
import json
import logging
import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters
)

# Logging Yapılandırması
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ================= ORTAM DEĞİŞKENLERİ =================
TOKEN = os.environ.get("TELEGRAMTOKEN")
CHAT_ID_ENV = os.environ.get("CHATID")
SCRAPER_KEY = os.environ.get("SCRAPER_KEY")
NPOINT_ID = os.environ.get("NPOINT_ID")

ALLOWED_CHAT_ID = int(CHAT_ID_ENV) if CHAT_ID_ENV and CHAT_ID_ENV.isdigit() else None

# ================= BULUT VERİ TABANI YÖNETİMİ (NPOINT) =================

def load_data() -> dict:
    if not NPOINT_ID:
        return {}
    try:
        res = requests.get(f"https://api.npoint.io/{NPOINT_ID}", timeout=10)
        if res.status_code == 200:
            data = res.json()
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}

def save_data(data: dict):
    if not NPOINT_ID:
        return
    try:
        requests.post(f"https://api.npoint.io/{NPOINT_ID}", json=data, timeout=10)
    except Exception:
        pass

tracked_products = load_data()

# ================= YARDIMCI FONKSİYONLAR =================

def resolve_url(url: str) -> str:
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"})
        res = s.get(url, allow_redirects=True, timeout=12)
        return res.url
    except:
        return url

def parse_price(price_str: str) -> float:
    if not price_str:
        return 0.0
    try:
        clean = re.sub(r"[^\d.,]", "", str(price_str)).strip()
        if not clean: return 0.0
        if "." in clean and "," in clean: clean = clean.replace(".", "").replace(",", ".")
        elif "," in clean: clean = clean.replace(",", ".")
        elif "." in clean:
            parts = clean.split(".")
            if len(parts[-1]) == 3: clean = clean.replace(".", "")
        return float(clean)
    except:
        return 0.0

def scrape_amazon(raw_url: str):
    try:
        real_url = resolve_url(raw_url)
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}

        res = None
        if SCRAPER_KEY:
            try:
                target_url = f"http://api.scraperapi.com?api_key={SCRAPER_KEY}&url={requests.utils.quote(real_url)}&country_code=tr&render=true"
                res = requests.get(target_url, timeout=35)
            except: pass

        if not res or res.status_code != 200 or "productTitle" not in res.text:
            session = requests.Session()
            session.cookies.set("i18n-prefs", "TRY", domain=".amazon.com.tr")
            res = session.get(real_url, headers=headers, timeout=15)

        if not res or res.status_code != 200:
            return None

        soup = BeautifulSoup(res.text, "html.parser")
        
        # 1. Başlık
        title_el = soup.find("span", {"id": "productTitle"}) or soup.find("h1", {"id": "title"})
        if not title_el: return None
        title = title_el.get_text(strip=True)

        # 2. Fiyat (Sıfır + İkinci El)
        found_prices = []
        price_selectors = [
            "#corePrice_feature_div .a-price .a-offscreen",
            "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
            "#apex_desktop .a-price .a-offscreen",
            "#usedAccordion .a-price .a-offscreen",
            "#moreBuyingChoices_feature_div .a-price .a-offscreen"
        ]
        
        for sel in price_selectors:
            for el in soup.select(sel):
                p = parse_price(el.get_text())
                if p > 500.0: found_prices.append(p)
        
        extracted_price = min(found_prices) if found_prices else 0.0
        is_used = "ikinci el" in res.text.lower() or "used" in res.text.lower()

        # 3. KESİN STOK MANTIĞI: Fiyat varsa ürün vardır.
        in_stock = extracted_price > 0

        return {
            "title": title[:45] + "..." if len(title) > 45 else title,
            "price": extracted_price,
            "in_stock": in_stock,
            "is_used": is_used,
            "real_url": real_url
        }
    except Exception as e:
        logging.error(f"Scraping Hatası: {e}")
        return None

# ================= TELEGRAM KOMUTLARI =================

async def start_command(update, context):
    await update.message.reply_text("🤖 Bot aktif ve stok taramaya hazır.")

async def add_product(update, context):
    url = context.args[0] if context.args else update.message.text
    if "amazon" not in url.lower(): return
    
    msg = await update.message.reply_text("🔍 Taratılıyor...")
    data = scrape_amazon(url)
    
    if not data:
        await msg.edit_text("❌ Bilgiler çekilemedi.")
        return

    tracked_products[data["real_url"]] = {
        "title": data["title"], "last_price": data["price"], "target_price": 0.0, "in_stock": data["in_stock"]
    }
    save_data(tracked_products)
    
    status = f"✅ Stokta ({data['price']:.2f} TL)" if data["in_stock"] else "❌ Stokta Yok"
    await msg.edit_text(f"🎯 **Eklendi!**\n📦 {data['title']}\n📊 {status}", parse_mode="Markdown")

async def list_products(update, context):
    if not tracked_products: return
    text = "📋 *TAKİP EDİLEN*\n\n"
    for idx, (url, info) in enumerate(tracked_products.items(), 1):
        stok = f"✅ {info['last_price']:.2f} TL" if info["in_stock"] else "❌ Stokta Yok"
        text += f"*{idx}.* {info['title']}\n└ {stok}\n\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def delete_product(update, context):
    idx = int(context.args[0]) - 1
    urls = list(tracked_products.keys())
    if 0 <= idx < len(urls):
        removed = tracked_products.pop(urls[idx])
        save_data(tracked_products)
        await update.message.reply_text(f"🗑 {removed['title']} silindi.")

# ================= ARKA PLAN =================

async def check_all_products_job(context):
    global tracked_products
    tracked_products = load_data()
    updated = False
    
    for url, info in list(tracked_products.items()):
        curr = scrape_amazon(url)
        if not curr: continue
        
        # Stok veya fiyat değiştiyse bildir
        if curr["in_stock"] != info["in_stock"] or curr["price"] != info["last_price"]:
            msg = f"🔔 *Güncelleme*\n📦 {curr['title']}\n💰 Fiyat: {curr['price']:.2f} TL\n📊 Stok: {'✅ Var' if curr['in_stock'] else '❌ Yok'}"
            await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text=msg, parse_mode="Markdown")
            
        tracked_products[url].update({"in_stock": curr["in_stock"], "last_price": curr["price"]})
        updated = True
        
    if updated: save_data(tracked_products)

# ================= ANA =================

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("ekle", add_product))
    app.add_handler(CommandHandler("liste", list_products))
    app.add_handler(CommandHandler("sil", delete_product))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), add_product))
    app.job_queue.run_repeating(check_all_products_job, interval=600, first=20)
    app.run_polling()
