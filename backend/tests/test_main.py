"""Composition-root integration checks.

The track suites build their routers independently. This test proves that their real factory
signatures are wired together by ``main.py`` without touching Supabase, Vapi, or a provider.
"""

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_production_refuses_to_start_without_readiness_gates() -> None:
    import pytest

    with pytest.raises(RuntimeError, match="PRODUCTION_RETENTION_READY"):
        create_app(Settings(environment="production"))


def test_create_app_mounts_every_integrated_surface() -> None:
    app = create_app(Settings(supabase_url="", supabase_secret_key=""))

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/vapi/events").status_code == 401
        assert client.post("/vapi/tools").status_code == 401
        # The portal route is mounted; an unconfigured store must degrade to 503 rather than
        # attempting a network connection while the integration test is running.
        assert client.get("/api/orders").status_code == 503
