---
title: "Notes on Building a Small Weather Station"
source: "Example Maker Notes"
url: "https://example.org/maker/weather-station"
published: 2026-11-05
published_at: "2026-11-05T18:00:00+00:00"
clipped: 2026-11-07
clipped_at: "2026-11-07T10:24:05+00:00"
tags: ["传感器", "动手做"]
tldr: "用几个便宜的传感器搭气象站，难点不在电路，而在长期稳定地放在户外"
---

# Notes on Building a Small Weather Station

- 来源：Example Maker Notes
- 发布时间：2026年11月05日，18:00（UTC+0）
- 收录时间：2026年11月07日，10:24（UTC+0）

## 摘要

用几个便宜的传感器搭气象站，难点不在电路，而在长期稳定地放在户外

- 背景：作者想记录自家院子里的温度、湿度和气压。
- 方法：一块小开发板加三个传感器，每五分钟记一次。
- 经验：外壳通风和防晒比传感器精度更影响读数。

## 正文

I wanted a simple record of the weather in my own back garden, not the airport twenty kilometres away.

> 我想要的只是自家后院天气的简单记录，而不是二十公里外机场的数据。

The first version took one evening: a small board, a temperature sensor, a humidity sensor and a pressure sensor, all reading every five minutes.

> 第一版只用了一个晚上：一块小开发板，加上温度、湿度和气压三个传感器，每五分钟读一次。

The readings looked fine indoors, but outside the temperature jumped by six degrees whenever the sun touched the box.

> 放在室内时读数看起来没问题，可一放到户外，只要太阳照到盒子，温度就会跳高六度。

The fix was not a better sensor. It was a white, ventilated shield made from stacked plant saucers.

> 解决办法不是换更好的传感器，而是用几个叠起来的花盆托盘做了一个白色的通风罩。

After three months, the station has missed only two days of data, both during a long power cut.

> 三个月下来，这个气象站只缺了两天的数据，都是因为一次长时间停电。

If you build one, spend more time on the enclosure than on the wiring.

> 如果你也想做一个，花在外壳上的时间要比花在接线上的多。

[阅读原文](https://example.org/maker/weather-station)
