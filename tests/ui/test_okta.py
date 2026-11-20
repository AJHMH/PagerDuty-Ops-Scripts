"""The production identity provider: Okta OIDC (ADR 0008). The token exchange needs a live
Okta, so only the login redirect and the session read are checked here."""

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from pagerduty_ops_ui.app import create_app
from pagerduty_ops_ui.identity import OktaIdentity
from pagerduty_ops_ui.settings import OktaSettings

ISSUER = "https://dallas.okta.example/oauth2/default"
METADATA = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/v1/authorize",
    "token_endpoint": f"{ISSUER}/v1/token",
    "jwks_uri": f"{ISSUER}/v1/keys",
}


@pytest.fixture
def okta_client(settings, monkeypatch):
    settings.okta = OktaSettings(issuer=ISSUER, client_id="client-123",
                                 client_secret="okta-client-secret",
                                 redirect_uri="https://pd-ops.example/auth/callback")
    identity = OktaIdentity(settings.okta)

    async def metadata(*args, **kwargs):
        identity.oauth.okta.server_metadata.update(METADATA)
        return identity.oauth.okta.server_metadata

    monkeypatch.setattr(identity.oauth.okta, "load_server_metadata", metadata)
    app = create_app(settings, identity=identity, catalog={})
    with TestClient(app, base_url="https://pd-ops.example") as c:
        yield c


def test_login_redirects_to_okta_with_pkce_and_the_groups_scope(okta_client):
    response = okta_client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 302
    url = urlparse(response.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{ISSUER}/v1/authorize"
    query = parse_qs(url.query)
    assert query["client_id"] == ["client-123"]
    assert query["redirect_uri"] == ["https://pd-ops.example/auth/callback"]
    assert set(query["scope"][0].split()) == {"openid", "profile", "email", "groups"}
    assert query["code_challenge_method"] == ["S256"] and query["code_challenge"][0]
    assert "okta-client-secret" not in response.headers["location"]


def test_without_a_session_nobody_is_signed_in(okta_client):
    assert okta_client.get("/api/me").status_code == 401
