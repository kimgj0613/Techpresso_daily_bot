import os
import re
import smtplib
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import deepl
import feedparser
from bs4 import BeautifulSoup, NavigableString, Tag
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

# ✅ 0이면 당일, -1이면 전날, -2면 이틀 전...
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


def _remove_blocks_containing_keywords_safely(soup: BeautifulSoup, keywords) -> int:
    """
    keyword가 포함된 블록 삭제(안전 버전)
    - NavigableString(=BeautifulSoup 노드)만 대상으로 하고
    - node.find_parent 같은 내부 헬퍼에 의존하지 않아 에러를 피함
    """
    removed = 0

    for node in list(soup.find_all(string=True)):
        if not isinstance(node, NavigableString):
            continue

        s = str(node)
        if not _text_has_any(s, keywords):
            continue

        # 가장 안전한 컨테이너를 위로 탐색
        cur = node.parent
        container = None

        # 1) div/section 우선
        while cur is not None:
            if getattr(cur, "name", None) in ("div", "section"):
                container = cur
                break
            cur = cur.parent

        if container:
            txt = container.get_text(" ", strip=True)
            if txt and len(txt) <= 6000:
                container.decompose()
                removed += 1
                continue

        # 2) table(짧을 때만)
        cur = node.parent
        table = None
        while cur is not None:
            if getattr(cur, "name", None) == "table":
                table = cur
                break
            cur = cur.parent

        if table:
            txt = table.get_text(" ", strip=True)
            if txt and len(txt) <= 3500:
                table.decompose()
                removed += 1
                continue

        # 3) fallback: p/h*/td 정도만 제거
        parent = node.parent
        if parent and getattr(parent, "name", None) in ("p", "h1", "h2", "h3", "h4", "td"):
            parent.decompose()
            removed += 1

    return removed


# ----------------------
# ✅ 첫 번째 FROM OUR PARTNER 블록 제거 (앵커 기반, 타입 A/B 모두 안전)
# ----------------------
_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF]")


def _find_first_emoji_string(soup: BeautifulSoup):
    for node in soup.find_all(string=True):
        if not isinstance(node, NavigableString):
            continue
        if _EMOJI_RE.search(str(node)):
            return node
    return None


def _find_partner_marker_tag(soup: BeautifulSoup) -> Tag | None:
    """
    우선순위:
    1) h4#main-ad-title (가장 정확)
    2) id="main-ad-title" 어떤 태그든
    3) 텍스트 "FROM OUR PARTNER" 포함 노드의 상위 h* / div
    """
    tag = soup.find(id="main-ad-title")
    if isinstance(tag, Tag):
        return tag

    # fallback: text search
    for n in soup.find_all(string=True):
        if not isinstance(n, NavigableString):
            continue
        if "from our partner" in str(n).lower():
            h = n.find_parent(["h1", "h2", "h3", "h4", "h5", "h6"])
            if h:
                return h
            d = n.find_parent(["div", "section"])
            if d:
                return d
            return n.parent if isinstance(n.parent, Tag) else None

    return None


def _table_looks_like_issue(table: Tag) -> bool:
    """
    '기사 테이블' 판별 휴리스틱:
    - 테이블 텍스트에 이모지가 있으면 거의 확정(OneSip 본문 특성)
    - 아니면 padding-top: 50px 같은 기사 블록 스타일이 있으면 긍정
    """
    try:
        txt = table.get_text(" ", strip=True)
    except Exception:
        txt = ""
    if txt and _EMOJI_RE.search(txt):
        return True

    style = (table.get("style", "") or "").lower()
    if "padding-top" in style and "50" in style:
        return True

    return False


def _find_first_issue_table_after(marker_tag: Tag) -> Tag | None:
    """
    marker 이후 등장하는 table들 중,
    1) 이모지 포함(또는 기사 스타일) table을 우선 반환
    2) 없으면 marker 이후 첫 table 반환
    """
    first_table = None
    for t in marker_tag.find_all_next("table"):
        if first_table is None:
            first_table = t
        if _table_looks_like_issue(t):
            return t
    return first_table


def _remove_first_partner_block_until_first_issue_table(soup: BeautifulSoup) -> int:
    """
    시작: main-ad-title(또는 FROM OUR PARTNER 마커)
    끝: 그 다음 '첫 기사 테이블' 시작 직전까지

    ✅ 테이블 자체는 삭제하지 않음(= 절대 첫 기사까지 안 잘림)
    ✅ 타입 A/B 모두 대응
    """
    marker = _find_partner_marker_tag(soup)
    if not marker:
        return 0

    issue_table = _find_first_issue_table_after(marker)
    if not issue_table:
        return 0

    # marker에서 위로 올라가며 "광고 블록의 시작 컨테이너"를 잡는다.
    # (보통 div/section 안에 묶여 있음)
    start_block = marker
    parent_div = marker.find_parent(["div", "section"])
    if parent_div:
        start_block = parent_div

    removed = 0

    # 1) start_block과 issue_table이 같은 parent 아래 있으면: 그 parent의 contents 기준으로 구간 제거
    if start_block.parent is not None and issue_table.parent is not None and start_block.parent == issue_table.parent:
        siblings = list(start_block.parent.contents)
        try:
            i = siblings.index(start_block)
            j = siblings.index(issue_table)
        except ValueError:
            i = j = -1

        if i != -1 and j != -1 and i < j:
            for node in siblings[i:j]:
                if isinstance(node, NavigableString) and not str(node).strip():
                    node.extract()
                    continue
                try:
                    node.decompose()
                except Exception:
                    try:
                        node.extract()
                    except Exception:
                        pass
                removed += 1
            return removed

    # 2) fallback: start_block부터 다음 형제들을 issue_table 직전까지 제거
    cur = start_block
    while cur is not None and cur != issue_table:
        nxt = cur.next_sibling
        # 공백 문자열은 제거만
        if isinstance(cur, NavigableString):
            if not str(cur).strip():
                cur.extract()
            else:
                cur.extract()
                removed += 1
        else:
            try:
                cur.decompose()
                removed += 1
            except Exception:
                try:
                    cur.extract()
                    removed += 1
                except Exception:
                    pass
        if nxt is None:
            break
        cur = nxt

    return removed


def _ensure_first_issue_left_align(soup: BeautifulSoup):
    """
    파트너 블록 제거 후 첫 기사 제목이 가운데로 밀리는 현상 방지:
    첫 이모지 포함 td에 text-align:left 강제 부여.
    """
    emoji_node = _find_first_emoji_string(soup)
    if not emoji_node:
        return

    td = emoji_node.find_parent("td")
    if not td:
        return

    style = td.get("style", "") or ""
    if "text-align" not in style.lower():
        if style and not style.strip().endswith(";"):
            style += ";"
        style += " text-align: left;"
        td["style"] = style


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

        # 영어 알파벳이 거의 없으면 스킵
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

    # ✅ 0.5) 첫 번째 FROM OUR PARTNER 블록(광고)을 "첫 기사 테이블 직전"까지 통째로 삭제
    removed_partner = _remove_first_partner_block_until_first_issue_table(soup)
    if removed_partner:
        print("Main partner block removed (until first issue table):", removed_partner)

    # 1) 파트너 섹션 삭제(기타 파트너용, 잔여 처리)
    removed_partner2 = _remove_blocks_containing_keywords_safely(soup, PARTNER_KEYWORDS)
    if removed_partner2:
        print("Blocks removed by keywords (partner):", removed_partner2)

    # 2) AI Academy 섹션 삭제
    removed_ai = _remove_blocks_containing_keywords_safely(soup, REMOVE_SECTION_KEYWORDS)
    if removed_ai:
        print("Blocks removed by keywords (ai-academy):", removed_ai)

    # 3) 광고 제거
    for ad in soup.select("[data-testid='ad'], .sponsor, .advertisement"):
        ad.decompose()

    # 4) 브랜딩 치환 (Techpresso -> OneSip)
    _replace_brand_everywhere(soup, BRAND_FROM, BRAND_TO)

    # ✅ 5) URL 텍스트 제거(링크는 유지)
    remove_visible_urls(soup)

    # ✅ 6) 텍스트 노드 번역 (bold/strong은 제외)
    translate_text_nodes_inplace(soup)

    # ✅ 7) 첫 기사 left-align 보정(가운데 밀림 방지)
    _ensure_first_issue_left_align(soup)

    out_html = str(soup)

    # fallback: 본문이 너무 짧으면 제거 없이 다시 번역(단, 파트너/아카데미 삭제는 유지)
    text_len = len(BeautifulSoup(out_html, "html.parser").get_text(" ", strip=True))
    if text_len < 200:
        print("WARNING: HTML too small after cleanup. Falling back without header/footer removal.")
        soup2 = BeautifulSoup(html, "html.parser")

        _remove_first_partner_block_until_first_issue_table(soup2)
        _remove_blocks_containing_keywords_safely(soup2, PARTNER_KEYWORDS)
        _remove_blocks_containing_keywords_safely(soup2, REMOVE_SECTION_KEYWORDS)

        for ad in soup2.select("[data-testid='ad'], .sponsor, .advertisement"):
            ad.decompose()

        _replace_brand_everywhere(soup2, BRAND_FROM, BRAND_TO)
        remove_visible_urls(soup2)
        translate_text_nodes_inplace(soup2)
        _ensure_first_issue_left_align(soup2)

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
# RSS → 특정 날짜(오프셋) HTML 추출
# ======================
def fetch_issue_html_by_offset():
    feed = feedparser.parse(RSS_URL)

    target_date = (now_kst().date() + timedelta(days=ISSUE_OFFSET_DAYS))
    print("Target issue date (KST):", target_date, "offset:", ISSUE_OFFSET_DAYS)

    candidates = []
    for e in feed.entries:
        if not hasattr(e, "published_parsed"):
            continue
        if "content" not in e or not e.content:
            continue

        published_utc = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        published_kst_dt = published_utc.astimezone(KST)
        published_kst_date = published_kst_dt.date()

        candidates.append((published_kst_dt, published_kst_date, e.content[0].value))

    if not candidates:
        return None, None

    # 1) 정확히 target_date와 일치하는 발행본 우선
    exact = [c for c in candidates if c[1] == target_date]
    if exact:
        exact.sort(key=lambda x: x[0], reverse=True)
        return exact[0][2], target_date

    # 2) 없으면 target_date 이전(older) 중 가장 최신 fallback
    older = [c for c in candidates if c[1] < target_date]
    if older:
        older.sort(key=lambda x: x[0], reverse=True)
        chosen_dt, chosen_date, chosen_html = older[0]
        print("No exact match. Fallback to older issue date (KST):", chosen_date)
        return chosen_html, chosen_date

    # 3) 그래도 없으면 그냥 가장 최신(안전망)
    candidates.sort(key=lambda x: x[0], reverse=True)
    chosen_dt, chosen_date, chosen_html = candidates[0]
    print("No older match. Fallback to latest issue date (KST):", chosen_date)
    return chosen_html, chosen_date


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

    raw_html, issue_date = fetch_issue_html_by_offset()
    if not raw_html:
        print("No issue found for given offset.")
        return

    date_str = issue_date.strftime("%Y-%m-%d")

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
