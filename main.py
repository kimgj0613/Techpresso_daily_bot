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

# 디버그: GitHub Actions에서 HTML/PDF를 아티팩트로 보고 싶으면 1
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


def safe_print_deepl_usage(prefix="DeepL usage"):
    """DeepL 사용량 로그 (실패해도 전체 실행은 계속)"""
    if not translator:
        return
    try:
        usage = translator.get_usage()
        print(f"{prefix}: {usage.character.count}/{usage.character.limit}")
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
    ⚠️ 큰 부모 컨테이너를 날리면 본문까지 삭제될 수 있음.
    그래서 '키워드 포함 + 텍스트가 짧은 블록'만 제거.
    Beehiiv은 table 기반이 많아 table/tr/td도 포함.
    """
    candidates = soup.find_all(["header", "footer", "div", "section", "table", "tr", "td"])

    for tag in candidates:
        text = tag.get_text(" ", strip=True)
        if not text:
            continue

        kw = _match_keyword_count(text)
        if kw == 0:
            continue

        # 너무 긴 블록은 본문 포함 가능성이 커서 제거하지 않음
        if len(text) > 1600:
            continue

        # div/section/table/tr/td는 2개 이상 키워드일 때만 제거(오탐 방지)
        if tag.name in ["div", "section", "table", "tr", "td"]:
            if kw >= 2:
                tag.decompose()
        else:
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
        # ✅ 공백 보존 (strip=True만 쓰면 단어/URL이 붙어버려 레이아웃이 더 깨짐)
        text = tag.get_text(" ", strip=True)

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
    4) 결과가 너무 짧으면(본문 삭제) 제거 없이 다시 번역 fallback
    """
    soup = BeautifulSoup(html, "html.parser")

    _remove_techpresso_header_footer_safely(soup)

    # 광고 제거(있으면)
    for ad in soup.select("[data-testid='ad'], .sponsor, .advertisement"):
        ad.decompose()

    _replace_brand_everywhere(soup, BRAND_FROM, BRAND_TO)

    soup = _translate_blocks_in_soup(soup)
    out_html = str(soup)

    # 빈 페이지 방지 fallback
    text_len = len(BeautifulSoup(out_html, "html.parser").get_text(" ", strip=True))
    if text_len < 200:
        print("WARNING: HTML too small after cleanup. Falling back without header/footer removal.")
        soup2 = BeautifulSoup(html, "html.parser")
        for ad in soup2.select("[data-testid='ad'], .sponsor, .advertisement"):
            ad.decompose()
        _replace_brand_everywhere(soup2, BRAND_FROM, BRAND_TO)
        soup2 = _translate_blocks_in_soup(soup2)
        out_html = str(soup2)

    if DEBUG_DUMP_HTML:
        with open(f"debug_onesip_inner_{date_str}.html", "w", encoding="utf-8") as f:
            f.write(out_html)
        print("Wrote debug inner HTML:", f"debug_onesip_inner_{date_str}.html")

    return out_html


# ======================
# PDF용 HTML 래핑 + CSS (오른쪽 잘림/여백/스케일/한글 폰트)
# ======================
def wrap_html_for_pdf(inner_html: str) -> str:
    """
    ✅ 오른쪽 잘림 방지 핵심:
      - @page size(A4) + margin
      - 이미지/테이블/블록 max-width:100%
      - 긴 URL/단어 강제 줄바꿈
      - email HTML의 고정폭(600px+) 요소를 강제로 width:auto/100%로 눌러줌
    """
    css = """
    @page { size: A4; margin: 14mm; }

    html, body {
      margin: 0;
      padding: 0;
      width: 100%;
      font-family: "Noto Sans CJK KR", "Noto Sans KR", "Noto Sans", sans-serif;
      font-size: 11pt;
      line-height: 1.5;
      -webkit-text-size-adjust: 100%;
    }

    * { box-sizing: border-box; }

    /* 가장 흔한 잘림 원인: 고정폭 요소/이미지/테이블 */
    img, svg, video { max-width: 100% !important; height: auto !important; }
    table { width: 100% !important; max-width: 100% !important; border-collapse: collapse; }
    th, td { max-width: 100% !important; }

    div, section, article, main, header, footer {
      max-width: 100% !important;
      width: auto !important;
    }

    /* 긴 URL/긴 단어로 인한 가로 폭 튐 방지 */
    p, li, td, th, a, span {
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    /* email 본문이 너무 커 보이면 살짝 축소 (필요 시 0.92~0.98 조절) */
    .pdf-scale {
      transform: scale(0.96);
      transform-origin: top left;
      width: 104%;
    }

    /* 링크 표시 */
    a { text-decoration: none; }
    """

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>{css}</style>
</head>
<body>
  <div class="pdf-scale">
    {inner_html}
  </div>
</body>
</html>"""


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
def html_to_pdf(inner_html: str, date_str: str):
    filename = f"Gmail - OneSip_{date_str}.pdf"
    final_html = wrap_html_for_pdf(inner_html)

    if DEBUG_DUMP_HTML:
        with open(f"debug_onesip_pdf_{date_str}.html", "w", encoding="utf-8") as f:
            f.write(final_html)
        print("Wrote debug pdf HTML:", f"debug_onesip_pdf_{date_str}.html")

    HTML(string=final_html).write_pdf(filename)
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

    safe_print_deepl_usage("DeepL usage(before)")

    date_str = now_kst().strftime("%Y-%m-%d")

    raw_html = fetch_today_html()
    if not raw_html:
        print("No issue found today.")
        return

    translated_inner_html = translate_html_preserve_layout(raw_html, date_str)

    # 빈 페이지 방지
    final_text_len = len(BeautifulSoup(translated_inner_html, "html.parser").get_text(" ", strip=True))
    print("Final HTML text length:", final_text_len)
    if final_text_len < 200:
        raise RuntimeError("Final HTML seems empty. Aborting to avoid blank PDF.")

    pdf_path = html_to_pdf(translated_inner_html, date_str)

    # 번역 후 usage도 한 번 더
    safe_print_deepl_usage("DeepL usage(after)")

    send_email(pdf_path, date_str)
    print("Done:", pdf_path)


if __name__ == "__main__":
    main()
