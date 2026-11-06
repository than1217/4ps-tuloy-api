from datetime import date
from typing import Annotated, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

# Legacy request models keep ignoring unknown fields so existing callers do not break;
# they now bound lengths and reject malformed dates instead of silently returning "Not Found".
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
OptName = Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]]
OptCode = Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)]]
IsoDate = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{4}-\d{2}-\d{2}$")]


def _real_date(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return value
    date.fromisoformat(value)  # raises ValueError -> 422 for impossible dates such as 2020-02-30
    return value


class TokenResponse(BaseModel):
    access_token: str = Field(description="JWT to send as `Authorization: Bearer <access_token>`")
    token_type: str = Field("bearer", description="Always `bearer`")
    expires_in: int = Field(description="Seconds until the token expires. Sign in again after that.")

    model_config = ConfigDict(json_schema_extra={
        "examples": [{"access_token": "eyJhbGciOiJIUzI1NiJ9.example-payload.example-signature",
                      "token_type": "bearer", "expires_in": 1800}]
    })


class UserCreate(BaseModel):
    username: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9._@-]{3,64}$")]
    password: Annotated[str, StringConstraints(min_length=12, max_length=72)]


class UserInput(BaseModel):
    first_name: Name
    middle_name: OptName = None
    last_name: Name
    date_of_birth: IsoDate  # 'YYYY-MM-DD'
    sex: OptCode = None
    member_id: OptCode = None
    household_id: OptCode = None
    lrn: OptCode = None
    pcn: OptCode = None
    geocode: OptCode = None

    _dob = field_validator("date_of_birth")(_real_date)


class MothersMaiden(BaseModel):
    first_name: OptName = None
    middle_name: OptName = None
    last_name: OptName = None


class Degree(BaseModel):
    program_name: Optional[Annotated[str, StringConstraints(max_length=150)]] = None
    date_enrolled: Optional[str] = None
    program_duration: Optional[int] = Field(default=None, ge=0, le=20)
    academic_year: Optional[Annotated[str, StringConstraints(max_length=20)]] = None
    year_level: Optional[int] = Field(default=None, ge=0, le=10)
    semester: Optional[int] = Field(default=None, ge=0, le=4)


class ChedData(BaseModel):
    id: OptCode = None
    member_id: Optional[int] = Field(default=None, ge=0, le=4294967295)
    student_id: Optional[int] = Field(default=None, ge=0)
    mothers_maiden: Optional[MothersMaiden] = None
    degree: Optional[Degree] = None
    enrollment_status: OptCode = None
    date_encoded: Optional[str] = None
    date_updated: Optional[str] = None


class ChedResponsePayload(BaseModel):
    status: bool
    message: OptCode = None
    data: Optional[ChedData] = None
    error_code: OptCode = None
    enrollment_id: Optional[int] = Field(default=0, ge=0, le=4294967295)  # NOT NULL column


class VerifyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    tuloy_id: Optional[Union[str, int]] = None
    member_id: Optional[int | str] = None


class LogoItem(BaseModel):
    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
    url: str
    order: int = 0
    height: Optional[int] = Field(default=48, ge=10, le=300)


class AppSettingsSchema(BaseModel):
    app_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)] = "Tuloy Kulehiyo"
    app_tagline: Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]] = "DSWD • 4Ps"
    app_subtext: Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]] = (
        "Sign in with your approved portal account."
    )
    app_logo_url: Optional[str] = None
    app_logo_fit: Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)]] = "cover"
    app_logo_height: Optional[int] = Field(default=48, ge=10, le=300)
    app_logo_sidebar_height: Optional[int] = Field(default=34, ge=10, le=200)
    favicon_url: Optional[str] = None
    bg_type: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)] = "color"
    bg_color: Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]] = (
        "linear-gradient(135deg, #001B48 0%, #002366 50%, #0038A8 100%)"
    )
    bg_image_url: Optional[str] = None
    bg_overlay_color: Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]] = "#000000"
    bg_overlay_opacity: Optional[int] = Field(default=50, ge=0, le=100)
    text_title_color: Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]] = "#002366"
    text_tagline_color: Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]] = "#C5221F"
    text_subtext_color: Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]] = "#475569"
    logos: list[LogoItem] = Field(default_factory=list)

