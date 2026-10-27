"""Public contracts checked in CI: the API's OpenAPI spec and the event JSON Schemas.

`python -m txrisk.contract_check openapi` prints the spec the code serves at `/v1/openapi.json`,
for the committed snapshot and for `oasdiff`, which judges the spec's breaking changes.
`python -m txrisk.contract_check schemas <base> <current>` judges the event schemas: every
`*.json` under `<base>` must still be accepted by what `<current>` allows, since a consumer
built against the base would otherwise break. Adding an optional property or a new schema is
fine; removing or retyping a property, narrowing an enum, or requiring a new property is not.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from txrisk.api import ApiSettings, create_app

# The spec is built without connecting, so this database need not exist.
_SPEC_DATABASE_URL = "postgresql+psycopg://localhost/txrisk"


def openapi_spec() -> dict[str, Any]:
    """The OpenAPI spec the API serves, built without a database or model bundle."""
    spec: dict[str, Any] = create_app(ApiSettings(database_url=_SPEC_DATABASE_URL)).openapi()
    return spec


def _types(schema: dict[str, Any]) -> set[str]:
    declared = schema.get("type")
    if declared is None:
        return set()
    return set(declared) if isinstance(declared, list) else {declared}


def breaking_changes(base: Any, current: Any, path: str = "$") -> list[str]:
    """What `current` no longer allows of `base`, one line per change, for two JSON Schemas."""
    if not isinstance(base, dict) or not isinstance(current, dict):
        return []
    found: list[str] = []
    base_types, current_types = _types(base), _types(current)
    if base_types and current_types and not base_types <= current_types:
        found.append(f"{path}: type changed from {sorted(base_types)} to {sorted(current_types)}")
    if "enum" in base and "enum" in current:
        removed = [v for v in base["enum"] if v not in current["enum"]]
        if removed:
            found.append(f"{path}: enum values removed: {removed}")
    base_properties = base.get("properties", {})
    current_properties = current.get("properties", {})
    for name, schema in base_properties.items():
        if name not in current_properties:
            found.append(f"{path}.{name}: property removed")
        else:
            found += breaking_changes(schema, current_properties[name], f"{path}.{name}")
    for name in sorted(set(current.get("required", [])) - set(base.get("required", []))):
        found.append(f"{path}.{name}: newly required")
    if "items" in base and "items" in current:
        found += breaking_changes(base["items"], current["items"], f"{path}[]")
    return found


def schema_changes(base: Path, current: Path) -> list[str]:
    """Breaking changes between the `*.json` schemas in two folders; none for a missing `base`."""
    found: list[str] = []
    for schema in sorted(base.glob("*.json")) if base.is_dir() else []:
        counterpart = current / schema.name
        if not counterpart.exists():
            found.append(f"{schema.name}: schema removed")
            continue
        changes = breaking_changes(
            json.loads(schema.read_text(encoding="utf-8")),
            json.loads(counterpart.read_text(encoding="utf-8")),
        )
        found += [f"{schema.name} {change}" for change in changes]
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m txrisk.contract_check")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("openapi", help="print the OpenAPI spec the API serves")
    schemas = commands.add_parser("schemas", help="fail on breaking event schema changes")
    schemas.add_argument("base", type=Path, help="folder of the schemas to stay compatible with")
    schemas.add_argument("current", type=Path, help="folder of the schemas now")
    args = parser.parse_args(argv)
    if args.command == "openapi":
        print(json.dumps(openapi_spec(), indent=2, sort_keys=True))
        return 0
    changes = schema_changes(args.base, args.current)
    for change in changes:
        print(f"breaking: {change}", file=sys.stderr)
    return 1 if changes else 0


if __name__ == "__main__":
    sys.exit(main())
