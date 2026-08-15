import os
import re
import json
import random
import logging
import asyncio
import requests
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
]

# ================= KORUMALI BULUT VERİ TABANI YÖNETİMİ (NPOINT) =================

DATA_LOADED_SUCCESSFULLY = False

def load_data() -> dict:
    global DATA_LOADED_SUCCESSFULLY
    if not NPOINT_ID:
        logging.warning("NPOINT_ID değişkeni bulunamadı.")
        return {}
    
    for attempt in range(3):
        try:
            res = requests.get(f"https://api.npoint.io/{NPOINT_ID}", timeout=15)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, dict):
                    DATA_LOADED_SUCCESSFULLY = True
                    logging.info("NPoint veritabanı başarıyla yüklendi.")
                    return data
        except Exception as e:
            logging.warning(f"NPoint yükleme denemesi {attempt + 1}/3 başarısız: {e}")
            
    logging.error("NPoint verisine ulaşılamadı! Eski verilerin silinmemesi için kaydetme kilitlendi.")
    return {}

def save_data(data: dict):
    global DATA_LOADED_SUCCESSFULLY
    if not NPOINT_ID:
        return
    
    if not DATA_LOADED_SUCCESSFULLY and not data:
        logging.error("Veritabanı başlangıçta yüklenemediği için sıfırlama engellendi!")
        return

    for attempt in range(3):
        try:
            res = requests.post(f"https://api.npoint.io/{NPOINT_ID}", json=data, timeout=15)
            if res.status_code == 200:
                DATA_LOADED_SUCCESSFULLY = True
                return
        except Exception as e:
            logging.warning(f"NPoint kaydetme denemesi {attempt + 1}/3 başarısız: {e}")
            
    logging.error("NPoint verisi kaydedilemedi!")

tracked_products = load_data()

# ================= YARDIMCI FONKSİYONLAR =================

def get_tr_time() -> str:
    tr_tz = timezone(timedelta(hours=3))
    months = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
    now = datetime.now(tr_tz)
    return f"{now.day} {months[now.month - 1]} {now.strftime('%H:%M')}"

def resolve_url(url: str) -> str:
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": random.choice(USER_AGENTS)})
        res = s.get(url, allow_redirects=True, timeout=12)
        final_url = res.url.split("?")[0].split("/ref=")[0]
        return final_url
    except Exception as e:
        logging.error(f"URL Yönlendirme hatası: {e}")
        return url.split("?")[0].split("/ref=")[0]

def parse_price(price_str: str) -> float:
    if not price_str:
        return 0.0
    try:
        clean = re.sub(r"[^\d.,]", "", str(price_str)).strip()
        if not clean:
            return 0.0

        if "." in clean and "," in clean:
            if clean.rfind(",") > clean.rfind("."):
                clean = clean.replace(".", "").replace(",", ".")
            else:
                clean = clean.replace(",", "")
        elif "," in clean:
            parts = clean.split(",")
            if len(parts[-1]) == 2:
                clean = clean.replace(",", ".")
            elif len(parts[-1]) == 3 and len(parts) > 1:
                clean = clean.replace(",", "")
            else:
                clean = clean.replace(",", ".")
        elif "." in clean:
            parts = clean.split(".")
            if len(parts[-1]) == 3 and len(parts) > 1:
                clean = clean.replace(".", "")

        val = float(clean)
        return val if val > 0.0 else 0.0
    except Exception:
        return 0.0

# ================= AMAZON SCRAPER (SCRAPERAPI DESTEKLİ) =================

def extract_price_from_soup(soup) -> float:
    variation_selectors = [
        "#twister", 
        "#variation_color_name", 
        "#variation_size_name", 
        "#variation_style_name", 
        ".twister-plus-inline-twister", 
        "#inline-twister-row-color_name"
    ]
    for v_sel in variation_selectors:
        for match in soup.select(v_sel):
            match.decompose()

    price_containers = [
        "#corePrice_feature_div .a-price",
        "#corePriceDisplay_desktop_feature_div .a-price",
        "#apex_desktop .a-price"
    ]

    for container in price_containers:
        for p_box in soup.select(container):
            whole = p_box.select_one(".a-price-whole")
            fraction = p_box.select_one(".a-price-fraction")
            if whole:
                w_text = re.sub(r"[^\d]", "", whole.get_text())
                f_text = re.sub(r"[^\d]", "", fraction.get_text()) if fraction else "00"
                if w_text:
                    p_val = float(f"{w_text}.{f_text}")
                    if p_val > 0.0:
                        return p_val

    selectors = [
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#apex_desktop .a-price .a-offscreen",
        "#price_inside_buybox",
        "#priceblock_ourprice"
    ]

    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            p = parse_price(el.get_text())
            if p > 0.0:
                return p

    return 0.0

def extract_image_url(soup) -> str:
    try:
        img_el = soup.find("img", {"id": "landingImage"}) or soup.find("img", {"id": "imgBlkFront"})
        if img_el:
            if img_el.has_attr("data-old-hires") and img_el["data-old-hires"]:
                return img_el["data-old-hires"]
            if img_el.has_attr("src"):
                return img_el["src"]
    except Exception:
        pass
    return ""

def is_valid_html(html_text: str) -> bool:
    """Sayfanın gerçek bir ürün sayfası mı yoksa engel/CAPTCHA mı olduğunu kontrol eder."""
    if not html_text:
        return False
    
    text_lower = html_text.lower()
    if "enter the characters you see below" in text_lower or "robot değilim" in text_lower:
        return False
        
    return "producttitle" in text_lower or "id=\"title\"" in text_lower

def scrape_amazon(raw_url: str):
    try:
        real_url = resolve_url(raw_url)
        html_content = None
        
        # 1. ÖNCELİK: Ücretsiz Doğrudan İstek
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.google.com.tr/",
            "DNT": "1"
        }

        try:
            session = requests.Session()
            session.cookies.set("i18n-prefs", "TRY", domain=".amazon.com.tr")
            session.cookies.set("lc-main-tr", "tr_TR", domain=".amazon.com.tr")
            res = session.get(real_url, headers=headers, timeout=10)
            if res.status_code == 200 and is_valid_html(res.text):
                html_content = res.text
        except Exception as e:
            logging.warning(f"Doğrudan istek başarısız oldu: {e}")

        # 2. ÖNCELİK: ScraperAPI (Doğrudan istek başarısızsa veya engellendiyse)
        if not html_content and SCRAPER_KEY:
            logging.info(f"Doğrudan istek engellendi, ScraperAPI devreye giriyor: {real_url}")
            try:
                # ScraperAPI sorgu parametreleri
                target_url = (
                    f"http://api.scraperapi.com?"
                    f"api_key={SCRAPER_KEY}"
                    f"&url={requests.utils.quote(real_url)}"
                    f"&country_code=tr"
                    f"&device_type=desktop"
                )
                res = requests.get(target_url, timeout=25)
                if res.status_code == 200 and is_valid_html(res.text):
                    html_content = res.text
                else:
                    logging.error(f"ScraperAPI engeli aşamadı veya hatalı yanıt döndürdü (HTTP {res.status_code}).")
            except Exception as e:
                logging.error(f"ScraperAPI Bağlantı Hatası: {e}")

        if not html_content:
            return None

        soup = BeautifulSoup(html_content, "html.parser")

        title_el = soup.find("span", {"id": "productTitle"}) or soup.find("h1", {"id": "title"})
        if not title_el:
            return None
        
        title = title_el.get_text(strip=True)
        extracted_price = extract_price_from_soup(soup)
        image_url = extract_image_url(soup)

        has_buy_button = bool(soup.find("input", {"id": "add-to-cart-button"}) or soup.find("input", {"id": "buy-now-button"}))
        in_stock = (extracted_price > 0.0) or has_buy_button

        used_keywords = ["ikinci el", "kullanılmış", "fırsat ürünleri", "amazon warehouse", "used"]
        is_used = any(kw in html_content.lower() for kw in used_keywords)

        has_coupon = False
        coupon_selectors = ["#promoPriceBlockMessage_feature_div", "#vPCBadge", "#applicable_promotion_list", ".voucher-badge"]
        for c_sel in coupon_selectors:
            if soup.select(c_sel):
                has_coupon = True
                break

        return {
            "title": title[:45] + "..." if len(title) > 45 else title,
            "price": extracted_price,
            "in_stock": in_stock,
            "is_used": is_used,
            "has_coupon": has_coupon,
            "image_url": image_url,
            "real_url": real_url
        }
    except Exception as e:
        logging.error(f"Scraping Hatası: {e}")
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
        "🤖 <b>Amazon Stok & Fiyat Takip Botu Pro!</b>\n\n"
        "<b>Temel Komutlar:</b>\n"
        "▫️ `/ekle [link] [hedef_fiyat]` - Takip ekler\n"
        "▫️ `/liste` - Takip edilen ürünleri gösterir\n"
        "▫️ `/fiyat [sıra_no]` - Canlı anlık fiyatı çeker\n"
        "▫️ `/gecmis [sıra_no]` - Fiyat geçmişini gösterir\n"
        "▫️ `/tara` - Anında tüm ürünleri tarar\n"
        "▫️ `/rapor` - Genel durum raporu sunar\n"
        "▫️ `/sil [sıra_no]` - Listeden ürün çıkarır\n\n"
        "<b>Temizleme Komutları:</b>\n"
        "▫️ `/temizle_stoksuz` - Stokta olmayanları siler\n"
        "▫️ `/temizle_hepsi` - Tüm listeyi sıfırlar\n"
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
    elif update.message and update.message.text and "http" in update.message.text:
        parts = update.message.text.strip().split()
        url = parts[0]
        if len(parts) > 1:
            target_price = parse_price(parts[1])

    if not url or ("amazon" not in url.lower() and "amzn" not in url.lower()):
        await update.message.reply_text("❌ Lütfen geçerli bir Amazon ürün linki girin.")
        return

    msg = await update.message.reply_text("🔍 Ürün taranıyor...")
    data = await asyncio.to_thread(scrape_amazon, url)

    if not data:
        await msg.edit_text("❌ Ürün bilgileri çekilemedi. Amazon engeline takılmış olabilir veya link geçersiz.")
        return

    real_url = data["real_url"]
    now_tr = get_tr_time()
    history = [data["price"]] if data["price"] > 0 else []

    tracked_products[real_url] = {
        "title": data["title"],
        "last_price": data["price"],
        "target_price": target_price,
        "lowest_price": data["price"] if data["price"] > 0 else 999999.0,
        "in_stock": data["in_stock"],
        "has_coupon": data["has_coupon"],
        "image_url": data.get("image_url", ""),
        "last_check": now_tr,
        "history": history
    }
    await asyncio.to_thread(save_data, tracked_products)

    status_str = f"✅ Stokta ({data['price']:.2f} TL)" if data["in_stock"] else "❌ Stokta Yok"
    target_str = f"\n🎯 <b>Hedef Fiyat:</b> {target_price:.2f} TL" if target_price > 0 else ""

    reply = (
        f"🎯 <b>Ürün Takibe Eklendi!</b>\n\n"
        f"📦 <b>Ürün:</b> {data['title']}\n"
        f"📊 <b>Durum:</b> {status_str}{target_str}\n"
        f"🕒 <b>Tarama Zamanı:</b> {now_tr}"
    )
    
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 FIRSATA GİT", url=real_url)]])
    await msg.delete()

    # Fotoğraf gönderme fallback mekanizması
    if data.get("image_url"):
        try:
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=data["image_url"], caption=reply, parse_mode="HTML", reply_markup=keyboard)
            return
        except Exception as e:
            logging.warning(f"Resimli mesaj gönderilemedi, metin olarak gönderiliyor: {e}")

    await update.message.reply_text(reply, parse_mode="HTML", reply_markup=keyboard)

async def list_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    global tracked_products
    fresh_data = await asyncio.to_thread(load_data)
    if fresh_data:
        tracked_products = fresh_data

    if not tracked_products:
        await update.message.reply_text("📋 Şu anda takip edilen hiç ürün yok.")
        return

    text = "📋 <b>TAKİP EDİLEN ÜRÜNLER</b>\n\n"
    for idx, (url, info) in enumerate(tracked_products.items(), 1):
        stok_durum = f"✅ {info['last_price']:.2f} TL" if info["in_stock"] else "❌ Stokta Yok"
        target_info = f" (Hedef: {info['target_price']:.2f} TL)" if info.get("target_price", 0) > 0 else ""
        last_check = info.get("last_check", "Henüz taranmadı")
        
        text += (
            f"<b>{idx}.</b> {info['title']}\n"
            f"   └ Durum: {stok_durum}{target_info}\n"
            f"   └ 🕒 Son Tarama: {last_check}\n"
            f"   └ <a href='{url}'>Link</a>\n\n"
        )

    await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)

async def get_instant_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("❌ Kullanım: `/fiyat 1`", parse_mode="Markdown")
        return

    index = int(context.args[0]) - 1
    urls = list(tracked_products.keys())

    if index < 0 or index >= len(urls):
        await update.message.reply_text("❌ Geçersiz sıra numarası.")
        return

    target_url = urls[index]
    product = tracked_products[target_url]

    msg = await update.message.reply_text(f"⚡ <b>{product['title']}</b> için canlı fiyat sorgulanıyor...", parse_mode="HTML")
    
    current_data = await asyncio.to_thread(scrape_amazon, target_url)
    now_tr = get_tr_time()

    if not current_data:
        await msg.edit_text("❌ Anlık fiyat çekilemedi.")
        return

    tracked_products[target_url]["last_price"] = current_data["price"]
    tracked_products[target_url]["in_stock"] = current_data["in_stock"]
    tracked_products[target_url]["has_coupon"] = current_data["has_coupon"]
    tracked_products[target_url]["last_check"] = now_tr
    if current_data.get("image_url"):
        tracked_products[target_url]["image_url"] = current_data["image_url"]
        
    await asyncio.to_thread(save_data, tracked_products)

    stok_str = f"✅ {current_data['price']:.2f} TL" if current_data["in_stock"] else "❌ Stokta Yok"

    res_text = (
        f"⚡ <b>ANLIK FİYAT BİLGİSİ</b>\n\n"
        f"📦 <b>Ürün:</b> {current_data['title']}\n"
        f"💰 <b>Anlık Durum:</b> {stok_str}\n"
        f"🕒 <b>Sorgu Tarihi (TSİ):</b> {now_tr}"
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 FIRSATA GİT", url=target_url)]])
    await msg.delete()

    if current_data.get("image_url"):
        try:
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=current_data["image_url"], caption=res_text, parse_mode="HTML", reply_markup=keyboard)
            return
        except Exception:
            pass

    await update.message.reply_text(res_text, parse_mode="HTML", reply_markup=keyboard)

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("❌ Kullanım: `/gecmis 1`", parse_mode="Markdown")
        return

    index = int(context.args[0]) - 1
    urls = list(tracked_products.keys())

    if index < 0 or index >= len(urls):
        await update.message.reply_text("❌ Geçersiz sıra numarası.")
        return

    target_url = urls[index]
    product = tracked_products[target_url]
    history = product.get("history", [])

    if not history:
        await update.message.reply_text("📉 Geçmiş kaydı yok.", parse_mode="HTML")
        return

    hist_text = f"📈 <b>FİYAT GEÇMİŞİ & TREND</b>\n📦 <b>{product['title']}</b>\n\n"
    
    for i in range(len(history)):
        curr = history[i]
        icon = "➡️"
        if i > 0:
            prev = history[i-1]
            if curr < prev:
                icon = "📉 (Düştü)"
            elif curr > prev:
                icon = "↗️ (Yükseldi)"
        
        hist_text += f"• Kayıt {i+1}: <b>{curr:.2f} TL</b> {icon}\n"

    await update.message.reply_text(hist_text, parse_mode="HTML")

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    total = len(tracked_products)
    if total == 0:
        await update.message.reply_text("📊 Takipte ürün bulunmuyor.")
        return

    in_stock_count = sum(1 for p in tracked_products.values() if p.get("in_stock"))
    out_of_stock_count = total - in_stock_count
    coupon_count = sum(1 for p in tracked_products.values() if p.get("has_coupon"))

    report_text = (
        f"📊 <b>GENEL BOTA BAKIŞ RAPORU</b>\n\n"
        f"📦 Toplam Takip: <b>{total} Ürün</b>\n"
        f"✅ Stokta Olanlar: <b>{in_stock_count} Ürün</b>\n"
        f"❌ Stokta Olmayanlar: <b>{out_of_stock_count} Ürün</b>\n"
        f"🎟 Kuponlu Ürünler: <b>{coupon_count} Ürün</b>\n"
        f"🕒 Son Rapor Tarihi: {get_tr_time()}"
    )
    await update.message.reply_text(report_text, parse_mode="HTML")

async def force_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    msg = await update.message.reply_text("🔄 Anlık toplu tarama başlatıldı...")
    await check_all_products_job(context)
    await msg.edit_text("✅ Tarama tamamlandı!")

async def delete_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("❌ Kullanım: `/sil 1`", parse_mode="Markdown")
        return

    index = int(context.args[0]) - 1
    urls = list(tracked_products.keys())

    if index < 0 or index >= len(urls):
        await update.message.reply_text("❌ Geçersiz sıra numarası.")
        return

    target_url = urls[index]
    removed_item = tracked_products.pop(target_url)
    await asyncio.to_thread(save_data, tracked_products)
    await update.message.reply_text(f"🗑 <b>{removed_item['title']}</b> silindi.", parse_mode="HTML")

async def clear_out_of_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    global tracked_products
    removed_count = 0
    for url, info in list(tracked_products.items()):
        if not info.get("in_stock"):
            tracked_products.pop(url)
            removed_count += 1

    if removed_count > 0:
        await asyncio.to_thread(save_data, tracked_products)
        await update.message.reply_text(f"🗑 Stokta olmayan <b>{removed_count}</b> ürün silindi.", parse_mode="HTML")
    else:
        await update.message.reply_text("📋 Stokta olmayan ürün bulunamadı.")

async def clear_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    global tracked_products
    tracked_products.clear()
    await asyncio.to_thread(save_data, tracked_products)
    await update.message.reply_text("🗑 Tüm takip listesi temizlendi!")

# ================= ARKA PLAN TARAMA GÖREVİ =================

async def check_all_products_job(context: ContextTypes.DEFAULT_TYPE):
    global tracked_products
    try:
        fresh_data = await asyncio.to_thread(load_data)
        if fresh_data:
            tracked_products = fresh_data

        if not tracked_products or not ALLOWED_CHAT_ID:
            return

        updated = False
        now_tr = get_tr_time()

        for url, info in list(tracked_products.items()):
            current_data = await asyncio.to_thread(scrape_amazon, url)
            
            if not current_data:
                logging.warning(f"Ürün verisi çekilemedi: {url}")
                continue

            prev_stock = info.get("in_stock", True)
            prev_price = info.get("last_price", 0.0)
            prev_coupon = info.get("has_coupon", False)
            target_price = info.get("target_price", 0.0)
            lowest_price = info.get("lowest_price", 999999.0)

            curr_stock = current_data["in_stock"]
            curr_price = current_data["price"]
            curr_coupon = current_data["has_coupon"]
            is_used = current_data.get("is_used", False)
            image_url = current_data.get("image_url") or info.get("image_url", "")

            notify = False
            alert_reason = ""

            if curr_stock and not prev_stock:
                notify = True
                alert_reason = f"🚨 <b>STOK ALARMI!</b>\nÜrün tekrar stoğa girdi!\n💰 Fiyat: <b>{curr_price:.2f} TL</b>"

            elif curr_stock and curr_price > 0:
                if target_price > 0 and curr_price <= target_price and prev_price > target_price:
                    notify = True
                    tag = " (♻️ İkinci El / Depo)" if is_used else ""
                    alert_reason = f"🎯 <b>HEDEF FİYAT ALARMI!</b>{tag}\nİstediğiniz fiyata ulaşıldı!\n💰 Fiyat: <b>{curr_price:.2f} TL</b>"
                
                elif curr_price < prev_price and prev_price > 0:
                    drop_ratio = (prev_price - curr_price) / prev_price
                    if drop_ratio > 0.80 and curr_price < 1000.0:
                        notify = False
                    else:
                        notify = True
                        tag = "\n♻️ <i>(İkinci El / Depo)</i>" if is_used else ""
                        
                        ath_tag = ""
                        if curr_price < lowest_price:
                            ath_tag = "\n🔥 <b>TARİHİ EN DÜŞÜK FİYAT (ATH)!</b>"
                            info["lowest_price"] = curr_price

                        alert_reason = f"📉 <b>FİYAT DÜŞTÜ ALARMI!</b>{tag}{ath_tag}\nEski Fiyat: {prev_price:.2f} TL\nYeni Fiyat: <b>{curr_price:.2f} TL</b>"

            if curr_stock and curr_coupon and not prev_coupon:
                notify = True
                coupon_msg = "\n🎟 <b>KUPON / FIRSAT TESPİT EDİLDİ!</b>"
                alert_reason = alert_reason + coupon_msg if alert_reason else coupon_msg

            history = info.get("history", [])
            if curr_price > 0 and (not history or history[-1] != curr_price):
                history.append(curr_price)
                if len(history) > 10:
                    history.pop(0)

            tracked_products[url]["in_stock"] = curr_stock
            tracked_products[url]["last_price"] = curr_price
            tracked_products[url]["has_coupon"] = curr_coupon
            tracked_products[url]["last_check"] = now_tr
            tracked_products[url]["history"] = history
            tracked_products[url]["image_url"] = image_url
            updated = True

            if notify:
                msg = (
                    f"{alert_reason}\n\n"
                    f"📦 <b>{current_data['title']}</b>\n"
                    f"🕒 <b>Tarih:</b> {now_tr}"
                )
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 FIRSATA GİT", url=url)]])
                
                try:
                    if image_url:
                        await context.bot.send_photo(chat_id=ALLOWED_CHAT_ID, photo=image_url, caption=msg, parse_mode="HTML", reply_markup=keyboard)
                    else:
                        await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text=msg, parse_mode="HTML", reply_markup=keyboard)
                except Exception as e:
                    logging.error(f"Fotoğraflı bildirim gönderilemedi, düz metin deneniyor: {e}")
                    try:
                        await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text=msg, parse_mode="HTML", reply_markup=keyboard)
                    except Exception as err:
                        logging.error(f"Bildirim tamamen başarısız oldu: {err}")

        if updated:
            await asyncio.to_thread(save_data, tracked_products)
    except Exception as e:
        logging.error(f"Arka plan görevi hatası: {e}")

# ================= ANA BAŞLATICI =================

if __name__ == "__main__":
    if not TOKEN:
        print("[!] Hata: TELEGRAMTOKEN ortam değişkeni bulunamadı!")
        exit(1)

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("ekle", add_product))
    app.add_handler(CommandHandler("liste", list_products))
    app.add_handler(CommandHandler("fiyat", get_instant_price))
    app.add_handler(CommandHandler("gecmis", show_history))
    app.add_handler(CommandHandler("rapor", report_command))
    app.add_handler(CommandHandler("tara", force_scan))
    app.add_handler(CommandHandler("sil", delete_product))
    app.add_handler(CommandHandler("temizle_stoksuz", clear_out_of_stock))
    app.add_handler(CommandHandler("temizle_hepsi", clear_all))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), add_product))

    if app.job_queue:
        app.job_queue.run_repeating(check_all_products_job, interval=600, first=20)

    print("Bot başarıyla başlatıldı!")
    app.run_polling()
