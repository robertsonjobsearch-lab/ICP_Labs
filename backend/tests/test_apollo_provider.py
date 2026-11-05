import io
import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.providers import build_provider
from app.providers.apollo import ApolloProvider, SEARCH_URL, _NoRedirect
from app.providers.base import (
    ProspectQuery, ProviderError, ProviderNotConfiguredError,
    ProviderRateLimitedError, UnsupportedQueryError,
)
from app.schemas import SearchCriteria


def query(**criteria):
    return ProspectQuery(criteria=SearchCriteria(**(criteria or {"job_titles": ["CTO"]})), page=1, per_page=10)


def provider():
    return ApolloProvider(SecretStr("test-secret"))


def response(body):
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(body).encode()
    return opener


def test_factory_requires_key_and_never_exposes_it():
    for key in (None, SecretStr(""), SecretStr("  "), SecretStr("bad\nkey")):
        with pytest.raises(ProviderNotConfiguredError):
            build_provider("apollo", apollo_api_key=key)
    settings = Settings(database_url="postgresql://unused/test", apollo_api_key="test-secret")
    assert "test-secret" not in repr(settings)
    p = build_provider("apollo", apollo_api_key=settings.apollo_api_key)
    info = p.info().model_dump()
    assert info["name"] == "apollo" and info["is_demo"] is False
    assert info["contact_search_fields"] == ["full_name"]
    assert "test-secret" not in json.dumps(info)


def test_maps_supported_filters_and_uses_only_search_endpoint():
    q = query(industry="Hospital & Health Care", job_titles=["CTO"], locations=["Maryland"],
              employee_min=50, employee_max=500, keywords=["telehealth"], full_name="Jane Doe",
              provider_filters={"verified_email_only": True})
    opener = response({"total_entries": 0, "people": []})
    with patch("app.providers.apollo.build_opener", return_value=opener):
        result = provider().search(q)
    req = opener.open.call_args.args[0]
    assert req.full_url == SEARCH_URL and req.method == "POST"
    assert req.get_header("X-api-key") == "test-secret"
    assert opener.open.call_args.kwargs["timeout"] == 15
    assert json.loads(req.data) == {
        "page": 1, "per_page": 10, "organization_industries": ["hospital & health care"],
        "person_titles": ["CTO"], "include_similar_titles": False, "person_locations": ["Maryland"],
        "organization_num_employees_ranges": ["50,500"], "q_keywords": "telehealth",
        "q_person_name": "Jane Doe", "contact_email_status": ["verified"],
    }
    assert result.total == 0 and not result.has_more


@pytest.mark.parametrize("criteria", [
    {"company_name": "Example"}, {"first_name": "Jane"}, {"last_name": "Doe"},
    {"email": "jane@example.com"}, {"phone": "2025550101"},
    {"job_titles": ["CTO"], "provider_filters": {"unknown": True}},
    {"job_titles": ["CTO"], "provider_filters": {"verified_email_only": "yes"}},
])
def test_never_silently_drops_unsupported_filters(criteria):
    with patch("app.providers.apollo.build_opener") as opener:
        with pytest.raises(UnsupportedQueryError):
            provider().search(query(**criteria))
        opener.assert_not_called()


def test_sort_is_rejected():
    q = ProspectQuery(SearchCriteria(job_titles=["CTO"]), 1, 10, "company_name")
    with pytest.raises(UnsupportedQueryError):
        provider().search(q)


def test_preview_does_not_invent_contact_details_or_copy_obfuscated_name():
    body = {"total_entries": 30, "people": [{
        "id": "apollo-123", "first_name": "Jane", "last_name_obfuscated": "D**",
        "title": "CTO", "has_email": True, "has_city": True, "has_direct_phone": "Yes",
        "organization": {"name": "Example", "has_employee_count": True, "has_industry": True},
    }]}
    with patch("app.providers.apollo.build_opener", return_value=response(body)):
        page = provider().search(query())
    p = page.records[0]
    assert page.has_more and page.total == 30
    assert p.provider_record_id == "apollo-123" and not p.is_demo
    assert p.first_name == "Jane" and p.company_name == "Example" and p.job_title == "CTO"
    assert p.last_name is None and p.city is None and p.employee_count is None and p.industry is None
    assert p.email is None and p.email_status == "unavailable"
    assert p.phone is None and p.phone_status == "unavailable"


@pytest.mark.parametrize("code,error", [
    (401, ProviderNotConfiguredError), (403, ProviderNotConfiguredError),
    (429, ProviderRateLimitedError), (422, UnsupportedQueryError), (500, ProviderError), (302, ProviderError),
])
def test_http_errors_are_safe(code, error):
    opener = MagicMock()
    opener.open.side_effect = HTTPError(SEARCH_URL, code, "test-secret", {}, io.BytesIO(b"test-secret"))
    with patch("app.providers.apollo.build_opener", return_value=opener):
        with pytest.raises(error) as caught:
            provider().search(query())
    assert "test-secret" not in str(caught.value)


@pytest.mark.parametrize("exc", [URLError("test-secret"), TimeoutError("test-secret")])
def test_network_errors_are_safe(exc):
    opener = MagicMock()
    opener.open.side_effect = exc
    with patch("app.providers.apollo.build_opener", return_value=opener):
        with pytest.raises(ProviderError) as caught:
            provider().search(query())
    assert "test-secret" not in str(caught.value)


@pytest.mark.parametrize("body", [[], {}, {"people": None}, {"total_entries": 0, "people": [{}]},
    {"total_entries": "10", "people": []}, {"total_entries": -1, "people": []},
    {"total_entries": 1, "people": [None]},
    {"total_entries": 1, "people": [{"id": "123", "organization": []}]},
])
def test_malformed_response_fails_safely(body):
    with patch("app.providers.apollo.build_opener", return_value=response(body)):
        with pytest.raises(ProviderError):
            provider().search(query())


def test_response_size_limit():
    opener = response({})
    opener.open.return_value.__enter__.return_value.read.return_value = b"x" * 2_000_001
    with patch("app.providers.apollo.build_opener", return_value=opener):
        with pytest.raises(ProviderError):
            provider().search(query())


def test_redirects_cannot_forward_api_key():
    assert _NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com") is None


def test_search_save_and_export_preview(member_client, app):
    app.state.provider = provider()
    info = member_client.get("/api/search/provider").json()
    assert info["name"] == "apollo" and info["is_demo"] is False
    body = {"total_entries": 1, "people": [{
        "id": "apollo-integration-123", "first_name": "Jane", "title": "CTO",
        "organization": {"name": "Example"},
    }]}
    with patch("app.providers.apollo.build_opener", return_value=response(body)):
        result = member_client.post("/api/search", json={"criteria": {"job_titles": ["CTO"]}})
    assert result.status_code == 200, result.text
    record = result.json()["results"][0]
    assert result.json()["provider"] == "apollo" and not record["is_demo"]
    saved = member_client.post("/api/leads", json={"leads": [record]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["saved"] == 1
    again = member_client.post("/api/leads", json={"leads": [record]})
    assert again.json()["already_saved"] == 1
    exported = member_client.post("/api/export/csv", json={"records": [record]})
    assert exported.status_code == 200 and "Jane" in exported.text
    assert "test-secret" not in exported.text
    history = member_client.get("/api/history").json()
    assert history["items"][0]["provider"] == "apollo"
    assert "test-secret" not in result.text
