"""Create copyable Factiva queries for a fixed sample, using echo_aliases.py.
Run locally with an authorised sample; this script does not access Factiva.
"""

import argparse
import csv
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from echo_aliases import BANK_ALIASES, NGO_ALIASES, NGO_QUERY_OVERRIDES


def _lookup(mapping, key):
    if key in mapping:
        return mapping[key]
    matches = [v for k, v in mapping.items() if k.strip().casefold() == key.strip().casefold()]
    if len(matches) == 1:
        return matches[0]
    raise ValueError(f"Missing or ambiguous dictionary entry: {key!r}")


def or_terms(terms):
    """Quote literal terms; Boolean expressions live in NGO_QUERY_OVERRIDES."""
    unique = {}
    for value in terms:
        clean = " ".join(str(value).split()).replace('"', "")
        if clean:
            unique.setdefault(clean.casefold(), clean)
    if not unique:
        raise ValueError("An empty alias list cannot form a query.")
    return "(" + " OR ".join(f'"{term}"' for term in unique.values()) + ")"


def campaign_query(row):
    banks = [b.strip() for b in row["banks"].split(";") if b.strip()]
    if not banks:
        raise ValueError(f"Campaign {row['uid_archive']} has no target banks.")
    bank_clause = or_terms([term for bank in banks for term in _lookup(BANK_ALIASES, bank)])
    ngo = row["ngo"]
    _lookup(NGO_ALIASES, ngo)  # Fail explicitly even when a query override is present.
    override = next((v for k, v in NGO_QUERY_OVERRIDES.items() if k.strip().casefold() == ngo.strip().casefold()), None)
    ngo_clause = override or or_terms(_lookup(NGO_ALIASES, ngo))
    return f"({bank_clause} AND {ngo_clause})"


def prepare_rows(rows, pre_days=7, post_days=14):
    if not rows:
        raise ValueError("The sample is empty.")
    ids = [r["uid_archive"].strip() for r in rows]
    if any(not uid for uid in ids) or len(set(ids)) != len(ids):
        raise ValueError("Expected one non-missing uid_archive per campaign.")
    result = []
    for original in rows:
        row = dict(original)
        row["uid_archive"] = row["uid_archive"].strip()
        campaign_date = date.fromisoformat(row["date"][:10])
        row["date"] = campaign_date.isoformat()
        row["search_start"] = (campaign_date - timedelta(days=pre_days)).isoformat()
        row["search_end"] = (campaign_date + timedelta(days=post_days)).isoformat()
        row["post7_end"] = (campaign_date + timedelta(days=7)).isoformat()
        campaign_query(row)
        result.append(row)
    return result


def make_batches(rows, max_chars=1800):
    if max_chars < 100:
        raise ValueError("max_chars must be at least 100.")
    by_year = defaultdict(dict)
    for row in sorted(rows, key=lambda r: (r["date"], r["uid_archive"])):
        query = campaign_query(row)
        if len(query) > max_chars:
            raise ValueError(f"Campaign {row['uid_archive']} needs {len(query)} characters: split its bank aliases into separate queries, retaining its ID in both.")
        year = date.fromisoformat(row["date"]).year
        group = by_year[year].setdefault(query, {"query": query, "rows": []})
        group["rows"].append(row)
    batches = []

    def add_batch(groups, year):
        source_rows = [r for group in groups for r in group["rows"]]
        query = "\nOR\n".join(group["query"] for group in groups)
        batches.append({
            "batch_id": f"echo_batch_{len(batches) + 1:02d}",
            "campaign_year": year,
            "uid_archives": ";".join(r["uid_archive"] for r in source_rows),
            "n_campaigns": len(source_rows),
            "n_unique_clauses": len(groups),
            "date_from": min(r["search_start"] for r in source_rows),
            "date_to": max(r["search_end"] for r in source_rows),
            "query_characters": len(query),
            "query": query,
            "export_filename": f"factiva_echo_batch_{len(batches) + 1:02d}.rtf",
            "result_count": "", "export_complete": "", "review_complete": "",
        })

    for year, grouped in sorted(by_year.items()):
        pending = []
        for group in grouped.values():
            proposed = pending + [group]
            if pending and len("\nOR\n".join(g["query"] for g in proposed)) > max_chars:
                add_batch(pending, year)
                pending = []
            pending.append(group)
        if pending:
            add_batch(pending, year)
    expected = {r["uid_archive"] for r in rows}
    assigned = [uid for b in batches for uid in b["uid_archives"].split(";")]
    if set(assigned) != expected or len(assigned) != len(expected):
        raise RuntimeError("Batch manifest must assign every sampled campaign exactly once.")
    return batches


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def create_files(sample_path, output_dir, max_chars=1800):
    sample_path, output_dir = Path(sample_path), Path(output_dir)
    with sample_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = prepare_rows(list(csv.DictReader(handle)))
    batches = make_batches(rows, max_chars=max_chars)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "echo_sample_search.csv", rows)
    write_csv(output_dir / "echo_query_batches.csv", batches)
    instructions = [
        "FACTIVA: FIXED ECHO SAMPLE — COPYABLE SEARCH BATCHES", "",
        "Keep the same agreed source panel in every batch, with sources joined by OR.",
        "Use Full Article, All Languages, All Regions, Duplicates Off.",
        "Set the Date filter to the inclusive range shown for each batch.",
        "Copy only the Boolean query. Export all results with full text and metadata.",
        "If an export needs multiple files, keep the batch ID in every filename and reconcile counts.",
        "Identical bank–NGO clauses within a year are searched once; all campaign IDs remain in the manifest.",
        "Apply each campaign's own search window after export. Check allegation, NGO chapter and bank.",
        "A missing match is unresolved until bank + report/allegation follow-up searches are complete.",
        "These dictionaries are proposed retrieval terms, not validated coverage labels.",
        "Use these echo_* files together; the older pilot processor needs its inputs adapted for this sample.", "",
    ]
    for batch in batches:
        instructions.extend([
            "=" * 72,
            f"{batch['batch_id']} | {batch['n_campaigns']} campaigns | {batch['query_characters']} characters",
            f"Campaign IDs: {batch['uid_archives']}",
            f"Date filter: {batch['date_from']} through {batch['date_to']}",
            f"Suggested export: {batch['export_filename']}", "", batch["query"], "",
        ])
    (output_dir / "echo_query_batches.txt").write_text("\n".join(instructions), encoding="utf-8")
    summary = {"campaigns": len(rows), "banks": len({b.strip() for r in rows for b in r["banks"].split(";") if b.strip()}),
               "ngo_names": len({r["ngo"] for r in rows}), "primary_query_batches": len(batches),
               "maximum_query_characters": max(b["query_characters"] for b in batches),
               "output_dir": str(output_dir)}
    print(json.dumps(summary, ensure_ascii=True))
    return rows, batches


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, default=Path("handoff/echo_sample.csv"))
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--max-chars", type=int, default=1800)
    args = parser.parse_args()
    create_files(args.sample, args.output_dir or args.sample.parent / "echo_expansion", args.max_chars)
