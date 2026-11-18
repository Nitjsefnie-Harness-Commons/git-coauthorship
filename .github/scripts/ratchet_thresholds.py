#!/usr/bin/env python3
"""Read the coverage floor from .github/ci-thresholds.json, or stage a raised
copy of it for ratchet-push.yml; refuse any record or measurement outside the
schema, so a refusal is never mistaken for "no raise"."""
import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

GAP = Decimal("1.5")
FIELDS = ("floor", "measured")


def percentage(text: str, field: str) -> Decimal:
    """A finite number in [0, 100], read with decimal arithmetic."""
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{field} must be a number, not {text!r}") from None
    if not number.is_finite() or not 0 <= number <= 100:
        raise ValueError(f"{field} must be a percentage, not {text!r}")
    return number


def load(path: Path) -> tuple[dict[str, Any], Decimal]:
    """The whole document and its floor, with every key checked."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"schema_version", "coverage"}:
        raise ValueError("the keys must be exactly schema_version and coverage")
    version = document["schema_version"]
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise ValueError("schema_version must be 1")
    coverage = document["coverage"]
    if not isinstance(coverage, dict) or set(coverage) != {"python"}:
        raise ValueError("coverage must hold exactly python")
    python = coverage["python"]
    if not isinstance(python, dict) or set(python) != set(FIELDS):
        raise ValueError("coverage.python must hold exactly floor and measured")
    for field in FIELDS:
        if type(python[field]) not in (int, float):
            raise ValueError(f"coverage.python.{field} must be a JSON number, not {python[field]!r}")
    floor, _ = (percentage(str(python[field]), f"coverage.python.{field}") for field in FIELDS)
    return document, floor


def stage(path: Path, measured: Decimal, out_dir: Path) -> None:
    """Write the raised document into out_dir only when the floor rises."""
    document, floor = load(path)
    raised = measured - GAP
    if raised <= floor:
        print(f"No raise: {measured} - {GAP} does not exceed the floor {floor}")
        return
    document["coverage"]["python"] = {"floor": float(raised), "measured": float(measured)}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / path.name).write_text(json.dumps(document, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Coverage floor: {floor} -> {raised}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("floor").add_argument("thresholds", type=Path)
    raising = commands.add_parser("raise")
    raising.add_argument("thresholds", type=Path)
    raising.add_argument("--measured", required=True)
    raising.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "floor":
            print(load(args.thresholds)[1])
        else:
            stage(args.thresholds, percentage(args.measured, "measured"), args.out_dir)
    except (OSError, ValueError) as error:
        print(f"{args.thresholds}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
