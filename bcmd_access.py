"""NAPA workspace affiliation rule, separate from other APIs' module scopes."""
from scopes import KNOWN_SCOPES


def affiliation(principal):
    agencies = {g.agency for g in principal.grants
                if g.scope in KNOWN_SCOPES and g.agency in {'DSWD', 'TEST'}
                and g.partner_id == g.agency}
    # Deterministic audit attribution for an account with both bindings.
    return 'DSWD' if 'DSWD' in agencies else 'TEST' if 'TEST' in agencies else None
