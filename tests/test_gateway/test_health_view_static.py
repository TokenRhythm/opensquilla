"""Vue route contract for the retired standalone Health destination."""

from pathlib import Path

SHARED_ROUTES = Path("opensquilla-webui/src/router/sharedRoutes.ts")
HEALTH_VIEW = Path("opensquilla-webui/src/views/HealthView.vue")


def test_health_deep_link_redirects_to_vue_overview() -> None:
    routes = SHARED_ROUTES.read_text(encoding="utf-8")

    assert "path: '/health'" in routes
    assert "redirect: '/overview'" in routes
    assert "HealthView.vue" not in routes
    assert not HEALTH_VIEW.exists()
