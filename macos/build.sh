#!/bin/sh
# Build for the current Mac; the finished bundle may be moved to /Applications.
set -eu
src=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
output=${1:-"$src/build"}
mkdir -p "$output"
output=$(cd -- "$output" && pwd)
name='Codex 纽约线路.app'
if [ -e "$output/$name" ]; then
    echo "已有同名应用。请指定另一个输出目录，或移走之前的构建。" >&2
    exit 1
fi
work=$(mktemp -d "$output/.codex-ny-build.XXXXXXXX")
trap 'rm -rf "$work"' EXIT
app="$work/$name"
icons="$work/NewYorkRoute.iconset"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources" "$icons"
for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$src/NewYorkRoute.png" --out "$icons/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z "$double" "$double" "$src/NewYorkRoute.png" --out "$icons/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$icons" -o "$app/Contents/Resources/NewYorkRoute.icns"
cp "$src/Info.plist" "$app/Contents/Info.plist"
cp "$src/route.py" "$src/clash_route.py" "$app/Contents/Resources/"
xcrun swiftc -O -target "$(uname -m)-apple-macos14.0" -framework AppKit "$src/Menu.swift" -o "$app/Contents/MacOS/CodexNY"
codesign --force --sign - "$app"
codesign --verify --strict "$app"
mv "$app" "$output/$name"
echo "$output/$name"
