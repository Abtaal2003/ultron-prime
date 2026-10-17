"""Typed, validated runtime configuration (spec section 14).

All values come from the environment or a ``.env`` file. Secrets are ``SecretStr`` so they are
never rendered by ``repr`` or serialised by accident. Relative paths resolve against the project
root (``ULTRON_HOME`` overrides it), so the server behaves the same from any working directory.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(os.environ.get("ULTRON_HOME", Path(__file__).resolve().parent.parent)).resolve()

Effort = Literal["low", "medium", "high", "xhigh", "max"]
AccessMode = Literal["local", "tailnet", "lan"]

KNOWN_PROVIDERS = frozenset({"anthropic", "groq", "gemini", "fake"})


def _csv(value: Any) -> Any:
    """Parse a comma separated string into a list of trimmed, non-empty items."""
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


CsvList = Annotated[list[str], NoDecode]


class RouteSpec(str):
    """A ``provider:model`` routing string with helpers to split it."""

    @property
    def provider(self) -> str:
        """Return the provider part of the route."""
        return self.split(":", 1)[0]

    @property
    def model(self) -> str:
        """Return the model part of the route."""
        return self.split(":", 1)[1]


def parse_route(value: str) -> RouteSpec:
    """Validate and wrap a ``provider:model`` string.

    Raises:
        ValueError: if the string is not ``provider:model`` with a known provider.
    """
    if ":" not in value:
        raise ValueError(f"route '{value}' must look like provider:model")
    provider, model = value.split(":", 1)
    if provider not in KNOWN_PROVIDERS:
        raise ValueError(f"route '{value}' names unknown provider '{provider}'")
    if not model.strip():
        raise ValueError(f"route '{value}' has an empty model id")
    return RouteSpec(value)


class Settings(BaseSettings):
    """Every configuration key from the specification, validated at startup."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- Server and access (6.5) -------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = Field(8787, ge=1, le=65535)
    access_mode: AccessMode = "local"
    allowed_hosts: CsvList = ["localhost", "127.0.0.1"]
    allowed_origins: CsvList = ["http://localhost:8787"]
    session_idle_hours: float = Field(12, gt=0)
    session_max_days: float = Field(14, gt=0)
    login_max_attempts: int = Field(5, ge=1)
    tls_cert_file: str = ""
    tls_key_file: str = ""
    remote_confirm_tools: CsvList = ["isolated_python_sandbox", "network_ping_diagnostic"]
    sse_heartbeat_s: float = Field(15, gt=0)
    turn_event_retention_hours: float = Field(24, gt=0)
    max_request_mb: int = Field(60, ge=1)

    # --- Provider keys ------------------------------------------------------------------------
    anthropic_api_key: SecretStr = SecretStr("")
    groq_api_key: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")

    # --- Role routing -------------------------------------------------------------------------
    route_main: str = "anthropic:claude-sonnet-5-5"
    route_guard: str = "groq:openai/gpt-oss-20b"
    route_memory_judge: str = "anthropic:claude-sonnet-5-5"
    route_summarizer: str = "groq:openai/gpt-oss-20b"
    route_persona_eval_judge: str = "gemini:gemini-3.5-flash"
    route_quarantine_reader: str = "groq:openai/gpt-oss-20b"
    route_quarantine_reader_vision: str = "gemini:gemini-3.5-flash"
    route_vision_fallback: str = "gemini:gemini-3.5-flash"
    route_planner: str = "anthropic:claude-sonnet-5-5"
    route_redteam_attacker: str = "gemini:gemini-3.5-flash"
    route_redteam_judge: str = "groq:openai/gpt-oss-120b"
    fallback_chain: CsvList = [
        "anthropic:claude-sonnet-5-5",
        "gemini:gemini-3.5-flash",
        "groq:openai/gpt-oss-120b",
    ]
    llm_request_timeout_s: float = Field(60, gt=0)
    llm_max_retries: int = Field(2, ge=0, le=6)
    llm_backoff_base_s: float = Field(0.8, gt=0)

    # --- Sampling policy (7.5) ----------------------------------------------------------------
    effort_tech: Effort = "high"
    effort_review: Effort = "high"
    effort_conversation: Effort = "medium"
    effort_refusal: Effort = "low"
    effort_judges: Effort = "low"
    temperature_tools: float = Field(0.2, ge=0, le=2)
    temperature_narration: float = Field(0.7, ge=0, le=2)

    # --- Identity and safety ------------------------------------------------------------------
    scope_strictness: Literal["strict", "balanced", "open"] = "balanced"
    stream_mode: Literal["guarded", "raw"] = "guarded"
    guard_fail_posture: Literal["strict"] = "strict"
    output_guard_llm_mode: Literal["flagged", "always", "off"] = "flagged"
    egress_policy: Literal["confirm_tainted", "deny_tainted", "off"] = "confirm_tainted"
    egress_max_holds_per_turn: int = Field(2, ge=0)
    ping_max_hostname_len: int = Field(64, ge=8, le=253)
    quarantine_mode: Literal["standard", "strict", "off"] = "standard"
    quarantine_sources: CsvList = ["web", "uploads_external"]
    quarantine_max_summary_chars: int = Field(1500, ge=100)
    camel_mode: Literal["off", "egress_turns", "always"] = "off"
    camel_strict_control_flow: bool = True
    camel_max_statements: int = Field(60, ge=1)
    camel_max_iterations: int = Field(200, ge=1)
    repeat_detection: bool = True
    repeat_cross_chat: bool = True
    repeat_cross_chat_window_days: int = Field(30, ge=1)
    probe_cooldown_threshold: int = Field(3, ge=2)

    # --- Persona ------------------------------------------------------------------------------
    persona_canon_through: str = "S1E02"
    persona_profanity: Literal["sparing", "off"] = "sparing"
    persona_quote_callbacks: bool = True
    persona_era_weight_visionquest: float = Field(0.7, ge=0, le=1)
    style_anchors_k: int = Field(3, ge=0, le=6)
    quote_ngram_limit: int = Field(8, ge=3)
    charter_path: str = "./src/core/persona/charter.md"

    # --- Embeddings ---------------------------------------------------------------------------
    embedding_backend: Literal["local", "local_light", "local_alt", "gemini", "hashing"] = "local"
    local_embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    gemini_embedding_model: str = "gemini-embedding-2"

    # --- Storage ------------------------------------------------------------------------------
    sqlite_path: str = "./data/ultron.db"
    checkpoint_path: str = "./data/checkpoints.db"
    chroma_path: str = "./data/chroma"

    # --- Memory and history -------------------------------------------------------------------
    stm_token_budget: int = Field(6000, ge=500)
    history_top_k: int = Field(3, ge=0)
    memory_top_k: int = Field(4, ge=0)
    passive_cross_chat_recall: bool = True
    max_proposals_per_turn: int = Field(2, ge=0)
    memory_eval_every_n_turns: int = Field(3, ge=1)

    # --- Tools --------------------------------------------------------------------------------
    workspace_root: str = "./workspace"
    tavily_api_key: SecretStr = SecretStr("")
    sandbox_backend: Literal["subprocess"] = "subprocess"
    sandbox_timeout_s: int = Field(10, ge=1, le=30)
    sandbox_memory_mb: int = Field(256, ge=32)
    sandbox_max_output_files: int = Field(10, ge=0)
    sandbox_max_output_mb: int = Field(20, ge=1)
    allow_private_ranges: bool = True
    enabled_tools: CsvList = ["all"]

    # --- Files --------------------------------------------------------------------------------
    attachments_path: str = "./data/attachments"
    upload_max_mb: int = Field(20, ge=1)
    upload_max_mb_per_message: int = Field(50, ge=1)
    upload_max_files_per_message: int = Field(5, ge=1)
    upload_max_pdf_pages: int = Field(300, ge=1)
    upload_zip_max_ratio: int = Field(100, ge=2)
    upload_zip_max_uncompressed_mb: int = Field(200, ge=1)
    image_max_pixels: int = Field(50_000_000, ge=1_000)
    image_strip_metadata: bool = True
    provider_file_api: bool = False
    forge_max_output_mb: int = Field(20, ge=1)
    forge_max_rows: int = Field(100_000, ge=1)

    # --- Red team (nightly workflow only) -----------------------------------------------------
    redteam_nightly_budget_usd: float = Field(2.0, ge=0)
    redteam_attempts_per_class: int = Field(20, ge=1)
    redteam_max_refinement_rounds: int = Field(3, ge=0)
    redteam_usd_per_mtok_in: float = Field(3.0, ge=0)
    redteam_usd_per_mtok_out: float = Field(15.0, ge=0)

    # --- Loop guards (12.2) -------------------------------------------------------------------
    max_tool_iterations: int = Field(8, ge=1)
    max_tool_time_s: float = Field(90, gt=0)
    max_concurrent_tools: int = Field(3, ge=1)
    max_quarantined_reads_per_turn: int = Field(10, ge=0)
    max_files_written_per_turn: int = Field(10, ge=0)
    guard_timeout_s: float = Field(8, gt=0)
    llm_decide_timeout_s: float = Field(60, gt=0)

    # --- Observability ------------------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "pretty"] = "pretty"
    otel_enabled: bool = False
    otel_capture_content: bool = False

    # ----------------------------------------------------------------------------------------
    @field_validator(
        "allowed_hosts",
        "allowed_origins",
        "remote_confirm_tools",
        "fallback_chain",
        "quarantine_sources",
        "enabled_tools",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        return _csv(value)

    @field_validator(
        "route_main",
        "route_guard",
        "route_memory_judge",
        "route_summarizer",
        "route_persona_eval_judge",
        "route_quarantine_reader",
        "route_quarantine_reader_vision",
        "route_vision_fallback",
        "route_planner",
        "route_redteam_attacker",
        "route_redteam_judge",
    )
    @classmethod
    def _check_route(cls, value: str) -> str:
        return str(parse_route(value))

    @field_validator("fallback_chain")
    @classmethod
    def _check_chain(cls, value: list[str]) -> list[str]:
        return [str(parse_route(item)) for item in value]

    @model_validator(mode="after")
    def _cross_checks(self) -> Settings:
        if self.access_mode == "lan" and not (self.tls_cert_file and self.tls_key_file):
            raise ValueError("ACCESS_MODE=lan requires TLS_CERT_FILE and TLS_KEY_FILE")
        if self.upload_max_mb_per_message < self.upload_max_mb:
            raise ValueError("UPLOAD_MAX_MB_PER_MESSAGE must be at least UPLOAD_MAX_MB")
        if self.session_idle_hours > self.session_max_days * 24:
            raise ValueError("SESSION_IDLE_HOURS cannot exceed SESSION_MAX_DAYS")
        return self

    # ----------------------------------------------------------------------------------------
    def resolve(self, value: str) -> Path:
        """Resolve a possibly relative path against the project root."""
        path = Path(value)
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()

    @property
    def sqlite_file(self) -> Path:
        """Absolute path of the main database."""
        return self.resolve(self.sqlite_path)

    @property
    def checkpoint_file(self) -> Path:
        """Absolute path of the LangGraph checkpoint database."""
        return self.resolve(self.checkpoint_path)

    @property
    def chroma_dir(self) -> Path:
        """Absolute path of the Chroma persistent directory."""
        return self.resolve(self.chroma_path)

    @property
    def workspace_dir(self) -> Path:
        """Absolute path of the operator workspace root."""
        return self.resolve(self.workspace_root)

    @property
    def attachments_dir(self) -> Path:
        """Absolute path of the immutable attachment store."""
        return self.resolve(self.attachments_path)

    @property
    def data_dir(self) -> Path:
        """Directory holding databases, auth state, and calibration files."""
        return self.sqlite_file.parent

    @property
    def charter_file(self) -> Path:
        """Absolute path of the identity charter."""
        return self.resolve(self.charter_path)

    def tool_enabled(self, name: str) -> bool:
        """Return whether a tool is enabled by ``ENABLED_TOOLS``."""
        return "all" in self.enabled_tools or name in self.enabled_tools

    def role_routes(self) -> dict[str, str]:
        """Return every configured role as ``role -> provider:model``."""
        return {
            "main": self.route_main,
            "guard": self.route_guard,
            "memory_judge": self.route_memory_judge,
            "summarizer": self.route_summarizer,
            "persona_eval_judge": self.route_persona_eval_judge,
            "quarantine_reader": self.route_quarantine_reader,
            "quarantine_reader_vision": self.route_quarantine_reader_vision,
            "vision_fallback": self.route_vision_fallback,
            "planner": self.route_planner,
            "redteam_attacker": self.route_redteam_attacker,
            "redteam_judge": self.route_redteam_judge,
        }

    def effort_for(self, task_type: str) -> Effort:
        """Map a guard ``task_type`` onto the configured Claude effort level (3.12.6)."""
        return {
            "TECH": self.effort_tech,
            "REVIEW": self.effort_review,
            "CONVERSATION": self.effort_conversation,
            "REFUSAL": self.effort_refusal,
        }.get(task_type, self.effort_conversation)

    def key_presence(self) -> dict[str, bool]:
        """Report which provider keys exist, never their values."""
        return {
            "anthropic": bool(self.anthropic_api_key.get_secret_value()),
            "groq": bool(self.groq_api_key.get_secret_value()),
            "gemini": bool(self.gemini_api_key.get_secret_value()),
            "tavily": bool(self.tavily_api_key.get_secret_value()),
        }

    def secret_values(self) -> list[str]:
        """Return every non-empty secret, for redaction filters only."""
        values = [
            self.anthropic_api_key.get_secret_value(),
            self.groq_api_key.get_secret_value(),
            self.gemini_api_key.get_secret_value(),
            self.tavily_api_key.get_secret_value(),
        ]
        return [v for v in values if v]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
