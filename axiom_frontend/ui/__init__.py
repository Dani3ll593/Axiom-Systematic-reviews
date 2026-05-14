"""ui package — screens and reusable components."""
from ui.components import (
    inject_css, render_header, render_chips, render_footer, render_mock_badge,
)
from ui.screen_config import render_screen_config
from ui.screen_progress import render_screen_progress
from ui.screen_results import render_screen_results

__all__ = [
    "inject_css", "render_header", "render_chips", "render_footer", "render_mock_badge",
    "render_screen_config", "render_screen_progress", "render_screen_results",
]
