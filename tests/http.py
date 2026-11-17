"""Authenticated API client for tests exercising task and environment behavior."""

from httpx import AsyncClient

TEST_ACCESS_KEY = "test-only-access-key-0123456789abcdef"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_ACCESS_KEY}"}


class AuthenticatedClient(AsyncClient):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("headers", AUTH_HEADERS)
        super().__init__(*args, **kwargs)
