# finance-hackathon

## Task 1
NGO campaigns, bank returns and media coverage. The current Factiva workflow
uses a 300-campaign sample and keeps licensed inputs and individual labels local.

With authorised local inputs, run from the repository root:

```bash
python factiva_echo_queries.py
python factiva_echo_process.py
python finalize_echo.py
```

The query generator reads `handoff/echo_sample.csv`. Factiva searches and relevance
review are separate steps: place exports in `handoff/echo_exports/`, save decisions
from `handoff/echo_processed/review.html`, then rerun processing and finalisation.
Finalisation also requires the local `handoff/lookup_banks.csv`. It uses pandas;
the query and processing helpers use Python's standard library.

Keep `echo_aliases.py`, `echo_duplicates.py`, `factiva_echo_queries.py`,
`factiva_echo_process.py` and `finalize_echo.py` at the repository root, alongside
the existing dependencies `factiva_process.py`, `factiva_queries.py` and
`factiva_review.js`.


Do not upload individual labeled records, raw datasets, Factiva content, search
batches or review files. Share local inputs only through authorised group access.
Publish only code and reviewed aggregate results; check notebook outputs and
Markdown for individual records before committing. `.gitignore` does not remove
already tracked data or copies in Git history.
