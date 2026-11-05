"""
Create the final echo pools from existing local relevance decisions.
Run from the project root: python finalize_echo.py
"""
import argparse
import json
from pathlib import Path

import pandas as pd


def load_csv(path):
    return pd.read_csv(path, dtype={"uid_archive": str}, keep_default_na=False)


def save_csv(frame, path):
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def clean_bank(value):
    return str(value).strip().casefold()


def deduplicate(accepted):
    ordered = accepted.sort_values(["article_date", "article_id"])
    keys = ["uid_archive", "duplicate_group_id"]
    first = ordered.drop_duplicates(keys).copy()
    details = ordered.groupby(keys, as_index=False).agg(
        accepted_editions=("article_id", "nunique"),
        edition_article_ids=("article_id", lambda x: "; ".join(dict.fromkeys(x))),
        banks_in_copy_group=("banks_relevant", lambda x: "; ".join(sorted({b.strip() for s in x for b in s.split(";") if b.strip()}))),
    )
    first = first.merge(details, on=keys, validate="one_to_one")
    first = first.rename(columns={"is_duplicate": "original_is_duplicate", "duplicate_of": "original_duplicate_of"})
    first["kept_in_final_pool"] = 1
    return first


def bank_evidence(accepted):
    expanded = accepted.assign(ds_name=accepted["banks_relevant"].str.split(";")).explode("ds_name")
    expanded["ds_name"] = expanded["ds_name"].map(clean_bank)
    if expanded["ds_name"].eq("").any():
        raise ValueError("Accepted evidence has no relevant bank")
    # Check each bank's first actual appearance, including banks added to a later edition.
    expanded = expanded.sort_values(["article_date", "article_id"])
    return expanded.drop_duplicates(["uid_archive", "ds_name", "source_type", "duplicate_group_id"])


def summarize_evidence(rows):
    press = rows.loc[rows["source_type"].eq("press")]
    wire = rows.loc[rows["source_type"].eq("wire")]
    p7 = press.loc[press["relative_day"].between(0, 7)]
    w7 = wire.loc[wire["relative_day"].between(0, 7)]
    pre = press.loc[press["relative_day"].between(-7, -1)]
    return {
        "media_observed_post7": int(len(p7) > 0),
        "media_observed_pre7": int(len(pre) > 0),
        "media_observed_post14": int(press["relative_day"].between(0, 14).any()),
        "wire_observed_post7": int(len(w7) > 0),
        "news_observed_post7": int(len(p7) > 0 or len(w7) > 0),
        "press_stories_post7": p7["count_story_id"].nunique(),
        "press_outlets_post7": p7["outlet_family"].nunique(),
        "wire_stories_post7": w7["count_story_id"].nunique(),
        "first_press_date": press["article_date"].min() if len(press) else "",
        "first_press_post7_date": p7["article_date"].min() if len(p7) else "",
    }


def build_final_pool(project, output_dir=None):
    project = Path(project).resolve()
    output = Path(output_dir) if output_dir else project / "handoff/echo_final"
    output.mkdir(parents=True, exist_ok=True)
    raw = project / "handoff/echo_processed"
    review = load_csv(raw / "candidate_matches.csv")
    sample = load_csv(project / "handoff/echo_expansion/echo_sample_search.csv")
    status = load_csv(raw / "campaign_status.csv")
    if sample["uid_archive"].duplicated().any() or status["uid_archive"].duplicated().any():
        raise ValueError("Campaign IDs must be unique")
    if set(sample["uid_archive"]) != set(status["uid_archive"]):
        raise ValueError("Search status and sample IDs differ")
    review["relative_day"] = pd.to_numeric(review["relative_day"], errors="raise")
    if review.duplicated(["uid_archive", "article_id"]).any():
        raise ValueError("Duplicated candidate article-campaign key")

    accepted = review.loc[review["review_decision"].eq("yes") &
                          review["source_panel_scope"].eq("core") &
                          review["source_type"].isin(["press", "wire"])].copy()
    press_pool = deduplicate(accepted.loc[accepted["source_type"].eq("press")])
    wire_pool = deduplicate(accepted.loc[accepted["source_type"].eq("wire")])
    save_csv(press_pool, output / "final_pool.csv")
    save_csv(wire_pool, output / "final_wire_pool.csv")

    # Exclude unresolved campaigns from BOTH sides of the comparison.
    uncertain = set(review.loc[review["review_decision"].isin(["", "unsure"]), "uid_archive"])
    incomplete = set(status.loc[
        status["collection_issues"].ne("") |
        pd.to_numeric(status["date_window_complete"]).ne(1) |
        pd.to_numeric(status["export_count_complete"]).ne(1) |
        pd.to_numeric(status["access_restricted"]).eq(1), "uid_archive"])
    all_campaigns = sample.merge(status[["uid_archive", "collection_issues", "access_restricted"]],
                                 on="uid_archive", validate="one_to_one")
    all_campaigns["excluded_uncertain"] = all_campaigns["uid_archive"].isin(uncertain)
    all_campaigns["excluded_collection"] = all_campaigns["uid_archive"].isin(incomplete)
    all_campaigns["comparison_eligible"] = ~(all_campaigns["excluded_uncertain"] | all_campaigns["excluded_collection"])
    excluded = all_campaigns.loc[~all_campaigns["comparison_eligible"]].copy()
    save_csv(excluded, output / "excluded_campaigns.csv")

    # At campaign level use the earliest accepted publication of each press/wire copy group.
    story_pool = pd.concat([press_pool, wire_pool], ignore_index=True)
    by_campaign = {uid: frame for uid, frame in story_pool.groupby("uid_archive")}
    empty = story_pool.iloc[:0]
    measures = pd.DataFrame([
        {"uid_archive": uid, **summarize_evidence(by_campaign.get(uid, empty))}
        for uid in sample["uid_archive"]
    ])
    campaigns = all_campaigns.loc[all_campaigns["comparison_eligible"]].merge(measures, on="uid_archive", validate="one_to_one")
    campaigns["sentiment_group"] = pd.to_numeric(campaigns["sentiment"]).map(lambda x: "Negative" if x < 0 else "Positive" if x > 0 else "Neutral")

    evidence = bank_evidence(accepted)
    save_csv(evidence, output / "final_bank_evidence.csv")
    bank_base = campaigns.assign(ds_name=campaigns["banks"].str.split(";")).explode("ds_name")
    bank_base["ds_name"] = bank_base["ds_name"].map(clean_bank)
    if bank_base.duplicated(["uid_archive", "ds_name"]).any():
        raise ValueError("Duplicate campaign-bank in sample")
    by_bank = {(uid, bank): frame for (uid, bank), frame in evidence.groupby(["uid_archive", "ds_name"])}
    bank_measures = pd.DataFrame([
        {"uid_archive": uid, "ds_name": bank,
         **summarize_evidence(by_bank.get((uid, bank), evidence.iloc[:0]))}
        for uid, bank in bank_base[["uid_archive", "ds_name"]].itertuples(index=False, name=None)
    ])
    # Replace campaign-level measures with bank-specific evidence before return joins.
    bank_base = bank_base.drop(columns=[c for c in measures.columns if c != "uid_archive"])
    banks = bank_base.merge(bank_measures, on=["uid_archive", "ds_name"], validate="one_to_one")
    lookup = load_csv(project / "handoff/lookup_banks.csv")
    lookup["ds_name"] = lookup["ds_name"].map(clean_bank)
    lookup = lookup[["ds_name", "parent_country", "isin"]].drop_duplicates()
    if lookup["ds_name"].duplicated().any():
        raise ValueError("Bank lookup has ambiguous labels")
    banks = banks.merge(lookup[["ds_name", "parent_country", "isin"]], on="ds_name", how="left", validate="many_to_one")
    if banks["parent_country"].isna().any():
        raise ValueError("A target bank has no country mapping")
    country = banks.groupby("uid_archive")["parent_country"].agg(lambda x: "; ".join(sorted(set(x))))
    campaigns["target_countries"] = campaigns["uid_archive"].map(country)
    campaigns["bank_region"] = campaigns["target_countries"].map(
        lambda x: "US" if x == "US" else "US and Europe" if "US" in x.split("; ") else "Europe")
    save_csv(campaigns, output / "final_campaign_pool.csv")
    save_csv(banks, output / "final_bank_pool.csv")

    summaries = []
    for variable in ["sentiment_group", "ngo_power", "climate", "bank_region"]:
        for value, group in campaigns.groupby(variable, dropna=False):
            summaries.append(dict(variable=variable, value=value, campaigns=len(group),
                                  press_echo_post7=int(group["media_observed_post7"].sum()),
                                  news_echo_post7=int(group["news_observed_post7"].sum()),
                                  press_echo_pct=100 * group["media_observed_post7"].mean()))
    save_csv(pd.DataFrame(summaries), output / "echo_descriptives.csv")
    summary = {
        "candidate_matches": len(review), "uncertain_matches_excluded": int(review["review_decision"].eq("unsure").sum()),
        "accepted_matches": int(review["review_decision"].eq("yes").sum()),
        "accepted_outside_source_panel_excluded": int((review["review_decision"].eq("yes") & ~review.index.isin(accepted.index)).sum()),
        "press_pool_rows": len(press_pool), "press_unique_article_ids": press_pool["article_id"].nunique(),
        "wire_pool_rows": len(wire_pool), "wire_unique_article_ids": wire_pool["article_id"].nunique(),
        "copy_matches_collapsed": len(accepted) - len(press_pool) - len(wire_pool),
        "sample_campaigns": len(sample), "campaigns_with_uncertainty_excluded": len(uncertain),
        "campaigns_with_collection_issues_excluded": len(incomplete), "excluded_campaigns_total": len(excluded),
        "analysis_campaigns": len(campaigns), "analysis_campaign_bank_pairs": len(banks),
        "analysis_campaigns_press_post7": int(campaigns["media_observed_post7"].sum()),
        "analysis_campaigns_press_pre7": int(campaigns["media_observed_pre7"].sum()),
        "analysis_campaigns_news_post7": int(campaigns["news_observed_post7"].sum()),
        "analysis_bank_pairs_press_post7": int(banks["media_observed_post7"].sum()),
        "analysis_bank_pairs_news_post7": int(banks["news_observed_post7"].sum()),
        "post7_definition": "First accepted appearance of a deduplicated copy group, days 0 through 7 inclusive.",
        "zero_definition": "No echo detected by the saved fixed-panel searches; not verified absence of all media coverage.",
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    assert not press_pool.duplicated(["uid_archive", "duplicate_group_id"]).any()
    assert not wire_pool.duplicated(["uid_archive", "duplicate_group_id"]).any()
    assert set(campaigns["uid_archive"]).isdisjoint(uncertain | incomplete)
    assert len(campaigns) + len(excluded) == len(sample)
    assert press_pool["review_decision"].eq("yes").all()
    print(json.dumps(summary, indent=2))
    return {"articles": press_pool, "wires": wire_pool, "campaigns": campaigns, "banks": banks, "summary": summary}


def attach_returns(bank_pool, cars):
    """Join the notebook's Task C `cars` by exact campaign ID and target bank.

    CARs shared by several campaigns remain identified; use single_campaign_event
    for the first descriptive comparison, so one bank-day CAR is not repeated.
    """
    required = {"ds_name", "uids", "trade_date"}
    if not required <= set(cars):
        raise ValueError(f"cars must contain {sorted(required)}")
    returns = cars.copy()
    returns["ds_name"] = returns["ds_name"].map(clean_bank)
    returns["uids"] = returns["uids"].astype(str)
    returns["single_campaign_event"] = returns["uids"].map(lambda s: len({v.strip() for v in s.split(";") if v.strip()}) == 1)
    returns = returns.assign(uid_archive=returns["uids"].str.split(";")).explode("uid_archive")
    returns["uid_archive"] = returns["uid_archive"].str.strip().str.replace(r"\.0$", "", regex=True)
    keys = ["uid_archive", "ds_name"]
    if returns.duplicated(keys).any():
        raise ValueError("Several return rows match one campaign-bank; resolve the event mapping first")
    joined = bank_pool.merge(returns, on=keys, how="left", validate="one_to_one", suffixes=("", "_event"), indicator="return_match")
    return joined


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    build_final_pool(args.project, args.output_dir)
