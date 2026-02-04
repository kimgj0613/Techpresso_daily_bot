import os
import re
import time
import feedparser
from datetime import datetime, timezone
from dateutil import tz
from bs4 import BeautifulSoup
from weasyprint import HTML
import smtplib, ssl
from email.message import EmailMessage
import deepl


# ======================
# 기본 설정
# ======================
RSS_URL = os.getenv("RSS_URL", "https://rss.beehiiv.com/feeds/ez2zQOMePQ.xml")
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY")
DEEPL_SERVER_URL = os.getenv("DEEPL_SERVER_URL", "https://api-free.deepl.com")  # Free 기본

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))

KST = tz.gettz("Asia/Seoul")

translator = None
if DEEPL_API_KEY:
    translator = deepl.Translator(DEEPL_API_KEY, server_url=DEEPL_SERVER_URL)


# ======================
# 유틸
# ======================
def now_kst():
    return datetime.now(tz=KST)


def is_sunday_kst():
    return now_kst().weekday() == 6


def safe_print_deepl_usage():
    """DeepL 사용량 로그 (네트워크 이슈로 실패해도 전체 실행은 계속)"""
    if not translator:
        return
    try:
        usage = translator.get_usage()
        # usage.character.count / usage.character.limit
        print(f"DeepL usage: {usage.character.count}/{usage.character.limit}")
    except Exception as e:
        print("DeepL usage check failed:", e)


# ======================
# DeepL 번역 (긴 텍스트 안전 처리)
# ======================
def _split_by_paragraph(text: str, max_chars: int = 4500):
    """
    DeepL 호출 안정성을 위해 문단 기준으로 쪼개기.
    (문단 하나가 너무 길면 강제 분할)
    """
    text = (text or "").strip()
    if not text:
        return []

    paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks = []
    buf = ""

    for p in paras:
        add = p + "\n\n"
        if len(buf) + len(add) <= max_chars:
            buf += add
        else:
            if buf.strip():
                chunks.append(buf.strip())
            # 문단 하나가 max_chars를 초과하면 강제 분할
            if len(add) > max_chars:
                for i in range(0, len(add), max_chars):
                    part = add[i : i + max_chars].strip()
                    if part:
                        chunks.append(part)
                buf = ""
            else:
                buf = add

    if buf.strip():
        chunks.append(buf.strip())

    return chunks


def translate_text(text: str, retries: int = 3) -> str:
    """
    - OpenAI/LibreTranslate 없이 DeepL만 사용
    - source_lang을 고정하지 않고 자동 감지 (혼합 텍스트/고유명사에 안정적)
    - 긴 텍스트는 문단 chunk로 나눠 번역
    """
    if not text or not text.strip():
        return text

    if translator is None:
        raise ValueError("DEEPL_API_KEY가 설정되지 않았습니다.")

    chunks = _split_by_paragraph(text, max_chars=4500)
    if not chunks:
        return text

    out_parts = []
    for ch in chunks:
        translated = None
        for i in range(retries):
            try:
                result = translator.translate_text(
                    ch,
                    target_lang="KO",
                    preserve_formatting=True,
                )
                translated = result.text
                break
            except Exception as e:
                print("DEEPL ERROR:", e)
                time.sleep(2 * (i + 1))

        out_parts.append(translated if translated is not None else ch)

    return "\n\n".join(out_parts)


# ======================
# HTML 번역 (레이아웃 유지, 링크 정보 최소 보존)
# ======================
def _append_link_hrefs_if_any(tag):
    """
    tag 내부에 a[href]가 있으면, 링크 텍스트만 남기게 되면서 URL이 사라지므로
    괄호로 URL을 덧붙여 정보 손실 최소화.
    예) "Read more" + (https://...)
    """
    links = []
    for a in tag.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if href and href not in links:
            links.append(href)
    return links


def translate_html_preserve_layout(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # ✅ 광고/스폰서/광고 영역 제거(있으면)
    for ad in soup.select("[data-testid='ad'], .sponsor, .advertisement"):
        ad.decompose()

    # 문단/리스트/헤더만 번역
    for tag in soup.find_all(["p", "li", "h1", "h2", "h3"]):
        # 공백 보존(인라인 태그 섞여도 단어가 붙지 않게)
        text = tag.get_text(" ", strip=True)

        # 너무 짧은 문장은 스킵
        if len(text) < 5:
            continue

        # 링크 URL 보존용
        hrefs = _append_link_hrefs_if_any(tag)

        translated = translate_text(text)

        # 링크 URL 덧붙이기(원하면 끌 수 있음)
        if hrefs:
            translated += "\n" + "\n".join([f"({u})" for u in hrefs])

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

        if published_kst == today and "content" in e and e.content:
            return e.content[0].value

    return None


# ======================
# PDF 생성
# ======================
def html_to_pdf(html: str, date_str: str):
    filename = f"Gmail - Techpresso_{date_str}.pdf"
    HTML(string=html).write_pdf(filename)
    return filename


# ======================
# 이메일 발송
# ======================
def send_email(pdf_path: str, date_str: str):
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    mail_from = os.getenv("MAIL_FROM")
    mail_to = os.getenv("MAIL_TO")

    missing = [k for k, v in {
        "SMTP_USER": smtp_user,
        "SMTP_PASS": smtp_pass,
        "MAIL_FROM": mail_from,
        "MAIL_TO": mail_to,
    }.items() if not v]
    if missing:
        raise ValueError(f"이메일 설정 환경변수가 비었습니다: {', '.join(missing)}")

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
            filename=os.path.basename(pdf_path),
        )

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)


# ======================
# 메인
# ======================
def main():
    if is_sunday_kst():
        print("Sunday – skipped")
        return

    # ✅ DeepL 사용량 로그(실패해도 계속 진행)
    safe_print_deepl_usage()

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
