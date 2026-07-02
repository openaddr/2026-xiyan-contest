#!/bin/bash
# 打包提交用 ZIP（macOS/Linux 入口）：实际逻辑在 package.py
set -e
cd "$(dirname "$0")"
python3 package.py
