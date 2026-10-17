"""离线单元测试（不访问网络、不使用真实 bundle）。

运行::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import assets_update as AU  # noqa: E402
import common as C  # noqa: E402
import release  # noqa: E402
import replace_texture as RT  # noqa: E402
import table_update as TU  # noqa: E402
import update_exceldb as UED  # noqa: E402


def _manifest(*entries):
    return {"BundleFiles": list(entries)}


def _entry(name, crc="a" * 32, size=100):
    return {"Name": name, "Size": size, "Crc": crc, "IsPrologue": False, "IsSplitDownload": False}


class ManifestTests(unittest.TestCase):
    def test_check_manifest_rejects_bad_shapes(self):
        with self.assertRaises(ValueError):
            C.check_manifest({}, source="x")
        with self.assertRaises(ValueError):
            C.check_manifest({"BundleFiles": []}, source="x")
        with self.assertRaises(ValueError):
            C.check_manifest({"BundleFiles": [{"Size": 1}]}, source="x")

    def test_diff_manifests(self):
        old = {item["Name"]: item for item in _manifest(
            _entry("same.bundle", "a" * 32),
            _entry("changed.bundle", "b" * 32),
            _entry("removed.bundle", "c" * 32),
        )["BundleFiles"]}
        new = {item["Name"]: item for item in _manifest(
            _entry("same.bundle", "a" * 32),
            _entry("changed.bundle", "d" * 32),
            _entry("added.bundle", "e" * 32),
        )["BundleFiles"]}

        added, changed, removed = C.diff_manifests(old, new)
        self.assertEqual(added, ["added.bundle"])
        self.assertEqual(changed, ["changed.bundle"])
        self.assertEqual(removed, ["removed.bundle"])

    def test_diff_is_case_insensitive(self):
        old = {"x.bundle": _entry("x.bundle", "ABCDEF" + "0" * 26)}
        new = {"x.bundle": _entry("x.bundle", "abcdef" + "0" * 26)}
        self.assertEqual(C.diff_manifests(old, new), ([], [], []))


class VersionTests(unittest.TestCase):
    def test_versions_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "current.txt"
            C.write_versions_file(path, {
                "ResourceVersion": "202610131404",
                "TableVersion": "202610151554",
                "MediaVersion": "202609171806",
            })
            data = C.read_versions_file(path)
            self.assertEqual(data["ResourceVersion"], "202610131404")
            self.assertEqual(data["TableVersion"], "202610151554")
            self.assertIn("MediaVersion: 202609171806", path.read_text(encoding="utf-8"))

    def test_versions_changed_only_checks_two_fields(self):
        base = {"ResourceVersion": "1", "TableVersion": "2", "MediaVersion": "3"}
        self.assertFalse(C.versions_changed(base, {**base, "MediaVersion": "9"}))
        self.assertTrue(C.versions_changed(base, {**base, "TableVersion": "8"}))
        self.assertTrue(C.versions_changed({}, base))

    def test_version_date_and_release_date(self):
        self.assertEqual(C.version_date("202610131404").isoformat(), "2026-10-13")
        self.assertIsNone(C.version_date("abc"))
        self.assertEqual(
            C.release_date({"ResourceVersion": "202610131404", "TableVersion": "202610151554"}),
            "261015",
        )
        self.assertEqual(C.release_date({"ResourceVersion": "202610131404", "TableVersion": "202610151554"}, "2025-01-02"), "250102")


class ReplacementMappingTests(unittest.TestCase):
    def test_longest_prefix_wins(self):
        sets = {"a-b": Path("a-b"), "a-b-c": Path("a-b-c")}
        self.assertEqual(RT.match_replacement_set("a-b-c-2026-06-04_assets_all_1.bundle", sets), "a-b-c")
        self.assertEqual(RT.match_replacement_set("a-b-2026-06-04_assets_all_1.bundle", sets), "a-b")

    def test_match_requires_boundary(self):
        sets = {"uis-01_common-03": Path("x")}
        self.assertIsNone(RT.match_replacement_set("uis-01_common-031-2026.bundle", sets))
        self.assertEqual(RT.match_replacement_set("uis-01_common-03.bundle", sets), "uis-01_common-03")
        self.assertEqual(RT.match_replacement_set("uis-01_common-03-2026.bundle", sets), "uis-01_common-03")

    def test_discover_skips_empty_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "has-images").mkdir()
            (root / "has-images" / "a.png").write_bytes(b"x")
            (root / "empty").mkdir()
            sets = RT.discover_replacement_sets(root)
            self.assertEqual(list(sets), ["has-images"])


class ExcelsTests(unittest.TestCase):
    CONFIG = {"DemoSchema": {"primary_key": "GroupId", "ids": [1, 2, 3]}}

    def _make_db(self, path: Path, rows):
        """真实表结构里 rowid 与 GroupId 不是同一个字段，这里保持一致。"""
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE "DemoSchema" (id INTEGER PRIMARY KEY, GroupId INTEGER, Bytes BLOB)')
        conn.executemany('INSERT INTO "DemoSchema" (GroupId, Bytes) VALUES (?, ?)', rows)
        conn.commit()
        conn.close()

    def test_patch_copies_rows_without_touching_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old.db"
            new = Path(tmp) / "new.db"
            out = Path(tmp) / "out.db"
            self._make_db(old, [(1, b"OLD-1"), (2, b"OLD-2"), (3, b"OLD-3")])
            self._make_db(new, [(1, b"NEW-1"), (2, b"NEW-2"), (3, b"NEW-3")])
            before = new.read_bytes()

            stats = UED.apply_patch(old, new, out, config=self.CONFIG, verify=True)

            self.assertEqual(stats["updated"], 3)
            self.assertEqual(new.read_bytes(), before)
            conn = sqlite3.connect(out)
            rows = dict(conn.execute('SELECT GroupId, Bytes FROM "DemoSchema"').fetchall())
            conn.close()
            self.assertEqual(rows, {1: b"OLD-1", 2: b"OLD-2", 3: b"OLD-3"})

    def test_row_count_mismatch_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old.db"
            new = Path(tmp) / "new.db"
            out = Path(tmp) / "out.db"
            self._make_db(old, [(1, b"OLD-1a"), (1, b"OLD-1b"), (2, b"OLD-2")])
            self._make_db(new, [(1, b"NEW-1"), (2, b"NEW-2")])

            stats = UED.apply_patch(
                old, new, out, config={"DemoSchema": {"primary_key": "GroupId", "ids": [1, 2]}}, verify=False
            )

            self.assertEqual(stats["updated"], 1)
            reasons = {item["id"]: item["reason"] for item in stats["skipped"]}
            self.assertIn("行数不一致", reasons[1])

    def test_missing_table_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old.db"
            new = Path(tmp) / "new.db"
            out = Path(tmp) / "out.db"
            self._make_db(old, [(1, b"OLD")])
            self._make_db(new, [(1, b"NEW")])
            with self.assertRaises(sqlite3.OperationalError):
                UED.apply_patch(old, new, out, config={"NoSuchTable": {"primary_key": "Id", "ids": [1]}}, verify=False)


class PackagingTests(unittest.TestCase):
    def test_zip_tree_keeps_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "out"
            (source / "AssetBundles").mkdir(parents=True)
            (source / "AssetBundles" / "a.bundle").write_bytes(b"hello" * 1000)
            (source / "TableBundles").mkdir(parents=True)
            (source / "TableBundles" / "123_abc").write_bytes(b"db")
            target = Path(tmp) / "pack.zip"

            count = release._zip_tree(source, target, method="deflate", level=9)

            self.assertEqual(count, 2)
            with zipfile.ZipFile(target) as archive:
                self.assertEqual(
                    sorted(archive.namelist()),
                    ["AssetBundles/a.bundle", "TableBundles/123_abc"],
                )
                self.assertEqual(archive.read("AssetBundles/a.bundle"), b"hello" * 1000)

    def test_package_without_output_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = C.Config.create(
                root,
                work_dir=str(root / "work"),
                out_dir=str(root / "out"),
                manifest_dir=str(root / "manifest"),
                dist_dir=str(root / "dist"),
            )
            cfg.prepare()
            result = release.package(cfg, "261015")
            self.assertFalse(result.has_package)
            self.assertIsNone(result.uncensor_zip)


class ReleaseNotesTests(unittest.TestCase):
    def test_notes_contain_warnings_and_table_info(self):
        assets = AU.AssetsResult(resource_version="202610141404", official_entries=24155)
        assets.shipped = [
            AU.ShippedBundle(
                name="a.bundle", kind="patched", md5="1" * 32, size=1234,
                official_crc="2" * 32, official_size=1000, upstream_crc_old="2" * 32,
                upstream_changed=True, textures=3,
            ),
            AU.ShippedBundle(
                name="b.bundle", kind="exclusion", md5="3" * 32, size=4321,
                official_crc="4" * 32, official_size=4000, upstream_crc_old="5" * 32,
                upstream_changed=True,
            ),
        ]
        assets.exclusion_upstream_changed = ["b.bundle"]
        assets.missing_exclusions = ["gone.bundle"]
        assets.official_added = ["x.bundle"]
        assets.official_changed = ["a.bundle", "b.bundle"]
        assets.official_removed = ["y.bundle"]
        assets.targeted = ["a.bundle", "b.bundle"]
        assets.not_patchable = ["nope.bundle"]
        assets.carried = ["a.bundle"]
        assets.unpatchable_carried = ["stale.bundle"]
        assets.dropped = ["vanished.bundle"]
        assets.repatch_changed = ["a.bundle"]

        table = TU.TableResult(
            table_version="202610151554",
            table_manifest_path=Path("TableManifest.json"),
            official_crc="8" * 32,
            official_size=161075200,
            pristine_md5="8" * 32,
            patched_md5="9" * 32,
            patched_size=161140736,
            output_name="6993339912994747134_" + "8" * 32,
            rows_updated=42,
            skipped=[{"id": 7, "reason": "新库中不存在"}],
        )
        package = release.PackageResult(
            uncensor_zip=Path("dist/BlueArchiveCN-Uncensor-261015.zip"),
            restore_zip=Path("dist/BlueArchiveCN-Restore-261015.zip"),
            file_count=2,
            uncensor_bytes=1024,
            restore_bytes=2048,
        )
        manifests = release.ManifestStats(official_entries=24155, modified_entries=2)

        notes = release.build_notes(
            versions={"ResourceVersion": "202610141404", "TableVersion": "202610151554"},
            old_versions={"ResourceVersion": "202610131404", "TableVersion": "202610131404"},
            assets=assets,
            table=table,
            manifests=manifests,
            package=package,
            yymmdd="261015",
        )

        self.assertIn("BlueArchiveCN-Uncensor-261015", notes)
        self.assertIn("exclusions 上游资源有变化", notes)
        self.assertIn("exclusions 在新官方清单中不存在", notes)
        self.assertIn("gone.bundle", notes)
        self.assertIn("上一版补丁无法继续生成", notes)
        self.assertIn("stale.bundle", notes)
        self.assertIn("vanished.bundle", notes)
        self.assertIn("本地补丁内容有变化", notes)
        self.assertIn(table.output_name, notes)
        self.assertIn("TableBundles", notes)
        self.assertIn("BlueArchiveCN-Restore-261015.zip", notes)


class AssetsStageTests(unittest.TestCase):
    def test_load_exclusions_only_accepts_bundles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exclusions = root / "assets" / "exclusions"
            exclusions.mkdir(parents=True)
            (exclusions / "a.bundle").write_bytes(b"x")
            (exclusions / "b.txt").write_bytes(b"x")
            cfg = C.Config.create(root, work_dir=str(root / "work"), out_dir=str(root / "out"), manifest_dir=str(root / "manifest"), dist_dir=str(root / "dist"))
            found = AU.load_exclusions(cfg)
            self.assertEqual(list(found), ["a.bundle"])


if __name__ == "__main__":
    unittest.main()
