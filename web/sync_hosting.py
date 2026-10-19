"""Copy the canonical frontend into the retained hosting source, or check parity."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil


WEB = Path(__file__).resolve().parent
ASSETS = (
    (WEB / "index.html", WEB / "hosting/site-assets/index.html"),
    (WEB / "app.js", WEB / "hosting/public/app.js"),
    (WEB / "styles.css", WEB / "hosting/public/styles.css"),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report mismatched assets without writing files.")
    args = parser.parse_args(argv)
    mismatches = []
    for source, destination in ASSETS:
        if not source.is_file():
            print(f"Canonical frontend asset is missing: {source.name}")
            return 2
        if args.check:
            if not destination.is_file() or source.read_bytes() != destination.read_bytes():
                mismatches.append(destination.relative_to(WEB).as_posix())
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    if mismatches:
        print("Hosting frontend needs synchronization: " + ", ".join(mismatches))
        return 1
    print("Hosting frontend matches the three canonical assets." if args.check else "Synchronized three frontend assets; no deployment performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
