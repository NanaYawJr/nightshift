"""Central settings. Everything reads config from here, never from os.environ directly."""

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

    # Fabric REST base. Versioned deliberately — v1 is the documented surface.
    fabric_api_base: str = "https://api.fabric.microsoft.com/v1"
    fabric_scope: str = "https://api.fabric.microsoft.com/.default"


settings = Settings()
