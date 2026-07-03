#!/usr/bin/env python3
"""打包提交用 ZIP（跨平台，Windows/macOS/Linux 均可）。

任务书 10.1 要求：ZIP 根目录直接包含可执行的 start.sh。
Windows 打包的两个坑，这里都处理：
  1. start.sh 若带 CRLF，Linux 平台 bash 直接起不来 —— 强制归一化为 LF；
  2. Windows 原生压缩不保存 Unix 可执行位 —— 直接在 zip 条目里写入 0755。
"""
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))   # 仓库根目录
CLIENT = os.path.join(HERE, "client")               # 客户端源码目录


def _build_version():
    """从 client/lychee/version.py 读 BUILD_VERSION（文件解析，避免 import 副作用）。"""
    try:
        with open(os.path.join(CLIENT, "lychee", "version.py"), encoding="utf-8") as f:
            m = re.search(r'^BUILD_VERSION\s*=\s*["\']([^"\']+)["\']', f.read(), re.M)
        if m:
            return m.group(1)
    except OSError:
        pass
    return None


# 文件名带版本号（如 gameclient-3.13-convergence.zip）；读不到版本则回退原名
_ver = _build_version()
OUT_NAME = f"gameclient-{_ver}.zip" if _ver else "gameclient.zip"
OUT = os.path.join(HERE, "dist", OUT_NAME)

# (打包路径[zip内相对根目录], 是否脚本[LF+755])
INCLUDE_FILES = [
    ("start.sh", True),
    ("main.py", False),
    ("README.md", False),
]
INCLUDE_DIRS = ["lychee", "lychee_basic_client"]

EXEC_ATTR = 0o755 << 16      # zip external_attr 的 Unix 权限位
NORM_ATTR = 0o644 << 16


def add_file(zf, rel_path, is_script):
    src = os.path.join(CLIENT, rel_path)
    with open(src, "rb") as f:
        data = f.read()
    if is_script:
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")  # 强制 LF
    info = zipfile.ZipInfo(rel_path.replace(os.sep, "/"))
    info.external_attr = EXEC_ATTR if is_script else NORM_ATTR
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, data)
    print(f"  + {rel_path}{'  (LF, 755)' if is_script else ''}")


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    if os.path.exists(OUT):
        os.remove(OUT)

    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, is_script in INCLUDE_FILES:
            add_file(zf, rel, is_script)
        for d in INCLUDE_DIRS:
            for root, dirs, files in os.walk(os.path.join(CLIENT, d)):
                dirs[:] = [x for x in dirs if x != "__pycache__"]
                for fn in sorted(files):
                    if fn.endswith(".pyc"):
                        continue
                    rel = os.path.relpath(os.path.join(root, fn), CLIENT)
                    add_file(zf, rel, fn.endswith(".sh"))

    # 自检：start.sh 在根目录、LF、可执行位；并打印构建版本防止打旧包
    with zipfile.ZipFile(OUT) as zf:
        names = zf.namelist()
        assert "start.sh" in names, "start.sh 必须在 ZIP 根目录"
        data = zf.read("start.sh")
        assert b"\r" not in data, "start.sh 仍含 CR，LF 归一化失败"
        mode = (zf.getinfo("start.sh").external_attr >> 16) & 0o777
        assert mode == 0o755, f"start.sh 权限异常: {oct(mode)}"
        ver = "UNKNOWN"
        for line in zf.read("lychee/version.py").decode("utf-8").splitlines():
            if line.startswith("BUILD_VERSION"):
                ver = line.split("=", 1)[1].strip().strip('"')
        print(f"\n自检通过: start.sh 位于根目录 / LF / 权限 {oct(mode)}")
        print(f"*** 构建版本: {ver} ***  (对局日志开头应出现同样版本号)")
        print(f"打包完成: {OUT}  ({len(names)} 个文件)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
