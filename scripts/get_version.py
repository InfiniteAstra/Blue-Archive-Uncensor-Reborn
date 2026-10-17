"""获取国服当前资源版本号，并与仓库中的 ``current.txt`` 比对。

用法::

    python scripts/get_version.py                    # 打印版本号
    python scripts/get_version.py --json             # 以 JSON 输出
    python scripts/get_version.py --check            # 与 current.txt 比对
    python scripts/get_version.py --check --github-output
    python scripts/get_version.py --check --json

``--check`` 会输出 ``changed=true/false``，供 GitHub Actions 判断是否需要更新。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import common as C

VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
DIGITS_RE = re.compile(r"^\d{8,}$")


class VersionError(RuntimeError):
    """接口没有返回可用的版本号。"""


def _game_version() -> str:
    """从官网 meta 接口提取客户端版本号。"""
    payload = C.fetch_json(C.SETUP_API, timeout=(15, 60))
    candidates = []
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            candidates.extend(str(value) for value in data.values() if isinstance(value, str))
        elif isinstance(data, str):
            candidates.append(data)
        candidates.extend(str(value) for value in payload.values() if isinstance(value, str))

    for text in candidates:
        match = VERSION_RE.search(text)
        if match:
            return ".".join(match.groups())

    raw = C.fetch_bytes(C.SETUP_API, timeout=(15, 60)).decode("utf-8", "replace")
    match = VERSION_RE.search(raw)
    if not match:
        raise VersionError(f"无法从 {C.SETUP_API} 解析客户端版本号: {raw[:200]!r}")
    return ".".join(match.groups())


def _state(game_version: str) -> Dict[str, Any]:
    response = C.http_get(
        C.STATE_API,
        headers={
            "APP-VER": game_version,
            "PLATFORM-ID": "1",
            "CHANNEL-ID": "2",
        },
        timeout=(15, 60),
    )
    try:
        info = response.json()
    except ValueError as exc:
        raise VersionError(f"{C.STATE_API} 返回的不是 JSON: {response.text[:200]!r}") from exc
    if not isinstance(info, dict):
        raise VersionError(f"{C.STATE_API} 返回结构异常: {info!r}")
    return info


def fetch_versions(*, retries: int = 3, sleep: float = 2.0) -> Dict[str, str]:
    """返回 ``GameVersion`` / ``ResourceVersion`` / ``TableVersion`` / ``MediaVersion``。"""
    last_error: Optional[BaseException] = None

    for attempt in range(1, retries + 1):
        try:
            game_version = _game_version()
            info = _state(game_version)
            versions = {
                "GameVersion": game_version,
                "ResourceVersion": str(info.get("ResourceVersion") or "").strip(),
                "TableVersion": str(info.get("TableVersion") or "").strip(),
                "MediaVersion": str(info.get("MediaVersion") or "").strip(),
                "PatchVersion": str(info.get("PatchVersion") or "").strip(),
            }
            if not DIGITS_RE.match(versions["ResourceVersion"]) or not DIGITS_RE.match(versions["TableVersion"]):
                state = info.get("State")
                message = str(info.get("Msg") or "").strip()
                raise VersionError(
                    "接口未返回有效版本号 "
                    f"(State={state}, Msg={message!r}, ResourceVersion={versions['ResourceVersion']!r}, "
                    f"TableVersion={versions['TableVersion']!r})"
                )
            return versions
        except Exception as exc:  # noqa: BLE001 - 网络/接口问题都需要重试
            last_error = exc
            C.warn(f"获取版本失败 ({attempt}/{retries}): {exc}")
            if attempt < retries:
                time.sleep(sleep * attempt)

    raise VersionError(f"无法获取当前版本号: {last_error}")


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="获取蔚蓝档案国服资源版本号")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出全部版本信息")
    parser.add_argument("--check", action="store_true", help="与 current.txt 比对，输出 changed")
    parser.add_argument(
        "--current",
        default=str(C.ROOT / "current.txt"),
        help="current.txt 路径（默认仓库根目录）",
    )
    parser.add_argument("--github-output", action="store_true", help="写入 $GITHUB_OUTPUT")
    return parser.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)

    try:
        versions = fetch_versions()
    except VersionError as exc:
        C.error(str(exc))
        return 1

    current_path = Path(args.current)
    current = C.read_versions_file(current_path)
    changed = C.versions_changed(current, versions)

    if args.github_output:
        C.set_outputs(
            changed="true" if changed else "false",
            game_version=versions["GameVersion"],
            resource_version=versions["ResourceVersion"],
            table_version=versions["TableVersion"],
            media_version=versions.get("MediaVersion", ""),
            old_resource_version=current.get("ResourceVersion", ""),
            old_table_version=current.get("TableVersion", ""),
        )

    if args.json:
        C.log(json.dumps({**versions, "Changed": changed, "Current": current}, ensure_ascii=False, indent=2))
        return 0

    C.log(f"GameVersion: {versions['GameVersion']}")
    C.log(f"ResourceVersion: {versions['ResourceVersion']}")
    C.log(f"TableVersion: {versions['TableVersion']}")
    C.log(f"MediaVersion: {versions.get('MediaVersion', '')}")

    if args.check:
        if not current:
            C.log(f"{current_path} 不存在或为空，视为需要更新")
        elif changed:
            C.log(
                "版本变化: "
                f"ResourceVersion {current.get('ResourceVersion', '?')} -> {versions['ResourceVersion']}, "
                f"TableVersion {current.get('TableVersion', '?')} -> {versions['TableVersion']}"
            )
        else:
            C.log("版本一致，无需更新")

        if os.environ.get("GITHUB_STEP_SUMMARY"):
            if changed:
                C.append_summary(
                    "### 发现新版本\n\n"
                    "| 项目 | 旧 | 新 |\n| --- | --- | --- |\n"
                    f"| ResourceVersion | `{current.get('ResourceVersion', '—')}` | `{versions['ResourceVersion']}` |\n"
                    f"| TableVersion | `{current.get('TableVersion', '—')}` | `{versions['TableVersion']}` |\n"
                )
            else:
                C.append_summary(
                    "### 版本未变化\n\n"
                    f"ResourceVersion `{versions['ResourceVersion']}` · "
                    f"TableVersion `{versions['TableVersion']}`\n"
                )

    return 0


if __name__ == "__main__":
    sys.exit(main())
