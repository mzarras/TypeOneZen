#!/usr/bin/env python3
"""List all fields and values from session and lap records in a .fit file.

Usage: python3 parsers/list_fit_fields.py <path_to_fit_file>
"""

import sys
from pathlib import Path
from fitparse import FitFile


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 list_fit_fields.py <path_to_fit_file>")
        sys.exit(1)

    fit_path = Path(sys.argv[1]).expanduser().resolve()
    if not fit_path.exists():
        print(f"File not found: {fit_path}")
        sys.exit(1)

    ff = FitFile(str(fit_path))

    print(f"File: {fit_path.name}")
    print("=" * 70)

    print("\n--- SESSION RECORDS ---")
    for i, record in enumerate(ff.get_messages("session")):
        print(f"\n  Session #{i}:")
        for field in sorted(record.fields, key=lambda f: f.name):
            units = f" ({field.units})" if field.units else ""
            print(f"    {field.name}: {field.value}{units}")

    # Re-parse for laps (fitparse is a generator)
    ff2 = FitFile(str(fit_path))
    print("\n--- LAP RECORDS ---")
    for i, record in enumerate(ff2.get_messages("lap")):
        print(f"\n  Lap #{i}:")
        for field in sorted(record.fields, key=lambda f: f.name):
            units = f" ({field.units})" if field.units else ""
            print(f"    {field.name}: {field.value}{units}")

    # Also show activity records
    ff3 = FitFile(str(fit_path))
    print("\n--- ACTIVITY RECORDS ---")
    for i, record in enumerate(ff3.get_messages("activity")):
        print(f"\n  Activity #{i}:")
        for field in sorted(record.fields, key=lambda f: f.name):
            units = f" ({field.units})" if field.units else ""
            print(f"    {field.name}: {field.value}{units}")


if __name__ == "__main__":
    main()
