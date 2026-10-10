"""Application settings loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    app_name: str = "routewise"
    app_env: str = "development"
    debug: bool = False
    log_level: str = "info"

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # PostgreSQL
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "routewise"
    postgres_user: str = "routewise"
    postgres_password: str = "changeme"
    database_url: str = ""

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_url: str = "redis://localhost:6379/0"

    # Admin Security
    admin_secret_key: str = "rw_admin_supersecret_key_change_in_production"

    # Provider: OpenRouter
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Provider: Gemini
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"

    # Provider: Groq
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"

    # Provider: Hugging Face
    hf_api_key: str = ""
    hf_base_url: str = "https://router.huggingface.co/v1"

    # Provider: Mock
    mock_provider_url: str = "http://mock-provider:8001"
    enable_mock_fallback: bool = False

    # Rate Limiting
    default_rpm_limit: int = 60
    default_daily_budget: float = 1.00

    # Semantic Cache
    cache_enabled: bool = True
    cache_similarity_threshold: float = 0.95
    cache_ttl_seconds: int = 3600
    embedding_model: str = "all-MiniLM-L6-v2"

    # Auth
    api_key_prefix: str = "rw_"
    gateway_api_key: str = "rw_dev_key"

    # Dashboard (CORS)
    dashboard_origin: str = "http://localhost:3000"

    model_config = {"env_file": (".env", "../.env"), "extra": "ignore"}

    @property
    def effective_database_url(self) -> str:
        """Construct robust database URL avoiding unresolved environment substitution."""
        if self.database_url and not self.database_url.startswith("${"):
            return self.database_url
        user, pwd = self.postgres_user, self.postgres_password
        host, port, db = self.postgres_host, self.postgres_port, self.postgres_db
        return f"postgresql+asyncpg://{user}:{pwd}@{host}:{port}/{db}"


settings = Settings()
