#!/bin/zsh
# 下载 App 自带的两款开源字体到 Spark/Fonts/（不进 git，见 .gitignore）：
#   正文：霞鹜文楷 Regular（lxgw/LxgwWenKai，约 26 MB）
#   标题：思源黑体 CN Medium（adobe-fonts/source-han-sans，从约 51 MB 的 CN 压缩包里只取这一个字重）
# 两款都是 SIL Open Font License，可以随 App 分发。没跑这个脚本也能编译，标题和正文会退回系统字体。
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p Spark/Fonts
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

WENKAI_TAG=v1.530
SHS_TAG=2.005R

if [[ ! -f Spark/Fonts/LXGWWenKai-Regular.ttf ]]; then
  curl -fL --progress-bar -o "$tmp/LXGWWenKai-Regular.ttf" \
    "https://github.com/lxgw/LxgwWenKai/releases/download/$WENKAI_TAG/LXGWWenKai-Regular.ttf"
  mv "$tmp/LXGWWenKai-Regular.ttf" Spark/Fonts/
fi

if [[ ! -f Spark/Fonts/SourceHanSansCN-Medium.otf ]]; then
  curl -fL --progress-bar -o "$tmp/shs.zip" \
    "https://github.com/adobe-fonts/source-han-sans/releases/download/$SHS_TAG/19_SourceHanSansCN.zip"
  unzip -j -o "$tmp/shs.zip" '*SourceHanSansCN-Medium.otf' -d "$tmp" >/dev/null
  mv "$tmp/SourceHanSansCN-Medium.otf" Spark/Fonts/
fi

ls -lh Spark/Fonts
