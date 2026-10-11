"""仓库卫生检查：防止已删除的上游/捐赠内容与不该提交的文件回归。

这些断言很「琐碎」，但正是它们最容易被后续改动无意破坏：
- 模板里引用的本地图片/链接必须真实存在（删文件时容易漏掉引用）
- 文档里引用的图片必须存在且没有孤儿文件
- 已移除的「支持作者 / 捐赠」页面与路由不得复活
- Agent / IDE 本地文件不得出现在仓库里
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT / "src"), str(_ROOT / "tests" / "_support")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import stub_deps  # noqa: E402

stub_deps.install()

TEMPLATES = _ROOT / "src" / "web" / "templates"
DOCS_DIRS = [_ROOT / "docs", _ROOT / "docs" / "channels"]
README = _ROOT / "README.md"

# 上游/捐赠相关内容的关键词：不应出现在界面与文档里
FORBIDDEN_BRANDING = (
    "Sunanang",
    "sunanang",
    "star-history",
    "捐赠",
    "打赏",
    "赞赏",
    "支持作者",
    "wechat_pay",
    "ali_pay",
)

# 允许保留上游版权声明的位置（MIT 要求保留，见 README 许可证小节）
BRANDING_ALLOWLIST = {_ROOT / "LICENSE", _ROOT / "README.md"}


class TestNoForbiddenBranding(unittest.TestCase):
    def test_templates_have_no_upstream_branding(self):
        for tpl in sorted(TEMPLATES.glob("*.html")):
            text = tpl.read_text(encoding="utf-8")
            for word in FORBIDDEN_BRANDING:
                with self.subTest(template=tpl.name, word=word):
                    self.assertNotIn(
                        word, text, f"{tpl.name} 仍含「{word}」——界面不应出现上游/捐赠内容"
                    )

    def test_docs_have_no_upstream_branding(self):
        for doc in sorted(_ROOT.glob("docs/**/*.md")):
            if doc in BRANDING_ALLOWLIST:
                continue
            text = doc.read_text(encoding="utf-8")
            for word in FORBIDDEN_BRANDING:
                with self.subTest(doc=doc.name, word=word):
                    self.assertNotIn(word, text, f"{doc} 仍含「{word}」")

    def test_readme_keeps_license_attribution_only(self):
        """README 只应在「许可证」小节保留上游署名（MIT 强制要求）。"""
        text = README.read_text(encoding="utf-8")
        # 捐赠与 Star History 必须彻底移除
        for word in ("捐赠", "打赏", "star-history"):
            self.assertNotIn(word, text, f"README 仍含「{word}」")
        # 署名只允许出现在许可证小节之后
        lic_pos = text.find("## 许可证")
        self.assertGreater(lic_pos, 0, "应保留「许可证」小节")
        for m in re.finditer(r"Sunanang", text):
            self.assertGreater(
                m.start(), lic_pos, "上游署名只应出现在许可证小节内"
            )

    def test_readme_documents_fork_and_license_obligation(self):
        """必须说明本仓库是 fork、且署名保留是 MIT 义务（否则后人会误删）。"""
        text = README.read_text(encoding="utf-8")
        self.assertIn("fork", text)
        self.assertIn("MIT", text)
        self.assertIn("版权声明", text)

    def test_license_file_keeps_upstream_copyright(self):
        text = (_ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2024 Sunanang", text, "MIT 要求保留原始版权声明，不得删除")


class TestSupportFeatureRemoved(unittest.TestCase):
    def test_support_template_gone(self):
        self.assertFalse((TEMPLATES / "support.html").exists(), "support.html 应已删除")

    def test_ui_app_has_no_support_route(self):
        src = (_ROOT / "src" / "web" / "ui_app.py").read_text(encoding="utf-8")
        for bad in ('"/support"', "/support/img/", "support.html", "SUPPORT_QR"):
            with self.subTest(token=bad):
                self.assertNotIn(bad, src, f"ui_app.py 仍有支持作者页残留：{bad}")

    def test_app_paths_has_no_qr_constants(self):
        src = (_ROOT / "src" / "web" / "app_paths.py").read_text(encoding="utf-8")
        self.assertNotIn("SUPPORT_QR", src)

    def test_no_donation_assets(self):
        for rel in (
            "assets/icons/wechat_pay.jpg",
            "assets/icons/ali_pay.jpg",
            "docs/images/wechat_pay.jpg",
            "docs/images/ali_pay.jpg",
            "docs/donations.md",
        ):
            with self.subTest(path=rel):
                self.assertFalse((_ROOT / rel).exists(), f"{rel} 应已删除")


class TestNoAgentFiles(unittest.TestCase):
    """Agent / IDE 本地文件不应出现在仓库里（已加入 .gitignore）。"""

    AGENT_PATHS = (
        ".claude",
        ".cursor",
        ".windsurf",
        ".continue",
        "AGENTS.md",
        "CLAUDE.md",
        "GEMINI.md",
        ".mcp.json",
        "mcp.json",
    )

    def test_not_present_on_disk(self):
        for rel in self.AGENT_PATHS:
            with self.subTest(path=rel):
                self.assertFalse((_ROOT / rel).exists(), f"{rel} 不该出现在仓库里")

    def test_gitignore_covers_them(self):
        ignores = (_ROOT / ".gitignore").read_text(encoding="utf-8")
        for token in (".claude/", ".cursor/", "AGENTS.md", ".mcp.json"):
            with self.subTest(token=token):
                self.assertIn(token, ignores, f".gitignore 应忽略 {token}")

    def test_dockerignore_covers_them(self):
        ignores = (_ROOT / ".dockerignore").read_text(encoding="utf-8")
        for token in (".claude", ".cursor", "AGENTS.md"):
            with self.subTest(token=token):
                self.assertIn(token, ignores, f".dockerignore 应排除 {token}")


class TestLocalReferencesResolve(unittest.TestCase):
    """模板与文档里引用的本地图片/资源必须存在（删文件时最容易漏）。"""

    def _local_images(self, text: str) -> set[str]:
        found: set[str] = set()
        for m in re.finditer(r'(?:src|srcset)="([^"{}]+\.(?:png|jpg|jpeg|svg|gif))"', text):
            found.add(m.group(1))
        for m in re.finditer(r"!\[[^\]]*\]\(([^)]+\.(?:png|jpg|jpeg|svg|gif))\)", text):
            found.add(m.group(1))
        return {p for p in found if not p.startswith(("http://", "https://", "/", "data:"))}

    def test_template_images_exist(self):
        for tpl in sorted(TEMPLATES.glob("*.html")):
            text = tpl.read_text(encoding="utf-8")
            for rel in self._local_images(text):
                # 模板里的路径相对站点根（/app），对应仓库内 src/web/templates 之外的部分
                candidate = _ROOT / rel.replace("../", "")
                with self.subTest(template=tpl.name, image=rel):
                    self.assertTrue(
                        candidate.exists(),
                        f"{tpl.name} 引用了不存在的资源 {rel}（查找于 {candidate}）",
                    )

    def test_markdown_images_exist(self):
        for doc in sorted(list(_ROOT.glob("docs/**/*.md")) + [README]):
            text = doc.read_text(encoding="utf-8")
            for rel in self._local_images(text):
                candidate = (doc.parent / rel).resolve()
                with self.subTest(doc=doc.name, image=rel):
                    self.assertTrue(candidate.exists(), f"{doc} 引用了不存在的图片 {rel}")

    def test_no_orphan_screenshots(self):
        """docs/channels/images 下不应有没被任何文档引用的图片。

        注意：只按「完整文件名」判断会误伤——上游 bark.md 引用的是 `bark-step1.png`，
        而磁盘上的文件是 `bark-step1.jpg`（文档写错扩展名）。为此增加「同主干不同扩展名」
        的识别：这种文件不是孤儿，而是文档写错了，应改文档而不是删图片。
        """
        images_dir = _ROOT / "docs" / "channels" / "images"
        if not images_dir.is_dir():
            self.skipTest("无图片目录")
        text = "\n".join(
            p.read_text(encoding="utf-8")
            for p in list(_ROOT.glob("docs/**/*.md")) + [README]
        )
        referenced = set(re.findall(r"[\[(/]([A-Za-z0-9._\-]+\.(?:png|jpg|jpeg|svg|gif))", text))
        stems_referenced = {Path(n).stem for n in referenced}

        orphans, mismatched = [], []
        for p in sorted(images_dir.glob("*")):
            if p.name in referenced:
                continue
            if p.stem in stems_referenced:
                mismatched.append(p.name)
            else:
                orphans.append(p.name)

        self.assertEqual(
            mismatched,
            [],
            "以下图片被文档引用但扩展名不一致（应改文档的引用，而不是删图片）：" + str(mismatched),
        )
        self.assertEqual(orphans, [], f"以下图片未被任何文档引用，应删除：{orphans}")

    def test_no_dangling_links_to_deleted_files(self):
        removed = (
            "donations.md",
            "publish-amd64.sh",
            "publish-arm64.sh",
            "magicpush.md",
            "wecom-app.md",
            "docker-manifest.yml",
            "merge-manifest.yml",
            "support.html",
        )
        for doc in sorted(list(_ROOT.glob("docs/**/*.md")) + [README]):
            if doc.name == "LICENSE":
                continue
            text = doc.read_text(encoding="utf-8")
            for token in removed:
                with self.subTest(doc=doc.name, token=token):
                    self.assertNotIn(token, text, f"{doc} 仍指向已删除的 {token}")


class TestWorkflowsSane(unittest.TestCase):
    def test_only_expected_workflows(self):
        wf_dir = _ROOT / ".github" / "workflows"
        names = sorted(p.name for p in wf_dir.glob("*.yml"))
        self.assertEqual(names, ["build-image.yml"], f"workflows 应只剩构建流程，实际：{names}")

    def test_build_workflow_lowercases_image_name(self):
        """GHCR 镜像名必须全小写；仓库名含大写时不能直接用 github.repository。"""
        src = (_ROOT / ".github" / "workflows" / "build-image.yml").read_text(encoding="utf-8")
        self.assertIn("${GITHUB_REPOSITORY,,}", src, "必须把仓库名转为小写")
        self.assertIn("packages: write", src, "推 GHCR 需要 packages: write")

    def test_removed_publish_scripts(self):
        for rel in ("publish-amd64.sh", "publish-arm64.sh"):
            with self.subTest(script=rel):
                self.assertFalse((_ROOT / rel).exists(), f"{rel} 应已删除（由 Actions 构建取代）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
