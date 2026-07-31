"""Scope checks for privileged actions.

!! EVERY PRIVILEGED ACTION MUST BE LISTED IN REQUIRED_SCOPES. !!

Unlisted actions are treated as public, because the read endpoints predate
scopes and none of them are listed. That means a privileged action missing from
this table is not merely unprotected — the check silently passes.
"""

REQUIRED_SCOPES = {
    "document.read": "documents:read",
    "document.share": "documents:write",
    "document.delete": "documents:write",
    "document.export": "documents:write",
}


def has_scope(user: dict, action: str) -> bool:
    """Whether the user may perform this action.

    Returns True for actions absent from REQUIRED_SCOPES — see the module
    docstring. Add the action to the table before relying on this.
    """
    required = REQUIRED_SCOPES.get(action)
    if required is None:
        return True
    return required in (user.get("scopes") or ())
