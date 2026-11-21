#!/bin/sh
# Builds Ramu Listen.app next to this script. Run: sh listen/build.sh && open "listen/Ramu Listen.app"
set -e
cd "$(dirname "$0")"
APP="Ramu Listen.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp Info.plist "$APP/Contents/Info.plist"
swiftc -O main.swift -o "$APP/Contents/MacOS/ramu-listen" -framework Speech -framework AVFoundation -framework ApplicationServices
codesign --force --sign - "$APP"
echo "built $APP"
