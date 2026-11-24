#!/bin/bash
# 在模拟器里用样例库模式启动 Spark（说明见同目录 README.md「样例库模式」）。
#
# 用法：dev/run_fixture.sh <模拟器名或 UDID>
#   例：dev/run_fixture.sh "iPhone 17 Pro"
#
# 做的事：为模拟器构建 Debug 版 → 装到这台模拟器 → 带 SPARK_FIXTURE_DIR 启动。
# simctl launch 只把 SIMCTL_CHILD_ 开头的环境变量传给 App（去掉前缀）。
#
# 只有用这个脚本（或带同样的环境变量）启动才是样例库模式。
# 从模拟器主屏幕点图标打开（不带环境变量）时，DEBUG 版只显示说明页「模拟器上只能用样例库模式启动」，
# 不读这台模拟器里原来的数据和钥匙串。确实要用真实数据时，启动时加 SIMCTL_CHILD_SPARK_SIM_REAL=1（见 README.md）。
set -euo pipefail

if [ $# -ne 1 ] || [ -z "$1" ]; then
  echo "用法：$0 <模拟器名或 UDID>" >&2
  exit 2
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
IOS="$(dirname "$HERE")"
FIXTURE="$HERE/fixture-vault"
BUNDLE="com.minxu.spark"
DERIVED="${SPARK_FIXTURE_DERIVED:-${TMPDIR:-/tmp}/spark-fixture-build}"

if [ ! -d "$FIXTURE/Spark" ]; then
  echo "找不到样例库：$FIXTURE" >&2
  exit 1
fi

# 模拟器名换成 UDID（给的就是 UDID 时原样用）
ARG="$1"
if [[ "$ARG" =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]]; then
  UDID="$ARG"
else
  UDID="$(xcrun simctl list devices available -j | /usr/bin/env python3 -c '
import json, sys
name = sys.argv[1]
devs = [d for rt in json.load(sys.stdin)["devices"].values() for d in rt if d.get("name") == name]
booted = [d for d in devs if d.get("state") == "Booted"]
pick = (booted or devs or [None])[0]
print(pick["udid"] if pick else "")
' "$ARG")"
fi
if [ -z "$UDID" ]; then
  echo "找不到叫「$ARG」的模拟器（xcrun simctl list devices available 看有哪些）" >&2
  exit 1
fi

echo "模拟器：$ARG（$UDID）"
xcrun simctl boot "$UDID" 2>/dev/null || true   # 已经开着时会报错，不管它

echo "构建（Debug，模拟器）……"
xcodebuild -project "$IOS/Spark.xcodeproj" -scheme Spark -configuration Debug \
  -destination "id=$UDID" -derivedDataPath "$DERIVED" build -quiet

APP="$DERIVED/Build/Products/Debug-iphonesimulator/Spark.app"
if [ ! -d "$APP" ]; then
  echo "构建产物不在预期的位置：$APP" >&2
  exit 1
fi

echo "安装……"
xcrun simctl install "$UDID" "$APP"

echo "以样例库模式启动（SPARK_FIXTURE_DIR=$FIXTURE）……"
SIMCTL_CHILD_SPARK_FIXTURE_DIR="$FIXTURE" xcrun simctl launch --terminate-running-process "$UDID" "$BUNDLE"

echo "已启动。状态栏区域里应该有橙色的样例库标记：屏幕最上沿一条细线，加一行很小的「样例库」。没有标记就不是样例库模式，别继续操作。"
