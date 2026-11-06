import json

from fastapi import APIRouter, Depends
from mysql.connector import Error

from config import get_settings
from database import get_app_connection, get_readonly_connection, query_failed
from errors import ApiError
from observability import audit
from schemas import AppSettingsSchema, LogoItem
from security import Principal, get_beneficiary_principal, get_settings_writer

router = APIRouter(tags=["System"])

APP_SETTINGS_COLUMNS = (
    "`id`,`app_name`,`app_tagline`,`app_subtext`,`app_logo_url`,`app_logo_fit`,"
    "`app_logo_height`,`app_logo_sidebar_height`,`favicon_url`,`bg_type`,`bg_color`,"
    "`bg_image_url`,`bg_overlay_color`,`bg_overlay_opacity`,`text_title_color`,"
    "`text_tagline_color`,`text_subtext_color`,`logos_json`,`updated_at`"
)

DEFAULT_BG_COLOR = "linear-gradient(135deg, #001B48 0%, #002366 50%, #0038A8 100%)"
DEFAULT_SUBTEXT = "Sign in with your approved portal account."
DEFAULT_LOGOS = [
    {
        "id": "logo-dswd",
        "name": "DSWD Emblem",
        "url": "/src/images/dswd-logo.png",
        "order": 0,
        "height": 48,
    },
    {
        "id": "logo-bagong",
        "name": "Bagong Pilipinas Logo",
        "url": "/src/images/bagong-pilipinas-logo.png",
        "order": 1,
        "height": 48,
    },
]


def _row_to_settings(row: dict | None) -> AppSettingsSchema:
    if not row:
        return AppSettingsSchema(logos=[LogoItem(**item) for item in DEFAULT_LOGOS])

    raw_logos = row.get("logos_json")
    try:
        parsed_logos = json.loads(raw_logos) if raw_logos else DEFAULT_LOGOS
        if not isinstance(parsed_logos, list):
            parsed_logos = DEFAULT_LOGOS
    except Exception:
        parsed_logos = DEFAULT_LOGOS

    sorted_logos = sorted(
        (item for item in parsed_logos if isinstance(item, dict)),
        key=lambda x: x.get("order", 0),
    )

    return AppSettingsSchema(
        app_name=row.get("app_name") or "Tuloy Kulehiyo",
        app_tagline=row.get("app_tagline") or "DSWD • 4Ps",
        app_subtext=row.get("app_subtext") or DEFAULT_SUBTEXT,
        app_logo_url=row.get("app_logo_url"),
        app_logo_fit=row.get("app_logo_fit") or "cover",
        app_logo_height=row.get("app_logo_height") if row.get("app_logo_height") is not None else 48,
        app_logo_sidebar_height=(
            row.get("app_logo_sidebar_height") if row.get("app_logo_sidebar_height") is not None else 34
        ),
        favicon_url=row.get("favicon_url"),
        bg_type=row.get("bg_type") or "color",
        bg_color=row.get("bg_color") or DEFAULT_BG_COLOR,
        bg_image_url=row.get("bg_image_url"),
        bg_overlay_color=row.get("bg_overlay_color") or "#000000",
        bg_overlay_opacity=row.get("bg_overlay_opacity") if row.get("bg_overlay_opacity") is not None else 50,
        text_title_color=row.get("text_title_color") or "#002366",
        text_tagline_color=row.get("text_tagline_color") or "#C5221F",
        text_subtext_color=row.get("text_subtext_color") or "#475569",
        logos=[LogoItem(**item) for item in sorted_logos],
    )


@router.get(
    "/GetAppSettings",
    response_model=AppSettingsSchema,
    summary="Get App Settings",
    description="Load current application branding, logos, favicon, and login background settings for TuloyWebApp.",
)
def get_app_settings(
    current_user: Principal = Depends(get_beneficiary_principal),
    db=Depends(get_readonly_connection),
):
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute(
            f"SELECT {APP_SETTINGS_COLUMNS} FROM app_settings ORDER BY `updated_at` DESC, `id` DESC LIMIT 1"
        )
        row = cursor.fetchone()
    except Error as exc:
        raise query_failed(exc, "app_settings_read_failed")
    finally:
        cursor.close()

    audit("system.app_settings.read", "success", actor=current_user.actor)
    return _row_to_settings(row)


@router.post(
    "/PostAppSettings",
    response_model=AppSettingsSchema,
    summary="Post App Settings",
    description="Update application branding, app name, logos, favicon, and login background settings in app_settings.",
)
def post_app_settings(
    payload: AppSettingsSchema,
    current_user: Principal = Depends(get_settings_writer),
    db=Depends(get_app_connection),
):
    # Any signed-in account could otherwise rewrite the login page for everyone (logo and background URLs).
    if not get_settings().settings_write_enabled:
        audit("system.app_settings.write", "denied", actor=f"user:{current_user.id}", reason="writes_disabled")
        raise ApiError(403, "SETTINGS_WRITE_DISABLED", "Settings changes are disabled on this server")
    app_name = payload.app_name.strip() if payload.app_name else "Tuloy Kulehiyo"
    app_tagline = payload.app_tagline.strip() if payload.app_tagline else "DSWD • 4Ps"
    app_subtext = payload.app_subtext.strip() if payload.app_subtext else DEFAULT_SUBTEXT
    app_logo_url = payload.app_logo_url
    app_logo_fit = payload.app_logo_fit or "cover"
    app_logo_height = payload.app_logo_height if payload.app_logo_height is not None else 48
    app_logo_sidebar_height = payload.app_logo_sidebar_height if payload.app_logo_sidebar_height is not None else 34
    favicon_url = payload.favicon_url
    bg_type = payload.bg_type or "color"
    bg_color = payload.bg_color or DEFAULT_BG_COLOR
    bg_image_url = payload.bg_image_url
    bg_overlay_color = payload.bg_overlay_color or "#000000"
    bg_overlay_opacity = payload.bg_overlay_opacity if payload.bg_overlay_opacity is not None else 50
    text_title_color = payload.text_title_color or "#002366"
    text_tagline_color = payload.text_tagline_color or "#C5221F"
    text_subtext_color = payload.text_subtext_color or "#475569"

    logos_dicts = sorted(
        (logo.model_dump() for logo in payload.logos),
        key=lambda x: x.get("order", 0),
    )
    logos_json = json.dumps(logos_dicts)

    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute("SELECT `id` FROM app_settings ORDER BY `updated_at` DESC, `id` DESC LIMIT 1")
        existing = cursor.fetchone()
        if existing and existing.get("id") is not None:
            cursor.execute(
                """
                UPDATE app_settings SET
                    `app_name` = %s,
                    `app_tagline` = %s,
                    `app_subtext` = %s,
                    `app_logo_url` = %s,
                    `app_logo_fit` = %s,
                    `app_logo_height` = %s,
                    `app_logo_sidebar_height` = %s,
                    `favicon_url` = %s,
                    `bg_type` = %s,
                    `bg_color` = %s,
                    `bg_image_url` = %s,
                    `bg_overlay_color` = %s,
                    `bg_overlay_opacity` = %s,
                    `text_title_color` = %s,
                    `text_tagline_color` = %s,
                    `text_subtext_color` = %s,
                    `logos_json` = %s,
                    `updated_at` = CURRENT_TIMESTAMP
                WHERE `id` = %s
                """,
                (
                    app_name,
                    app_tagline,
                    app_subtext,
                    app_logo_url,
                    app_logo_fit,
                    app_logo_height,
                    app_logo_sidebar_height,
                    favicon_url,
                    bg_type,
                    bg_color,
                    bg_image_url,
                    bg_overlay_color,
                    bg_overlay_opacity,
                    text_title_color,
                    text_tagline_color,
                    text_subtext_color,
                    logos_json,
                    existing["id"],
                ),
            )
        else:
            cursor.execute(
                """
                INSERT INTO app_settings (
                    `app_name`, `app_tagline`, `app_subtext`, `app_logo_url`, `app_logo_fit`,
                    `app_logo_height`, `app_logo_sidebar_height`, `favicon_url`, `bg_type`,
                    `bg_color`, `bg_image_url`, `bg_overlay_color`, `bg_overlay_opacity`,
                    `text_title_color`, `text_tagline_color`, `text_subtext_color`, `logos_json`
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    app_name,
                    app_tagline,
                    app_subtext,
                    app_logo_url,
                    app_logo_fit,
                    app_logo_height,
                    app_logo_sidebar_height,
                    favicon_url,
                    bg_type,
                    bg_color,
                    bg_image_url,
                    bg_overlay_color,
                    bg_overlay_opacity,
                    text_title_color,
                    text_tagline_color,
                    text_subtext_color,
                    logos_json,
                ),
            )
        db.commit()
    except Error as exc:
        db.rollback()
        audit("system.app_settings.write", "error", actor=current_user.actor)
        raise query_failed(exc, "app_settings_write_failed")
    finally:
        cursor.close()

    audit("system.app_settings.write", "success", actor=current_user.actor)
    return AppSettingsSchema(
        app_name=app_name,
        app_tagline=app_tagline,
        app_subtext=app_subtext,
        app_logo_url=app_logo_url,
        app_logo_fit=app_logo_fit,
        app_logo_height=app_logo_height,
        app_logo_sidebar_height=app_logo_sidebar_height,
        favicon_url=favicon_url,
        bg_type=bg_type,
        bg_color=bg_color,
        bg_image_url=bg_image_url,
        bg_overlay_color=bg_overlay_color,
        bg_overlay_opacity=bg_overlay_opacity,
        text_title_color=text_title_color,
        text_tagline_color=text_tagline_color,
        text_subtext_color=text_subtext_color,
        logos=[LogoItem(**item) for item in logos_dicts],
    )
