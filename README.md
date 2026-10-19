# BlueArchiveCN-Uncensor

蔚蓝档案（国服）反和谐资源自动构建仓库。

每次游戏更新后，自动比对官方资源清单，把需要修改的 AssetBundle 重新打上替换贴图，
还原 `ExcelDB.db` 中被和谐的文本。

## 一、整体流程

```
scripts/get_version.py            取 ResourceVersion / TableVersion
        │
        ├─ 与 current.txt 一致 ──► 直接结束（不装依赖、不下载）
        │
        ▼ 不一致
assets_update.py                  ① AssetBundles
   ├─ 下载官方 bundleDownloadInfo.json（当前 ResourceVersion）
   ├─ 与旧官方清单比对 → 新增 / CRC 变化
   ├─ 读取上一版 bundleDownloadInfo_Modified.json → 继承已修改的文件
   ├─ exclusions 里的 bundle 只要新清单里还有同名文件，一律打包
   ├─ 贴图命中 → replace_texture.py 打入替换贴图 → out/AssetBundles/
   ├─ exclusions 命中 → 直接复制本地文件 → out/AssetBundles/
   └─ 官方原版 → work/restore/AssetBundles/（给 Restore 包）
table_update.py                   ② TableBundles
   ├─ 下载 TableManifest_{TableVersion}.json → 取 ExcelDB.db 的 Crc / Size
   ├─ 下载官方 ExcelDB（pool/TableBundles/{crc前两位}/{crc}）
   ├─ 下载固定旧库（含未和谐文本）→ update_exceldb.py 还原
   └─ 输出 6993339912994747134_{官方 ExcelDB md5} → out/TableBundles/
release.py                        ③ 清单 & ④ 打包
   ├─ manifest/bundleDownloadInfo_Official.json（官方原样）
   ├─ manifest/bundleDownloadInfo_Modified.json（官方 + 本次修改的 Crc/Size）
   ├─ manifest/manifest_state.json（两份清单对应的版本）
   └─ dist/*.zip + dist/release-notes.md
update_all.py                     串起以上步骤，写 current.txt 与 $GITHUB_OUTPUT
```

### 版本判定

只比较 `ResourceVersion` 与 `TableVersion`（`MediaVersion` 仅记录）。
两者都没变时 Action 在“检查版本”这一步就结束，不会安装 UnityPy、不会下载任何资源。

### 需要处理的文件从哪来

| 来源 | 说明 |
| --- | --- |
| 官方差异 | 新官方清单里新增、或 `Crc`（实际是 MD5）发生变化的文件 |
| 上一版继承 | 上一版 Modified 清单里已经改过的文件，**重新生成后继续发布** |
| exclusions | 新官方清单中存在同名文件的 exclusion，始终打包 |

继承机制保证每个 Release 都是**当前版本完整的补丁集**（不需要按顺序安装历史包），
也让镜像站的修改清单始终覆盖所有已修改文件。

## 二、目录结构

```
.github/workflows/update.yml   定时任务：检查版本 → 构建 → 发布 → 提交版本号
current.txt                    上次处理的 ResourceVersion / TableVersion
manifest/
  bundleDownloadInfo_Official.json   官方清单（下载后原样保存）
  bundleDownloadInfo_Modified.json   修改后的清单（本次打包文件的 Crc=MD5、Size 已替换）
  manifest_state.json                两份清单对应的版本，用于判断基线是否过期
assets/
  replacement/<bundle 前缀>/<Texture2D 名称>.png   要替换的贴图
  exclusions/<完整 bundle 名称>.bundle             需要人工处理、不能自动打补丁的 bundle
scripts/
  common.py            配置、下载（带 MD5/大小校验、重试）、清单/版本工具、GHA 输出
  get_version.py       取版本，并可与 current.txt 比对
  assets_update.py     AssetBundle 阶段
  replace_texture.py   往 bundle 里写贴图（可单独当工具用）
  table_update.py      TableBundles 阶段
  update_exceldb.py    用旧库还原 ExcelDB 指定 ID 的数据（可单独当工具用）
  release.py           清单回写、zip 打包、Release 说明
  update_all.py        一键入口（Action 调用它）
requirements.txt       UnityPy 等运行期依赖
```

> `manifest/` 下的两个清单每次更新会重写，单个约 4.6 MB，仓库体积会随版本推进增长。


## 三、在 GitHub 上运行

* `Actions → Update Resources → Run workflow` 可手动触发，支持三个开关：
  * `force`：忽略版本比对强制执行；
  * `skip_table`：只做 AssetBundles；
  * `all_diffs`：下载所有差异 bundle。
* 需要仓库的 `Settings → Actions → General → Workflow permissions` 允许 **Read and write**；
  workflow 已声明 `permissions: contents: write`，用于推送 `current.txt` / `manifest/` 和创建 Release。
* 发布顺序是“先发 Release，再提交版本号”：如果提交/推送失败，下次运行会重新构建并覆盖同名附件，
  不会出现版本号已更新但 Release 缺失的情况。


## 四、本地运行

```bash
python -m pip install -r requirements.txt

# 1) 只看当前版本 / 是否需要更新
python scripts/get_version.py --check

# 2) 完整跑一遍（产物在 work/、out/、dist/，会改写 current.txt 与 manifest/）
python scripts/update_all.py

# 3) 只更新 AssetBundles，跳过 300MB 的 ExcelDB
python scripts/update_all.py --skip-table

# 4) 用指定的旧清单模拟一次版本变化（调试用）
python scripts/update_all.py --force --skip-table \
    --baseline-manifest work/old_official.json \
    --manifest-dir work/test/manifest --out-dir work/test/out \
    --dist-dir work/test/dist --work-dir work/test/work \
    --current work/test/current.txt --date 261015
```

单独使用两个工具脚本：

```bash
# 往一个 bundle 里写贴图（按目录名自动匹配 assets/replacement/<前缀>）
python scripts/replace_texture.py --bundle some.bundle --replacement assets/replacement \
    --output out/AssetBundles/some.bundle

# 批量处理一个目录
python scripts/replace_texture.py --bundle-dir work/downloads/bundles \
    --replacement assets/replacement --output-dir out/AssetBundles

# 还原 ExcelDB
python scripts/update_exceldb.py --old ExcelDB_old.db --new ExcelDB_new.db --out ExcelDB_patched.db
```

## 五、维护指南

### 新增要替换的贴图

1. 找到目标 bundle 的名称（例如 `uis-01_common-03_nonequipment-_mxload-textures-2026-06-04_assets_all_817837721.bundle`）；
2. 在 `assets/replacement/` 下建立**去掉日期与哈希**的目录，即 `uis-01_common-03_nonequipment-_mxload-textures/`；
3. 把图片按 **Texture2D 的 `m_Name`** 命名（`Item_Icon_ExSkillCD_Random_0.png`），放进去即可。
   尺寸不一致会告警并同步修改贴图尺寸；压缩格式无法编码时自动退回 RGBA32。
4. 下一个 Release 会自动带上它（上游没变化也会重新生成，因为补丁是按“线上是否已修改”继承的）。

### exclusions

`assets/exclusions/` 放的是**已经人工改好、不能被自动打补丁**的 bundle（完整文件名）。
规则：

* 只要新官方清单里还有同名文件，就始终使用本地文件打包；
* 该 bundle 的官方 MD5 与 `manifest/bundleDownloadInfo_Official.json` 中不同 →
  在 Release 里警告“上游资源有变化，可能需要手动重新修改”；
* 该 bundle 在新官方清单里已不存在 → 在 Release 里提示上游可能改名/删除，本次不上传。

### ExcelDB 还原 ID

`scripts/update_exceldb.py` 顶部 `CONFIG` 维护「表名 / 主键 / ID 列表」，
覆盖 `ScenarioScriptDBSchema`、`LocalizeDBSchema`、`LocalizeEtcDBSchema`、`CharacterDialogDBSchema`。
新旧库行数不一致的 ID 会被跳过并写进日志与 Release 说明。

还原后会执行一次 `ANALYZE`（可用 `--no-analyze` 关闭），这样输出库的大小与历史发布保持一致。

### TableBundles 文件名前缀

`TABLE_BUNDLE_ID = 6993339912994747134`（`scripts/common.py`），
输出文件名为 `6993339912994747134_<官方 ExcelDB 的 MD5>`，可用 `--table-bundle-id` 覆盖。

### 清单基线

`manifest/manifest_state.json` 记录两份清单对应的 `ResourceVersion`：

* 官方清单基线过期/缺失 → 从 CDN 拉取 `current.txt` 对应版本的官方清单；
* 上一版修改清单过期/缺失 → 从镜像站 `mx.infastra.de5.net` 拉取对应版本的修改清单；
* 也可以用 `--baseline-manifest` 指定任意官方清单。

## 六、替换方法
> [!TIP]
> 本仓库的 Release 仅适用于 Android 平台。iOS 以及 HarmonyOS 平台均未经测试，尽量避免使用。
> 
> 在替换资源前，请先确保您可以正常读写`/storage/emulated/0/Android/data/`。
> 如果无法读写，建议使用 ADB 授予文件管理器权限。

* 定位到`/storage/emulated/0/Android/data/com.RoamingStar.BlueArchive[.bilibili]/files`。正常情况下，这里应当存在 AssetBundles 和 TableBundles 等目录。如果是空白的，说明您的设备未能正常读写`/storage/emulated/0/Android/data/`。
* 打开从 Release 下载的 .zip 文件，将其中的两个目录（AssetBundles 和 TableBundles）解压到`files`目录中。如果文件管理器提示文件已存在，请务必选择覆盖。

## 七、使用须知

> [!IMPORTANT]
> 本项目仅供学习和展示用途。
> 所有通过本项目获取的内容均应仅用于合法和正当的目的。开发者不对任何因使用本项目而引发的直接或间接损失、损害、法律责任或其他后果承担任何责任。
> 用户在使用本项目时需自行承担风险，并确保遵守所有相关法律法规。严禁将本项目用于任何未经授权或非法的活动。用户应对自身的行为负责。

## 八、版权声明
* **"Blue Archive"** is a registered trademark of **NEXON Korea Corp. & NEXON GAMES Co., Ltd.** All rights reserved.
* **“蔚蓝档案”** 是 **上海星啸网络科技有限公司** 的注册商标，版权所有。

