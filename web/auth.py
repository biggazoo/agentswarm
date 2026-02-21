import os
from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

SECRET_KEY = os.environ.get("DASHBOARD_SECRET_KEY", "changeme_random_string")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "garrison123")
serializer = URLSafeTimedSerializer(SECRET_KEY)
SESSION_COOKIE = "agentswarm_session"


def make_session_token() -> str:
    return serializer.dumps({"auth": True})


def verify_session_token(token: str) -> bool:
    try:
        data = serializer.loads(token, max_age=86400)  # 24 hour session
        return bool(data.get("auth"))
    except (BadSignature, SignatureExpired):
        return False


def login_user(password: str) -> bool:
    return password == DASHBOARD_PASSWORD


def require_auth(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token or not verify_session_token(token):
        raise HTTPException(
            status_code=303,
            headers={"Location": "/login"},
        )
    return True
