#!/bin/bash
# 生成 ~/Developer/Spark.app —— 双击即启动 Spark。
#
# 用 .app 而不是直接给 .command 贴图标：只有正经的 bundle 才能被 Finder、Dock、
# Spotlight 一致识别，也不会因为拷贝/同步丢掉图标（贴在文件上的自定义图标存在
# 资源分支里，很容易掉）。
#
# 这个 app 本身不跑服务，它只是打开 Terminal 执行「启动 Spark.command」——这样
# 日志看得见、Ctrl+C 停得掉，和原来的用法完全一致。
set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP="$(dirname "$REPO")/Spark.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp "$REPO/assets/Spark.icns" "$APP/Contents/Resources/Spark.icns"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Spark</string>
  <key>CFBundleDisplayName</key><string>Spark</string>
  <key>CFBundleIdentifier</key><string>local.minxu.spark</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>Spark</string>
  <key>CFBundleIconFile</key><string>Spark</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/Spark" <<'RUN'
#!/bin/bash
# 先按相对位置找启动脚本（Spark.app 和 spark/ 是同级目录），这样整个 Developer
# 目录搬到别处也照样能用；找不到再回落到写死的路径。
HERE="$(cd "$(dirname "$0")/../../.." && pwd)"
LAUNCHER="$HERE/spark/启动 Spark.command"
[ -f "$LAUNCHER" ] || LAUNCHER="$HOME/Developer/spark/启动 Spark.command"

if [ ! -f "$LAUNCHER" ]; then
  osascript -e 'display alert "找不到 Spark" message "没有找到「启动 Spark.command」。请确认 Spark.app 和 spark/ 目录放在一起，或者仓库还在 ~/Developer/spark。" as critical'
  exit 1
fi
open -a Terminal "$LAUNCHER"
RUN
chmod +x "$APP/Contents/MacOS/Spark"

touch "$APP"                       # 让 Finder 重新读一遍图标
echo "生成好了：$APP"
