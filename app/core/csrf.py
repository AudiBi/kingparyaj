from fastapi_csrf_protect import CsrfProtect
from pydantic import BaseModel
from app.config import settings


class CsrfSettings(BaseModel):
    secret_key: str = settings.CSRF_SECRET_KEY


@CsrfProtect.load_config
def get_csrf_config():
    return CsrfSettings()