"""Package the moderated widget layout with the pinned local SIP library."""

import argparse
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "widget.zip")
    parser.add_argument("--check", action="store_true", help="Verify archive contents without recompressing them")
    args = parser.parse_args()
    source = ROOT / "amo-widget"
    files = [source / name for name in ("manifest.json", "script.js", "style.css", "i18n/ru.json")]
    files += sorted((source / "images").glob("*.png"))
    files += sorted((source / "vendor").glob("*"))
    if not (source / "vendor/jssip.min.js").is_file() or not (source / "vendor/LICENSES.txt").is_file():
        parser.error("Build the pinned SIP bundle and include its licenses first")
    version = json.loads((source / "manifest.json").read_text())["widget"]["version"]
    if f"var VERSION = '{version}'" not in (source / "script.js").read_text():
        parser.error("Widget source and manifest versions differ")
    if args.check:
        try:
            with zipfile.ZipFile(args.output) as archive:
                expected = {path.relative_to(source).as_posix(): path.read_bytes() for path in files}
                if sorted(archive.namelist()) != sorted(expected):
                    parser.error("Widget archive has missing, duplicate or unexpected files")
                if any(archive.read(name) != contents for name, contents in expected.items()):
                    parser.error("Widget archive contents differ from the shipped source")
        except (OSError, zipfile.BadZipFile) as exc:
            parser.error(f"Cannot verify widget archive ({type(exc).__name__})")
        print(f"Verified widget {version}: {args.output}")
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(source).as_posix(), date_time=(2026, 11, 22, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    print(f"Built widget {version}: {args.output} ({args.output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
