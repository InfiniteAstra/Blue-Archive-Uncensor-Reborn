"""把旧版（已汉化）ExcelDB 里指定 ID 的二进制数据还原到新版 ExcelDB 中。

数据来源：``ExcelDB_old.db``（固定链接的已还原库）+ ``ExcelDB_new.db``（官方最新库）。
输出：新的 ExcelDB 文件，由调用方按 ``<TABLE_BUNDLE_ID>_<官方 ExcelDB 的 MD5>`` 重命名。

用法::

    python scripts/update_exceldb.py --old ExcelDB_old.db --new ExcelDB_new.db --out ExcelDB_patched.db

``CONFIG`` 里维护需要还原的表、主键与 ID 列表。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import common as C

# 填入需要还原的表名、主键列名(PrimaryKey) 和 ID 列表
CONFIG: Dict[str, Dict[str, Any]] = {
    "ScenarioScriptDBSchema": {
        "primary_key": "GroupId",
        "ids": [11000, 11004, 11070, 11080, 11090, 11100, 11110, 11115,
                11120, 11130, 11140, 11150, 12130, 12140, 12200, 21040,
                21050, 31130, 32010, 32020, 32050, 32080, 33050, 33190,
                41130, 41140, 22040, 22180, 22200, 22250, 101040, 2102,
                3501, 70004010, 1000703, 1001108, 1000605, 1300506, 1000302, 1000305,
                1000309, 1001302, 1000403, 1300906, 1300605, 1300115, 1600206, 2300006,
                1600402, 1600405, 1600102, 1600103, 1600105, 1600106, 1300002, 1300003,
                1300005, 1300006, 1300008, 1300009, 2000106, 2000108, 2000109, 2000308,
                1001405, 2300602, 1301103, 1002003, 1002006, 1001903, 1001905, 1001906,
                2300706, 1002102, 1600505, 2000506, 2000520, 1002202, 1002203, 1002303,
                1002402, 1002503, 2000603, 1002602, 1002703, 1002802, 1002803, 1002805,
                1002820, 1002902, 1002903, 1002906, 2000802, 2000805, 2000905, 1600703,
                2001002, 1003103, 1003202, 1003203, 2001102, 1003320, 1600802, 1600805,
                1003502, 1003505, 1003802, 1004115, 2001702, 1004303, 1004305, 2600702,
                2600703, 1004705, 1004802, 1004803, 1004805, 2001802, 1005105, 2001902,
                2001903, 2001905, 2001906, 1005306, 1601105, 2002302, 1006102, 1006103,
                1006106, 2002403, 1006603, 1006802, 1006902, 1007002, 2002605, 2002606,
                1007102, 2601005, 2601006, 1007302, 1007506, 1301303, 1301305, 2003102,
                1007905, 1008103, 1008205, 1008502, 1008503, 2003305, 1601405, 1008902,
                1008805, 1009103, 1601505, 1009303, 2003605, 2003702, 1010003, 1010202,
                2601302, 1601606, 2004006, 1010803, 1011202, 1011205, 1011206, 2601403,
                1011105, 2004406, 1011708, 1601705, 10000080, 10003065, 10005005, 10008045,
                10008060, 10023070, 10023080, 80000005, 80000037, 80000045, 80000064, 80000065,
                80000066, 80000078, 80000083, 90001170, 90001180, 10025100, 10034060, 10035010,
                10035140, 10036005, 10036020, 10036030, 10036040, 10038020, 10038040, 10039050,
                10041020, 10042005, 10044075, 102
                ]
    },
    "LocalizeDBSchema": {
        "primary_key": "Key",
        "ids": [1029945371, 2057882901, 1744275228, 811393088, 628819338, 3331637074, 4107418006, 1517757557,   # 等级 -> Lv.
                2580347134, 2337505291, 2733632040, 3743583277, 1135425457, 138558751, 4175536381, 1532089779,
                1910077527, 2188655192, 419177587, 3953084252, 3772582990, 2415005115, 1896192126, 1007123337,
                189349535, 2293460914, 4263931589, 3796678481, 1372884507, 2980494512, 3765994902, 868929312,
                4120206193, 1035203172, 2664709769,  # 评级 -> Rank.
                926614149, 2740345672, 2661368153, 3000054060, 3632616866,  # 桃信 -> MomoTalk
                3571189559, 2833660799, 2554617240, 1018913594, 4092712805, 1675488441, 313032494,
                1866624346, 848842177, 3554655960, 1967388246, 2731154380, 1167271857, 1843429870,  # 桃桃朋友 -> MomoFriends
                235360828, 3268040273, 4132343069, 674923685, 1449222903, 3446804866, 4212842525, 2102106008,  # 地下市场 -> 黑市
                977349902, 2930519510, 4138470134, 838899701, 1735550433, 3874145345, 1625249857, 3011986499,
                2005605206, 67492089, 2585148003, 847788836, 2791958937, 1843801151, 3253843096, 923060075,
                1404779164, 2747481849, 1425412045, 3968629623, 3210250803,
                784340309, 93064324, 2415391145, 3999678078, 1245099651, 1616431540, 1198879100, 541904195,  # 快速战斗 -> 扫荡
                1672123177, 2850910145, 463173820, 1022712003, 794509738, 622156807, 2432806128, 3818868405,
                2374631294, 2763842194, 3155830952, 1891289477, 2534254496, 4039049428, 1331289465, 2136033445,
                874215495, 666499897, 148191485, 621730782, 266731063, 3350374795, 1701953036, 3638325472,
                2009196575, 3259988798, 2852233577, 3529996898, 2862510707, 2855453804, 2216110631, 1808836041,
                3520807201, 3402334795, 4263662051, 1005054603, 1748745776, 2944566319, 1062253632, 3184677446,
                3438518456, 1040697366, 2484856030, 1532599915, 3141934736, 2338774266, 1931481224, 1918389992,
                2980942762, 2291564435, 356271106, 3515831479, 4014114460, 309413923, # 成员 -> 学员
                404105934, 2978806820, 2206508743, 1843937705, 3997137133, 417290178,  # 学院后缀补全
                2224197674, 2877335168, 3736730534, 4226222338, 2940461121, 1563751060,
                633276955, 2235111023, 2985457478, 1400947375, 4121984922, 1500655914,  # MIN & MAX
                3000054060
                ]
    },
    "LocalizeEtcDBSchema": {
        "primary_key": "Key",
        "ids": [1115694813, 3392023658,  # MomoTalk
                2320490131, 2237745495, 1332059101, 3223551422, 3033340240, 1207585231, 2073706703, 4279598505,  # MomoFriends
                529277183, 3122346673, 186943085, 1883652491, 2764760079, 4032042015, 3194820480, 303399993,
                3920217594, 988814768, 1303798775, 2327997342, 2538482771, 421326092, 792048723, 2633292506,
                790433508, 3761193180, 4237292601, 4119019242, 66543757, 2157520585, 3679010773
                ]
    },
    "CharacterDialogDBSchema": {
        "primary_key": "CharacterId",
        "ids": [16001,  # 明日奈
                13000,  # 茜
                10028,  # 明日奈（邦尼）
                20019,  # 茜（邦尼）
                26009,  # 柚子（女仆）
                10112,  # 明日奈（制服）
                26001   # 小玉
                ]
    }
}


def apply_patch(
    old_db: Path,
    new_db: Path,
    out_db: Path,
    *,
    config: Optional[Dict[str, Dict[str, Any]]] = None,
    analyze: bool = False,
    verify: bool = True,
) -> Dict[str, Any]:
    """把 ``old_db`` 里的目标行覆盖到 ``new_db`` 的副本（``out_db``）上。"""
    config = CONFIG if config is None else config
    old_db, new_db, out_db = Path(old_db), Path(new_db), Path(out_db)

    for path, label in ((old_db, "旧库"), (new_db, "新库")):
        if not path.is_file():
            raise FileNotFoundError(f"找不到{label}: {path}")
    if old_db.resolve() == out_db.resolve() or new_db.resolve() == out_db.resolve():
        raise ValueError("输出文件不能与输入文件相同")

    out_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(new_db, out_db)
    C.log(f"[复制] {new_db.name} -> {out_db.name} ({C.human_size(out_db.stat().st_size)})")

    started = time.time()
    stats: Dict[str, Any] = {"tables": {}, "updated": 0, "skipped": [], "duration": 0.0}

    conn_old = sqlite3.connect(f"file:{old_db.as_posix()}?mode=ro", uri=True)
    conn_new = sqlite3.connect(out_db)
    try:
        cursor_old = conn_old.cursor()
        cursor_new = conn_new.cursor()

        for table_name, table_config in config.items():
            primary_key = table_config["primary_key"]
            target_ids = table_config.get("ids") or []
            if not target_ids:
                continue

            C.log(f"──── {table_name} ({primary_key}, {len(target_ids)} 个 ID) ────")
            table_updated = 0
            table_skipped: List[Dict[str, Any]] = []

            for target_id in target_ids:
                cursor_old.execute(
                    f'SELECT rowid, Bytes FROM "{table_name}" WHERE "{primary_key}" = ? ORDER BY rowid',
                    (target_id,),
                )
                old_rows = cursor_old.fetchall()

                cursor_new.execute(
                    f'SELECT rowid FROM "{table_name}" WHERE "{primary_key}" = ? ORDER BY rowid',
                    (target_id,),
                )
                new_rows = cursor_new.fetchall()

                if not old_rows or not new_rows:
                    reason = "旧库中不存在" if not old_rows else "新库中不存在"
                    table_skipped.append({"id": target_id, "reason": reason})
                    continue
                if len(old_rows) != len(new_rows):
                    reason = f"行数不一致(旧 {len(old_rows)} / 新 {len(new_rows)})"
                    table_skipped.append({"id": target_id, "reason": reason})
                    continue

                for (old_rowid, old_bytes), (new_rowid,) in zip(old_rows, new_rows):
                    cursor_new.execute(
                        f'UPDATE "{table_name}" SET Bytes = ? WHERE rowid = ?',
                        (sqlite3.Binary(old_bytes), new_rowid),
                    )
                    table_updated += 1

            if table_skipped:
                C.log(
                    f"  [跳过] {len(table_skipped)} 个 ID: "
                    + ", ".join(f"{item['id']}({item['reason']})" for item in table_skipped[:8])
                    + (" ..." if len(table_skipped) > 8 else "")
                )
            C.log(f"  [完成] 覆盖 {table_updated} 行")

            stats["tables"][table_name] = {
                "updated": table_updated,
                "skipped": table_skipped,
                "ids": len(target_ids),
            }
            stats["updated"] += table_updated
            stats["skipped"].extend(table_skipped)

        if analyze:
            C.log("[ANALYZE] 正在统计索引信息 ...")
            cursor_new.execute("ANALYZE;")

        conn_new.commit()
    except Exception:
        conn_new.rollback()
        raise
    finally:
        conn_old.close()
        conn_new.close()

    stats["duration"] = time.time() - started

    if verify:
        _verify_database(out_db)

    C.log(
        f"[结果] 累计覆盖 {stats['updated']} 行, 跳过 {len(stats['skipped'])} 个 ID, "
        f"耗时 {stats['duration']:.1f}s, 输出 {C.human_size(out_db.stat().st_size)}"
    )
    return stats


def _verify_database(path: Path) -> None:
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA quick_check").fetchone()
    finally:
        conn.close()
    if not result or str(result[0]).lower() != "ok":
        raise RuntimeError(f"输出数据库校验失败: {path} -> {result}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="用旧版 ExcelDB 的汉化数据覆盖新版 ExcelDB")
    parser.add_argument("--old", default=str(C.ROOT / "ExcelDB_old.db"), help="旧库（已汉化）")
    parser.add_argument("--new", default=str(C.ROOT / "ExcelDB_new.db"), help="新库（官方最新）")
    parser.add_argument("--out", default=str(C.ROOT / "ExcelDB_patched.db"), help="输出文件")
    parser.add_argument("--analyze", action="store_true", help="额外执行 ANALYZE（会改变文件内容）")
    parser.add_argument("--no-verify", action="store_true", help="跳过 quick_check 校验")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        apply_patch(Path(args.old), Path(args.new), Path(args.out), analyze=args.analyze, verify=not args.no_verify)
    except Exception as exc:  # noqa: BLE001 - CLI 统一报错
        C.error(f"{type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
