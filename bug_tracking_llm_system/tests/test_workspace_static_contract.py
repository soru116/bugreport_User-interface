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

    def test_semi_automatic_mode_can_select_another_patch_candidate(self) -> None:
        app = (PROJECT_ROOT / "workspace" / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('state.automationMode !== "automatic"', app)
        self.assertIn('data-action="select-patch"', app)
        self.assertIn('case "select-patch":', app)
        self.assertIn('`${path}/select-patch`', app)
        self.assertIn("提交訊息也會依此補丁重新產生", app)

    def test_semi_automatic_mode_can_select_another_localization_candidate(self) -> None:
        app = (PROJECT_ROOT / "workspace" / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('data-action="select-localization"', app)
        self.assertIn('case "select-localization":', app)
        self.assertIn('`${path}/select-localization`', app)
        self.assertIn("舊補丁與提交訊息會被清除", app)


if __name__ == "__main__":
    unittest.main()
