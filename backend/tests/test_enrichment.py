from datetime import UTC, datetime, timedelta
from unittest.mock import patch, MagicMock
import io
import json
from urllib.error import HTTPError

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.models import AuditEvent, SavedLead
from app.providers.apollo import ApolloProvider
from app.providers.apollo_enrichment import ApolloEnrichment, EnrichmentResult, EnrichmentDenied
from app.providers.base import ProviderError
from app.schemas import Prospect

PERSON = "587cf802f65125cad923a266"


def contact(**fields):
    return Prospect(provider="apollo", provider_record_id=PERSON, is_demo=False,
                    data_source="Apollo.io enriched contact", first_name="Jane", last_name="Doe",
                    email="jane@example.com", email_status="verified", **fields)


def activate(app):
    app.state.provider = ApolloProvider(SecretStr("secret-test"))


def reveal(client, **extra):
    return client.post("/api/enrichment", json={"person_id": PERSON, "acknowledge_credits": True, **extra})


def test_explicit_reveal_saves_and_caches_without_repeat_charge(member_client, app, db):
    activate(app)
    with patch.object(ApolloEnrichment, "reveal", return_value=EnrichmentResult(contact())) as remote:
        first = reveal(member_client)
        again = reveal(member_client)
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "ready"
    assert again.json()["job_id"] == first.json()["job_id"]
    assert remote.call_count == 1
    assert member_client.get("/api/leads").json()["items"][0]["email"] == "jane@example.com"
    events = db.scalars(select(AuditEvent).where(AuditEvent.event_type == "apollo_reveal_requested")).all()
    assert len(events) == 1 and events[0].details["credits_reserved"] == 1
    assert "jane@example.com" not in json.dumps(events[0].details)
    assert "secret-test" not in first.text


def test_phone_pending_refresh_merges_without_losing_email(member_client, app, db):
    activate(app)
    phone = Prospect(provider="apollo", provider_record_id=PERSON, is_demo=False,
                     data_source="Apollo.io enriched contact", phone="+12025550101")
    with patch.object(ApolloEnrichment, "reveal", return_value=EnrichmentResult(contact(), "123456789")):
        result = reveal(member_client, include_phone=True).json()
    assert result["state"] == "pending"
    event = db.get(AuditEvent, result["job_id"])
    event.details = {**event.details, "next_poll_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
    db.commit()
    with patch.object(ApolloEnrichment, "poll", return_value=EnrichmentResult(phone)) as remote:
        ready = member_client.post(f"/api/enrichment/{event.id}/refresh")
    assert ready.json()["state"] == "ready", ready.text
    assert ready.json()["record"]["email"] == "jane@example.com"
    assert ready.json()["record"]["phone"] == "+12025550101"
    assert remote.call_count == 1


def test_quota_refuses_before_spending(member_client, app):
    activate(app)
    app.state.settings.apollo_user_daily_credit_budget = 8
    with patch.object(ApolloEnrichment, "reveal") as remote:
        assert reveal(member_client, include_phone=True).status_code == 429
        remote.assert_not_called()


def test_global_budget_and_anonymous_access(client, member_client, app):
    activate(app)
    app.state.settings.apollo_daily_credit_budget = 0
    with patch.object(ApolloEnrichment, "reveal") as remote:
        assert reveal(member_client).status_code == 429
        remote.assert_not_called()
    client.cookies.clear()
    assert reveal(client).status_code == 401


def test_refresh_respects_poll_time_and_ownership(member_client, app):
    activate(app)
    with patch.object(ApolloEnrichment, "reveal", return_value=EnrichmentResult(contact(), "123")):
        job = reveal(member_client, include_phone=True).json()["job_id"]
    with patch.object(ApolloEnrichment, "poll") as remote:
        result = member_client.post(f"/api/enrichment/{job}/refresh")
        assert result.json()["state"] == "pending"
        remote.assert_not_called()


def test_uncertain_call_is_never_repeated(member_client, app):
    activate(app)
    with patch.object(ApolloEnrichment, "reveal", side_effect=ProviderError("secret-test")) as remote:
        first = reveal(member_client)
        again = reveal(member_client)
    assert first.json()["state"] == "uncertain"
    assert again.json()["job_id"] == first.json()["job_id"]
    assert remote.call_count == 1 and "secret-test" not in first.text


def test_denied_call_can_be_explicitly_retried_after_key_fix(member_client, app):
    activate(app)
    with patch.object(ApolloEnrichment, "reveal", side_effect=EnrichmentDenied()):
        assert reveal(member_client).json()["state"] == "denied"
    with patch.object(ApolloEnrichment, "reveal", return_value=EnrichmentResult(contact())):
        assert reveal(member_client).json()["state"] == "ready"


def test_reveal_requires_acknowledgment_and_csrf(member_client, app):
    activate(app)
    assert member_client.post("/api/enrichment", json={"person_id": PERSON}).status_code == 422
    assert member_client.post("/api/enrichment", json={"person_id": PERSON, "acknowledge_credits": False}).status_code == 422
    member_client.headers.pop("X-CSRF-Token")
    assert reveal(member_client).status_code == 403


def test_jobs_are_private(member_client, app, client, make_user):
    from tests.conftest import login
    activate(app)
    with patch.object(ApolloEnrichment, "reveal", return_value=EnrichmentResult(contact())):
        job = reveal(member_client).json()["job_id"]
    make_user(email="other@example.com")
    login(client, "other@example.com")
    assert client.post(f"/api/enrichment/{job}/refresh").status_code == 404


def test_enrichment_requests_no_personal_email_or_waterfall():
    service = ApolloEnrichment(ApolloProvider(SecretStr("secret-test")))
    body = {"person": {"id": PERSON, "first_name": "Jane", "last_name": "Doe", "email": "jane@example.com", "email_status": "verified"},
            "request_id": "123456789", "phone_enrichment": {"status": "pending"}}
    with patch.object(service, "_request", return_value=body) as remote:
        result = service.reveal(PERSON, True)
    path, payload = remote.call_args.args
    assert path == "people/match" and payload["id"] == PERSON
    assert payload["reveal_personal_emails"] is False
    assert payload["poll_only"] is True and payload["reveal_phone_number"] is True
    assert payload["run_waterfall_email"] is False and payload["run_waterfall_phone"] is False
    assert result.prospect.last_name == "Doe" and result.request_id == "123456789"


def test_poll_pending_respects_apollo_retry_delay():
    service = ApolloEnrichment(ApolloProvider(SecretStr("secret-test")))
    opener = MagicMock()
    opener.open.side_effect = HTTPError("https://api.apollo.io", 404, "", {}, io.BytesIO(b'{"error_code":"result_pending","retry_after_seconds":30}'))
    with patch("app.providers.apollo_enrichment.build_opener", return_value=opener):
        result = service.poll("123", PERSON)
    assert result.request_id == "123" and result.retry_after == 30


def test_poll_accepts_only_requested_person_and_preserves_unknown_verification():
    service = ApolloEnrichment(ApolloProvider(SecretStr("secret-test")))
    body = {"webhook_result": {"people": [{"id": PERSON, "phone_numbers": [{"sanitized_number": "+12025550101", "status_cd": "valid_number"}]}]}}
    with patch.object(service, "_request", return_value=body):
        result = service.poll("123", PERSON)
    assert result.prospect.phone == "+12025550101" and result.prospect.phone_status == "unknown"
    with pytest.raises(ProviderError):
        service.prospect({"id": "another-person"}, PERSON)
