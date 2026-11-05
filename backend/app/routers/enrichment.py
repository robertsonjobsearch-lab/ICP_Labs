"""User-authorized enrichment with persistent reservations, caching and quotas."""
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, StringConstraints
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_db
from app.deps import get_current_user, get_settings_dep
from app.models import AuditEvent, SavedLead, User
from app.providers import get_provider, ProspectProvider
from app.providers.apollo import ApolloProvider
from app.providers.apollo_enrichment import ApolloEnrichment, EnrichmentDenied
from app.rate_limit import enforce_rate_limit
from app.schemas import Prospect, StrictModel

router = APIRouter(prefix="/api/enrichment", tags=["enrichment"])
EVENT = "apollo_reveal_requested"


class RevealRequest(StrictModel):
    person_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,40}$")]
    include_phone: bool = False
    acknowledge_credits: Literal[True]


class RevealResponse(BaseModel):
    job_id: int
    state: str
    record: Prospect | None
    message: str
    retry_after: int = 10


def _lead(db: Session, user_id: int, person_id: str) -> SavedLead | None:
    return db.scalar(select(SavedLead).where(
        SavedLead.user_id == user_id, SavedLead.provider == "apollo", SavedLead.provider_record_id == person_id,
    ))


def _save(db: Session, user_id: int, prospect: Prospect) -> None:
    values = prospect.model_dump()
    updates = {k: v for k, v in values.items() if v is not None and k not in (
        "provider", "provider_record_id", "email_status", "phone_status",
    )}
    for field in ("email", "phone"):
        if values[field]:
            updates[field + "_status"] = values[field + "_status"]
    statement = insert(SavedLead).values(user_id=user_id, **values)
    db.execute(statement.on_conflict_do_update(
        constraint="uq_saved_leads_user_record", set_=updates,
    ))


def _view(db: Session, job: AuditEvent) -> RevealResponse:
    details = job.details
    state = details["state"]
    person_id = details["person_id"]
    lead = _lead(db, job.actor_user_id, person_id)
    messages = {
        "requesting": "Reveal submitted. Check status; it will not be submitted again.",
        "pending": "Name/email saved when available. Phone lookup is processing. Check status shortly.",
        "ready": "Lookup complete. Available details are saved in Saved Leads. Apollo may not have every field.",
        "denied": "Apollo rejected the request. Check API key permissions, credits and rate limits, then try again.",
        "uncertain": "Apollo's response could not be confirmed. No paid retry was made. Check Apollo usage before contacting your administrator.",
        "failed": "Phone lookup could not be completed. Previously returned details remain saved; no new paid request was made.",
    }
    return RevealResponse(
        job_id=job.id, state=state, record=Prospect.model_validate(lead, from_attributes=True) if lead else None,
        message=messages[state], retry_after=details.get("retry_after", 10),
    )


def _service(provider: ProspectProvider) -> ApolloEnrichment:
    if not isinstance(provider, ApolloProvider):
        raise HTTPException(422, "Contact reveal is available only with Apollo.")
    return ApolloEnrichment(provider)


@router.post("", response_model=RevealResponse)
def reveal(
    body: RevealRequest, request: Request, db: Session = Depends(get_db),
    user: User = Depends(get_current_user), settings: Settings = Depends(get_settings_dep),
    provider: ProspectProvider = Depends(get_provider),
) -> RevealResponse:
    if not settings.auth_required:
        raise HTTPException(403, "Sign-in must be enabled before using Apollo credits.")
    service = _service(provider)
    enforce_rate_limit(request, f"enrich:user:{user.id}", 10)
    target = body.person_id + (":phone" if body.include_phone else ":email")
    # Serialize only the reservation, across all workers/instances, so quotas
    # and repeated clicks cannot race. Release before any external HTTP call.
    db.execute(text("SELECT pg_advisory_xact_lock(1489128701)"))
    existing = db.scalar(select(AuditEvent).where(
        AuditEvent.actor_user_id == user.id, AuditEvent.event_type == EVENT,
        AuditEvent.target_id == target,
    ).order_by(AuditEvent.id.desc()).limit(1))
    if existing and existing.details["state"] != "denied":
        result = _view(db, existing)
        db.commit()
        return result
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    quota = select(func.coalesce(func.sum(AuditEvent.details["credits_reserved"].as_integer()), 0)).where(
        AuditEvent.event_type == EVENT, AuditEvent.created_at >= start,
        AuditEvent.details["state"].as_string() != "denied",
    )
    cost = 9 if body.include_phone else 1
    global_used = db.scalar(quota)
    user_used = db.scalar(quota.where(AuditEvent.actor_user_id == user.id))
    if global_used + cost > settings.apollo_daily_credit_budget or user_used + cost > settings.apollo_user_daily_credit_budget:
        db.rollback()
        raise HTTPException(429, "The application's daily Apollo credit budget is reached. Try after midnight UTC or ask your administrator.")
    job = AuditEvent(
        event_type=EVENT, actor_user_id=user.id, target_type="apollo_person", target_id=target,
        details={"person_id": body.person_id, "include_phone": body.include_phone,
                 "credits_reserved": cost, "state": "requesting"},
    )
    db.add(job)
    db.commit()  # Durable before spending credits. Never automatically repeat a paid call.
    try:
        result = service.reveal(body.person_id, body.include_phone)
        if result.prospect:
            _save(db, user.id, result.prospect)
        job.details = {**job.details, "state": "pending" if result.request_id else "ready",
                       "request_id": result.request_id,
                       "next_poll_at": (datetime.now(UTC) + timedelta(seconds=10)).isoformat()}
        db.commit()
    except EnrichmentDenied:
        db.rollback()
        job.details = {**job.details, "state": "denied"}
        db.commit()
    except Exception:
        db.rollback()
        job.details = {**job.details, "state": "uncertain"}
        db.commit()
    return _view(db, job)


@router.post("/{job_id}/refresh", response_model=RevealResponse)
def refresh(
    job_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user),
    provider: ProspectProvider = Depends(get_provider),
) -> RevealResponse:
    service = _service(provider)
    enforce_rate_limit(request, f"enrich-poll:user:{user.id}", 30)
    job = db.scalar(select(AuditEvent).where(
        AuditEvent.id == job_id, AuditEvent.event_type == EVENT, AuditEvent.actor_user_id == user.id,
    ).with_for_update())
    if job is None:
        raise HTTPException(404, "Reveal request not found")
    if job.details["state"] == "requesting" and job.created_at < datetime.now(UTC) - timedelta(minutes=2):
        job.details = {**job.details, "state": "uncertain"}
        db.commit()
    if job.details["state"] != "pending" or datetime.fromisoformat(job.details["next_poll_at"]) > datetime.now(UTC):
        return _view(db, job)
    try:
        result = service.poll(job.details["request_id"], job.details["person_id"])
        if result.prospect:
            _save(db, user.id, result.prospect)
        job.details = {**job.details, "state": "pending" if result.request_id else "ready",
                       "retry_after": result.retry_after,
                       "next_poll_at": (datetime.now(UTC) + timedelta(seconds=result.retry_after)).isoformat()}
        db.commit()
    except EnrichmentDenied:
        # Polling doesn't spend credits. Keep the request recoverable after a
        # key-permission fix or temporary Apollo rate limit.
        db.rollback()
        return _view(db, job).model_copy(update={"message": "Phone status unavailable. Check Apollo key polling permissions and try Check status again."})
    except Exception:
        db.rollback()
        job.details = {**job.details, "state": "failed"}
        db.commit()
    return _view(db, job)
