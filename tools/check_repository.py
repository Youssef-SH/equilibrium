#!/usr/bin/env python3.13
"""Statically check retained resources, documentation links, UIDs, and B1 shape."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_BUILDINGS = {
    "residential_district", "factory", "commercial_district",
    "public_services_center", "recovery_hub", "recycling_center",
    "biogas_facility", "landfill", "fossil_power_plant",
    "renewable_power_plant", "green_space",
}
EXPECTED_FOUNDATIONS = {
    "industrial_legacy", "circular_foundation", "energy_pioneer",
    "investment_district",
}
BUILDING_FIELDS = {
    "cost_micro", "duration_seconds", "upkeep_micro_per_minute", "jobs",
    "demand_micro_E", "generation_micro_E", "income_micro_per_minute",
    "air_micro_per_minute", "housing", "services", "collection_per_minute",
    "recycling_per_minute", "reuse_per_minute", "treatment_per_minute",
    "waste_per_minute",
}


def main() -> int:
    errors: list[str] = []

    for folder in ("simulation", "tests", "tools"):
        for source in (ROOT / folder).rglob("*.gd"):
            text = source.read_text()
            references = re.findall(r'(?:preload|load)\("res://([^"\n]+)"\)', text)
            references += re.findall(r'^extends "res://([^"\n]+)"', text, re.MULTILINE)
            for path in references:
                if not (ROOT / path).is_file():
                    errors.append(f"Missing resource in {source.relative_to(ROOT)}: {path}")
            if folder == "simulation" and re.search(r"res://(?:tests|tools)/", text):
                errors.append(f"Production dependency crosses boundary: {source.relative_to(ROOT)}")

    for uid in ROOT.rglob("*.gd.uid"):
        if any(part in {".git", ".godot", ".venv"} for part in uid.parts):
            continue
        if not uid.with_suffix("").is_file():
            errors.append(f"Orphaned UID: {uid.relative_to(ROOT)}")
    for source in [*(ROOT / "simulation").rglob("*.gd"), *(ROOT / "tests").rglob("*.gd"), *(ROOT / "tools").rglob("*.gd")]:
        if not Path(str(source) + ".uid").is_file():
            errors.append(f"Missing UID: {source.relative_to(ROOT)}")

    pages = [ROOT / "README.md", ROOT / "AGENTS.md", *(ROOT / "docs").rglob("*.md")]
    for page in pages:
        for target in re.findall(r"\]\(([^)]+)\)", page.read_text()):
            if "://" in target or target.startswith("#") or target.startswith("/"):
                continue
            destination = target.split("#", 1)[0]
            if destination and not (page.parent / destination).exists():
                errors.append(f"Broken link in {page.relative_to(ROOT)}: {target}")

    balance = json.loads((ROOT / "config/balance/B1.json").read_text())
    if set(balance.get("buildings", {})) != EXPECTED_BUILDINGS:
        errors.append("B1 building catalogue does not contain the canonical 11 identifiers")
    if set(balance.get("foundations", {})) != EXPECTED_FOUNDATIONS:
        errors.append("B1 foundation catalogue does not contain the canonical four identifiers")
    for name, building in balance.get("buildings", {}).items():
        for tier in ("l1", "upgrade_delta"):
            if set(building.get(tier, {})) != BUILDING_FIELDS:
                errors.append(f"B1 {name}.{tier} has an unexpected field set")
        if not {"build_unlock_micro", "upgrade_unlock_micro"} <= set(building):
            errors.append(f"B1 {name} is missing unlock thresholds")
    if balance.get("city_count") != 4 or balance.get("match_seconds") != 1200:
        errors.append("B1 structural match constants changed")
    if sum(balance.get("local_simulation", {}).get(f"household_{kind}_percent", -1000) for kind in ("R", "O", "X")) != 100:
        errors.append("B1 household waste composition does not total 100 percent")

    if errors:
        print("\n".join(errors))
        return 1
    print("PASS: retained resources, production boundaries, UIDs, links, and B1 structure")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
