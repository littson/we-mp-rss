"""Migrate the pre-1.5 flat configuration to the current nested schema."""

from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
from pathlib import Path

import yaml

PASSTHROUGH_KEYS = {
    "app_name",
    "db",
    "notice",
    "secret",
    "user_agent",
    "interval",
    "port",
    "debug",
    "max_page",
}


def migrate_config(legacy: dict, template: dict) -> dict:
    result = copy.deepcopy(template)
    for key in PASSTHROUGH_KEYS:
        if key in legacy:
            result[key] = copy.deepcopy(legacy[key])

    gather = result.setdefault("gather", {})
    gather["content"] = True
    gather["content_auto_check"] = False
    if "model" in legacy:
        gather["model"] = legacy["model"]
    elif isinstance(legacy.get("gather"), dict) and "model" in legacy["gather"]:
        gather["model"] = legacy["gather"]["model"]

    if isinstance(legacy.get("gather"), dict):
        gather.update(copy.deepcopy(legacy["gather"]))
        gather["content"] = True
        gather["content_auto_check"] = False

    rss = result.setdefault("rss", {})
    if isinstance(legacy.get("rss"), dict):
        rss.update(copy.deepcopy(legacy["rss"]))
    if legacy.get("rss_base_url"):
        rss["base_url"] = legacy["rss_base_url"]
    rss["full_context"] = True

    if isinstance(legacy.get("redis"), dict):
        result.setdefault("redis", {}).update(copy.deepcopy(legacy["redis"]))
    else:
        # Legacy deployments had no Redis dependency. Keep file-backed behavior
        # instead of unexpectedly binding or connecting to localhost:6379.
        redis = result.setdefault("redis", {})
        redis["url"] = ""
        redis.setdefault("server", {})["enabled"] = False

    # Retain these values for audit/fallback. Current code reads data/wx.lic.
    for key in ("token", "cookie"):
        if key in legacy:
            result[key] = legacy[key]
    return result


def migrate_license(legacy: dict, current: dict | None = None) -> dict:
    result = copy.deepcopy(current or {})
    if result.get("token_data", {}).get("token"):
        return result
    token = legacy.get("token")
    cookie = legacy.get("cookie")
    if token:
        result["token_data"] = {
            "token": token,
            "cookie": cookie or "",
            "fingerprint": "",
            "expiry": {},
        }
    return result


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _write_atomic(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--license-output", type=Path)
    args = parser.parse_args()

    legacy = _load_yaml(args.source)
    migrated = migrate_config(legacy, _load_yaml(args.template))
    _write_atomic(
        args.output,
        yaml.safe_dump(migrated, allow_unicode=True, sort_keys=False),
    )
    if args.license_output:
        current = {}
        if args.license_output.exists():
            current = json.loads(args.license_output.read_text(encoding="utf-8") or "{}")
        license_data = migrate_license(legacy, current)
        _write_atomic(
            args.license_output,
            json.dumps(license_data, ensure_ascii=False, indent=2),
        )


if __name__ == "__main__":
    main()
