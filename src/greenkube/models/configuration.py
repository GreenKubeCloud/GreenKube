"""Typed models for layered, versioned runtime configuration."""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConfigurationLayer(StrEnum):
    """Sources that make up the effective application configuration."""

    BOOTSTRAP = "bootstrap"
    SECRETS = "secrets"
    RUNTIME_POLICY = "runtime_policy"
    DISCOVERY = "discovery"


class ConfigurationSnapshot(BaseModel):
    """An immutable view of the effective configuration."""

    model_config = ConfigDict(frozen=True)

    version: int = Field(ge=0)
    bootstrap: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, Any] = Field(default_factory=dict)
    runtime_policy: dict[str, Any] = Field(default_factory=dict)
    discovery: dict[str, Any] = Field(default_factory=dict)
    loaded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ConfigurationPersistenceAcknowledgement(BaseModel):
    """Result returned after applying a runtime configuration update."""

    version: int = Field(ge=0)
    persisted: bool
    keys: tuple[str, ...] = ()
    backend: str = "memory"
    message: str = ""


# Short aliases keep integrations that use the report terminology readable.
ConfigSnapshot = ConfigurationSnapshot
PersistenceAcknowledgement = ConfigurationPersistenceAcknowledgement
