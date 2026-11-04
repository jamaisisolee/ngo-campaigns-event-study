# finance-hackathon

## Factiva pilot: code and analysis only

The Factiva section in `Hackathon.ipynb` selects a balanced 30-campaign pilot,
prepares searches, reviews article relevance and plots confirmed coverage against
NGO power, campaign prominence and prior target-bank stock volatility.

Restricted inputs and generated records stay local. Do not commit Sigwatch/NGO
datasets, Datastream files, Factiva exports, article text, search batches,
campaign-level CSVs or the generated `review.html`. The local `handoff/` directory
is ignored, including review backups. An ignored path that is already tracked by
Git remains tracked; these rules do not remove older data from repository history.

### Running with locally authorised data

1. Run the notebook's data preparation to create `handoff/campaigns_banks.csv`,
   `handoff/events_banks.csv` and `handoff/lookup_banks.csv`, then run the pilot
   selection/export cells to create `handoff/factiva_pilot.csv`.
2. Run `python factiva_queries.py` to generate copyable queries and the batch
   manifest locally. Perform the searches in Factiva and place full-article RTF
   exports in `handoff/factiva_exports/`. The script does not access Factiva.
3. Run the notebook's Factiva processing cell, or `python factiva_process.py`.
   Open the locally generated review page and save decisions back to
   `handoff/factiva/candidate_matches.csv`. A browser draft alone is not saved
   analysis input. Rerun processing after saving the CSV.
4. Run the echo figure cell with local coverage outputs and `banks_ri.xlsm` under
   `banks_data_bocconi/` or `DATA/banks_data_bocconi/`.

Keep `factiva_queries.py`, `factiva_process.py` and `factiva_review.js` together
at the repository root. Processing imports aliases/query functions from
`factiva_queries.py`; its generated review page loads `factiva_review.js`.
The helpers use Python's standard library. The pilot/plot cells require NumPy,
pandas, Matplotlib, openpyxl and IPython; other notebook sections have their own
dependencies. A clean clone cannot reproduce licensed-data results without
separately authorised local inputs.

### Interpretation and outstanding checks

The primary `media` indicator records accepted newspaper coverage from campaign
day through day +7 (calendar days). `wire_post7` records newswire coverage in that
same window; a figure using either indicator must say it includes newswires.
Earlier newspaper coverage and newspaper coverage through day +14 are separate
outputs. Repeated editions are grouped as stories while publication dates and
article identifiers remain available locally.

Account restrictions prevented completion of all searches. Positive matches are
provisional reviewed evidence; other campaigns remain missing, not verified
zeros. Name co-occurrence is insufficient: the article must concern the same
allegation/campaign and relevant targeted bank. In particular, the Dow Jones
Keystone XL/TransCanada story previously flagged for campaign 39432 does not
mention RBS and should not establish an RBS campaign echo without further evidence.

This selectively balanced pilot supports descriptive analysis, not a population
coverage rate, a causal echo effect or a reliable test of whether NGO power predicts
coverage. Prominence refers to the bank's placement in the original NGO
communication, not prominence in the newspaper article.
