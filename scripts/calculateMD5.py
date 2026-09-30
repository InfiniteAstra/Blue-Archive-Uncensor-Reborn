from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent

OUT_DIR = ROOT_DIR / "out"

CATALOG_ROOT = (
    ROOT_DIR
    / "modified"
    / "AssetBundles"
    / "Catalog"
)


def calculate_md5(path: Path) -> str:
    md5 = hashlib.md5()

    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)

            if not chunk:
                break

            md5.update(chunk)

    return md5.hexdigest()


def main() -> None:
    if not OUT_DIR.exists():
        print("out/ 不存在，没有需要修改的 Bundle")
        return

    catalog_files = sorted(
        CATALOG_ROOT.rglob(
            "bundleDownloadInfo.json"
        )
    )

    if len(catalog_files) != 1:
        raise RuntimeError(
            "bundleDownloadInfo.json 数量异常，"
            f"找到 {len(catalog_files)} 个"
        )

    catalog_path = catalog_files[0]

    with catalog_path.open(
        "r",
        encoding="utf-8",
    ) as f:
        catalog = json.load(f)

    entries = catalog.get("BundleFiles")

    if not isinstance(entries, list):
        raise RuntimeError(
            "bundleDownloadInfo.json 中不存在 BundleFiles"
        )

    entry_map = {}

    for entry in entries:
        name = entry.get("Name")

        if not name:
            continue

        if name in entry_map:
            raise RuntimeError(
                f"BundleFiles 中存在重复 Name: {name}"
            )

        entry_map[name] = entry

    bundle_files = sorted(
        OUT_DIR.rglob("*.bundle")
    )

    print(
        f"发现 {len(bundle_files)} 个修改后的 Bundle"
    )

    for bundle_path in bundle_files:
        name = bundle_path.name

        if name not in entry_map:
            raise RuntimeError(
                f"bundleDownloadInfo.json 中找不到: {name}"
            )

        md5 = calculate_md5(bundle_path)
        size = bundle_path.stat().st_size

        entry = entry_map[name]

        old_crc = entry.get("Crc")
        old_size = entry.get("Size")

        entry["Crc"] = md5
        entry["Size"] = size

        print(f"[UPDATE] {name}")
        print(f"  Crc : {old_crc} -> {md5}")
        print(f"  Size: {old_size} -> {size}")

    with catalog_path.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            catalog,
            f,
            ensure_ascii=False,
            indent=2,
        )
        f.write("\n")

    print()
    print(
        f"bundleDownloadInfo.json 已更新: "
        f"{catalog_path}"
    )


if __name__ == "__main__":
    main()