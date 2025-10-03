# /opt/wg-vpn-bot/app/settings.py
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, AnyHttpUrl, field_validator
from typing import List, Any, Literal
import json

class Settings(BaseSettings):
    # Telegram
    bot_token: str = Field(..., alias="BOT_TOKEN")
    admin_ids: List[int] = Field(default_factory=list, alias="ADMIN_IDS")

    # Webhook (публичные URL/пути)
    webhook_base: AnyHttpUrl = Field(..., alias="WEBHOOK_BASE")
    webhook_secret: str = Field(..., alias="WEBHOOK_SECRET")
    webapp_host: str = Field("127.0.0.1", alias="WEBAPP_HOST")
    webapp_port: int = Field(8081, alias="WEBAPP_PORT")

    # WGDashboard
    wgd_api_base: str = Field(..., alias="WGD_API_BASE")
    wgd_api_token: str = Field(..., alias="WGD_API_TOKEN")
    wgd_interface: str = Field("wg0", alias="WGD_INTERFACE")
    wgd_webhook_secret: str = Field("", alias="WGD_WEBHOOK_SECRET")
    wgd_auth_scheme: Literal["auto","bearer","token","x-api-key"] = Field("auto", alias="WGD_AUTH_SCHEME")

    # Plans
    trial_days: int = Field(7, alias="TRIAL_DAYS")
    trial_device_limit: int = Field(1, alias="TRIAL_DEVICE_LIMIT")
    paid_days: int = Field(30, alias="PAID_DAYS")
    paid_device_limit: int = Field(3, alias="PAID_DEVICE_LIMIT")

    # Misc
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    rate_limit_per_min: int = Field(30, alias="RATE_LIMIT_PER_MIN")
    database_path: str = Field("./data/bot.db", alias="DATABASE_PATH")
    data_dir: str = Field("./data", alias="DATA_DIR")

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v: Any):
        if v is None or v == "":
            return []
        if isinstance(v, list):
            return [int(x) for x in v]
        if isinstance(v, int):
            return [v]
        s = str(v).strip()
        if s.startswith("[") and s.endswith("]"):
            return [int(x) for x in json.loads(s)]
        return [int(p.strip()) for p in s.split(",") if p.strip()]

    # Pydantic v2 config: читаем .env сами, игнорим лишние ключи
    model_config = SettingsConfigDict(
        env_file="/opt/wg-vpn-bot/.env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

SET = Settings()
