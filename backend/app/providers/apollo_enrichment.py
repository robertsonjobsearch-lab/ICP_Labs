"""Explicit Apollo enrichment, with no waterfall or automatic paid retries."""
import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from app.providers.apollo import ApolloProvider, _NoRedirect, MAX_RESPONSE_BYTES
from app.providers.base import ProviderError
from app.schemas import Prospect


class EnrichmentDenied(ProviderError):
    """Apollo rejected a request before processing it; an explicit retry is safe."""


@dataclass
class EnrichmentResult:
    prospect: Prospect | None
    request_id: str | None = None
    retry_after: int = 10


class ApolloEnrichment:
    def __init__(self, provider: ApolloProvider):
        self.provider = provider

    def _request(self, path: str, payload: dict | None = None) -> dict:
        req = Request(
            "https://api.apollo.io/api/v1/" + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            method="POST" if payload is not None else "GET",
            headers={"x-api-key": self.provider._api_key.get_secret_value(),
                     "Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with build_opener(_NoRedirect()).open(req, timeout=15) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ProviderError("Response too large")
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ProviderError("Invalid enrichment response")
            return body
        except HTTPError as exc:
            code = exc.code
            raw = exc.read(MAX_RESPONSE_BYTES)
            exc.close()
            if code == 404 and payload is None:
                try:
                    body = json.loads(raw)
                    if body.get("error_code") == "result_pending":
                        retry = body.get("retry_after_seconds", 10)
                        return {"pending": True, "retry_after": max(10, min(int(retry), 300))}
                except (ValueError, TypeError, AttributeError):
                    pass
            if code in (400, 401, 403, 422, 429):
                raise EnrichmentDenied("Apollo rejected enrichment. Check key permissions, credits, and limits.") from None
            raise ProviderError("Apollo enrichment failed") from None
        except (OSError, URLError, ValueError):
            raise ProviderError("Apollo enrichment response unavailable") from None

    def reveal(self, person_id: str, phone: bool) -> EnrichmentResult:
        body = self._request("people/match", {
            "id": person_id, "reveal_personal_emails": False, "reveal_phone_number": phone,
            "poll_only": phone, "run_waterfall_email": False, "run_waterfall_phone": False,
        })
        person = body.get("person")
        prospect = self.prospect(person, person_id) if person else None
        phone_status = body.get("phone_enrichment") or {}
        request_id = body.get("request_id") if phone and phone_status.get("status") == "pending" else None
        if request_id is not None:
            request_id = str(request_id)
            if not re.fullmatch(r"-?\d{1,19}", request_id):
                raise ProviderError("Invalid Apollo phone request identifier")
        elif phone and phone_status.get("status") == "pending":
            raise ProviderError("Missing Apollo phone request identifier")
        return EnrichmentResult(prospect, request_id)

    def poll(self, request_id: str, person_id: str) -> EnrichmentResult:
        if not re.fullmatch(r"-?\d{1,19}", request_id):
            raise ProviderError("Invalid request identifier")
        body = self._request("webhook_result/" + request_id)
        if body.get("pending"):
            return EnrichmentResult(None, request_id, body["retry_after"])
        result = body.get("webhook_result")
        if not isinstance(result, dict) or not isinstance(result.get("people"), list):
            raise ProviderError("Invalid phone result")
        for person in result["people"]:
            if person.get("id") == person_id:
                return EnrichmentResult(self.prospect(person, person_id))
        # A completed lookup can legitimately find no phone.
        return EnrichmentResult(None)

    @staticmethod
    def prospect(person: dict, expected_id: str) -> Prospect:
        if not isinstance(person, dict) or person.get("id") != expected_id:
            raise ProviderError("Apollo returned a different person")
        org = person.get("organization") or {}
        phones = person.get("phone_numbers") or []
        phone = next((p for p in phones if p.get("sanitized_number") or p.get("raw_number")), {})
        number = phone.get("sanitized_number") or phone.get("raw_number")
        email = person.get("email")
        # Apollo can return a placeholder rather than a revealed address.
        if email and "email_not_unlocked" in email:
            email = None
        email_status = person.get("email_status")
        return Prospect(
            provider="apollo", provider_record_id=expected_id, is_demo=False,
            data_source="Apollo.io enriched contact",
            first_name=person.get("first_name"), last_name=person.get("last_name"),
            job_title=person.get("title"), email=email,
            email_status=email_status if email_status in ("verified", "unverified") else "unknown",
            phone=number, phone_status="unknown",
            company_name=org.get("name"), company_website=org.get("website_url"),
            industry=org.get("industry"), employee_count=org.get("estimated_num_employees"),
            city=person.get("city"), state=person.get("state"), country=person.get("country"),
        )
