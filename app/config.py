from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://sportiq:sportiq_dev_password@localhost:5432/sportiq"
    football_data_token: str = ""
    api_football_base_url: str = "https://v3.football.api-sports.io"
    api_football_key: str = ""
    api_football_timeout: int = 20
    web_backfill_enabled: bool = False
    web_backfill_competition: str = "PL"
    cors_origins: str = "http://localhost:3000"
    live_poll_seconds: int = 60
    fixture_lookahead_days: int = 7
    # Non-football sports: ESPN public scoreboards; API-SPORTS takes over a
    # sport only when API_SPORTS_KEY is set (never committed; set in Render).
    multisport_enabled: bool = True
    multisport_schedule_minutes: int = 60
    multisport_live_minutes: int = 10
    api_sports_key: str = ""
    api_sports_sports: str = ""          # comma list; empty = all supported
    api_sports_daily_budget: int = 80    # per sport API per UTC day
    api_sports_reserve: int = 10         # stop when the key has this many left
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
