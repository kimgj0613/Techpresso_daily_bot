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


def _remove_first_partner_main_ad(soup: BeautifulSoup) -> int:
    """
    첫 번째 FROM OUR PARTNER (main-ad-* 1st sponsor block) 제거.
    케이스가 2가지:
      - GitLab: main-ad-copy id 존재
      - IBM: main-ad-copy id 없음 (그냥 <div>...</div> 로 이어짐)
    해결: main-ad-headline 기준으로 다음 섹션(table) 시작 전까지 광고 블록을 통째로 제거.
    """
    def _is_section_start_table(tbl) -> bool:
        if not tbl or getattr(tbl, "name", None) != "table":
            return False
        td = tbl.find("td")
        if not td:
            return False
        style = (td.get("style") or "").lower()
        # Techpresso 본문 섹션 시작이 보통 padding-top: 50px 로 시작함
        return "padding-top: 50px" in style

    # main-ad-headline이 있으면 "첫번째 파트너 광고"가 있다고 본다
    headline = soup.find(id="main-ad-headline")
    if not headline:
        return 0

    removed = 0

    # 1) FROM OUR PARTNER 헤더 제거 (h태그 또는 b/strong)
    for s in list(soup.find_all(string=True)):
        t = re.sub(r"\s+", " ", str(s)).strip().upper()
        if t == "FROM OUR PARTNER":
            header = s.find_parent(["h1", "h2", "h3", "h4", "h5", "h6"])
            if header:
                header.decompose()
                removed += 1
            else:
                btag = s.find_parent(["b", "strong"])
                if btag:
                    btag.decompose()
                    removed += 1
            break

    # 2) main-ad 관련 id들(있으면) 제거
    for _id in ("main-ad-copy", "main-ad-image-link", "main-ad-image", "main-ad-headline"):
        n = soup.find(id=_id)
        if n:
            n.decompose()
            removed += 1

    # 3) ✅ 핵심: headline(원래 위치) 뒤에 이어지는 "광고 내용 div"도 함께 제거
    #    - IBM 케이스: id 없는 <div>...</div>가 남아있음
    #
    # headline이 decompose 됐을 수 있으니, 다시 기준점을 잡는다:
    # main-ad-image-link가 있으면 그걸, 없으면 main-ad-headline이 있었던 자리를 근처에서 찾는다.
    anchor = soup.find(id="main-ad-image-link") or soup.find(id="main-ad-headline")

    # anchor가 이미 제거돼서 못 찾으면: "FROM OUR PARTNER" 텍스트 있었던 근처를 대신 찾기 어렵다.
    # -> headline이 제거되기 전에 sibling div들을 제거해야 안전하므로, 위에서 id 제거 전에 anchor를 잡는 게 베스트.
    # 그래서 여기서는 anchor가 없을 때를 대비해 "main-ad-image" 기준도 한 번 더 탐색.
    if not anchor:
        anchor = soup.find(id="main-ad-image")

    # anchor가 없으면 더 할 수 없음(이미 제거됐거나 구조가 다름)
    if not anchor:
        return removed

    # anchor의 다음 table(섹션 시작 table)을 찾는다
    end_table = anchor.find_next("table")
    while end_table and not _is_section_start_table(end_table):
        end_table = end_table.find_next("table")

    # end_table이 없으면 안전하게: 다음 table 1개를 끝으로 본다
    if not end_table:
        end_table = anchor.find_next("table")

    # anchor 이후의 형제 div/br 들을 end_table 전까지 제거
    cur = anchor
    for _ in range(80):  # 무한루프 방지
        nxt = cur.next_sibling
        # next_sibling이 None이면 다음 요소로 이동(구조상)
        if nxt is None:
            nxt = cur.find_next_sibling()

        if nxt is None:
            break

        # end_table에 도달하면 중단
        if getattr(nxt, "name", None) == "table" and nxt == end_table:
            break

        # 공백 문자열은 제거
        if isinstance(nxt, NavigableString):
            if str(nxt).strip() == "":
                nxt.extract()
                cur = cur  # cur 유지
                continue
            else:
                nxt.extract()
                removed += 1
                cur = cur
                continue

        # br / div / p / ul 등 광고 구성요소는 제거
        if getattr(nxt, "name", None) in ("br", "div", "p", "ul", "ol", "li", "span", "img", "a"):
            nxt.decompose()
            removed += 1
            cur = cur  # cur 유지(계속 같은 anchor 기준으로 다음 sibling 제거)
            continue

        # 그 외 태그도 너무 공격적으로 지우면 위험 -> 한 번만 스킵하고 다음으로
        cur = nxt

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

    # 0.5) 첫 번째 FROM OUR PARTNER(main-ad GitLab) 패턴 제거
    removed_main_ad = _remove_first_partner_main_ad(soup)
    if removed_main_ad:
        print("Main partner ad removed (main-ad-*):", removed_main_ad)

    # 1) 파트너 섹션 삭제(기타 파트너용)
    _remove_blocks_containing_keywords_safely(soup, PARTNER_KEYWORDS)

    # 2) AI Academy 섹션 삭제
    _remove_blocks_containing_keywords_safely(soup, REMOVE_SECTION_KEYWORDS)

    # 3) 광고 제거
    for ad in soup.select("[data-testid='ad'], .sponsor, .advertisement"):
        ad.decompose()

    # 4) 브랜딩 치환 (Techpresso -> OneSip)
    _replace_brand_everywhere(soup, BRAND_FROM, BRAND_TO)

    # ✅ 5) URL을 PDF에 표시하지 않도록 텍스트 URL 제거
    remove_visible_urls(soup)

    # ✅ 6) 링크 포함 '텍스트 노드만' 번역 (태그 구조 유지 → 링크 유지)
    translate_text_nodes_inplace(soup)

    out_html = str(soup)

    # fallback: 본문이 너무 짧으면 제거 없이 다시 번역(단, 파트너/아카데미 삭제는 유지)
    text_len = len(BeautifulSoup(out_html, "html.parser").get_text(" ", strip=True))
    if text_len < 200:
        print("WARNING: HTML too small after cleanup. Falling back without header/footer removal.")
        soup2 = BeautifulSoup(html, "html.parser")

        _remove_first_partner_main_ad(soup2)
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
    filename = f"HCS - OneSip_{date_str}.pdf"
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
