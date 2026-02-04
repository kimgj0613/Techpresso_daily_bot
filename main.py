import os
import re
import time
import feedparser
from datetime import datetime, timezone
from dateutil import tz
from bs4 import BeautifulSoup, NavigableString, Tag
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

PROTECT_TERMS = ["OneSip"]
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
# HTML 전처리/삭제/번역
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
    "AI Academy",
    "Try it free for 14 days",
    "매일 다루는 AI 도구를 마스터하고 싶으신가요?",
    "AI 아카데미",
]

PARTNER_KEYWORDS = [
    "FROM OUR PARTNER",
    # 가끔 변형이 있을 수 있어 백업 키워드도 추가
    "From our partner",
]


URL_RE = re.compile(r"(https?://\S+|www\.\S+)", re.IGNORECASE)


def _match_keyword_count(text: str, keywords) -> int:
    t = (text or "").lower()
    return sum(1 for k in keywords if k.lower() in t)


def _replace_brand_everywhere(soup: BeautifulSoup, old: str, new: str):
    for t in soup.find_all(string=True):
        if old in t:
            t.replace_with(t.replace(old, new))


def _remove_techpresso_header_footer_safely(soup: BeautifulSoup):
    candidates = soup.find_all(["header", "footer", "div", "section", "table", "tr", "td"])
    for tag in candidates:
        text = tag.get_text(" ", strip=True)
        if not text:
            continue
        kw = _match_keyword_count(text, REMOVE_KEYWORDS_HEADER_FOOTER)
        if kw == 0:
            continue
        # 너무 큰 덩어리는 위험(본문까지 날아감) → 스킵
        if len(text) > 1800:
            continue
        # 키워드가 최소 2개 이상일 때만 제거(오탐 방지)
        if kw >= 2:
            tag.decompose()


def _remove_blocks_by_keywords_smallest_container(soup: BeautifulSoup, keywords, max_container_len=9000):
    """
    keywords가 포함된 위치에서 td/tr/div/section/table 중 "가장 작은 컨테이너"를 골라 제거.
    큰 outer table을 날려서 본문이 통째로 사라지는 문제를 방지.
    """
    lowered = [k.lower() for k in keywords]

    def has_kw(s: str) -> bool:
        t = (s or "").lower()
        return any(k in t for k in lowered)

    removed = 0
    for node in list(soup.find_all(string=True)):
        if not isinstance(node, str):
            continue
        if not has_kw(node):
            continue

        candidates = []
        for name in ["td", "tr", "div", "section", "table"]:
            anc = node.find_parent(name)
            if not anc:
                continue
            txt = anc.get_text(" ", strip=True)
            if not txt:
                continue
            # 너무 큰 컨테이너는 "메일 전체"일 가능성 ↑
            if len(txt) > max_container_len:
                continue
            candidates.append((len(txt), anc))

        if not candidates:
            # 그래도 없으면 텍스트 노드의 부모만 제거(최후의 수단)
            parent = node.parent
            if parent and getattr(parent, "decompose", None):
                parent.decompose()
                removed += 1
            continue

        # 가장 작은 블록을 삭제
        candidates.sort(key=lambda x: x[0])
        candidates[0][1].decompose()
        removed += 1

    return removed




def _remove_partner_sections_only(soup: BeautifulSoup):
    """Remove only 'FROM OUR PARTNER' blocks safely.

    Strategy:
    1) Find the text node containing 'FROM OUR PARTNER' (case-insensitive).
    2) Prefer removing the nearest *reasonable-sized* table wrapping that block.
       (Partner ads are typically table-wrapped in Beehiiv emails.)
    3) If the nearest table is too large (likely the whole email wrapper) or absent,
       remove the smallest reasonable container among tr/td/div/section.
    4) Repeat until no more partner blocks remain.
    """
    removed = 0

    def _parent_tag(node):
        p = getattr(node, "parent", None)
        return p if isinstance(p, Tag) else None

    def _text_len(tag: Tag) -> int:
        return len(tag.get_text(" ", strip=True) or "")

    while True:
        hit = None
        for node in list(soup.find_all(string=True)):
            txt = (str(node) or "").strip()
            if not txt:
                continue
            if "from our partner" in txt.lower():
                hit = node
                break

        if hit is None:
            break

        ptag = _parent_tag(hit)
        if ptag is None:
            break

        # 1) nearest table (preferred)
        table = ptag if ptag.name == "table" else ptag.find_parent("table")
        if table:
            tlen = _text_len(table)
            if 200 < tlen < 20000:
                table.decompose()
                removed += 1
                continue

        # 2) fallback: smallest reasonable container
        candidates = []
        for name in ("tr", "td", "div", "section"):
            anc = ptag if ptag.name == name else ptag.find_parent(name)
            if not anc:
                continue
            alen = _text_len(anc)
            if 200 < alen < 20000:
                candidates.append((alen, anc))

        if candidates:
            candidates.sort(key=lambda x: x[0])
            candidates[0][1].decompose()
            removed += 1
            continue

        # 3) last resort: remove immediate parent tag
        ptag.decompose()
        removed += 1

    print("Partner blocks removed:", removed)


def _remove_partner_sections(soup: BeautifulSoup):
    """
    파트너 섹션 전부 삭제:
    1) Beehiiv 광고 블록에서 자주 쓰는 id 기반 제거 (정확/안전)
    2) 'FROM OUR PARTNER' 텍스트 기반 제거 (smallest container 방식으로 안전하게)
    """
    # 1) 구조 기반 (있으면 제일 확실)
    id_selectors = [
        "#main-ad-title",
        "#spotlight-ad-title",
        "#spotlight-ad-block",
    ]
    for sel in id_selectors:
        for node in soup.select(sel):
            # 너무 큰 상위 table로 올라가지 않도록 td/tr부터 우선 제거
            td = node.find_parent("td")
            tr = node.find_parent("tr")
            table = node.find_parent("table")

            for target in [td, tr]:
                if target:
                    target.decompose()
                    break
            else:
                if table and len(table.get_text(" ", strip=True)) < 12000:
                    table.decompose()
                else:
                    node.decompose()

    # 2) 키워드 기반 (전부 제거)
    removed = _remove_blocks_by_keywords_smallest_container(soup, PARTNER_KEYWORDS, max_container_len=9000)
    print("Partner blocks removed:", removed)


def _remove_ai_academy_section(soup: BeautifulSoup):
    removed = _remove_blocks_by_keywords_smallest_container(soup, REMOVE_SECTION_KEYWORDS, max_container_len=9000)
    print("AI Academy blocks removed:", removed)


def remove_visible_urls(soup: BeautifulSoup):
    """
    텍스트로 노출된 URL만 제거해서 PDF에 URL이 보이지 않게.
    <a href> 속성은 유지되므로 링크는 클릭 가능.
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
            cleaned = re.sub(r"\(\s*\)", "", cleaned)
            cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
            node.replace_with(cleaned)


def translate_text_nodes_inplace(soup: BeautifulSoup):
    """
    HTML 태그 구조는 그대로 유지하고, 텍스트 노드만 번역.
    => <a href> 링크 유지 + URL은 표시/번역하지 않음
    """
    translated_nodes = 0

    for node in list(soup.find_all(string=True)):
        if not isinstance(node, NavigableString):
            continue

        parent = node.parent.name if node.parent else ""
        if parent in ("script", "style"):
            continue

        text = str(node)
        if not text.strip():
            continue

        # 텍스트 노드에 URL이 섞여 있으면 번역 전에 제거
        if URL_RE.search(text):
            text = URL_RE.sub("", text)

        # 영어 알파벳이 거의 없으면 스킵
        if len(re.findall(r"[A-Za-z]", text)) < 2:
            continue

        # 너무 긴 노드는 비용/실패 위험 → 스킵
        if len(text) > 2000:
            continue

        translated = translate_text(text)
        node.replace_with(translated)
        translated_nodes += 1

    print("Translated text nodes:", translated_nodes)


def translate_html_preserve_layout(html: str, date_str: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # 0) 헤더/푸터(상단/하단 구독 등) 제거
    _remove_techpresso_header_footer_safely(soup)

    # 1) 파트너 섹션 삭제 (FROM OUR PARTNER only, table-first)
    _remove_partner_sections_only(soup)

    # 2) AI Academy 섹션 삭제
    _remove_ai_academy_section(soup)

    # 3) 광고 셀렉터(있으면)
    for ad in soup.select("[data-testid='ad'], .sponsor, .advertisement"):
        ad.decompose()

    # 4) 브랜딩 치환
    _replace_brand_everywhere(soup, BRAND_FROM, BRAND_TO)

    # 5) URL은 화면에 보이지 않게(텍스트 URL 제거)
    remove_visible_urls(soup)

    # 6) 링크 구조 유지하며 텍스트만 번역
    translate_text_nodes_inplace(soup)

    out_html = str(soup)

    # fallback: 너무 작으면(삭제가 과했을 때) 헤더/푸터 제거만 빼고 재시도
    text_len = len(BeautifulSoup(out_html, "html.parser").get_text(" ", strip=True))
    if text_len < 200:
        print("WARNING: HTML too small after cleanup. Falling back without header/footer removal.")
        soup2 = BeautifulSoup(html, "html.parser")

        _remove_partner_sections(soup2)
        _remove_ai_academy_section(soup2)
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
# PDF용 HTML 래핑 + CSS (잘림 방지/여백/폰트)
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
