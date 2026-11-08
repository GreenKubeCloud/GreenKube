"""Git object IDs and GreenKube content digests (roadmap M6).

The single definition of what GreenKube accepts as an immutable Git object ID
(a full SHA-1 or SHA-256 hex string, any case) and as a content digest
(``sha256:`` followed by 64 lowercase hex characters). Callers use
``fullmatch``; partial matches are never valid.
"""

import re
from typing import TypeGuard

#: A full Git commit, tree or blob ID: 40 (SHA-1) or 64 (SHA-256) hex characters.
GIT_OBJECT_SHA = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")

#: A GreenKube plan, snapshot or render digest.
SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


_REVISION_SUFFIX = re.compile(rf"(?:sha1|sha256):({GIT_OBJECT_SHA.pattern})$")


def is_full_commit_sha(value: object) -> TypeGuard[str]:
    """Return whether ``value`` is an immutable full SHA-1 or SHA-256 object ID."""
    return isinstance(value, str) and GIT_OBJECT_SHA.fullmatch(value) is not None


def revision_commit(value: str) -> str | None:
    """Return the lowercase commit of a bare SHA or a ``…sha1:<sha>``/``…sha256:<sha>`` revision."""
    if GIT_OBJECT_SHA.fullmatch(value):
        return value.lower()
    match = _REVISION_SUFFIX.search(value)
    return match.group(1).lower() if match is not None else None
