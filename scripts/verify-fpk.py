"""校验 FPK 产物结构与内容（不解压到工作目录，直接读 tar 流）。

用法：python scripts/verify-fpk.py [dist/FnMessageBot-<ver>[-native].fpk]
默认取 dist/ 下最新的 .fpk。形态按包内容自动判定：
- native 型（含 cmd/fnmessagebots/src/main.py）：src 与主仓库逐文件一致、
  四渠道默认配置、LF 行尾、resource 不声明 docker-project。
- Docker 型（含 app/docker/docker-compose.yaml）：docker-project 声明、
  GHCR 镜像名、四渠道默认配置、LF 行尾。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import tarfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

REMOVED_CHANNEL_KEYS = {
    "bark_url", "bark_icon", "pushplus_params", "smtp_params",
    "meow_params", "magic_push_params", "wecom_app_params",
}
SUPPORTED_CHANNEL_KEYS = {
    "wechat_webhook_url", "dingtalk_webhook_url", "feishu_webhook_url", "webhook_params",
}
VERSION_RE = re.compile(r"FnMessageBot-([0-9.]+)(?:-native|-docker)?\.fpk")


def latest_package() -> Path:
    pkgs = sorted(DIST.glob("FnMessageBot-*.fpk"))
    if not pkgs:
        raise SystemExit("错误: dist/ 下没有 .fpk 产物")
    return pkgs[-1]


def load_blobs(pkg: Path) -> dict[str, bytes]:
    """fnpack 把 app/ 与 config/ 收进 app.tgz 内嵌归档，需展开。"""
    with tarfile.open(pkg, "r:*") as tf:
        blobs: dict[str, bytes] = {}
        for m in tf.getmembers():
            if not m.isfile():
                continue
            f = tf.extractfile(m)
            if f is not None:
                blobs[str(PurePosixPath(m.name))] = f.read()
    if "app.tgz" in blobs:
        inner = tarfile.open(fileobj=io.BytesIO(blobs["app.tgz"]), mode="r:gz")
        for m in inner.getmembers():
            if not m.isfile():
                continue
            f = inner.extractfile(m)
            if f is not None:
                blobs[f"app/{m.name}"] = f.read()
    return blobs


def check_channels(cfg: dict) -> None:
    bad = REMOVED_CHANNEL_KEYS & set(cfg)
    assert not bad, f"默认配置仍含已移除渠道键: {bad}"
    assert SUPPORTED_CHANNEL_KEYS & set(cfg), "默认配置缺少任一支持渠道字段"
    print("  [OK] 默认配置为四渠道口径")


def check_lf(blobs: dict[str, bytes], names: list[str]) -> None:
    for name in names:
        assert name in blobs, f"包内缺少脚本: {name}"
        assert blobs[name].count(b"\r\n") == 0, f"{name} 含 CRLF 行尾，NAS 上将无法执行"
    print("  [OK] bash 脚本均为 LF 行尾")


def check_required(blobs: dict[str, bytes], required: list[str]) -> None:
    for name in required:
        assert name in blobs, f"包内缺少关键文件: {name}"
    print(f"  [OK] {len(required)} 个关键文件齐全")


def verify_native(blobs: dict[str, bytes]) -> None:
    check_required(blobs, [
        "manifest", "cmd/main", "cmd/install_callback", "cmd/upgrade_callback",
        "cmd/ui_port", "cmd/data_permissions", "config/privilege",
        "app/ui/config", "wizard/install",
        "cmd/fnmessagebots/requirements.txt",
        "cmd/fnmessagebots/src/main.py",
        "cmd/fnmessagebots/config/config.json",
    ])

    resource = json.loads(blobs["config/resource"].decode("utf-8"))
    assert "docker-project" not in resource, "native 型不应声明 docker-project"
    print("  [OK] resource 无 docker-project（native 直跑系统 Python，不建容器）")

    check_channels(json.loads(blobs["cmd/fnmessagebots/config/config.json"].decode("utf-8")))

    repo_files = {
        str(p.relative_to(ROOT)).replace("\\", "/"): p.read_bytes()
        for p in (ROOT / "src").rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and p.suffix not in (".pyc", ".pyo", ".pyd")
        and "data" not in p.parts
    }
    pkg_src = {
        n.removeprefix("cmd/fnmessagebots/"): b
        for n, b in blobs.items()
        if n.startswith("cmd/fnmessagebots/src/")
    }
    missing = set(repo_files) - set(pkg_src)
    extra = set(pkg_src) - set(repo_files)
    assert not missing, f"包内 src 缺文件: {sorted(missing)[:5]}"
    assert not extra, f"包内 src 多文件: {sorted(extra)[:5]}"
    diff = [n for n in repo_files if repo_files[n] != pkg_src[n]]
    assert not diff, f"包内 src 与主仓库不一致: {diff[:5]}"
    print(f"  [OK] src/ 与主仓库一致（{len(repo_files)} 个文件）")

    n_assets = sum(1 for n in blobs if n.startswith("cmd/fnmessagebots/assets/"))
    assert n_assets > 0, "包内缺少 assets/"
    print(f"  [OK] assets/ {n_assets} 个文件")

    check_lf(blobs, [
        "cmd/main", "cmd/install_callback", "cmd/upgrade_callback",
        "cmd/ui_port", "cmd/data_permissions",
    ])


def verify_docker(blobs: dict[str, bytes]) -> None:
    check_required(blobs, [
        "manifest", "cmd/main", "cmd/install_callback", "cmd/upgrade_callback",
        "cmd/lib_vol_override.sh", "config/privilege", "app/ui/config", "wizard/install",
        "app/docker/docker-compose.yaml", "app/docker/docker-compose.base.yaml",
        "app/docker/config/config.json",
    ])

    resource = json.loads(blobs["config/resource"].decode("utf-8"))
    assert "docker-project" in resource, "config/resource 缺少 docker-project 声明"
    print("  [OK] resource 为 Docker 型（安装即建容器）")

    for name in ("app/docker/docker-compose.yaml", "app/docker/docker-compose.base.yaml"):
        compose = blobs[name].decode("utf-8")
        assert "ghcr.io/young-zj66/fnmessagebots" in compose, f"{name} 镜像名不是本仓库 GHCR"
        assert "sunanang" not in compose, f"{name} 仍指向上游镜像"
    print("  [OK] compose 镜像为 ghcr.io/young-zj66/fnmessagebots")

    check_channels(json.loads(blobs["app/docker/config/config.json"].decode("utf-8")))

    check_lf(blobs, ["cmd/main", "cmd/install_callback", "cmd/upgrade_callback", "cmd/lib_vol_override.sh"])


def main() -> None:
    pkg = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_package()
    print(f"校验: {pkg.name}")

    # 1) sha256
    checksum = Path(str(pkg) + ".sha256").read_text(encoding="utf-8").split()
    expect = [hashlib.sha256(pkg.read_bytes()).hexdigest(), pkg.name]
    assert checksum == expect, f"SHA-256 不匹配: {checksum} != {expect}"
    print("  [OK] SHA-256 校验")

    blobs = load_blobs(pkg)

    # 2) manifest version 与文件名一致
    manifest = blobs["manifest"].decode("utf-8")
    mver = re.search(r"^version\s*=\s*(\S+)", manifest, re.M).group(1)
    mname = VERSION_RE.search(pkg.name)
    assert mname, f"文件名不符合 FnMessageBot-<ver>[-native].fpk: {pkg.name}"
    assert mver == mname.group(1), f"manifest 版本 {mver} 与文件名 {mname.group(1)} 不一致"
    print(f"  [OK] 版本一致: {mver}")

    # 3) 形态判定与分形态校验
    is_native = "cmd/fnmessagebots/src/main.py" in blobs
    print(f"  [OK] 形态: {'native（包内嵌代码）' if is_native else 'docker（代码在 GHCR 镜像内）'}")
    if is_native:
        verify_native(blobs)
    else:
        verify_docker(blobs)

    total = len(blobs)
    print(f"\n通过：{pkg.name}（{total} 个文件，{pkg.stat().st_size/1024/1024:.1f} MB）")


if __name__ == "__main__":
    main()
