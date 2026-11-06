# finance-hackathon

Finance with Big Data hackathon project on NGO campaigns targeting banks, media coverage of those campaigns, and bank stock returns.

**Course:** 20598 – Finance with Big Data, Bocconi University  
**Instructor:** Clément Mazet-Sonilhac  
**Group:** 7 — Cecilia Lo Cicero, Sara Pulidori, Alissa Sharuda, Nico Visentin

## Project overview

The project combines Sigwatch NGO campaign records, Datastream bank return data, and a manually reviewed Factiva sample to study:

- Which banks and campaigns are represented in the data
- Whether selected campaigns receive detectable press or newswire coverage
- How bank returns behave around campaign dates
- Whether return patterns vary with campaign characteristics, geography, climate focus, or the government in power

The analysis is descriptive and exploratory. An observed return near a campaign date does not by itself show that the campaign caused the return.

## Repository contents

The main analysis is in **Hackathon.ipynb**. It is organized into four parts:

- **Part A — NGO data:** Loads and cleans Sigwatch records, matches campaign targets to priced banks, creates the name-to-ISIN lookup, describes campaigns, and prepares handoff files. It also includes the campaign echo sample, Factiva summaries, NLP sentiment checks, and source-link checks.
- **Part B — Bank return data:** Builds the daily bank return panel, prepares risk factors, estimates rolling betas, and calculates expected returns.
- **Part C — Event-study handoff:** Calculates abnormal returns and cumulative abnormal returns (CARs), including a bank-sector benchmark extension.
- **Part D — Campaign and political analyses:** Compares negative and positive campaigns, examines campaign characteristics and climate campaigns around COP21, reports robustness checks, and studies differences by government type.

Part A writes handoff files to **handoff/** for later sections. The Factiva helper scripts are run separately, as described below.

## Data and privacy

The notebook expects project data in a local **data/** directory, or in the repository root if no data directory is present. Inputs include Sigwatch Stata files, Datastream workbooks, factor and risk-free-rate data, FX data, and the name-cleaning reference used for bank matching. Some outputs and inputs are generated locally during the workflow.

The Factiva analysis uses a sample of 300 campaigns. Searches, article exports, and relevance decisions are handled separately from the notebook. Licensed source material and individual review labels must remain local.

Do not upload or commit raw datasets, licensed Factiva content, individual labeled records, search batches, or review files. Share local inputs only through authorised group access. Publish code and reviewed aggregate results only. Before committing, check notebook outputs and Markdown for individual records. A `.gitignore` rule does not remove files already tracked or copies in Git history.

## Running the Factiva echo workflow

Run these commands from the repository root with authorised local inputs. The query generator reads **handoff/echo_sample.csv**.

~~~bash
python factiva_echo_queries.py
python factiva_echo_process.py
python finalize_echo.py
~~~

Factiva searching and relevance review are manual steps between query generation and processing:

1. Run the query generator.
2. Conduct the searches in Factiva and place authorised exports in **handoff/echo_exports/**.
3. Review candidate records in **handoff/echo_processed/review.html** and save the review decisions locally.
4. Run the processing and finalisation steps.

Finalisation also requires **handoff/lookup_banks.csv**. The query and processing helpers use the Python standard library; finalisation uses pandas.

Keep these files at the repository root:

- **echo_aliases.py**
- **echo_duplicates.py**
- **factiva_echo_queries.py**
- **factiva_echo_process.py**
- **finalize_echo.py**

They are used alongside the existing dependencies **factiva_process.py**, **factiva_queries.py**, and **factiva_review.js**.

## Notebook inputs and outputs

The notebook creates or uses intermediate files under **handoff/**, including:

- **events_banks.csv** — One row per bank-day, with event flags and ISIN
- **campaigns_banks.csv** — Campaign-level records and text
- **lookup_banks.csv** — Bank-name mapping and ISIN lookup
- **events_banks_nlp.csv** and **campaigns_banks_nlp.csv** — NLP and link-check fields
- **panel_returns.csv** — Daily bank return panel
- **betas_daily.csv** — Rolling beta estimates
- **cars.csv** — Event-level CAR results used by Part D

The Factiva workflow also uses **handoff/echo_sample.csv**, **handoff/echo_exports/**, **handoff/echo_processed/**, and **handoff/echo_final/**. Exact intermediate files depend on the stage completed and the local inputs available.

## Analysis notes

- The event date is the campaign date recorded by Sigwatch. It may not be the publication date of the original campaign communication.
- News echo is based on accepted, campaign-relevant stories retrieved from selected sources during calendar days 0–7. No detected echo means no qualifying coverage was retrieved by the documented searches; it does not prove that no coverage existed.
- Stock returns are studied around the recorded campaign date using event windows and abnormal returns. CAPM results are compared with Fama–French five-factor results in the notebook.
- Campaigns may name many firms, and bank-days can include multiple campaigns. The notebook flags multi-target campaigns and applies event-window overlap rules; check the unit of observation when comparing tables.
- The Factiva sample is limited and subject to collection-related exclusions. Its results describe the eligible searched sample and should not be generalized to all campaigns or all media coverage.
- Government categories require coding choices, and the political comparisons are observational rather than causal.

## Reproducibility

Run notebook cells from a clean kernel, in order, using the intended local data and helper files. Review refreshed outputs against the narrative before sharing or committing the notebook. Saved notebook outputs may depend on locally generated caches and restricted inputs that are not included in the repository.

## Team task tracking

The notebook records the original work split as:

- **A:** Day 1, Day 2, Tasks 1 and 3
- **B:** Task 4
- **C:** Task 5
- **D:** Tasks 6 and 7
