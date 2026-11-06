from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from mysql.connector import Error

from config import get_settings
from database import get_app_connection, get_readonly_connection, query_failed
from errors import ApiError
from observability import audit, pseudonym
from ratelimit import login_limiter
from schemas import TokenResponse, UserCreate
from security import (
    authenticate_user,
    create_access_token,
    get_password_hash
)

router = APIRouter(tags=["Authentication"])


class SignInForm:
    """Sign-in form: username and password only. OAuth2 extras that clients may also send
    (grant_type, scope, client_id, client_secret) are accepted and ignored."""

    def __init__(
        self,
        username: Annotated[str, Form(min_length=1, max_length=255, description="API account username")],
        password: Annotated[str, Form(min_length=1, max_length=128, description="API account password",
                                      json_schema_extra={"format": "password"})],
    ):
        self.username = username
        self.password = password


def login_throttle(request: Request, form_data: SignInForm = Depends()) -> None:
    # Declared before the DB dependency so throttled attempts never take a pool connection.
    client_ip = request.client.host if request.client else "unknown"
    try:
        login_limiter.check(f"{client_ip}:{form_data.username.lower()}")
    except ApiError:
        audit("auth.token", "throttled", subject=pseudonym(form_data.username), client_ip=client_ip)
        raise


@router.post(
    "/token",
    response_model=TokenResponse,
    summary="Sign in",
    description="Exchange a username and password for a Bearer access token. Send it on later calls as "
                "`Authorization: Bearer <access_token>`. What the token may do depends on the account's "
                "active grants, checked on every request.",
    responses={
        401: {"description": "Incorrect username or password"},
        403: {"description": "Account disabled"},
        429: {"description": "Too many sign-in attempts; retry after `Retry-After` seconds"},
    },
)
def login_for_access_token(
    request: Request,
    form_data: SignInForm = Depends(),
    _throttle: None = Depends(login_throttle),
    db=Depends(get_readonly_connection),
):
    client_ip = request.client.host if request.client else "unknown"
    try:
        user = authenticate_user(db, form_data.username, form_data.password)
    except Error as exc:
        raise query_failed(exc, "login_lookup_failed")
    if not user:
        audit("auth.token", "denied", reason="bad_credentials",
              subject=pseudonym(form_data.username), client_ip=client_ip)
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED,
            "UNAUTHENTICATED",
            "Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if user.get("disabled"):
        audit("auth.token", "denied", actor=f"user:{user['id']}", reason="disabled")
        raise ApiError(status.HTTP_403_FORBIDDEN, "ACCOUNT_DISABLED", "Inactive user")
    access_token = create_access_token(data={"sub": user["username"]})
    audit("auth.token", "success", actor=f"user:{user['id']}")
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": get_settings().access_token_expire_minutes * 60,
    }


@router.post("/signup", status_code=status.HTTP_201_CREATED, summary="Create an account (local/dev only)",
             include_in_schema=get_settings().allow_signup)
def register_user(user: UserCreate, db=Depends(get_app_connection)):
    # Open self-registration is off unless ALLOW_SIGNUP=true (local/dev only; config refuses it elsewhere).
    if not get_settings().allow_signup:
        raise ApiError(status.HTTP_404_NOT_FOUND, "NOT_FOUND", "Not Found")
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id FROM api_users WHERE username = %s", (user.username,))
        if cursor.fetchone():
            raise ApiError(status.HTTP_400_BAD_REQUEST, "USERNAME_TAKEN", "Username already registered")
        cursor.execute(
            "INSERT INTO api_users (username, hashed_password) VALUES (%s, %s)",
            (user.username, get_password_hash(user.password))
        )
        db.commit()
        audit("auth.signup", "success", actor=f"user:{cursor.lastrowid}")
    except Error as exc:
        db.rollback()
        raise query_failed(exc, "signup_failed")
    finally:
        cursor.close()

    return {"message": "User created successfully. You can now log in."}
