from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://sportiq:sportiq_dev_password@localhost:5432/sportiq"
    football_data_token: str = ""
    api_football_base_url: str = "https://v3.football.api-sports.io"
    api_football_key: str = ""
    api_football_timeout: int = 20
    cors_origins: str = "http://localhost:3000"
    live_poll_seconds: int = 60
    fixture_lookahead_days: int = 7
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def normalized_database_url(self):
        url = self.database_url
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+psycopg2://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+psycopg2://", 1)
        return url

    @property
    def allowed_origins(self):
        return [x.strip() for x in self.cors_origins.split(",") if x.strip()]

settings = Settings()
