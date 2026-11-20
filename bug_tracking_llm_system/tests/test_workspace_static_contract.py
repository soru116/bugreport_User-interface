from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WorkspaceStaticContractTests(unittest.TestCase):
    def test_patch_draft_uses_success_node_but_no_patch_uses_warning_node(self) -> None:
        styles = (PROJECT_ROOT / "workspace" / "static" / "styles.css").read_text(encoding="utf-8")

        self.assertIn(
            '.track li[data-optional][data-s="draft"] .node { background: var(--done);',
            styles,
        )
        self.assertIn(
            '.track li[data-optional][data-s="draft"] .node::before { content: "✓"; }',
            styles,
        )
        self.assertIn(
            '.track li[data-s="draft"]:has(+ li[data-optional])::after,',
            styles,
        )
        self.assertIn(
            '.track li[data-optional][data-s="none"] .node { background: var(--fallback-soft);',
            styles,
        )

        app = (PROJECT_ROOT / "workspace" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('["generated", "draft", "resolved"].includes(patchState)', app)
        self.assertIn('commitRegenerate(r, "current", "產生提交訊息")', app)


if __name__ == "__main__":
    unittest.main()
