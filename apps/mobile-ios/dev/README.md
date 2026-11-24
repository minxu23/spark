# 样例库模式（只在开发时用）

在模拟器里用一套内置的假文档启动 App，检查界面、截图。和真实笔记库、同步、阅读记录、模型接口完全隔离。

背景：之前在模拟器里运行 App 并同步，把测试数据写进了真实笔记库。此后规定不在模拟器里打开文档，
界面改动就没法在模拟器里检查。样例库模式解决这个问题。

## 怎么启动

```
apps/mobile-ios/dev/run_fixture.sh "iPhone 17 Pro"     # 模拟器名或 UDID
```

脚本为模拟器构建 Debug 版、装上、带环境变量 `SPARK_FIXTURE_DIR=<本目录>/fixture-vault` 启动
（`xcrun simctl launch` 只传 `SIMCTL_CHILD_` 开头的变量）。

只有带这个环境变量启动才是样例库模式。

## 模拟器上默认不许以平时模式运行

DEBUG 构建在模拟器上，启动时既没给 `SPARK_FIXTURE_DIR`、也没给 `SPARK_SIM_REAL=1`（比如从主屏幕点图标打开）：
App 只显示一页说明「模拟器上只能用样例库模式启动」，不建笔记库、不读任何存储、不读钥匙串、不联网。
分流在入口 `SparkMain` 里，`SparkApp` 一个状态都不建。

确实要用这台模拟器里的真实数据时，启动时加 `SPARK_SIM_REAL=1`：

```
SIMCTL_CHILD_SPARK_SIM_REAL=1 xcrun simctl launch <UDID> com.minxu.spark
```

在 Xcode 里跑时，在 scheme 的 Run → Arguments → Environment Variables 里加 `SPARK_SIM_REAL` = `1`（或加 `SPARK_FIXTURE_DIR`）。

真机和 Release 构建不受影响，跟以前一样直接以平时模式运行。

判定（`Fixture.mode`，三种结果）：

| 构建、设备 | SPARK_FIXTURE_DIR | SPARK_SIM_REAL | 结果 |
|---|---|---|---|
| 真机或 Release | 任意 | 任意 | 平时模式 |
| 模拟器 DEBUG | 给了（不全是空白） | 任意 | 样例库 |
| 模拟器 DEBUG | 没给 | `1` | 平时模式（真实数据） |
| 模拟器 DEBUG | 没给 | 没给或不是 `1` | 拦下，只显示说明页 |

## 怎么确认处在样例库模式

- 状态栏区域里有样例库标记（`Spark/FixtureBadge.swift` 的 `FixtureOverlay`）：屏幕最上沿一条 2pt 的橙色细线，
  加一行很小的橙色「样例库」（刘海、灵动岛 iPhone 在时间下面；iPad 在状态栏正中）。iPhone 横屏没有状态栏时，
  细线竖着贴在刘海那一侧的屏幕边上。没有这个标记就不是样例库模式，别继续操作。
- 标记是场景上单独的一个浮层窗口：不参与布局、不改安全区、不接点击，弹出的 sheet 和全屏页面上也看得到。
- 书架六个页签都有内容：Quiet Workshop（播客）、Example Systems Summit 2026（会议）、Example Lab Blog / 示例科技周刊 / 简报（信息）、
  5 篇剪藏、2 篇报告、3 篇随手记。设置里看不到 GitHub 仓库，同步来源是「数据线」。
- 拷样例库出错时，那一条会换成出错原因。

## 开关

三个条件同时成立才开（`Shared/Fixture.swift`，模式只在 `Fixture.currentMode` 定一次，别处读 `Fixture.isOn` / `Fixture.isBlocked`）：

1. DEBUG 构建；
2. 运行在模拟器（`#if targetEnvironment(simulator)`）；
3. 启动时给了 `SPARK_FIXTURE_DIR`。

真机和 Release 构建里这段代码（连同说明页和拦下的判定）不编进去，`Fixture.isOn` 恒为 `false`。开关在进程启动时定一次。

分享扩展是另一个进程，拿不到环境变量，看主 App 在 App Group 里留的标记（两种互斥）：

- 主 App 以样例库模式启动：留 `<App Group>/SparkFixture/active`、清掉 `real`。扩展按样例库模式走（不联网、存储换到样例库的位置）。
- 主 App 以 `SPARK_SIM_REAL=1` 启动：留 `<App Group>/SparkFixture/real`、清掉 `active`。扩展按平时模式走。
- 两种标记都没有（主 App 在这台模拟器上还没以这两种模式启动过）：扩展只显示同样的说明，点「知道了」结束，不读钥匙串、不写发件箱。

标记一直留到下一次以另一种模式启动主 App，或删掉 App。

## 隔离了什么

每次启动先清掉上一次的样例库位置，再把 `SPARK_FIXTURE_DIR` 整份拷进来。上一次划的线、已读状态不留到下一次。

| 东西 | 平时 | 样例库模式 |
|---|---|---|
| 笔记库文件（Vault.root） | Application Support/vault | Library/SparkFixture/support/vault（启动时从 fixture-vault 拷进来，附一份数据线清单） |
| 状态文件、待确认修改、手机废纸篓、处理记录补丁、手机任务 | Application Support/*.json | Library/SparkFixture/support/*.json |
| 用户添加的字体 | Application Support/Fonts | Library/SparkFixture/support/Fonts |
| 崩溃记录 | Application Support/Diagnostics | Library/SparkFixture/support/Diagnostics |
| 缓存（摘要卡、笔记扫描） | Caches | Library/SparkFixture/caches |
| UserDefaults.standard、@AppStorage | App 自己的 | suite `com.minxu.spark.fixture`（启动时清空） |
| App Group 的 UserDefaults | `group.com.minxu.spark` | suite `com.minxu.spark.fixture.group`（启动时清空，只放「只用数据线」） |
| 发件箱、阅读记录、剪藏收件箱 | App Group/Library/Spark/…、App Group/clips | App Group/SparkFixture/… |
| 钥匙串（GitHub 令牌、模型 Key） | 读写 | 不读不写，当作没有 |
| 公式页网页的数据 | 默认数据存储 | 不落盘的数据存储 |

联网一律不做：GitHub、模型接口、订阅检查和 RSS、YouTube、网页剪藏、图片下载、网络时间、系统字体下载都在
`NetGate` 关口被拦下，报「样例库模式不联网」。数据线同步的文件在平时的笔记库目录里，样例库模式不读。
`tests/test_fixture_network_scan.py` 扫源码，新加的联网用法没过关口、也不在豁免清单里就失败。

## 样例库内容

`fixture-vault/` 跟真实笔记库同样的目录结构和文件格式（`Spark/`、`output/`），内容全部是编的，链接都用 example.com / example.org。
不进 App 包（不在 Xcode 同步的文件夹里）。

改样例库后跑 `pytest tests/test_fixture_vault.py`，它按 App 的规则检查每类文档的数量和格式。
