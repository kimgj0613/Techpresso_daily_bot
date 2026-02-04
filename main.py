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
DEEPL_SERVER_URL = os.getenv("DEEPL_SERVER_URL", "https://api-free.deepl.com")

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))

MAIL_SUBJECT_PREFIX = "☕ OneSip | Today’s Tech in One Sip"
MAIL_BODY_LINE = "OneSip – Your daily tech clarity"

BRAND_FROM = "Techpresso"
BRAND_TO = "OneSip"

PROTECT_TERMS = ["OneSip"]
DEBUG_DUMP_HTML = os.getenv("DEBUG_DUMP_HTML", "0") == "1"

KST = tz.gettz("Asia/Seoul")

translator = deepl.Translator(DEEPL_API_KEY, server_url=DEEPL_SERVER_URL) if DEEPL_API_KEY else None


# ======================
# 시간/유틸
# ======================
def now_kst():
    return datetime.now(tz=KST)


def is_sunday_kst():
    return now_kst().weekday() == 6


def safe_print_deepl_usage(prefix):
    if not translator:
        return
    try:
        u = translator.get_usage()
        print(f"{prefix}: {u.character.count}/{u.character.limit}")
    except Exception as e:
        print("DeepL usage check failed:", e)


# ======================
# 번역 보호
# ======================
def protect_terms(text):
    mapping = {}
    out = text
    for t in PROTECT_TERMS:
        ph = f"__PROTECT_{t.upper()}__"
        if t in out:
            out = out.replace(t, ph)
            mapping[ph] = t
    return out, mapping


def restore_terms(text, mapping):
    for k, v in mapping.items():
        text = text.replace(k, v)
    return text


# ======================
# DeepL 번역
# ======================
def translate_text(text, retries=3):
    if not translator or not text.strip():
        return text

    protected, mapping = protect_terms(text)
    for i in range(retries):
        try:
            r = translator.translate_text(
                protected,
                target_lang="KO",
                preserve_formatting=True,
            )
            return restore_terms(r.text, mapping)
        except Exception as e:
            print("DEEPL ERROR:", e)
            time.sleep(2 * (i + 1))

    return text


# ======================
# HTML 제거 유틸 (안전)
# ======================
def iter_text_and_parent(soup):
    for s in soup.find_all(string=True):
        if not isinstance(s, NavigableString):
            continue
        txt = str(s).strip()
        if not txt:
            continue
        parent = getattr(s, "parent", None)
        if isinstance(parent, Tag):
            yield txt, parent


def smallest_container(parent, max_len=9000):
    best = None
    for name in ("td", "tr", "div", "section", "table"):
        anc = parent if parent.name == name else parent.find_parent(name)
        if not anc:
            continue
        txt = anc.get_text(" ", strip=True)
        if not txt or len(txt) > max_len:
            continue
        if best is None or len(txt) < len(best.get_text(" ", strip=True)):
            best = anc
    return best


def remove_blocks_by_keywords(soup, keywords, max_len=9000):
    lowered = [k.lower() for k in keywords]
    removed = 0

    for text, parent in list(iter_text_and_parent(soup)):
        if not any(k in text.lower() for k in lowered):
            continue

        target = smallest_container(parent, max_len)
        if target:
            target.decompose()
            removed += 1

    return removed


# ======================
# 전처리 단계
# ======================
REMOVE_HEADER_FOOTER = [
    "Join Free", "Upgrade", "Subscribe for free",
    "Not subscribed to", "Read Online"
]

REMOVE_AI_SECTION = [
    "AI Academy",
    "Want to master the AI tools",
    "매일 다루는 AI 도구"
]

PARTNER_KEYWORDS = ["FROM OUR PARTNER", "From our partner"]

URL_RE = re.compile(r"(https?://\S+|www\.\S+)", re.I)


def cleanup_html(soup):
    remove_blocks_by_keywords(soup, REMOVE_HEADER_FOOTER, 1800)
    print("Partner blocks removed:",
          remove_blocks_by_keywords(soup, PARTNER_KEYWORDS))
    print("AI Academy blocks removed:",
          remove_blocks_by_keywords(soup, REMOVE_AI_SECTION))


def replace_brand(soup):
    for s in soup.find_all(string=True):
        if BRAND_FROM in s:
            s.replace_with(s.replace(BRAND_FROM, BRAND_TO))


def remove_visible_urls(soup):
    for s in soup.find_all(string=True):
        if not isinstance(s, NavigableString):
            continue
        if URL_RE.search(s):
            s.replace_with(URL_RE.sub("", s))


def translate_nodes(soup):
    count = 0
    for s in soup.find_all(string=True):
        if not isinstance(s, NavigableString):
            continue
        parent = s.parent.name if s.parent else ""
        if parent in ("script", "style"):
            continue
        txt = str(s).strip()
        if len(re.findall(r"[A-Za-z]", txt)) < 2 or len(txt) > 2000:
            continue
        s.replace_with(translate_text(txt))
        count += 1
    print("Translated text nodes:", count)


def translate_html_preserve_layout(html):
    soup = BeautifulSoup(html, "html.parser")

    cleanup_html(soup)
    replace_brand(soup)
    remove_visible_urls(soup)
    translate_nodes(soup)

    return str(soup)


# ======================
# PDF
# ======================
def wrap_html(inner):
    return f"""
    <html><head>
    <meta charset="utf-8">
    <style>
      body {{ font-family: sans-serif; font-size: 11pt; }}
      img, table {{ max-width: 100%; }}
    </style>
    </head>
    <body>{inner}</body></html>
    """


def html_to_pdf(inner, date_str):
    fname = f"Gmail - OneSip_{date_str}.pdf"
    HTML(string=wrap_html(inner)).write_pdf(fname)
    return fname


# ======================
# RSS
# ======================
def fetch_today_html():
    feed = feedparser.parse(RSS_URL)
    today = now_kst().date()

    for e in feed.entries:
        if not hasattr(e, "published_parsed"):
            continue
        d = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        if d.astimezone(KST).date() == today and e.content:
            return e.content[0].value
    return None


# ======================
# Email
# ======================
def send_email(pdf_path, date_str):
    msg = EmailMessage()
    msg["Subject"] = f"{MAIL_SUBJECT_PREFIX} ({date_str})"
    msg["From"] = os.getenv("MAIL_FROM")
    msg["To"] = os.getenv("MAIL_TO")
    msg.set_content(MAIL_BODY_LINE)

    with open(pdf_path, "rb") as f:
        msg.add_attachment(
            f.read(),
            maintype="application",
            subtype="pdf",
            filename=os.path.basename(pdf_path),
        )

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as s:
        s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
        s.send_message(msg)


# ======================
# main
# ======================
def main():
    if is_sunday_kst():
        print("Sunday skipped")
        return

    safe_print_deepl_usage("DeepL usage(before)")

    html = fetch_today_html()
    if not html:
        print("No issue today")
        return

    date_str = now_kst().strftime("%Y-%m-%d")
    inner = translate_html_preserve_layout(html)

    if len(BeautifulSoup(inner, "html.parser").get_text(strip=True)) < 200:
        raise RuntimeError("Final HTML too small")

    pdf = html_to_pdf(inner, date_str)
    safe_print_deepl_usage("DeepL usage(after)")
    send_email(pdf, date_str)

    print("Done:", pdf)


if __name__ == "__main__":
    main()
