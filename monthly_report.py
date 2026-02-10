import os
import re
import json
import math
import ssl
import smtplib
from pathlib import Path
from datetime import datetime, timedelta
from email.message import EmailMessage

import deepl
from dateutil import tz
from weasyprint import HTML

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import KMeans


KST = tz.gettz("Asia/Seoul")

DATA_DIR = Path(os.getenv("DATA_DIR", "data/daily"))
KEEP_DAYS = int(os.getenv("KEEP_DAYS", "62"))

# report language: EN or KO
REPORT_LANG = os.getenv("REPORT_LANG", "KO").upper()

DEEPL_API_KEY = os.getenv("DEEPL_API_KEY")
DEEPL_SERVER_URL = os.getenv("DEEPL_SERVER_URL", "https://api-free.deepl.com")
translator = None
if DEEPL_API_KEY:
    translator = deepl.Translator(DEEPL_API_KEY, server_url=DEEPL_SERVER_URL)

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))

MAIL_SUBJECT_PREFIX_MONTHLY = "☕ OneSip | Monthly Tech Trends"
MAIL_BODY_LINE = "OneSip – Your monthly tech clarity"


def now_kst():
    return datetime.now(tz=KST)


def _ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def prune_daily_data(keep_days: int = 62):
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


def load_month_documents(target_ym: str | None = None):
    """
    target_ym: 'YYYY-MM' 형식이면 그 달만 로드.
    미지정(None)이면 KST 기준 '지난 달'로 로드.
    """
    _ensure_dir(DATA_DIR)

    if target_ym is None:
        today = now_kst().date()
        first_this_month = today.replace(day=1)
        last_month_last_day = first_this_month - timedelta(days=1)
        target_ym = last_month_last_day.strftime("%Y-%m")

    docs = []
    meta = []
    for fp in sorted(DATA_DIR.glob(f"{target_ym}-*.json")):
        try:
            payload = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue

        date_str = payload.get("date") or fp.stem
        for it in payload.get("issues", []):
            title = (it.get("title") or "").strip()
            link = (it.get("link") or "").strip()
            bullets = it.get("bullets") or []
            text = " ".join([title] + [b for b in bullets if isinstance(b, str)])
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) < 20:
                continue
            docs.append(text)
            meta.append({"date": date_str, "title": title, "link": link})

        # other_news도 보조로 포함(가볍게)
        for it in payload.get("other_news", []):
            title = (it.get("title") or "").strip()
            link = (it.get("link") or "").strip()
            text = re.sub(r"\s+", " ", title).strip()
            if len(text) < 20:
                continue
            docs.append(text)
            meta.append({"date": date_str, "title": title, "link": link})

    return target_ym, docs, meta


def choose_k(n_docs: int) -> int:
    if n_docs <= 3:
        return 1
    k = int(round(math.sqrt(n_docs)))
    k = max(3, min(6, k))
    k = min(k, n_docs)  # never exceed docs
    return k


def cluster_docs(docs, k: int):
    vectorizer = TfidfVectorizer(
        stop_words="english",
        ngram_range=(1, 2),
        max_df=0.85,
        min_df=2,
    )
    X = vectorizer.fit_transform(docs)

    if k <= 1 or X.shape[0] < 4:
        # 단일 클러스터 취급
        labels = [0] * X.shape[0]
        return vectorizer, X, labels, None

    km = KMeans(n_clusters=k, random_state=42, n_init="auto")
    labels = km.fit_predict(X)
    return vectorizer, X, labels, km


def top_terms_for_cluster(vectorizer, km, cluster_id: int, top_n: int = 8):
    feature_names = vectorizer.get_feature_names_out()
    center = km.cluster_centers_[cluster_id]
    idx = center.argsort()[::-1][:top_n]
    return [feature_names[i] for i in idx]


def representative_items(X, labels, km, meta, cluster_id: int, top_n: int = 3):
    # centroid와 cosine similarity가 높은 문서 선택
    import numpy as np

    idxs = [i for i, c in enumerate(labels) if c == cluster_id]
    if not idxs:
        return []

    if km is None:
        # 단일 클러스터일 때는 날짜 최신순으로
        items = [meta[i] for i in idxs]
        items.sort(key=lambda x: x.get("date", ""), reverse=True)
        return items[:top_n]

    center = km.cluster_centers_[cluster_id]
    sub = X[idxs].toarray()
    # cosine similarity with centroid
    denom = (np.linalg.norm(sub, axis=1) * (np.linalg.norm(center) + 1e-12) + 1e-12)
    sims = (sub @ center) / denom
    order = sims.argsort()[::-1]
    reps = [meta[idxs[i]] for i in order[:top_n]]
    return reps


def translate_if_needed(text: str) -> str:
    if REPORT_LANG != "KO":
        return text
    if not translator:
        # KO 요청인데 키가 없으면 원문 유지
        return text
    if not text or not text.strip():
        return text
    try:
        r = translator.translate_text(text, target_lang="KO", preserve_formatting=True)
        return r.text
    except Exception as e:
        print("DeepL translate failed:", e)
        return text


def build_report_html(target_ym: str, themes: list, notable: list, so_what: list, watchlist: list, refs: list):
    title = f"OneSip Monthly Tech Trends — {target_ym}"
    title_ko = translate_if_needed(title)

    def li(items):
        return "".join(f"<li>{x}</li>" for x in items)

    themes_html = ""
    for th in themes:
        name = th["name"]
        name_ko = translate_if_needed(name)
        kws = ", ".join(th["keywords"])
        kws_ko = translate_if_needed(kws)
        reps = "".join(
            f'<li><a href="{r["link"]}">{r["title"]}</a> <span class="muted">({r["date"]})</span></li>'
            for r in th["representatives"]
            if r.get("link")
        )
        themes_html += f"""
        <div class="card">
          <div class="card-title">{name_ko}</div>
          <div class="muted">Keywords: {kws_ko}</div>
          <ul>{reps}</ul>
        </div>
        """

    notable_ko = [translate_if_needed(x) for x in notable]
    so_what_ko = [translate_if_needed(x) for x in so_what]
    watch_ko = [translate_if_needed(x) for x in watchlist]

    refs_html = "".join(
        f'<li><a href="{r["link"]}">{r["title"]}</a> <span class="muted">({r["date"]})</span></li>'
        for r in refs
        if r.get("link")
    )

    css = """
    @page { size: A4; margin: 14mm; }
    body { font-family: "Noto Sans CJK KR","Noto Sans KR","Noto Sans",sans-serif; font-size: 10.5pt; line-height: 1.35; }
    h1 { margin: 0 0 10px 0; font-size: 16pt; }
    h2 { margin: 14px 0 6px 0; font-size: 12pt; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .card { border: 1px solid #ddd; border-radius: 10px; padding: 8px 10px; }
    .card-title { font-weight: 700; margin-bottom: 4px; }
    ul { margin: 6px 0 0 18px; padding: 0; }
    li { margin: 2px 0; }
    .muted { color: #666; font-size: 9pt; }
    a { color: #0b57d0; text-decoration: none; }
    .small { font-size: 9.5pt; }
    """

    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><style>{css}</style></head>
<body>
  <h1>{title_ko}</h1>

  <h2>Top Themes</h2>
  <div class="grid">
    {themes_html}
  </div>

  <div class="grid">
    <div class="card">
      <div class="card-title">Notable Shifts</div>
      <ul class="small">{li(notable_ko)}</ul>
    </div>
    <div class="card">
      <div class="card-title">So What</div>
      <ul class="small">{li(so_what_ko)}</ul>
    </div>
  </div>

  <div class="card" style="margin-top:10px;">
    <div class="card-title">Watchlist (Next Month)</div>
    <ul class="small">{li(watch_ko)}</ul>
  </div>

  <h2>Reference Links</h2>
  <ul class="small">{refs_html}</ul>

  <div class="muted" style="margin-top:10px;">
    Generated from saved OneSip daily items (English raw), clustered via TF-IDF + KMeans.
  </div>
</body>
</html>
"""
    return html


def html_to_pdf(html: str, target_ym: str) -> str:
    filename = f"OneSip_Monthly_{target_ym}.pdf"
    HTML(string=html).write_pdf(filename)
    return filename


def send_email(pdf_path: str, target_ym: str):
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
    msg["Subject"] = f"{MAIL_SUBJECT_PREFIX_MONTHLY} ({target_ym})"
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.set_content(
        f"{MAIL_BODY_LINE}\n\n"
        f"Monthly trends report for {target_ym} is attached.\n"
        "— OneSip ☕️"
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


def main():
    # 2달 보관 정책도 월간 실행 때 한 번 더 적용(혹시 데일리 실패했어도 정리됨)
    prune_daily_data(keep_days=KEEP_DAYS)

    report_month = os.getenv("REPORT_MONTH")  # optional: YYYY-MM
    target_ym, docs, meta = load_month_documents(report_month)

    if len(docs) < 6:
        # 데이터가 적으면 최근 30일로 fallback
        print("Not enough docs for month; fallback to last 30 days.")
        cutoff = now_kst().date() - timedelta(days=30)
        docs2, meta2 = [], []
        for fp in sorted(DATA_DIR.glob("*.json")):
            try:
                d = datetime.strptime(fp.stem, "%Y-%m-%d").date()
            except Exception:
                continue
            if d < cutoff:
                continue
            payload = json.loads(fp.read_text(encoding="utf-8"))
            date_str = payload.get("date") or fp.stem
            for it in payload.get("issues", []):
                title = (it.get("title") or "").strip()
                link = (it.get("link") or "").strip()
                bullets = it.get("bullets") or []
                text = " ".join([title] + [b for b in bullets if isinstance(b, str)])
                text = re.sub(r"\s+", " ", text).strip()
                if len(text) < 20:
                    continue
                docs2.append(text)
                meta2.append({"date": date_str, "title": title, "link": link})
        docs, meta = docs2, meta2

    if not docs:
        raise RuntimeError("No documents found to build monthly report.")

    k = choose_k(len(docs))
    vectorizer, X, labels, km = cluster_docs(docs, k)

    themes = []
    if km is None:
        # 단일 테마
        reps = representative_items(X, labels, km, meta, 0, top_n=6)
        themes.append({
            "name": "Overall Themes (low volume)",
            "keywords": [],
            "representatives": reps[:3],
        })
    else:
        for cid in range(k):
            kws = top_terms_for_cluster(vectorizer, km, cid, top_n=8)
            reps = representative_items(X, labels, km, meta, cid, top_n=3)

            # 사람이 읽기 좋은 테마명(키워드 기반)
            name = " / ".join(kws[:3]).title()
            themes.append({"name": name, "keywords": kws, "representatives": reps})

        # 클러스터 크기 큰 순으로 정렬
        counts = {cid: 0 for cid in range(k)}
        for c in labels:
            counts[c] += 1
        themes.sort(key=lambda t: counts.get(int(themes.index(t)), 0), reverse=True)

    # 상위 5개만 1페이지에 노출
    themes = themes[:5]

    # Notable / So what / Watchlist는 “룰 기반”으로 깔끔하게 (나중에 고도화 가능)
    notable = [
        "Agents and copilots are shifting from demos to enterprise workflows.",
        "AI infrastructure (chips, inference, cost) keeps resurfacing as a core bottleneck.",
        "Productization accelerates: platforms bundle models, tools, and distribution.",
    ]
    so_what = [
        "Treat agent governance (security, audit, evaluation) as a first-class requirement.",
        "Plan for cost controls: caching, routing, smaller models, and eval-driven rollout.",
        "Track vendor lock-in signals (proprietary toolchains, closed ecosystems, pricing).",
    ]
    watchlist = [
        "Open-source vs. closed agent stacks: who wins enterprise trust?",
        "Regulatory and IP pressure around training data and synthetic content.",
        "Hardware + inference optimization breakthroughs that move the cost curve.",
    ]

    # refs: 전체에서 대표 링크 10개
    refs = [m for m in meta if m.get("link")]
    refs.sort(key=lambda x: x.get("date", ""), reverse=True)
    refs = refs[:12]

    report_html = build_report_html(target_ym, themes, notable, so_what, watchlist, refs)
    pdf_path = html_to_pdf(report_html, target_ym)

    send_email(pdf_path, target_ym)
    print("Monthly report generated:", pdf_path)


if __name__ == "__main__":
    main()
