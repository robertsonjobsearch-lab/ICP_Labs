"""Apollo People API search. Never calls enrichment or reveals contact details."""

import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import SecretStr, ValidationError

from app.providers.base import (
    ProspectPage, ProspectProvider, ProspectQuery, ProviderError,
    ProviderNotConfiguredError, ProviderRateLimitedError, UnsupportedQueryError,
)
from app.schemas import MAX_EMPLOYEES, Prospect, ProviderFilterInfo

SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
MAX_RESPONSE_BYTES = 2_000_000


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the API key to a redirect destination.
        return None


class ApolloProvider(ProspectProvider):
    name = "apollo"
    display_name = "Apollo.io search previews (email and phone not revealed)"
    contact_search_fields = ("full_name",)
    filters = (ProviderFilterInfo(
        key="verified_email_only", label="Has verified email (address not revealed)", type="boolean",
    ),)

    def __init__(self, api_key: SecretStr | None, timeout: float = 15):
        key = api_key.get_secret_value().strip() if api_key else ""
        if not key or "\r" in key or "\n" in key:
            raise ProviderNotConfiguredError("Apollo API key is missing or invalid")
        self._api_key = SecretStr(key)
        self._timeout = timeout

    def validate_query(self, query: ProspectQuery) -> None:
        super().validate_query(query)
        if query.criteria.company_name:
            raise UnsupportedQueryError(
                "Apollo preview search does not support company name in this integration. "
                "Clear Company name and use job titles, locations, industry, or company size. "
                "Search saved contacts by company on Saved Leads."
            )

    def _payload(self, query: ProspectQuery) -> dict:
        c = query.criteria
        payload = {"page": query.page, "per_page": query.per_page}
        if c.job_titles:
            payload["person_titles"] = c.job_titles
            payload["include_similar_titles"] = False
        if c.locations:
            payload["person_locations"] = c.locations
        if c.industry:
            # Apollo requires its exact lowercase industry name.
            payload["organization_industries"] = [c.industry.lower()]
        if c.employee_min is not None or c.employee_max is not None:
            payload["organization_num_employees_ranges"] = [
                f"{c.employee_min or 1},{c.employee_max or MAX_EMPLOYEES}"
            ]
        if c.keywords:
            payload["q_keywords"] = " ".join(c.keywords)
        if c.full_name:
            payload["q_person_name"] = c.full_name
        if c.provider_filters.get("verified_email_only"):
            payload["contact_email_status"] = ["verified"]
        return payload

    def search(self, query: ProspectQuery) -> ProspectPage:
        self.validate_query(query)
        request = Request(
            SEARCH_URL, data=json.dumps(self._payload(query)).encode(), method="POST",
            headers={"x-api-key": self._api_key.get_secret_value(),
                     "Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with build_opener(_NoRedirect()).open(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ProviderError("Apollo response too large")
            body = json.loads(raw)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 429:
                raise ProviderRateLimitedError("Apollo rate limit reached") from None
            if status in (401, 403):
                raise ProviderNotConfiguredError("Apollo API access unavailable") from None
            if status == 422:
                raise UnsupportedQueryError(
                    "Apollo rejected these search criteria. Use an exact Apollo industry name "
                    "(for example, 'hospital & health care') or clear Industry and try again."
                ) from None
            raise ProviderError("Apollo request failed") from None
        except (OSError, URLError, ValueError) as exc:
            raise ProviderError("Apollo request failed") from None
        try:
            if not isinstance(body, dict) or not isinstance(body.get("people"), list):
                raise ValueError("Invalid response")
            people = body["people"]
            total = body.get("total_entries")
            if type(total) is not int or total < 0 or len(people) > query.per_page:
                raise ValueError("Invalid pagination")
            records = [self._prospect(person) for person in people]
        except (TypeError, ValueError, KeyError, AttributeError, ValidationError):
            raise ProviderError("Apollo returned an invalid response") from None
        return ProspectPage(
            records=records, page=query.page, per_page=query.per_page, total=total,
            has_more=query.page * query.per_page < min(total, 50_000),
        )

    @staticmethod
    def _prospect(person: dict) -> Prospect:
        if not isinstance(person, dict):
            raise ValueError("Invalid person")
        organization = person.get("organization")
        if organization is None:
            organization = {}
        if not isinstance(organization, dict):
            raise ValueError("Invalid organization")
        # has_email, has_city, has_employee_count, etc. indicate availability,
        # not values. Obfuscated surnames are not usable surnames.
        return Prospect(
            provider="apollo", provider_record_id=person["id"], is_demo=False,
            data_source="Apollo.io search preview (contact details not revealed)",
            first_name=person.get("first_name"), job_title=person.get("title"),
            company_name=organization.get("name"),
        )
