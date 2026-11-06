from fastapi import Request, Response
from typing import Optional
import hashlib
import re


def cache_key_builder(
    func,
    namespace: str = "",
    *,
    request: Optional[Request] = None,
    response: Optional[Response] = None,
    args: tuple = (),
    kwargs: Optional[dict] = None,
) -> str:
    """Build cache keys scoped to the calling user, excluding per-request DB objects.

    The principal's id is part of the key so one caller can never be served another
    caller's cached response, even if routes later return caller-specific data.
    """
    kwargs = kwargs or {}
    principal = kwargs.get("current_user")
    owner = getattr(principal, "actor", f"user:{principal['id']}") if principal is not None else "anonymous"
    filtered_kwargs = {k: v for k, v in kwargs.items() if k not in ("db", "current_user")}
    raw_key = f"{func.__module__}:{func.__name__}:{owner}:{args}:{sorted(filtered_kwargs.items())}"
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return f"{namespace}:{key_hash}"


def generate_md5_hash(first_name: str, last_name: str, dob: str) -> str:
    """Generate MD5 hash from the inputs to match the database match_keys format."""
    first_clean = re.sub(r'\s+', ' ', first_name).strip().upper()
    last_clean = re.sub(r'\s+', ' ', last_name).strip().upper()
    raw_string = f"{first_clean}/{last_clean}/{dob}"
    return hashlib.md5(raw_string.encode('utf-8')).hexdigest()
