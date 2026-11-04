/* Local review editor. CSV remains the input to the Python analysis. */
(() => {
  const payload = JSON.parse(document.getElementById("review-data").textContent);
  const key = "factiva-review-" + payload.revision;
  const status = document.getElementById("save-status");
  const editable = ["review_decision", "review_note", "banks_relevant", "reviewer"];
  let rows = payload.rows;
  try {
    const draft = JSON.parse(localStorage.getItem(key) || "null");
    if (draft && draft.length === rows.length && draft.every((r, i) =>
      r.uid_archive === rows[i].uid_archive && r.article_id === rows[i].article_id)) {
      rows.forEach((row, i) => editable.forEach(field => { row[field] = draft[i][field] || ""; }));
      status.textContent = "Browser draft restored. Save CSV to update the analysis.";
    }
  } catch (_) { /* Saving CSV also works when browser storage is unavailable. */ }

  document.querySelectorAll(".story").forEach(story => {
    const indices = story.dataset.rows.split(",").map(Number);
    story.querySelectorAll("[data-field]").forEach(control => {
      const field = control.dataset.field;
      const values = indices.map(i => rows[i][field] || "");
      const same = values.every(value => value === values[0]);
      control.value = same ? values[0] : control.tagName === "SELECT" ? "mixed" : "";
      if (!same) control.title = "Versions have different saved values; leave unchanged to preserve them.";
      control.addEventListener("input", () => {
        if (control.value === "mixed") return;
        indices.forEach(i => { rows[i][field] = control.value; rows[i].reviewer = "user"; });
        try {
          localStorage.setItem(key, JSON.stringify(rows));
          status.textContent = "Draft saved in this browser. Click Save CSV to update the analysis.";
        } catch (_) {
          status.textContent = "Changes are in this tab only. Click Save CSV before closing it.";
        }
      });
    });
  });

  const csv = () => "\ufeff" + [payload.fields, ...rows.map(row =>
    payload.fields.map(field => row[field] ?? ""))].map(values =>
      values.map(value => '"' + String(value).replaceAll('"', '""') + '"').join(",")
    ).join("\r\n") + "\r\n";

  function valid() {
    const missing = rows.find(row => row.review_decision === "yes" && !row.banks_relevant.trim());
    if (missing) {
      status.textContent = "Confirm the relevant bank names for campaign " + missing.uid_archive + " before saving.";
      return false;
    }
    return true;
  }

  function download() {
    if (!valid()) return;
    const link = document.createElement("a");
    const url = URL.createObjectURL(new Blob([csv()], {type: "text/csv;charset=utf-8"}));
    link.href = url; link.download = "candidate_matches.csv"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    status.textContent = "CSV downloaded. Replace handoff/factiva/candidate_matches.csv with this file, then rerun A4c.";
  }

  async function save() {
    if (!valid()) return;
    if (!window.showSaveFilePicker) return download();
    try {
      const handle = await window.showSaveFilePicker({
        suggestedName: "candidate_matches.csv",
        types: [{description: "Review CSV", accept: {"text/csv": [".csv"]}}]
      });
      const file = await handle.createWritable();
      await file.write(csv()); await file.close();
      status.textContent = "CSV saved. Rerun A4c, then refresh this page.";
    } catch (error) {
      status.textContent = error.name === "AbortError" ? "Save cancelled; your browser draft remains." :
        "Direct save is unavailable here. Use Download CSV and replace the project review CSV.";
    }
  }
  document.getElementById("save-csv").addEventListener("click", save);
  document.getElementById("download-csv").addEventListener("click", download);
  window.factivaReview = {csv, save, download};
})();
