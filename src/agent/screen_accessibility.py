"""Pembacaan teks UI Automation dengan batas waktu dan satu worker maksimum."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import sys
import threading
import time


@dataclass(frozen=True)
class AccessibleText:
    text: str = ""
    complete: bool = False


_lock = threading.Lock()
_busy = False


def _read(hwnd: int, budget: float = .30) -> AccessibleText:
    import comtypes
    import comtypes.client

    comtypes.CoInitialize()
    automation = root = walker = node = child = pattern = None
    nodes = deque()
    try:
        module = comtypes.client.GetModule("UIAutomationCore.dll")
        automation = comtypes.client.CreateObject(module.CUIAutomation, interface=module.IUIAutomation)
        root = automation.ElementFromHandle(hwnd)
        walker = automation.ControlViewWalker
        nodes = deque([(root, 0)])
        labels, seen = [], set()
        deadline = time.monotonic() + budget
        visited = 0
        while nodes and visited < 160 and time.monotonic() < deadline:
            node, depth = nodes.popleft()
            visited += 1
            try:
                if node.CurrentIsOffscreen or node.CurrentIsPassword:
                    continue
                label = " ".join(str(node.CurrentName or "").split())[:1200]
                if label and label not in seen:
                    labels.append(label)
                    seen.add(label)
                # Hanya pola dokumen yang dapat menggantikan OCR seluruh
                # window. Nilai address bar/search box bukan isi halaman.
                if node.CurrentControlType == 50030:
                    try:
                        pattern = node.GetCurrentPattern(10014).QueryInterface(module.IUIAutomationTextPattern)
                        text = pattern.DocumentRange.GetText(8000).strip()
                        if len(text) >= 12:
                            return AccessibleText(text, True)
                    except (OSError, AttributeError, comtypes.COMError):
                        pass
                if depth < 5:
                    child = walker.GetFirstChildElement(node)
                    siblings = 0
                    while child and siblings < 40 and len(nodes) + visited < 160 and time.monotonic() < deadline:
                        nodes.append((child, depth + 1))
                        child = walker.GetNextSiblingElement(child)
                        siblings += 1
            except (OSError, AttributeError, comtypes.COMError):
                continue
        return AccessibleText("\n".join(labels)[:8000], False)
    finally:
        nodes.clear()
        pattern = child = node = walker = root = automation = None
        comtypes.CoUninitialize()


def read_accessible_text(hwnd: int, timeout: float = .4) -> AccessibleText:
    """Provider yang macet tidak boleh menggandakan thread atau menahan chat."""
    global _busy
    if sys.platform != "win32" or not hwnd:
        return AccessibleText()
    with _lock:
        if _busy:
            return AccessibleText()
        _busy = True
    ready = threading.Event()
    result = []

    def work():
        global _busy
        try:
            result.append(_read(hwnd))
        except Exception:
            result.append(AccessibleText())
        finally:
            with _lock:
                _busy = False
            ready.set()

    threading.Thread(target=work, daemon=True, name="bagasai-screen-text").start()
    if ready.wait(max(0, timeout)):
        return result[0]
    return AccessibleText()
