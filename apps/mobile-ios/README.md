# Spark 手机客户端（iOS）

读电脑上 Spark 同步到 GitHub 私有仓库（`spark-vault`）的笔记，也能把任务交回给电脑处理。
交互设计稿：https://claude.ai/artifact/5FDAKGB4yMVnhaH7HX5XH1

## 怎么跑

1. （可选）下载自带字体：`dev/fetch_fonts.sh`。霞鹜文楷（正文）和思源黑体（标题）共约 34 MB，
   不进 git。不下载也能编译运行，标题和正文退回系统字体。
2. 用 Xcode 打开 `Spark.xcodeproj`，选 Spark scheme 跑到模拟器或真机。iOS 17 起。
3. 第一次打开填仓库（`minxu23/spark-vault`）和 GitHub 细粒度令牌：只选这个仓库，
   权限给 Contents 只读、Issues 读写。令牌存在钥匙串里。

不想连真 GitHub 调界面时，用本机的假 GitHub：

```
python3 dev/mock_github.py /path/to/一份笔记库副本 --port 8799
```

Debug 版连接页展开「开发用」，API 地址填 `http://127.0.0.1:8799`，令牌随便填。
`POST /_mock/issues/<n>/status` 可以模拟电脑端写回任务进度（见脚本开头的说明）。

## 结构

| 文件 | 做什么 |
|---|---|
| `Shared/GitHub.swift` | 钥匙串里的连接信息；用到的几个 GitHub 接口（文件树、blob、Issue）。主 App 和分享扩展共用 |
| `Shared/SparkTask.swift` | 拼任务 Issue；App Group 里给分享扩展用的书架文件夹名、上次选的位置 |
| `SparkShare/` | 分享扩展：在 YouTube、Safari、播客 App 里「分享 → Spark」，选好放到哪个节目就交给电脑 |
| `Vault.swift` | 手机上的那份笔记库：按 blob sha 增量同步，整理稿和文字记录打开时再下载 |
| `Library.swift` | 从文件树和 `.manifest.json` 拼出节目、会议、订阅、报告；跟阅读页同一套认法 |
| `Markdown.swift` | 够用的 Markdown 渲染 |
| `Theme.swift` / `Fonts.swift` | 跟电脑阅读页同一套配色；字体注册、添加自定义字体 |
| `*View.swift` | 最新、书架、节目、单集阅读、任务、我的 |

小结和一句话总结直接取自处理记录（`.manifest.json`），所以第一次同步只下处理记录、
节目总结和报告，全部小结就能离线看。

主 App 和分享扩展共用钥匙串组 `com.minxu.spark.shared`（令牌）和 App Group `group.com.minxu.spark`
（文件夹名），见 `Spark.entitlements` / `SparkShare.entitlements`。装到真机时 Xcode 的自动签名会替你的
开发者账号注册这两项。

手机上添加的任务是仓库里带 `spark-task` 标签的 Issue，格式和电脑端的处理见
`core/remote_tasks.py`。
