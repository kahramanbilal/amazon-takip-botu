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

# Güvenlik için Chat ID integer formatına dönüştürülür
ALLOWED_CHAT_ID = int(CHAT_ID_ENV) if CHAT_ID_ENV and CHAT_ID_ENV.isdigit() else None

# Veri depolama dosyası
DATA_FILE = "tracked_products.json"

# Amazon Tarayıcı Başlıkları
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
}

# ================= VERİ TABANI (JSON) YÖNETİMİ =================

def load_data() -> dict:
    """Verileri JSON dosyasından yükler."""
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Veri yükleme hatası: {e}")
            return {}
    return {}

def save_data(data: dict):
    """Verileri JSON dosyasına kaydeder."""
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Veri kaydetme hatası: {e}")

# Global hafıza
tracked_products = load_data()

# ================= YARDIMCI FONKSİYONLAR =================

def parse_price(price_str: str) -> float:
    """Metin halindeki fiyatı sayısal değere çevirir."""
    if not price_str:
        return 0.0
    try:
        clean = re.sub(r"[^\d,]", "", price_str)
        clean = clean.replace(",", ".")
        return float(clean)
    except Exception:
        return 0.0

def resolve_url(url: str) -> str:
    """Kısa linkleri (amzn.eu) gerçek URL'e dönüştürür."""
    try:
        res = requests.head(url, headers=HEADERS, allow_redirects=True, timeout=10)
        return res.url
    except Exception:
        return url

def scrape_amazon(url: str):
    """Amazon sayfasından başlık, stok ve en düşük fiyatı tarar."""
    try:
        full_url = resolve_url(url)
        res = requests.get(full_url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return None

        soup = BeautifulSoup(res.text, "html.parser")
        page_text = res.text.lower()

        # Ürün Başlığı
        title_el = soup.find("span", {"id": "productTitle"})
        title = title_el.get_text(strip=True) if title_el else "Amazon Ürünü"

        # Fiyat Analizi (Ana satıcı + Diğer satıcılar)
        prices = []
        main_price_el = soup.find("span", class_="a-offscreen")
        if main_price_el:
            prices.append(parse_price(main_price_el.get_text()))

        other_sellers_box = soup.find("div", {"id": "mbc"}) or soup.find("div", {"id": "moreBuyingChoices_feature_div"})
        if other_sellers_box:
            other_prices = other_sellers_box.find_all("span", class_="a-color-price")
            for p in other_prices:
                prices.append(parse_price(p.get_text()))

        valid_prices = [p for p in prices if p > 0]
        lowest_price = min(valid_prices) if valid_prices else 0.0

        # Stok Durumu
        out_keywords = ["şu anda stokta yok", "geçici olarak temin edilememektedir", "currently unavailable"]
        is_out_of_stock = any(kw in page_text for kw in out_keywords)
        has_add_to_cart = soup.find("input", {"id": "add-to-cart-button"}) is not None
        in_stock = not is_out_of_stock and (has_add_to_cart or lowest_price > 0)

        return {
            "title": title[:40] + "..." if len(title) > 40 else title,
            "price": lowest_price,
            "in_stock": in_stock,
            "real_url": full_url
        }
    except Exception as e:
        logging.error(f"Scraping hatası ({url}): {e}")
        return None

def is_authorized(update: Update) -> bool:
    """Yalnızca yetkili Chat ID'den gelen mesajlara izin verir."""
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
        if len(context.args) > 1 and context.args[1].replace(".", "", 1).isdigit():
            target_price = float(context.args[1])
    elif update.message.text and "http" in update.message.text:
        parts = update.message.text.strip().split()
        url = parts[0]
        if len(parts) > 1 and parts[1].replace(".", "", 1).isdigit():
            target_price = float(parts[1])

    if not url or ("amazon" not in url.lower() and "amzn" not in url.lower()):
        await update.message.reply_text("❌ Lütfen geçerli bir Amazon ürün linki girin.")
        return

    msg = await update.message.reply_text("🔍 Ürün taranıyor, lütfen bekleyin...")
    data = scrape_amazon(url)

    if not data:
        await msg.edit_text("❌ Ürün bilgileri çekilemedi. Linki kontrol edin.")
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

        notify = False
        alert_reason = ""

        # 1. Stok Yokken Stoğa Girdi
        if curr_stock and not prev_stock:
            notify = True
            alert_reason = f"🚨 <b>STOK ALARMI!</b>\nÜrün tekrar stoğa girdi!\n💰 Fiyat: <b>{curr_price:.2f} TL</b>"

        # 2. Fiyat Düştü veya Hedef Fiyata Ulaşıldı
        elif curr_stock and curr_price > 0:
            if target_price > 0 and curr_price <= target_price and prev_price > target_price:
                notify = True
                alert_reason = f"🎯 <b>HEDEF FİYAT ALARMI!</b>\nİstediğiniz fiyata ulaşıldı!\n💰 Fiyat: <b>{curr_price:.2f} TL</b> (Hedef: {target_price:.2f} TL)"
            elif curr_price < prev_price and prev_price > 0:
                notify = True
                alert_reason = f"📉 <b>FİYAT DÜŞTÜ ALARMI!</b>\nEski Fiyat: {prev_price:.2f} TL\nYeni Fiyat: <b>{curr_price:.2f} TL</b>"

        # Bilgileri Güncelle
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

    # Komut Kayıtları
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("ekle", add_product))
    app.add_handler(CommandHandler("liste", list_products))
    app.add_handler(CommandHandler("sil", delete_product))
    app.add_handler(CommandHandler("durum", status_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), add_product))

    # Arka Plan Görevi: 10 Dakikada bir (600 saniye) çalışır
    app.job_queue.run_repeating(check_all_products_job, interval=600, first=20)

    print("Bot başarıyla başlatıldı!")
    app.run_polling()

