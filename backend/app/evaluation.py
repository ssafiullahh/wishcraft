"""Offline scoring for dynamic discovery (concept 10: AI evaluation).

Run from ``backend`` with ``python -m app.evaluation``. The gold fixtures are
evaluation-only and are never loaded by the production discovery workflow.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from statistics import mean
from typing import Any

import httpx


FIXTURE_PATH = Path(__file__).resolve().parents[1] / "evals" / "discovery_cases.json"


def normalize_name(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_text).split())


def score_case(case: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """Score exact name+date matches, date accuracy, range compliance, and duplicates."""
    expected = {normalize_name(item["name"]): item["date"] for item in case["expected_occasions"]}
    predicted_rows = response.get("occasions", [])
    start, end = case["start_date"], case["end_date"]
    predicted = [(normalize_name(row.get("name", "")), row.get("date", "")) for row in predicted_rows]
    in_range = [(name, day) for name, day in predicted if start <= day <= end]
    exact = {(name, day) for name, day in in_range if expected.get(name) == day}
    expected_names = set(expected)
    predicted_names = {name for name, _ in in_range}
    name_matches = expected_names & predicted_names
    duplicates = len(in_range) - len(predicted_names)
    precision = len(exact) / len(predicted) if predicted else 0.0
    recall = len(exact) / len(expected) if expected else 1.0
    date_accuracy = len(exact) / len(name_matches) if name_matches else (1.0 if not expected_names & predicted_names else 0.0)
    range_compliance = len(in_range) / len(predicted) if predicted else 1.0
    return {
        "case": case["id"],
        "model": response.get("meta", {}).get("model"),
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "exact_date_accuracy": round(date_accuracy, 3),
        "range_compliance": round(range_compliance, 3),
        "duplicates": duplicates,
        "rejected_by_api": response.get("meta", {}).get("rejected_count", 0),
        "matched": [{"name": name, "date": day} for name, day in sorted(exact)],
        "missing_or_wrong_date": [
            {"name": case_item["name"], "expected_date": case_item["date"],
             "model_date": next((day for name, day in in_range if name == normalize_name(case_item["name"])), None)}
            for case_item in case["expected_occasions"]
            if (normalize_name(case_item["name"]), case_item["date"]) not in exact
        ],
        "unexpected": [
            {"name": row.get("name"), "date": row.get("date")}
            for row in predicted_rows if normalize_name(row.get("name", "")) not in expected_names
        ],
    }


def run_evaluation(base_url: str, fixture_path: Path = FIXTURE_PATH, *, include_wishes: bool = False) -> dict[str, Any]:
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))
    reports = []
    wish_samples = []
    with httpx.Client(timeout=90) as client:
        for case in cases:
            request = {key: case[key] for key in ("country", "region", "timezone", "start_date", "end_date")}
            request["cultural_preferences"] = case.get("cultural_preferences", [])
            response = client.post(f"{base_url.rstrip('/')}/api/v1/occasions/discover", json=request)
            response.raise_for_status()
            reports.append(score_case(case, response.json()))
            if include_wishes:
                occasion = case["expected_occasions"][0]["name"]
                wish_response = client.post(f"{base_url.rstrip('/')}/api/v1/wishes/generate", json={
                    "occasion": occasion, "recipient_name": "Taylor", "relationship": "Friend",
                    "tone": "Warm and heartfelt", "language": "English",
                })
                wish_response.raise_for_status()
                wish = wish_response.json()["wish"]
                wish_samples.append({
                    "case": case["id"], "occasion": occasion, "language": "English",
                    "tone": "Warm and heartfelt", "wish": wish,
                    "mechanical_checks": {
                        "non_empty": bool(wish.strip()), "within_length_limit": len(wish) <= 2500,
                        "recipient_name_present": "taylor" in wish.casefold(),
                    },
                    # Human ratings avoid pretending there is a reliable universal quality heuristic.
                    "human_review_1_to_5": {
                        "relevance": None, "factuality": None, "tone_fit": None,
                        "language_quality": None, "inclusivity": None,
                    },
                    "invented_personal_facts": None,
                })
    return {
        "suite": "wishcraft-discovery-v1",
        "cases": reports,
        "wish_samples": wish_samples,
        "macro_average": {
            key: round(mean(item[key] for item in reports), 3)
            for key in ("precision", "recall", "exact_date_accuracy", "range_compliance")
        } if reports else {},
        "review_note": "Model confidence is not evaluation evidence. Review unexpected and wrong-date items against an authoritative calendar.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Score WishCraft occasion discovery against reviewed examples.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Running WishCraft API base URL")
    parser.add_argument("--fixtures", type=Path, default=FIXTURE_PATH)
    parser.add_argument("--min-recall", type=float, default=0.0, help="Optional CI threshold from 0 to 1")
    parser.add_argument("--include-wishes", action="store_true", help="Generate wish samples with a human-review rubric")
    parser.add_argument("--output", type=Path, help="Also write the JSON report to this path")
    args = parser.parse_args()
    try:
        report = run_evaluation(args.base_url, args.fixtures, include_wishes=args.include_wishes)
    except (OSError, httpx.HTTPError, ValueError, KeyError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 2
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 1 if report["macro_average"].get("recall", 0.0) < args.min_recall else 0


if __name__ == "__main__":
    raise SystemExit(main())
