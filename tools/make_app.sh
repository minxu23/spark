#!/bin/bash
# 生成 ~/Developer/Spark.app —— Spark 的 Mac 应用。
#
# 外壳在 apps/macos：打开就在后台启动 spark.py；侧边栏、文章列表、工具栏是原生的，
# 文章和工具页面由这个服务提供，菜单栏看任务状态，任务跑着时不让电脑闲置休眠。
# 改 Python 不用重新生成，重启服务就行；只有改 apps/macos 或搬动仓库才要重新运行这个脚本。
#
# 日志在 ~/Library/Logs/Spark/server.log。
set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP="$(dirname "$REPO")/Spark.app"
BUILD="$(mktemp -d)/Spark.app"

swift build -c release --package-path "$REPO/apps/macos"

mkdir -p "$BUILD/Contents/MacOS" "$BUILD/Contents/Resources"
cp "$(swift build -c release --package-path "$REPO/apps/macos" --show-bin-path)/Spark" "$BUILD/Contents/MacOS/Spark"
cp "$REPO/assets/Spark.icns" "$BUILD/Contents/Resources/Spark.icns"

cat > "$BUILD/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Spark</string>
  <key>CFBundleDisplayName</key><string>Spark</string>
  <key>CFBundleIdentifier</key><string>local.minxu.spark</string>
  <key>CFBundleVersion</key><string>2.0</string>
  <key>CFBundleShortVersionString</key><string>2.0</string>
  <key>CFBundleExecutable</key><string>Spark</string>
  <key>CFBundleIconFile</key><string>Spark</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>SparkRepo</key><string>$REPO</string>
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSAllowsLocalNetworking</key><true/>
    <key>NSAllowsArbitraryLoadsInWebContent</key><true/>
  </dict>
</dict>
</plist>
PLIST

codesign --force --sign - "$BUILD"

# 旧版本进废纸篓，不直接删
if [ -e "$APP" ]; then
  mv "$APP" "$HOME/.Trash/Spark-$(date +%Y%m%d%H%M%S).app"
fi
mv "$BUILD" "$APP"
touch "$APP"                       # 让 Finder 重新读一遍图标
echo "生成好了：$APP"
