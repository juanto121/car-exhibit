#!/bin/sh
# Render a figure HTML file to a 2x PNG.
# Usage: ./render.sh pipeline               -> pipeline.png
#        HEIGHT=1300 ./render.sh pipeline   -> taller crop (CSS pixels; PNG is 2x)
set -e
cd "$(dirname "$0")"
name="${1:-pipeline}"
chrome="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$chrome" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=2 \
  --window-size=1800,"${HEIGHT:-1380}" --screenshot="$PWD/$name.png" "file://$PWD/$name.html" >/dev/null 2>&1
echo "Wrote $name.png"
