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

MAIL_SUBJECT_PREFIX = "☕ OneSip | Today’s Tech in One Sip"
MAIL_BODY_LINE = "OneSip – Your daily tech clarity"

BRAND_FROM = "Techpresso"
BRAND_TO = "OneSip"

# 디버그: GitHub Actions 아티팩트로 확인하고 싶으면 1로 설정
DEBUG_DUMP_HTML = os.getenv("DEBUG_DUMP_HTML", "0") == "1"

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
    """DeepL 사용량 로그 (실패해도 전체 실행은 계속)"""
    if not translator:
        return
    try:
        usage = translator.get_usage()
        print(f"DeepL usage: {usage.character.count}/{usage.character.limit}")
    except Exception as e:
        print("DeepL usage check failed:", e)


# ======================
# DeepL 번역 (긴 텍스트 안정 처리)
# ======================
def _split_by_paragraph(text: str, max_chars: int = 4500):
    text = (text or "").strip()
    if not text:
        return []

    paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks, buf = [], ""

    for p in paras:
        add = p + "\n\n"
        if len(buf) + len(add) <= max_chars:
            buf += add
        else:
            if buf.strip():
                chunks.append(buf.strip())
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
# HTML 정리/브랜딩/번역
# ======================
REMOVE_KEYWORDS = [
    "Join Free",
    "Upgrade",
    "Together with",
    "this is your daily",
    "Not subscribed to",
    "Subscribe for free",
    "Advertise",
    "Feedback",
    "Read Online",
]


def _match_keyword_count(text: str) -> int:
    t = (text or "").lower()
    return sum(1 for k in REMOVE_KEYWORDS if k.lower() in t)


def _remove_techpresso_header_footer_safely(soup: BeautifulSoup):
    """
    ⚠️ 핵심: '큰 부모 컨테이너'를 날리면 본문까지 삭제되어 PDF가 빈 페이지가 됨.
    그래서 '키워드가 포함되면서 텍스트가 짧은 블록'만 제거한다.
    Beehiiv은 table 기반인 경우가 많아서 table/tr/td도 포함.
    """
    candidates = soup.find_all(["header", "footer", "div", "section", "table", "tr", "td"])

    for tag in candidates:
        text = tag.get_text(" ", strip=True)
        if not text:
            continue

        kw = _match_keyword_count(text)

        # 안전장치 1) 키워드가 아예 없으면 패스
        if kw == 0:
            continue

        # 안전장치 2) 너무 긴 블록(본문 포함 가능성)을 삭제하지 않음
        # 헤더/푸터는 보통 짧기 때문에 1200~2000 사이가 안전
        if len(text) > 1600:
            continue

        # 안전장치 3) div/section/table/tr/td는 최소 2개 이상 키워드가 잡힐 때만 삭제
        if tag.name in ["div", "section", "table", "tr", "td"]:
            if kw >= 2:
                tag.decompose()
        else:
            # header/footer는 1개만 잡혀도 삭제
            tag.decompose()


def _replace_brand_everywhere(soup: BeautifulSoup, old: str, new: str):
    for t in soup.find_all(string=True):
        if old in t:
            t.replace_with(t.replace(old, new))


def _append_link_hrefs_if_any(tag):
    links = []
    for a in tag.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if href and href not in links:
            links.append(href)
    return links


def _translate_blocks_in_soup(soup: BeautifulSoup) -> BeautifulSoup:
    for tag in soup.find_all(["p", "li", "h1", "h2", "h3"]):
        text = tag.get_text(" ", strip=True)  # 공백 보존

        if len(text) < 5:
            continue

        hrefs = _append_link_hrefs_if_any(tag)
        translated = translate_text(text)

        if hrefs:
            translated += "\n" + "\n".join([f"({u})" for u in hrefs])

        tag.clear()
        tag.append(translated)

    return soup


def translate_html_preserve_layout(html: str, date_str: str) -> str:
    """
    1) 안전하게 헤더/푸터 제거
    2) 브랜딩 치환
    3) 번역
    4) 만약 결과가 비면(=본문이 삭제됨) 제거 없이 다시 번역하는 fallback
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1) 헤더/푸터 안전 제거
    _remove_techpresso_header_footer_safely(soup)

    # 2) 광고 제거(있으면)
    for ad in soup.select("[data-testid='ad'], .sponsor, .advertisement"):
        ad.decompose()

    # 3) 브랜딩 치환
    _replace_brand_everywhere(soup, BRAND_FROM, BRAND_TO)

    # 4) 번역
    soup = _translate_blocks_in_soup(soup)
    out_html = str(soup)

    # ---- 빈 페이지 방지: 본문이 사실상 사라졌으면 fallback ----
    # (p/li/h1~h3가 거의 없거나, 결과 텍스트가 너무 짧으면 실패로 간주)
    text_len = len(BeautifulSoup(out_html, "html.parser").get_text(" ", strip=True))
    if text_len < 200:
        print("WARNING: HTML content too small after cleanup. Falling back without header/footer removal.")

        soup2 = BeautifulSoup(html, "html.parser")
        # 광고만 제거
        for ad in soup2.select("[data-testid='ad'], .sponsor, .advertisement"):
            ad.decompose()
        _replace_brand_everywhere(soup2, BRAND_FROM, BRAND_TO)
        soup2 = _translate_blocks_in_soup(soup2)
        out_html = str(soup2)

    # 디버그용 HTML 덤프
    if DEBUG_DUMP_HTML:
        with open(f"debug_onesip_{date_str}.html", "w", encoding="utf-8") as f:
            f.write(out_html)
        print("Wrote debug HTML:", f"debug_onesip_{date_str}.html")

    return out_html


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
    filename = f"Gmail - OneSip_{date_str}.pdf"
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
    msg["Subject"] = f"{MAIL_SUBJECT_PREFIX} ({date_str})"
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.set_content(f"{MAIL_BODY_LINE}\n\n오늘의 한글 번역본을 첨부합니다.")

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

    safe_print_deepl_usage()

    date_str = now_kst().strftime("%Y-%m-%d")

    raw_html = fetch_today_html()
    if not raw_html:
        print("No issue found today.")
        return

    translated_html = translate_html_preserve_layout(raw_html, date_str)

    # 추가 안전장치: 최종 HTML 텍스트가 너무 짧으면 중단(빈 PDF 방지)
    final_text_len = len(BeautifulSoup(translated_html, "html.parser").get_text(" ", strip=True))
    print("Final HTML text length:", final_text_len)
    if final_text_len < 200:
        raise RuntimeError("Final HTML seems empty. Aborting to avoid blank PDF.")

    pdf_path = html_to_pdf(translated_html, date_str)
    send_email(pdf_path, date_str)

    print("Done:", pdf_path)


if __name__ == "__main__":
    main()
