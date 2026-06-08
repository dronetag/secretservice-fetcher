#!/usr/bin/env python3
"""Dummy app: one pydantic-settings model fed from a config FILE *and* ENV vars.

This is the natural fit for ss-fetcher: the YAML config file (from ``--config``
or ``$APP_CONFIG``) carries the non-secret structure, while the secrets arrive as
environment variables (the ``[[env]]`` entries ss-fetcher injects) -- and env
vars win over the file. pydantic-settings merges both into one validated object.

    pip install pydantic-settings pyyaml

    ss-fetcher run -- python3 app.py --config {config}
    # or with files/env already expanded (direnv / prepare):
    APP_CONFIG=config/prod.yaml O365_CLIENT_ID=... O365_CLIENT_SECRET=... python3 app.py
"""

from __future__ import annotations

import argparse
import os

from pydantic import BaseModel, SecretStr

try:
    from pydantic_settings import (
        BaseSettings,
        PydanticBaseSettingsSource,
        SettingsConfigDict,
        YamlConfigSettingsSource,
    )
except ImportError:  # pragma: no cover - example dependency
    raise SystemExit("this example needs:  pip install pydantic-settings pyyaml")


class Database(BaseModel):
    host: str
    user: str
    password: SecretStr


class Api(BaseModel):
    base_url: str
    api_key: SecretStr


class Settings(BaseSettings):
    """Settings loaded from the YAML file and the environment at the same time."""

    # env var names are matched case-insensitively, so O365_CLIENT_ID -> o365_client_id
    model_config = SettingsConfigDict(extra="ignore")

    # ...from the YAML config file (non-secret structure)
    database: Database
    api: Api
    # ...from the environment (secrets injected by ss-fetcher)
    o365_client_id: SecretStr
    o365_client_secret: SecretStr

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # First source wins: environment variables override the file.
        sources: list[PydanticBaseSettingsSource] = [env_settings]
        config_path = os.environ.get("APP_CONFIG")
        if config_path:
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_path))
        return tuple(sources)


def main() -> None:
    parser = argparse.ArgumentParser(description="dummy app (pydantic-settings)")
    parser.add_argument("--config", help="YAML config file (else $APP_CONFIG)")
    args = parser.parse_args()
    if args.config:
        os.environ["APP_CONFIG"] = args.config
    if "APP_CONFIG" not in os.environ:
        parser.error("no config: pass --config or set $APP_CONFIG")

    settings = Settings()  # <- reads the file AND the environment, then validates

    # SecretStr masks values in output, so this proves they loaded without leaking.
    print(f"config file       : {os.environ['APP_CONFIG']}")
    print("--- from the file ---")
    print(f"  database.host     = {settings.database.host}")
    print(f"  database.user     = {settings.database.user}")
    print(f"  database.password = {settings.database.password}")
    print(f"  api.base_url      = {settings.api.base_url}")
    print(f"  api.api_key       = {settings.api.api_key}")
    print("--- from the environment ---")
    print(f"  O365_CLIENT_ID     = {settings.o365_client_id}")
    print(f"  O365_CLIENT_SECRET = {settings.o365_client_secret}")
    print("--- the app would now talk to the API ---")


if __name__ == "__main__":
    main()
