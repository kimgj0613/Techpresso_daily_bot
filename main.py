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

# ✅ 0이면 당일, -1이면 전날, -2이면 이틀 전...
ISSUE_OFFSET_DAYS = int(os.getenv("ISSUE_OFFSET_DAYS", "0"))

KST = tz.gettz("Asia/Seoul")

translator = None
if DEEPL_API_KEY:
    translator = deepl.Translator(DEEPL_API_KEY, server_url=DEEPL_SERVER_URL)


# ======================
# 유틸
# ======================
def now_kst():
    return datetime.now(tz=KST)


def get_target_issue_date_kst() -> datetime.date:
    return (now_kst().date() + timedelta(days=ISSUE_OFFSET_DAYS))


def safe_print_deepl_usage(prefix="DeepL usage"):
    if not translator:
        return
    try:
        usage = translator.get_usage()
        print(f"{prefix}: {usage.character.count}/{usage.character.limit}")
    except Exception as e:
        print("DeepL usage check failed:", e)


def _safe_find_parent(node, names):
    """
    BeautifulSoup 노드가 분리되었거나( decompose 이후 ),
    일부 환경에서 NavigableString parent 접근 에러가 날 수 있어서
    find_parent는 무조건 안전하게 감싼다.
    """
    try:
        if hasattr(node, "find_parent"):
            return node.find_parent(names)
    except Exception:
        return None
    return None


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

        # div/section/table/tr/td는 너무 과감하면 본문까지 날아가서 kw>=2일 때만
        if tag.name in ["div", "section", "table", "tr", "td"]:
            if kw >= 2:
                tag.decompose()
        else:
            tag.decompose()


def _remove_blocks_containing_keywords_safely(soup: BeautifulSoup, keywords) -> int:
    """
    keywords가 포함된 블록을 삭제하되,
    table/tr/td를 바로 지우면 다른 섹션까지 같이 날아갈 수 있어서
    기본은 div/section을 우선 삭제하고, table은 '작은' 경우에만 삭제.
    """
    removed = 0
    for node in list(soup.find_all(string=True)):
        # NavigableString도 str의 subclass라서 그냥 str 검사만 하면 위험.
        if not isinstance(node, NavigableString):
            continue

        text = str(node)
        if not text.strip():
            continue
        if not _text_has_any(text, keywords):
            continue

        # 1) div/section 우선
        container = _safe_find_parent(node, ["div", "section"])
        if container:
            txt = container.get_text(" ", strip=True)
            if txt and len(txt) <= 6000:
                container.decompose()
                removed += 1
                continue

        # 2) table (짧을 때만)
        table = _safe_find_parent(node, "table")
        if table:
            txt = table.get_text(" ", strip=True)
            if txt and len(txt) <= 3500:
                table.decompose()
                removed += 1
                continue

        # 3) 마지막 fallback: 주변 문단/셀만 제거
        parent = getattr(node, "parent", None)
        if parent and getattr(parent, "name", "") in ("p", "h1", "h2", "h3", "h4", "td"):
            parent.decompose()
            removed += 1

    return removed


# ----------------------
# (핵심) 첫 번째 FROM OUR PARTNER 제거: "다음 첫 이모지" 전까지 삭제
# ----------------------
# 이모지 대략 범위(뉴스 헤더에 나오는 🚀💥📱📈🖥️📚🎁🧰 등 포함)
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"  # Misc Symbols and Pictographs
    "\U0001F600-\U0001F64F"  # Emoticons
    "\U0001F680-\U0001F6FF"  # Transport & Map
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
    "\U0001FA00-\U0001FAFF"  # Symbols and Pictographs Extended-A
    "\u2600-\u26FF"          # Misc symbols
    "\u2700-\u27BF"          # Dingbats
    "]+"
)


def _find_first_partner_marker_node(soup: BeautifulSoup):
    for s in soup.find_all(string=True):
        if not isinstance(s, NavigableString):
            continue
        if "from our partner" in str(s).lower():
            return s
    return None


def _find_first_emoji_node_after(start_node: NavigableString):
    """
    start_node 이후 문서 순서에서 처음 이모지가 포함된 텍스트 노드를 찾는다.
    """
    try:
        it = start_node.next_elements
    except Exception:
        return None

    for el in it:
        if not isinstance(el, NavigableString):
            continue
        t = str(el)
        if not t.strip():
            continue
        if EMOJI_RE.search(t):
            return el
    return None


def _remove_first_partner_until_emoji(soup: BeautifulSoup) -> int:
    """
    첫 번째 FROM OUR PARTNER 가 등장하면,
    다음 첫 이모지 텍스트 노드가 나올 때까지 DOM 상의 요소들을 제거한다.
    (이모지부터는 살린다)
    """
    marker = _find_first_partner_marker_node(soup)
    if not marker:
        return 0

    emoji_node = _find_first_emoji_node_after(marker)
    if not emoji_node:
        # 이모지를 못 찾으면 과감 삭제가 위험하니 제거 안 함
        return 0

    end_tag = _safe_find_parent(emoji_node, ["td", "div", "p", "h1", "h2", "h3", "h4", "section"])
    if not end_tag:
        return 0

    # start_tag는 marker가 속한 "적당히 작은" 컨테이너부터 잡는다.
    # (h4/td/div 순으로 시도)
    start_tag = _safe_find_parent(marker, ["h1", "h2", "h3", "h4", "td", "div", "section"])
    if not start_tag:
        return 0

    # end_tag의 조상은 제거 대상에서 제외(부모를 지우면 end_tag까지 같이 날아감)
    end_ancestors = set()
    cur = end_tag
    while cur is not None:
        end_ancestors.add(cur)
        cur = getattr(cur, "parent", None)

    removed = 0
    # start_tag부터 end_tag 직전까지, 문서 순서상 요소들을 모아서 제거
    to_kill = []
    for el in start_tag.next_elements:
        if el == end_tag:
            break
        if not hasattr(el, "name"):
            continue  # 문자열 등
        if el in end_ancestors:
            continue
        # html/body는 제외
        if getattr(el, "name", "") in ("html", "body"):
            continue
        to_kill.append(el)

    # start_tag 자체도 제거(단, end_tag의 조상이면 안 됨)
    if start_tag not in end_ancestors:
        to_kill.insert(0, start_tag)

    # 중복 제거(깊은 자식부터 제거되는 걸 막기 위해 고유화)
    seen = set()
    uniq = []
    for t in to_kill:
        if t in seen:
            continue
        seen.add(t)
        uniq.append(t)

    for t in uniq:
        try:
            t.decompose()
            removed += 1
        except Exception:
            pass

    return removed


def _fix_first_article_alignment(soup: BeautifulSoup):
    """
    첫 파트너 블록을 제거한 뒤, 첫 기사(이모지로 시작)가
    가운데 정렬처럼 보이는 현상을 완화하기 위해
    첫 이모지 헤더의 td/부모의 center 관련 속성을 제거하고 left로 강제.
    """
    # 첫 이모지 텍스트 노드 찾기
    first_emoji_str = None
    for s in soup.find_all(string=True):
        if not isinstance(s, NavigableString):
            continue
        t = str(s).strip()
        if not t:
            continue
        if EMOJI_RE.search(t):
            first_emoji_str = s
            break

    if not first_emoji_str:
        return

    td = _safe_find_parent(first_emoji_str, "td")
    if not td:
        return

    # td 및 상위 몇 단계에서 align/style의 center 제거
    cur = td
    for _ in range(5):
        if not cur or not hasattr(cur, "attrs"):
            break

        if cur.has_attr("align") and str(cur["align"]).lower() == "center":
            del cur["align"]

        style = cur.get("style", "")
        if style:
            style2 = re.sub(r"text-align\s*:\s*center\s*;?", "", style, flags=re.I)
            style2 = style2.strip()
            if style2:
                cur["style"] = style2
            else:
                if cur.has_attr("style"):
                    del cur["style"]

        cur = getattr(cur, "parent", None)

    # td는 left로 명시
    td_style = td.get("style", "")
    if "text-align" not in td_style.lower():
        td["style"] = (td_style + "; " if td_style else "") + "text-align: left !important;"


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

        parent = getattr(node, "parent", None)
        parent_name = parent.name if parent else ""
        if parent_name in ("script", "style"):
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

        parent = getattr(node, "parent", None)
        parent_name = parent.name if parent else ""
        if parent_name in ("script", "style"):
            continue

        # ✅ Trending tools 등에서 bold/strong(도구명/고유명사)은 번역 제외
        if parent_name in ("strong", "b"):
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

    # 1) 첫 번째 FROM OUR PARTNER: 다음 첫 이모지 전까지 제거
    removed_until_emoji = _remove_first_partner_until_emoji(soup)
    if removed_until_emoji:
        print("Main partner ad removed (until emoji):", removed_until_emoji)

    # 2) 기타 파트너 섹션 삭제(남아있는 FROM OUR PARTNER가 더 있으면)
    removed_partner_keywords = _remove_blocks_containing_keywords_safely(soup, PARTNER_KEYWORDS)
    if removed_partner_keywords:
        print("Blocks removed by keywords (partner):", removed_partner_keywords)

    # 3) AI Academy 섹션 삭제
    removed_ai = _remove_blocks_containing_keywords_safely(soup, REMOVE_SECTION_KEYWORDS)
    if removed_ai:
        print("Blocks removed by keywords (ai-academy):", removed_ai)

    # 4) 광고 제거
    for ad in soup.select("[data-testid='ad'], .sponsor, .advertisement"):
        try:
            ad.decompose()
        except Exception:
            pass

    # 5) 브랜딩 치환 (Techpresso -> OneSip)
    _replace_brand_everywhere(soup, BRAND_FROM, BRAND_TO)

    # 6) 첫 기사 얼라인 보정
    _fix_first_article_alignment(soup)

    # 7) URL을 PDF에 표시하지 않도록 텍스트 URL 제거
    remove_visible_urls(soup)

    # 8) 텍스트 노드만 번역
    translate_text_nodes_inplace(soup)

    out_html = str(soup)

    # fallback: 본문이 너무 짧으면(과삭제) -> partner 제거만 유지하고 헤더/푸터 제거는 풀어본다
    text_len = len(BeautifulSoup(out_html, "html.parser").get_text(" ", strip=True))
    if text_len < 200:
        print("WARNING: HTML too small after cleanup. Falling back without header/footer removal.")
        soup2 = BeautifulSoup(html, "html.parser")

        _remove_first_partner_until_emoji(soup2)
        _remove_blocks_containing_keywords_safely(soup2, PARTNER_KEYWORDS)
        _remove_blocks_containing_keywords_safely(soup2, REMOVE_SECTION_KEYWORDS)

        for ad in soup2.select("[data-testid='ad'], .sponsor, .advertisement"):
            try:
                ad.decompose()
            except Exception:
                pass

        _replace_brand_everywhere(soup2, BRAND_FROM, BRAND_TO)
        _fix_first_article_alignment(soup2)
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
# RSS → 타겟 날짜 HTML 추출
# ======================
def fetch_issue_html_for_date(target_date_kst):
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

    # ✅ 문구 세련되게
    msg.set_content(
        f"{MAIL_BODY_LINE}\n\n"
        f"오늘의 Tech Issue를 OneSip으로 담았습니다.\n"
        f"가볍게 읽어보시고 하루를 시작해보세요 ☕️"
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

    target_date = get_target_issue_date_kst()
    date_str = target_date.strftime("%Y-%m-%d")
    print(f"Target issue date (KST): {date_str} offset: {ISSUE_OFFSET_DAYS}")

    raw_html = fetch_issue_html_for_date(target_date)
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
