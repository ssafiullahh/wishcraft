"""Small immutable prompt contexts keep wording separate from API controllers."""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class DiscoveryContext:
    country: str
    region: str
    timezone: str
    start_date: date
    end_date: date
    cultural_preferences: list[str]


@dataclass(frozen=True)
class WishContext:
    occasion: str
    recipient_name: str
    relationship: str
    tone: str
    language: str
    additional_context: str
