# Small Data Pipelines That Last

- 所属会议：Example Systems Summit 2026
- 链接：https://example.com/summit/2026/small-pipelines
- 发布时间：2026年10月22日
- 说明：本文由 AI 基于转写整理为流畅演讲稿（原文/中文对照）

## 演讲稿

Most data pipelines in small teams are scripts that grew. That is fine, as long as they can be rerun.

> 小团队里的大多数数据管线都是慢慢长大的脚本。这没关系，只要它们能重跑。

Write every intermediate result to disk, so any single step can be run again on its own.

> 每一步的中间结果都写到磁盘上，这样任何一步都能单独重跑。

Every dependency is a future upgrade you will have to do. Prefer the standard library.

> 每一个依赖都是将来必须做的一次升级。能用标准库就用标准库。

Write error messages for the person who gets woken up at three in the morning.

> 报错信息要写给凌晨三点被叫醒的那个人看。
