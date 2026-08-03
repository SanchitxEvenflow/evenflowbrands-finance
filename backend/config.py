import os
from typing import Optional
from pydantic_settings import BaseSettings

_env_path = os.path.join(os.path.dirname(__file__), ".env")


class Settings(BaseSettings):
    client_id: str
    client_secret: str
    refresh_token: str
    org_id: str
    gmail_user: Optional[str] = None
    gmail_app_password: Optional[str] = None
    resend_api_key: Optional[str] = None
    resend_from_email: Optional[str] = None

    unicommerce_username: Optional[str] = None
    unicommerce_password: Optional[str] = None
    unicommerce_base_url: str = "https://evenflow.unicommerce.com"
    unicommerce_client_id: str = "my-trusted-client"

    blinkit_email: str = "invoicing@evenflowbrands.com"

    slack_webhook_url: str = ""

    model_config = {"env_file": _env_path, "extra": "ignore"}


settings = Settings()
