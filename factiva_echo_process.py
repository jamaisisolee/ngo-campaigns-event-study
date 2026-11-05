"""Audit the fixed 300-campaign Factiva collection and prepare relevance review.

Original RTF filenames are retained. Uses the existing factiva_process.py RTF
reader, the sample dictionaries and the saved batch manifest. No network access.
"""

import argparse
import csv
import hashlib
import importlib.util
import json
import re
import sys
import html
from functools import lru_cache
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from echo_aliases import BANK_ALIASES, NGO_ALIASES
from factiva_echo_queries import campaign_query
from echo_duplicates import flag_duplicates


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path, rows, fields=None):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or list(rows[0]), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_reader(project):
    sys.dont_write_bytecode = True
    sys.path.append(str(project))
    spec = importlib.util.spec_from_file_location("factiva_pilot_reader", project / "factiva_process.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def split_clauses(query):
    """Identify complete top-level campaign clauses, retaining connector errors."""
    quoted, depth, start = False, 0, None
    clauses, gaps, previous_end = [], [], 0
    for i, char in enumerate(query):
        if char == '"':
            quoted = not quoted
        elif not quoted and char == "(":
            if depth == 0:
                gap = query[previous_end:i].strip()
                if clauses and gap.upper() != "OR":
                    gaps.append(gap or "missing OR between clauses")
                elif not clauses and gap:
                    gaps.append(gap)
                start = i
            depth += 1
        elif not quoted and char == ")":
            depth -= 1
            if depth < 0:
                gaps.append("unbalanced closing parenthesis")
                depth = 0
            elif depth == 0 and start is not None:
                clauses.append(query[start:i+1])
                previous_end = i+1
                start = None
    if depth or quoted:
        gaps.append("unbalanced parentheses or quotes")
    if query[previous_end:].strip():
        gaps.append("trailing query text")
    return clauses, gaps


def source_details(source, normalize):
    key = normalize(source)
    if "blog" in key:
        return "excluded", "blog"
    if "reuters" in key:
        return "wire", "core"
    extras = ("financial times deutschland", "wirtschaftswoche", "enjeux les echos", "les echos week end")
    if any(extra in key for extra in extras):
        return "press", "publisher_extra"
    return "press", "core"


def process(project, sample_path, manifest_path, input_dir, output_dir):
    project, input_dir, output_dir = Path(project), Path(input_dir), Path(output_dir)
    base = load_reader(project)
    sample = read_csv(sample_path)
    campaigns = {r["uid_archive"]: r for r in sample}
    if len(campaigns) != len(sample):
        raise ValueError("The sample has duplicated campaign IDs.")
    manifest = read_csv(manifest_path)
    query_by_uid = {uid: base.query_key(campaign_query(r)) for uid, r in campaigns.items()}
    files = sorted(p for p in input_dir.iterdir() if p.suffix.lower() == ".rtf")
    if not files:
        raise ValueError(f"No RTF exports found in {input_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    exports, articles, export_groups = [], {}, defaultdict(list)
    for path in files:
        e = base.parse_export(path)
        actual = base.query_key(e["query"])
        scores = sorted((sum(query_by_uid[uid] in actual for uid in b["uid_archives"].split(";")), b["batch_id"], b) for b in manifest)
        best = scores[-1]
        if best[0] == 0 or (len(scores) > 1 and scores[-2][0] == best[0]):
            raise ValueError(f"Cannot uniquely associate {path.name} with a batch")
        batch = best[2]
        clauses, gaps = split_clauses(e["query"])
        clause_keys = {base.query_key(q) for q in clauses}
        e.update(batch=batch, batch_id=batch["batch_id"], connector_issues=gaps,
                 included_uids=[uid for uid in batch["uid_archives"].split(";") if query_by_uid[uid] in clause_keys],
                 exact_batch=int(actual == base.query_key(batch["query"])))
        export_groups[(actual, e["date_from"], e["date_to"], e["source_filter"], e["language_filter"])].append(e)
        exports.append(e)
        for article in e["articles"]:
            article["source_type"], article["source_panel_scope"] = source_details(article["source"], base.normalize)
            old = articles.get(article["article_id"])
            if old and old["body"] != article["body"]:
                raise ValueError(f"Conflicting text under accession {article['article_id']}")
            if not old:
                article["input_files"] = path.name
                articles[article["article_id"]] = article
            else:
                old["input_files"] += "; " + path.name
    for group in export_groups.values():
        ids = {a["article_id"] for e in group for a in e["articles"]}
        expected = {e["reported_results"] for e in group}
        for e in group:
            e["count_matches"] = len(expected) == 1 and len(ids) == next(iter(expected))
            e["unique_records_across_parts"] = len(ids)
    base.group_story_versions(articles)
    duplicate_edges = flag_duplicates(articles)
    audit = []
    for e in exports:
        audit.append({
            "input_file": e["path"].name, "batch_id": e["batch_id"], "exact_batch_query": e["exact_batch"],
            "reported_results": e["reported_results"], "article_records_in_file": len(e["articles"]),
            "unique_records_across_parts": e["unique_records_across_parts"], "export_count_complete": int(e["count_matches"]),
            "actual_date_from": e["date_from"], "actual_date_to": e["date_to"],
            "query_connector_issues": "; ".join(e["connector_issues"]),
            "exact_campaign_clauses": ";".join(e["included_uids"]),
            "changed_or_omitted_campaign_clauses": ";".join(uid for uid in e["batch"]["uid_archives"].split(";") if uid not in e["included_uids"]),
            "source_filter": e["source_filter"], "language_filter": e["language_filter"], "actual_query": e["query"],
        })
    write_csv(output_dir / "search_audit.csv", audit)
    write_csv(output_dir / "articles.csv", list(articles.values()))
    duplicate_fields = ["article_id", "article_date", "source", "outlet_family", "headline",
                        "is_duplicate", "duplicate_of", "duplicate_kind", "duplicate_group_id", "duplicate_group_size",
                        "count_story_id", "related_story_versions", "has_updated_versions", "deduplicated_first_date",
                        "export_occurrences", "repeated_in_exports", "input_files"]
    write_csv(output_dir / "article_duplicates.csv", list(articles.values()), duplicate_fields)
    write_csv(output_dir / "duplicate_links.csv",
              [dict(article_a=a, article_b=b, method=method, text_similarity=score) for a, b, method, score in duplicate_edges],
              ["article_a", "article_b", "method", "text_similarity"])

    @lru_cache(maxsize=None)
    def alias_pattern(alias):
        return re.compile(r"(?<![a-z0-9])" + re.escape(base.normalize(alias)) + r"(?![a-z0-9])")

    def contains(text, alias):
        return bool(alias_pattern(alias).search(text))

    narratives = {key: base.normalize(a["headline"] + " " + a["body"]) for key, a in articles.items()}
    bank_matches = {key: {bank for bank, aliases in BANK_ALIASES.items() if any(contains(text, a) for a in aliases)} for key, text in narratives.items()}
    previous_path = output_dir / "candidate_matches.csv"
    previous = {(r["uid_archive"], r["article_id"]): r for r in read_csv(previous_path)} if previous_path.exists() else {}
    review = []
    for uid, campaign in campaigns.items():
        targets = {b.strip() for b in campaign["banks"].split(";") if b.strip()}
        ngo_aliases = NGO_ALIASES[campaign["ngo"]]
        for key, article in articles.items():
            if not campaign["search_start"] <= article["article_date"] <= campaign["search_end"]:
                continue
            matched = sorted(targets & bank_matches[key])
            has_ngo = any(contains(narratives[key], a) for a in ngo_aliases)
            if not matched and not has_ngo:
                continue
            relative = (date.fromisoformat(article["article_date"]) - date.fromisoformat(campaign["date"])).days
            aliases = [a for bank in matched for a in BANK_ALIASES[bank]] + ngo_aliases
            old = previous.get((uid, key), {})
            review.append({
                "uid_archive": uid, "article_id": key, "review_decision": old.get("review_decision", ""),
                "review_note": old.get("review_note", ""), "reviewer": old.get("reviewer", ""),
                "banks_relevant": old.get("banks_relevant", ""), "article_date": article["article_date"],
                "relative_day": relative, "window": "pre7" if relative < 0 else "post7" if relative <= 7 else "post8_14",
                "source_type": article["source_type"], "source": article["source"], "headline": article["headline"],
                "language": article["language"], "banks_matched": "; ".join(matched), "ngo_name_matched": int(has_ngo),
                "priority": "bank_and_ngo" if matched and has_ngo else "bank_only" if matched else "ngo_only",
                "article_excerpt": base.excerpt(article, aliases), "campaign_report": campaign["report"],
                "story_group_id": article["story_group_id"], "input_file": article["input_file"],
                "source_panel_scope": article["source_panel_scope"],
                **{k: article[k] for k in ["is_duplicate", "duplicate_of", "duplicate_kind", "duplicate_group_id", "count_story_id", "outlet_family", "has_updated_versions"]},
            })
    review.sort(key=lambda r: (r["uid_archive"], r["article_date"], r["source"], r["article_id"]))
    for r in review:
        if r["review_decision"] not in {"", "yes", "no", "unsure"}:
            raise ValueError("Invalid relevance decision")
        if r["review_decision"] == "yes":
            relevant = {b.strip() for b in r["banks_relevant"].split(";") if b.strip()}
            targets = {b.strip() for b in campaigns[r["uid_archive"]]["banks"].split(";")}
            if not relevant or not relevant <= targets:
                raise ValueError(f"Accepted review must use targeted canonical bank names: {r['uid_archive']} / {r['article_id']}")
    write_csv(output_dir / "candidate_matches.csv", review, base.REVIEW_FIELDS + ["source_panel_scope", "is_duplicate", "duplicate_of", "duplicate_kind", "duplicate_group_id", "count_story_id", "outlet_family", "has_updated_versions"])

    # Preserve human search-status fields across reruns; absence of evidence is not a zero.
    status_path = output_dir / "campaign_status.csv"
    old_status = {r["uid_archive"]: r for r in read_csv(status_path)} if status_path.exists() else {}
    notes_path = output_dir / "collection_notes.json"
    collection_notes = json.loads(notes_path.read_text(encoding="utf-8")) if notes_path.exists() else {}
    statuses, coverage, bank_coverage = [], [], []
    review_by_uid = defaultdict(list)
    for r in review:
        review_by_uid[r["uid_archive"]].append(r)
    for uid, campaign in campaigns.items():
        matched_exports = [e for e in exports if uid in e["included_uids"]]
        safe_exports = [e for e in matched_exports if not e["connector_issues"]]
        dates_ok = base.covers_interval([(e["date_from"], e["date_to"]) for e in safe_exports], campaign["search_start"], campaign["search_end"])
        counts_ok = base.covers_interval([(e["date_from"], e["date_to"]) for e in safe_exports if e["count_matches"]], campaign["search_start"], campaign["search_end"])
        reasons = []
        if not matched_exports:
            reasons.append("planned campaign clause changed or absent")
        if not dates_ok:
            reasons.append("planned search/date coverage incomplete or connector issue")
        if dates_ok and not counts_ok:
            reasons.append("reported results not fully exported")
        rows = review_by_uid[uid]
        pending = sum(r["review_decision"] in {"", "unsure"} for r in rows)
        accepted = [r for r in rows if r["review_decision"] == "yes" and r["source_type"] != "excluded"]
        allegation_complete = old_status.get(uid, {}).get("allegation_search_complete", "")
        no_echo_confirmed = bool(dates_ok and counts_ok and pending == 0 and allegation_complete == "1")
        press = [r for r in accepted if r["source_type"] == "press" and r["source_panel_scope"] == "core"]
        wires = [r for r in accepted if r["source_type"] == "wire"]
        p7 = [r for r in press if 0 <= r["relative_day"] <= 7]
        batch_ids = {b["batch_id"] for b in manifest if uid in b["uid_archives"].split(";")}
        access_note = "; ".join(collection_notes[b] for b in sorted(batch_ids) if b in collection_notes)
        statuses.append({"uid_archive": uid, "date": campaign["date"], "ngo": campaign["ngo"],
                         "exact_clause_present": int(bool(matched_exports)), "date_window_complete": int(dates_ok),
                         "export_count_complete": int(counts_ok), "candidate_records": len(rows), "pending_reviews": pending,
                         "accepted_records": len(accepted), "collection_issues": "; ".join(reasons),
                         "allegation_search_complete": allegation_complete,
                         "access_restricted": int(bool(access_note)), "access_note": access_note})
        coverage.append({"uid_archive": uid, "media": 1 if p7 else 0 if no_echo_confirmed else "",
                         "media_pre7": 1 if any(-7 <= r["relative_day"] < 0 for r in press) else "",
                         "media_post14": 1 if any(0 <= r["relative_day"] <= 14 for r in press) else "",
                         "wire_post7": 1 if any(0 <= r["relative_day"] <= 7 for r in wires) else "",
                         "publisher_extra_post7": 1 if any(r["source_panel_scope"] == "publisher_extra" and 0 <= r["relative_day"] <= 7 for r in accepted) else "",
                         "confirmed_press_stories_post7": len({r["count_story_id"] for r in p7}),
                         "confirmed_press_outlets_post7": len({r["outlet_family"] for r in p7}),
                         "first_confirmed_press_date": min((r["article_date"] for r in press), default=""),
                         "first_confirmed_press_post7_date": min((r["article_date"] for r in p7), default=""),
                         "status": "positive_evidence" if p7 else "verified_zero" if no_echo_confirmed else "unresolved"})
        for bank in (b.strip() for b in campaign["banks"].split(";") if b.strip()):
            br = [r for r in accepted if bank in {b.strip() for b in r["banks_relevant"].split(";")}]
            bp = [r for r in br if r["source_type"] == "press" and r["source_panel_scope"] == "core"]
            bank_coverage.append({"uid_archive": uid, "ds_name": bank,
                                  "media": 1 if any(0 <= r["relative_day"] <= 7 for r in bp) else 0 if no_echo_confirmed else "",
                                  "media_pre7": 1 if any(-7 <= r["relative_day"] < 0 for r in bp) else "",
                                  "media_post14": 1 if any(0 <= r["relative_day"] <= 14 for r in bp) else "",
                                  "wire_post7": 1 if any(r["source_type"] == "wire" and 0 <= r["relative_day"] <= 7 for r in br) else "",
                                  "evidence_article_ids": ";".join(r["article_id"] for r in br)})
    write_csv(status_path, statuses)
    write_csv(output_dir / "media_coverage.csv", coverage)
    write_csv(output_dir / "media_coverage_bank.csv", bank_coverage)
    repair = ["SEARCH REPAIRS FOR THE FIXED ECHO SAMPLE", "", "Keep original files and the same actual source panel.", ""]
    for e in exports:
        if not e["count_matches"]:
            next_step = ("Account access restriction recorded: " + collection_notes[e["batch_id"]]
                         if e["batch_id"] in collection_notes else
                         "Export all remaining records for the same search, retaining the search summary.")
            repair.extend([f"{e['path'].name} / {e['batch_id']}: {e['reported_results']} results reported; {e['unique_records_across_parts']} unique records exported.", next_step, ""])
        if e["connector_issues"]:
            repair.extend([f"{e['path'].name} / {e['batch_id']}: query connector issue: {'; '.join(e['connector_issues'])}", ""])
    for row in statuses:
        if row["collection_issues"]:
            c = campaigns[row["uid_archive"]]
            repair.extend([f"Campaign {row['uid_archive']} / {c['ngo']}: {row['collection_issues']}",
                           f"Date filter {c['search_start']} through {c['search_end']}", campaign_query(c), ""])
    (output_dir / "followup_queries.txt").write_text("\n".join(repair), encoding="utf-8")
    base.write_review_html(output_dir / "review.html", review, campaigns, articles)
    page = (output_dir / "review.html").read_text(encoding="utf-8")
    page = page.replace("Factiva pilot", "Factiva 300-campaign sample").replace("handoff/factiva/candidate_matches.csv", "handoff/echo_processed/candidate_matches.csv").replace("rerun A4c", "rerun the echo processor").replace("../../factiva_review.js", "echo_review.js")
    page = page.replace("<nav>", '<p><a href="article_duplicates.csv">Download duplicate flags</a> · <a href="search_audit.csv">Download search audit</a></p><nav>', 1)
    page = page.replace("<th>Article ID</th>", "<th>Article ID</th><th>Copy/update flag</th>")
    for article in articles.values():
        key = article["article_id"]
        flag = article["duplicate_kind"]
        if article["duplicate_of"]:
            flag += " of " + article["duplicate_of"]
        if article["has_updated_versions"]:
            flag += "; related updated versions retained"
        page = page.replace(f"<td>{html.escape(key)}</td></tr>", f"<td>{html.escape(key)}</td><td>{html.escape(flag)}</td></tr>")
    (output_dir / "review.html").write_text(page, encoding="utf-8")
    js = (project / "factiva_review.js").read_text(encoding="utf-8").replace("handoff/factiva/candidate_matches.csv", "handoff/echo_processed/candidate_matches.csv").replace("Rerun A4c", "Rerun the echo processor").replace("rerun A4c", "rerun the echo processor")
    (output_dir / "echo_review.js").write_text(js, encoding="utf-8")
    state = {"campaigns": campaigns, "articles": articles, "review": review, "audit": audit, "statuses": statuses, "coverage": coverage}
    (output_dir / "review_state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    summary = {"exports": len(exports), "publication_records": sum(len(e["articles"]) for e in exports),
               "unique_article_ids": len(articles), "campaigns": len(campaigns), "candidate_pairs": len(review),
               "candidate_story_pairs": len({(r["uid_archive"], r["story_group_id"]) for r in review}),
               "campaigns_with_candidates": len({r["uid_archive"] for r in review}),
               "decisions": dict(Counter(r["review_decision"] or "pending" for r in review)),
               "campaigns_with_collection_issues": sum(bool(r["collection_issues"]) for r in statuses),
               "source_filter_variants": len({e["source_filter"] for e in exports}),
               "flagged_copy_records": sum(a["is_duplicate"] for a in articles.values()),
               "repeated_export_records": sum(len(e["articles"]) for e in exports) - len(articles),
               "unique_text_groups": len({a["duplicate_group_id"] for a in articles.values()}),
               "distinct_story_groups_including_updates": len({a["count_story_id"] for a in articles.values()})}
    (output_dir / "processing_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary))
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--sample", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    process(args.project, args.sample or args.project / "handoff/echo_expansion/echo_sample_search.csv",
            args.manifest or args.project / "handoff/echo_expansion/echo_query_batches.csv",
            args.input_dir or args.project / "handoff/echo_exports",
            args.output_dir or args.project / "handoff/echo_processed")
