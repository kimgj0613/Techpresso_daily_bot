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
    - DeepL만 사용
    - source_lang 고정하지 않고 자동 감지(혼합 텍스트 안정)
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
# HTML 정리/브랜딩/번역
# ======================
def _remove_techpresso_header_footer(soup: BeautifulSoup):
    """
    Beehiiv 공통 헤더/푸터(Join Free/Upgrade/Subscribe 등) 제거.
    클래스가 자주 바뀌므로 텍스트 패턴 기반으로 제거.
    """

    # 제거 판단에 쓰는 키워드(대체로 헤더/푸터에만 존재)
    keywords = [
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
    kw_lower = [k.lower() for k in keywords]

    def match_score(text: str) -> int:
        t = (text or "").lower()
        score = 0
        for k in kw_lower:
            if k in t:
                score += 1
        return score

    # 1) header/footer 태그는 우선 제거 시도
    for tag in soup.find_all(["header", "footer"]):
        text = tag.get_text(" ", strip=True)
        if match_score(text) >= 1:
            tag.decompose()

    # 2) div/section 중에서도 헤더/푸터 블록이 포함된 컨테이너 제거
    #    오탐을 줄이기 위해 '2개 이상' 키워드 매칭일 때 제거
    for tag in soup.find_all(["div", "section"]):
        text = tag.get_text(" ", strip=True)
        if match_score(text) >= 2:
            tag.decompose()


def _replace_brand_everywhere(soup: BeautifulSoup, old: str, new: str):
    # 텍스트 노드 전체 치환
    for t in soup.find_all(string=True):
        if old in t:
            t.replace_with(t.replace(old, new))


def _append_link_hrefs_if_any(tag):
    """
    번역 과정에서 a 태그 구조가 사라질 수 있으므로 URL을 괄호로 덧붙여
    링크 정보 손실을 최소화.
    """
    links = []
    for a in tag.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if href and href not in links:
            links.append(href)
    return links


def translate_html_preserve_layout(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # (1) Beehiiv 공통 헤더/푸터 제거
    _remove_techpresso_header_footer(soup)

    # (2) 광고/스폰서/광고 영역 제거(있으면)
    for ad in soup.select("[data-testid='ad'], .sponsor, .advertisement"):
        ad.decompose()

    # (3) 브랜딩 치환 (Techpresso -> OneSip)
    _replace_brand_everywhere(soup, BRAND_FROM, BRAND_TO)

    # (4) 문단/리스트/헤더만 번역
    for tag in soup.find_all(["p", "li", "h1", "h2", "h3"]):
        text = tag.get_text(" ", strip=True)  # 공백 유지

        if len(text) < 5:
            continue

        # 링크 URL 보존
        hrefs = _append_link_hrefs_if_any(tag)

        translated = translate_text(text)

        # 링크 URL 덧붙이기
        if hrefs:
            translated += "\n" + "\n".join([f"({u})" for u in hrefs])

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

    translated_html = translate_html_preserve_layout(raw_html)
    pdf_path = html_to_pdf(translated_html, date_str)
    send_email(pdf_path, date_str)

    print("Done:", pdf_path)


if __name__ == "__main__":
    main()
