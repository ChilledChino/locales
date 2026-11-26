#!/usr/bin/env python3
"""
Validates all locale & mode source files under src/<service>/ and builds
single-file bundles under dist/<service>/<locale>.json + dist/<service>/index.json.

Usage:
  python scripts/build.py          # validate and write dist/
  python scripts/build.py --check  # validate and verify dist/ is up to date (CI mode)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LOCALES_JSON = ROOT / "locales.json"
SRC_DIR = ROOT / "src"
DIST_DIR = ROOT / "dist"

POS_PLACEHOLDER_RE = re.compile(r"\{(\d+)\}")
NAMED_PLACEHOLDER_RE = re.compile(r"(?<!\{)\{([a-zA-Z_]\w*)(?:,|\})")
ANY_DOUBLE_BRACE_RE = re.compile(r"\{\{([^{}]+)\}\}")
VALID_BRAND_TOKENS = {"brand", "character", "coin", "coins", "formerBrand", "together"}
RESERVED_FILES = {"commands.json", "formatting.json"}


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"{path.relative_to(ROOT)}: invalid JSON: {exc}") from exc


def extract_placeholders(text: str, extra_valid_tokens: set[str] | None = None) -> tuple[list[str], list[str]]:
    pos = sorted(set(POS_PLACEHOLDER_RE.findall(text)))
    allowed = VALID_BRAND_TOKENS | (extra_valid_tokens or set())
    bad_brand = sorted(set(t for t in ANY_DOUBLE_BRACE_RE.findall(text) if t not in allowed))
    return pos, bad_brand


def extract_named_placeholders(text: str) -> list[str]:
    cleaned = ANY_DOUBLE_BRACE_RE.sub("", text)
    return sorted(set(NAMED_PLACEHOLDER_RE.findall(cleaned)))


def load_flat_categories(locale_dir: Path, errors: list[str]) -> dict[str, dict[str, str]]:
    categories: dict[str, dict[str, str]] = {}
    if not locale_dir.exists():
        return categories
    for path in sorted(locale_dir.glob("*.json")):
        if path.name in RESERVED_FILES:
            continue
        data = load_json(path)
        if not isinstance(data, dict):
            errors.append(f"{path.relative_to(ROOT)}: must be a JSON object")
            continue
        cat_map: dict[str, str] = {}
        for k, v in data.items():
            if not isinstance(k, str) or not isinstance(v, str):
                errors.append(f"{path.relative_to(ROOT)}: key '{k}' must map to a string")
                continue
            cat_map[k] = v
        categories[path.stem] = dict(sorted(cat_map.items()))
    return categories


def validate_commands(path: Path, data: Any, errors: list[str]) -> dict[str, dict[str, str]]:
    if not isinstance(data, dict):
        errors.append(f"{path.relative_to(ROOT)}: commands.json must be an object")
        return {"names": {}, "descriptions": {}}
    out: dict[str, dict[str, str]] = {"names": {}, "descriptions": {}}
    for section, max_len in (("names", 32), ("descriptions", 100)):
        raw = data.get(section, {})
        if not isinstance(raw, dict):
            errors.append(f"{path.relative_to(ROOT)}: '{section}' must be an object")
            continue
        for k, v in raw.items():
            if not isinstance(v, str) or not (1 <= len(v.strip()) <= max_len):
                errors.append(
                    f"{path.relative_to(ROOT)}: {section}['{k}'] length must be 1..{max_len} (got {len(v) if isinstance(v, str) else 'non-string'})"
                )
                continue
            out[section][k] = v
        out[section] = dict(sorted(out[section].items()))
    return out


def validate_against_base(
    service: str,
    code: str,
    mode_label: str,
    categories: dict[str, dict[str, str]],
    base_categories: dict[str, dict[str, str]],
    errors: list[str],
) -> int:
    translated_count = 0
    for cat, entries in categories.items():
        if cat not in base_categories:
            errors.append(f"{service}/{code}/{mode_label}: unknown category '{cat}' (not in default locale)")
            continue
        base_cat = base_categories[cat]
        for key, val in entries.items():
            if key not in base_cat:
                errors.append(f"{service}/{code}/{mode_label}/{cat}: unknown key '{key}'")
                continue
            if not val.strip():
                errors.append(f"{service}/{code}/{mode_label}/{cat}: empty value for '{key}'")
                continue
            translated_count += 1
            base_val = base_cat[key]
            base_double = set(ANY_DOUBLE_BRACE_RE.findall(base_val))
            b_pos, _ = extract_placeholders(base_val, base_double)
            t_pos, t_bad_brand = extract_placeholders(val, base_double)
            if b_pos != t_pos:
                errors.append(
                    f"{service}/{code}/{mode_label}/{cat}.{key}: positional placeholders {t_pos} != base {b_pos}"
                )
            if t_bad_brand:
                errors.append(
                    f"{service}/{code}/{mode_label}/{cat}.{key}: unknown brand tokens {t_bad_brand}"
                )
            if service == "web":
                b_named = extract_named_placeholders(base_val)
                t_named = extract_named_placeholders(val)
                if b_named != t_named:
                    errors.append(
                        f"{service}/{code}/{mode_label}/{cat}.{key}: named placeholders {t_named} != base {b_named}"
                    )
    return translated_count


def serialize_bundle(data: Any) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def build_service(
    service: str,
    registry: dict[str, Any],
    errors: list[str],
    check_only: bool,
    stale_files: list[str],
) -> None:
    default_locale: str = registry["defaultLocale"]
    default_mode: str = registry["defaultMode"]
    service_src = SRC_DIR / service
    base_dir = service_src / default_locale

    if not base_dir.exists():
        errors.append(f"src/{service}/{default_locale}: default locale folder is missing")
        return

    base_categories = load_flat_categories(base_dir, errors)
    total_base_keys = sum(len(cat) for cat in base_categories.values())
    if total_base_keys == 0:
        errors.append(f"src/{service}/{default_locale}: no translation keys found")
        return

    defined_modes = {m["id"]: m for m in registry.get("modes", [])}
    enabled_modes = [
        m for m in registry.get("modes", [])
        if m.get("services", {}).get(service, {}).get("enabled", False)
    ]
    enabled_mode_ids = {m["id"] for m in enabled_modes}

    enabled_locales = [
        loc for loc in registry.get("locales", [])
        if loc.get("services", {}).get(service, {}).get("enabled", False)
    ]

    service_dist = DIST_DIR / service
    expected_dist_files: set[str] = {"index.json"}
    index_locales: list[dict[str, Any]] = []

    for loc in enabled_locales:
        code = loc["code"]
        loc_dir = service_src / code
        if not loc_dir.exists():
            errors.append(f"src/{service}/{code}: enabled in locales.json but folder does not exist")
            continue

        categories = load_flat_categories(loc_dir, errors)
        if code == default_locale:
            translated_keys = total_base_keys
        else:
            translated_keys = validate_against_base(service, code, "normal", categories, base_categories, errors)

        # Load modes overlays (only modes enabled for this service, excluding defaultMode)
        modes_out: dict[str, dict[str, dict[str, str]]] = {}
        mode_key_counts: dict[str, int] = {}
        modes_dir = loc_dir / "modes"
        if modes_dir.exists():
            for child in sorted(modes_dir.iterdir()):
                if not child.is_dir():
                    continue
                mode_id = child.name
                if mode_id not in defined_modes:
                    errors.append(f"src/{service}/{code}/modes/{mode_id}: mode not declared in locales.json")
                    continue
                if mode_id == default_mode:
                    errors.append(f"src/{service}/{code}/modes/{mode_id}: default mode belongs in root folder, not modes/")
                    continue
                if mode_id not in enabled_mode_ids:
                    continue
                mode_cats = load_flat_categories(child, errors)
                count = validate_against_base(service, code, f"modes/{mode_id}", mode_cats, base_categories, errors)
                if mode_cats:
                    modes_out[mode_id] = mode_cats
                    mode_key_counts[mode_id] = count

        commands_path = loc_dir / "commands.json"
        commands_out = (
            validate_commands(commands_path, load_json(commands_path), errors)
            if commands_path.exists()
            else {"names": {}, "descriptions": {}}
        )

        formatting_path = loc_dir / "formatting.json"
        formatting_out = load_json(formatting_path) if formatting_path.exists() else None

        bundle: dict[str, Any] = {
            "schemaVersion": 1,
            "service": service,
            "code": code,
            "categories": categories,
            "modes": modes_out,
        }
        if service == "bot" or commands_path.exists():
            bundle["commands"] = commands_out
        if formatting_out is not None:
            bundle["formatting"] = formatting_out

        payload = serialize_bundle(bundle)
        sha256 = hashlib.sha256(payload).hexdigest()
        file_name = f"{code}.json"
        expected_dist_files.add(file_name)
        target = service_dist / file_name
        write_or_check(target, payload, check_only, stale_files)

        coverage = round((translated_keys / total_base_keys) * 100.0, 2) if total_base_keys else 0.0
        index_locales.append(
            {
                "code": code,
                "nativeName": loc["nativeName"],
                "englishName": loc["englishName"],
                "discordLocales": loc.get("discordLocales", []),
                "aliases": loc.get("aliases", []),
                "fallback": loc.get("fallback"),
                "dateTimeRecognizerCulture": loc.get("dateTimeRecognizerCulture", "en-us"),
                "file": file_name,
                "sha256": sha256,
                "bytes": len(payload),
                "translatedKeys": translated_keys,
                "totalKeys": total_base_keys,
                "coverage": coverage,
                "modeKeys": mode_key_counts,
            }
        )

    index_obj = {
        "schemaVersion": 1,
        "service": service,
        "defaultLocale": default_locale,
        "defaultMode": default_mode,
        "locales": index_locales,
        "modes": [
            {
                "id": m["id"],
                "name": m["name"],
                "description": m.get("description", ""),
                "aliases": m.get("aliases", []),
            }
            for m in enabled_modes
        ],
    }
    index_payload = serialize_bundle(index_obj)
    write_or_check(service_dist / "index.json", index_payload, check_only, stale_files)

    if service_dist.exists():
        for existing in sorted(service_dist.glob("*.json")):
            if existing.name not in expected_dist_files:
                if check_only:
                    stale_files.append(f"unexpected file {existing.relative_to(ROOT)}")
                else:
                    existing.unlink()


def write_or_check(path: Path, payload: bytes, check_only: bool, stale_files: list[str]) -> None:
    if check_only:
        if not path.exists() or path.read_bytes() != payload:
            stale_files.append(str(path.relative_to(ROOT)))
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and bundle locale packs.")
    parser.add_argument("--check", action="store_true", help="Validate without writing; fail if dist/ is stale.")
    args = parser.parse_args()

    registry = load_json(LOCALES_JSON)
    errors: list[str] = []
    stale_files: list[str] = []

    if not SRC_DIR.exists():
        print("ERROR: src/ directory missing", file=sys.stderr)
        return 1

    services = sorted(p.name for p in SRC_DIR.iterdir() if p.is_dir())
    for service in services:
        build_service(service, registry, errors, args.check, stale_files)

    if errors:
        print("Locale validation FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    if stale_files:
        print("dist/ is out of date (run `python scripts/build.py`):", file=sys.stderr)
        for f in stale_files:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(f"OK: validated and bundled services: {', '.join(services)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
