"""发布阶段：回写仓库清单、打包 Release 附件、生成说明文档。"""

from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import assets_update as AU
import common as C
import table_update as TU

COMPRESSION_METHODS = {
    "deflate": zipfile.ZIP_DEFLATED,
    "lzma": zipfile.ZIP_LZMA,
    "bzip2": zipfile.ZIP_BZIP2,
    "stored": zipfile.ZIP_STORED,
}

# --------------------------------------------------------------------------- #
# Release 附件命名
#
# 注意：GitHub 的附件上传端点（uploads.github.com）**不接受非 ASCII 的附件名**，
# 即使客户端已经做了 percent-encoding，也会返回 “HTTP 404: Not Found”。
# 所以 zip 文件名一律用 ASCII，中文只放在 Release 标题 / 说明里（DISPLAY_*）。
# --------------------------------------------------------------------------- #

UNCNESOR_ZIP_TEMPLATE = "BlueArchiveCN-Uncensor-{date}.zip"
RESTORE_ZIP_TEMPLATE = "BlueArchiveCN-Restore-{date}.zip"

#: Release 标题可以随便写中文（和附件名不同，标题没有 ASCII 限制）
RELEASE_TITLE_TEMPLATE = "BlueArchiveCN-Uncensor-{date}"

UNCNESOR_DISPLAY = "反和谐包"
RESTORE_DISPLAY = "恢复包"

#: 除 ASCII 之外，这些字符在附件名 / 下载 URL 里也容易出问题
FORBIDDEN_ASSET_CHARS = set(' "#%&?/\\*:<>\'`|')

INSTALL_HINT = """\
1. 解压{uncensor_display} `{uncensor}`，把其中的 `AssetBundles/`、`TableBundles/` 合并进游戏目录（覆盖同名文件）：
   * Android：`/storage/emulated/0/Android/data/com.RoamingStar.BlueArchive/files/`
   * iOS：`/private/var/mobile/Containers/Data/Application/com.RoamingStar.BlueArchive/Documents/`
2. 本包已经包含**当前版本全部**需要替换的文件（含上一版继承下来的补丁），直接覆盖即可，不需要先装历史包。
3. 如需还原官方资源，解压{restore_display} `{restore}` 覆盖同名文件即可。
4. 附件名必须是 ASCII（GitHub 限制），中文名只用于显示。
"""


def validate_asset_name(name: str) -> None:
    """校验 Release 附件名；不合格时抛错并说明原因。"""
    trouble = [ch for ch in name if ord(ch) > 127 or ch in FORBIDDEN_ASSET_CHARS]
    if trouble:
        raise ValueError(
            f"Release 附件名必须是 ASCII、且不能包含 {''.join(sorted(FORBIDDEN_ASSET_CHARS))!r}: {name!r}\n"
            "  -> GitHub 的 uploads.github.com 对非 ASCII 附件名只会返回 HTTP 404 Not Found。\n"
            "  -> 中文名请写到 Release 标题 / 说明里（release.py 的 DISPLAY_* 常量），"
            "或者把中文名文件放到自建镜像站。"
        )


def asset_name(kind: str, yymmdd: str) -> str:
    """生成 Release 附件文件名（ASCII）。"""
    template = UNCNESOR_ZIP_TEMPLATE if kind == "uncensor" else RESTORE_ZIP_TEMPLATE
    name = template.format(date=yymmdd)
    validate_asset_name(name)
    return name


def release_title(yymmdd: str) -> str:
    """Release 标题（允许中文）。"""
    return RELEASE_TITLE_TEMPLATE.format(date=yymmdd)


@dataclass
class ManifestStats:
    official_entries: int = 0
    modified_entries: int = 0
    official_path: Optional[Path] = None
    modified_path: Optional[Path] = None


@dataclass
class PackageResult:
    uncensor_zip: Optional[Path] = None
    restore_zip: Optional[Path] = None
    file_count: int = 0
    uncensor_bytes: int = 0
    restore_bytes: int = 0

    @property
    def has_package(self) -> bool:
        return self.uncensor_zip is not None


# --------------------------------------------------------------------------- #
# 清单回写
# --------------------------------------------------------------------------- #

def patch_manifests(
    cfg: C.Config,
    versions: Dict[str, str],
    assets: AU.AssetsResult,
    table: Optional[TU.TableResult],
    *,
    old_versions: Dict[str, str],
) -> ManifestStats:
    resource_version = versions["ResourceVersion"]
    table_version = versions.get("TableVersion", "")
    stats = ManifestStats()

    raw_path = cfg.downloads_dir / f"bundleDownloadInfo_{resource_version}.json"
    if not raw_path.is_file():
        raise FileNotFoundError(f"找不到已下载的官方清单: {raw_path}")

    # 1. 官方清单：原样保存（保持官方字节，便于将来直接对比）
    cfg.manifest_dir.mkdir(parents=True, exist_ok=True)
    official_bytes = raw_path.read_bytes()
    cfg.official_manifest.write_bytes(official_bytes)
    manifest = C.read_json(raw_path)
    index = C.check_manifest(manifest, source="官方清单")
    stats.official_entries = len(index)
    stats.official_path = cfg.official_manifest
    C.log(f"[清单] {cfg.official_manifest.name}: {len(index)} 个文件, {C.human_size(len(official_bytes))}")

    # 2. 修改清单：以官方清单为底，替换本次打包文件的 Crc / Size
    for item in assets.shipped:
        entry = index.get(item.name)
        if entry is None:
            C.warn(f"跳过清单回写：{item.name} 不在官方清单中")
            continue
        entry["Crc"] = item.md5
        entry["Size"] = item.size

    C.write_json(cfg.modified_manifest, manifest)
    stats.modified_entries = len(assets.shipped)
    stats.modified_path = cfg.modified_manifest
    C.log(
        f"[清单] {cfg.modified_manifest.name}: 修改 {stats.modified_entries} 个条目, "
        f"{C.human_size(cfg.modified_manifest.stat().st_size)}"
    )

    # 3. 状态文件：记录这两份清单对应的版本
    C.write_json(
        cfg.manifest_state,
        {
            "resourceVersion": resource_version,
            "tableVersion": table_version,
            "previousResourceVersion": old_versions.get("ResourceVersion", ""),
            "previousTableVersion": old_versions.get("TableVersion", ""),
            "officialEntries": stats.official_entries,
            "modifiedEntries": stats.modified_entries,
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )
    C.log(f"[清单] {cfg.manifest_state.name}: 标记版本 {resource_version} / {table_version}")
    return stats


# --------------------------------------------------------------------------- #
# 打包
# --------------------------------------------------------------------------- #

def _zip_tree(source: Path, target: Path, *, method: str, level: int) -> int:
    compression = COMPRESSION_METHODS[method]
    files = list(C.iter_files(source))
    if not files:
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    tmp.unlink(missing_ok=True)

    options: Dict[str, Any] = {
        "compression": compression,
        "allowZip64": True,
        "strict_timestamps": False,
    }
    if compression != zipfile.ZIP_STORED:
        options["compresslevel"] = level

    C.log(
        f"[打包] {target.name}: {len(files)} 个文件, {C.human_size(C.dir_size(source))}, "
        f"压缩方式 {method}-{level}"
    )
    with zipfile.ZipFile(tmp, "w", **options) as archive:
        for order, path in enumerate(files, start=1):
            arcname = path.relative_to(source).as_posix()
            archive.write(path, arcname)
            if path.stat().st_size > 32 * 1024 * 1024:
                C.log(f"    [{order}/{len(files)}] {arcname} ({C.human_size(path.stat().st_size)})")

    os.replace(tmp, target)
    C.log(f"[打包] 完成: {C.human_size(target.stat().st_size)}")
    return len(files)


def package(
    cfg: C.Config,
    yymmdd: str,
    *,
    method: str = "deflate",
    level: int = 9,
) -> PackageResult:
    result = PackageResult()
    out_files = list(C.iter_files(cfg.out_dir))
    if not out_files:
        C.warn("out/ 为空，没有需要发布的文件")
        return result

    if method not in COMPRESSION_METHODS:
        raise ValueError(f"不支持的压缩方式: {method}")

    C.clean_dir(cfg.dist_dir)
    result.uncensor_zip = cfg.dist_dir / asset_name("uncensor", yymmdd)
    result.file_count = _zip_tree(cfg.out_dir, result.uncensor_zip, method=method, level=level)
    result.uncensor_bytes = result.uncensor_zip.stat().st_size

    restore_files = list(C.iter_files(cfg.restore_dir))
    if restore_files:
        result.restore_zip = cfg.dist_dir / asset_name("restore", yymmdd)
        _zip_tree(cfg.restore_dir, result.restore_zip, method=method, level=level)
        result.restore_bytes = result.restore_zip.stat().st_size
    else:
        C.warn("没有可用于 Restore 包的官方原版文件")

    # GitHub Release 对单个附件有 2 GiB 限制
    limit = 2 * 1024**3
    for path, label in (
        (result.uncensor_zip, UNCNESOR_DISPLAY),
        (result.restore_zip, RESTORE_DISPLAY),
    ):
        if path is not None and path.stat().st_size > limit:
            C.warn(
                f"{label} 包 {path.name} 有 {C.human_size(path.stat().st_size)}，"
                "超过 GitHub Release 单文件 2 GiB 限制，上传会失败"
            )

    C.log(
        f"[打包] Uncensor {C.human_size(result.uncensor_bytes)}"
        + (f", Restore {C.human_size(result.restore_bytes)}" if result.restore_zip else "")
    )
    return result


# --------------------------------------------------------------------------- #
# Release 说明
# --------------------------------------------------------------------------- #

def _bundle_table(items: Sequence[AU.ShippedBundle]) -> str:
    lines = [
        "| 文件 | 类型 | 来源 | 替换贴图 | 大小 | MD5 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in items:
        kind = "贴图补丁" if item.kind == "patched" else "exclusions"
        if item.upstream_changed:
            origin = "本次变化"
        elif item.carried:
            origin = "继承"
        elif item.kind == "exclusion":
            origin = "首次纳入"
        else:
            origin = "—"
        textures = str(item.textures) if item.kind == "patched" else "-"
        lines.append(
            f"| `{item.name}` | {kind} | {origin} | {textures} | {C.human_size(item.size)} | `{item.md5}` |"
        )
    return "\n".join(lines)


def build_notes(
    *,
    versions: Dict[str, str],
    old_versions: Dict[str, str],
    assets: AU.AssetsResult,
    table: Optional[TU.TableResult],
    manifests: ManifestStats,
    package: PackageResult,
    yymmdd: str,
) -> str:
    patched = [item for item in assets.shipped if item.kind == "patched"]
    excluded = [item for item in assets.shipped if item.kind == "exclusion"]

    parts: List[str] = []
    parts.append(f"# BlueArchiveCN-反和谐包-{yymmdd}")
    parts.append("")
    parts.append(
        "> 自动构建 · "
        f"ResourceVersion `{versions.get('ResourceVersion', '')}` · "
        f"TableVersion `{versions.get('TableVersion', '')}`"
    )
    parts.append("")

    parts.append("## 版本变化")
    parts.append("")
    parts.append("| 项目 | 旧 | 新 |")
    parts.append("| --- | --- | --- |")
    for key in ("ResourceVersion", "TableVersion"):
        parts.append(
            f"| {key} | `{old_versions.get(key, '') or '—'}` | `{versions.get(key, '') or '—'}` |"
        )
    parts.append("")

    parts.append("## AssetBundles")
    parts.append("")
    parts.append(
        f"- 官方清单条目：{assets.official_entries}"
        f"（官方基线：{assets.baseline_source}；上一版清单：{assets.modified_source or '无'}）"
    )
    parts.append(
        f"- 上游差异：新增 {len(assets.official_added)} · CRC 变化 {len(assets.official_changed)} · "
        f"删除 {len(assets.official_removed)}"
    )
    parts.append(
        f"- 继承上一版补丁：{len(assets.carried)} 个（上游未变化，重新生成后一并发布）"
    )
    parts.append(
        f"- 打包：{len(assets.shipped)} 个（贴图补丁 {len(patched)} · exclusions {len(excluded)}），"
        f"解压后 {C.human_size(assets.total_size)}"
    )
    if package.uncensor_zip:
        parts.append(f"- `{package.uncensor_zip.name}`：{C.human_size(package.uncensor_bytes)}")
    if package.restore_zip:
        parts.append(f"- `{package.restore_zip.name}`：{C.human_size(package.restore_bytes)}")
    parts.append("")

    if patched:
        parts.append(f"<details><summary>贴图补丁明细（{len(patched)} 个）</summary>")
        parts.append("")
        parts.append(_bundle_table(patched))
        parts.append("")
        parts.append("</details>")
        parts.append("")

    if excluded:
        parts.append(f"<details><summary>exclusions 明细（{len(excluded)} 个）</summary>")
        parts.append("")
        parts.append(_bundle_table(excluded))
        parts.append("")
        parts.append("</details>")
        parts.append("")

    warnings: List[str] = []

    if assets.exclusion_upstream_changed:
        warnings.append("### exclusions 上游资源有变化")
        warnings.append("")
        warnings.append("| bundle | 旧 MD5 | 新 MD5 |")
        warnings.append("| --- | --- | --- |")
        for item in assets.shipped:
            if item.kind == "exclusion" and item.upstream_changed:
                old_crc = item.upstream_crc_old or "（新增文件）"
                warnings.append(f"| `{item.name}` | `{old_crc}` | `{item.official_crc}` |")
        warnings.append("")
        warnings.append(
            "> 这些 bundle 仍按 `assets/exclusions/` 中的本地文件打包，"
            "但上游内容已经变化，可能需要手动重新修改后再提交，否则补丁可能不完全。"
        )
        warnings.append("")

    if assets.missing_exclusions:
        warnings.append("### exclusions 在新官方清单中不存在")
        warnings.append("")
        warnings.append("> 上游可能改名或删除了这些文件，本次没有上传；请确认后更新 `assets/exclusions/`。")
        warnings.append("")
        for name in assets.missing_exclusions:
            warnings.append(f"- `{name}`")
        warnings.append("")

    if assets.unpatchable_carried:
        warnings.append("### 上一版补丁无法继续生成")
        warnings.append("")
        warnings.append(
            "> 这些文件在上一版修改清单里，但本次找不到对应的 `assets/replacement/` 贴图目录，"
            "将不会包含在本包与新的 Modified 清单中。"
        )
        warnings.append("")
        for name in assets.unpatchable_carried:
            warnings.append(f"- `{name}`")
        warnings.append("")

    if assets.dropped:
        warnings.append("<details><summary>上一版修改清单中已消失的文件</summary>")
        warnings.append("")
        warnings.append(
            "> 官方新清单中已不存在这些文件（上游改名或删除），本次不再提供。"
        )
        warnings.append("")
        for name in assets.dropped[:50]:
            warnings.append(f"- `{name}`")
        if len(assets.dropped) > 50:
            warnings.append(f"- ……其余 {len(assets.dropped) - 50} 个省略")
        warnings.append("")
        warnings.append("</details>")
        warnings.append("")

    if assets.repatch_changed or assets.exclusion_updated:
        warnings.append("<details><summary>本地补丁内容有变化</summary>")
        warnings.append("")
        warnings.append(
            "> 上游文件没有变化，但重新生成的补丁与上一版不同：通常是 `assets/replacement/` "
            "里的贴图更新了（exclusions 则是本地 bundle 被手动替换）。"
        )
        warnings.append("")
        for name in assets.repatch_changed:
            warnings.append(f"- 贴图补丁 `{name}`")
        for name in assets.exclusion_updated:
            warnings.append(f"- exclusions `{name}`")
        warnings.append("")
        warnings.append("</details>")
        warnings.append("")

    if assets.not_patchable:
        warnings.append("<details><summary>有差异但无需打包的 bundle</summary>")
        warnings.append("")
        warnings.append(f"> 共 {len(assets.not_patchable)} 个候选 bundle 中没有可替换贴图，按规则不打包。")
        warnings.append("")
        for name in assets.not_patchable[:50]:
            warnings.append(f"- `{name}`")
        if len(assets.not_patchable) > 50:
            warnings.append(f"- ……其余 {len(assets.not_patchable) - 50} 个省略")
        warnings.append("")
        warnings.append("</details>")
        warnings.append("")

    if warnings:
        parts.append("## ⚠️ 需要手动关注")
        parts.append("")
        parts.extend(warnings)

    if table is not None:
        parts.append("## TableBundles")
        parts.append("")
        parts.append(f"- ExcelDB.db 官方：`{table.official_crc}`（{C.human_size(table.official_size)}）")
        parts.append(f"- 还原行数：{table.rows_updated}，跳过 ID：{len(table.skipped)}")
        parts.append(
            f"- 输出：`TableBundles/{table.output_name}`"
            f"（{C.human_size(table.patched_size)}，md5 `{table.patched_md5}`）"
        )
        if table.skipped:
            detail = "、".join(f"{item['id']}({item['reason']})" for item in table.skipped[:10])
            parts.append(f"- 跳过原因（前 10 个）：{detail}")
        parts.append("")

    parts.append("## 安装说明")
    parts.append("")
    uncensor_name = package.uncensor_zip.name if package.uncensor_zip else UNCNESOR_ZIP_TEMPLATE.format(date="*")
    restore_name = package.restore_zip.name if package.restore_zip else RESTORE_ZIP_TEMPLATE.format(date="*")
    parts.append(
        INSTALL_HINT.format(
            uncensor=uncensor_name,
            restore=restore_name,
            uncensor_display=UNCNESOR_DISPLAY,
            restore_display=RESTORE_DISPLAY,
        ).rstrip()
    )
    parts.append("")

    parts.append(
        f"> 清单条目：Modified {manifests.modified_entries} 个修改 / 官方 {manifests.official_entries} 个总条目"
    )
    parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def write_notes(cfg: C.Config, notes: str) -> Path:
    path = cfg.dist_dir / "release-notes.md"
    cfg.dist_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(notes, encoding="utf-8")
    C.log(f"[说明] 已写入 {path}")
    return path
