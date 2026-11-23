"""WishCraft API and workflow composition.

Engineering map (comments point to the implementation, not a roadmap):
1 Context engineering: app.prompts + timezone-aware DiscoveryContext.
2 LLM reasoning: OccasionDiscoveryService asks the configured model to find/rank events.
3 Structured outputs: Pydantic request/response models and OccasionValidator.
4 Orchestration: explicit discovery and wish-generation service steps below.
5 AI memory: SQLite-backed PreferencesRepository survives server restarts.
6 Guardrails: request constraints, output validation, rate limits, and concurrency bounds.
7 Human-in-the-loop: frontend selects, edits, copies, and regenerates; API keeps state editable.
8 Observability: request IDs, privacy-conscious events, and OpenTelemetry workflow spans.
9 Resilience: provider retries/fallback plus safe errors and bounded model concurrency.
10 AI evaluation: app.evaluation scores live discovery against reviewed fixtures.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from app.llm.provider import LLMError, LLMProvider, parse_json_object
from app.main_types import DiscoveryContext, WishContext
from app.observability import configure_observability, record_event, workflow_span
from app.prompts import PromptBuilder
from app.repositories.preferences import PreferencesRepository
from app.validators import Occasion, OccasionValidator, WishValidator, discovery_json_schema

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("wishcraft")
app = FastAPI(title="WishCraft API", version="1.1.0")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "*").split(","), allow_methods=["GET", "POST", "PUT"], allow_headers=["*"])
configure_observability(app)
provider = LLMProvider()
preferences_repository = PreferencesRepository()
MAX_CONCURRENT_LLM = max(1, int(os.getenv("MAX_CONCURRENT_LLM_REQUESTS", "3")))
_llm_slots = threading.BoundedSemaphore(MAX_CONCURRENT_LLM)


class DiscoveryRequest(BaseModel):
    """Validated API boundary for location and inclusive date context (concept 3)."""
    country: str = Field(min_length=2, max_length=80)
    region: str = Field(min_length=2, max_length=80)
    timezone: str = Field(min_length=1, max_length=64)
    start_date: date
    end_date: date
    cultural_preferences: list[str] = Field(default_factory=list, max_length=12)
    demo_mode: bool = False

    @field_validator("country", "region")
    @classmethod
    def clean_place(cls, value: str) -> str:
        clean = " ".join(value.split())
        if any(ord(char) < 32 for char in clean):
            raise ValueError("Place names cannot contain control characters.")
        return clean

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        value = value.strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use a valid IANA timezone, such as America/Toronto.") from exc
        return value

    @field_validator("cultural_preferences")
    @classmethod
    def bounded_preferences(cls, values: list[str]) -> list[str]:
        cleaned = [" ".join(value.split()) for value in values]
        if any(not value or len(value) > 100 for value in cleaned):
            raise ValueError("Each calendar preference must contain 1–100 characters.")
        return cleaned

    @model_validator(mode="after")
    def valid_range(self):
        if self.end_date < self.start_date or (self.end_date - self.start_date).days > 92:
            raise ValueError("Date range must be inclusive and no longer than 93 days.")
        return self


class DiscoveryResponse(BaseModel):
    country: str
    region: str
    timezone: str
    discovery_period: dict[str, date]
    occasions: list[Occasion]
    meta: dict[str, object]


class WishRequest(BaseModel):
    """Constrain free-text fields and whitelist behavioral controls (concept 6)."""
    occasion: str = Field(min_length=1, max_length=120)
    recipient_name: str = Field(default="", max_length=80)
    relationship: Literal["Friend", "Family member", "Spouse / Partner", "Colleague", "Manager", "Customer", "Teacher", "Other"] = "Friend"
    tone: Literal["Warm and heartfelt", "Funny and cheerful", "Formal and professional", "Short and simple", "Poetic and inspirational"] = "Warm and heartfelt"
    language: Literal["English", "Urdu", "Arabic", "French", "Spanish", "Hindi"] = "English"
    additional_context: str = Field(default="", max_length=500)
    demo_mode: bool = False

    @field_validator("occasion", "recipient_name", "relationship", "tone", "language", "additional_context")
    @classmethod
    def reject_control_chars(cls, value: str, info: ValidationInfo) -> str:
        if any(ord(char) < 32 and char not in "\n\t" for char in value):
            raise ValueError("Text fields cannot contain control characters.")
        clean = value.strip()
        if info.field_name == "occasion" and not clean:
            raise ValueError("Enter an occasion.")
        return clean


class WishResponse(BaseModel):
    wish: str = Field(min_length=1, max_length=WishValidator.MAX_LENGTH)
    language: str
    tone: str
    meta: dict[str, object]


class Preferences(BaseModel):
    language: Literal["English", "Urdu", "Arabic", "French", "Spanish", "Hindi"] = "English"
    tone: Literal["Warm and heartfelt", "Funny and cheerful", "Formal and professional", "Short and simple", "Poetic and inspirational"] = "Warm and heartfelt"
    country: str = Field(default="Canada", min_length=2, max_length=80)
    region: str = Field(default="Ontario", min_length=2, max_length=80)
    timezone: str = Field(default="America/Toronto", min_length=1, max_length=64)

    @field_validator("country", "region")
    @classmethod
    def clean_preference_place(cls, value: str) -> str:
        clean = " ".join(value.split())
        if len(clean) < 2:
            raise ValueError("Enter a country and region.")
        return clean

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        value = value.strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use a valid IANA timezone.") from exc
        return value


class SlidingWindowRateLimiter:
    """Per-process abuse guard for a small, unauthenticated public demo."""

    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self.limit = max(1, limit)
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._last_sweep = 0.0

    def allow(self, client_key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            # Bound memory if many one-off client addresses hit this single-process demo.
            if now - self._last_sweep >= self.window_seconds:
                for key, old_bucket in list(self._hits.items()):
                    while old_bucket and now - old_bucket[0] >= self.window_seconds:
                        old_bucket.popleft()
                    if not old_bucket:
                        del self._hits[key]
                self._last_sweep = now
            if client_key not in self._hits and len(self._hits) >= 10_000:
                return False
            bucket = self._hits[client_key]
            while bucket and now - bucket[0] >= self.window_seconds:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True


rate_limiter = SlidingWindowRateLimiter(int(os.getenv("RATE_LIMIT_PER_MINUTE", "30")))
MAX_API_REQUEST_BYTES = max(1024, int(os.getenv("MAX_API_REQUEST_BYTES", "32768")))


@app.middleware("http")
async def request_guardrails(request: Request, call_next):
    """Concepts 6/8/9: rate-limit API work and log a request ID without user payloads."""
    request_id = str(uuid.uuid4())
    started = time.perf_counter()
    path = request.url.path
    if path.startswith("/api/v1/") and path != "/api/v1/health" and request.method != "OPTIONS":
        declared_length = request.headers.get("content-length")
        if declared_length:
            try:
                if int(declared_length) > MAX_API_REQUEST_BYTES:
                    response = JSONResponse(status_code=413, content={"detail": "Request is too large."})
                    response.headers["X-Request-ID"] = request_id
                    return response
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Invalid content length."})
        # Use the socket peer only. Do not trust forwarded IP headers unless a known proxy is configured.
        client_key = request.client.host if request.client else "unknown"
        if not rate_limiter.allow(client_key):
            response = JSONResponse(status_code=429, content={"detail": "Too many requests. Please wait and try again."})
            response.headers["X-Request-ID"] = request_id
            return response
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        record_event("api.request", request_id=request_id, method=request.method, path=path,
                     status_code=response.status_code, duration_ms=round((time.perf_counter() - started) * 1000))
        return response
    except Exception as exc:
        record_event("api.request.error", request_id=request_id, method=request.method, path=path,
                     error_type=type(exc).__name__)
        raise


def call_model(workflow: str, system: str, prompt: str, *, json_mode: bool = False, json_schema: dict | None = None):
    """Bound simultaneous LLM work; overload fails quickly instead of queueing indefinitely."""
    if not _llm_slots.acquire(blocking=False):
        raise LLMError("The model is busy. Please try again shortly.")
    try:
        with workflow_span("llm.provider_call", {"workflow.name": workflow}) as span:
            content, metadata = provider.complete(system, prompt, json_mode=json_mode, json_schema=json_schema)
            if span:
                span.set_attribute("llm.provider", metadata.get("provider", "unknown"))
                span.set_attribute("llm.model", metadata.get("model", "unknown"))
                span.set_attribute("llm.duration_ms", metadata.get("duration_ms", 0))
                span.set_attribute("llm.fallback_used", metadata.get("fallback_used", False))
            return content, metadata
    finally:
        _llm_slots.release()


class OccasionDiscoveryService:
    """One inspectable workflow: context → model → schema → guardrails → ranked response."""

    @staticmethod
    def discover(req: DiscoveryRequest) -> DiscoveryResponse:
        started = time.perf_counter()
        if req.demo_mode:
            return DiscoveryResponse(country=req.country, region=req.region, timezone=req.timezone,
                discovery_period={"start_date": req.start_date, "end_date": req.end_date}, occasions=[],
                meta={"demo": True, "provider": "Demo mode", "duration_ms": 0, "validation": "passed"})
        context = DiscoveryContext(req.country, req.region, req.timezone, req.start_date, req.end_date, req.cultural_preferences)
        with workflow_span("occasion.context_engineering", {
            "context.range_days": (req.end_date - req.start_date).days + 1,
            "context.preference_count": len(req.cultural_preferences),
        }) as context_span:
            system, prompt = PromptBuilder.discovery(context)
            if context_span:
                context_span.set_attribute("context.timezone", req.timezone)
        try:
            raw, info = call_model("occasion_discovery", system, prompt, json_mode=True, json_schema=discovery_json_schema())
            with workflow_span("occasion.structured_output_validation") as output_span:
                parsed = parse_json_object(raw)
            rows = parsed.get("occasions")
            if not isinstance(rows, list):
                raise LLMError("The model response did not contain an occasions list.")
            with workflow_span("occasion.guardrails") as guardrail_span:
                validation = OccasionValidator.validate(rows, req.start_date, req.end_date)
                if guardrail_span:
                    guardrail_span.set_attribute("guardrail.candidate_count", len(rows))
                    guardrail_span.set_attribute("guardrail.accepted_count", len(validation.occasions))
                    guardrail_span.set_attribute("guardrail.rejected_count", sum(validation.rejected.values()))
            rejection_total = sum(validation.rejected.values())
            record_event("workflow.completed", workflow="occasion_discovery", provider=info["provider"], model=info["model"],
                         duration_ms=round((time.perf_counter() - started) * 1000), valid_count=len(validation.occasions),
                         rejected_count=rejection_total, rejection_reasons=validation.rejected)
            return DiscoveryResponse(country=req.country, region=req.region, timezone=req.timezone,
                discovery_period={"start_date": req.start_date, "end_date": req.end_date}, occasions=validation.occasions,
                meta={**info, "validation": "passed_with_rejections" if rejection_total else "passed",
                      "result_status": "empty_after_validation" if not validation.occasions else "results_available",
                      "rejected_count": rejection_total,
                      "rejection_reasons": validation.rejected,
                      "unverified_count": len(validation.occasions),
                      "duration_ms": round((time.perf_counter() - started) * 1000)})
        except LLMError as exc:
            record_event("workflow.failed", workflow="occasion_discovery", error_type=type(exc).__name__)
            raise HTTPException(502, detail=str(exc)) from exc


@app.post("/api/v1/occasions/discover", response_model=DiscoveryResponse)
def discover(req: DiscoveryRequest):
    return OccasionDiscoveryService.discover(req)


@app.post("/api/v1/wishes/generate", response_model=WishResponse)
def generate(req: WishRequest):
    started = time.perf_counter()
    if req.demo_mode:
        name = f" {req.recipient_name}" if req.recipient_name else ""
        wish = f"Wishing you a wonderful {req.occasion}{name}! May your day be filled with happiness and memorable moments."
        return WishResponse(wish=wish, language=req.language, tone=req.tone,
                            meta={"demo": True, "provider": "Demo mode", "duration_ms": 0})
    context = WishContext(req.occasion, req.recipient_name, req.relationship, req.tone, req.language, req.additional_context)
    with workflow_span("wish.context_engineering") as context_span:
        system, prompt = PromptBuilder.wish(context)
        if context_span:
            context_span.set_attribute("context.language", req.language)
            context_span.set_attribute("context.tone", req.tone)
    try:
        raw, info = call_model("wish_generation", system, prompt)
        with workflow_span("wish.output_guardrails") as guardrail_span:
            wish = WishValidator.validate(raw)
            if guardrail_span:
                guardrail_span.set_attribute("guardrail.output_characters", len(wish))
    except LLMError as exc:
        record_event("workflow.failed", workflow="wish_generation", error_type=type(exc).__name__)
        raise HTTPException(502, detail=str(exc)) from exc
    elapsed = round((time.perf_counter() - started) * 1000)
    record_event("workflow.completed", workflow="wish_generation", provider=info["provider"], model=info["model"],
                 duration_ms=elapsed, output_characters=len(wish), fallback_used=info.get("fallback_used", False))
    return WishResponse(wish=wish, language=req.language, tone=req.tone, meta={**info, "duration_ms": elapsed})


@app.get("/api/v1/preferences", response_model=Preferences)
def get_preferences():
    # Concept 5: this memory is persistent across restarts but intentionally local/global
    # because MVP authentication and multi-user identity are not implemented.
    return Preferences(**preferences_repository.get())


@app.put("/api/v1/preferences", response_model=Preferences)
def put_preferences(prefs: Preferences):
    saved = preferences_repository.save(prefs.model_dump())
    record_event("memory.preferences_saved", fields=list(saved.keys()))
    return Preferences(**saved)


@app.get("/api/v1/health")
def health():
    return {"status": "ok", "provider": provider.provider, "model": provider.model,
            "fallback_configured": bool(os.getenv("LLM_FALLBACK_PROVIDER"))}


static_dir = Path(os.getenv("FRONTEND_DIR", str(Path(__file__).resolve().parents[2] / "frontend")))
if static_dir.exists():
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")
