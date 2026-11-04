"""Audit Factiva exports and prepare article-to-campaign review. No network access."""
import argparse
import csv
import hashlib
import html
import json
import shutil
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

from factiva_queries import BANK_ALIASES, NGO_ALIASES, campaign_query

import re

def rtf_to_text(raw):
    token = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|([^\\{}]+)")
    skip_dest = {"fonttbl","colortbl","stylesheet","info","pict","object","header","headerf","headerl","headerr","footer","footerf","footerl","footerr","shp","nonshppict","fldinst","listtable","listoverridetable"}
    stack, output = [], []
    ignored, ucskip, skip = False, 1, 0
    codepage = "cp1252"
    for match in token.finditer(raw):
        word, number, hexchar, symbol, brace, plain = match.groups()
        if brace == "{":
            stack.append((ignored, ucskip))
        elif brace == "}":
            if stack:
                ignored, ucskip = stack.pop()
            skip = 0
        elif word:
            value = int(number) if number is not None else None
            if word in skip_dest:
                ignored = True
            elif word == "ansicpg" and value:
                codepage = f"cp{value}"
            elif word == "uc" and value is not None:
                ucskip = value
            elif word == "u" and value is not None:
                if not ignored:
                    output.append(chr(value if value >= 0 else value + 65536))
                skip = ucskip
            elif not ignored:
                if word in {"par","line","row","page","sect"}:
                    output.append("\n")
                elif word in {"tab","cell"}:
                    output.append("\t")
                elif word in {"emdash","endash","bullet","lquote","rquote","ldblquote","rdblquote"}:
                    output.append({"emdash":"—","endash":"–","bullet":"•","lquote":"‘","rquote":"’","ldblquote":"“","rdblquote":"”"}[word])
        elif hexchar is not None:
            if skip:
                skip -= 1
            elif not ignored:
                output.append(bytes.fromhex(hexchar).decode(codepage, errors="replace"))
        elif symbol:
            if symbol == "*":
                ignored = True
            elif symbol in "{}\\":
                if skip:
                    skip -= 1
                elif not ignored:
                    output.append(symbol)
            elif symbol in {"~","_","-"} and not ignored:
                output.append({"~":" ","_":"-","-":""}[symbol])
        elif plain and not ignored:
            plain = plain.replace("\r","").replace("\n","")
            if skip:
                plain, skip = plain[skip:], max(0, skip - len(plain))
            if plain:
                output.append(plain.encode("latin1", errors="replace").decode(codepage, errors="replace"))
    text = "".join(output)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


FIELDS = re.compile(r"(?m)^[ \t]*([A-Z][A-Z0-9]{0,4})\t")
ACCESSION = re.compile(r"(?m)^[ \t]*AN\tDocument[ \t]+(\S+)[^\n]*")
REVIEW_FIELDS = [
    "uid_archive", "article_id", "review_decision", "review_note", "reviewer", "banks_relevant",
    "article_date", "relative_day", "window", "source_type", "source",
    "headline", "language", "banks_matched", "ngo_name_matched", "priority",
    "article_excerpt", "campaign_report", "story_group_id", "input_file",
]


def normalize(value):
    value = unicodedata.normalize("NFKD", html.unescape(value or ""))
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def query_key(value):
    return re.sub(r"\s+", "", value).casefold()


def fields_from_block(block):
    tags = list(FIELDS.finditer(block))
    return {
        tag[1]: html.unescape(block[tag.end():tags[i+1].start() if i+1 < len(tags) else len(block)].strip())
        for i, tag in enumerate(tags)
    }


def source_family(source):
    key = normalize(source)
    for phrase, family in [
        ("financial times deutschland", "Financial Times Deutschland"),
        ("financial times", "Financial Times"),
        ("new york times", "The New York Times"),
        ("wall street journal", "The Wall Street Journal"),
        ("les echos", "Les Echos"), ("guardian", "The Guardian"),
    ]:
        if phrase in key:
            return family
    return source


def classify_source(source):
    return "wire" if any(term in normalize(source) for term in (
        "reuters", "dow jones", "associated press", "agence france presse",
        "afp", "ansa", "ap planner",
    )) else "press"


def group_story_versions(articles):
    """Group near-identical editions, retaining every publication record/date."""
    earlier = defaultdict(list)
    for article in sorted(articles.values(), key=lambda a: (a["article_date"], a["article_id"])):
        key = (article["source_family"], normalize(article["headline"]))
        body = normalize(article["body"])
        for previous, previous_body in earlier[key]:
            gap = (date.fromisoformat(article["article_date"]) - date.fromisoformat(previous["article_date"])).days
            if gap <= 2 and SequenceMatcher(None, body, previous_body).ratio() >= .98:
                article["story_group_id"] = previous["story_group_id"]
                break
        earlier[key].append((article, body))
    dates = defaultdict(list)
    for article in articles.values():
        dates[article["story_group_id"]].append(article["article_date"])
    for article in articles.values():
        article["story_first_date"] = min(dates[article["story_group_id"]])


def parse_export(path):
    text = rtf_to_text(path.read_bytes().decode("latin1"))
    article_text, marker, summary_text = text.rpartition("Search Summary")
    if not marker:
        raise ValueError(f"No search summary in {path.name}")
    summary = dict(re.findall(r"(?m)^[ \t]*([A-Za-z ]+)\t([^\n]*)", summary_text))
    summary = {key.strip(): value.strip() for key, value in summary.items()}
    if "Text" not in summary or "Date" not in summary:
        raise ValueError(f"Missing query/date range in {path.name}")
    start, end = [datetime.strptime(d, "%d/%m/%Y").date() for d in summary["Date"].split(" to ")]
    articles, cursor = [], 0
    for accession in ACCESSION.finditer(article_text):
        fields = fields_from_block(article_text[cursor:accession.start()])
        cursor = accession.end()
        if not fields.get("HD") or not fields.get("PD"):
            raise ValueError(f"Missing headline/date before {accession[1]} in {path.name}")
        body = "\n\n".join(fields.get(field, "") for field in ("LP", "TD") if fields.get(field))
        source = fields.get("SN", "")
        if not source or not body:
            raise ValueError(f"Missing source/body for {accession[1]} in {path.name}")
        family = source_family(source)
        articles.append({
            "article_id": accession[1], "headline": fields["HD"],
            "article_date": datetime.strptime(fields["PD"], "%d %B %Y").date().isoformat(),
            "source": source, "source_code": fields.get("SC", ""), "source_family": family,
            "source_type": classify_source(source), "language": fields.get("LA", ""),
            "word_count": fields.get("WC", ""), "lead": fields.get("LP", ""), "body": body,
            "story_group_id": hashlib.sha256((family + "|" + normalize(body)).encode()).hexdigest()[:16],
            "input_file": path.name,
        })
    if FIELDS.search(article_text[cursor:]):
        raise ValueError(f"An article is truncated after the last accession in {path.name}")
    return {
        "path": path, "query": summary["Text"], "date_from": start.isoformat(),
        "date_to": end.isoformat(), "reported_results": int(summary.get("Results Found", "0").replace(",", "")),
        "source_filter": summary.get("Source", ""), "language_filter": summary.get("Language", ""),
        "articles": articles,
    }


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path, rows, fieldnames=None):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames or list(rows[0]), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def find_batch(export, manifest, campaigns):
    actual = query_key(export["query"])
    scores = [
        (sum(query_key(campaign_query(campaigns[uid])) in actual for uid in batch["uid_archives"].split(";")),
         export["date_from"][:4] == batch["campaign_year"], batch)
        for batch in manifest
    ]
    best = max(scores, key=lambda item: (item[0], item[1]))
    if not best[0]:
        raise ValueError(f"Could not associate {export['path'].name} with a prepared batch")
    return best[2]


def contains_alias(text, alias):
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(normalize(alias)) + r"(?![a-z0-9])", text))


def ngo_found(text, ngo):
    if ngo == "Which? (UK Consumers Association)":
        if contains_alias(text, "Consumers Association"):
            return True
        words = text.split()
        cues = {"consumer", "consumers", "watchdog", "survey", "report", "research"}
        return any(
            word == "which" and cues.intersection(words[max(0, i-5):i+6])
            for i, word in enumerate(words)
        )
    return any(contains_alias(text, alias) for alias in NGO_ALIASES.get(ngo, [ngo]))


def excerpt(article, aliases):
    paragraphs = [p.strip() for p in article["body"].split("\n\n") if p.strip()]
    selected = [p for p in paragraphs if any(contains_alias(normalize(p), a) for a in aliases)]
    return "\n\n".join(selected or paragraphs[:2])[:2200]


def covers_interval(intervals, start, end):
    cursor, finish = date.fromisoformat(start), date.fromisoformat(end)
    for lower, upper in sorted(intervals):
        lower, upper = date.fromisoformat(lower), date.fromisoformat(upper)
        if lower > cursor:
            return False
        if upper >= cursor:
            cursor = upper + timedelta(days=1)
        if cursor > finish:
            return True
    return cursor > finish


def prepare_candidates(campaigns, articles, previous):
    review = []
    for uid, campaign in campaigns.items():
        campaign_date = date.fromisoformat(campaign["date"])
        for article in articles.values():
            if not campaign["search_start"] <= article["article_date"] <= campaign["search_end"]:
                continue
            narrative = normalize(article["headline"] + " " + article["body"])
            matched = [
                bank.strip() for bank in campaign["banks"].split(";")
                if any(contains_alias(narrative, alias) for alias in BANK_ALIASES[bank.strip()])
            ]
            has_ngo = ngo_found(narrative, campaign["ngo"])
            if not matched and not has_ngo:
                continue
            day = (date.fromisoformat(article["article_date"]) - campaign_date).days
            aliases = [a for bank in matched for a in BANK_ALIASES[bank]] + NGO_ALIASES.get(campaign["ngo"], [])
            old = previous.get((uid, article["article_id"]), {})
            review.append({
                "uid_archive": uid, "article_id": article["article_id"],
                "review_decision": old.get("review_decision", ""), "review_note": old.get("review_note", ""),
                "reviewer": old.get("reviewer", ""), "banks_relevant": old.get("banks_relevant", ""),
                "article_date": article["article_date"], "relative_day": day,
                "window": "pre7" if day < 0 else "post7" if day <= 7 else "post8_14",
                "source_type": old.get("source_type") or article["source_type"], "source": article["source"],
                "headline": article["headline"], "language": article["language"],
                "banks_matched": "; ".join(matched), "ngo_name_matched": int(has_ngo),
                "priority": "bank_and_ngo" if matched and has_ngo else "bank_only" if matched else "ngo_only",
                "article_excerpt": excerpt(article, aliases), "campaign_report": campaign["report"],
                "story_group_id": article["story_group_id"], "input_file": article["input_file"],
            })
    return sorted(review, key=lambda row: (row["uid_archive"], row["article_date"], row["source"], row["article_id"]))


def write_review_html(path, review, campaigns, articles):
    esc = lambda value: html.escape(str(value), quote=True)
    data = [{field: row.get(field, "") for field in REVIEW_FIELDS} for row in review]
    revision = hashlib.sha256(json.dumps([
        [r[k] for k in ("uid_archive", "article_id", "review_decision", "review_note", "reviewer", "banks_relevant")]
        for r in data
    ], ensure_ascii=True).encode()).hexdigest()[:16]
    payload = json.dumps(dict(rows=data, fields=REVIEW_FIELDS, revision=revision), ensure_ascii=True).replace("<", r"\u003c")
    grouped = defaultdict(list)
    for index, row in enumerate(review):
        grouped[(row["uid_archive"], row["story_group_id"])].append(index)
    blocks = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Factiva pilot review</title>',
        '<style>body{font:16px/1.5 system-ui;max-width:1050px;margin:35px auto;padding:0 20px;color:#19283a;background:#f7f8fa}section,.story{background:white;padding:22px;margin:20px 0;border:1px solid #d6dee7;border-radius:8px}.story{background:#fafbfd}pre{white-space:pre-wrap;font:inherit}small{color:#526172}summary{cursor:pointer;font-weight:600}nav{display:flex;flex-wrap:wrap;gap:12px}h1,h2,h3{line-height:1.25}.toolbar{position:sticky;top:0;background:#fff;padding:12px;border:1px solid #d6dee7;z-index:2}.fields{display:grid;grid-template-columns:1fr 2fr;gap:12px}.notes{grid-column:1/-1}label{font-size:14px;font-weight:600}input,select,textarea{display:block;box-sizing:border-box;width:100%;padding:9px;font:inherit;border:1px solid #aebac7;border-radius:4px}button{padding:9px 15px;margin-right:8px;cursor:pointer}td,th{text-align:left;padding:5px;font-size:13px;vertical-align:top}#save-status{margin:8px 0 0;font-size:14px}@media(max-width:650px){.fields{grid-template-columns:1fr}td{overflow-wrap:anywhere}}</style></head><body>',
        '<h1>Factiva pilot: review candidate matches</h1>',
        '<p>Edit decisions, banks and notes below. Browser drafts are restored where supported. <b>Save CSV</b> over <code>handoff/factiva/candidate_matches.csv</code> to update the analysis, then rerun A4c. Download CSV is available as a fallback.</p>',
        '<div class="toolbar"><button id="save-csv">Save review CSV</button><button id="download-csv">Download CSV</button><p id="save-status">Ready. CSV is the saved input to the analysis.</p></div>',
        '<noscript><p>Open this page in a browser with JavaScript enabled to edit and save reviews.</p></noscript>',
        '<p>Near-identical editions from one outlet are grouped. A decision applies to every listed version; dates and article IDs remain separate. The earliest date shown is the earliest version in these exports, not proof of the first-ever publication. Wires and unresolved cases remain separate from confirmed newspaper evidence.</p>',
        f'<p><b>{len(grouped)} review items from {len(review)} campaign/article records.</b> Assistant decisions are provisional and can be changed.</p><nav>',
    ]
    blocks.extend(f'<a href="#campaign-{esc(uid)}">{esc(uid)}</a>' for uid in campaigns)
    blocks.append('</nav>')
    for uid, campaign in campaigns.items():
        items = [(key, indices) for key, indices in grouped.items() if key[0] == uid]
        blocks.extend([
            f'<section id="campaign-{esc(uid)}"><h2>{esc(uid)} · {esc(campaign["date"])} · {esc(campaign["ngo"])}</h2>',
            f'<p><b>Target bank names:</b> {esc(campaign["banks"])}</p>',
            f'<details><summary>Campaign description</summary><pre>{esc(campaign["report"])}</pre></details>',
            f'<p>{len(items)} candidate stories.</p>',
        ])
        for _, indices in items:
            rows = [review[i] for i in indices]
            row = rows[0]
            article = articles[row["article_id"]]
            blocks.extend([
                f'<article class="story" data-rows="{",".join(map(str, indices))}"><h3>{esc(row["headline"])}</h3>',
                f'<small>{esc(article["source_family"])} · {len(rows)} publication version(s) · earliest exported version {esc(article["story_first_date"])}</small>',
                '<div class="fields"><label>Review decision<select data-field="review_decision">',
            ])
            values = {r["review_decision"] for r in rows}
            selected = row["review_decision"] if len(values) == 1 else "mixed"
            for value, label in [("", "Pending"), ("yes", "Relevant"), ("no", "Unrelated"), ("unsure", "Unsure"), ("mixed", "Different saved decisions")]:
                blocks.append(f'<option value="{value}"{" selected" if value == selected else ""}>{label}</option>')
            blocks.extend([
                '</select></label>',
                f'<label>Banks actually covered<input data-field="banks_relevant" value="{esc(row["banks_relevant"])}" placeholder="Target bank names, separated by semicolons"></label>',
                f'<label class="notes">Notes<textarea data-field="review_note" rows="3">{esc(row["review_note"])}</textarea></label></div>',
                f'<details><summary>Matching passages</summary><pre>{esc(row["article_excerpt"])}</pre></details>',
                f'<details><summary>Full article — first listed version</summary><pre>{esc(article["body"])}</pre></details>',
                '<details><summary>Publication versions and dates</summary><table><tr><th>Date / relative day</th><th>Outlet</th><th>Article ID</th></tr>',
            ])
            for version in rows:
                blocks.append(f'<tr><td>{esc(version["article_date"])} / {version["relative_day"]:+d}</td><td>{esc(version["source"])}</td><td>{esc(version["article_id"])}</td></tr>')
            blocks.append('</table>')
            for version in rows[1:]:
                other = articles[version["article_id"]]
                if other["body"] != article["body"]:
                    blocks.append(f'<details><summary>Version text: {esc(version["article_date"])} · {esc(version["article_id"])}</summary><pre>{esc(other["body"])}</pre></details>')
            blocks.append('</details></article>')
        blocks.append('</section>')
    blocks.extend([f'<script type="application/json" id="review-data">{payload}</script>',
                   '<script src="../../factiva_review.js"></script></body></html>'])
    path.write_text("\n".join(blocks), encoding="utf-8")


def process(input_dir, handoff):
    out = handoff / "factiva"
    out.mkdir(exist_ok=True)
    campaigns = {r["uid_archive"]: r for r in read_csv(handoff / "factiva_pilot.csv")}
    manifest = read_csv(handoff / "factiva_query_batches.csv")
    files = sorted(input_dir.glob("*.rtf"))
    if not files:
        raise ValueError(f"No .rtf exports found in {input_dir}")
    exports, articles, groups = [], {}, defaultdict(list)
    for path in files:
        export = parse_export(path)
        batch = find_batch(export, manifest, campaigns)
        export.update(batch_id=batch["batch_id"], batch=batch)
        actual = query_key(export["query"])
        export["included_uids"] = [
            uid for uid in batch["uid_archives"].split(";")
            if query_key(campaign_query(campaigns[uid])) in actual
        ]
        export["missing_uids"] = [uid for uid in batch["uid_archives"].split(";") if uid not in export["included_uids"]]
        groups[(actual, export["date_from"], export["date_to"], normalize(export["source_filter"]), normalize(export["language_filter"]))].append(export)
        exports.append(export)
        for article in export["articles"]:
            article["batch_id"] = batch["batch_id"]
            previous = articles.get(article["article_id"])
            if previous and previous["body"] != article["body"]:
                raise ValueError(f"Conflicting versions of article {article['article_id']}")
            articles.setdefault(article["article_id"], article)
    group_story_versions(articles)
    for members in groups.values():
        observed = {a["article_id"] for e in members for a in e["articles"]}
        counts = {e["reported_results"] for e in members}
        for export in members:
            export["count_matches"] = len(counts) == 1 and len(observed) == next(iter(counts))
    audit = [{
        "input_file": e["path"].name, "batch_id": e["batch_id"],
        "reported_results": e["reported_results"], "articles_in_file": len(e["articles"]),
        "count_matches_across_parts": int(e["count_matches"]),
        "actual_date_from": e["date_from"], "actual_date_to": e["date_to"],
        "expected_date_from": e["batch"]["date_from"], "expected_date_to": e["batch"]["date_to"],
        "missing_campaign_clauses": ";".join(e["missing_uids"]),
        "source_filter": e["source_filter"], "language_filter": e["language_filter"],
        "actual_query": e["query"],
    } for e in exports]
    write_csv(out / "search_audit.csv", audit)
    write_csv(out / "articles.csv", list(articles.values()))
    review_path = out / "candidate_matches.csv"
    previous = {(r["uid_archive"], r["article_id"]): r for r in read_csv(review_path)} if review_path.exists() else {}
    review = prepare_candidates(campaigns, articles, previous)
    if any(r["review_decision"].strip().lower() not in {"", "yes", "no", "unsure"} for r in review):
        raise ValueError("Review decisions must be blank, yes, no or unsure")
    for row in review:
        if row["source_type"] not in {"press", "wire", "excluded"}:
            raise ValueError("Source type must be press, wire or excluded")
        if row["review_decision"].strip().lower() == "yes":
            relevant = {bank.strip() for bank in row["banks_relevant"].split(";") if bank.strip()}
            targeted = {bank.strip() for bank in campaigns[row["uid_archive"]]["banks"].split(";")}

    statuses, coverage, fixes = [], [], []
    for uid, campaign in campaigns.items():
        included = [e for e in exports if uid in e["included_uids"]]
        full_dates = covers_interval([(e["date_from"], e["date_to"]) for e in included], campaign["search_start"], campaign["search_end"])
        full_export = covers_interval([(e["date_from"], e["date_to"]) for e in included if e["count_matches"]], campaign["search_start"], campaign["search_end"])
        candidates = [r for r in review if r["uid_archive"] == uid]
        accepted = [r for r in candidates if r["review_decision"].strip().lower() == "yes"]
        reasons = []
        if not included:
            reasons.append("campaign clause absent from downloaded searches")
        elif not full_dates:
            reasons.append("campaign date window not fully searched")
        if included and not full_export and full_dates:
            reasons.append("export count needs checking")
        if not included or not full_dates:
            fixes.append((campaign, reasons))
        statuses.append({
            "uid_archive": uid, "date": campaign["date"], "ngo": campaign["ngo"],
            "clause_present": int(bool(included)), "date_window_complete": int(full_dates),
            "export_count_complete": int(full_export), "candidate_records": len(candidates),
            "pending_reviews": sum(r["review_decision"].strip().lower() in {"", "unsure"} for r in candidates),
            "accepted_records": len(accepted), "collection_issues": "; ".join(reasons),
            "allegation_search_complete": "",
        })
        press7 = [r for r in accepted if r["source_type"] == "press" and 0 <= r["relative_day"] <= 7]
        pre7 = [r for r in accepted if r["source_type"] == "press" and -7 <= r["relative_day"] < 0]
        press14 = [r for r in accepted if r["source_type"] == "press" and 0 <= r["relative_day"] <= 14]
        wire7 = [r for r in accepted if r["source_type"] == "wire" and 0 <= r["relative_day"] <= 7]
        coverage.append({
            "uid_archive": uid, "media": 1 if press7 else "",
            "media_pre7": 1 if pre7 else "", "media_post14": 1 if press14 else "",
            "wire_post7": 1 if wire7 else "",
            "confirmed_press_stories_post7": len({r["story_group_id"] for r in press7}),
            "first_confirmed_press_date": min((r["article_date"] for r in accepted if r["source_type"] == "press"), default=""),
            "status": "positive_evidence" if press7 else "unresolved",
        })
    write_csv(out / "campaign_status.csv", statuses)
    write_csv(out / "media_coverage_pilot.csv", coverage)
    bank_coverage = []
    for uid, campaign in campaigns.items():
        for bank in (name.strip() for name in campaign["banks"].split(";")):
            accepted = [r for r in review if r["uid_archive"] == uid
                        and r["review_decision"].strip().lower() == "yes"
                        and bank in {name.strip() for name in r["banks_relevant"].split(";")}]
            press = [r for r in accepted if r["source_type"] == "press"]
            bank_coverage.append({
                "uid_archive": uid, "ds_name": bank,
                "media": 1 if any(0 <= r["relative_day"] <= 7 for r in press) else "",
                "media_pre7": 1 if any(-7 <= r["relative_day"] < 0 for r in press) else "",
                "media_post14": 1 if any(0 <= r["relative_day"] <= 14 for r in press) else "",
                "evidence_article_ids": ";".join(r["article_id"] for r in accepted),
            })
    write_csv(out / "media_coverage_bank_pilot.csv", bank_coverage)
    followups = [
        "FACTIVA: SEARCH REPAIRS FOUND IN THE DOWNLOADED EXPORTS",
        "Keep the same source panel and Full Article search setting.",
        "These repair omitted clauses/date windows. Before coding a zero, also complete",
        "bank + allegation/report follow-up searches and review all candidates.", "",
    ]
    for campaign, reasons in fixes:
        followups.extend([
            f"Campaign {campaign['uid_archive']} | {campaign['ngo']}",
            "Reason: " + "; ".join(reasons),
            f"Date filter: {campaign['search_start']} through {campaign['search_end']} inclusive",
            campaign_query(campaign), "",
        ])
    for export in exports:
        if not export["count_matches"]:
            followups.extend([
                f"EXPORT CHECK: {export['path'].name} ({export['batch_id']})",
                f"Search summary reports {export['reported_results']} results; {len(export['articles'])} article records are in this file.",
                "Check for an omitted result or a hidden duplicate; do not assume a completed negative search.", "",
            ])
    (out / "followup_queries.txt").write_text("\n".join(followups), encoding="utf-8")
    write_review_html(out / "review.html", review, campaigns, articles)
    (out / "README.md").write_text("""# Factiva pilot processing

Accession numbers identify publication records. story_group_id groups identical text and near-identical
versions (same outlet/title, at most two days apart, text similarity at least 98%).
Review shows a grouped story once but preserves every date and article ID for timing analysis.
Dates come from the PD publication-date field, not the date inside an accession number.
Source-type labels are a first pass: check actual outlets against the intended source panel.
The selected sources can include wires and entire publication families.
This balanced 30-campaign pilot does not estimate the population coverage rate.
""", encoding="utf-8")
    print(f"Imported {len(exports)} exports, {sum(len(e['articles']) for e in exports)} records, {len(articles)} unique article IDs.")
    print(f"Prepared {len(review)} candidate pairs for {len({r['uid_archive'] for r in review})} campaigns.")
    print(f"Search repairs needed for {len(fixes)} campaigns; see {out / 'followup_queries.txt'}.")
    print(f"Review: {out / 'review.html'}")
    return dict(articles=articles, review=review, audit=audit, statuses=statuses, coverage=coverage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handoff", type=Path, default=Path("handoff"))
    parser.add_argument("--input-dir", type=Path, default=Path("handoff/factiva_exports"))
    parser.add_argument("--import-dir", type=Path, help="Copy Factiva-*.rtf exports here, leaving originals intact.")
    args = parser.parse_args()
    args.input_dir.mkdir(parents=True, exist_ok=True)
    if args.import_dir:
        for path in sorted(args.import_dir.glob("Factiva-*.rtf")):
            if parse_export(path)["articles"]:
                destination = args.input_dir / path.name
                if path.resolve() != destination.resolve():
                    if destination.exists() and destination.read_bytes() != path.read_bytes():
                        raise ValueError(f"Import would overwrite a different file: {destination}")
                    shutil.copy2(path, destination)
    process(args.input_dir, args.handoff)


if __name__ == "__main__":
    main()

