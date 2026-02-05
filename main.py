import os
import re
import smtplib
import ssl
import time
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage

import deepl
import feedparser
from bs4 import BeautifulSoup, NavigableString
from dateutil import tz
from weasyprint import HTML


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

# ✅ 발행본 날짜 오프셋 (GitHub Variables로 제어)
#  0: 오늘(KST 기준), -1: 어제, -2: 그제 ...
ISSUE_OFFSET_DAYS_RAW = os.getenv("ISSUE_OFFSET_DAYS", "0").strip()

KST = tz.gettz("Asia/Seoul")

translator = None
if DEEPL_API_KEY:
    translator = deepl.Translator(DEEPL_API_KEY, server_url=DEEPL_SERVER_URL)


# ======================
# 유틸
# ======================
def now_kst():
    return datetime.now(tz=KST)


def get_issue_offset_days() -> int:
    raw = ISSUE_OFFSET_DAYS_RAW
    try:
        return int(raw)
    except ValueError:
        print("Invalid ISSUE_OFFSET_DAYS, using 0:", raw)
        return 0


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

REMOVE_SECTION_KEYWORDS = [
    "Want to master the AI tools we cover every day?",
    "매일 다루는 AI 도구를 마스터하고 싶으신가요?",
    "AI 아카데미",
]

PARTNER_KEYWORDS = [
    "FROM OUR PARTNER",
]


def _text_has_any(text: str, keywords):
    t = (text or "").lower()
    return any(k.lower() in t for k in keywords)


def _match_keyword_count(text: str, keywords) -> int:
    t = (text or "").lower()
    return sum(1 for k in keywords if k.lower() in t)


def _replace_brand_everywhere(soup: BeautifulSoup, old: str, new: str):
    for t in soup.find_all(string=True):
        if old in t:
            t.replace_with(t.replace(old, new))


def _remove_techpresso_header_footer_safely(soup: BeautifulSoup):
    """
    너무 큰 컨테이너를 날려서 본문이 사라지는 걸 줄이기 위해
    '짧은 블록' 위주로만 제거.
    """
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


def _remove_blocks_containing_keywords_safely(soup: BeautifulSoup, keywords):
    """
    keywords가 포함된 블록을 삭제하되,
    table/tr/td를 바로 지우면 다른 섹션까지 같이 날아갈 수 있어서
    기본은 div/section을 우선 삭제하고, table은 '작은' 경우에만 삭제.
    """
    for node in list(soup.find_all(string=True)):
        if not isinstance(node, str):
            continue
        if not _text_has_any(node, keywords):
            continue

        # 1) div/section 우선 (가장 안전)
        container = node.find_parent(["div", "section"])
        if container:
            txt = container.get_text(" ", strip=True)
            if txt and len(txt) <= 6000:
                container.decompose()
                continue

        # 2) 그래도 없으면 table(짧을 때만)
        table = node.find_parent("table")
        if table:
            txt = table.get_text(" ", strip=True)
            if txt and len(txt) <= 3500:
                table.decompose()
                continue

        # 3) 마지막 fallback: 해당 텍스트 노드 주변만 제거(과감한 삭제 방지)
        parent = node.parent
        if parent and parent.name in ("p", "h1", "h2", "h3", "h4", "td"):
            parent.decompose()


# ----------------------
# 1) 첫 번째 FROM OUR PARTNER(메인 광고 블록) 제거 - 구조 기반(가장 확실)
# ----------------------
def _remove_main_partner_ad_row(soup: BeautifulSoup) -> int:
    """
    Beehiiv 메인 광고 블록은 보통 아래 id들을 포함.
    이 id가 속한 td/tr(row)을 통째로 제거하면 광고 내용 변형(main-ad-copy 유무 등)에 안전.
    """
    selectors = [
        "#main-ad-title",
        "#main-ad-headline",
        "#main-ad-image-link",
        "#main-ad-image",
        "#main-ad-copy",
    ]

    removed = 0
    for sel in selectors:
        for node in list(soup.select(sel)):
            # 가능한 한 "row(td/tr)" 단위로 제거
            td = node.find_parent("td")
            tr = node.find_parent("tr")

            # td가 row 패딩(50px) 블록인 경우가 많음
            if tr:
                tr.decompose()
                removed += 1
                continue
            if td:
                td.decompose()
                removed += 1
                continue

            # 최후
            node.decompose()
            removed += 1

    return removed


# ----------------------
# 2) 요청한 방식: "첫 번째 FROM OUR PARTNER" 이후 첫 이모지 전까지 삭제(이모지부터는 살림)
# ----------------------
# 이모지(그림문자) 감지용: 대부분의 뉴스 헤더가 🚀/💻/📱 처럼 시작
EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U00002600-\U000026FF]"
)


def _remove_first_partner_until_first_emoji(soup: BeautifulSoup) -> int:
    """
    - FROM OUR PARTNER가 포함된 첫 블록을 찾고
    - 그 블록(보통 td.row 내부)에서 첫 이모지가 등장하는 지점 전까지를 삭제
    """
    # 1) 시작점 찾기: main-ad-title이 있으면 그게 1순위
    start = soup.find(id="main-ad-title")
    if not start:
        # 없으면 텍스트 기반
        for s in soup.find_all(string=True):
            if isinstance(s, str) and s.strip().upper() == "FROM OUR PARTNER":
                start = s.parent if getattr(s, "parent", None) else None
                break

    if not start:
        return 0

    container_td = start.find_parent("td")
    if not container_td:
        return 0

    # container_td 안에서 "첫 이모지" 찾기 (partner 구간에는 보통 이모지 없음)
    emoji_node = None
    for s in container_td.find_all(string=True):
        if not isinstance(s, str):
            continue
        if EMOJI_RE.search(s):
            emoji_node = s
            break

    # 이모지가 없다면: 통째로 제거
    if not emoji_node:
        tr = container_td.find_parent("tr")
        if tr:
            tr.decompose()
            return 1
        container_td.decompose()
        return 1

    # 이모지를 포함하는 “살릴 덩어리”의 최상위(컨테이너 td의 직접 자식) 찾기
    keep_tag = emoji_node.parent if getattr(emoji_node, "parent", None) else None
    if not keep_tag:
        return 0

    top = keep_tag
    while top.parent and top.parent != container_td:
        top = top.parent

    # container_td의 contents 앞부분부터 top 직전까지 삭제
    removed = 0
    for child in list(container_td.contents):
        if child == top:
            break
        # NavigableString이면 그냥 제거, Tag면 decompose
        try:
            child.extract()
        except Exception:
            try:
                child.decompose()
            except Exception:
                pass
        removed += 1

    return removed


# ----------------------
# URL 표시 제거 + 링크 유지 번역
# ----------------------
URL_RE = re.compile(r"(https?://\S+|www\.\S+)", re.IGNORECASE)


def remove_visible_urls(soup: BeautifulSoup):
    """
    '텍스트로 노출된 URL'만 제거해서 PDF에 URL이 보이지 않게.
    <a href="...">는 건드리지 않아서 링크는 유지됨.
    """
    for node in list(soup.find_all(string=True)):
        if not isinstance(node, NavigableString):
            continue

        parent = node.parent.name if node.parent else ""
        if parent in ("script", "style"):
            continue

        txt = str(node)
        if not txt.strip():
            continue

        if URL_RE.search(txt):
            cleaned = URL_RE.sub("", txt)
            cleaned = re.sub(r"\(\s*\)", "", cleaned)  # 빈 괄호 제거
            cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
            node.replace_with(cleaned)


def translate_text_nodes_inplace(soup: BeautifulSoup):
    """
    HTML 태그 구조는 그대로 유지하고, 텍스트 노드만 번역.
    => <a href> 링크 유지 + URL은 번역/표시하지 않음
    """
    translated_nodes = 0

    for node in list(soup.find_all(string=True)):
        if not isinstance(node, NavigableString):
            continue

        parent = node.parent.name if node.parent else ""
        if parent in ("script", "style"):
            continue

        # ✅ Trending tools 등에서 bold/strong(도구명/고유명사)은 번역 제외
        if parent in ("strong", "b"):
            continue

        text = str(node)
        if not text.strip():
            continue

        # URL이 텍스트로 들어있다면(혹시 남았으면) 번역 전에 제거
        if URL_RE.search(text):
            text = URL_RE.sub("", text)

        # 영어 알파벳이 거의 없으면 스킵(숫자/기호/이미 한글 위주)
        if len(re.findall(r"[A-Za-z]", text)) < 2:
            continue

        # 너무 긴 노드는 위험/비용 큼 → 스킵
        if len(text) > 2000:
            continue

        translated = translate_text(text)
        if translated is None:
            continue

        node.replace_with(translated)
        translated_nodes += 1

    print("Translated text nodes:", translated_nodes)


def translate_html_preserve_layout(html: str, date_str: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # 0) 헤더/푸터 제거
    _remove_techpresso_header_footer_safely(soup)

    # 1) 메인 파트너 광고 제거(구조 기반)
    removed_struct = _remove_main_partner_ad_row(soup)
    if removed_struct:
        print("Main partner ad removed (structure):", removed_struct)

    # 2) 그래도 남아있으면(변형 대비) "FROM OUR PARTNER ~ 첫 이모지 전" 컷
    removed_emoji = _remove_first_partner_until_first_emoji(soup)
    if removed_emoji:
        print("Main partner ad removed (until emoji):", removed_emoji)

    # 3) 파트너 섹션(기타) 삭제(키워드 기반)
    _remove_blocks_containing_keywords_safely(soup, PARTNER_KEYWORDS)

    # 4) AI Academy 섹션 삭제
    _remove_blocks_containing_keywords_safely(soup, REMOVE_SECTION_KEYWORDS)

    # 5) 광고 제거(기타 셀렉터)
    for ad in soup.select("[data-testid='ad'], .sponsor, .advertisement"):
        ad.decompose()

    # 6) 브랜딩 치환
    _replace_brand_everywhere(soup, BRAND_FROM, BRAND_TO)

    # 7) URL 텍스트 제거
    remove_visible_urls(soup)

    # 8) 텍스트 노드 번역(링크 유지)
    translate_text_nodes_inplace(soup)

    out_html = str(soup)

    # fallback: 본문이 너무 짧으면 헤더/푸터 제거 없이 다시(파트너/아카데미는 유지)
    text_len = len(BeautifulSoup(out_html, "html.parser").get_text(" ", strip=True))
    if text_len < 200:
        print("WARNING: HTML too small after cleanup. Falling back without header/footer removal.")
        soup2 = BeautifulSoup(html, "html.parser")

        _remove_main_partner_ad_row(soup2)
        _remove_first_partner_until_first_emoji(soup2)
        _remove_blocks_containing_keywords_safely(soup2, PARTNER_KEYWORDS)
        _remove_blocks_containing_keywords_safely(soup2, REMOVE_SECTION_KEYWORDS)

        for ad in soup2.select("[data-testid='ad'], .sponsor, .advertisement"):
            ad.decompose()

        _replace_brand_everywhere(soup2, BRAND_FROM, BRAND_TO)
        remove_visible_urls(soup2)
        translate_text_nodes_inplace(soup2)

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
# RSS → 대상 날짜 HTML 추출 (ISSUE_OFFSET_DAYS)
# ======================
def fetch_target_html():
    feed = feedparser.parse(RSS_URL)

    offset = get_issue_offset_days()  # 0, -1, -2 ...
    target_date = now_kst().date() + timedelta(days=offset)

    print("ISSUE_OFFSET_DAYS:", offset)
    print("Target issue date (KST):", target_date)

    for e in feed.entries:
        if not hasattr(e, "published_parsed"):
            continue

        published_utc = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        published_kst = published_utc.astimezone(KST).date()

        if published_kst == target_date and "content" in e and e.content:
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

    missing = [
        k
        for k, v in {
            "SMTP_USER": smtp_user,
            "SMTP_PASS": smtp_pass,
            "MAIL_FROM": mail_from,
            "MAIL_TO": mail_to,
        }.items()
        if not v
    ]
    if missing:
        raise ValueError(f"이메일 설정 환경변수가 비었습니다: {', '.join(missing)}")

    msg = EmailMessage()
    msg["Subject"] = f"{MAIL_SUBJECT_PREFIX} ({date_str})"
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.set_content(
        f"{MAIL_BODY_LINE}\n\n"
        "오늘의 Tech Issue를 OneSip으로 담았습니다.\n"
        "가볍게 읽어보시고 하루를 시작해보세요 ☕️"
    )

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
    safe_print_deepl_usage("DeepL usage(before)")

    date_str = now_kst().strftime("%Y-%m-%d")

    raw_html = fetch_target_html()
    if not raw_html:
        print("No issue found for target date.")
        return

    translated_inner_html = translate_html_preserve_layout(raw_html, date_str)

    final_text_len = len(
        BeautifulSoup(translated_inner_html, "html.parser").get_text(" ", strip=True)
    )
    print("Final HTML text length:", final_text_len)

    if final_text_len < 200:
        raise RuntimeError("Final HTML seems empty. Aborting to avoid blank PDF.")

    pdf_path = html_to_pdf(translated_inner_html, date_str)

    safe_print_deepl_usage("DeepL usage(after)")

    send_email(pdf_path, date_str)
    print("Done:", pdf_path)


if __name__ == "__main__":
    main()
