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
    cors_origins: list[str] = []

    # Copilot model (empty string = use CLI default)
    copilot_model: str = "claude-opus-4.8-1m"

    # Auth toggle (disable for local dev)
    auth_enabled: bool = False

    # Telegram Bot
    telegram_bot_token: str = ""
    telegram_allowed_users: list[str] = []  # Telegram usernames allowed (empty = allow all)
    telegram_webhook_secret: str = ""
    # Streaming-output verbosity for tool events shown in the chat.
    #   "verbose" — show every ⚡ tool event (legacy behavior; can trigger Telegram flood limits on long agent runs)
    #   "brief"   — show only high-signal events (task, report_intent, create, agent, edit) — default
    #   "silent"  — drop every ⚡ tool event from the chat (only the final narrative is shown)
    # Tool events are always retained in history and server logs regardless of this setting.
    telegram_tool_verbosity: str = "brief"

    # OpenAI API key (used for Whisper voice transcription)
    openai_api_key: str = ""

    # Azure Speech Services (for voice transcription)
    azure_speech_key: str = ""
    azure_speech_region: str = "eastus"

    # Azure Communication Services (for cron job email notifications)
    azure_comm_connection_string: str = ""
    email_sender_address: str = ""
    # How long (in days) blob-storage report links embedded in cron emails stay valid.
    email_link_expiry_days: int = 7

    # Cron job API secret (shared with Azure Function timer)
    cron_secret: str = ""

    # Agent run queue. Queue depth doubles as the KEDA scale signal for the
    # Container App, so a run message must outlive the request that created it.
    agent_queue_name: str = "agent-runs"
    # Visibility timeout applied on dequeue and re-applied while a run is in
    # flight. Long enough that a renewal hiccup won't redeliver mid-run.
    agent_queue_visibility_timeout: int = 900
    agent_queue_poll_interval: int = 5
    # Redeliveries tolerated before a run is abandoned as poison.
    agent_queue_max_attempts: int = 3
    # Upper bound on how long a replica is held open for non-HTTP work
    # (e.g. a Telegram agent run). A crashed replica can't pin the app past it.
    activity_lease_ttl: int = 7200
    # Run records retained in blob storage (oldest pruned first).
    max_run_records: int = 100

    # Logging
    log_level: str = "INFO"  # DEBUG, INFO, WARNING, ERROR

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def agents_path(self) -> str:
        return f"{self.workspace_dir}/{self.agents_dir}"

    @property
    def skills_path(self) -> str:
        return f"{self.workspace_dir}/{self.skills_dir}"


settings = Settings()
