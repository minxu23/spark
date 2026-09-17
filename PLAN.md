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

### P2 入库（第一个新能力，任务 4 卡在这）

- `core/vault.py`：扫描沿用 notes2insight；新增 `ingest()`，按库内既有命名
  （`2026-08-30_Ep. 027 - ...`）写入 `Podcast/<节目>/` 与 `Podcast/_transcripts/<节目>/`
- 索引层把同一期的整理稿 + 逐字稿登记成一个条目两个表示：检索只扫整理稿，
  回溯时读逐字稿。**不删任何文件**——同名 ≠ 重复内容
- 先拿 `The AI Daily Brief`（433 对）验证
- 写库前先 dry-run 出路径清单确认；只新增，不覆盖同名已存在文件

验收：扫描条目数 4917 → 约 3518 而材料一篇不少；同一主题检索结果集前后一致。

### P3 统一摘要卡（任务 4 真正成立）

`core/digest.py`：一套 schema、三套模板（会议演讲 / 播客单集 / 笔记），缓存键统一。
动 prompt 会影响产出质量，必须拿已有素材跑新旧对照。

### P4 统一任务运行时

`core/jobs.py` 取 summit2md 那份（有 stop/pause/resume），加持久化，为定时铺路。

### P5+ 订阅清单 + 定时 → 成本记账 → 演示层开放

### 前端

不在计划内。P2 之后再判断是否值得重写 2700 行手写 DOM。

## 停顿点

P2 之后停一周实际使用，再决定是否上 P3。
