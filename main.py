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

# 번역에서 절대 건드리면 안 되는 단어(브랜드/고유명사)
PROTECT_TERMS = ["OneSip"]

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
    if not translator:
        return
    try:
        usage = translator.get_usage()
        print(f"{prefix}: {usage.character.count}/{usage.character.limit}")
    except Exception as e:
        print("DeepL usage check failed:", e)


# ======================
# 번역 보호(placeholder)
# ======================
def protect_terms(text: str):
    """
    OneSip 같은 단어가 번역되지 않게 placeholder로 바꾸고,
    번역 후 다시 되돌릴 수 있게 매핑을 반환한다.
    """
    if not text:
        return text, {}

    mapping = {}
    out = text

    for term in PROTECT_TERMS:
        placeholder = f"__PROTECT_{re.sub(r'[^A-Za-z0-9]', '', term).upper()}__"
        if term in out:
            out = out.replace(term, placeholder)
            mapping[placeholder] = term

    return out, mapping


def restore_terms(text: str, mapping: dict):
    if not text or not mapping:
        return text
    out = text
    for ph, term in mapping.items():
        out = out.replace(ph, term)
    return out


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

    # ✅ OneSip 보호
    protected, mapping = protect_terms(text)

    chunks = _split_by_paragraph(protected, max_chars=4500)
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

    joined = "\n\n".join(out_parts)
    # ✅ placeholder 원복
    return restore_terms(joined, mapping)


# ======================
# HTML 제거/브랜딩/번역
# ======================
REMOVE_KEYWORDS_HEADER_FOOTER = [
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

# (3) 삭제하고 싶은 섹션: Want to master...
REMOVE_SECTION_KEYWORDS = [
    "Want to master the AI tools we cover every day?",
    "매일 다루는 AI 도구를 마스터하고 싶으신가요?",
    "AI 아카데미",  # 혹시 카피가 바뀌어도 잡히도록
]

# (4) 파트너 섹션 전체 삭제
PARTNER_KEYWORDS = [
    "FROM OUR PARTNER",
]


def _text_has_any(text: str, keywords):
    t = (text or "").lower()
    return any(k.lower() in t for k in keywords)


def _remove_blocks_containing_keywords(soup: BeautifulSoup, keywords, strict=False):
    """
    keywords를 포함하는 블록(컨테이너)을 통째로 제거.
    - strict=False: 과하게 큰 부모를 제거하지 않도록 안전장치 포함
    """
    # 키워드가 포함된 "문자열 노드"를 찾고, 그 부모 컨테이너를 위로 타고 올라가 제거
    for node in list(soup.find_all(string=True)):
        if not isinstance(node, str):
            continue
        if not _text_has_any(node, keywords):
            continue

        # 우선 table 기반이면 table이 가장 안전 (뉴스레터 레이아웃이 table인 경우가 많음)
        container = node.find_parent(["table", "section", "div", "tr", "td"])
        if not container:
            continue

        # 너무 큰 걸 날리면 본문까지 같이 날아갈 수 있으니 안전장치
        text = container.get_text(" ", strip=True)
        if not text:
            continue

        if not strict:
            # 파트너/아카데미 블록은 보통 짧음. 9000 넘어가면 본문 포함 가능성 ↑
            if len(text) > 9000:
                # 한 단계 더 작은 쪽을 시도
                smaller = node.find_parent(["tr", "td", "div", "section"])
                if smaller and len(smaller.get_text(" ", strip=True)) < len(text):
                    container = smaller
                else:
                    continue

        container.decompose()


def _match_keyword_count(text: str, keywords) -> int:
    t = (text or "").lower()
    return sum(1 for k in keywords if k.lower() in t)


def _remove_techpresso_header_footer_safely(soup: BeautifulSoup):
    candidates = soup.find_all(["header", "footer", "div", "section", "table", "tr", "td"])
    for tag in candidates:
        text = tag.get_text(" ", strip=True)
        if not text:
            continue
        kw = _match_keyword_count(text, REMOVE_KEYWORDS_HEADER_FOOTER)
        if kw == 0:
            continue
        if len(text) > 1600:
            continue
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


def _is_leaf_text_container(tag) -> bool:
    """
    Trending tools가 p/li가 아니라 div/span/td에 박혀있는 경우가 있어서
    'leaf 컨테이너'도 번역 대상으로 포함.
    """
    if tag.name not in ["div", "span", "td"]:
        return False
    # 자식에 구조 태그가 있으면 leaf가 아니라고 보고 제외
    if tag.find(["p", "li", "ul", "ol", "table", "img", "h1", "h2", "h3", "h4"]):
        return False
    # br 정도는 허용
    non_br_children = [c for c in tag.find_all(True, recursive=False) if c.name != "br"]
    return len(non_br_children) == 0


def _translate_blocks_in_soup(soup: BeautifulSoup) -> BeautifulSoup:
    # 기존 + h4 + leaf div/span/td까지 확대 (Trending tools 대응)
    for tag in soup.find_all(["p", "li", "h1", "h2", "h3", "h4", "div", "span", "td"]):
        if tag.name in ["div", "span", "td"] and not _is_leaf_text_container(tag):
            continue

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
    soup = BeautifulSoup(html, "html.parser")

    # 0) 헤더/푸터 제거(기존)
    _remove_techpresso_header_footer_safely(soup)

    # 1) (4) FROM OUR PARTNER 섹션 전체 삭제 (두 번 나오는 것 모두)  ✅
    _remove_blocks_containing_keywords(soup, PARTNER_KEYWORDS, strict=False)

    # 2) (3) Want to master... / AI Academy 섹션 삭제 ✅
    _remove_blocks_containing_keywords(soup, REMOVE_SECTION_KEYWORDS, strict=False)

    # 3) 광고 제거
    for ad in soup.select("[data-testid='ad'], .sponsor, .advertisement"):
        ad.decompose()

    # 4) 브랜딩 치환 (Techpresso -> OneSip)
    _replace_brand_everywhere(soup, BRAND_FROM, BRAND_TO)

    # 5) 번역 (Trending tools 포함되도록 확장)
    soup = _translate_blocks_in_soup(soup)
    out_html = str(soup)

    # fallback: 본문이 너무 짧으면 제거 없이 다시 번역
    text_len = len(BeautifulSoup(out_html, "html.parser").get_text(" ", strip=True))
    if text_len < 200:
        print("WARNING: HTML too small after cleanup. Falling back without header/footer removal.")
        soup2 = BeautifulSoup(html, "html.parser")

        # 파트너/아카데미 삭제는 유지
        _remove_blocks_containing_keywords(soup2, PARTNER_KEYWORDS, strict=False)
        _remove_blocks_containing_keywords(soup2, REMOVE_SECTION_KEYWORDS, strict=False)

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
# PDF용 HTML 래핑 + CSS (잘림 방지/여백/한글 폰트)
# ======================
def wrap_html_for_pdf(inner_html: str) -> str:
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

    img, svg, video { max-width: 100% !important; height: auto !important; }
    table { width: 100% !important; max-width: 100% !important; border-collapse: collapse; }
    th, td { max-width: 100% !important; }

    div, section, article, main, header, footer {
      max-width: 100% !important;
      width: auto !important;
    }

    p, li, td, th, a, span {
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .pdf-scale {
      transform: scale(0.96);
      transform-origin: top left;
      width: 104%;
    }
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

    final_text_len = len(BeautifulSoup(translated_inner_html, "html.parser").get_text(" ", strip=True))
    print("Final HTML text length:", final_text_len)
    if final_text_len < 200:
        raise RuntimeError("Final HTML seems empty. Aborting to avoid blank PDF.")

    pdf_path = html_to_pdf(translated_inner_html, date_str)

    safe_print_deepl_usage("DeepL usage(after)")

    send_email(pdf_path, date_str)
    print("Done:", pdf_path)


if __name__ == "__main__":
    main()
