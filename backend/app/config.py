from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # Azure Blob Storage
    azure_storage_connection_string: str = ""
    azure_storage_container: str = "copilot-files"

    # Azure Entra ID (Azure AD)
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""

    # Agent/Skill workspace directory (where .agent.md / .skill.md live)
    workspace_dir: str = "/workspace"
    agents_dir: str = ".github/agents"
    skills_dir: str = ".github/skills"

    # CORS
    cors_origins: list[str] = ["*"]

    # Copilot model (empty string = use CLI default)
    copilot_model: str = ""

    # Auth toggle (disable for local dev)
    auth_enabled: bool = False

    # Telegram Bot
    telegram_bot_token: str = ""
    telegram_allowed_users: list[str] = []  # Telegram usernames allowed (empty = allow all)
    telegram_webhook_secret: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def agents_path(self) -> str:
        return f"{self.workspace_dir}/{self.agents_dir}"

    @property
    def skills_path(self) -> str:
        return f"{self.workspace_dir}/{self.skills_dir}"


settings = Settings()
