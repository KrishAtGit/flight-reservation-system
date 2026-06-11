from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str
    SYNC_DATABASE_URL: str
    SEAT_HOLD_MINUTES: int = 10
    SECRET_KEY: str = "changeme"

    class Config:
        env_file = ".env"


settings = Settings()