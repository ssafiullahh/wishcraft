# Discovery evaluation

Run against a live WishCraft server from `backend/`:

```powershell
python -m app.evaluation
```

Include generated greeting samples and a blank human-review rubric, and save the report:

```powershell
python -m app.evaluation --include-wishes --output eval-report.json
```

The report measures exact-name-and-date precision/recall, date accuracy, range compliance, duplicates, rejected outputs, unexpected candidates, and mechanical wish checks. Wish samples include human scoring slots for relevance, factuality, tone, language, inclusivity, and invented personal facts. The fixtures are small evaluation labels only; the production discovery service never reads them. `--min-recall 0.7` provides a simple optional CI threshold.

Automated scores cannot establish that a culturally sensitive or movable occasion is appropriate. Before accepting a model or prompt change, review the unexpected and wrong-date rows against an authoritative calendar and rate wish samples on factual accuracy, relevance, tone, language, inclusivity, and whether they invented personal facts. Record any accepted exceptions alongside fixture changes so evaluation results remain reviewable.
