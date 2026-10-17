# manifest

这两个 JSON 由 GitHub Action 自动生成 / 覆盖，请不要手工修改：

| 文件 | 内容 |
| --- | --- |
| `bundleDownloadInfo_Official.json` | 官方 `bundleDownloadInfo.json` 原样保存（对应 `current.txt` 里的 `ResourceVersion`） |
| `bundleDownloadInfo_Modified.json` | 以官方清单为底，把本次发布的补丁文件的 `Crc`（=MD5）与 `Size` 替换掉 |
| `manifest_state.json` | 上面两份清单对应的版本，用于下一次比对时判断基线是否过期 |

镜像站可以直接使用 `bundleDownloadInfo_Modified.json` 作为“修改后的清单”。
