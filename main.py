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

# ✅ 0이면 당일, -1이면 하루 전, -2이면 이틀 전...
ISSUE_OFFSET_DAYS = int(os.getenv("ISSUE_OFFSET_DAYS", "0"))

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
    "AI Academy",
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
        try:
            text = tag.get_text(" ", strip=True)
        except Exception:
            continue

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


def _find_ancestor_tag(node, names):
    """
    node에서 시작해서 부모로 올라가며 names 중 하나인 태그를 찾는다.
    bs4의 find_parent()를 안 쓰고, 직접 parent 체인을 타서
    NavigableString parent 관련 예외를 근본적으로 회피한다.
    """
    try:
        cur = getattr(node, "parent", None)
    except Exception:
        return None

    steps = 0
    while cur is not None and steps < 30:
        try:
            if getattr(cur, "name", None) in names:
                return cur
            cur = getattr(cur, "parent", None)
        except Exception:
            return None
        steps += 1
    return None


def _remove_blocks_containing_keywords_safely(soup: BeautifulSoup, keywords):
    """
    keywords가 포함된 블록을 삭제하되,
    div/section 우선 삭제, table은 작은 경우만 삭제.
    find_parent() 대신 안전한 parent 체인 탐색으로 예외 방지.
    """
    nodes = list(soup.find_all(string=True))  # 스냅샷

    removed = 0
    for node in nodes:
        # ✅ NavigableString만 취급 (일반 str은 parent가 없음)
        if not isinstance(node, NavigableString):
            continue

        # node가 이미 분리된 경우가 있어서, 텍스트만 안전하게 읽기
        try:
            node_text = str(node)
        except Exception:
            continue

        if not _text_has_any(node_text, keywords):
            continue

        # 1) div/section 우선
        container = _find_ancestor_tag(node, {"div", "section"})
        if container:
            try:
                txt = container.get_text(" ", strip=True)
            except Exception:
                txt = ""
            if txt and len(txt) <= 6000:
                try:
                    container.decompose()
                    removed += 1
                except Exception:
                    pass
                continue

        # 2) table(짧을 때만)
        table = _find_ancestor_tag(node, {"table"})
        if table:
            try:
                txt = table.get_text(" ", strip=True)
            except Exception:
                txt = ""
            if txt and len(txt) <= 3500:
                try:
                    table.decompose()
                    removed += 1
                except Exception:
                    pass
                continue

        # 3) fallback: 주변 작은 태그만 제거
        try:
            parent = getattr(node, "parent", None)
        except Exception:
            parent = None

        if parent and getattr(parent, "name", None) in ("p", "h1", "h2", "h3", "h4", "td"):
            try:
                parent.decompose()
                removed += 1
            except Exception:
                pass

    if removed:
        print("Blocks removed by keywords:", removed)


# ----------------------
# ✅ 첫번째 FROM OUR PARTNER: "FROM OUR PARTNER" ~ "첫 이모지" 직전까지 문자열로 안전 컷
# ----------------------
# 대부분의 뉴스 이모지: 🚀💥📱📈🖥️📚🎁🧰 등
EMOJI_RE = re.compile(
    "["  # wide unicode ranges
    "\U0001F300-\U0001FAFF"  # emoticons, symbols, transport, supplemental symbols
    "\U00002700-\U000027BF"  # dingbats
    "\U00002600-\U000026FF"  # misc symbols
    "]+"
)

def strip_first_partner_until_emoji(html: str) -> tuple[str, int]:
    """
    첫 번째 'FROM OUR PARTNER' 시작 지점부터,
    그 이후 처음 등장하는 이모지(🚀💥📱...) 바로 직전까지를 통째로 삭제.
    - HTML 태그를 반쯤 잘라먹지 않도록 'FROM OUR PARTNER'가 들어있는 <b>/<strong>/<h*> 시작점으로 당겨서 컷
    - 이모지가 없으면 아무것도 안 함
    """
    if not html:
        return html, 0

    low = html.lower()
    key = "from our partner"
    kpos = low.find(key)
    if kpos < 0:
        return html, 0

    m = EMOJI_RE.search(html, kpos)
    if not m:
        return html, 0

    emoji_pos = m.start()

    # 컷 시작점을 '<b' 또는 '<strong' 또는 '<h'로 당겨서, 태그 중간 컷 방지
    start_candidates = []
    for token in ("<b", "<strong", "<h"):
        p = low.rfind(token, 0, kpos)
        if p != -1 and (kpos - p) < 300:
            start_candidates.append(p)

    start_pos = max(start_candidates) if start_candidates else kpos

    new_html = html[:start_pos] + html[emoji_pos:]
    return new_html, 1


def _force_left_align_around_first_story(soup: BeautifulSoup):
    """
    파트너 영역 삭제 후 첫 기사(이모지로 시작하는 td)가
    가운데로 밀리는 현상 방지:
    - 첫 이모지를 포함한 td와 그 조상들의 center 정렬을 left로 리셋
    """
    first_emoji_node = None
    for s in soup.find_all(string=True):
        if not isinstance(s, NavigableString):
            continue
        if EMOJI_RE.search(str(s)):
            first_emoji_node = s
            break

    if not first_emoji_node:
        return

    td = first_emoji_node.find_parent("td") if hasattr(first_emoji_node, "find_parent") else None
    if td:
        style = td.get("style", "")
        if "text-align" not in style.lower():
            td["style"] = (style + "; text-align:left;").strip(";")
        else:
            td["style"] = re.sub(r"text-align\s*:\s*center", "text-align:left", style, flags=re.I)

    # 조상 쪽에 center/align=center 있으면 제거/덮어쓰기
    cur = td if td else getattr(first_emoji_node, "parent", None)
    steps = 0
    while cur is not None and getattr(cur, "name", None) and steps < 10:
        if cur.has_attr("align") and str(cur["align"]).lower() == "center":
            del cur["align"]

        if cur.has_attr("style"):
            st = cur["style"]
            st2 = re.sub(r"text-align\s*:\s*center\s*;?", "", st, flags=re.I)
            st2 = st2.strip()
            if st2 != st:
                cur["style"] = st2

        cur = cur.parent
        steps += 1


# ----------------------
# URL 표시 제거 + 링크 유지 번역
# ----------------------
URL_RE = re.compile(r"(https?://\S+|www\.\S+)", re.IGNORECASE)


def remove_visible_urls(soup: BeautifulSoup):
    for node in list(soup.find_all(string=True)):
        if not isinstance(node, NavigableString):
            continue

        parent = node.parent.name if getattr(node, "parent", None) else ""
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
    translated_nodes = 0

    for node in list(soup.find_all(string=True)):
        if not isinstance(node, NavigableString):
            continue

        parent_tag = node.parent.name if getattr(node, "parent", None) else ""
        if parent_tag in ("script", "style"):
            continue

        # ✅ bold/strong(도구명/고유명사)은 번역 제외
        if parent_tag in ("strong", "b"):
            continue

        text = str(node)
        if not text.strip():
            continue

        if URL_RE.search(text):
            text = URL_RE.sub("", text)

        if len(re.findall(r"[A-Za-z]", text)) < 2:
            continue

        if len(text) > 2000:
            continue

        translated = translate_text(text)
        if translated is None:
            continue

        node.replace_with(translated)
        translated_nodes += 1

    print("Translated text nodes:", translated_nodes)


def translate_html_preserve_layout(raw_html: str, date_str: str) -> str:
    # ✅ 0) 문자열 단계에서 "첫 FROM OUR PARTNER ~ 첫 이모지" 컷
    pre_html, cut = strip_first_partner_until_emoji(raw_html)
    if cut:
        print("Main partner ad removed (until emoji):", cut)

    soup = BeautifulSoup(pre_html, "html.parser")

    # 1) 헤더/푸터 제거
    _remove_techpresso_header_footer_safely(soup)

    # 2) 기타 파트너 섹션 삭제(남아있는 FROM OUR PARTNER 블록들)
    _remove_blocks_containing_keywords_safely(soup, PARTNER_KEYWORDS)

    # 3) AI Academy 섹션 삭제
    _remove_blocks_containing_keywords_safely(soup, REMOVE_SECTION_KEYWORDS)

    # 4) 광고 제거
    for ad in soup.select("[data-testid='ad'], .sponsor, .advertisement"):
        ad.decompose()

    # 5) 브랜딩 치환 (Techpresso -> OneSip)
    _replace_brand_everywhere(soup, BRAND_FROM, BRAND_TO)

    # 6) URL 텍스트 노출 제거
    remove_visible_urls(soup)

    # ✅ 7) 파트너 컷 이후 첫 기사 정렬 깨짐 방지(왼쪽 정렬 강제)
    _force_left_align_around_first_story(soup)

    # 8) 텍스트 노드만 번역
    translate_text_nodes_inplace(soup)

    out_html = str(soup)

    # fallback: 본문이 너무 짧으면(= 과삭제) 헤더/푸터 제거만 제외하고 재시도
    text_len = len(BeautifulSoup(out_html, "html.parser").get_text(" ", strip=True))
    if text_len < 200:
        print("WARNING: HTML too small after cleanup. Falling back without header/footer removal.")
        soup2 = BeautifulSoup(pre_html, "html.parser")

        _remove_blocks_containing_keywords_safely(soup2, PARTNER_KEYWORDS)
        _remove_blocks_containing_keywords_safely(soup2, REMOVE_SECTION_KEYWORDS)

        for ad in soup2.select("[data-testid='ad'], .sponsor, .advertisement"):
            ad.decompose()

        _replace_brand_everywhere(soup2, BRAND_FROM, BRAND_TO)
        remove_visible_urls(soup2)
        _force_left_align_around_first_story(soup2)
        translate_text_nodes_inplace(soup2)

        out_html = str(soup2)

    if DEBUG_DUMP_HTML:
        with open(f"debug_onesip_inner_{date_str}.html", "w", encoding="utf-8") as f:
            f.write(out_html)
        print("Wrote debug inner HTML:", f"debug_onesip_inner_{date_str}.html")

    return out_html


# ======================
# PDF용 HTML 래핑 + CSS
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
# RSS → 특정 날짜 HTML 추출
# ======================
def fetch_issue_html(target_date_kst):
    feed = feedparser.parse(RSS_URL)

    for e in feed.entries:
        if not hasattr(e, "published_parsed"):
            continue

        published_utc = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        published_kst_date = published_utc.astimezone(KST).date()

        if published_kst_date == target_date_kst and "content" in e and e.content:
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

    target_date = now_kst().date() + timedelta(days=ISSUE_OFFSET_DAYS)
    date_str = target_date.strftime("%Y-%m-%d")

    print(f"Target issue date (KST): {date_str} offset: {ISSUE_OFFSET_DAYS}")

    raw_html = fetch_issue_html(target_date)
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
