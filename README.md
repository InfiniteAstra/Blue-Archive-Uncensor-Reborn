# BlueArchive-Uncensor
非官方《蔚蓝档案》资源自动化修改

## 简介
基于 GitHub Action 实现资源自动化拉取、修改、并生成 .zip 压缩包上传 GitHub Release。

## 用法
> [!TIP]
> 本仓库的 Release 仅适用于 Android 平台。iOS 以及 HarmonyOS 平台均未经测试，尽量避免使用。
> 
> 在替换资源前，请先确保您可以正常读写`/storage/emulated/0/Android/data/`。
> 如果无法读写，建议使用 ADB 授予文件管理器权限。

* 定位到`/storage/emulated/0/Android/data/com.RoamingStar.BlueArchive[.bilibili]/files`。正常情况下，这里应当存在 AssetBundles 和 TableBundles 等目录。如果是空白的，说明您的设备未能正常读写`/storage/emulated/0/Android/data/`。
* 打开从 Release 下载的 .zip 文件，将其中的两个目录（AssetBundles 和 TableBundles）解压到`files`目录中。如果文件管理器提示文件已存在，请务必选择覆盖。


## 使用须知

> [!IMPORTANT]
> 本项目仅供学习和展示用途。
> 所有通过本项目获取的内容均应仅用于合法和正当的目的。开发者不对任何因使用本项目而引发的直接或间接损失、损害、法律责任或其他后果承担任何责任。
> 用户在使用本项目时需自行承担风险，并确保遵守所有相关法律法规。严禁将本项目用于任何未经授权或非法的活动。用户应对自身的行为负责。

## 版权声明
* **"Blue Archive"** is a registered trademark of **NEXON Korea Corp. & NEXON GAMES Co., Ltd.** All rights reserved.
* **“蔚蓝档案”** 是 **上海星啸网络科技有限公司** 的注册商标，版权所有。

