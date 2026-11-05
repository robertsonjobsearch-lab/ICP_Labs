import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SearchHistory
from app.providers import (
    ProspectProvider,
    ProspectQuery,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderRateLimitedError,
    build_provider,
)
from tests.conftest import login

EXAMPLE_ICP = {
    "industry": "Healthcare",
    "job_titles": ["CIO", "CTO", "IT Director"],
    "locations": ["Maryland", "Virginia", "Washington, D.C."],
    "employee_min": 100,
    "employee_max": 1000,
}

PROSPECT_FIELDS = {
    "provider", "provider_record_id", "is_demo", "data_source", "company_name", "first_name", "last_name",
    "job_title", "email", "email_status", "phone", "phone_status", "company_website", "industry",
    "employee_count", "city", "state", "country",
}


def test_provider_info(member_client: TestClient):
    info = member_client.get("/api/search/provider").json()

    assert info["name"] == "mock"
    assert info["is_demo"] is True
    assert "company_name" in info["sort_fields"]
    assert info["filters"][0]["key"] == "verified_email_only"
    assert info["contact_search_fields"] == ["full_name", "first_name", "last_name", "email", "phone"]


def test_search_returns_formatted_demo_results(member_client: TestClient):
    response = member_client.post("/api/search", json={"criteria": EXAMPLE_ICP, "per_page": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "mock" and body["is_demo"] is True
    assert body["page"] == 1 and body["per_page"] == 10
    assert body["total"] >= len(body["results"]) > 0
    assert body["total_pages"] == -(-body["total"] // 10)
    for record in body["results"]:
        assert set(record) == PROSPECT_FIELDS
        assert record["is_demo"] is True


def test_pagination_and_sorting(member_client: TestClient):
    criteria = {"employee_min": 1}
    p1 = member_client.post("/api/search", json={"criteria": criteria, "per_page": 50}).json()
    p2 = member_client.post("/api/search", json={"criteria": criteria, "per_page": 50, "page": 2}).json()
    sorted_desc = member_client.post(
        "/api/search",
        json={"criteria": criteria, "per_page": 50, "sort_by": "company_name", "sort_dir": "desc"},
    ).json()

    assert p1["has_more"] and p1["total_pages"] == min(-(-p1["total"] // 50), 20)
    assert {r["provider_record_id"] for r in p1["results"]}.isdisjoint(
        r["provider_record_id"] for r in p2["results"]
    )
    names = [r["company_name"].lower() for r in sorted_desc["results"]]
    assert names == sorted(names, reverse=True)


def test_search_history_records_criteria_not_results(member_client: TestClient, db: Session):
    first = member_client.post("/api/search", json={"criteria": EXAMPLE_ICP}).json()
    member_client.post("/api/search", json={"criteria": EXAMPLE_ICP, "page": 2})
    resorted = member_client.post(
        "/api/search", json={"criteria": EXAMPLE_ICP, "sort_by": "company_name", "record_history": False}
    ).json()

    rows = list(db.scalars(select(SearchHistory)))
    assert len(rows) == 1, "only the first page of a new search is recorded"
    assert resorted["search_id"] is None
    assert rows[0].id == first["search_id"]
    assert rows[0].criteria["industry"] == "Healthcare"
    assert rows[0].result_count == first["total"]
    assert "results" not in rows[0].criteria

    listing = member_client.get("/api/history").json()
    assert listing["total"] == 1
    assert member_client.get(f"/api/history/{first['search_id']}").json()["criteria"]["job_titles"] == [
        "CIO", "CTO", "IT Director",
    ]


def test_history_is_private_to_each_user(member_client: TestClient, app: FastAPI, make_user):
    search_id = member_client.post("/api/search", json={"criteria": EXAMPLE_ICP}).json()["search_id"]
    make_user(email="other@example.com")

    with TestClient(app) as other:
        login(other, "other@example.com")
        assert other.get("/api/history").json()["total"] == 0
        assert other.get(f"/api/history/{search_id}").status_code == 404


def test_search_is_rate_limited(member_client: TestClient, settings):
    statuses = [
        member_client.post("/api/search", json={"criteria": {"industry": "Software"}}).status_code
        for _ in range(settings.search_rate_limit_per_minute + 1)
    ]

    assert statuses[:-1] == [200] * settings.search_rate_limit_per_minute
    assert statuses[-1] == 429


class _FailingProvider(ProspectProvider):
    name = "failing"
    display_name = "Failing test provider"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def search(self, query: ProspectQuery):
        raise self.exc


@pytest.mark.parametrize(
    "exc,status",
    [
        (ProviderError("upstream 500 at https://api.example/v1?api_key=SECRET123"), 502),
        (RuntimeError("socket blew up with SECRET123"), 502),
        (ProviderRateLimitedError("429 from upstream SECRET123"), 429),
        (ProviderNotConfiguredError("missing key SECRET123"), 503),
    ],
)
def test_provider_errors_are_reported_safely(member_client: TestClient, app: FastAPI, db: Session, exc, status, caplog):
    app.state.provider = _FailingProvider(exc)

    response = member_client.post("/api/search", json={"criteria": EXAMPLE_ICP})

    assert response.status_code == status
    assert "SECRET123" not in response.text
    assert "SECRET123" not in caplog.text
    assert db.scalar(select(SearchHistory)) is None


def test_unknown_provider_fails_at_startup():
    with pytest.raises(ProviderNotConfiguredError):
        build_provider("not-a-provider")
