# Spark

把 `summit2md` 和 `notes2insight` 合并成一个项目：共享 `core/`，`apps/` 下各自保留前端。

目标不是少一个端口，是让任务之间能接力——会议/播客的产物进笔记库，笔记库的材料
和会议产物能混在一起出报告。

## 布局

```
core/     llm · keys · vault(含入库) · digest · jobs
apps/     summit2md · notes2insight   ← 从 ~/Claude/ 复制而来，前端不动
tests/
```

**已于 2026-09-17 切换**：日常使用的是 `apps/` 下这两份，`summit2md/output`（35M 产物）
和 notes2insight 的 `.cache`（摘要卡缓存）都已搬过来，Spark 不再依赖 `~/Claude`。
老目录 `~/Claude/summit2md`、`~/Claude/notes2insight` 保留为只读后路，不再改动。

## 任务清单

1. Summit 总结（已有）
2. Podcast 定期跟进（需要订阅清单 + 定时，属新建）
3. 库里挑内容出专题报告（已有）
4. 跨会议/播客出专题报告（**卡在入库，见 P2**）
5. 入库：产物直接落笔记库
6. 订阅清单：节目 → 链接 → 上次处理到哪一集
7. 定时无人值守
8. 演示层（deck/pptx）对所有任务开放
9. 跨报告再综合
10. 成本记账
11. 索引层去重：整理稿/逐字稿登记成一个条目两个表示

## 分期

### P1 共享后端层（地基，无新能力）

- `core/llm.py`：合并两份五后端实现
  - 取 notes2insight：stdout+stderr 一起读、`finish_reason` 归因、per-context certifi 回退
  - 取 summit2md：`thinking: disabled`、空内容统一兜底
- `core/keys.py`：`~/.spark/keys` 优先，回落 `~/.summit2md/keys`；老 key 文件不迁移
- 两个 app 副本改为 import core，保留 `SummarizeError` / `LLMError` 别名

**已知偏差（相对最初设想）**：原打算在 P1 去掉 summit2md 进程级的
`os.environ.setdefault("SSL_CERT_FILE", certifi.where())`。实测这台机器的系统 Python
缺 CA，而 `yt_dlp` 是**库调用**、跑在同一进程里，靠的就是这两个环境变量。删掉会让
字幕下载在这台机器上直接失败。因此保留，改由 `core/llm.py` 自己用 per-context 回退
（两层都有，互不冲突）。等真正把两个 app 合进一个进程时再处理这个全局副作用。

### P2 统一输出目的地（已完成）

**原先的前提是错的。** 计划里写"产物没进库、要手动搬"——实际上 summit2md 一直支持
自定义输出目录，笔记库 `会议/` 下已经有 9 个 summit2md 原样布局的条目（README.md +
speech/ + transcripts/ + logs/ + topics/）。入库通道本来就有，真正的问题是**不一致**：
2026-09-13~15 的运行写进了笔记库，09-16 之后的 6 个留在了 app 的默认 output/ 里。

另外查明：库里有两种互不相通的笔记格式。`会议/<名称>/` 是 summit2md 的目录树；
`Podcast/<节目>/` 是另一条流水线的单篇笔记（头部元数据 + `## 摘要` + `## 观点`，
署名 `整理模型：gemini`）。产生第二种格式的工具不在 `~/Claude` 下。**该流水线已停用，
以后统一用 summit2md**，所以 `Podcast/` 下是历史存量，不再新增。

实际做的（范围收窄为"只统一输出目的地"）：

- `core/vault.py`：库的位置一处定义，`SPARK_VAULT` 环境变量可覆盖；
  `default_output_dir()` 在库不可用时退回 app 目录，避免凭空造一棵目录树
- summit2md 的 `DEFAULT_OUTPUT_DIR` 改为笔记库的 `会议/`
- notes2insight 的 `DEFAULT_VAULT` 改为引用 core，去掉第二处硬编码
- 那 6 个滞留在 app 目录的产物已搬进 `会议/`（用户手工完成）。manifest 里存的是
  `relative_path`，搬目录不影响"跳过已生成"，续跑照常

结果：notes2insight 现在索引到 `会议/` 下 835 篇，**任务 4（跨会议/播客出专题报告）
全量可行**。

**没做、留到以后**：

- 格式转换（summit2md 产物 → `Podcast/` 那套单篇笔记格式）。gemini 流水线既然停了，
  新内容直接用 summit2md 的布局即可；历史存量要不要回填格式，等实际用一阵再说
- 索引层"一个条目两个表示"（`Podcast/` 的整理稿 + 逐字稿 1399 对）。纯属扫描成本
  优化，不影响结果正确性——检索层已按标题+日期去重

### 缓存键改为内容寻址（2026-09-17）

摘要卡缓存原本按「路径 + mtime + 关注点 + 后端 + 模型」做键，于是：

- 给文件夹改名（会议/ → Spark/）会让那 835 篇的缓存整批作废，而内容一个字没变
- 同步工具、Finder、Obsidian 插件碰一下文件就改 mtime，同样白白重新摘取

改成按正文哈希寻址：键 = 提示词版本 + 后端 + 摘取模型 + 关注点 + 标题 + 日期 +
sha1(正文)。路径仍进提示词（给模型一点来源上下文）但不进键——这跟 idx 的处理一致，
它也在提示词里、不在键里，因为输出格式里没有它，卡片内容由正文决定。

代价：正文/标题/日期完全相同但路径不同的两篇会共用一张卡。对内容派生的产物来说
这是对的行为。

**已有的 103 条旧缓存救不回来**：旧键要用到当时的关注点和模型，而关注点是任意
文本，无法枚举反推。一次性损失，上限是重新摘取一遍那批笔记。

### 合并成一个服务（已完成）

一个进程、一个端口（8760）、一个入口。落地页在 `/`，两个 app 挂在 `/summit/` 和
`/notes/` 下。

- `spark.py`：用 WSGI 层的 `DispatcherMiddleware` 按前缀分发，**34 条路由一条都没改**
  ——每个 app 收到的仍是自己原来的 `/api/env` 这种路径
- 两个 app 改成正经的包（`apps/*/__init__.py` + 相对 import）。此前两边都有顶层
  `pipeline.py` / `server.py`，`sys.modules` 全局，谁先 import 谁赢——这条隐患就此消解，
  测试里那个 importlib 别名 hack 也删掉了
- 前端 **38 处绝对路径**（`/api/`、`/static/`、`/deck/`）改成相对。不带斜杠的
  `/summit` 由 werkzeug 自动 308 到 `/summit/`，相对路径才解析得对
- summit2md 顶部那句进程级 `SSL_CERT_FILE` 挪进 `core/certs.py::ensure_ca_env()`，
  从 import 副作用变成显式调用。合进程后它也会影响 notes2insight，影响是良性的
  （同一份 certifi CA 包），但必须看得见
- 各 app 自己的启动器（`run.sh`、`.command`、`launch.py`）删掉了——它们会另起进程
  占独立端口，跟单端口相冲突。只调试单个 app 用 `python3 -m apps.<app>.server`

用内置浏览器实际验证：两个界面都能打开，网络面板里所有请求都落在
`/summit/api/...` 和 `/notes/api/...` 上并返回 200，包括带查询参数的笔记树。

### P4 统一任务运行时

`core/jobs.py` 取 summit2md 那份（有 stop/pause/resume），加持久化，为定时铺路。

### P5+ 订阅清单 + 定时 → 成本记账 → 演示层开放

### 统一入口（已完成）

一个启动器 → 一个落地页选任务 → 进各自的界面。`spark.py` 把两个 app 拉起来并在
8760 提供落地页；端口已经被占用时直接沿用，不重复拉起（也不会把用户自己开的服务
纳入自己的子进程，退出时不误杀）。两个 app 一行代码都没改。

### 前端真正合成一个服务（未做）

一个端口、一个进程、两个 UI 挂在 /summit 和 /notes 下。要动的：

- 两个 app 都有顶层 `pipeline.py` / `server.py`，`sys.modules` 是全局的——得先改成包
- 34 条路由挂 blueprint 前缀
- 前端 36 处绝对路径（`/api/...`、`/static/...`）要改成相对
- summit2md 顶部那句进程级 `SSL_CERT_FILE` 会影响同进程里的 notes2insight，得一并处理

落地页在这一步会变成合并后服务的首页，不是白做的。

## 停顿点

P2 之后停一周实际使用，再决定是否上 P3。
