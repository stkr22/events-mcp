"""Admin app environment."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_CHARS = 32


class AdminSettings(BaseSettings):
    """Settings of ``events-mcp admin`` (``EVENTS_ADMIN_*``)."""

    model_config = SettingsConfigDict(env_prefix="EVENTS_ADMIN_", extra="ignore")

    database_url: SecretStr
    """Connection string of the ``events_admin`` role."""

    host: str = "0.0.0.0"  # noqa: S104 - listens inside a pod; the network policy is the boundary
    port: int = 8080

    public_base_url: str = "http://localhost:8080"
    """Where users reach the app; builds the OIDC redirect URI (``/auth/callback``) and the origin check."""

    oidc_issuer: str | None = None
    """Zitadel issuer URL. Unset turns OIDC off, which is only for local development with ``dev_auth``."""
    oidc_client_id: str | None = None
    """A public PKCE client: no secret."""

    session_secret: SecretStr = SecretStr("")
    """Signs the session cookie; at least 32 characters outside dev mode."""
    session_max_age_sec: int = 8 * 3600

    admin_role: str = "events-admin"
    """Zitadel project role that may edit. It is read from the sign-in (scope urn:zitadel:iam:org:project:roles),
    so access is managed in Zitadel and lasts as long as the session (``session_max_age_sec``)."""

    dev_auth: bool = False
    """Sign in as a fixed admin at ``/auth/dev-login``. Refused when ``oidc_issuer`` is set."""

    static_dir: str | None = None
    """Built single-page UI to serve at ``/``."""

    @property
    def secure_cookies(self) -> bool:
        """Cookies are ``Secure`` whenever the public URL is https."""
        return self.public_base_url.startswith("https://")

    def check(self) -> None:
        """Refuse an unsafe combination at startup."""
        if self.oidc_issuer and not self.oidc_client_id:
            raise ValueError("EVENTS_ADMIN_OIDC_CLIENT_ID is required with an OIDC issuer")
        if self.oidc_issuer and self.dev_auth:
            raise ValueError("dev_auth cannot be combined with an OIDC issuer")
        if not self.oidc_issuer and not self.dev_auth:
            raise ValueError("set EVENTS_ADMIN_OIDC_ISSUER (or EVENTS_ADMIN_DEV_AUTH=true for local development)")
        if not self.dev_auth and len(self.session_secret.get_secret_value()) < MIN_SECRET_CHARS:
            raise ValueError("EVENTS_ADMIN_SESSION_SECRET must be at least 32 characters")
