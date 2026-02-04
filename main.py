import os
import time
import requests
import feedparser
from datetime import datetime, timedelta, timezone
from dateutil import tz
from bs4 import BeautifulSoup
from weasyprint import HTML
import smtplib, ssl
from email.message import EmailMessage


# ======================
# 기본 설정
# ======================
RSS_URL = os.getenv("RSS_URL", "https://rss.beehiiv.com/feeds/ez2zQOMePQ.xml")
TRANSLATE_URL = os.getenv("TRANSLATE_URL", "https://libretranslate.com/translate")

KST = tz.gettz("Asia/Seoul")


# ======================
# 유틸
# ======================
def now_kst():
    return datetime.now(tz=KST)


def is_sunday_kst():
    return now_kst().weekday() == 6


# ======================
# 번역 (HTML 텍스트 노드만)
# ======================
def translate_text(text, retries=3):
    if not text.strip():
        return text

    payload = {
        "q": text,
        "source": "en",
        "target": "ko",
        "format": "text"
    }

    for i in range(retries):
        try:
            r = requests.post(TRANSLATE_URL, json=payload, timeout=60)
            if r.status_code == 200:
                return r.json().get("translatedText", text)
        except Exception:
            print("ERROR:", e)


    return text  # 실패 시 원문 유지


def translate_html_preserve_layout(html):
    soup = BeautifulSoup(html, "html.parser")

    # ----------------------
    # ✅ 광고 제거 (Beehiiv 기준)
    # ----------------------
    for ad in soup.select("[data-testid='ad'], .sponsor, .advertisement"):
        ad.decompose()

    # ----------------------
    # 문단/리스트 단위로 번역 (정답)
    # ----------------------
    for tag in soup.find_all(["p", "li", "h1", "h2", "h3"]):
        text = tag.get_text(strip=True)
    
        # 너무 짧은 문장은 스킵
        if len(text) < 5:
            continue
    
        translated = translate_text(text)
    
        # 태그 안 내용을 통째로 교체
        tag.clear()
        tag.append(translated)
    
    return str(soup)


# ======================
# RSS → 오늘자 HTML 추출
# ======================
def fetch_today_html():
    feed = feedparser.parse(RSS_URL)
    today = now_kst().date()

    for e in feed.entries:
        if not hasattr(e, "published_parsed"):
            continue

        published_utc = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        published_kst = published_utc.astimezone(KST).date()

        if published_kst == today and "content" in e:
            return e.content[0].value

    return None


# ======================
# PDF 생성
# ======================
def html_to_pdf(html, date_str):
    filename = f"Gmail - Techpresso_{date_str}.pdf"
    HTML(string=html).write_pdf(filename)
    return filename


# ======================
# 이메일 발송
# ======================
def send_email(pdf_path, date_str):
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    mail_from = os.getenv("MAIL_FROM")
    mail_to = os.getenv("MAIL_TO")

    msg = EmailMessage()
    msg["Subject"] = f"Techpresso 한국어 번역 ({date_str})"
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.set_content("오늘의 Techpresso 한글 번역본을 첨부합니다.")

    with open(pdf_path, "rb") as f:
        msg.add_attachment(
            f.read(),
            maintype="application",
            subtype="pdf",
            filename=pdf_path
        )

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)


# ======================
# 메인
# ======================
def main():
    if is_sunday_kst():
        print("Sunday – skipped")
        return

    date_str = now_kst().strftime("%Y-%m-%d")

    raw_html = fetch_today_html()
    if not raw_html:
        print("No Techpresso issue found today.")
        return

    translated_html = translate_html_preserve_layout(raw_html)
    pdf_path = html_to_pdf(translated_html, date_str)
    send_email(pdf_path, date_str)

    print("Done:", pdf_path)


if __name__ == "__main__":
    main()
