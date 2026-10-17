"""TableBundles 阶段：取 TableManifest → 下载官方 ExcelDB → 用旧库还原 → 输出补丁文件。

输出：

* ``out/TableBundles/<TABLE_BUNDLE_ID>_<官方 ExcelDB 的 MD5>``：本次发布的补丁库；
* ``work/restore/TableBundles/<同名文件>``：官方原版，用于 Restore 包；
* ``work/report/table.json``：供 Release 说明使用的统计信息。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import common as C
import update_exceldb as UED

EXCELDB_NAME = "ExcelDB.db"
SQLITE_MAGIC = b"SQLite format 3\x00"


@dataclass
class TableResult:
    table_version: str
    table_manifest_path: Path
    official_crc: str = ""
    official_size: int = 0
    pristine_md5: str = ""
    patched_md5: str = ""
    patched_size: int = 0
    output_name: str = ""
    output_path: Optional[Path] = None
    rows_updated: int = 0
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    duration: float = 0.0
    excel_entries: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "table_version": self.table_version,
            "table_manifest_path": str(self.table_manifest_path),
            "official_crc": self.official_crc,
            "official_size": self.official_size,
            "pristine_md5": self.pristine_md5,
            "patched_md5": self.patched_md5,
            "patched_size": self.patched_size,
            "output_name": self.output_name,
            "rows_updated": self.rows_updated,
            "skipped": self.skipped,
            "duration": round(self.duration, 1),
            "excel_entries": self.excel_entries,
        }


def _assert_sqlite(path: Path, label: str) -> None:
    with open(path, "rb") as handle:
        header = handle.read(len(SQLITE_MAGIC))
    if header != SQLITE_MAGIC:
        raise RuntimeError(f"{label} 不是合法的 SQLite 数据库: {path}")


def fetch_table_manifest(cfg: C.Config, table_version: str) -> Dict[str, Any]:
    """下载 TableManifest（官方无后缀，本地存成 .json）。"""
    cache = cfg.downloads_dir / f"TableManifest_{table_version}.json"
    if cache.is_file():
        try:
            data = C.read_json(cache)
            C.log(f"[复用] TableManifest {table_version}: {cache.name}")
            return data
        except Exception as exc:  # noqa: BLE001 - 缓存损坏则重新下载
            C.warn(f"缓存的 TableManifest 不可用({exc})，重新下载")
            cache.unlink(missing_ok=True)

    url = cfg.table_manifest_template.format(version=table_version)
    C.log(f"[下载] TableManifest {table_version}: {url}")
    raw = C.fetch_bytes(url, timeout=(15, 300))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(raw)
    C.log(f"[完成] TableManifest: {C.human_size(len(raw))} -> {cache}")

    try:
        return C.read_json(cache)
    except ValueError as exc:
        raise RuntimeError(f"TableManifest 不是合法 JSON: {exc}") from exc


def _excel_entry(manifest: Dict[str, Any]) -> Dict[str, Any]:
    table = manifest.get("Table")
    if not isinstance(table, dict):
        raise RuntimeError("TableManifest 缺少 Table 字段")
    entry = table.get(EXCELDB_NAME)
    if not isinstance(entry, dict) or not entry.get("Crc"):
        raise RuntimeError(
            f"TableManifest 中找不到 {EXCELDB_NAME} 的 Crc（现有条目: {', '.join(sorted(table))[:200]}）"
        )
    return entry


def run(cfg: C.Config, versions: Dict[str, str], *, analyze: bool = True) -> Optional[TableResult]:
    table_version = versions.get("TableVersion", "")
    if not table_version:
        C.warn("没有 TableVersion，跳过 TableBundles 阶段")
        return None

    started = time.time()
    manifest = fetch_table_manifest(cfg, table_version)
    entry = _excel_entry(manifest)
    crc = str(entry["Crc"]).lower()
    size = int(entry.get("Size") or 0)

    result = TableResult(
        table_version=table_version,
        table_manifest_path=cfg.downloads_dir / f"TableManifest_{table_version}.json",
        official_crc=crc,
        official_size=size,
        excel_entries=len(manifest.get("Table") or {}),
    )
    C.log(f"[信息] {EXCELDB_NAME}: md5={crc}, size={C.human_size(size)}")

    # 官方最新库
    pool_url = cfg.table_pool_template.format(prefix=crc[:2], crc=crc)
    new_db = C.download(
        pool_url,
        cfg.downloads_dir / "ExcelDB_new.db",
        md5=crc,
        size=size,
        desc=f"{EXCELDB_NAME} (官方 {table_version})",
    )
    _assert_sqlite(new_db, EXCELDB_NAME)
    result.pristine_md5 = C.md5_file(new_db)

    # 固定的旧库（已汉化）
    old_db = C.download(
        cfg.old_exceldb_url,
        cfg.downloads_dir / "ExcelDB_old.db",
        desc="ExcelDB_old.db (固定旧库)",
    )
    _assert_sqlite(old_db, "ExcelDB_old.db")
    C.log(f"[信息] 旧库: {C.human_size(old_db.stat().st_size)}, md5={C.md5_file(old_db)}")

    # 还原
    patched_db = cfg.downloads_dir / "ExcelDB_patched.db"
    patched_db.unlink(missing_ok=True)
    stats = UED.apply_patch(old_db, new_db, patched_db, analyze=analyze, verify=True)
    result.rows_updated = int(stats.get("updated") or 0)
    result.skipped = list(stats.get("skipped") or [])

    result.patched_md5 = C.md5_file(patched_db)
    result.patched_size = patched_db.stat().st_size

    if result.patched_md5 == result.pristine_md5:
        C.warn(
            "还原后的 ExcelDB 与官方完全一致——请检查旧库是否仍包含需要还原的内容，"
            "或 CONFIG 中的 ID 是否已失效"
        )
    else:
        C.log(
            f"[信息] 补丁后: {C.human_size(result.patched_size)}, md5={result.patched_md5}, "
            f"相对官方 {result.patched_size - result.official_size:+d} 字节"
        )

    # 输出
    result.output_name = f"{cfg.table_bundle_id}_{result.pristine_md5}"
    cfg.out_tables.mkdir(parents=True, exist_ok=True)
    result.output_path = cfg.out_tables / result.output_name
    result.output_path.unlink(missing_ok=True)
    patched_db.replace(result.output_path)

    cfg.restore_tables.mkdir(parents=True, exist_ok=True)
    C.copy_with_digest(new_db, cfg.restore_tables / result.output_name)

    result.duration = time.time() - started
    C.log(
        f"[完成] TableBundles: {result.output_name} "
        f"({C.human_size(result.patched_size)}, 覆盖 {result.rows_updated} 行, "
        f"跳过 {len(result.skipped)} 个 ID, {result.duration:.1f}s)"
    )

    C.write_json(cfg.report_dir / "table.json", result.as_dict())
    return result
