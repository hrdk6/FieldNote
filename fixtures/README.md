# FIXTURE DATA

Everything in this folder is **synthetic** and exists only so FieldNote can run, be tested and be
demonstrated offline without API keys or network access.

* Brands are **fictional** (e.g. "Voltra", "Zipp", "Novaphone", "Pixelo") and every URL uses the reserved
  `.example` domain. Nothing here describes a real company, product, price or event.
* `fixtures/<workspace>/manifest.json` defines two *rounds* (a baseline week and a current week) and an
  overlay that swaps the workspace's real competitor list for the fictional brands in offline mode.
* `news.json`, `pages.json`, `reddit.json`, `youtube.json` and `metrics.csv` are generated
  deterministically by `scripts/build_fixtures.py`. Each workspace deliberately includes one
  prompt-injection post and one hidden-text injection to exercise FieldNote's defences.
* `meeting_notes/` holds 12 hand-labeled meeting notes (fictional people) and `labels.json`, used by the
  action-item extraction eval.

Dashboards and briefs built from these files show a **FIXTURE DATA** banner.
