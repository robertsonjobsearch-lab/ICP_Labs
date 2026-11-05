from fastapi import Request
from pydantic import SecretStr

from app.providers.base import (
    ProspectPage,
    ProspectProvider,
    ProspectQuery,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderRateLimitedError,
    UnsupportedQueryError,
)
from app.providers.mock import MockProvider

__all__ = [
    "ProspectPage",
    "ProspectProvider",
    "ProspectQuery",
    "ProviderError",
    "ProviderNotConfiguredError",
    "ProviderRateLimitedError",
    "UnsupportedQueryError",
    "build_provider",
    "get_provider",
]


def build_provider(name: str, *, apollo_api_key: SecretStr | None = None) -> ProspectProvider:
    if name == "mock":
        return MockProvider()
    if name == "apollo":
        from app.providers.apollo import ApolloProvider
        return ApolloProvider(apollo_api_key)
    raise ProviderNotConfiguredError(f"Unknown contact provider '{name}'")


def get_provider(request: Request) -> ProspectProvider:
    return request.app.state.provider
