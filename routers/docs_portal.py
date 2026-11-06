"""API docs portals (Stoplight Elements, served from docs_site/; no third-party CDN).

/tuloy/docs       Partner docs: Authentication + Partner API. Public page and spec.
/tuloydswd/docs   DSWD internal docs: Authentication + Beneficiaries + System + Payments + BCMD/NAPA + Partner API. The page is
                  a sign-in shell with no API content; its spec needs a Bearer token whose
                  account holds an active tuloy:docs:internal grant.

Each spec is cut from the app's own OpenAPI document and keeps only the schemas its paths
reference, so the partner spec carries nothing about the internal routes.
"""
import copy
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.routing import APIRoute
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from scopes import DOCS_INTERNAL
from security import require_scope

SITE_DIR = Path(__file__).resolve().parent.parent / "docs_site"
ASSETS_PREFIX = "/docs-assets"

PARTNER_TAGS = frozenset({"Authentication", "Partner API"})
INTERNAL_TAGS = frozenset({"Authentication", "Beneficiaries", "System", "Payments", "Partner API", "BCMD / NAPA"})
EXCLUDED_PATHS = frozenset({"/signup"})  # local/dev account creation is never documented here
INTERNAL_NAVIGATION = (
    ('Authentication', 'Authentication'), ('Partner API', 'Partner API'),
    ('Beneficiaries', 'Beneficiary'), ('Payments', 'Payments'),
    ('BCMD / NAPA', 'BCMD/NAPA'), ('System', 'System'),
)

# Elements renders with inline styles and data: URIs; everything else comes from this origin.
DOCS_CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

router = APIRouter(include_in_schema=False)


def _refs(node, found: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[1])
        for value in node.values():
            _refs(value, found)
    elif isinstance(node, list):
        for value in node:
            _refs(value, found)


def build_spec(full: dict, tags: frozenset[str], title: str, description: str) -> dict:
    spec = copy.deepcopy(full)
    paths = {}
    for path, ops in spec.get("paths", {}).items():
        if path in EXCLUDED_PATHS:
            continue
        kept = {m: op for m, op in ops.items() if set(op.get("tags", ())) & tags}
        if kept:
            paths[path] = kept
    spec["paths"] = paths
    spec["info"] = {**spec.get("info", {}), "title": title, "description": description}
    spec["tags"] = [t for t in spec.get("tags", []) if t.get("name") in tags]

    # Keep only schemas reachable from the kept paths (follow refs between schemas too).
    schemas = spec.get("components", {}).get("schemas", {})
    keep: set[str] = set()
    _refs(paths, keep)
    pending = list(keep)
    while pending:
        nested: set[str] = set()
        _refs(schemas.get(pending.pop(), {}), nested)
        for name in nested - keep:
            keep.add(name)
            pending.append(name)
    if schemas:
        spec["components"]["schemas"] = {k: v for k, v in schemas.items() if k in keep}
    return spec


def _has_none(node) -> bool:
    if node is None:
        return True
    if isinstance(node, dict):
        return any(_has_none(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_none(v) for v in node)
    return False


def arrange_internal_spec(spec):
    """Overview is Elements' built-in info page; order its operation groups next."""
    names = dict(INTERNAL_NAVIGATION)
    originals = {tag['name']: tag for tag in spec.get('tags', [])}
    spec['tags'] = [{**originals.get(source, {}), 'name': display}
                    for source, display in INTERNAL_NAVIGATION]
    ranks = {display: rank for rank, (_, display) in enumerate(INTERNAL_NAVIGATION)}
    for operations in spec['paths'].values():
        for operation in operations.values():
            operation['tags'] = [names.get(tag, tag) for tag in operation.get('tags', [])]
    def rank(item):
        return min((ranks.get(tag, len(ranks)) for operation in item[1].values()
                    for tag in operation.get('tags', [])), default=len(ranks))
    spec['paths'] = dict(sorted(spec['paths'].items(), key=rank))
    return spec


def api_routes(routes):
    """APIRoutes, including those of included routers (FastAPI 0.14x wraps them as _IncludedRouter)."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from api_routes(inner.routes)


def restore_null_examples(spec: dict, routes) -> dict:
    """Put back explicit nulls in response examples.

    FastAPI serialises its OpenAPI document with exclude_none, which drops ``"tuloy_id": null`` from an
    example and makes the key look absent. Examples are copied again from each route's own ``responses``.
    """
    for route in api_routes(routes):
        for method in route.methods or ():
            op = spec.get("paths", {}).get(route.path_format, {}).get(method.lower())
            if op is None:
                continue
            for code, source in (route.responses or {}).items():
                target = op.get("responses", {}).get(str(code))
                for media, content in ((source or {}).get("content") or {}).items():
                    for key in ("example", "examples"):
                        if target is not None and key in content and _has_none(content[key]):
                            target.setdefault("content", {}).setdefault(media, {})[key] = copy.deepcopy(content[key])
    return spec


def _cached_spec(request: Request, key: str, tags: frozenset[str], title: str, description: str) -> dict:
    cache = request.app.state.__dict__.setdefault("docs_specs", {})
    if key not in cache:
        full = request.app.openapi()
        if key == 'internal':
            # Document the actual hidden staff routes only in the protected portal.
            # Never mutate app.openapi(), the live routes or the partner contract.
            full = copy.deepcopy(full)
            routes = []
            for route in api_routes(request.app.routes):
                if route.path.startswith(('/v1/api/tuloy/staff/bcmd/', '/v1/api/tuloy/staff/cor/')):
                    documented = copy.copy(route)
                    documented.include_in_schema = True
                    documented.tags = ['BCMD / NAPA'] if '/bcmd/' in route.path else ['Beneficiaries']
                    routes.append(documented)
            supplement = get_openapi(title=title, version=request.app.version, routes=routes,
                                     openapi_version=request.app.openapi_version)
            full.setdefault('paths', {}).update(supplement.get('paths', {}))
            for name, definitions in supplement.get('components', {}).items():
                full.setdefault('components', {}).setdefault(name, {}).update(definitions)
            full.setdefault('tags', []).append({'name': 'BCMD / NAPA', 'description': (
                'Shared persisted BCMD batches used by the NAPA workspace. Signed-in DSWD and TEST '
                'users with an active matching agency/partner binding are allowed; no module-specific '
                'scope is required, except tuloy:bcmd:selection:review (DSWD) for the duplicate-selection review '
                'routes. Docs access still needs tuloy:docs:internal. Categories TK (CHED, ENROLLED, first year, '
                'semester 1), TA (CHED, ENROLLED) and TT (CHED, GRADUATED). Each completed DQA run gives three NAPA files: '
                'DETAIL (passed records), SUMMARY (counts) and FAILED (failed and for-verification records). Every file '
                'requires current recorded approvals. No payment is released.')})
        spec = build_spec(full, tags, title, description)
        if key == 'internal':
            spec = arrange_internal_spec(spec)
        cache[key] = restore_null_examples(spec, request.app.routes)
    return cache[key]


PARTNER_DESCRIPTION = "Tuloy Partner API: sign in, then call the partner operations your account is granted."
INTERNAL_DESCRIPTION = "DSWD internal reference: authentication, beneficiary routes, system settings, payments, BCMD / NAPA and the Partner API."


def _page(name: str) -> FileResponse:
    return FileResponse(SITE_DIR / name, media_type="text/html", headers={"Content-Security-Policy": DOCS_CSP})


@router.get("/tuloy/docs")
def partner_docs():
    return _page("partner.html")


@router.get("/tuloy/openapi.json")
def partner_spec(request: Request):
    return JSONResponse(_cached_spec(request, "partner", PARTNER_TAGS, "Tuloy Partner API", PARTNER_DESCRIPTION))


@router.get("/tuloydswd/docs")
def internal_docs():
    return _page("internal.html")


@router.get("/tuloydswd/openapi.json", dependencies=[Depends(require_scope(DOCS_INTERNAL))])
def internal_spec(request: Request):
    return JSONResponse(_cached_spec(request, "internal", INTERNAL_TAGS, "Tuloy API (DSWD internal)",
                                     INTERNAL_DESCRIPTION))


def mount(app) -> None:
    app.include_router(router)
    # Vendor first: the /docs-assets mount would otherwise match /docs-assets/vendor/... too.
    app.mount(ASSETS_PREFIX + "/vendor", StaticFiles(directory=SITE_DIR / "vendor"), name="docs-vendor")
    app.mount(ASSETS_PREFIX, StaticFiles(directory=SITE_DIR / "assets"), name="docs-assets")
