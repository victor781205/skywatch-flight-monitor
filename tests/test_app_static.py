import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"
STYLE_PATH = ROOT / "style.css"


class AppStaticRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = APP_PATH.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source, filename=str(APP_PATH))
        cls.style = STYLE_PATH.read_text(encoding="utf-8")

    def _function(self, name: str) -> ast.FunctionDef:
        for node in self.tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail(f"missing function: {name}")

    def test_monitor_policy_editor_has_no_undefined_target_reference(self) -> None:
        function = self._function("page_monitors")
        target_loads = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id == "target"
        ]
        self.assertEqual([], target_loads)

        updates = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update_monitor_policies"
        ]
        self.assertEqual(1, len(updates))
        self.assertNotIn(
            "target_price",
            {keyword.arg for keyword in updates[0].keywords if keyword.arg},
        )

    def test_non_schedule_policy_save_preserves_next_run(self) -> None:
        function = self._function("_schedule_update_fields")
        function_source = ast.get_source_segment(self.source, function) or ""
        self.assertIn("schedule_type == current_type", function_source)
        self.assertIn("next_value == current_value", function_source)
        self.assertIn("return {},", function_source)
        page_source = ast.get_source_segment(self.source, self._function("page_monitors")) or ""
        self.assertIn("**schedule_updates", page_source)

    def test_streamlit_uses_width_api_only(self) -> None:
        self.assertNotIn("use_container_width", self.source)
        self.assertIn('width="stretch"', self.source)

    def test_mobile_navigation_is_single_row_and_scrollable(self) -> None:
        self.assertIn("flex-wrap: nowrap !important", self.style)
        self.assertIn("overflow-x: auto !important", self.style)
        self.assertIn("white-space: nowrap !important", self.style)
        self.assertIn("overflow: clip !important", self.style)
        self.assertIn("padding-bottom: 0 !important", self.style)
        for page in (
            "營運總覽",
            "搜尋與比較",
            "監控策略",
            "價格情報",
            "執行與通知",
            "系統設定",
            "備份與復原",
            "使用說明",
        ):
            self.assertIn(f'"{page}"', self.source)

    def test_all_dataframes_use_arrow_safe_adapter(self) -> None:
        calls = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "st"
            and node.func.attr == "dataframe"
        ]
        self.assertGreater(len(calls), 0)
        for call in calls:
            self.assertTrue(call.args, "st.dataframe must receive a frame")
            self.assertIsInstance(call.args[0], ast.Call)
            adapter = call.args[0]
            self.assertIsInstance(adapter.func, ast.Name)
            self.assertEqual("_arrow_safe_frame", adapter.func.id)


if __name__ == "__main__":
    unittest.main()
