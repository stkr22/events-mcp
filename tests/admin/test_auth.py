"""The OIDC flow against a fake provider: PKCE, nonce and state, signature, audience and issuer."""

import base64
import hashlib
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import jwt.algorithms
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from events_mcp.admin.auth import AuthError, Oidc

ISSUER = "https://id.example"
CLIENT = "client-1"
REDIRECT = "https://admin.example/auth/callback"


class Provider:
    """Just enough of an OIDC provider, served through an httpx mock transport."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwks = {"keys": [{**jwk, "kid": "k1", "alg": "RS256", "use": "sig"}]}
        self.issued: dict[str, Any] = {}
        self.verifier_seen: str | None = None
        self.challenge: str | None = None
        self.token_status = 200
        self.claims_override: dict[str, Any] = {}
        self.userinfo: dict[str, Any] = {"name": "Ada", "email": "ada@example.org"}
        self.sign_with = self.key

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/keys",
                    "userinfo_endpoint": f"{ISSUER}/userinfo",
                },
            )
        if url.endswith("/keys"):
            return httpx.Response(200, json=self.jwks)
        if url.endswith("/userinfo"):
            return httpx.Response(200, json={"sub": "user-1", **self.userinfo})
        if url.endswith("/token"):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.verifier_seen = form["code_verifier"]
            assert form["client_id"] == CLIENT and "client_secret" not in form  # a public client
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            claims = {
                "iss": ISSUER, "aud": CLIENT, "sub": "user-1", "iat": int(time.time()), "exp": int(time.time()) + 60,
                "nonce": self.issued["nonce"], **self.claims_override,
            }  # fmt: skip
            token = jwt.encode(claims, self.sign_with, algorithm="RS256", headers={"kid": "k1"})
            return httpx.Response(200, json={"id_token": token, "access_token": "at"})
        return httpx.Response(404)


@pytest.fixture
def provider() -> Provider:
    return Provider()


@pytest.fixture
async def oidc(provider: Provider) -> Oidc:
    return Oidc(ISSUER, CLIENT, REDIRECT, httpx.AsyncClient(transport=httpx.MockTransport(provider.handler)))


async def start(oidc: Oidc, provider: Provider) -> dict[str, str]:
    url, flow = await oidc.start()
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["code_challenge_method"] == "S256" and q["client_id"] == CLIENT and q["redirect_uri"] == REDIRECT
    expected = base64.urlsafe_b64encode(hashlib.sha256(flow.verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == expected  # the verifier never leaves the server
    provider.issued = {"nonce": flow.nonce}
    return flow.as_dict()


async def test_a_good_sign_in(oidc: Oidc, provider: Provider) -> None:
    flow = await start(oidc, provider)
    who = await oidc.finish("code", flow["state"], flow)
    assert (who.sub, who.name, who.email) == ("user-1", "Ada", "ada@example.org")  # from userinfo
    assert provider.verifier_seen == flow["verifier"]


async def test_wrong_state_is_refused(oidc: Oidc, provider: Provider) -> None:
    flow = await start(oidc, provider)
    with pytest.raises(AuthError, match="expired"):
        await oidc.finish("code", "other-state", flow)
    with pytest.raises(AuthError, match="expired"):
        await oidc.finish("code", "x", {})


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"aud": "someone-else"}, "not valid"),
        ({"iss": "https://evil.example"}, "not valid"),
        ({"exp": 1}, "not valid"),
        ({"nonce": "replayed"}, "does not belong"),
    ],
)
async def test_a_forged_or_replayed_token_is_refused(
    oidc: Oidc, provider: Provider, override: dict[str, Any], message: str
) -> None:
    flow = await start(oidc, provider)
    provider.claims_override = override
    with pytest.raises(AuthError, match=message):
        await oidc.finish("code", flow["state"], flow)


async def test_a_token_signed_with_another_key_is_refused(oidc: Oidc, provider: Provider) -> None:
    flow = await start(oidc, provider)
    provider.sign_with = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AuthError, match="not valid"):
        await oidc.finish("code", flow["state"], flow)


async def test_the_provider_refusing_the_code_is_an_auth_error(oidc: Oidc, provider: Provider) -> None:
    flow = await start(oidc, provider)
    provider.token_status = 400
    with pytest.raises(AuthError, match="refused"):
        await oidc.finish("code", flow["state"], flow)


ROLE_CLAIM = "urn:zitadel:iam:org:project:roles"


async def test_roles_in_the_id_token_are_read(oidc: Oidc, provider: Provider) -> None:
    flow = await start(oidc, provider)
    provider.claims_override = {ROLE_CLAIM: {"events-admin": {"org1": "example.org"}, "viewer": {"org1": "x"}}}
    who = await oidc.finish("code", flow["state"], flow)
    assert who.roles == ["events-admin", "viewer"]


async def test_roles_fall_back_to_userinfo_and_the_project_specific_claim_counts(
    oidc: Oidc, provider: Provider
) -> None:
    flow = await start(oidc, provider)
    provider.userinfo = {"urn:zitadel:iam:org:project:123:roles": {"events-admin": {"org1": "x"}}}
    who = await oidc.finish("code", flow["state"], flow)
    assert who.roles == ["events-admin"]


async def test_no_roles_means_no_roles(oidc: Oidc, provider: Provider) -> None:
    flow = await start(oidc, provider)
    provider.userinfo = {}
    assert (await oidc.finish("code", flow["state"], flow)).roles == []


async def test_userinfo_about_someone_else_is_ignored(oidc: Oidc, provider: Provider) -> None:
    flow = await start(oidc, provider)
    provider.userinfo = {"sub": "someone-else", ROLE_CLAIM: {"events-admin": {"o": "x"}}}
    assert (await oidc.finish("code", flow["state"], flow)).roles == []


async def test_the_roles_scope_is_requested(oidc: Oidc, provider: Provider) -> None:
    url, _ = await oidc.start()
    assert ROLE_CLAIM in parse_qs(urlparse(url).query)["scope"][0].split()
