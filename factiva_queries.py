"""Prepare copyable Factiva batches from the pilot; does not access Factiva."""

import argparse
import csv
from collections import defaultdict
from datetime import date
from pathlib import Path


# Public names, rather than the abbreviated Datastream labels in the sample.
BANK_ALIASES = {
    "abn amro bank": ["ABN AMRO"],
    "banco de sabadell": ["Banco Sabadell", "Banco de Sabadell"],
    "banco santander": ["Santander"],
    "bank of america": ["Bank of America"],
    "bankia": ["Bankia"],
    "barclays": ["Barclays"],
    "bbv.argentaria": ["BBVA", "Banco Bilbao Vizcaya Argentaria"],
    "bnp paribas": ["BNP Paribas"],
    "caixabank": ["CaixaBank", "La Caixa"],
    "citigroup": ["Citigroup", "Citibank", "Citi"],
    "commerzbank": ["Commerzbank"],
    "credit agricole": ["Credit Agricole", "Crédit Agricole"],
    "deutsche bank": ["Deutsche Bank"],
    "erste group bank": ["Erste Group", "Erste Bank"],
    "goldman sachs gp.": ["Goldman Sachs"],
    "hsbc holdings": ["HSBC", "First Direct"],
    "ing groep": ["ING", "ING Group"],
    "intesa sanpaolo": ["Intesa Sanpaolo"],
    "jp morgan chase & co.": ["JPMorgan", "JP Morgan", "J.P. Morgan", "Chase"],
    "kbc group": ["KBC"],
    "lloyds banking group": ["Lloyds", "Lloyds TSB", "Halifax"],
    "metro bank": ["Metro Bank"],
    "pnc finl.svs.gp.": ["PNC"],
    "royal bank of sctl.gp.": ["Royal Bank of Scotland", "RBS", "NatWest"],
    "societe generale": ["Societe Generale", "Société Générale"],
    "standard chartered": ["Standard Chartered"],
    "tcf financial": ["TCF Financial", "TCF Bank"],
    "unicredit": ["UniCredit"],
    "us bancorp": ["US Bancorp", "U.S. Bancorp", "US Bank", "U.S. Bank"],
    "van lanschot kempen": ["Van Lanschot", "Van Lanschot Bankiers"],
    "wells fargo & co": ["Wells Fargo"],
}

NGO_ALIASES = {
    "ADICAE": ["ADICAE"],
    "Amigos de la Tierra FoE Spain": ["Amigos de la Tierra", "Friends of the Earth"],
    "Amis de la Terre France / FoE France": ["Amis de la Terre", "Friends of the Earth"],
    "CEE Bankwatch Network": ["Bankwatch"],
    "Climate Action Network Europe / CAN Europe": ["Climate Action Network", "CAN Europe"],
    "Consumentenbond": ["Consumentenbond"],
    "FairFin (previously Netwerk Vlaanderen)": ["FairFin", "Netwerk Vlaanderen"],
    "FERN Forests and the European Union Resource Network": ["FERN", "Forests and the European Union Resource Network"],
    "Friends of the Earth Scotland / FoE Scotland": ["Friends of the Earth Scotland", "Friends of the Earth"],
    "Greenpeace France": ["Greenpeace"],
    "Institute for Policy Studies U.S.": ["Institute for Policy Studies"],
    "International Campaign to Abolish Nuclear Weapons (ICAN)": ["International Campaign to Abolish Nuclear Weapons", "ICAN"],
    "Market Forces (Australia)": ["Market Forces"],
    "Milieudefensie / FoE Netherlands": ["Milieudefensie", "Friends of the Earth"],
    "Oxfam Agir Ici France": ["Oxfam", "Agir Ici"],
    "Oxfam Novib Netherlands": ["Oxfam", "Novib"],
    "Rainforest Action Network / RAN": ["Rainforest Action Network"],
    "Rainforest Action Network RAN": ["Rainforest Action Network"],
    "Red Metropolitana No Alto Maipo": ["Red Metropolitana No Alto Maipo", "No Alto Maipo"],
    "ResponsiBank Indonesia": ["ResponsiBank", "Responsibank"],
    "SETEM": ["SETEM"],
    "Stop Huntingdon Animal Cruelty / SHAC": ["Stop Huntingdon Animal Cruelty", "SHAC"],
    "Tax Justice Network TJN": ["Tax Justice Network"],
    "U.S. PIRG": ["US PIRG", "U.S. PIRG", "Public Interest Research Group"],
    "War On Want": ["War on Want"],
}


def or_terms(terms):
    # Ordinary straight quotes, with duplicate aliases removed ignoring case.
    unique = {}
    for term in terms:
        clean = term.replace('"', "").strip()
        if clean:
            unique.setdefault(clean.casefold(), clean)
    if not unique:
        raise ValueError("An empty alias list cannot form a query.")
    return "(" + " OR ".join(f'"{term}"' for term in unique.values()) + ")"


def campaign_query(row):
    aliases = []
    for bank in row["banks"].split(";"):
        bank = bank.strip()
        if bank not in BANK_ALIASES:
            raise ValueError(f"Add public-name aliases for bank: {bank}")
        aliases.extend(BANK_ALIASES[bank])
    ngo = row["ngo"]
    if ngo == "Which? (UK Consumers Association)":
        # The word 'which' alone is too ambiguous for a useful bank query.
        ngo_clause = (
            '("Consumers Association" OR '
            '("Which" NEAR5 (consumer* OR watchdog OR survey OR report OR research)))'
        )
    elif ngo in NGO_ALIASES:
        ngo_clause = or_terms(NGO_ALIASES[ngo])
    else:
        raise ValueError(f"Add public-name aliases for NGO: {ngo}")
    return f"({or_terms(aliases)} AND {ngo_clause})"


def make_batches(rows, max_chars=1800):
    """Keep bank-NGO pairs together; exact campaign dates are applied after export."""
    uids = [row["uid_archive"] for row in rows]
    if len(set(uids)) != len(uids):
        raise ValueError("The pilot must contain distinct campaign IDs.")
    by_year = defaultdict(list)
    for row in sorted(rows, key=lambda item: (item["date"], item["uid_archive"])):
        campaign_date = date.fromisoformat(row["date"])
        start, end = date.fromisoformat(row["search_start"]), date.fromisoformat(row["search_end"])
        if not start <= campaign_date <= end:
            raise ValueError(f"Invalid date window for {row['uid_archive']}")
        query = campaign_query(row)
        if len(query) > max_chars:
            raise ValueError(f"Campaign {row['uid_archive']} exceeds max_chars; split its bank aliases or increase the setting.")
        by_year[campaign_date.year].append((row, query))

    batches = []

    def add_batch(items, year):
        source_rows = [item[0] for item in items]
        query = "\nOR\n".join(item[1] for item in items)
        batches.append({
            "batch_id": f"batch_{len(batches) + 1:02d}",
            "campaign_year": year,
            "uid_archives": ";".join(row["uid_archive"] for row in source_rows),
            "n_campaigns": len(source_rows),
            "date_from": min(row["search_start"] for row in source_rows),
            "date_to": max(row["search_end"] for row in source_rows),
            "query_characters": len(query),
            "query": query,
            "export_filename": f"factiva_batch_{len(batches) + 1:02d}.rtf",
            "result_count": "",
            "export_complete": "",
            "review_complete": "",
        })

    for year, entries in sorted(by_year.items()):
        pending = []
        for entry in entries:
            proposed = pending + [entry]
            if pending and len("\nOR\n".join(item[1] for item in proposed)) > max_chars:
                add_batch(pending, year)
                pending = []
            pending.append(entry)
        if pending:
            add_batch(pending, year)
    return batches


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("handoff/factiva_pilot.csv"))
    parser.add_argument("--max-chars", type=int, default=1800, help="Conservative batch size; adjust for your interface if needed.")
    args = parser.parse_args()
    with args.input.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("The pilot file is empty.")
    batches = make_batches(rows, max_chars=args.max_chars)
    output_dir = args.input.parent
    manifest_path = output_dir / "factiva_query_batches.csv"
    with manifest_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(batches[0]))
        writer.writeheader()
        writer.writerows(batches)

    instructions = [
        "FACTIVA PILOT: COPY-AND-PASTE SEARCH BATCHES",
        "",
        "These are bank + lead-NGO queries, not verified article matches.",
        "Set your agreed newspaper source panel once, with sources joined by OR.",
        "Search Full Article, use All Languages and All Regions, and Duplicates Off.",
        "For each batch, change the Date filter to the date_from/date_to below.",
        "Copy only the Boolean query into Free Text Search; dates remain in the Date filter.",
        "Export all candidate full articles in the batch, retaining metadata and IDs.",
        "The suggested RTF filename links each export to its campaigns.",
        "",
        "IMPORTANT: a batch has a broader date window than an individual campaign.",
        "After export, filter each candidate against its own campaign's search_start/search_end.",
        "Check the actual allegation and banks mentioned before coding coverage.",
        "A zero in this lead-NGO search is provisional: run a bank + allegation/report query",
        "for unmatched campaigns because articles can omit the NGO name.",
        "This script does not run searches, translate articles or download Factiva content.",
        "",
    ]
    for batch in batches:
        instructions.extend([
            "=" * 72,
            f"{batch['batch_id']} | {batch['n_campaigns']} campaign(s)",
            f"Campaign IDs: {batch['uid_archives']}",
            f"Date filter: {batch['date_from']} through {batch['date_to']} (inclusive)",
            f"Suggested export: {batch['export_filename']}",
            f"Query length: {batch['query_characters']} characters",
            "",
            batch["query"],
            "",
        ])
    text_path = output_dir / "factiva_query_batches.txt"
    text_path.write_text("\n".join(instructions), encoding="utf-8")
    print(f"Prepared {len(batches)} batches for {len(rows)} campaigns.")
    print(f"Copyable queries: {text_path}")
    print(f"Search log: {manifest_path}")


if __name__ == "__main__":
    main()
