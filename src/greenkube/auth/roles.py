"""Group to role mapping for single sign-on (roadmap F16).

The mapping is a JSON object from role to the list of IdP groups granting it,
for example ``{"admin": ["platform-admins"], "operator": ["sre"]}``. A user
receives the highest role any of their groups grants; without a matching group
they receive ``AUTH_DEFAULT_ROLE`` (``viewer`` by default), or no access at all
when it is ``none``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

from greenkube.auth.principal import Role

#: Roles from the least to the most privileged.
ROLE_ORDER: tuple[str, ...] = (Role.VIEWER.value, Role.OPERATOR.value, Role.ADMIN.value)
NO_ROLE = "none"
MAX_GROUPS = 256
MAX_GROUP_LENGTH = 256


def role_rank(role: str | None) -> int:
    """Position of ``role`` in :data:`ROLE_ORDER`; ``-1`` for an unknown role."""
    try:
        return ROLE_ORDER.index(role or "")
    except ValueError:
        return -1


def parse_role_groups(text: str | Mapping | None) -> dict[str, frozenset[str]]:
    """Parse ``AUTH_ROLE_GROUPS``; raise ``ValueError`` on an unknown role or a malformed value."""
    if text is None or (isinstance(text, str) and not text.strip()):
        return {}
    if isinstance(text, Mapping):
        raw = text
    else:
        try:
            raw = json.loads(text)
        except ValueError as error:
            raise ValueError('AUTH_ROLE_GROUPS must be a JSON object such as {"admin": ["group"]}.') from error
    if not isinstance(raw, Mapping):
        raise ValueError('AUTH_ROLE_GROUPS must be a JSON object such as {"admin": ["group"]}.')
    mapping: dict[str, frozenset[str]] = {}
    for role, groups in raw.items():
        if role not in ROLE_ORDER:
            raise ValueError(f"AUTH_ROLE_GROUPS: unknown role {role!r} (expected viewer, operator or admin).")
        if isinstance(groups, str):
            groups = [groups]
        if not isinstance(groups, list) or not all(isinstance(group, str) for group in groups):
            raise ValueError(f"AUTH_ROLE_GROUPS: the groups of {role!r} must be a list of strings.")
        cleaned = frozenset(group.strip() for group in groups if group.strip())
        if cleaned:
            mapping[role] = cleaned
    return mapping


def normalize_default_role(value: str | None) -> str:
    """Validate ``AUTH_DEFAULT_ROLE`` (``viewer``, ``operator``, ``admin`` or ``none``)."""
    text = str(value or Role.VIEWER.value).strip().lower()
    if text not in (*ROLE_ORDER, NO_ROLE):
        raise ValueError("AUTH_DEFAULT_ROLE must be 'viewer', 'operator', 'admin' or 'none'.")
    return text


def clean_groups(groups: Iterable[object]) -> frozenset[str]:
    """Keep at most :data:`MAX_GROUPS` non-empty string groups of bounded length."""
    cleaned: set[str] = set()
    for group in groups:
        if not isinstance(group, str):
            continue
        text = group.strip()
        if text and len(text) <= MAX_GROUP_LENGTH:
            cleaned.add(text)
        if len(cleaned) >= MAX_GROUPS:
            break
    return frozenset(cleaned)


def split_groups_header(value: str | None) -> frozenset[str]:
    """Groups sent by an authentication proxy, comma-separated (oauth2-proxy, Authentik, Pomerium)."""
    return clean_groups((value or "").split(","))


def role_for_groups(groups: Iterable[str], mapping: Mapping[str, frozenset[str]], default_role: str) -> str | None:
    """The highest role granted by ``groups``; ``default_role`` otherwise (``None`` for ``none``)."""
    members = set(groups)
    for role in reversed(ROLE_ORDER):
        if members & set(mapping.get(role, ())):
            return role
    return None if default_role == NO_ROLE else default_role
