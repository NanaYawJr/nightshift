"""Central settings. Everything reads config from here, never from os.environ directly.

The service principal credentials are optional. They are required locally,
where MSAL needs them, and absent in a Fabric notebook, which authenticates as
itself (D-029). Declaring them mandatory would make importing this module fail
in Fabric before a single collector ran.

Note: values in .env take precedence over real environment variables. If you
rotate a secret and auth still fails, check .env before checking Codespaces
secrets.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Empty in Fabric. common.auth raises a clear error if the MSAL path is
    # reached without them, rather than letting a blank credential produce an
    # opaque authentication failure.
    fabric_tenant_id: str = ""
    fabric_client_id: str = ""
    fabric_client_secret: str = ""

    calibration_workspace: str = "PowerBiProjects"

    # Workspace names carry the digit 1, not a lowercase L (D-018). Kept here
    # so there is one place to correct when they are eventually renamed.
    estate_workspace: str = "h1-finance-prod"
    nightshift_workspace: str = "h1-nightshift"

    # Where telemetry is written in Fabric. Separate from the estate so the
    # agent's observations outlive whatever it is observing, and so the estate
    # can be torn down and rebuilt without losing the record.
    telemetry_lakehouse: str = "lh_nightshift"
    estate_lakehouse: str = "lh_finance_bronze"

    # The reasoning layer. Any provider speaking the OpenAI chat-completions
    # shape works, which is all of the free tiers — so switching provider is
    # these three values and nothing else in the project.
    #
    # Copy model_name verbatim from the provider's own console. Model ids churn
    # faster than anything else here, and a stale one comes back as a 400 that
    # lists the valid ones.
    model_api_key: str = ""
    model_name: str = ""
    model_base_url: str = ""

    # Fabric REST. Versioned deliberately — v1 is the documented surface.
    fabric_api_base: str = "https://api.fabric.microsoft.com/v1"
    fabric_scope: str = "https://api.fabric.microsoft.com/.default"

    # Power BI REST. A separate audience from Fabric in the MSAL path, with its
    # own token. Semantic model refresh history lives here, not in the Fabric
    # API.
    powerbi_api_base: str = "https://api.powerbi.com/v1.0/myorg"
    powerbi_scope: str = "https://analysis.windows.net/powerbi/api/.default"


settings = Settings()