# Contact-data providers

The backend provider interface is in `backend/app/providers/base.py`. The frontend
calls `/api/search`; provider credentials never reach the browser.

## Apollo.io

Set `APOLLO_API_KEY` in the backend host's environment store, then deploy with
`CONTACT_PROVIDER=apollo`. Keys are handled as Pydantic `SecretStr`; missing keys
fail at startup rather than falling back to demonstration data. Leave the existing
database, Sites proxy secret, cookies, and sign-in configuration unchanged.

Create a private, scoped Apollo API key allowing
`api/v1/mixed_people/api_search`. No master key is required. Search uses only
`POST https://api.apollo.io/api/v1/mixed_people/api_search`.

Supported criteria:

- Job titles (strict matching), person locations, employee count range, and keywords.
- Full name through Apollo's `q_person_name` filter.
- Industry: an exact Apollo industry name, lowercased, such as
  `hospital & health care`, `financial services`, or `computer software`.
  The existing UI's demonstration suggestions are not an Apollo industry catalog.
- Has verified email: filters availability; it does not reveal the address.

Company name, separate first/last name fields, email, phone, and custom sorting
are rejected explicitly rather than silently ignored. Company-name discovery
would require a separate organization lookup; this adapter makes no such call.
Saved Leads retains local contact/company filtering for imported or saved records.

Apollo search returns previews: first name, title, company name, and Apollo person
ID. Obfuscated surnames are left empty. Availability flags are not contact details:
email, phone, full surname, city, employee count, and other absent fields stay null.
Records are labeled `Apollo.io search preview (contact details not revealed)` and
`is_demo=false`. Saving/exporting previews uses the existing application endpoints.

People API Search uses zero credits. Contact reveal is a separate explicit action
in the Sites Dashboard and Saved Leads: select 1-5 Apollo prospects, click Reveal
contact details, approve the credit notice, then submit. Available details are
automatically saved for the signed-in user. Name/work-email enrichment can use up
to 1 credit per person; the optional phone lookup can use up to 9 total per person.
Personal emails, waterfall enrichment, sequences and outreach are excluded.

The backend requires API key scopes `api/v1/people/match` and
`api/v1/webhook_result/show` alongside search. Phone delivery uses `poll_only=true`
and an authenticated, user-owned Check status action; no public webhook is exposed.
Polling respects Apollo's retry delay and does not make another paid request.

Persistent audit reservations serialize credit usage across backend workers and
cache requests by user, Apollo ID and reveal mode. Repeated clicks reuse that job.
No contact details or credentials are stored in audit events; available contact
values are stored in the user's Saved Leads. Network failures with uncertain
outcomes never automatically repeat a paid call. Rejected requests can be retried
explicitly after correcting key permissions or limits.

Conservative daily budgets reserve the maximum possible credits before submitting:
`APOLLO_USER_DAILY_CREDIT_BUDGET=25` per user and
`APOLLO_DAILY_CREDIT_BUDGET=100` across the app by default, resetting at midnight UTC.
Set either to zero to disable new enrichment. Reservations may exceed actual Apollo
usage (for example, no email/phone found), which is intentional. Apollo remains the
source of truth for billing. Sign-in must be enabled to spend credits.

Endpoints: `POST /api/enrichment` accepts `person_id`, `include_phone` and
`acknowledge_credits=true`; `POST /api/enrichment/{job_id}/refresh` checks a job
owned by the caller. Both enforce authentication and CSRF. Results update existing
saved previews without erasing previously returned fields.

The adapter has a 15-second request timeout, bounded response size, no redirects
or automatic retries, and safe errors for authentication, rate limits, validation,
network failures, and malformed responses. API error bodies and keys are not logged.

References verified November 5, 2026:

- https://docs.apollo.io/reference/people-api-search
- https://docs.apollo.io/docs/create-api-key
- https://docs.apollo.io/openapi/apollo-rest-api.json

## Demonstration provider

`CONTACT_PROVIDER=mock` returns fictional prospects with `is_demo=true`, reserved
`.example` email domains, and fictional telephone numbers. Existing saved sample
records remain marked as demo when switching the discovery provider.

## Adding another provider

Subclass `ProspectProvider`, implement `search()`, register in `build_provider()`,
and add mocked HTTP tests. Return only provider-supplied values, reject unsupported
filters, and use the existing safe provider error types. Only advertise contact
search fields and sort fields the remote API actually supports.
