from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# 路径
# ============================================================

ROOT_DIR = Path(__file__).resolve().parent.parent

SCRIPTS_DIR = ROOT_DIR / "scripts"

REPLACEMENT_DIR = ROOT_DIR / "replacement"
EXCLUSIONS_DIR = ROOT_DIR / "assetexclusions"

OUT_DIR = ROOT_DIR / "out"
MODIFIED_DIR = ROOT_DIR / "modified"
RELEASE_DIR = ROOT_DIR / "release"

MODIFIED_BUNDLES_DIR = MODIFIED_DIR / "AssetBundles"
MODIFIED_TABLES_DIR = MODIFIED_DIR / "TableBundles"

CURRENT_TXT = ROOT_DIR / "current.txt"

# 仅用于本次运行的临时文件（不进入压缩包）
TMP_CATALOG = ROOT_DIR / ".official_bundleDownloadInfo.json"
TMP_TABLE_MANIFEST = ROOT_DIR / ".official_TableManifest.json"


# ============================================================
# 输出命名
# ============================================================

EXCEL_DB_PREFIX = "6993339912994747134"

ZIP_PREFIX = "BlueArchiveCN-Uncensor-Data"


# ============================================================
# URL
# ============================================================

OFFICIAL_BUNDLE_INFO_BASE = (
    "https://static.bluearchive-cn.com"
    "/prodm39/AssetBundles/Catalog"
)

OFFICIAL_BUNDLE_BASE = (
    "https://static.bluearchive-cn.com"
    "/prodm39/AssetBundles/Android"
)

OFFICIAL_TABLE_MANIFEST_BASE = (
    "https://static.bluearchive-cn.com"
    "/prodm39/Manifest/TableBundles"
)

OFFICIAL_TABLE_BUNDLE_BASE = (
    "https://static.bluearchive-cn.com"
    "/prodm39/pool/TableBundles"
)

OLD_EXCEL_DB_URL = (
    "https://mx.infastra.de5.net"
    "/prodm39/pool/TableBundles/51/"
    "517bb1fb1aa4d980ab87644ec10ecb2a"
)


# Bundle 名称末尾：-2026-06-04_assets_all_817837721
# 去掉后与 replacement 文件夹名称精确匹配
# （必须与 replaceTexture2D.py 中的规则保持一致）
BUNDLE_SUFFIX_RE = re.compile(
    r"-\d{4}-\d{2}-\d{2}_assets_all_\d+$",
    re.IGNORECASE,
)


# ============================================================
# HTTP Session
# ============================================================

def create_session() -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )

    adapter = HTTPAdapter(max_retries=retry)

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


SESSION = create_session()


# ============================================================
# 下载（支持断线重试 + 断点续传 + 大小校验）
# ============================================================

def download_bytes(
    url: str,
    output: Path,
    max_attempts: int = 6,
) -> None:

    print(f"[DOWNLOAD] {url}")

    output.parent.mkdir(parents=True, exist_ok=True)

    tmp = output.with_name(output.name + ".part")

    if tmp.exists():
        tmp.unlink()

    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):

        resume_from = tmp.stat().st_size if tmp.exists() else 0

        headers = {"Accept-Encoding": "identity"}

        if resume_from > 0:
            headers["Range"] = f"bytes={resume_from}-"

        try:
            with SESSION.get(
                url,
                headers=headers,
                stream=True,
                timeout=(15, 120),
            ) as response:

                if response.status_code == 416:
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError("416 Range Not Satisfiable")

                response.raise_for_status()

                if response.status_code == 206:
                    mode = "ab"
                else:
                    mode = "wb"
                    resume_from = 0

                content_length = response.headers.get("Content-Length")

                expected = (
                    resume_from + int(content_length)
                    if content_length is not None
                    else None
                )

                with tmp.open(mode) as f:
                    for chunk in response.iter_content(
                        chunk_size=1024 * 1024
                    ):
                        if chunk:
                            f.write(chunk)

            actual = tmp.stat().st_size

            if expected is not None and actual != expected:
                raise RuntimeError(
                    f"文件大小不一致: expected={expected}, actual={actual}"
                )

            tmp.replace(output)
            return

        except Exception as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)

            print(
                f"  [RETRY {attempt}/{max_attempts}] "
                f"{type(exc).__name__}: {exc}；{wait}s 后重试"
            )

            time.sleep(wait)

    tmp.unlink(missing_ok=True)

    raise RuntimeError(
        f"下载失败（已重试 {max_attempts} 次）: {url}"
    ) from last_exc


def download_json(
    url: str,
    output: Path,
) -> dict:

    download_bytes(url, output)

    with output.open("r", encoding="utf-8") as f:
        return json.load(f)


# ============================================================
# 工具函数
# ============================================================

def calculate_md5(path: Path) -> str:
    md5 = hashlib.md5()

    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)

            if not chunk:
                break

            md5.update(chunk)

    return md5.hexdigest()


def write_github_output(values: dict[str, str]) -> None:
    output = os.environ.get("GITHUB_OUTPUT")

    if not output:
        return

    with open(output, "a", encoding="utf-8") as f:
        for key, value in values.items():
            f.write(f"{key}={value}\n")


def release_date_str() -> str:
    """yymmdd，使用 UTC+8（中国时间）。"""

    tz = timezone(timedelta(hours=8))

    return datetime.now(tz).strftime("%y%m%d")


def normalize_bundle_name(name: str) -> str:
    stem = (
        name[: -len(".bundle")]
        if name.lower().endswith(".bundle")
        else name
    )

    return BUNDLE_SUFFIX_RE.sub("", stem).lower()


# ============================================================
# current.txt
# ============================================================

def read_current_txt() -> tuple[str, str]:

    if not CURRENT_TXT.exists():
        raise FileNotFoundError(
            f"找不到 current.txt: {CURRENT_TXT}"
        )

    text = CURRENT_TXT.read_text(encoding="utf-8")

    resource_match = re.search(
        r"(?im)^\s*ResourceVersion\s*[:=]\s*(\d+)\s*$",
        text,
    )

    table_match = re.search(
        r"(?im)^\s*TableVersion\s*[:=]\s*(\d+)\s*$",
        text,
    )

    if resource_match is None:
        raise RuntimeError("current.txt 中找不到 ResourceVersion")

    if table_match is None:
        raise RuntimeError("current.txt 中找不到 TableVersion")

    return (
        resource_match.group(1),
        table_match.group(1),
    )


# ============================================================
# 选择需要处理的 Bundle
# ============================================================

def get_replacement_names() -> set[str]:

    if not REPLACEMENT_DIR.is_dir():
        print(f"[WARNING] replacement 目录不存在: {REPLACEMENT_DIR}")
        return set()

    return {
        p.name.lower()
        for p in REPLACEMENT_DIR.iterdir()
        if p.is_dir()
    }


def select_target_bundles(
    catalog: dict,
    replacement_names: set[str],
) -> tuple[list[str], set[str], set[str]]:
    """
    返回：
        targets          官方 catalog 中与 replacement 文件夹匹配的 Bundle 全名
        unmatched        没有任何 Bundle 对应的 replacement 文件夹名
        official_names   官方 catalog 中全部 Bundle 全名
    """

    entries = catalog.get("BundleFiles")

    if not isinstance(entries, list):
        raise RuntimeError(
            "bundleDownloadInfo.json 中不存在 BundleFiles"
        )

    targets: list[str] = []
    matched: set[str] = set()
    official_names: set[str] = set()

    for entry in entries:

        name = entry.get("Name")

        if not name or not name.lower().endswith(".bundle"):
            continue

        if name in official_names:
            raise RuntimeError(f"发现重复 Bundle Name: {name}")

        official_names.add(name)

        key = normalize_bundle_name(name)

        if key in replacement_names:
            targets.append(name)
            matched.add(key)

    unmatched = replacement_names - matched

    return targets, unmatched, official_names


# ============================================================
# AssetsExclusions：不修改，直接放入输出
# ============================================================

def find_exclusion_bundles() -> dict[str, Path]:

    result: dict[str, Path] = {}

    if not EXCLUSIONS_DIR.is_dir():
        print(f"[INFO] 没有 AssetsExclusions 目录，跳过")
        return result

    for path in sorted(EXCLUSIONS_DIR.rglob("*.bundle")):

        if not path.is_file():
            continue

        if path.name in result:
            raise RuntimeError(
                f"AssetsExclusions 中存在重名 Bundle: {path.name}\n"
                f"  {result[path.name]}\n"
                f"  {path}"
            )

        with path.open("rb") as f:
            head = f.read(64)

        if head.startswith(b"version https://git-lfs"):
            raise RuntimeError(
                f"{path} 是 Git LFS 指针，checkout 时需要 lfs: true"
            )

        if path.stat().st_size == 0:
            raise RuntimeError(f"{path} 大小为 0")

        result[path.name] = path

    return result


def copy_exclusion_bundles(
    exclusions: dict[str, Path],
) -> int:

    if not exclusions:
        return 0

    MODIFIED_BUNDLES_DIR.mkdir(parents=True, exist_ok=True)

    for name, source in exclusions.items():

        target = MODIFIED_BUNDLES_DIR / name

        shutil.copy2(source, target)

        if (
            not target.exists()
            or target.stat().st_size != source.stat().st_size
        ):
            raise RuntimeError(f"复制失败: {target}")

        print(f"[COPY-EXCLUSION] {source} -> {target}")

    return len(exclusions)


# ============================================================
# 运行脚本
# ============================================================

def run_script(script_name: str) -> None:

    script = SCRIPTS_DIR / script_name

    if not script.exists():
        raise FileNotFoundError(f"找不到脚本: {script}")

    print()
    print("=" * 72)
    print(f"[RUN] {script}")
    print("=" * 72)

    subprocess.run(
        [sys.executable, str(script)],
        cwd=ROOT_DIR,
        check=True,
    )


# ============================================================
# 清理
# ============================================================

def remove_temp_files() -> None:

    for path in ROOT_DIR.glob("*.bundle"):
        if path.is_file():
            path.unlink()

    for path in (
        ROOT_DIR / "ExcelDB_new.db",
        ROOT_DIR / "ExcelDB_old.db",
        ROOT_DIR / "ExcelDB_new_backup.db",
        TMP_CATALOG,
        TMP_TABLE_MANIFEST,
    ):
        if path.exists():
            path.unlink()


def clean_workspace() -> None:

    print("[CLEAN] 清理 generated workspace")

    for path in (MODIFIED_DIR, OUT_DIR, RELEASE_DIR):
        if path.exists():
            shutil.rmtree(path)

    remove_temp_files()


# ============================================================
# out/ -> modified/AssetBundles
# ============================================================

def copy_modified_bundles() -> int:

    if not OUT_DIR.exists():
        return 0

    bundle_files = sorted(OUT_DIR.rglob("*.bundle"))

    if not bundle_files:
        return 0

    MODIFIED_BUNDLES_DIR.mkdir(parents=True, exist_ok=True)

    count = 0

    for source in bundle_files:

        # 统一平铺到 AssetBundles/ 下
        target = MODIFIED_BUNDLES_DIR / source.name

        shutil.copy2(source, target)

        print(f"[COPY] {source} -> {target}")

        count += 1

    return count


# ============================================================
# 打包
# ============================================================

def build_zip(zip_path: Path) -> int:

    zip_path.parent.mkdir(parents=True, exist_ok=True)

    if zip_path.exists():
        zip_path.unlink()

    count = 0

    with zipfile.ZipFile(
        zip_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as zf:

        for directory in (
            MODIFIED_BUNDLES_DIR,
            MODIFIED_TABLES_DIR,
        ):

            if not directory.is_dir():
                continue

            for path in sorted(directory.rglob("*")):

                if not path.is_file():
                    continue

                # 压缩包内路径：AssetBundles/xxx.bundle、TableBundles/xxx
                arcname = path.relative_to(MODIFIED_DIR).as_posix()

                zf.write(path, arcname)

                count += 1

    return count


# ============================================================
# 主流程
# ============================================================

def main() -> int:

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--current-version-file",
        required=True,
    )

    parser.add_argument(
        "--github-output",
        action="store_true",
    )

    args = parser.parse_args()

    current_version_file = Path(args.current_version_file)

    if not current_version_file.is_absolute():
        current_version_file = ROOT_DIR / current_version_file

    # --------------------------------------------------------
    # 版本比较
    # --------------------------------------------------------

    with current_version_file.open("r", encoding="utf-8") as f:
        current_info = json.load(f)

    official_resource_version = str(current_info["ResourceVersion"])
    official_table_version = str(current_info["TableVersion"])

    (
        repository_resource_version,
        repository_table_version,
    ) = read_current_txt()

    print()
    print(f"Repository ResourceVersion: {repository_resource_version}")
    print(f"Official   ResourceVersion: {official_resource_version}")
    print(f"Repository TableVersion   : {repository_table_version}")
    print(f"Official   TableVersion   : {official_table_version}")

    changed = (
        repository_resource_version != official_resource_version
        or repository_table_version != official_table_version
    )

    if not changed:

        print()
        print("ResourceVersion 和 TableVersion 均未变化。")

        if args.github_output:
            write_github_output({"changed": "false"})

        return 0

    clean_workspace()

    # --------------------------------------------------------
    # 1. 官方 catalog（仅用于确定 Bundle 全名，不进入压缩包）
    # --------------------------------------------------------

    catalog = download_json(
        f"{OFFICIAL_BUNDLE_INFO_BASE}/"
        f"{official_resource_version}/"
        "Android/bundleDownloadInfo.json",
        TMP_CATALOG,
    )

    # --------------------------------------------------------
    # 2. 选择需要修改的 Bundle
    #    replacement/<名称> 与官方 catalog 中的 Bundle 名匹配
    #    每次都生成完整数据包，不依赖旧版本 catalog
    # --------------------------------------------------------

    replacement_names = get_replacement_names()

    targets, unmatched, official_names = select_target_bundles(
        catalog,
        replacement_names,
    )

    exclusions = find_exclusion_bundles()

    stale = sorted(n for n in exclusions if n not in official_names)

    if stale:
        raise RuntimeError(
            "AssetsExclusions 中的以下 Bundle 不在官方 catalog 中，"
            "可能官方已更新，请更换文件:\n  "
            + "\n  ".join(stale)
        )

    for name in sorted(unmatched):
        print(
            f"[WARNING] replacement/{name} "
            "在当前官方 catalog 中找不到对应 Bundle"
        )

    # AssetsExclusions 优先：不下载、不修改
    download_targets = [n for n in targets if n not in exclusions]

    print()
    print(f"匹配 replacement 的 Bundle : {len(targets)}")
    print(f"AssetsExclusions Bundle    : {len(exclusions)}")
    print(f"需要下载并修改的 Bundle    : {len(download_targets)}")

    # --------------------------------------------------------
    # 3. 下载并替换 Texture2D
    # --------------------------------------------------------

    for name in download_targets:

        if Path(name).name != name:
            raise RuntimeError(f"Bundle Name 包含路径: {name}")

        download_bytes(
            f"{OFFICIAL_BUNDLE_BASE}/{name}",
            ROOT_DIR / name,
        )

    if download_targets:
        run_script("replaceTexture2D.py")
    else:
        print("[SKIP] 没有需要替换的 Bundle，跳过 replaceTexture2D.py")

    # --------------------------------------------------------
    # 4. 放入 modified/AssetBundles
    # --------------------------------------------------------

    modified_bundle_count = copy_modified_bundles()
    exclusion_count = copy_exclusion_bundles(exclusions)

    print(f"修改后的 Bundle        : {modified_bundle_count}")
    print(f"直接复制的 Bundle      : {exclusion_count}")

    # --------------------------------------------------------
    # 5. ExcelDB
    # --------------------------------------------------------

    table_manifest = download_json(
        f"{OFFICIAL_TABLE_MANIFEST_BASE}/"
        f"{official_table_version}/"
        "TableManifest",
        TMP_TABLE_MANIFEST,
    )

    try:
        excel_crc = str(
            table_manifest["Table"]["ExcelDB.db"]["Crc"]
        ).lower()
    except KeyError as exc:
        raise RuntimeError(
            "TableManifest 中找不到 Table -> ExcelDB.db"
        ) from exc

    print()
    print(f"官方 ExcelDB Crc: {excel_crc}")

    excel_new_path = ROOT_DIR / "ExcelDB_new.db"
    excel_old_path = ROOT_DIR / "ExcelDB_old.db"

    download_bytes(
        f"{OFFICIAL_TABLE_BUNDLE_BASE}/{excel_crc[:2]}/{excel_crc}",
        excel_new_path,
    )

    download_bytes(
        OLD_EXCEL_DB_URL,
        excel_old_path,
    )

    run_script("updateExcelDB.py")

    if not excel_new_path.exists():
        raise RuntimeError(
            "updateExcelDB.py 执行后没有生成 ExcelDB_new.db"
        )

    excel_md5 = calculate_md5(excel_new_path)

    print()
    print(f"ExcelDB MD5  : {excel_md5}")
    print(f"ExcelDB Size : {excel_new_path.stat().st_size}")

    MODIFIED_TABLES_DIR.mkdir(parents=True, exist_ok=True)

    excel_target = (
        MODIFIED_TABLES_DIR
        / f"{EXCEL_DB_PREFIX}_{excel_md5}"
    )

    shutil.move(str(excel_new_path), str(excel_target))

    print(f"[MOVE] ExcelDB -> {excel_target}")

    # --------------------------------------------------------
    # 6. 打包
    # --------------------------------------------------------

    zip_name = f"{ZIP_PREFIX}{release_date_str()}.zip"
    zip_path = RELEASE_DIR / zip_name

    file_count = build_zip(zip_path)

    print()
    print(f"[ZIP] {zip_path}")
    print(
        f"      {file_count} 个文件，"
        f"{zip_path.stat().st_size / 1024 / 1024:.1f} MiB"
    )

    # --------------------------------------------------------
    # 7. 清理临时文件
    # --------------------------------------------------------

    remove_temp_files()

    print()
    print("=" * 72)
    print("资源处理完成")
    print("=" * 72)
    print(f"ResourceVersion : {official_resource_version}")
    print(f"TableVersion    : {official_table_version}")
    print(f"修改 Bundle     : {modified_bundle_count}")
    print(f"排除 Bundle     : {exclusion_count}")
    print(f"ExcelDB MD5     : {excel_md5}")
    print(f"压缩包          : {zip_path}")
    print("=" * 72)

    if args.github_output:
        write_github_output(
            {
                "changed": "true",
                "zip_name": zip_name,
                "zip_path": zip_path.relative_to(ROOT_DIR).as_posix(),
            }
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
