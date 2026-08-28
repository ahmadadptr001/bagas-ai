"""Textual widgets untuk antarmuka bagas-ai.

Widget-widget ini menggantikan komponen prompt_toolkit + Rich Live yang
sebelumnya dibangun secara manual di ``interfaces/cli.py``. Setiap widget
bertanggung jawab atas satu area visual dan berkomunikasi via Textual
message passing.
"""
from __future__ import annotations

from .status_bar import StatusBar
from .chat_box import ChatBox
from .message_list import MessageList
from .plan_panel import PlanPanel
from .plan_sidebar import PlanSidebar
from .image_preview import ImagePreview
from .progress_bar import TurnProgressBar
from .logo import LogoWidget
from .streaming_preview import StreamingPreview
from .thinking_block import ThinkingBlock
from .queue_strip import QueueStrip
from .modal_screens import (SelectScreen, MultiSelectScreen, ConfirmScreen,
                            TextPromptScreen, ThemeScreen)

__all__ = [
    "StatusBar",
    "ChatBox",
    "MessageList",
    "PlanPanel",
    "PlanSidebar",
    "ImagePreview",
    "TurnProgressBar",
    "LogoWidget",
    "StreamingPreview",
    "ThinkingBlock",
    "QueueStrip",
    "SelectScreen",
    "MultiSelectScreen",
    "ConfirmScreen",
    "TextPromptScreen",
    "ThemeScreen",
]
