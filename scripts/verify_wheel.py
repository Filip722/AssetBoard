from __future__ import annotations

import sys
import zipfile
from pathlib import Path

SOURCE_TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "assetboard" / "web" / "templates"
SOURCE_VENDOR = Path(__file__).resolve().parents[1] / "src" / "assetboard" / "static" / "vendor"
REQUIRED_FILES = {
    "assetboard/static/css/app.css",
    "assetboard/static/vendor/htmx.min.js",
    "assetboard/static/vendor/alpinejs-csp.min.js",
    "assetboard/static/vendor/echarts.min.js",
    "assetboard/locales/en/LC_MESSAGES/messages.mo",
    "assetboard/locales/pl/LC_MESSAGES/messages.mo",
}


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: verify_wheel.py WHEEL")
    wheel_path = Path(sys.argv[1])
    vendor_files = sorted(path for path in SOURCE_VENDOR.rglob("*") if path.is_file())
    if not vendor_files:
        raise SystemExit(f"Source vendor directory is empty: {SOURCE_VENDOR}")
    required_files = REQUIRED_FILES | {
        f"assetboard/web/templates/{path.relative_to(SOURCE_TEMPLATES).as_posix()}"
        for path in SOURCE_TEMPLATES.rglob("*")
        if path.is_file()
    }
    required_files |= {
        f"assetboard/static/vendor/{path.relative_to(SOURCE_VENDOR).as_posix()}"
        for path in vendor_files
    }
    with zipfile.ZipFile(wheel_path) as wheel:
        contents = set(wheel.namelist())
        print(f"Contents of {wheel_path}:")
        for path in sorted(contents):
            print(path)

    missing_files = sorted(required_files - contents)
    if not any(
        path.startswith("assetboard-") and path.endswith(".dist-info/licenses/LICENSE")
        for path in contents
    ):
        missing_files.append("<dist-info>/licenses/LICENSE")
    if missing_files:
        raise SystemExit(f"Wheel is missing runtime resources: {', '.join(missing_files)}")
    print(
        "All source vendor files, templates, static assets, and locale catalogs "
        "are present in the wheel."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
