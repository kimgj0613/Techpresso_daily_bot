import os
import re
import json
import smtplib
import ssl
import time
from pathlib import Path
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

# ✅ Daily JSON storage (English raw only)
DATA_DIR = Path(os.getenv("DATA_DIR", "data/daily"))
KEEP_DAYS = int(os.getenv("KEEP_DAYS", "62"))  # 약 2달


# ======================
# 유틸
# ======================
def now_kst():
    return datetime.now(tz=KST)


def _ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def prune_daily_data(keep_days: int = 62):
    """data/daily 안에서 keep_days보다 오래된 YYYY-MM-DD.json 삭제"""
    _ensure_dir(DATA_DIR)
    cutoff = now_kst().date() - timedelta(days=keep_days)

    removed = 0
    for fp in DATA_DIR.glob("*.json"):
        try:
            d = datetime.strptime(fp.stem, "%Y-%m-%d").date()
        except Exception:
            continue
        if d < cutoff:
            fp.unlink(missing_ok=True)
            removed += 1

    if removed:
        print(f"Pruned old daily json files: {removed} (keep_days={keep_days})")


def save_daily_json(date_str: str, payload: dict):
    _ensure_dir(DATA_DIR)
    fp = DATA_DIR / f"{date_str}.json"
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("Saved daily json:", str(fp))


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


# ----------------------
# ✅ 기사(이슈) 판별/이모지
# ----------------------
_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF]")


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


def _container_has_issue_tables(tag: Tag) -> bool:
    """컨테이너 내부에 '기사 테이블'이 있으면 True (이모지로 오판하지 않기 위해 table 기반만 봄)"""
    try:
        for t in tag.find_all("table"):
            if _table_looks_like_issue(t):
                return True
    except Exception:
        pass
    return False


def _container_has_issue_content(tag: Tag) -> bool:
    """
    이 컨테이너 안에 '기사 본문'이 들어있으면 True.
    - 이모지(🚀💥 등) 포함 텍스트가 있거나
    - 기사 테이블로 보이는 table이 있으면 True
    """
    try:
        txt = tag.get_text(" ", strip=True)
        if txt and _EMOJI_RE.search(txt):
            return True
    except Exception:
        pass

    try:
        for t in tag.find_all("table"):
            if _table_looks_like_issue(t):
                return True
    except Exception:
        pass

    return False


def _remove_blocks_containing_keywords_safely(soup: BeautifulSoup, keywords) -> int:
    """
    keyword가 포함된 블록 삭제(안전 강화 버전)
    ✅ 단, '기사 컨텐츠(이모지/기사 테이블)'가 포함된 큰 컨테이너는 절대 삭제하지 않음
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
            # ✅ 기사 내용이 섞여 있으면 삭제 금지
            if _container_has_issue_content(container):
                continue

            txt = container.get_text(" ", strip=True)
            # 너무 큰 블록은 위험 -> 삭제 금지(기준 더 빡세게)
            if txt and len(txt) <= 2500:
                container.decompose()
                removed += 1
                continue

        # 2) table(짧을 때만) — table 자체가 기사면 삭제 금지
        cur = node.parent
        table = None
        while cur is not None:
            if getattr(cur, "name", None) == "table":
                table = cur
                break
            cur = cur.parent

        if table:
            if _table_looks_like_issue(table):
                continue

            txt = table.get_text(" ", strip=True)
            if txt and len(txt) <= 1800:
                table.decompose()
                removed += 1
                continue

        # 3) fallback: p/h*/td 정도만 제거(기사 td면 삭제 금지)
        parent = node.parent
        if parent and getattr(parent, "name", None) in ("p", "h1", "h2", "h3", "h4", "td"):
            # td가 기사(이모지 포함)면 삭제 금지
            try:
                ptxt = parent.get_text(" ", strip=True)
                if ptxt and _EMOJI_RE.search(ptxt):
                    continue
            except Exception:
                pass

            parent.decompose()
            removed += 1

    return removed


# ----------------------
# ✅ AI Academy(🎓) 블록 제거: academy.techpresso.co 링크 기반 (정확/안전)
# ----------------------
def _remove_ai_academy_block_by_link(soup: BeautifulSoup) -> int:
    """
    🎓 AI Academy 프로모션 블록 제거
    - 특징: academy.techpresso.co 링크가 포함됨
    - 가능하면 tr/table 단위로 제거(레이아웃 안전)
    - 기사 보호는 '기사 테이블 존재 여부'로만 판단 (🎓 이모지로 기사 오판 방지)
    """
    removed = 0
    anchors = soup.find_all("a", href=True)

    for a in anchors:
        href = a.get("href", "") or ""
        if "academy.techpresso.co" not in href:
            continue

        tr = a.find_parent("tr")
        if isinstance(tr, Tag) and not _container_has_issue_tables(tr):
            tr.decompose()
            removed += 1
            continue

        table = a.find_parent("table")
        if isinstance(table, Tag) and not _container_has_issue_tables(table):
            table.decompose()
            removed += 1
            continue

        parent = a.find_parent(["div", "section", "td", "p"])
        if isinstance(parent, Tag) and not _container_has_issue_tables(parent):
            parent.decompose()
            removed += 1

    return removed


# ----------------------
# ✅ Partner 블록 제거 (기존 로직 유지)
# ----------------------
def _find_partner_marker_tag(soup: BeautifulSoup) -> Tag | None:
    tag = soup.find(id="main-ad-title")
    if isinstance(tag, Tag):
        return tag

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


def _find_first_issue_table_after(marker_tag: Tag) -> Tag | None:
    first_table = None
    for t in marker_tag.find_all_next("table"):
        if first_table is None:
            first_table = t
        if _table_looks_like_issue(t):
            return t
    return first_table


def _remove_first_partner_block_until_first_issue_table(soup: BeautifulSoup) -> int:
    marker = _find_partner_marker_tag(soup)
    if not marker:
        return 0

    issue_table = _find_first_issue_table_after(marker)
    if not issue_table:
        return 0

    common_parent = issue_table
    while common_parent is not None:
        try:
            if marker in common_parent.descendants:
                break
        except Exception:
            pass
        common_parent = common_parent.parent

    if common_parent is None:
        return 0

    start_child = marker
    while start_child.parent is not None and start_child.parent != common_parent:
        start_child = start_child.parent

    end_child = issue_table
    while end_child.parent is not None and end_child.parent != common_parent:
        end_child = end_child.parent

    removed = 0
    siblings = list(common_parent.contents)

    try:
        i = siblings.index(start_child)
        j = siblings.index(end_child)
    except ValueError:
        return 0

    if i >= j:
        return 0

    for node in siblings[i:j]:
        if isinstance(node, NavigableString):
            node.extract()
            removed += 1
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


def _remove_spotlight_partner_block(soup: BeautifulSoup) -> int:
    removed = 0

    block = soup.find(id="spotlight-ad-block")
    if isinstance(block, Tag):
        tr = block.find_parent("tr")
        if isinstance(tr, Tag):
            tr.decompose()
        else:
            block.decompose()
        removed += 1

    title = soup.find(id="spotlight-ad-title")
    if isinstance(title, Tag):
        tr = title.find_parent("tr")
        if isinstance(tr, Tag):
            tr.decompose()
        else:
            title.decompose()
        removed += 1

    return removed


def _find_next_partner_text_node(soup: BeautifulSoup) -> NavigableString | None:
    for n in soup.find_all(string=True):
        if not isinstance(n, NavigableString):
            continue
        if "from our partner" in str(n).lower():
            return n
    return None


def _remove_partner_block_around_text_node(n: NavigableString) -> bool:
    if not n or not n.parent:
        return False

    tr = n.find_parent("tr")
    if isinstance(tr, Tag):
        if not _container_has_issue_content(tr):
            tr.decompose()
            return True

    table = n.find_parent("table")
    if isinstance(table, Tag):
        if not _table_looks_like_issue(table) and not _container_has_issue_content(table):
            table.decompose()
            return True

    container = n.find_parent(["div", "section"])
    if isinstance(container, Tag):
        if not _container_has_issue_content(container):
            txt = container.get_text(" ", strip=True)
            if txt and len(txt) <= 3000:
                container.decompose()
                return True

    parent = n.find_parent(["h1", "h2", "h3", "h4", "p", "td"])
    if isinstance(parent, Tag):
        try:
            ptxt = parent.get_text(" ", strip=True)
            if ptxt and _EMOJI_RE.search(ptxt):
                return False
        except Exception:
            pass
        parent.decompose()
        return True

    return False


def _remove_partner_blocks_until_limit(soup: BeautifulSoup, max_blocks: int = 5) -> int:
    removed = 0
    for _ in range(max_blocks):
        n = _find_next_partner_text_node(soup)
        if not n:
            break
        if _remove_partner_block_around_text_node(n):
            removed += 1
            continue
        try:
            n.extract()
        except Exception:
            pass
        break
    return removed


def _remove_partner_everything(soup: BeautifulSoup) -> None:
    removed_main = _remove_first_partner_block_until_first_issue_table(soup)
    if removed_main:
        print("Main partner block removed (until first issue table):", removed_main)

    removed_spot = _remove_spotlight_partner_block(soup)
    if removed_spot:
        print("Spotlight partner block removed:", removed_spot)

    removed_rest = _remove_partner_blocks_until_limit(soup, max_blocks=5)
    if removed_rest:
        print("Extra partner blocks removed:", removed_rest)


# ----------------------
# 첫 기사 정렬 보정
# ----------------------
def _find_first_emoji_string(soup: BeautifulSoup):
    for node in soup.find_all(string=True):
        if not isinstance(node, NavigableString):
            continue
        if _EMOJI_RE.search(str(node)):
            return node
    return None


def _ensure_first_issue_left_align(soup: BeautifulSoup):
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
    translated_nodes = 0

    for node in list(soup.find_all(string=True)):
        if not isinstance(node, NavigableString):
            continue

        parent = node.parent.name if node.parent else ""
        if parent in ("script", "style"):
            continue

        if parent in ("strong", "b"):
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


def translate_html_preserve_layout(html: str, date_str: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    _remove_techpresso_header_footer_safely(soup)
    _remove_partner_everything(soup)

    removed_academy = _remove_ai_academy_block_by_link(soup)
    if removed_academy:
        print("AI Academy block removed by link:", removed_academy)

    removed_partner2 = _remove_blocks_containing_keywords_safely(soup, PARTNER_KEYWORDS)
    if removed_partner2:
        print("Blocks removed by keywords (partner):", removed_partner2)

    removed_ai = _remove_blocks_containing_keywords_safely(soup, REMOVE_SECTION_KEYWORDS)
    if removed_ai:
        print("Blocks removed by keywords (ai-academy):", removed_ai)

    for ad in soup.select("[data-testid='ad'], .sponsor, .advertisement"):
        ad.decompose()

    _replace_brand_everywhere(soup, BRAND_FROM, BRAND_TO)

    remove_visible_urls(soup)
    translate_text_nodes_inplace(soup)
    _ensure_first_issue_left_align(soup)

    out_html = str(soup)

    text_len = len(BeautifulSoup(out_html, "html.parser").get_text(" ", strip=True))
    if text_len < 200:
        print("WARNING: HTML too small after cleanup. Falling back without header/footer removal.")
        soup2 = BeautifulSoup(html, "html.parser")
        _remove_partner_everything(soup2)
        _remove_ai_academy_block_by_link(soup2)
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
# ✅ Daily 구조화(영문 원문) 추출
# ======================
def extract_structured_from_issue_html(raw_html: str) -> dict:
    """
    Techpresso issue HTML에서 기사/기타 섹션을 구조화해서 뽑는다.
    - 번역 전(raw_html) 기준(영문 원문)으로 저장
    - partner/academy/광고 제거 로직 재사용해서 데이터 깨끗하게 유지
    """
    soup = BeautifulSoup(raw_html, "html.parser")

    _remove_techpresso_header_footer_safely(soup)
    _remove_partner_everything(soup)
    _remove_ai_academy_block_by_link(soup)
    _remove_blocks_containing_keywords_safely(soup, REMOVE_SECTION_KEYWORDS)
    _remove_blocks_containing_keywords_safely(soup, PARTNER_KEYWORDS)

    issues = []
    tables = soup.find_all("table")
    for tbl in tables:
        if not _table_looks_like_issue(tbl):
            continue

        td = tbl.find("td")
        if not td:
            continue

        a = td.find("a", href=True)
        title = td.get_text(" ", strip=True)
        link = a["href"] if a else None

        bullets = []
        nxt = tbl.find_next_sibling()
        if nxt and getattr(nxt, "name", None) == "ul":
            for li in nxt.find_all("li"):
                t = li.get_text(" ", strip=True)
                if t:
                    bullets.append(t)

        if title and link:
            issues.append({"title": title, "link": link, "bullets": bullets[:6]})

    other_news = []
    header_node = soup.find(string=lambda s: isinstance(s, str) and "Other news & articles you might like" in s)
    if header_node:
        container = header_node.find_parent()
        if container:
            for li in container.find_all_next("li", limit=80):
                a = li.find("a", href=True)
                txt = li.get_text(" ", strip=True)
                if a and txt:
                    cleaned = txt.replace("LINK", "").strip()
                    other_news.append({"title": cleaned, "link": a["href"]})
                if len(other_news) >= 40:
                    break

    return {"issues": issues, "other_news": other_news}


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

    exact = [c for c in candidates if c[1] == target_date]
    if exact:
        exact.sort(key=lambda x: x[0], reverse=True)
        return exact[0][2], target_date

    older = [c for c in candidates if c[1] < target_date]
    if older:
        older.sort(key=lambda x: x[0], reverse=True)
        chosen_dt, chosen_date, chosen_html = older[0]
        print("No exact match. Fallback to older issue date (KST):", chosen_date)
        return chosen_html, chosen_date

    candidates.sort(key=lambda x: x[0], reverse=True)
    chosen_dt, chosen_date, chosen_html = candidates[0]
    print("No older match. Fallback to latest issue date (KST):", chosen_date)
    return chosen_html, chosen_date


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

    raw_html, issue_date = fetch_issue_html_by_offset()
    if not raw_html:
        print("No issue found for given offset.")
        return

    date_str = issue_date.strftime("%Y-%m-%d")

    # ✅ Daily JSON 저장(영문 원문) + 2달 보관 정책
    prune_daily_data(keep_days=KEEP_DAYS)
    try:
        structured = extract_structured_from_issue_html(raw_html)
        save_daily_json(date_str, {"date": date_str, **structured})
    except Exception as e:
        # Daily PDF 발송이 main 목적이니, 저장 실패는 전체 실패로 만들지 않음
        print("WARNING: failed to extract/save daily json:", e)

    # 데일리 PDF는 기존대로 '번역본'으로 생성/발송
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
