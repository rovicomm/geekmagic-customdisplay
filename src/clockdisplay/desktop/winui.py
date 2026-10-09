"""Native desktop windows, drawn with plain Win32 through ctypes (no tkinter in the exe).

One daemon thread owns every window and runs the message loop; other threads hand it work
with UI.call(fn). Both kinds of window are layered (per-pixel alpha via UpdateLayeredWindow),
so there is no WM_PAINT: each frame is pushed to the screen as a premultiplied BGRA bitmap.

  * MirrorWindow: the desktop "display". Shows whatever the VirtualDevice shows (animating
    GIFs), or a clock when the device is back on its clock theme. Drag to move, right-click
    for a menu.
  * popup(): a small click-through GIF that shows for a few seconds, e.g. the reset GIFs
    over the Zebar bar.
"""
from __future__ import annotations

import ctypes
import datetime as dt
import io
import logging
import queue
import threading
from ctypes import wintypes as wt
from typing import Callable

from PIL import Image, ImageChops, ImageDraw, ImageSequence

from clockdisplay.render import frames

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_LAYERED = 0x8, 0x80, 0x80000
WS_EX_TRANSPARENT, WS_EX_NOACTIVATE = 0x20, 0x8000000
CS_DROPSHADOW = 0x20000
WM_DESTROY, WM_TIMER, WM_APP, WM_NULL = 0x0002, 0x0113, 0x8000, 0x0000
WM_NCHITTEST, WM_NCLBUTTONDBLCLK, WM_NCRBUTTONUP = 0x0084, 0x00A3, 0x00A5
WM_CONTEXTMENU, WM_EXITSIZEMOVE = 0x007B, 0x0232
HTCAPTION = 2
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
HWND_TOPMOST, HWND_NOTOPMOST, HWND_MESSAGE = -1, -2, -3
MF_STRING, MF_CHECKED, MF_SEPARATOR = 0x0, 0x8, 0x800
TPM_RETURNCMD, TPM_RIGHTBUTTON = 0x100, 0x2
ULW_ALPHA, AC_SRC_ALPHA = 0x2, 0x1
IDC_ARROW, IDC_SIZEALL = 32512, 32646
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
SM_CXSCREEN = 0


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_byte)]


def _sig(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = restype, argtypes
    return fn


_RegisterClassExW = _sig(user32, "RegisterClassExW", wt.ATOM, ctypes.POINTER(WNDCLASSEXW))
_CreateWindowExW = _sig(user32, "CreateWindowExW", wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR,
                        wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                        wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID)
_DefWindowProcW = _sig(user32, "DefWindowProcW", LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_GetMessageW = _sig(user32, "GetMessageW", wt.BOOL, ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
_TranslateMessage = _sig(user32, "TranslateMessage", wt.BOOL, ctypes.POINTER(wt.MSG))
_DispatchMessageW = _sig(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(wt.MSG))
_PostMessageW = _sig(user32, "PostMessageW", wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_DestroyWindow = _sig(user32, "DestroyWindow", wt.BOOL, wt.HWND)
_ShowWindow = _sig(user32, "ShowWindow", wt.BOOL, wt.HWND, ctypes.c_int)
_SetWindowPos = _sig(user32, "SetWindowPos", wt.BOOL, wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                     ctypes.c_int, ctypes.c_int, wt.UINT)
_GetWindowRect = _sig(user32, "GetWindowRect", wt.BOOL, wt.HWND, ctypes.POINTER(wt.RECT))
_SetTimer = _sig(user32, "SetTimer", ctypes.c_size_t, wt.HWND, ctypes.c_size_t, wt.UINT, wt.LPVOID)
_KillTimer = _sig(user32, "KillTimer", wt.BOOL, wt.HWND, ctypes.c_size_t)
_GetDC = _sig(user32, "GetDC", wt.HDC, wt.HWND)
_ReleaseDC = _sig(user32, "ReleaseDC", ctypes.c_int, wt.HWND, wt.HDC)
_UpdateLayeredWindow = _sig(user32, "UpdateLayeredWindow", wt.BOOL, wt.HWND, wt.HDC,
                            ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE), wt.HDC,
                            ctypes.POINTER(wt.POINT), wt.COLORREF, ctypes.POINTER(BLENDFUNCTION),
                            wt.DWORD)
_LoadCursorW = _sig(user32, "LoadCursorW", wt.HANDLE, wt.HINSTANCE, wt.LPVOID)
_CreatePopupMenu = _sig(user32, "CreatePopupMenu", wt.HMENU)
_AppendMenuW = _sig(user32, "AppendMenuW", wt.BOOL, wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR)
_TrackPopupMenu = _sig(user32, "TrackPopupMenu", ctypes.c_int, wt.HMENU, wt.UINT, ctypes.c_int,
                       ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID)
_DestroyMenu = _sig(user32, "DestroyMenu", wt.BOOL, wt.HMENU)
_SetForegroundWindow = _sig(user32, "SetForegroundWindow", wt.BOOL, wt.HWND)
_GetCursorPos = _sig(user32, "GetCursorPos", wt.BOOL, ctypes.POINTER(wt.POINT))
_GetSystemMetrics = _sig(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
_CreateCompatibleDC = _sig(gdi32, "CreateCompatibleDC", wt.HDC, wt.HDC)
_CreateDIBSection = _sig(gdi32, "CreateDIBSection", wt.HBITMAP, wt.HDC, ctypes.POINTER(BITMAPINFOHEADER),
                         wt.UINT, ctypes.POINTER(wt.LPVOID), wt.HANDLE, wt.DWORD)
_SelectObject = _sig(gdi32, "SelectObject", wt.HGDIOBJ, wt.HDC, wt.HGDIOBJ)
_DeleteObject = _sig(gdi32, "DeleteObject", wt.BOOL, wt.HGDIOBJ)
_DeleteDC = _sig(gdi32, "DeleteDC", wt.BOOL, wt.HDC)
_GetModuleHandleW = _sig(kernel32, "GetModuleHandleW", wt.HMODULE, wt.LPCWSTR)


# --- frames ----------------------------------------------------------------------

Frame = tuple[bytes, int, int, int]  # premultiplied BGRA, width, height, duration ms


def _to_frame(img: Image.Image, size: int, radius: int, duration: int = 0) -> Frame:
    """Scale to size x size, round the corners, premultiply alpha, pack as BGRA."""
    img = img.convert("RGBA")
    if img.size != (size, size):
        img = img.resize((size, size), Image.LANCZOS)
    if radius:
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius, fill=255)
        img.putalpha(ImageChops.multiply(img.getchannel("A"), mask))
    r, g, b, a = img.split()
    img = Image.merge("RGBA", [ImageChops.multiply(c, a) for c in (r, g, b)] + [a])
    return img.tobytes("raw", "BGRA"), size, size, duration


def decode(content: bytes | Image.Image, size: int, radius: int = 0) -> list[Frame]:
    """Every frame of a JPEG/GIF (bytes) or PIL image, ready for UpdateLayeredWindow."""
    if isinstance(content, Image.Image):
        return [_to_frame(content, size, radius)]
    with Image.open(io.BytesIO(content)) as im:
        return [_to_frame(fr.copy(), size, radius, max(20, int(fr.info.get("duration") or 100)))
                for fr in ImageSequence.Iterator(im)]


# --- UI thread ---------------------------------------------------------------------

class UI:
    """The thread that owns the windows. Start once; then call(fn) runs fn on it."""

    def __init__(self):
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._windows: dict[int, "LayeredWindow"] = {}
        self._ready = threading.Event()
        self._hwnd = None
        self.hinstance = None
        self._proc = WNDPROC(self._wndproc)  # kept alive for the life of the windows
        self._thread = threading.Thread(target=self._run, name="winui", daemon=True)

    def start(self) -> "UI":
        self._thread.start()
        self._ready.wait(5)
        return self

    def call(self, fn: Callable[[], None]) -> None:
        self._queue.put(fn)
        if self._hwnd:
            _PostMessageW(self._hwnd, WM_APP, 0, 0)

    def _run(self) -> None:
        try:
            user32.SetThreadDpiAwarenessContext.restype = wt.LPVOID
            user32.SetThreadDpiAwarenessContext.argtypes = (wt.LPVOID,)
            user32.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        except AttributeError:  # older than Windows 10 1607: bitmap-scaled, still works
            pass
        self.hinstance = _GetModuleHandleW(None)
        for name, style in (("ClockDisplayHost", 0), ("ClockDisplayWindow", CS_DROPSHADOW)):
            wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), style=style, lpfnWndProc=self._proc,
                             hInstance=self.hinstance, lpszClassName=name,
                             hCursor=_LoadCursorW(None, IDC_ARROW))
            _RegisterClassExW(ctypes.byref(wc))
        self._hwnd = _CreateWindowExW(0, "ClockDisplayHost", "", 0, 0, 0, 0, 0,
                                      HWND_MESSAGE, None, self.hinstance, None)
        self._ready.set()
        msg = wt.MSG()
        while _GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _TranslateMessage(ctypes.byref(msg))
            _DispatchMessageW(ctypes.byref(msg))

    def create(self, win: "LayeredWindow", ex_style: int) -> int:
        hwnd = _CreateWindowExW(ex_style | WS_EX_LAYERED | WS_EX_TOOLWINDOW, "ClockDisplayWindow",
                                win.title, WS_POPUP, 0, 0, 0, 0, None, None, self.hinstance, None)
        self._windows[hwnd] = win
        return hwnd

    def forget(self, hwnd: int) -> None:
        self._windows.pop(hwnd, None)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if hwnd == self._hwnd and msg == WM_APP:
                while True:
                    try:
                        fn = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        fn()
                    except Exception:
                        log.exception("UI call failed")
                return 0
            win = self._windows.get(hwnd)
            if win is not None:
                result = win.handle(msg, wparam, lparam)
                if result is not None:
                    return result
        except Exception:
            log.exception("window message %#x failed", msg)
        return _DefWindowProcW(hwnd, msg, wparam, lparam)


_ui: UI | None = None
_ui_lock = threading.Lock()


def ui() -> UI:
    global _ui
    with _ui_lock:
        if _ui is None:
            _ui = UI().start()
        return _ui


# --- windows -------------------------------------------------------------------------

class LayeredWindow:
    """A borderless per-pixel-alpha window that plays a list of frames. UI thread only."""
    title = "ClockDisplay"
    TIMER = 1

    def __init__(self, ui: UI, x: int, y: int, ex_style: int):
        self.ui, self.x, self.y = ui, x, y
        self.frames: list[Frame] = []
        self.index = 0
        self.hwnd = ui.create(self, ex_style)

    def play(self, frames_: list[Frame]) -> None:
        _KillTimer(self.hwnd, self.TIMER)
        self.frames, self.index = frames_, 0
        self._draw()

    def _draw(self) -> None:
        if not self.frames:
            return
        data, w, h, duration = self.frames[self.index]
        screen = _GetDC(None)
        mem = _CreateCompatibleDC(screen)
        bits = wt.LPVOID()
        header = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h,
                                  biPlanes=1, biBitCount=32)
        bmp = _CreateDIBSection(mem, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        try:
            ctypes.memmove(bits, data, len(data))
            old = _SelectObject(mem, bmp)
            blend = BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)
            _UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(wt.POINT(self.x, self.y)),
                                 ctypes.byref(wt.SIZE(w, h)), mem, ctypes.byref(wt.POINT(0, 0)),
                                 0, ctypes.byref(blend), ULW_ALPHA)
            _SelectObject(mem, old)
        finally:
            _DeleteObject(bmp)
            _DeleteDC(mem)
            _ReleaseDC(None, screen)
        if len(self.frames) > 1:
            _SetTimer(self.hwnd, self.TIMER, duration, None)

    def show(self) -> None:
        _ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)

    def destroy(self) -> None:
        _KillTimer(self.hwnd, self.TIMER)
        self.ui.forget(self.hwnd)
        _DestroyWindow(self.hwnd)

    def handle(self, msg: int, wparam: int, lparam: int) -> int | None:
        if msg == WM_TIMER and wparam == self.TIMER:
            _KillTimer(self.hwnd, self.TIMER)
            self.index = (self.index + 1) % len(self.frames)
            self._draw()
            return 0
        return None


class MirrorWindow(LayeredWindow):
    """The desktop display. `settings` is the config "window" block; on_change(settings) is
    called after a move/resize/topmost change so it can be saved, on_close() on Close."""
    title = "ClockDisplay"
    CLOCK_TIMER = 2
    SIZES = (240, 360, 480)
    CMD_TOP, CMD_CLOSE, CMD_SIZE = 1, 2, 10

    def __init__(self, ui: UI, settings: dict, on_change: Callable[[dict], None],
                 on_close: Callable[[], None]):
        self.settings = dict(settings)
        self.on_change, self.on_close = on_change, on_close
        x, y = self.settings.get("x"), self.settings.get("y")
        if x is None or y is None:  # first run: top-right corner, clear of a top bar
            x, y = _GetSystemMetrics(SM_CXSCREEN) - self.size - 24, 64
        super().__init__(ui, int(x), int(y), WS_EX_TOPMOST if self.settings.get("topmost") else 0)
        self.content: bytes | None = None
        self.set_content(None)
        self.show()

    @property
    def size(self) -> int:
        return int(self.settings.get("size") or 240)

    @property
    def radius(self) -> int:
        return self.size // 24

    def set_content(self, content: bytes | None) -> None:
        self.content = content
        _KillTimer(self.hwnd, self.CLOCK_TIMER)
        if content is None:
            self._tick_clock()
        else:
            self.play(decode(content, self.size, self.radius))

    def _tick_clock(self) -> None:
        now = dt.datetime.now()
        self.play(decode(frames.clock(now), self.size, self.radius))
        _SetTimer(self.hwnd, self.CLOCK_TIMER, 1000 - now.microsecond // 1000 + 5, None)

    def _menu(self) -> None:
        menu = _CreatePopupMenu()
        topmost = bool(self.settings.get("topmost"))
        _AppendMenuW(menu, MF_STRING | (MF_CHECKED if topmost else 0), self.CMD_TOP, "Always on top")
        _AppendMenuW(menu, MF_SEPARATOR, 0, None)
        for i, s in enumerate(self.SIZES):
            _AppendMenuW(menu, MF_STRING | (MF_CHECKED if s == self.size else 0),
                         self.CMD_SIZE + i, f"{s} × {s}")
        _AppendMenuW(menu, MF_SEPARATOR, 0, None)
        _AppendMenuW(menu, MF_STRING, self.CMD_CLOSE, "Close")
        pt = wt.POINT()
        _GetCursorPos(ctypes.byref(pt))
        _SetForegroundWindow(self.hwnd)
        cmd = _TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON, pt.x, pt.y, 0, self.hwnd, None)
        _PostMessageW(self.hwnd, WM_NULL, 0, 0)
        _DestroyMenu(menu)
        if cmd == self.CMD_TOP:
            self.settings["topmost"] = not topmost
            _SetWindowPos(self.hwnd, HWND_NOTOPMOST if topmost else HWND_TOPMOST, 0, 0, 0, 0,
                          SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            self.on_change(dict(self.settings))
        elif cmd >= self.CMD_SIZE:
            self.settings["size"] = self.SIZES[cmd - self.CMD_SIZE]
            self.set_content(self.content)
            self.on_change(dict(self.settings))
        elif cmd == self.CMD_CLOSE:
            self.on_close()

    def handle(self, msg: int, wparam: int, lparam: int) -> int | None:
        if msg == WM_TIMER and wparam == self.CLOCK_TIMER:
            if self.content is None:
                self._tick_clock()
            return 0
        if msg == WM_NCHITTEST:
            return HTCAPTION  # drag from anywhere
        if msg == WM_NCLBUTTONDBLCLK:
            return 0  # no maximise
        if msg in (WM_NCRBUTTONUP, WM_CONTEXTMENU):
            self._menu()
            return 0
        if msg == WM_EXITSIZEMOVE:
            r = wt.RECT()
            _GetWindowRect(self.hwnd, ctypes.byref(r))
            self.x, self.y = r.left, r.top
            self.settings.update(x=r.left, y=r.top)
            self.on_change(dict(self.settings))
            return 0
        return super().handle(msg, wparam, lparam)


class Popup(LayeredWindow):
    """Click-through, topmost, never focused: plays each GIF for `seconds`, then goes away."""
    title = "ClockDisplay pop-up"
    NEXT_TIMER = 3

    def __init__(self, ui: UI, gifs: list[bytes], seconds: float, x: int, y: int, size: int):
        super().__init__(ui, x, y, WS_EX_TOPMOST | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        self.queue = [decode(g, size, size // 10) for g in gifs]
        self.ms = max(500, int(seconds * 1000))
        self._next()
        self.show()

    def _next(self) -> None:
        if not self.queue:
            self.destroy()
            return
        self.play(self.queue.pop(0))
        _SetTimer(self.hwnd, self.NEXT_TIMER, self.ms, None)

    def handle(self, msg: int, wparam: int, lparam: int) -> int | None:
        if msg == WM_TIMER and wparam == self.NEXT_TIMER:
            _KillTimer(self.hwnd, self.NEXT_TIMER)
            self._next()
            return 0
        return super().handle(msg, wparam, lparam)


def popup(gifs: list[bytes], seconds: float, at: tuple[int, int] | None = None, size: int = 120) -> None:
    """Show `gifs` one after another, `seconds` each. `at` is the screen point (physical
    pixels) the pop-up hangs from, centred below it; default the top-right of the screen."""
    u = ui()

    def make() -> None:
        if at is None:
            x, y = _GetSystemMetrics(SM_CXSCREEN) - size - 24, 48
        else:
            x, y = at[0] - size // 2, at[1] + 4
        Popup(u, gifs, seconds, x, y, size)

    u.call(make)
