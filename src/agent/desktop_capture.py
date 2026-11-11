"""Capture satu sumber, tanpa loop FPS atau rekaman latar."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import sys
import threading
import time

from . import termux


@dataclass(frozen=True)
class WindowTarget:
    hwnd: int
    pid: int
    title: str


@dataclass(frozen=True)
class CaptureInfo:
    key: tuple
    label: str
    hwnd: int = 0
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    backend: str = "Pillow"
    captured_at: float = 0


_state = threading.RLock()
_capture_lock = threading.Lock()
_print_lock = threading.Lock()
_preview_print_lock = threading.Lock()
_target: WindowTarget | None = None
_generation = 0
_camera = None
_release_pending = False
_last_info = CaptureInfo((0,), "Monitor utama")


@contextmanager
def _physical_pixels():
    previous, function = None, None
    if sys.platform == "win32":
        import ctypes
        try:
            function = ctypes.WinDLL("user32").SetThreadDpiAwarenessContext
            function.argtypes, function.restype = [ctypes.c_void_p], ctypes.c_void_p
            previous = function(ctypes.c_void_p(-4))
        except (OSError, AttributeError):
            pass
    try:
        yield
    finally:
        if function is not None and previous:
            function(previous)


def _user32():
    import ctypes
    from ctypes import wintypes as w
    api = ctypes.WinDLL("user32", use_last_error=True)
    for name in ("IsWindow", "IsWindowVisible", "IsIconic"):
        function = getattr(api, name)
        function.argtypes, function.restype = [w.HWND], w.BOOL
    api.GetForegroundWindow.argtypes, api.GetForegroundWindow.restype = [], w.HWND
    api.GetWindowRect.argtypes, api.GetWindowRect.restype = [w.HWND, ctypes.POINTER(w.RECT)], w.BOOL
    api.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    api.GetWindowThreadProcessId.restype = w.DWORD
    api.GetWindowTextLengthW.argtypes, api.GetWindowTextLengthW.restype = [w.HWND], ctypes.c_int
    api.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    api.GetWindowTextW.restype = ctypes.c_int
    return api


def _window(hwnd: int) -> WindowTarget:
    import ctypes
    from ctypes import wintypes as w
    api = _user32()
    if not api.IsWindow(hwnd) or not api.IsWindowVisible(hwnd) or api.IsIconic(hwnd):
        raise RuntimeError("Jendela tujuan tertutup atau diminimalkan. Pilih lagi lewat /video pilih.")
    pid = w.DWORD()
    api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    title = ctypes.create_unicode_buffer(max(1, api.GetWindowTextLengthW(hwnd)) + 1)
    api.GetWindowTextW(hwnd, title, len(title))
    return WindowTarget(int(hwnd), pid.value, title.value or "Jendela tanpa judul")


def list_windows() -> list[WindowTarget]:
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes as w
    api = _user32()
    foreground = api.GetForegroundWindow()
    windows = []
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

    @callback_type
    def visit(hwnd, _):
        try:
            if hwnd == foreground or not api.GetWindowTextLengthW(hwnd):
                return True
            item = _window(hwnd)
            rect = w.RECT()
            if api.GetWindowRect(hwnd, ctypes.byref(rect)) and rect.right - rect.left >= 160 and rect.bottom - rect.top >= 100:
                windows.append(item)
        except (OSError, RuntimeError):
            pass
        return len(windows) < 60

    api.EnumWindows.argtypes, api.EnumWindows.restype = [callback_type, w.LPARAM], w.BOOL
    api.EnumWindows(visit, 0)
    return windows


def select_target(hwnd: int = 0) -> None:
    global _target, _generation
    target = _window(hwnd) if hwnd else None
    with _state:
        _target = target
        _generation += 1


def source_label() -> str:
    with _state:
        return _target.title if _target else "Monitor utama"


def last_capture_info() -> CaptureInfo:
    with _state:
        return _last_info


def _drop_camera() -> None:
    global _camera
    camera, _camera = _camera, None
    if camera is not None:
        try:
            camera.release()
        except Exception:
            pass


def _dxcam_compatibility() -> None:
    # DXcam 0.3.0 melepas COM pointer secara eksplisit lalu mengosongkan
    # atributnya. comtypes ikut Release saat pointer di-GC: referensi tambahan
    # menyeimbangkan dua pelepasan tersebut. Batasi workaround pada versi ini.
    from importlib.metadata import version
    if version("dxcam") != "0.3.0":
        return
    from dxcam.core.stagesurf import StageSurface
    from dxcam.core.dxgi_duplicator import DXGIDuplicator
    for cls, member in ((StageSurface, "texture"), (DXGIDuplicator, "duplicator")):
        if getattr(cls.release, "_bagas_balanced", False):
            continue
        original = cls.release

        def release(resource, original=original, member=member):
            pointer = getattr(resource, member, None)
            if pointer:
                pointer.AddRef()
            return original(resource)

        release._bagas_balanced = True
        cls.release = release


def release_capture() -> None:
    """Tidak menunggu capture yang sedang berjalan di thread lain."""
    global _release_pending, _generation
    with _state:
        _release_pending = True
        _generation += 1
    if _capture_lock.acquire(blocking=False):
        try:
            _drop_camera()
        finally:
            _capture_lock.release()


def _dxgi(bounds):
    global _camera
    import dxcam
    from PIL import Image
    _dxcam_compatibility()
    if _camera is None:
        _camera = dxcam.create(backend="dxgi", processor_backend="numpy",
                               output_color="RGB", max_buffer_len=2)
    frame = _camera.grab(region=bounds, new_frame_only=False)
    if frame is None:
        # Tidak pernah memakai buffer lama sebagai tangkapan baru.
        raise RuntimeError("DXGI belum menyediakan frame baru")
    return Image.fromarray(frame)


def _render_window(hwnd: int):
    """Minta window menggambar dirinya; BitBlt dari window tertutup bisa kosong."""
    import ctypes
    from ctypes import wintypes as w
    from PIL import Image

    class Header(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("width", w.LONG), ("height", w.LONG),
                    ("planes", w.WORD), ("bits", w.WORD), ("compression", w.DWORD),
                    ("image_size", w.DWORD), ("xppm", w.LONG), ("yppm", w.LONG),
                    ("used", w.DWORD), ("important", w.DWORD)]

    api = _user32()
    api.IsHungAppWindow.argtypes, api.IsHungAppWindow.restype = [w.HWND], w.BOOL
    if api.IsHungAppWindow(hwnd):
        raise RuntimeError("Aplikasi tujuan sedang tidak merespons")
    rect = w.RECT()
    if not api.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("Batas window tidak tersedia")
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if not (0 < width <= 16384 and 0 < height <= 16384 and width * height <= 40_000_000):
        raise RuntimeError("Ukuran window tidak dapat ditangkap")
    api.GetWindowDC.argtypes, api.GetWindowDC.restype = [w.HWND], w.HDC
    api.ReleaseDC.argtypes = [w.HWND, w.HDC]
    api.PrintWindow.argtypes, api.PrintWindow.restype = [w.HWND, w.HDC, w.UINT], w.BOOL
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi.CreateCompatibleDC.argtypes, gdi.CreateCompatibleDC.restype = [w.HDC], w.HDC
    gdi.CreateCompatibleBitmap.argtypes, gdi.CreateCompatibleBitmap.restype = [w.HDC, ctypes.c_int, ctypes.c_int], w.HBITMAP
    gdi.SelectObject.argtypes, gdi.SelectObject.restype = [w.HDC, w.HANDLE], w.HANDLE
    gdi.GetDIBits.argtypes = [w.HDC, w.HBITMAP, w.UINT, w.UINT, w.LPVOID, ctypes.POINTER(Header), w.UINT]
    gdi.GetDIBits.restype = ctypes.c_int
    gdi.DeleteObject.argtypes = [w.HANDLE]
    gdi.DeleteDC.argtypes = [w.HDC]
    source = api.GetWindowDC(hwnd)
    dc = gdi.CreateCompatibleDC(source) if source else None
    bitmap = gdi.CreateCompatibleBitmap(source, width, height) if dc else None
    previous = gdi.SelectObject(dc, bitmap) if bitmap else None
    try:
        if not source or not dc or not bitmap or not previous:
            raise RuntimeError("Buffer window tidak dapat dibuat")
        if not api.PrintWindow(hwnd, dc, 2):  # PW_RENDERFULLCONTENT
            raise RuntimeError("Aplikasi tidak menyediakan gambar window")
        gdi.SelectObject(dc, previous)
        previous = None
        header = Header(ctypes.sizeof(Header), width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0)
        pixels = ctypes.create_string_buffer(width * height * 4)
        if gdi.GetDIBits(dc, bitmap, 0, height, pixels, ctypes.byref(header), 0) != height:
            raise RuntimeError("Piksel window tidak dapat dibaca")
        return Image.frombytes("RGB", (width, height), pixels.raw, "raw", "BGRX")
    finally:
        if previous:
            gdi.SelectObject(dc, previous)
        if bitmap:
            gdi.DeleteObject(bitmap)
        if dc:
            gdi.DeleteDC(dc)
        if source:
            api.ReleaseDC(hwnd, source)


def _print_window(hwnd: int, timeout: float = 2.0, *, preview: bool = False):
    """Provider window yang macet tidak menahan chat atau menambah worker."""
    slot = _preview_print_lock if preview else _print_lock
    if not slot.acquire(blocking=False):
        raise RuntimeError("Capture jendela sebelumnya belum selesai")
    ready = threading.Event()
    result = []

    def work():
        try:
            with _physical_pixels():
                result.append((_render_window(hwnd), None))
        except Exception as exc:
            result.append((None, exc))
        finally:
            slot.release()
            ready.set()

    threading.Thread(target=work, daemon=True, name="bagasai-window-capture").start()
    if not ready.wait(max(0, timeout)):
        raise RuntimeError("Jendela tidak merespons capture. Coba lagi setelah aplikasi siap.")
    image, error = result[0]
    if error is not None:
        raise error
    return image


def selected_target_id() -> int:
    with _state:
        return _target.hwnd if _target else 0


def capture_thumbnail(target: WindowTarget | None, size=(240, 144)):
    """Snapshot kecil untuk picker, tanpa mengubah sumber atau cache observasi."""
    if termux.IS_TERMUX:
        from PIL import Image
        image, _info = capture()
        try:
            image.thumbnail(size, Image.Resampling.LANCZOS)
            return image.convert("RGB")
        finally:
            image.close()
    from PIL import Image, ImageGrab
    with _physical_pixels():
        if target is not None:
            actual = _window(target.hwnd)
            if actual.pid != target.pid:
                raise RuntimeError("Identitas jendela berubah")
            image = _print_window(target.hwnd, timeout=.8, preview=True)
        else:
            image = ImageGrab.grab()
        try:
            image.thumbnail(size, Image.Resampling.LANCZOS)
            return image.convert("RGB")
        finally:
            image.close()


def capture():
    """Kembalikan gambar resolusi asli dan identitas sumber yang tervalidasi."""
    global _release_pending
    with _capture_lock, _physical_pixels():
        try:
            return _capture_frame()
        finally:
            with _state:
                pending = _release_pending
                _release_pending = False
            if pending:
                _drop_camera()


def _capture_frame():
    global _last_info, _release_pending
    with _state:
        target, generation = _target, _generation
        if _release_pending:
            _drop_camera()
            _release_pending = False
    if termux.IS_TERMUX:
        import os
        import tempfile
        from PIL import Image
        fd, nama = tempfile.mkstemp(prefix="bagasai-screencap-", suffix=".png")
        os.close(fd)
        try:
            if not termux.screenshot(nama):
                raise RuntimeError(
                    "screencap Android tidak tersedia atau gagal mengambil layar")
            image = Image.open(nama)
            image.load()
        finally:
            try:
                os.remove(nama)
            except OSError:
                pass
        with _state:
            if generation != _generation:
                raise InterruptedError("Sumber layar berubah saat capture")
            info = CaptureInfo((generation, 0, 0), "Layar Android", 0,
                               (0, 0, image.width, image.height), "screencap",
                               time.time())
            _last_info = info
        return image, info
    from PIL import ImageGrab
    hwnd, bounds, label, backend = 0, (0, 0, 0, 0), "Monitor utama", "Pillow"
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes as w
        api = _user32()
        if target:
            actual = _window(target.hwnd)
            if actual.pid != target.pid:
                raise RuntimeError("Identitas jendela berubah. Pilih lagi lewat /video pilih.")
            hwnd, label = actual.hwnd, actual.title
            rect = w.RECT()
            if not api.GetWindowRect(hwnd, ctypes.byref(rect)):
                raise RuntimeError("Batas jendela tidak dapat dibaca")
            bounds = rect.left, rect.top, rect.right, rect.bottom
        # DXGI mengambil piksel monitor. Window di belakang aplikasi lain
        # harus memakai PrintWindow, agar terminal tidak ikut tertangkap.
        foreground = api.GetForegroundWindow()
        on_primary = not hwnd or (bounds[0] >= 0 and bounds[1] >= 0
            and bounds[2] <= api.GetSystemMetrics(0) and bounds[3] <= api.GetSystemMetrics(1))
        try:
            if hwnd and (foreground != hwnd or not on_primary):
                raise RuntimeError("Gunakan capture window asli")
            image = _dxgi(bounds if hwnd else None)
            backend = "DXGI"
        except Exception:
            _drop_camera()
            image = _print_window(hwnd) if hwnd else ImageGrab.grab()
            backend = "PrintWindow" if hwnd else "Pillow"
    else:
        image = ImageGrab.grab()
    if image.width < 2 or image.height < 2:
        raise RuntimeError("Tangkapan jendela kosong")
    with _state:
        if generation != _generation:
            raise InterruptedError("Sumber layar berubah saat capture")
        info = CaptureInfo((generation, hwnd, target.pid if target else 0), label,
                           hwnd, bounds, backend, time.time())
        _last_info = info
    return image, info
