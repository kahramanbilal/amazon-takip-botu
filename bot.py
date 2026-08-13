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
        logging.warning("NPOINT_ID değişkeni bulunamadı. Veriler geçici hafızada saklanacak.")
        return {}
    try:
        res = requests.get(f"https://api.npoint.io/{NPOINT_ID}", timeout=10)
        if res.status_code == 200:
            data = res.json()
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logging.error(f"NPoint yükleme hatası: {e}")
    return {}

def save_data(data: dict):
    if not NPOINT_ID:
        return
    try:
        requests.post(f"https://api.npoint.io/{NPOINT_ID}", json=data, timeout=10)
    except Exception as e:
        logging.error(f"NPoint kaydetme hatası: {e}")

tracked_products = load_data()

# ================= YARDIMCI FONKSİYONLAR =================

def resolve_url(url: str) -> str:
    """Kısa amzn.eu linklerini uzun Amazon ürün linkine dönüştürür."""
    try:
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        })
        res = s.get(url, allow_redirects=True, timeout=12)
        return res.url
    except Exception as e:
        logging.error(f"URL Yönlendirme hatası: {e}")
        return url

def parse_price(price_str: str) -> float:
    """Amazon TR fiyat formatlarını temiz float sayıya dönüştürür."""
    if not price_str:
        return 0.0
    try:
        clean = re.sub(r"[^\d.,]", "", str(price_str)).strip()
        if not clean:
            return 0.0

        if "." in clean and "," in clean:
            clean = clean.replace(".", "").replace(",", ".")
        elif "," in clean:
            clean = clean.replace(",", ".")
        elif "." in clean:
            parts = clean.split(".")
            if len(parts[-1]) == 3:
                clean = clean.replace(".", "")

        return float(clean)
    except Exception:
        return 0.0

def scrape_amazon(raw_url: str):
    try:
        real_url = resolve_url(raw_url)
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Upgrade-Insecure-Requests": "1"
        }

        res = None
        if SCRAPER_KEY:
            try:
                target_url = f"http://api.scraperapi.com?api_key={SCRAPER_KEY}&url={requests.utils.quote(real_url)}&country_code=tr&render=true"
                res = requests.get(target_url, timeout=35)
            except Exception:
                pass

        if not res or res.status_code != 200 or "productTitle" not in res.text:
            if SCRAPER_KEY:
                try:
                    target_url = f"http://api.scraperapi.com?api_key={SCRAPER_KEY}&url={requests.utils.quote(real_url)}&country_code=tr"
                    res = requests.get(target_url, timeout=25)
                except Exception:
                    pass

        if not res or res.status_code != 200 or "productTitle" not in res.text:
            session = requests.Session()
            session.cookies.set("i18n-prefs", "TRY", domain=".amazon.com.tr")
            res = session.get(real_url, headers=headers, timeout=15)

        if not res or res.status_code != 200:
            logging.warning("Amazon yanıtı alınamadı!")
            return None

        soup = BeautifulSoup(res.text, "html.parser")
        html_text = res.text

        if "enter the characters you see below" in html_text.lower() or "robot değilim" in html_text.lower():
            logging.warning("Amazon Captcha engeline takıldı!")
            return None

        # 1. BAŞLIK ÇEKME
        title_el = soup.find("span", {"id": "productTitle"}) or soup.find("h1", {"id": "title"})
        if not title_el:
            return None
        
        title = title_el.get_text(strip=True)

        # 2. HEM SIFIR HEM İKİNCİ EL/DEPO FİYATLARINI ALIP EN DÜŞÜĞÜNÜ SEÇME
        found_prices = []
        is_used = False

        price_selectors = [
            "#corePrice_feature_div .a-price .a-offscreen",
            "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
            "#apex_desktop .a-price .a-offscreen",
            "#usedAccordion .a-price .a-offscreen",          # İkinci el Akordiyon alanı
            "#usedBuyBox .a-price .a-offscreen",            # İkinci el BuyBox
            "#moreBuyingChoices_feature_div .a-price .a-offscreen", # Diğer Satıcılar / İkinci el
            "#priceblock_ourprice",
            "#priceblock_dealprice"
        ]

        for sel in price_selectors:
            elements = soup.select(sel)
            for el in elements:
                p = parse_price(el.get_text())
                if p > 500.0:  # Kupon/taksit tutarlarını elemek için
                    found_prices.append(p)

        # Sayfada İkinci El İbaresi veya İkinci El Fiyatı Var mı Kontrol Et
        used_keywords = ["ikinci el", "kullanılmış", "fırsat ürünleri", "amazon warehouse", "used"]
        has_used_mention = any(kw in html_text.lower() for kw in used_keywords)

        extracted_price = min(found_prices) if found_prices else 0.0

        # Eğer bulunan en düşük fiyat ana fiyattan ucuzsa ve sayfada ikinci el ibaresi varsa:
        if extracted_price > 0 and has_used_mention:
            # İkinci el fiyatı olma ihtimali yüksek
            is_used = True

        # 3. STOK KONTROLÜ
        out_keywords = ["şu anda stokta yok", "currently unavailable", "geçici olarak temin edilememektedir"]
        is_out = any(kw in html_text.lower() for kw in out_keywords)

        in_stock = not is_out and extracted_price > 0

        return {
            "title": title[:45] + "..." if len(title) > 45 else title,
            "price": extracted_price,
            "in_stock": in_stock,
            "is_used": is_used,
            "real_url": real_url
        }
    except Exception as e:
        logging.error(f"Scraping Hatası ({raw_url}): {e}")
        return None

def is_authorized(update: Update) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    return update.effective_chat.id == ALLOWED_CHAT_ID

# ================= TELEGRAM KOMUTLARI =================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    welcome_text = (
        "🤖 <b>Amazon Stok & Fiyat Takip Botu Aktif!</b>\n\n"
        "<b>Komutlar:</b>\n"
        "▫️ `/ekle [link] [hedef_fiyat]` - Takip ekler\n"
        "▫️ `/liste` - Takip edilen tüm ürünleri gösterir\n"
        "▫️ `/sil [sıra_no]` - Listeden ürün çıkarır\n"
        "▫️ `/durum` - Sistem durumunu raporlar\n\n"
        "💡 <i>Doğrudan bir Amazon linki de gönderebilirsiniz!</i>"
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")

async def add_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    url = None
    target_price = 0.0

    if context.args:
        url = context.args[0]
        if len(context.args) > 1:
            target_price = parse_price(context.args[1])
    elif update.message.text and "http" in update.message.text:
        parts = update.message.text.strip().split()
        url = parts[0]
        if len(parts) > 1:
            target_price = parse_price(parts[1])

    if not url or ("amazon" not in url.lower() and "amzn" not in url.lower()):
        await update.message.reply_text("❌ Lütfen geçerli bir Amazon ürün linki girin.")
        return

    msg = await update.message.reply_text("🔍 Ürün taranıyor, lütfen bekleyin...")
    data = scrape_amazon(url)

    if not data:
        await msg.edit_text("❌ Ürün bilgileri çekilemedi. Amazon geçici engel koymuş olabilir, lütfen 1-2 dakika sonra tekrar deneyin.")
        return

    real_url = data["real_url"]
    tracked_products[real_url] = {
        "title": data["title"],
        "last_price": data["price"],
        "target_price": target_price,
        "in_stock": data["in_stock"]
    }
    save_data(tracked_products)

    status_str = f"✅ Stokta ({data['price']:.2f} TL)" if data["in_stock"] else "❌ Stokta Yok"
    target_str = f"\n🎯 <b>Hedef Fiyat:</b> {target_price:.2f} TL" if target_price > 0 else ""

    reply = (
        f"🎯 <b>Ürün Takibe Eklendi!</b>\n\n"
        f"📦 <b>Ürün:</b> {data['title']}\n"
        f"📊 <b>Durum:</b> {status_str}{target_str}\n"
        f"🔗 <a href='{real_url}'>Ürüne Git</a>"
    )
    await msg.edit_text(reply, parse_mode="HTML", disable_web_page_preview=True)

async def list_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    global tracked_products
    tracked_products = load_data()

    if not tracked_products:
        await update.message.reply_text("📋 Şu anda takip edilen hiç ürün yok.")
        return

    text = "📋 <b>TAKİP EDİLEN ÜRÜNLER</b>\n\n"
    for idx, (url, info) in enumerate(tracked_products.items(), 1):
        stok_durum = f"✅ {info['last_price']:.2f} TL" if info["in_stock"] else "❌ Stokta Yok"
        target_info = f" (Hedef: {info['target_price']:.2f} TL)" if info.get("target_price", 0) > 0 else ""
        text += f"<b>{idx}.</b> {info['title']}\n   └ Durum: {stok_durum}{target_info}\n   └ <a href='{url}'>Link</a>\n\n"

    text += "<i>Silmek için: <code>/sil [sıra_no]</code></i>"
    await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)

async def delete_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("❌ Lütfen silinecek ürünün numarasını girin. Örn: `/sil 1`", parse_mode="Markdown")
        return

    index = int(context.args[0]) - 1
    urls = list(tracked_products.keys())

    if index < 0 or index >= len(urls):
        await update.message.reply_text("❌ Geçersiz sıra numarası.")
        return

    target_url = urls[index]
    removed_item = tracked_products.pop(target_url)
    save_data(tracked_products)
    await update.message.reply_text(f"🗑 <b>{removed_item['title']}</b> takipten çıkarıldı.", parse_mode="HTML")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    count = len(tracked_products)
    await update.message.reply_text(f"⚡ Bot sorunsuz çalışıyor!\n📊 Toplam Takip Edilen Ürün: <b>{count}</b>", parse_mode="HTML")

# ================= ARKA PLAN TARAMA GÖREVİ =================

async def check_all_products_job(context: ContextTypes.DEFAULT_TYPE):
    global tracked_products
    tracked_products = load_data()

    if not tracked_products or not ALLOWED_CHAT_ID:
        return

    updated = False
    for url, info in list(tracked_products.items()):
        current_data = scrape_amazon(url)
        if not current_data:
            continue

        prev_stock = info["in_stock"]
        prev_price = info["last_price"]
        target_price = info.get("target_price", 0.0)

        curr_stock = current_data["in_stock"]
        curr_price = current_data["price"]
        is_used = current_data.get("is_used", False)

        notify = False
        alert_reason = ""

        if curr_stock and not prev_stock:
            notify = True
            alert_reason = f"🚨 <b>STOK ALARMI!</b>\nÜrün tekrar stoğa girdi!\n💰 Fiyat: <b>{curr_price:.2f} TL</b>"
        elif curr_stock and curr_price > 0:
            if target_price > 0 and curr_price <= target_price and prev_price > target_price:
                notify = True
                tag = " (♻️ İkinci El / Depo Fırsatı)" if is_used else ""
                alert_reason = f"🎯 <b>HEDEF FİYAT ALARMI!</b>{tag}\nİstediğiniz fiyata ulaşıldı!\n💰 Fiyat: <b>{curr_price:.2f} TL</b> (Hedef: {target_price:.2f} TL)"
            elif curr_price < prev_price and prev_price > 0:
                notify = True
                tag = "\n♻️ <i>(İkinci El / Depo Seçeneği eklendi/düştü)</i>" if is_used else ""
                alert_reason = f"📉 <b>FİYAT DÜŞTÜ ALARMI!</b>{tag}\nEski Fiyat: {prev_price:.2f} TL\nYeni Fiyat: <b>{curr_price:.2f} TL</b>"

        tracked_products[url]["in_stock"] = curr_stock
        tracked_products[url]["last_price"] = curr_price
        updated = True

        if notify:
            msg = (
                f"{alert_reason}\n\n"
                f"📦 <b>{current_data['title']}</b>\n"
                f"🔗 <a href='{url}'>Ürüne Gitmek İçin Tıklayın</a>"
            )
            try:
                await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text=msg, parse_mode="HTML")
            except Exception as e:
                logging.error(f"Bildirim hatası: {e}")

    if updated:
        save_data(tracked_products)

# ================= ANA BAŞLATICI =================

if __name__ == "__main__":
    if not TOKEN:
        print("[!] Hata: TELEGRAMTOKEN ortam değişkeni bulunamadı!")
        exit(1)

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("ekle", add_product))
    app.add_handler(CommandHandler("liste", list_products))
    app.add_handler(CommandHandler("sil", delete_product))
    app.add_handler(CommandHandler("durum", status_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), add_product))

    app.job_queue.run_repeating(check_all_products_job, interval=600, first=20)

    print("Bot başarıyla başlatıldı!")
    app.run_polling()
