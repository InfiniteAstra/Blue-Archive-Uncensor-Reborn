"""AssetBundle 阶段：比对清单 → 下载需要的 bundle → 打贴图补丁 / 套用 exclusions。

需要处理的文件来自两处：

1. **官方清单差异**：新官方清单里新增、或 CRC（实际是 MD5）发生变化的文件；
2. **上一版继承**：仓库里 ``bundleDownloadInfo_Modified.json`` 中已经被改过的文件。
   继承下来重新生成，保证每个 Release 都是当前版本的**完整**补丁集，
   同时让镜像站上的修改清单始终覆盖所有已修改文件。

输出：

* ``out/AssetBundles/*.bundle``：本次发布需要提供的文件；
* ``work/restore/AssetBundles/*.bundle``：上面每个文件对应的官方原版，用于 Restore 包；
* ``work/report/assets.json``：供 Release 说明使用的统计信息。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

import common as C
import replace_texture as RT


@dataclass
class ShippedBundle:
    name: str
    kind: str  # "patched" | "exclusion"
    md5: str
    size: int
    official_crc: str
    official_size: int
    upstream_crc_old: str
    upstream_changed: bool
    carried: bool = False
    textures: int = 0
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "md5": self.md5,
            "size": self.size,
            "official_crc": self.official_crc,
            "official_size": self.official_size,
            "upstream_crc_old": self.upstream_crc_old,
            "upstream_changed": self.upstream_changed,
            "carried": self.carried,
            "textures": self.textures,
            "note": self.note,
        }


@dataclass
class AssetsResult:
    resource_version: str
    official_entries: int = 0
    baseline_source: str = ""
    baseline_version: str = ""
    modified_source: str = ""
    official_added: List[str] = field(default_factory=list)
    official_changed: List[str] = field(default_factory=list)
    official_removed: List[str] = field(default_factory=list)
    carried: List[str] = field(default_factory=list)
    targeted: List[str] = field(default_factory=list)
    shipped: List[ShippedBundle] = field(default_factory=list)
    not_patchable: List[str] = field(default_factory=list)
    unpatchable_carried: List[str] = field(default_factory=list)
    dropped: List[str] = field(default_factory=list)
    new_exclusions: List[str] = field(default_factory=list)
    missing_exclusions: List[str] = field(default_factory=list)
    exclusion_upstream_changed: List[str] = field(default_factory=list)
    exclusion_updated: List[str] = field(default_factory=list)
    repatch_changed: List[str] = field(default_factory=list)

    @property
    def total_size(self) -> int:
        return sum(item.size for item in self.shipped)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "resource_version": self.resource_version,
            "official_entries": self.official_entries,
            "baseline_source": self.baseline_source,
            "baseline_version": self.baseline_version,
            "modified_source": self.modified_source,
            "official_added": len(self.official_added),
            "official_changed": len(self.official_changed),
            "official_removed": len(self.official_removed),
            "carried": len(self.carried),
            "targeted": len(self.targeted),
            "shipped": [item.as_dict() for item in self.shipped],
            "total_size": self.total_size,
            "not_patchable": self.not_patchable,
            "unpatchable_carried": self.unpatchable_carried,
            "dropped": self.dropped,
            "new_exclusions": self.new_exclusions,
            "missing_exclusions": self.missing_exclusions,
            "exclusion_upstream_changed": self.exclusion_upstream_changed,
            "exclusion_updated": self.exclusion_updated,
            "repatch_changed": self.repatch_changed,
        }


# --------------------------------------------------------------------------- #
# 清单获取
# --------------------------------------------------------------------------- #

def fetch_manifest(url: str, cache_path: Path, label: str) -> Tuple[bytes, Dict[str, Dict[str, Any]]]:
    """下载清单（带本地缓存）并返回原始字节与索引。"""
    cache_path = Path(cache_path)
    if cache_path.is_file():
        raw = cache_path.read_bytes()
        try:
            return raw, C.check_manifest(C.read_json(cache_path), source=f"{label}(缓存)")
        except Exception as exc:  # noqa: BLE001 - 缓存损坏则重新下载
            C.warn(f"缓存清单不可用({exc})，重新下载: {cache_path}")
            cache_path.unlink(missing_ok=True)

    C.log(f"[下载] {label}: {url}")
    raw = C.fetch_bytes(url, timeout=(15, 300))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(raw)
    manifest = C.check_manifest(C.read_json(cache_path), source=label)
    C.log(f"[完成] {label}: {C.human_size(len(raw))}, {len(manifest)} 个文件")
    return raw, manifest


def _read_manifest_state(cfg: C.Config) -> Dict[str, Any]:
    if not cfg.manifest_state.is_file():
        return {}
    try:
        return C.read_json(cfg.manifest_state)
    except Exception as exc:  # noqa: BLE001 - 状态文件损坏不致命
        C.warn(f"读取 {cfg.manifest_state} 失败: {exc}")
        return {}


def load_baseline(
    cfg: C.Config,
    old_versions: Dict[str, str],
    override: Optional[Path] = None,
) -> Tuple[Dict[str, Dict[str, Any]], str, str]:
    """加载“旧官方清单”，返回 (索引, 来源说明, 对应版本)。"""
    old_resource_version = (old_versions.get("ResourceVersion") or "").strip()

    if override:
        index = C.check_manifest(C.read_json(override), source=str(override))
        C.log(f"[基线] 使用指定清单 {override} ({len(index)} 个文件)")
        return index, f"指定文件 {Path(override).name}", old_resource_version

    state = _read_manifest_state(cfg)
    state_version = str(state.get("resourceVersion") or "").strip()
    repo_usable = bool(old_resource_version) and state_version == old_resource_version and cfg.official_manifest.is_file()

    if repo_usable:
        index = C.check_manifest(C.read_json(cfg.official_manifest), source=cfg.official_manifest.name)
        C.log(f"[基线] 使用仓库清单 {cfg.official_manifest.name} ({len(index)} 个文件, 对应 {state_version})")
        return index, "仓库 manifest/bundleDownloadInfo_Official.json", old_resource_version

    if cfg.official_manifest.is_file() and state_version and state_version != old_resource_version:
        C.warn(
            f"仓库清单对应版本 {state_version} 与 current.txt 的 {old_resource_version} 不一致，"
            "将从 CDN 拉取对应版本的官方清单作为基线"
        )

    if old_resource_version:
        url = cfg.catalog_template.format(version=old_resource_version)
        cache = cfg.downloads_dir / f"bundleDownloadInfo_{old_resource_version}.json"
        try:
            _, index = fetch_manifest(url, cache, f"旧版官方清单 {old_resource_version}")
            return index, f"CDN({old_resource_version})", old_resource_version
        except Exception as exc:  # noqa: BLE001 - 回退到仓库里的清单
            C.warn(f"无法从 CDN 获取旧版清单: {exc}")
            if cfg.official_manifest.is_file():
                index = C.check_manifest(C.read_json(cfg.official_manifest), source=cfg.official_manifest.name)
                C.warn(f"回退使用仓库清单 {cfg.official_manifest.name} 作为基线（版本未知）")
                return index, "仓库清单(版本未知)", old_resource_version
            raise

    if cfg.official_manifest.is_file():
        index = C.check_manifest(C.read_json(cfg.official_manifest), source=cfg.official_manifest.name)
        C.warn("current.txt 中没有 ResourceVersion，使用仓库清单作为基线")
        return index, "仓库清单(版本未知)", ""

    raise RuntimeError(
        "无法确定旧版官方清单：current.txt 缺失或未包含 ResourceVersion，"
        "且仓库中没有 manifest/bundleDownloadInfo_Official.json"
    )


def load_previous_modified(
    cfg: C.Config,
    old_versions: Dict[str, str],
) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """加载“上一版修改清单”，用于继承已经打过补丁的文件。"""
    old_resource_version = (old_versions.get("ResourceVersion") or "").strip()
    state = _read_manifest_state(cfg)
    state_version = str(state.get("resourceVersion") or "").strip()

    if cfg.modified_manifest.is_file() and old_resource_version and state_version == old_resource_version:
        index = C.check_manifest(C.read_json(cfg.modified_manifest), source=cfg.modified_manifest.name)
        C.log(f"[基线] 使用仓库清单 {cfg.modified_manifest.name} ({len(index)} 个文件, 对应 {state_version})")
        return index, "仓库 manifest/bundleDownloadInfo_Modified.json"

    # 仓库里没有可用的上一版清单时，尝试从镜像站取（镜像发布的正是我们的 Modified 清单）
    if old_resource_version:
        url = cfg.mirror_catalog_template.format(version=old_resource_version)
        cache = cfg.downloads_dir / f"mirror_bundleDownloadInfo_{old_resource_version}.json"
        try:
            _, index = fetch_manifest(url, cache, f"上一版修改清单 {old_resource_version}")
            return index, f"镜像({old_resource_version})"
        except Exception as exc:  # noqa: BLE001 - 拿不到就退化为只处理官方差异
            C.warn(f"无法获取上一版修改清单: {exc}")

    if cfg.modified_manifest.is_file():
        index = C.check_manifest(C.read_json(cfg.modified_manifest), source=cfg.modified_manifest.name)
        C.warn(f"回退使用仓库清单 {cfg.modified_manifest.name} 作为上一版基线（版本未知）")
        return index, "仓库清单(版本未知)"

    C.warn("没有上一版修改清单，本次只处理官方差异，已继承的补丁无法延续")
    return {}, ""


# --------------------------------------------------------------------------- #
# exclusions
# --------------------------------------------------------------------------- #

def load_exclusions(cfg: C.Config) -> Dict[str, Path]:
    directory = cfg.exclusions_dir
    if not directory.is_dir():
        C.warn(f"排除列表目录不存在: {directory}")
        return {}

    exclusions: Dict[str, Path] = {}
    for item in sorted(directory.iterdir()):
        if item.is_dir():
            C.warn(f"忽略 exclusions 中的子目录: {item.name}")
            continue
        if item.suffix.lower() != ".bundle":
            C.warn(f"忽略 exclusions 中的非 bundle 文件: {item.name}")
            continue
        exclusions[item.name] = item
    C.log(f"[排除列表] {len(exclusions)} 个 bundle: {directory}")
    return exclusions


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run(
    cfg: C.Config,
    versions: Dict[str, str],
    old_versions: Dict[str, str],
    *,
    baseline_manifest: Optional[Path] = None,
) -> AssetsResult:
    resource_version = versions["ResourceVersion"]
    result = AssetsResult(resource_version=resource_version)

    # 1. 新官方清单
    catalog_url = cfg.catalog_template.format(version=resource_version)
    _, new_index = fetch_manifest(
        catalog_url,
        cfg.downloads_dir / f"bundleDownloadInfo_{resource_version}.json",
        f"官方清单 {resource_version}",
    )
    result.official_entries = len(new_index)

    # 2. 两份基线
    old_official, source, base_version = load_baseline(cfg, old_versions, baseline_manifest)
    result.baseline_source = source
    result.baseline_version = base_version
    old_modified, modified_source = load_previous_modified(cfg, old_versions)
    result.modified_source = modified_source

    # 3. 官方差异
    added, changed, removed = C.diff_manifests(old_official, new_index)
    result.official_added, result.official_changed, result.official_removed = added, changed, removed
    upstream_diff = list(dict.fromkeys(added + changed))
    upstream_set: Set[str] = set(upstream_diff)
    C.log(
        f"[差异] 官方新增 {len(added)}, CRC 变化 {len(changed)}, 删除 {len(removed)} (对比 {source})"
    )

    # 4. exclusions（先加载，后面判断“上一版是否已修改”时要用）
    exclusions = load_exclusions(cfg)
    result.missing_exclusions = [name for name in exclusions if name not in new_index]
    if result.missing_exclusions:
        C.warn(
            f"exclusions 中有 {len(result.missing_exclusions)} 个 bundle 在新官方清单中不存在"
            "（上游可能改名/删除）:"
        )
        for name in result.missing_exclusions:
            C.log(f"    - {name}")

    # 5. 上一版已修改、需要继承的文件
    carried: List[str] = []
    for name, entry in old_modified.items():
        previous_crc = str(entry.get("Crc", "")).lower()
        if name not in new_index:
            # 只关心“上一版确实改过”的文件，官方单纯下线的普通文件不在这里刷屏
            upstream_crc = str((old_official.get(name) or {}).get("Crc", "")).lower()
            if name in exclusions or previous_crc != upstream_crc:
                result.dropped.append(name)
            continue
        current_crc = str(new_index[name].get("Crc", "")).lower()
        if previous_crc == current_crc or name in upstream_set:
            continue
        carried.append(name)
    result.carried = sorted(carried)
    carried_set = set(result.carried)
    C.log(f"[继承] 上一版已修改、本次继续保留 {len(result.carried)} 个")
    if result.dropped:
        C.warn(f"上一版修改清单中有 {len(result.dropped)} 个已修改文件在新官方清单中已不存在")

    # exclusions 是“永久补丁”，只要官方清单里还有同名文件就始终打包
    exclusion_targets = [name for name in exclusions if name in new_index]
    result.new_exclusions = [name for name in exclusion_targets if name not in old_modified]
    if result.new_exclusions:
        C.log(f"[排除列表] 新增 {len(result.new_exclusions)} 个需要打包的 exclusion")

    # 6. 需要下载的文件
    replacement_sets = RT.discover_replacement_sets(cfg.replacement_dir)
    candidates = sorted(set(upstream_diff) | carried_set | set(exclusion_targets))

    targets: List[str] = []
    for name in candidates:
        if cfg.all_diffs or name in exclusions or RT.match_replacement_set(name, replacement_sets):
            targets.append(name)

    target_set = set(targets)
    result.targeted = targets
    result.unpatchable_carried = sorted(carried_set - target_set)
    if result.unpatchable_carried:
        C.warn(
            f"有 {len(result.unpatchable_carried)} 个上一版补丁找不到对应贴图目录，无法继续生成: "
            + ", ".join(result.unpatchable_carried[:5])
        )

    skipped = sum(1 for name in upstream_diff if name not in target_set)
    C.log(
        f"[目标] 候选 {len(candidates)} 个（官方差异 {len(upstream_diff)} + 继承 {len(result.carried)}），"
        f"需要处理 {len(targets)} 个"
        + (f"，跳过 {skipped} 个与补丁无关的官方差异文件（--all-diffs 可全部下载）" if skipped else "")
    )

    # 7. 下载
    with C.group(f"下载 {len(targets)} 个 bundle"):
        for name in targets:
            entry = new_index[name]
            url = f"{cfg.asset_bundle_base}/{quote(name)}"
            C.download(
                url,
                cfg.bundles_dir / name,
                md5=entry.get("Crc"),
                size=entry.get("Size"),
                desc=f"bundle {name}",
            )

    # 8. 处理
    out_dir = cfg.out_bundles
    restore_dir = cfg.restore_bundles
    out_dir.mkdir(parents=True, exist_ok=True)
    restore_dir.mkdir(parents=True, exist_ok=True)

    for name in targets:
        entry = new_index[name]
        old_entry = old_official.get(name) or {}
        previous_entry = old_modified.get(name) or {}
        source_bundle = cfg.bundles_dir / name
        upstream_changed = str(old_entry.get("Crc", "")).lower() != str(entry.get("Crc", "")).lower()
        previous_crc = str(previous_entry.get("Crc", "")).lower()
        is_carried = name in carried_set

        if name in exclusions:
            local = exclusions[name]
            md5, size = C.copy_with_digest(local, out_dir / name)
            item = ShippedBundle(
                name=name,
                kind="exclusion",
                md5=md5,
                size=size,
                official_crc=str(entry.get("Crc", "")).lower(),
                official_size=int(entry.get("Size") or 0),
                upstream_crc_old=str(old_entry.get("Crc", "")).lower(),
                upstream_changed=upstream_changed,
                carried=is_carried,
                note="来自 assets/exclusions",
            )
            result.shipped.append(item)
            if upstream_changed:
                result.exclusion_upstream_changed.append(name)
                C.warn(f"[排除列表] {name} 上游资源有变化，可能需要手动重新修改该 bundle")
            elif previous_crc and previous_crc != md5:
                result.exclusion_updated.append(name)
                C.log(f"[exclusion] {name}: 本地文件与上一版不同（已手动更新？）")
            C.log(f"[exclusion] {name}: 直接使用本地文件 ({C.human_size(size)}, md5={md5})")
        else:
            prefix = RT.match_replacement_set(name, replacement_sets)
            if prefix is None:
                result.not_patchable.append(name)
                continue
            C.log(f"[patch] {name} <- {prefix}" + (" (继承)" if is_carried else ""))
            patch = RT.patch_bundle(
                source_bundle,
                replacement_sets[prefix],
                out_dir / name,
                verify=cfg.verify,
            )
            if not patch.changed:
                result.not_patchable.append(name)
                C.log(f"[patch] {name}: 没有可替换贴图，跳过打包")
                continue
            if not upstream_changed and previous_crc and previous_crc != patch.md5:
                result.repatch_changed.append(name)
                C.log(
                    f"[patch] {name}: 上游未变但补丁字节变化 "
                    f"({previous_crc[:8]} -> {patch.md5[:8]})，替换贴图可能已更新"
                )
            item = ShippedBundle(
                name=name,
                kind="patched",
                md5=patch.md5,
                size=patch.size_after,
                official_crc=str(entry.get("Crc", "")).lower(),
                official_size=int(entry.get("Size") or 0),
                upstream_crc_old=str(old_entry.get("Crc", "")).lower(),
                upstream_changed=upstream_changed,
                carried=is_carried,
                textures=len(patch.applied),
                note=patch.summary(),
            )
            result.shipped.append(item)
            if patch.failed:
                C.warn(f"{name}: {len(patch.failed)} 张贴图写入失败，请检查日志")

        # Restore 包需要官方原版
        C.copy_with_digest(source_bundle, restore_dir / name)

    # 9. 输出统计
    patched = sum(1 for item in result.shipped if item.kind == "patched")
    exclusions_shipped = len(result.shipped) - patched
    C.log(
        f"\n[打包] 共 {len(result.shipped)} 个 bundle ({C.human_size(result.total_size)}): "
        f"贴图补丁 {patched} 个, exclusions {exclusions_shipped} 个"
    )
    if result.not_patchable:
        C.log(
            f"[跳过] {len(result.not_patchable)} 个候选 bundle 没有可替换贴图，未打包: "
            + ", ".join(result.not_patchable[:5])
            + (" ..." if len(result.not_patchable) > 5 else "")
        )

    C.write_json(cfg.report_dir / "assets.json", result.as_dict())
    return result
