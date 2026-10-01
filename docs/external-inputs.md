# 外部输入与交付范围

## 公开仓库内容

上传自建 `server/` 源码、配置、翻译表、回归测试，`tools/` 中的自建脚本，以及选定技术文档。`tools/il2cpp-dumper/` 是本机第三方工具和生成结果，不随仓库上传。

根目录的个人交接、账号状态报告、日志、截图、视频及完整研究产物保留在原工作区。没有删除或移动这些文件。

## 上游参考输入

当前代码按下列目录寻找参考输入，协议回归需要前两项：

| 本地目录 | 上游参考 | 原工作区版本前缀 |
| --- | --- | --- |
| `source-client/astiob-digimon-linkz-client-250d17b/` | https://github.com/astiob/digimon-linkz-client | `250d17b` |
| `source-masters/astiob-digimon-linkz-masters-b4b6b56/` | https://github.com/astiob/digimon-linkz-masters | `b4b6b56` |
| `source-ronald/ronald-chun-digimon-linkz-6ea0f69/` | https://github.com/ronald-chun/digimon-linkz | `6ea0f69` |

版本前缀来自现有解包目录名，不代表本次已向上游确认完整提交 SHA 或远程可用性。准备输入时应核对版本，并按表中目录放置。不要用上游最新版本自动替换本机已验证的参考输入。

`server/linkz_server.py` 从客户端参考的 `Secure/ConstValue.cs` 读取协议常量；主数据从 `source-masters/astiob-*/` 读取。公开交付不复制这些第三方参考文件。

## 大型资源另存

| 本地位置 | 用途 | 本次处理 |
| --- | --- | --- |
| `org.is_saved.zip` | 原始资源来源，约 751 MB | 保留本地，不上传 Git |
| `assets-restored-all/` | 恢复的 Unity 资源缓存，约 749 MiB | 保留本地，后续可单独备份 |
| `client-local/` | 本地 APK 候选和签名密钥 | 不上传；密钥需独立保护 |
| `runtime/` | 正式存档、缓存版本、运行日志和备份 | 不上传，维持当前账号 |
| `android-sdk/`、`android-avd/` | 本机 Android 工具及设备镜像 | 不上传 |
| `downloads/`、`work/`、`.venv/` | 原始下载、工作产物和本机依赖 | 不上传 |

本次未新建网盘分享或 GitHub Release，也未删除任何原件。源码交付完成不等于游戏资源交付完成；要在新电脑游玩，需要自行准备合法持有的 APK 和资源。

## 验证边界

公开源码整理只变更 Git 排除规则和交付文档。业务代码没有因此改写，历史回归结果仍只覆盖各自批次。正式存档与签名文件不会加入 Git 索引；上传前检查索引大小和常见密钥格式，不以扫描代替对每类外部材料的使用权限判断。
