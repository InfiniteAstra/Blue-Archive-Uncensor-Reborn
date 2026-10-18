"""一键更新：版本比对 → AssetBundles → TableBundles → 清单回写 → 打包 → Release 说明。

GitHub Action 直接调用本脚本；本地调试也可以直接运行（会写到 work/ out/ dist/）。

常用参数::

    python scripts/update_all.py                      # 版本没变就直接退出
    python scripts/update_all.py --force              # 忽略版本比对
    python scripts/update_all.py --skip-table         # 只更新 AssetBundles
    python scripts/update_all.py --all-diffs          # 下载所有差异 bundle（默认只下补丁相关的）
    python scripts/update_all.py --date 261015        # 固定 Release 名称中的日期
    python scripts/update_all.py --baseline-manifest work/old.json   # 手动指定旧官方清单
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Dict, Optional, Sequence

import assets_update
import common as C
import get_version
import release
import table_update


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="蔚蓝档案国服反和谐资源自动更新")

    parser.add_argument("--root", help="仓库根目录（默认脚本上一级）")
    parser.add_argument("--work-dir", help="临时目录（默认 <root>/work）")
    parser.add_argument("--out-dir", help="发布内容目录（默认 <root>/out）")
    parser.add_argument("--manifest-dir", help="清单目录（默认 <root>/manifest）")
    parser.add_argument("--dist-dir", help="zip / 说明输出目录（默认 <root>/dist）")
    parser.add_argument("--current", help="current.txt 路径（默认 <root>/current.txt）")

    parser.add_argument("--force", action="store_true", help="版本一致时也强制更新")
    parser.add_argument("--skip-table", action="store_true", help="跳过 TableBundles 阶段")
    parser.add_argument(
        "--no-analyze",
        action="store_true",
        help="ExcelDB 还原后不执行 ANALYZE（默认执行，与历史发布保持一致的库大小）",
    )
    parser.add_argument("--all-diffs", action="store_true", help="下载全部差异 bundle（默认只下载补丁相关文件）")
    parser.add_argument("--no-verify", action="store_true", help="跳过 bundle 输出校验")
    parser.add_argument("--baseline-manifest", type=Path, help="手动指定旧官方清单文件")
    parser.add_argument("--date", help="Release 名称中的日期（yymmdd / yyyymmdd）")
    parser.add_argument("--table-bundle-id", default=C.TABLE_BUNDLE_ID, help="TableBundles 补丁文件前缀")
    parser.add_argument(
        "--compression",
        default="deflate",
        choices=sorted(release.COMPRESSION_METHODS),
        help="zip 压缩方式（默认 deflate）",
    )
    parser.add_argument("--level", type=int, default=9, help="压缩级别（默认 9）")
    return parser


def _log_versions(versions: Dict[str, str], old: Dict[str, str]) -> None:
    C.log(
        "版本信息:\n"
        f"  ResourceVersion: {old.get('ResourceVersion', '—')} -> {versions.get('ResourceVersion', '')}\n"
        f"  TableVersion   : {old.get('TableVersion', '—')} -> {versions.get('TableVersion', '')}\n"
        f"  MediaVersion   : {old.get('MediaVersion', '—')} -> {versions.get('MediaVersion', '')}\n"
        f"  GameVersion    : {versions.get('GameVersion', '—')}"
    )


def run(args: argparse.Namespace) -> int:
    cfg = C.Config.create(
        args.root,
        work_dir=args.work_dir,
        out_dir=args.out_dir,
        manifest_dir=args.manifest_dir,
        dist_dir=args.dist_dir,
        current_file=args.current,
        verify=not args.no_verify,
        all_diffs=args.all_diffs,
        table_bundle_id=args.table_bundle_id,
    )

    try:
        versions = get_version.fetch_versions()
    except get_version.VersionError as exc:
        C.error(f"获取版本号失败: {exc}")
        return 1

    old_versions = C.read_versions_file(cfg.current_file)
    _log_versions(versions, old_versions)

    changed = C.versions_changed(old_versions, versions)
    if not changed and not args.force:
        C.log("\nResourceVersion / TableVersion 均未变化，本次不需要更新。")
        C.set_outputs(
            changed="false",
            resource_version=versions.get("ResourceVersion", ""),
            table_version=versions.get("TableVersion", ""),
            release_tag="",
            has_package="false",
        )
        return 0

    if not changed:
        C.warn("版本未变化，但指定了 --force，继续执行")

    yymmdd = C.release_date(versions, args.date)
    tag = f"r{versions['ResourceVersion']}-t{versions['TableVersion']}"
    release_name = release.release_title(yymmdd)
    C.log(f"\nRelease: {release_name} (tag: {tag})")

    # 附件名必须是 ASCII：GitHub 的上传端点对非 ASCII 名字只返回 HTTP 404。
    # 这里提前校验，避免下载/打包跑了几分钟才在上传时失败。
    for kind in ("uncensor", "restore"):
        release.asset_name(kind, yymmdd)

    cfg.prepare()
    for path in (cfg.out_dir, cfg.restore_dir, cfg.dist_dir):
        C.clean_dir(path)

    with C.group("① AssetBundles"):
        assets = assets_update.run(
            cfg,
            versions,
            old_versions,
            baseline_manifest=args.baseline_manifest,
        )

    table: Optional[table_update.TableResult] = None
    if args.skip_table:
        C.warn("已指定 --skip-table，跳过 TableBundles 阶段")
    else:
        with C.group("② TableBundles"):
            table = table_update.run(cfg, versions, analyze=not args.no_analyze)

    with C.group("③ 回写清单"):
        manifests = release.patch_manifests(cfg, versions, assets, table, old_versions=old_versions)

    with C.group("④ 打包"):
        package = release.package(cfg, yymmdd, method=args.compression, level=args.level)
        notes = release.build_notes(
            versions=versions,
            old_versions=old_versions,
            assets=assets,
            table=table,
            manifests=manifests,
            package=package,
            yymmdd=yymmdd,
        )
        notes_path = release.write_notes(cfg, notes)

    # 成功后才会更新 current.txt（失败时保留旧版本号，方便重跑）
    C.write_versions_file(cfg.current_file, versions)
    C.log(f"[版本] 已更新 {cfg.current_file}: {versions['ResourceVersion']} / {versions.get('TableVersion', '')}")

    C.append_summary(notes)
    C.set_outputs(
        changed="true",
        resource_version=versions.get("ResourceVersion", ""),
        table_version=versions.get("TableVersion", ""),
        media_version=versions.get("MediaVersion", ""),
        old_resource_version=old_versions.get("ResourceVersion", ""),
        old_table_version=old_versions.get("TableVersion", ""),
        release_name=release_name,
        release_tag=tag,
        yymmdd=yymmdd,
        has_package="true" if package.has_package else "false",
        uncensor_zip=str(package.uncensor_zip) if package.uncensor_zip else "",
        restore_zip=str(package.restore_zip) if package.restore_zip else "",
        notes_file=str(notes_path),
        packaged_files=str(package.file_count),
        shipped_bundles=str(len(assets.shipped)),
        table_output=table.output_name if table else "",
    )

    C.log(
        "\n===== 完成 =====\n"
        f"  打包文件: {package.file_count} 个\n"
        f"  Uncensor: {package.uncensor_zip or '（无）'}\n"
        f"  Restore : {package.restore_zip or '（无）'}\n"
        f"  说明文档: {notes_path}\n"
        f"  清单    : {cfg.official_manifest.name} / {cfg.modified_manifest.name}"
    )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001 - CI 里需要完整堆栈
        traceback.print_exc()
        C.error(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
