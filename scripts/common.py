"""公共工具：配置、HTTP 下载（带校验/重试）、清单索引、GitHub Actions 输出。

所有脚本都通过 ``import common`` 使用本模块。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- #
# 仓库 / 站点常量
# --------------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parent.parent

OFFICIAL_BASE = "https://static.bluearchive-cn.com/prodm39"
MIRROR_BASE = "https://mx.infastra.de5.net/prodm39"

#: 官方 AssetBundle 文件下载地址（``{name}`` 为 bundleDownloadInfo 里的 Name）
ASSET_BUNDLE_BASE = OFFICIAL_BASE + "/AssetBundles/Android"
#: 官方资源清单
CATALOG_TEMPLATE = OFFICIAL_BASE + "/AssetBundles/Catalog/{version}/Android/bundleDownloadInfo.json"
#: 镜像站上的“修改后清单”（本仓库发布出去的那一份），用于在缺少仓库清单时找回上一版补丁
MIRROR_CATALOG_TEMPLATE = MIRROR_BASE + "/AssetBundles/Catalog/{version}/Android/bundleDownloadInfo.json"
#: 官方 TableManifest（注意官方没有 .json 后缀）
TABLE_MANIFEST_TEMPLATE = OFFICIAL_BASE + "/Manifest/TableBundles/{version}/TableManifest"
#: 官方 TableBundles 池，路径 = md5(文件) 前两位 / md5
TABLE_POOL_TEMPLATE = OFFICIAL_BASE + "/pool/TableBundles/{prefix}/{crc}"
#: 固定的旧版（已汉化）ExcelDB，update_exceldb.py 的还原来源
OLD_EXCELDB_URL = MIRROR_BASE + "/pool/TableBundles/51/517bb1fb1aa4d980ab87644ec10ecb2a"

#: 版本号接口
STATE_API = "https://gs-api.bluearchive-cn.com/api/state"
SETUP_API = "https://bluearchive-cn.com/api/meta/setup"

#: TableBundles 补丁文件的固定前缀（项目约定：文件名为 ``<ID>_<官方 ExcelDB 的 MD5>``）
TABLE_BUNDLE_ID = "6993339912994747134"

USER_AGENT = "BlueArchiveCN-Uncensor-Action/2.0 (+https://github.com/)"

IS_GITHUB = os.environ.get("GITHUB_ACTIONS") == "true"

CURRENT_KEYS = ("ResourceVersion", "TableVersion", "MediaVersion")


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #

def log(message: Any = "", end: str = "\n") -> None:
    print(message, end=end, flush=True)


def warn(message: Any) -> None:
    log(f"::warning::{message}" if IS_GITHUB else f"[警告] {message}")


def error(message: Any) -> None:
    log(f"::error::{message}" if IS_GITHUB else f"[错误] {message}")


@contextmanager
def group(title: str) -> Iterator[None]:
    """在 GitHub Actions 日志里折叠一段输出。"""
    if IS_GITHUB:
        log(f"::group::{title}")
    else:
        log(f"\n── {title} ──")
    try:
        yield
    finally:
        if IS_GITHUB:
            log("::endgroup::")


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.2f} GB"


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

@dataclass
class Config:
    """一次运行需要的所有路径与开关。"""

    root: Path
    work_dir: Path
    out_dir: Path
    manifest_dir: Path
    dist_dir: Path
    current_file: Path
    exclusions_dir: Path
    replacement_dir: Path
    verify: bool = True
    all_diffs: bool = False
    table_bundle_id: str = TABLE_BUNDLE_ID
    asset_bundle_base: str = ASSET_BUNDLE_BASE
    catalog_template: str = CATALOG_TEMPLATE
    mirror_catalog_template: str = MIRROR_CATALOG_TEMPLATE
    table_manifest_template: str = TABLE_MANIFEST_TEMPLATE
    table_pool_template: str = TABLE_POOL_TEMPLATE
    old_exceldb_url: str = OLD_EXCELDB_URL

    @classmethod
    def create(
        cls,
        root: Optional[str] = None,
        *,
        work_dir: Optional[str] = None,
        out_dir: Optional[str] = None,
        manifest_dir: Optional[str] = None,
        dist_dir: Optional[str] = None,
        current_file: Optional[str] = None,
        exclusions_dir: Optional[str] = None,
        replacement_dir: Optional[str] = None,
        verify: bool = True,
        all_diffs: bool = False,
        table_bundle_id: str = TABLE_BUNDLE_ID,
        **urls: str,
    ) -> "Config":
        root_path = Path(root).resolve() if root else ROOT

        def resolve(value: Optional[str], default: Path) -> Path:
            path = Path(value) if value else default
            if not path.is_absolute():
                path = root_path / path
            return path.resolve()

        kwargs = {key: value for key, value in urls.items() if value is not None}
        return cls(
            root=root_path,
            work_dir=resolve(work_dir, root_path / "work"),
            out_dir=resolve(out_dir, root_path / "out"),
            manifest_dir=resolve(manifest_dir, root_path / "manifest"),
            dist_dir=resolve(dist_dir, root_path / "dist"),
            current_file=resolve(current_file, root_path / "current.txt"),
            exclusions_dir=resolve(exclusions_dir, root_path / "assets" / "exclusions"),
            replacement_dir=resolve(replacement_dir, root_path / "assets" / "replacement"),
            verify=verify,
            all_diffs=all_diffs,
            table_bundle_id=table_bundle_id,
            **kwargs,
        )

    # ------------------------------------------------------------------ #
    # 派生路径
    # ------------------------------------------------------------------ #
    @property
    def downloads_dir(self) -> Path:
        return self.work_dir / "downloads"

    @property
    def bundles_dir(self) -> Path:
        """下载下来的官方 .bundle 文件。"""
        return self.downloads_dir / "bundles"

    @property
    def report_dir(self) -> Path:
        return self.work_dir / "report"

    @property
    def restore_dir(self) -> Path:
        """Restore 包的内容（官方原版资源）。"""
        return self.work_dir / "restore"

    @property
    def out_bundles(self) -> Path:
        return self.out_dir / "AssetBundles"

    @property
    def out_tables(self) -> Path:
        return self.out_dir / "TableBundles"

    @property
    def restore_bundles(self) -> Path:
        return self.restore_dir / "AssetBundles"

    @property
    def restore_tables(self) -> Path:
        return self.restore_dir / "TableBundles"

    @property
    def official_manifest(self) -> Path:
        return self.manifest_dir / "bundleDownloadInfo_Official.json"

    @property
    def modified_manifest(self) -> Path:
        return self.manifest_dir / "bundleDownloadInfo_Modified.json"

    @property
    def manifest_state(self) -> Path:
        return self.manifest_dir / "manifest_state.json"

    def prepare(self) -> None:
        for path in (self.downloads_dir, self.out_dir, self.report_dir, self.dist_dir):
            path.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

_session: Optional[requests.Session] = None


def http() -> requests.Session:
    global _session
    if _session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
        retry = Retry(
            total=5,
            connect=5,
            read=5,
            backoff_factor=1.5,
            status_forcelist=(408, 429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=8)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _session = session
    return _session


def http_get(url: str, *, timeout: Tuple[float, float] = (15, 180), **kwargs: Any) -> requests.Response:
    response = http().get(url, timeout=timeout, **kwargs)
    response.raise_for_status()
    return response


def fetch_json(url: str, **kwargs: Any) -> Any:
    return http_get(url, **kwargs).json()


def fetch_bytes(url: str, **kwargs: Any) -> bytes:
    return http_get(url, **kwargs).content


def md5_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _file_matches(path: Path, md5: Optional[str], size: Optional[int]) -> bool:
    if not path.is_file():
        return False
    if size is not None and path.stat().st_size != int(size):
        return False
    if md5 is not None and md5_file(path) != md5.lower():
        return False
    return True


def _download_once(
    url: str,
    target: Path,
    *,
    desc: Optional[str],
    expected_size: Optional[int],
    chunk: int,
) -> None:
    label = desc or Path(url).name
    with http_get(url, stream=True, timeout=(15, 300)) as response:
        total = expected_size
        if total is None:
            header = response.headers.get("Content-Length")
            total = int(header) if header and header.isdigit() else None
        done = 0
        next_mark = 0.0
        with open(target, "wb") as handle:
            for block in response.iter_content(chunk):
                handle.write(block)
                done += len(block)
                if total:
                    percent = done * 100.0 / total
                    if percent >= next_mark:
                        log(f"    {label}: {percent:5.1f}% ({human_size(done)} / {human_size(total)})")
                        next_mark = percent + 20
                else:
                    log(f"    {label}: {human_size(done)}")


def download(
    url: str,
    dest: Path,
    *,
    md5: Optional[str] = None,
    size: Optional[int] = None,
    desc: Optional[str] = None,
    chunk: int = 1 << 20,
    retries: int = 3,
) -> Path:
    """下载文件到 ``dest``，已存在且校验通过则直接复用。"""
    dest = Path(dest)
    expected_md5 = md5.lower() if md5 else None

    if dest.exists():
        if _file_matches(dest, expected_md5, size):
            log(f"[复用] {desc or dest.name} ({human_size(dest.stat().st_size)})")
            return dest
        log(f"[重下] {desc or dest.name} 已存在但校验不一致")
        dest.unlink()

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    last_error: Optional[BaseException] = None

    for attempt in range(1, retries + 1):
        try:
            _download_once(url, tmp, desc=desc, expected_size=size, chunk=chunk)
            actual_size = tmp.stat().st_size
            actual_md5 = md5_file(tmp)
            if size is not None and actual_size != int(size):
                raise ValueError(f"大小不符: 期望 {size}, 实际 {actual_size}")
            if expected_md5 is not None and actual_md5 != expected_md5:
                raise ValueError(f"MD5 不符: 期望 {expected_md5}, 实际 {actual_md5}")
            os.replace(tmp, dest)
            log(f"[完成] {desc or dest.name} ({human_size(actual_size)}, md5={actual_md5})")
            return dest
        except Exception as exc:  # noqa: BLE001 - 需要重试任意网络/校验错误
            last_error = exc
            tmp.unlink(missing_ok=True)
            warn(f"下载失败 ({attempt}/{retries}) {url}: {exc}")
            if attempt < retries:
                time.sleep(min(10, 2 * attempt))

    raise RuntimeError(f"无法下载 {url}: {last_error}")


def copy_with_digest(src: Path, dest: Path) -> Tuple[str, int]:
    """复制文件并返回 (md5, size)。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return md5_file(dest), dest.stat().st_size


# --------------------------------------------------------------------------- #
# JSON / 清单
# --------------------------------------------------------------------------- #

def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: Any, *, indent: Optional[int] = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if indent:
        text = json.dumps(data, ensure_ascii=False, indent=indent)
    else:
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text + "\n", encoding="utf-8")
    return path


def check_manifest(manifest: Any, *, source: str) -> Dict[str, Dict[str, Any]]:
    """校验清单结构并返回 ``Name -> entry`` 索引。"""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("BundleFiles"), list):
        raise ValueError(f"{source} 不是合法的 bundleDownloadInfo（缺少 BundleFiles 数组）")
    index: Dict[str, Dict[str, Any]] = {}
    for entry in manifest["BundleFiles"]:
        name = entry.get("Name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{source} 存在缺少 Name 的条目: {entry!r}")
        index[name] = entry
    if not index:
        raise ValueError(f"{source} 的 BundleFiles 为空")
    return index


def diff_manifests(
    old: Dict[str, Dict[str, Any]],
    new: Dict[str, Dict[str, Any]],
) -> Tuple[List[str], List[str], List[str]]:
    """返回 (新增, CRC 变化, 删除) 的文件名列表。"""
    added = [name for name in new if name not in old]
    removed = [name for name in old if name not in new]
    changed = [
        name
        for name in new
        if name in old and str(new[name].get("Crc", "")).lower() != str(old[name].get("Crc", "")).lower()
    ]
    return added, changed, removed


# --------------------------------------------------------------------------- #
# 版本文件
# --------------------------------------------------------------------------- #

def read_versions_file(path: Path) -> Dict[str, str]:
    path = Path(path)
    result: Dict[str, str] = {}
    if not path.is_file():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        result[key.strip()] = value.strip()
    return result


def write_versions_file(path: Path, versions: Dict[str, str]) -> Path:
    path = Path(path)
    lines = [f"{key}: {versions[key]}" for key in CURRENT_KEYS if versions.get(key)]
    for key, value in versions.items():
        if key not in CURRENT_KEYS and value:
            lines.append(f"{key}: {value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def versions_changed(old: Dict[str, str], new: Dict[str, str]) -> bool:
    """只比较 ResourceVersion 与 TableVersion。"""
    return any(
        (old.get(key) or "").strip() != (new.get(key) or "").strip()
        for key in ("ResourceVersion", "TableVersion")
    )


def version_date(version: str) -> Optional[date]:
    """从 ``202610131404`` 这样的版本号里解析出日期。"""
    digits = "".join(ch for ch in version or "" if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        return date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None


def release_date(versions: Dict[str, str], override: Optional[str] = None) -> str:
    """Release 名称中的 ``yymmdd``。

    优先使用显式覆盖值，其次取 ResourceVersion / TableVersion 中较新的日期，
    兜底使用当前 UTC+8 日期。这样重跑同一版本会得到同一个文件名（便于幂等重试）。
    """
    if override:
        value = "".join(ch for ch in override if ch.isdigit())
        if len(value) == 6:
            return value
        if len(value) == 8:
            return value[2:]
        raise ValueError(f"--date 需要是 yymmdd 或 yyyymmdd: {override}")

    candidates = [
        version_date(versions.get(key, ""))
        for key in ("ResourceVersion", "TableVersion")
    ]
    dates = [item for item in candidates if item is not None]
    if dates:
        return max(dates).strftime("%y%m%d")
    return datetime.now(timezone(timedelta(hours=8))).strftime("%y%m%d")


# --------------------------------------------------------------------------- #
# GitHub Actions
# --------------------------------------------------------------------------- #

def set_outputs(**values: Any) -> None:
    """写入 ``$GITHUB_OUTPUT``；本地运行时打印出来。"""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        for key, value in values.items():
            log(f"[output] {key}={value}")
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            text = str(value)
            if "\n" in text:
                delimiter = "DSH_EOF_" + hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
                handle.write(f"{key}<<{delimiter}\n{text}\n{delimiter}\n")
            else:
                handle.write(f"{key}={text}\n")


def append_summary(markdown: str) -> None:
    """写入 ``$GITHUB_STEP_SUMMARY``。"""
    text = markdown.rstrip() + "\n"
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        log("\n[step-summary]\n" + text)
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text)


# --------------------------------------------------------------------------- #
# 目录工具
# --------------------------------------------------------------------------- #

def clean_dir(path: Path) -> None:
    path = Path(path)
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def iter_files(root: Path) -> Iterator[Path]:
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            yield path


def dir_size(path: Path) -> int:
    return sum(item.stat().st_size for item in iter_files(path))
