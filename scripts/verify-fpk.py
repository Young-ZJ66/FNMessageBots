"""校验 FPK 产物结构与内容（不解压到工作目录，直接读 tar 流）。

用法：python scripts/verify-fpk.py [dist/FnMessageBot-<ver>.fpk]
默认取 dist/ 下最新的 .fpk。检查项：
- sha256 校验文件匹配
- 包内 cmd/fnmessagebots/src 与主仓库 src 一致（抽查明细）
- 包内默认 config.json 无已移除渠道键（bark/magic_push/meow/wecom/smtp/pushplus）
- 关键文件存在：manifest / cmd/main / app/ui/config / wizard/install
- manifest version 与文件名一致
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


def latest_package() -> Path:
    pkgs = sorted(DIST.glob("FnMessageBot-*.fpk"))
    if not pkgs:
        raise SystemExit("错误: dist/ 下没有 .fpk 产物")
    return pkgs[-1]


def main() -> None:
    pkg = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_package()
    print(f"校验: {pkg.name}")

    # 1) sha256
    checksum = Path(str(pkg) + ".sha256").read_text(encoding="utf-8").split()
    expect = [hashlib.sha256(pkg.read_bytes()).hexdigest(), pkg.name]
    assert checksum == expect, f"SHA-256 不匹配: {checksum} != {expect}"
    print("  [OK] SHA-256 校验")

    # 2) 读包内容。fnpack 把 app/ 与 config/ 收进 app.tgz 内嵌归档，需展开
    with tarfile.open(pkg, "r:*") as tf:
        members = {str(PurePosixPath(m.name)): m for m in tf.getmembers() if m.isfile()}
        blobs = {}
        for name, m in members.items():
            f = tf.extractfile(m)
            if f is not None:
                blobs[name] = f.read()
        if "app.tgz" in blobs:
            inner = tarfile.open(fileobj=io.BytesIO(blobs["app.tgz"]), mode="r:gz")
            for m in inner.getmembers():
                if not m.isfile():
                    continue
                f = inner.extractfile(m)
                if f is not None:
                    blobs[f"app/{m.name}"] = f.read()

    # 3) 关键文件
    required = [
        "manifest",
        "cmd/main",
        "cmd/install_callback",
        "cmd/upgrade_callback",
        "config/privilege",
        "app/ui/config",
        "wizard/install",
        "cmd/fnmessagebots/requirements.txt",
        "cmd/fnmessagebots/src/main.py",
    ]
    for name in required:
        assert name in blobs, f"包内缺少关键文件: {name}"
    print(f"  [OK] {len(required)} 个关键文件齐全")

    # 4) manifest version 与文件名一致
    manifest = blobs["manifest"].decode("utf-8")
    mver = re.search(r"^version\s*=\s*(\S+)", manifest, re.M).group(1)
    fname_ver = re.search(r"FnMessageBot-([0-9.]+)\.fpk", pkg.name).group(1)
    assert mver == fname_ver, f"manifest 版本 {mver} 与文件名 {fname_ver} 不一致"
    print(f"  [OK] 版本一致: {mver}")

    # 5) 默认 config 无已移除渠道键
    cfg = json.loads(blobs["cmd/fnmessagebots/config/config.json"].decode("utf-8"))
    bad = REMOVED_CHANNEL_KEYS & set(cfg)
    assert not bad, f"默认配置仍含已移除渠道键: {bad}"
    supported = {"wechat_webhook_url", "dingtalk_webhook_url", "feishu_webhook_url", "webhook_params"}
    assert supported & set(cfg), "默认配置缺少任一支持渠道字段"
    print("  [OK] 默认配置为四渠道口径")

    # 6) 包内 src 与主仓库一致（全部文件对比）
    src_root = ROOT / "src"
    repo_files = {
        str(p.relative_to(ROOT)).replace("\\", "/"): p.read_bytes()
        for p in src_root.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and p.suffix not in (".pyc", ".pyo", ".pyd")
        and "data" not in p.parts
    }
    pkg_src = {
        n: b for n, b in blobs.items() if n.startswith("cmd/fnmessagebots/src/")
    }
    norm_pkg = {n.removeprefix("cmd/fnmessagebots/"): b for n, b in pkg_src.items()}
    missing = set(repo_files) - set(norm_pkg)
    extra = set(norm_pkg) - set(repo_files)
    assert not missing, f"包内 src 缺文件: {sorted(missing)[:5]}"
    assert not extra, f"包内 src 多文件: {sorted(extra)[:5]}"
    diff = [n for n in repo_files if repo_files[n] != norm_pkg[n]]
    assert not diff, f"包内 src 与主仓库不一致: {diff[:5]}"
    print(f"  [OK] src/ 与主仓库一致（{len(repo_files)} 个文件）")

    # 7) assets 已带入
    n_assets = sum(1 for n in blobs if n.startswith("cmd/fnmessagebots/assets/"))
    assert n_assets > 0, "包内缺少 assets/"
    print(f"  [OK] assets/ {n_assets} 个文件")

    # 8) bash 脚本必须 LF 行尾（CRLF 在 NAS 上会 bad interpreter）
    for name in ("cmd/main", "cmd/install_callback", "cmd/upgrade_callback", "cmd/ui_port", "cmd/data_permissions"):
        assert blobs[name].count(b"\r\n") == 0, f"{name} 含 CRLF 行尾，NAS 上将无法执行"
    print("  [OK] bash 脚本均为 LF 行尾")

    total = len(blobs)
    print(f"\n通过：{pkg.name}（{total} 个文件，{pkg.stat().st_size/1024/1024:.1f} MB）")


if __name__ == "__main__":
    main()
