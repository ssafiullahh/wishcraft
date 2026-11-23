"""Output guardrails (concept 6) for untrusted model responses."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.llm.provider import LLMError


class Occasion(BaseModel):
    """One bounded, typed candidate; model confidence is never treated as proof."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    date: date
    category: Literal[
        "National and regional holidays",
        "Religious celebrations",
        "Cultural festivals",
        "Seasonal celebrations",
        "International observances",
        "Social occasions",
        "Other",
    ]
    description: str = Field(min_length=1, max_length=240)
    relevance: Literal["high", "medium", "low"]
    date_type: Literal["fixed", "variable"]
    confidence: float = Field(ge=0, le=1)
    verification_status: Literal["unverified", "requires_verification"]

    @field_validator("category", mode="before")
    @classmethod
    def normalize_category(cls, value: Any) -> str:
        text = " ".join(str(value).strip().lower().split())
        if any(word in text for word in ("national", "regional", "holiday")):
            return "National and regional holidays"
        if any(word in text for word in ("religious", "faith", "holy")):
            return "Religious celebrations"
        if any(word in text for word in ("cultural", "festival")):
            return "Cultural festivals"
        if any(word in text for word in ("season", "solstice", "equinox")):
            return "Seasonal celebrations"
        if any(word in text for word in ("international", "observance", "awareness")):
            return "International observances"
        if any(word in text for word in ("social", "community")):
            return "Social occasions"
        if text == "other":
            return "Other"
        raise ValueError("Unrecognized occasion category.")

    @field_validator("name", "description", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Expected text.")
        # Remove control characters and normalize whitespace before rendering as text.
        normalized = unicodedata.normalize("NFKC", value)
        normalized = "".join(char for char in normalized if unicodedata.category(char) != "Cc")
        return " ".join(normalized.split()).strip()


def discovery_json_schema() -> dict[str, Any]:
    """Provide the LLM with the same strict field contract used by the validator."""
    item_schema = Occasion.model_json_schema()
    return {
        "type": "object",
        "properties": {
            "occasions": {
                "type": "array",
                "items": item_schema,
                "maxItems": 20,
            }
        },
        "required": ["occasions"],
        "additionalProperties": False,
    }


@dataclass
class OccasionValidation:
    occasions: list[Occasion] = field(default_factory=list)
    rejected: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1


class OccasionValidator:
    """Apply deterministic checks after schema parsing and before display."""

    MAX_RESULTS = 20

    @classmethod
    def validate(cls, rows: list[Any], start: date, end: date) -> OccasionValidation:
        result = OccasionValidation()
        seen: set[tuple[str, date]] = set()
        if len(rows) > cls.MAX_RESULTS:
            result.reject("over_result_limit")
        for row in rows[: cls.MAX_RESULTS]:
            try:
                item = Occasion.model_validate(row)
            except Exception:
                result.reject("invalid_schema_or_content")
                continue
            if not start <= item.date <= end:
                result.reject("outside_requested_range")
                continue
            identity = (" ".join(item.name.casefold().split()), item.date)
            if identity in seen:
                result.reject("duplicate")
                continue
            seen.add(identity)
            # No external calendar source is connected. Treat model confidence as a hint,
            # never as verification; movable or low-confidence dates always need checking.
            needs_check = item.verification_status == "requires_verification" or item.date_type == "variable" or item.confidence < 0.8
            item = item.model_copy(update={
                "verification_status": "requires_verification" if needs_check else "unverified"
            })
            result.occasions.append(item)
        result.occasions.sort(key=lambda item: (item.date, {"high": 0, "medium": 1, "low": 2}[item.relevance]))
        return result


class WishValidator:
    """Keep generated greetings plain, bounded text without leaking model markup."""

    MAX_LENGTH = 2500
    INTERNAL_MARKUP = re.compile(r"(?is)<\s*/?\s*(think|analysis|system|developer|script)\b|```|^\s*(system|developer|assistant)\s*:")
    HTML_TAG = re.compile(r"(?s)<[^>]{1,200}>")

    @classmethod
    def validate(cls, text: Any) -> str:
        if not isinstance(text, str):
            raise LLMError("The model did not return a text greeting.")
        cleaned = unicodedata.normalize("NFKC", text)
        cleaned = "".join(char for char in cleaned if char in "\n\t" or unicodedata.category(char) != "Cc").strip()
        if not cleaned:
            raise LLMError("The model returned an empty greeting.")
        if len(cleaned) > cls.MAX_LENGTH:
            raise LLMError("The model response exceeded the greeting length limit.")
        if cls.INTERNAL_MARKUP.search(cleaned) or cls.HTML_TAG.search(cleaned):
            raise LLMError("The model returned internal or executable markup instead of a greeting.")
        return cleaned
