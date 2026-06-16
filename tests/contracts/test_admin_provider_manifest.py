"""Ensure admin UI manifest exposes every catalog credential/proxy binding."""

from __future__ import annotations

from api.admin_config import FIELD_BY_KEY
from config.provider_catalog import PROVIDER_CATALOG
from config.settings import Settings


def test_provider_catalog_remote_credentials_in_admin_manifest() -> None:
    missing: list[str] = []
    wrong_attr: list[str] = []

    for provider_id, desc in PROVIDER_CATALOG.items():
        if desc.credential_env is None:
            continue
        if desc.credential_attr is None:
            missing.append(
                f"{provider_id}: credential_env set but credential_attr missing"
            )
            continue
        entry = FIELD_BY_KEY.get(desc.credential_env)
        if entry is None:
            missing.append(
                f"{provider_id}: {desc.credential_env} not in admin FIELD_BY_KEY"
            )
            continue
        if entry.settings_attr != desc.credential_attr:
            wrong_attr.append(
                f"{provider_id}: {desc.credential_env} maps settings_attr="
                f"{entry.settings_attr!r}, catalog expects "
                f"{desc.credential_attr!r}"
            )

    assert not missing and not wrong_attr, "\n".join(missing + wrong_attr)


def test_provider_catalog_proxy_attrs_in_admin_manifest() -> None:
    missing_key: list[str] = []
    wrong_attr: list[str] = []

    for provider_id, desc in PROVIDER_CATALOG.items():
        if desc.proxy_attr is None:
            continue
        mf = Settings.model_fields[desc.proxy_attr]
        alias = mf.validation_alias
        if alias is None:
            missing_key.append(
                f"{provider_id}: {desc.proxy_attr} has no validation_alias "
                "(admin manifest expects env-backed proxy)"
            )
            continue
        env_key = str(alias)
        entry = FIELD_BY_KEY.get(env_key)
        if entry is None:
            missing_key.append(
                f"{provider_id}: proxy env {env_key} not in FIELD_BY_KEY"
            )
            continue
        if entry.settings_attr != desc.proxy_attr:
            wrong_attr.append(
                f"{provider_id}: {env_key} maps settings_attr="
                f"{entry.settings_attr!r}, catalog expects {desc.proxy_attr!r}"
            )

    assert not missing_key and not wrong_attr, "\n".join(missing_key + wrong_attr)
