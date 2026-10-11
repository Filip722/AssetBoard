from __future__ import annotations

from importlib import metadata, resources
from pathlib import Path

import assetboard

REQUIRED_RESOURCES = (
    "static/css/app.css",
    "static/vendor/htmx.min.js",
    "static/vendor/alpinejs-csp.min.js",
    "static/vendor/echarts.min.js",
    "locales/en/LC_MESSAGES/messages.mo",
    "locales/pl/LC_MESSAGES/messages.mo",
    "data/common-passwords.txt",
    "data/NOTICE",
    "data/SECLists-LICENSE.txt",
)


def main() -> int:
    package_path = Path(assetboard.__file__).resolve()
    source_tree = Path(__file__).resolve().parents[1] / "src" / "assetboard"
    if package_path.is_relative_to(source_tree):
        raise SystemExit(
            f"AssetBoard imported from the source tree, not the installed wheel: {package_path}"
        )

    package = resources.files("assetboard")
    templates = package.joinpath("web", "templates")
    source_templates = source_tree / "web" / "templates"
    source_vendor = source_tree / "static" / "vendor"
    vendor_resources = sorted(path for path in source_vendor.rglob("*") if path.is_file())
    if not vendor_resources:
        raise SystemExit(f"Source vendor directory is empty: {source_vendor}")
    template_resources = [
        path.relative_to(source_templates).as_posix()
        for path in source_templates.rglob("*")
        if path.is_file()
    ]
    if not template_resources:
        raise SystemExit("Installed wheel contains no templates")
    missing_resources = [
        resource
        for resource in REQUIRED_RESOURCES
        if not package.joinpath(*resource.split("/")).is_file()
    ]
    missing_resources.extend(
        f"web/templates/{resource}"
        for resource in template_resources
        if not templates.joinpath(*resource.split("/")).is_file()
    )
    for path in vendor_resources:
        relative_path = path.relative_to(source_vendor)
        installed_file = package.joinpath("static", "vendor", *relative_path.parts)
        if not installed_file.is_file():
            missing_resources.append(f"static/vendor/{relative_path.as_posix()}")
    distribution = metadata.distribution("assetboard")
    license_file = next(
        (file for file in distribution.files or () if ".dist-info/licenses/LICENSE" in str(file)),
        None,
    )
    if license_file is None or not distribution.locate_file(license_file).is_file():
        missing_resources.append("<dist-info>/licenses/LICENSE")
    if missing_resources:
        raise SystemExit(f"Installed wheel is missing resources: {', '.join(missing_resources)}")

    print(f"Non-editable AssetBoard installation: {package_path}")
    print("Installed resources:")
    for resource in (
        *REQUIRED_RESOURCES,
        *(
            f"static/vendor/{path.relative_to(source_vendor).as_posix()}"
            for path in vendor_resources
        ),
        *(f"web/templates/{path}" for path in template_resources),
    ):
        print(f"{resource}: present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
