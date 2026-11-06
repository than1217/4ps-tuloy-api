"""Keep native and SSO actor identifiers in their own foreign-key namespaces."""
from errors import ApiError


def actor_column(actor, prefix='created_by'):
    if prefix not in {'created_by', 'actor'}:
        raise ValueError('Unknown actor column prefix')
    if actor.kind not in {'api', 'staff'} or type(actor.id) is not int or actor.id <= 0:
        raise ApiError(403, 'FORBIDDEN', 'Unsupported batch actor identity')
    suffix = 'staff_user_id' if actor.kind == 'staff' else 'api_user_id'
    return f'{prefix}_{suffix}'


def actor_values(actor, prefix='created_by'):
    column = actor_column(actor, prefix)
    return {f'{prefix}_api_user_id': None, f'{prefix}_staff_user_id': None,
            column: actor.id}
