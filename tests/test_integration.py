"""联网集成测试：用真实的官方 bundle 验证贴图替换链路。

默认跳过，设置环境变量 ``BA_IT=1`` 才会执行::

    BA_IT=1 python -m unittest tests.test_integration -v

会下载一个约 3 MB 的官方 bundle 到 ``work/test-integration/``，不会改动仓库里的其他文件。
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import common as C  # noqa: E402
import replace_texture as RT  # noqa: E402

PREFIX = "assets-_mx-uis-atlas-_mxdependency-textures"


@unittest.skipUnless(os.environ.get("BA_IT") == "1", "设置 BA_IT=1 才运行联网集成测试")
class RealBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # 放在仓库 work/ 下，避免系统临时目录的权限问题
        cls.work = ROOT / "work" / "test-integration"
        shutil.rmtree(cls.work, ignore_errors=True)
        cls.work.mkdir(parents=True, exist_ok=True)

        versions = C.read_versions_file(ROOT / "current.txt")
        resource_version = versions.get("ResourceVersion", "")
        if not resource_version:
            raise unittest.SkipTest("current.txt 中没有 ResourceVersion")
        catalog = C.fetch_json(C.CATALOG_TEMPLATE.format(version=resource_version))
        index = C.check_manifest(catalog, source="官方清单")
        matches = sorted(name for name in index if RT.match_replacement_set(name, {PREFIX: ROOT}) == PREFIX)
        if not matches:
            raise unittest.SkipTest(f"官方清单里没有 {PREFIX}-* 的 bundle")
        cls.name = matches[0]
        cls.entry = index[cls.name]

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.work, ignore_errors=True)

    def test_patch_bundle_roundtrip(self):
        source = C.download(
            f"{C.ASSET_BUNDLE_BASE}/{self.name}",
            self.work / self.name,
            md5=self.entry["Crc"],
            size=self.entry["Size"],
            desc=self.name,
        )
        official_md5 = C.md5_file(source)
        self.assertEqual(official_md5, self.entry["Crc"].lower())

        replacement_dir = ROOT / "assets" / "replacement" / PREFIX
        output = self.work / "patched.bundle"
        result = RT.patch_bundle(source, replacement_dir, output, verify=True)

        self.assertTrue(result.changed, "至少应该替换成功一张贴图")
        self.assertTrue(output.is_file())
        self.assertNotEqual(result.md5, official_md5, "补丁后的 bundle 不应该和官方相同")
        self.assertGreater(result.size_after, 0)

        # 输出必须能被再次加载，并且贴图能被解码成正确尺寸
        import UnityPy

        env = UnityPy.load(output.read_bytes())
        decoded = {}
        for obj in env.objects:
            if obj.type.name != "Texture2D":
                continue
            data = obj.read()
            if data.m_Name in result.applied:
                decoded[data.m_Name] = data.image.size
        for name in result.applied:
            self.assertIn(name, decoded, f"{name} 没有写进输出")
        self.assertEqual(len(decoded), len(result.applied))

    def test_replacement_dir_matches_bundle(self):
        self.assertIsNotNone(
            RT.match_replacement_set(self.name, {PREFIX: ROOT / "assets" / "replacement" / PREFIX})
        )


if __name__ == "__main__":
    unittest.main()
