"""OIDC sign-in (authorization code with PKCE, public client) and the session that follows it.

The browser never holds a token: the server exchanges the code, checks the ID token, and keeps only
who the user is in a signed cookie. Being signed in is not enough; the ``sub`` must be on the allow-list.
"""

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

JWKS_TTL_SEC = 3600
ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "PS256"]


class AuthError(Exception):
    """Sign-in failed; the message is safe to show."""


@dataclass
class Flow:
    """What the login redirect must remember until the callback."""

    state: str
    nonce: str
    verifier: str

    def as_dict(self) -> dict[str, str]:
        """Session payload."""
        return {"state": self.state, "nonce": self.nonce, "verifier": self.verifier}


@dataclass
class Identity:
    """Who signed in."""

    sub: str
    name: str | None
    email: str | None


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Oidc:
    """One OIDC provider; discovery and keys are fetched lazily and cached."""

    def __init__(self, issuer: str, client_id: str, redirect_uri: str, http: httpx.AsyncClient) -> None:
        """Remember the client; nothing is fetched until the first sign-in."""
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self._http = http
        self._meta: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None
        self._jwks_at = 0.0

    async def _discovery(self) -> dict[str, Any]:
        if self._meta is None:
            r = await self._http.get(f"{self.issuer}/.well-known/openid-configuration")
            r.raise_for_status()
            meta = r.json()
            if meta.get("issuer", "").rstrip("/") != self.issuer:
                raise AuthError("the provider's issuer does not match the configured one")
            self._meta = meta
        return self._meta

    async def _key(self, kid: str | None) -> Any:
        meta = await self._discovery()
        stale = time.monotonic() - self._jwks_at > JWKS_TTL_SEC
        for attempt in (0, 1):
            if self._jwks is None or stale or attempt:
                r = await self._http.get(meta["jwks_uri"])
                r.raise_for_status()
                self._jwks, self._jwks_at, stale = r.json(), time.monotonic(), False
            for k in self._jwks["keys"]:
                if kid is None or k.get("kid") == kid:
                    return jwt.PyJWK(k).key
        raise AuthError("the ID token is signed with an unknown key")

    async def start(self) -> tuple[str, Flow]:
        """Return the provider URL to send the browser to, and the flow to remember."""
        meta = await self._discovery()
        flow = Flow(secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48))
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": "openid profile email",
                "state": flow.state,
                "nonce": flow.nonce,
                "code_challenge": _b64(hashlib.sha256(flow.verifier.encode()).digest()),
                "code_challenge_method": "S256",
            }
        )
        return f"{meta['authorization_endpoint']}?{query}", flow

    async def finish(self, code: str, state: str, flow: dict[str, str]) -> Identity:
        """Exchange the code, verify the ID token, and return who signed in."""
        if not flow or not secrets.compare_digest(state, flow.get("state", "")):
            raise AuthError("the sign-in attempt expired; try again")
        meta = await self._discovery()
        r = await self._http.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "code_verifier": flow["verifier"],
            },
        )
        if r.status_code != httpx.codes.OK:
            raise AuthError("the provider refused the sign-in")
        tokens = r.json()
        id_token = tokens.get("id_token")
        if not id_token:
            raise AuthError("the provider returned no ID token")
        try:
            header = jwt.get_unverified_header(id_token)
            claims = jwt.decode(
                id_token,
                await self._key(header.get("kid")),
                algorithms=ALGORITHMS,
                audience=self.client_id,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(f"the ID token is not valid: {exc}") from exc
        if not secrets.compare_digest(str(claims.get("nonce", "")), flow["nonce"]):
            raise AuthError("the ID token does not belong to this sign-in")
        name, email = claims.get("name") or claims.get("preferred_username"), claims.get("email")
        if (name is None or email is None) and tokens.get("access_token") and meta.get("userinfo_endpoint"):
            info = await self._userinfo(meta["userinfo_endpoint"], tokens["access_token"])
            name, email = name or info.get("name"), email or info.get("email")
        return Identity(sub=str(claims["sub"]), name=name, email=email)

    async def _userinfo(self, url: str, access_token: str) -> dict[str, Any]:
        """Best effort: the name and email are only shown in the UI, never used for access."""
        try:
            r = await self._http.get(url, headers={"Authorization": f"Bearer {access_token}"})
            return r.json() if r.status_code == httpx.codes.OK else {}
        except httpx.HTTPError, ValueError:
            return {}
