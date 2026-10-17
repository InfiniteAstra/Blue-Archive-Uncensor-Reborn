"""把 ``assets/replacement/<bundle 名称前缀>/`` 下的贴图打进对应的 .bundle 文件。

与旧脚本的区别：

* 目录不再写死，按命令行 / 配置传入；
* 默认输出到指定目录（原本是就地覆盖），需要就地覆盖时显式加 ``--in-place``；
* 一次性建立 ``Texture2D`` 名称索引，避免对每张图都遍历全部 objects（旧的 O(N*M) 扫描）；
* 输出后可选重新加载校验，并记录每个 bundle 的 md5 / 大小；
* 贴图尺寸不一致只告警，必要时可用 ``--require-same-size`` 跳过。

命令行::

    python scripts/replace_texture.py --bundle a.bundle --replacement assets/replacement --output out/AssetBundles
    python scripts/replace_texture.py --bundle-dir work/downloads/bundles \
        --replacement assets/replacement --output-dir out/AssetBundles
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import UnityPy
from PIL import Image

import common as C

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

#: Unity TextureFormat.RGBA32，压缩格式无法编码时作为兜底
TEXTURE_FORMAT_RGBA32 = 4


@dataclass
class PatchResult:
    bundle: Path
    output: Optional[Path] = None
    applied: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)
    resized: List[str] = field(default_factory=list)
    duplicates: List[str] = field(default_factory=list)
    size_before: int = 0
    size_after: int = 0
    md5: str = ""
    duration: float = 0.0

    @property
    def changed(self) -> bool:
        return bool(self.applied)

    def summary(self) -> str:
        return (
            f"修改 {len(self.applied)} 张, 缺失 {len(self.missing)} 张, "
            f"失败 {len(self.failed)} 张, 尺寸变化 {len(self.resized)} 张"
        )


# --------------------------------------------------------------------------- #
# 贴图目录 <-> bundle 名称
# --------------------------------------------------------------------------- #

def discover_replacement_sets(root: Path) -> Dict[str, Path]:
    """返回 ``bundle 名称前缀 -> 图片目录``。"""
    root = Path(root)
    result: Dict[str, Path] = {}
    if not root.is_dir():
        C.warn(f"贴图目录不存在: {root}")
        return result
    for item in sorted(root.iterdir()):
        if not item.is_dir():
            continue
        if not any(child.is_file() and child.suffix.lower() in IMAGE_SUFFIXES for child in item.iterdir()):
            C.log(f"[跳过] 贴图目录为空: {item.name}")
            continue
        result[item.name] = item
    return result


def match_replacement_set(bundle_name: str, sets: Dict[str, Path]) -> Optional[str]:
    """``academy-_mxload-prefabs-2026-06-04_assets_all_1.bundle`` -> ``academy-_mxload-prefabs``。

    取最长匹配，避免 ``a-b`` 与 ``a-b-c`` 这类前缀互相干扰。
    """
    stem = Path(bundle_name).stem
    for prefix in sorted(sets, key=len, reverse=True):
        if stem == prefix or stem.startswith(prefix + "-"):
            return prefix
    return None


# --------------------------------------------------------------------------- #
# UnityPy 辅助
# --------------------------------------------------------------------------- #

def _bundle_file(env):
    bundle = getattr(env, "file", None)
    if bundle is not None and hasattr(bundle, "save"):
        return bundle
    files = [item for item in env.files.values() if hasattr(item, "save")]
    if len(files) != 1:
        raise RuntimeError(f"期望 1 个 BundleFile，实际得到 {len(files)} 个")
    return files[0]


def index_textures(env) -> Dict[str, List[object]]:
    index: Dict[str, List[object]] = {}
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        data = obj.read()
        index.setdefault(data.m_Name, []).append(data)
    return index


def _mip_count(texture: object) -> int:
    value = getattr(texture, "m_MipCount", None)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _apply_image(texture: object, image: Image.Image) -> str:
    """把图片写进 Texture2D，返回实际使用的格式说明。"""
    target_format = getattr(texture, "m_TextureFormat", None)
    mips = _mip_count(texture)
    try:
        texture.set_image(image, target_format=target_format, mipmap_count=mips)
        return f"format={target_format}"
    except Exception as exc:  # noqa: BLE001 - 压缩格式可能拒绝非对齐尺寸
        C.warn(f"    按原格式写入失败({type(exc).__name__}: {exc})，改用 RGBA32")
        texture.set_image(image, target_format=TEXTURE_FORMAT_RGBA32, mipmap_count=1)
        return f"format={TEXTURE_FORMAT_RGBA32}(RGBA32)"


def _verify_output(path: Path, expected: Dict[str, Tuple[int, int]]) -> None:
    """重新加载输出文件，确认贴图存在、尺寸正确、且能被解码。"""
    # 用内存读取，避免 UnityPy 在 Windows 上一直占用文件句柄
    env = UnityPy.load(Path(path).read_bytes())
    found: Dict[str, Any] = {}
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        data = obj.read()
        found[data.m_Name] = data

    missing = [name for name in expected if name not in found]
    if missing:
        raise RuntimeError(f"校验失败，输出中缺少贴图: {', '.join(missing[:5])}")

    problems: List[str] = []
    for name, size in expected.items():
        data = found[name]
        actual = (int(getattr(data, "m_Width", 0)), int(getattr(data, "m_Height", 0)))
        if actual != size:
            problems.append(f"{name}: 尺寸 {actual} != {size}")
            continue
        try:
            decoded = data.image
        except Exception as exc:  # noqa: BLE001 - 解码失败说明贴图数据已损坏
            problems.append(f"{name}: 解码失败 {type(exc).__name__}: {exc}")
            continue
        if decoded is None or decoded.size != size:
            problems.append(f"{name}: 解码尺寸 {getattr(decoded, 'size', None)} != {size}")

    if problems:
        raise RuntimeError("校验失败: " + "; ".join(problems[:5]))


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def patch_bundle(
    bundle_path: Path,
    texture_dir: Path,
    output_path: Optional[Path] = None,
    *,
    packer: str = "original",
    require_same_size: bool = False,
    verify: bool = True,
) -> PatchResult:
    """把 ``texture_dir`` 下的图片写进 ``bundle_path``，返回统计结果。

    没有任何贴图被替换时不会写文件（``result.output`` 为 ``None``）。
    """
    import time

    started = time.time()
    bundle_path = Path(bundle_path)
    texture_dir = Path(texture_dir)
    result = PatchResult(bundle=bundle_path, size_before=bundle_path.stat().st_size)

    if not bundle_path.is_file():
        raise FileNotFoundError(f"bundle 不存在: {bundle_path}")
    if not texture_dir.is_dir():
        raise FileNotFoundError(f"贴图目录不存在: {texture_dir}")

    # 用内存读取：UnityPy 按路径加载时会在 Windows 上一直占用文件句柄
    env = UnityPy.load(bundle_path.read_bytes())
    bundle_file = _bundle_file(env)
    textures = index_textures(env)

    applied_sizes: Dict[str, Tuple[int, int]] = {}

    for image_path in sorted(texture_dir.iterdir()):
        if not image_path.is_file() or image_path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        name = image_path.stem
        matches = textures.get(name)
        if not matches:
            result.missing.append(name)
            continue
        if len(matches) > 1:
            result.duplicates.append(name)

        texture = matches[0]
        try:
            with Image.open(image_path) as raw:
                raw.load()
                image = raw.copy()

            width, height = int(getattr(texture, "m_Width", 0)), int(getattr(texture, "m_Height", 0))
            if (image.width, image.height) != (width, height):
                result.resized.append(name)
                if require_same_size:
                    C.warn(
                        f"    [跳过] {name}: 尺寸 {image.width}x{image.height} "
                        f"与原贴图 {width}x{height} 不一致（--require-same-size）"
                    )
                    continue
                C.warn(f"    [注意] {name}: 尺寸 {image.width}x{image.height} != 原贴图 {width}x{height}，将同时修改尺寸")

            detail = _apply_image(texture, image)
            texture.save()
            applied_sizes[name] = (image.width, image.height)
            result.applied.append(name)
            C.log(f"    [OK] {name} ({image.width}x{image.height}, {detail})")
        except Exception as exc:  # noqa: BLE001 - 单张贴图失败不应中断整个 bundle
            result.failed[name] = f"{type(exc).__name__}: {exc}"
            C.warn(f"    [失败] {name}: {type(exc).__name__}: {exc}")

    if not result.applied:
        C.log(f"  [跳过] {bundle_path.name}: 没有可替换的贴图")
        result.duration = time.time() - started
        return result

    output_path = Path(output_path) if output_path else bundle_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(output_path.name + ".tmp")

    try:
        data = bundle_file.save(packer=packer)
        tmp_path.write_bytes(data)
        os.replace(tmp_path, output_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    result.output = output_path
    result.size_after = output_path.stat().st_size
    result.md5 = C.md5_file(output_path)

    if verify:
        _verify_output(output_path, applied_sizes)

    result.duration = time.time() - started
    C.log(
        f"  [完成] {bundle_path.name}: {result.summary()}, "
        f"{C.human_size(result.size_before)} -> {C.human_size(result.size_after)}, "
        f"md5={result.md5} ({result.duration:.1f}s)"
    )
    if result.missing:
        C.log(f"    [未找到] {', '.join(result.missing[:8])}" + (" ..." if len(result.missing) > 8 else ""))
    if result.duplicates:
        C.warn(f"    同名 Texture2D 出现多次，已使用第一个: {', '.join(sorted(set(result.duplicates)))}")
    return result


def patch_directory(
    bundle_dir: Path,
    replacement_root: Path,
    output_dir: Path,
    *,
    verify: bool = True,
    require_same_size: bool = False,
    in_place: bool = False,
) -> List[PatchResult]:
    """批量处理目录内所有能匹配到贴图目录的 bundle。"""
    sets = discover_replacement_sets(replacement_root)
    if not sets:
        raise RuntimeError(f"{replacement_root} 下没有可用的贴图目录")

    bundle_dir = Path(bundle_dir)
    output_dir = Path(output_dir)
    results: List[PatchResult] = []

    for bundle_path in sorted(bundle_dir.glob("*.bundle")):
        prefix = match_replacement_set(bundle_path.name, sets)
        if prefix is None:
            C.log(f"[跳过] {bundle_path.name}: 没有匹配的贴图目录")
            continue
        output_path = bundle_path if in_place else output_dir / bundle_path.name
        C.log(f"[处理] {bundle_path.name} <- {prefix}")
        results.append(
            patch_bundle(
                bundle_path,
                sets[prefix],
                output_path,
                require_same_size=require_same_size,
                verify=verify,
            )
        )
    return results


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把替换贴图写入 AssetBundle")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--bundle", help="单个 .bundle 文件")
    source.add_argument("--bundle-dir", help="包含多个 .bundle 的目录")

    parser.add_argument(
        "--replacement",
        default=str(C.ROOT / "assets" / "replacement"),
        help="贴图根目录（子目录名 = bundle 名称前缀）",
    )
    parser.add_argument(
        "--texture-dir",
        help="直接指定贴图目录（仅 --bundle 时可用，默认按前缀自动匹配）",
    )
    parser.add_argument("--output", help="单个 bundle 的输出文件")
    parser.add_argument("--output-dir", help="批量模式输出目录")
    parser.add_argument("--in-place", action="store_true", help="就地覆盖 bundle（会保留 .bak 备份）")
    parser.add_argument("--no-verify", action="store_true", help="不重新加载校验输出文件")
    parser.add_argument("--require-same-size", action="store_true", help="贴图尺寸不一致时跳过")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    replacement_root = Path(args.replacement)

    if args.bundle:
        bundle_path = Path(args.bundle)
        if args.texture_dir:
            texture_dir = Path(args.texture_dir)
        else:
            sets = discover_replacement_sets(replacement_root)
            prefix = match_replacement_set(bundle_path.name, sets)
            if prefix is None:
                C.error(f"{bundle_path.name} 没有匹配的贴图目录")
                return 2
            texture_dir = sets[prefix]

        if args.in_place:
            output_path = bundle_path
            backup = bundle_path.with_suffix(bundle_path.suffix + ".bak")
            if not backup.exists():
                shutil.copy2(bundle_path, backup)
                C.log(f"已备份: {backup.name}")
        else:
            output_path = Path(args.output) if args.output else C.ROOT / "out" / "AssetBundles" / bundle_path.name

        result = patch_bundle(
            bundle_path,
            texture_dir,
            output_path,
            require_same_size=args.require_same_size,
            verify=not args.no_verify,
        )
        return 0 if not result.failed else 1

    output_dir = Path(args.output_dir) if args.output_dir else C.ROOT / "out" / "AssetBundles"
    results = patch_directory(
        Path(args.bundle_dir),
        replacement_root,
        output_dir,
        verify=not args.no_verify,
        require_same_size=args.require_same_size,
        in_place=args.in_place,
    )
    changed = [item for item in results if item.changed]
    C.log(f"\n共处理 {len(results)} 个 bundle，其中 {len(changed)} 个被修改")
    return 1 if any(item.failed for item in results) else 0


if __name__ == "__main__":
    sys.exit(main())
