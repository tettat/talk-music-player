"""Provider contracts. QQ is the only implemented provider in this release."""
from __future__ import annotations

from typing import Protocol


class MusicProvider(Protocol):
    id: str
    name: str

    def public(self) -> dict: ...
    def run(self, operation: str, **args) -> dict: ...
    async def login(self, provider: str) -> None: ...


class ProviderRegistry:
    """Explicit capabilities: future sources must not silently impersonate QQ tracks."""
    def __init__(self, providers=()):
        self.providers = {}
        for provider in providers:
            if provider.id in self.providers:
                raise ValueError('DUPLICATE_PROVIDER')
            self.providers[provider.id] = provider

    def describe(self):
        return [{'id': p.id, 'name': p.name,
                 'capabilities': ['search', 'play', 'lyrics', 'login']}
                for p in self.providers.values()]

    def get(self, provider_id):
        from library import PlayerError
        try:
            return self.providers[provider_id]
        except KeyError:
            raise PlayerError('PROVIDER_UNAVAILABLE', 422) from None
