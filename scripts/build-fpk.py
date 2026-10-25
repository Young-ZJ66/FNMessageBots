#!/usr/bin/env python3
"""跨平台 FPK 构建脚本（替代上游的 bash 版 build-fpk.sh）。

在独立临时目录组装 payload，再调用 fnpack 打包；源码、依赖与资源
均使用主仓库当前版本。Windows / Linux / macOS 均可运行（fnpack 二进制
按平台选择）。

用法（项目根目录）：
    python scripts/build-fpk.py                    # 自动探测 .fnpack/ 下的 fnpack
    python scripts/build-fpk.py --fnpack /path/to/fnpack
    python scripts/build-fpk.py --keep             # 保留临时目录便于排查

产物：dist/FnMessageBot-<version>.fpk（+ .sha256 校验文件）
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FPK_DIR = ROOT / "fpk"
DIST = ROOT / "dist"

# 源码复制到包内时排除的路径（与上游 build-fpk.sh 的 rsync 排除项一致）
CODE_EXCLUDES = {"__pycache__", "data", ".DS_Store"}
EXT_EXCLUDES = {".pyc", ".pyo", ".pyd"}


def find_fnpack(explicit: str | None) -> str:
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)
        found = shutil.which(explicit)
        if found:
            return found
        raise SystemExit(f"错误: fnpack 不存在于 {explicit}")

    # 1) 项目 .fnpack/ 目录（按平台命名，如 fnpack-1.2.3-windows-amd64）
    dot = ROOT / ".fnpack"
    if dot.is_dir():
        for child in sorted(dot.iterdir()):
            name = child.name.lower()
            if child.is_file() and name.startswith("fnpack") and (
                name.endswith(".exe")
                or name.startswith(("fnpack-1", "fnpack-2"))
                or "fnpack" == name
            ):
                if name.endswith(".exe") or ".sha256" not in name:
                    return str(child)
    # 2) PATH
    found = shutil.which("fnpack")
    if found:
        return found
    # 3) ~/.local/bin/fnpack（上游脚本的同位兜底）
    home_fn = Path.home() / ".local" / "bin" / "fnpack"
    if home_fn.is_file():
        return str(home_fn)
    raise SystemExit("错误: 未找到 fnpack（可用 --fnpack 指定路径，或放入 .fnpack/ 目录）")


def read_version() -> str:
    manifest = (FPK_DIR / "manifest").read_text(encoding="utf-8")
    m = re.search(r"^version\s*=\s*(\S+)\s*$", manifest, re.M)
    if not m:
        raise SystemExit("错误: fpk/manifest 中未找到 version")
    version = m.group(1)
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?", version):
        raise SystemExit(f"错误: fpk/manifest 版本无效: {version}")
    return version


def copy_tree(src: Path, dst: Path, *, excludes: set[str] = CODE_EXCLUDES) -> None:
    """递归复制，跳过 __pycache__/data 等目录与 .pyc 等后缀（等价 rsync 排除）。"""
    for item in src.iterdir():
        if item.name in excludes:
            continue
        target = dst / item.name
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            copy_tree(item, target, excludes=excludes)
        elif item.suffix.lower() in EXT_EXCLUDES or item.name == ".DS_Store":
            continue
        else:
            shutil.copy2(item, target)


def build_payload(payload: Path) -> Path:
    """组装 payload。Docker 型：fpk/ 脚手架原样（应用代码在 GHCR 镜像内，
    不需要打包 src）；镜像名已写死在 app/docker/docker-compose*.yaml。"""
    copy_tree(FPK_DIR, payload, excludes=CODE_EXCLUDES | {"src"})
    gi = payload / ".gitignore"
    if gi.exists():
        gi.unlink()
    return payload


def run_fnpack(fnpack: str, payload: Path, build_dir: Path) -> Path:
    """fnpack 把产物写到当前目录，因此 cwd 切到 build_dir。

    TMP 必须用正斜杠的原生绝对路径：fnpack(1.2.3, Go) 会把 TMP 里
    反斜杠+字母的组合（如 `\\f`）当转义序列吞掉，导致 mkdir 的路径
    缺段而 Access denied。Git Bash 传入的 /c/... 风格同样不行。
    """
    import os

    env = dict(os.environ)
    native_tmp = str(build_dir.resolve()).replace("\\", "/")
    env["TMP"] = env["TEMP"] = env["TMPDIR"] = native_tmp
    proc = subprocess.run(
        [fnpack, "build", "--directory", str(payload)],
        cwd=str(build_dir),
        env=env,
        capture_output=True,
        text=True,
    )
    print("[fnpack]", (proc.stdout or "").strip()[:500])
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit("错误: fnpack build 失败")
    pkg = build_dir / "FnMessageBot.fpk"
    if not pkg.is_file() or pkg.stat().st_size == 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit("错误: fnpack 未生成非空的 FnMessageBot.fpk")
    return pkg


def main() -> None:
    ap = argparse.ArgumentParser(description="构建 FnMessageBot FPK 安装包")
    ap.add_argument("--fnpack", help="fnpack 可执行文件路径（默认自动探测 .fnpack/ 与 PATH）")
    ap.add_argument("--keep", action="store_true", help="保留临时构建目录（排查用）")
    args = ap.parse_args()

    fnpack = find_fnpack(args.fnpack)
    version = read_version()
    print(f"fnpack : {fnpack}")
    print(f"version: {version}")

    # fnpack(1.2.3) 在部分系统的用户 Temp（C:\Users\...\Temp）下创建子目录会被
    # ACL 拒绝，且会把 TMP 里的 `\字母` 当转义吞掉；构建目录固定放在项目 dist/
    # 下（与源码同盘、正斜杠路径），规避这两类问题。
    DIST.mkdir(exist_ok=True)
    build_dir = Path(tempfile.mkdtemp(prefix="fnmb-fpk-", dir=str(DIST)))
    payload = build_dir / "payload"
    payload.mkdir(parents=True)
    try:
        build_payload(payload)
        pkg = run_fnpack(fnpack, payload, build_dir)

        DIST.mkdir(exist_ok=True)
        out = DIST / f"FnMessageBot-{version}.fpk"
        shutil.move(str(pkg), out)
        digest = hashlib.sha256(out.read_bytes()).hexdigest()
        (Path(str(out) + ".sha256")).write_text(f"{digest}  {out.name}\n", encoding="utf-8")
        size_mb = out.stat().st_size / 1024 / 1024
        print(f"已生成: {out}（{size_mb:.1f} MB）")
        print(f"校验文件: {out}.sha256")
    finally:
        if args.keep:
            print(f"保留临时目录: {build_dir}")
        else:
            shutil.rmtree(build_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
