import os
import re
import time
import json
import requests
import feedparser
from datetime import datetime, timedelta, timezone
from dateutil import tz
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas


RSS_URL_DEFAULT = "https://rss.beehiiv.com/feeds/ez2zQOMePQ.xml"
TRANSLATE_URL_DEFAULT = "https://libretranslate.com/translate"  # 무료 공개 엔드포인트(가끔 제한/지연 가능)

KST = tz.gettz("Asia/Seoul")


def kst_today_date() -> str:
    return datetime.now(tz=KST).strftime("%Y-%m-%d")


def is_sunday_kst() -> bool:
    return datetime.now(tz=KST).weekday() == 6  # Mon=0 ... Sun=6


def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"\s+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def libretranslate(text: str, source="en", target="ko", retries=3, backoff=2.0) -> str:
    """
    무료 공개 LibreTranslate 서버를 사용.
    실패하면 원문을 반환(전체 파이프라인이 멈추지 않게).
    """
    text = normalize_text(text)
    if not text:
        return ""

    url = os.getenv("TRANSLATE_URL", TRANSLATE_URL_DEFAULT)
    api_key = os.getenv("LIBRETRANSLATE_API_KEY", "")  # 필요 없을 수도 있음(서버 정책에 따라)
    payload = {
        "q": text,
        "source": source,
        "target": target,
        "format": "text",
    }
    if api_key:
        payload["api_key"] = api_key

    for i in range(retries):
        try:
            r = requests.post(url, json=payload, timeout=60)
            if r.status_code == 200:
                data = r.json()
                return data.get("translatedText", text)
            # 429/5xx 등은 재시도
        except requests.RequestException:
            pass
        time.sleep(backoff * (i + 1))

    return text  # 최후에는 원문 반환


def fetch_today_items():
    rss_url = os.getenv("RSS_URL", RSS_URL_DEFAULT)
    feed = feedparser.parse(rss_url)
    today = datetime.now(tz=KST).date()

    items = []
    for e in getattr(feed, "entries", []):
        # published_parsed가 있으면 날짜 필터
        pub_ok = True
        if hasattr(e, "published_parsed") and e.published_parsed:
            pub_dt_utc = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            pub_dt_kst = pub_dt_utc.astimezone(timezone(timedelta(hours=9))).date()
            pub_ok = (pub_dt_kst == today)

        if pub_ok:
            title = getattr(e, "title", "").strip()
            link = getattr(e, "link", "").strip()
            summary = getattr(e, "summary", "") or getattr(e, "description", "") or ""
            summary = re.sub(r"<[^>]+>", "", summary)  # 매우 단순한 HTML 태그 제거
            summary = normalize_text(summary)
            items.append({"title": title, "link": link, "text": summary})

    return items


def build_pdf(path: str, date_str: str, translated_items):
    # 한글 폰트 등록(ReportLab 기본 CID 폰트)
    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiMin-W3"))

    c = canvas.Canvas(path, pagesize=A4)
    width, height = A4
    margin = 50
    y = height - margin

    def draw_line(text, font_size=11, leading=16):
        nonlocal y
        c.setFont("HeiseiMin-W3", font_size)
        # 간단한 줄바꿈 처리
        max_chars = 90
        for line in text.split("\n"):
            line = line.rstrip()
            if not line:
                y -= leading
                continue
            while len(line) > max_chars:
                c.drawString(margin, y, line[:max_chars])
                line = line[max_chars:]
                y -= leading
                if y < margin:
                    c.showPage()
                    y = height - margin
                    c.setFont("HeiseiMin-W3", font_size)
            c.drawString(margin, y, line)
            y -= leading
            if y < margin:
                c.showPage()
                y = height - margin

    # Title
    draw_line(f"Techpresso 한국어 번역본", font_size=16, leading=22)
    draw_line(f"날짜: {date_str}", font_size=11, leading=18)
    draw_line("-" * 60, font_size=11, leading=18)

    for idx, item in enumerate(translated_items, start=1):
        draw_line(f"{idx}. {item['title_ko']}", font_size=13, leading=20)
        if item["link"]:
            draw_line(f"원문 링크: {item['link']}", font_size=10, leading=16)
        draw_line(item["text_ko"], font_size=11, leading=16)
        draw_line("-" * 60, font_size=11, leading=18)

    c.save()


def send_email_with_attachment(pdf_path: str, subject: str, body: str):
    import smtplib, ssl
    from email.message import EmailMessage

    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "465"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    mail_from = os.getenv("MAIL_FROM", smtp_user)
    mail_to = os.getenv("MAIL_TO")

    if not (smtp_user and smtp_pass and mail_to and mail_from):
        raise RuntimeError("Missing SMTP settings. Set SMTP_USER/SMTP_PASS/MAIL_FROM/MAIL_TO secrets.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.set_content(body)

    with open(pdf_path, "rb") as f:
        data = f.read()
    filename = os.path.basename(pdf_path)
    msg.add_attachment(data, maintype="application", subtype="pdf", filename=filename)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)


def main():
    # KST 일요일이면 종료(안전장치)
    if is_sunday_kst():
        print("Sunday KST: skipping.")
        return

    date_str = kst_today_date()
    items = fetch_today_items()

    if not items:
        print("No items found for today. Sending a notice email (optional).")
        # 원하면 빈 날에는 메일을 보내지 않도록 return 처리해도 됨.
        return

    translated_items = []
    for it in items:
        title_ko = libretranslate(it["title"], source="en", target="ko")
        # 원문 “그대로 번역”이 목표라 summary/text를 전체 번역
        text_ko = libretranslate(it["text"], source="en", target="ko")
        translated_items.append({
            "title_ko": title_ko,
            "text_ko": text_ko,
            "link": it["link"],
        })

    pdf_name = f"Techpresso_{date_str}.pdf"
    pdf_path = os.path.join(os.getcwd(), pdf_name)
    build_pdf(pdf_path, date_str, translated_items)

    subject = f"Techpresso 한국어 번역 ({date_str})"
    body = "오늘의 Techpresso 한국어 번역 PDF를 첨부합니다."
    send_email_with_attachment(pdf_path, subject, body)

    print(f"Done. Sent {pdf_name} to {os.getenv('MAIL_TO')}.")


if __name__ == "__main__":
    main()
