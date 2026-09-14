"""Central settings. Everything reads config from here, never from os.environ directly.

Note: values in .env take precedence over real environment variables. If you
rotate a secret and auth still fails, check .env before checking Codespaces
secrets.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    fabric_tenant_id: str
    fabric_client_id: str
    fabric_client_secret: str

    calibration_workspace: str = "PowerBiProjects"

    model_api_key: str = ""
    model_name: str = ""

    # Fabric REST. Versioned deliberately — v1 is the documented surface.
    fabric_api_base: str = "https://api.fabric.microsoft.com/v1"
    fabric_scope: str = "https://api.fabric.microsoft.com/.default"

    # Power BI REST. A separate audience from Fabric, with its own token.
    # Semantic model refresh history lives here, not in the Fabric API.
    powerbi_api_base: str = "https://api.powerbi.com/v1.0/myorg"
    powerbi_scope: str = "https://analysis.windows.net/powerbi/api/.default"


settings = Settings()