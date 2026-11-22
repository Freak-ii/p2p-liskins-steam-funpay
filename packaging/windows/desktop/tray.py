"""Значок в области уведомлений Windows на чистом ctypes (без pystray/Pillow).

Tray(...).run() крутит цикл сообщений Windows в текущем потоке, пока не вызван stop() или poll()
не вернул False. Меню строится заново при каждом открытии из menu(): список кортежей
(id, текст, флаги) или None для разделителя; флаги — множество из "checked", "disabled", "default".
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

WM_DESTROY, WM_CLOSE, WM_COMMAND, WM_TIMER, WM_NULL = 0x0002, 0x0010, 0x0111, 0x0113, 0x0000
WM_CONTEXTMENU = 0x007B
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_WAKE = WM_APP + 2
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO, NIF_SHOWTIP = 0x01, 0x02, 0x04, 0x10, 0x80
NIIF_INFO, NIIF_WARNING = 0x1, 0x2
NIN_SELECT, NIN_KEYSELECT, NIN_BALLOONUSERCLICK = 0x0400, 0x0401, 0x0405
NOTIFYICON_VERSION_4 = 4
MF_STRING, MF_SEPARATOR, MF_CHECKED, MF_GRAYED = 0x0, 0x800, 0x8, 0x1
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x2, 0x100, 0x80
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
IDI_APPLICATION = 32512


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT), ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM), ("time", wintypes.DWORD), ("pt", POINT)]


def _sig(fn, res, *args):
    fn.restype, fn.argtypes = res, list(args)


_sig(user32.DefWindowProcW, LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_sig(user32.RegisterClassW, wintypes.ATOM, ctypes.POINTER(WNDCLASSW))
_sig(user32.UnregisterClassW, wintypes.BOOL, wintypes.LPCWSTR, wintypes.HINSTANCE)
_sig(user32.CreateWindowExW, wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
     ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE,
     wintypes.LPVOID)
_sig(user32.DestroyWindow, wintypes.BOOL, wintypes.HWND)
_sig(user32.RegisterWindowMessageW, wintypes.UINT, wintypes.LPCWSTR)
_sig(user32.GetMessageW, wintypes.BOOL, ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
_sig(user32.TranslateMessage, wintypes.BOOL, ctypes.POINTER(MSG))
_sig(user32.DispatchMessageW, LRESULT, ctypes.POINTER(MSG))
_sig(user32.PostMessageW, wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_sig(user32.PostQuitMessage, None, ctypes.c_int)
_sig(user32.SetTimer, ctypes.c_size_t, wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID)
_sig(user32.KillTimer, wintypes.BOOL, wintypes.HWND, ctypes.c_size_t)
_sig(user32.CreatePopupMenu, wintypes.HMENU)
_sig(user32.AppendMenuW, wintypes.BOOL, wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR)
_sig(user32.SetMenuDefaultItem, wintypes.BOOL, wintypes.HMENU, wintypes.UINT, wintypes.UINT)
_sig(user32.TrackPopupMenu, wintypes.BOOL, wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
     wintypes.HWND, wintypes.LPVOID)
_sig(user32.DestroyMenu, wintypes.BOOL, wintypes.HMENU)
_sig(user32.GetCursorPos, wintypes.BOOL, ctypes.POINTER(POINT))
_sig(user32.SetForegroundWindow, wintypes.BOOL, wintypes.HWND)
_sig(user32.LoadImageW, wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int,
     ctypes.c_int, wintypes.UINT)
_sig(user32.LoadIconW, wintypes.HICON, wintypes.HINSTANCE, wintypes.LPVOID)
_sig(user32.DestroyIcon, wintypes.BOOL, wintypes.HICON)
_sig(shell32.Shell_NotifyIconW, wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
_sig(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)

MenuItem = "tuple[int, str, set[str]] | None"


class Tray:
    def __init__(self, icon_path: str, tooltip: str, menu: Callable[[], list], on_command: Callable[[int], None],
                 on_default: Callable[[], None], poll: Callable[[], bool] | None = None, poll_ms: int = 1000):
        self.icon_path, self.tooltip = icon_path, tooltip[:127]
        self.menu, self.on_command, self.on_default, self.poll = menu, on_command, on_default, poll
        self.poll_ms = poll_ms
        self.hwnd = None
        self._hicon = None
        self._own_icon = False
        self._calls: list[Callable[[], None]] = []
        self._lock = threading.Lock()
        self._balloon_click: Callable[[], None] | None = None
        self._wndproc = WNDPROC(self._proc)              # держим ссылку, иначе GC убьёт колбэк
        self._class = f"StarsCenterTray{id(self)}"
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

    # ---- вызовы из других потоков
    def call_soon(self, fn: Callable[[], None]) -> None:
        with self._lock:
            self._calls.append(fn)
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_WAKE, 0, 0)

    def stop(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def notify(self, title: str, text: str, warning: bool = False, on_click: Callable[[], None] | None = None) -> None:
        def do():
            self._balloon_click = on_click
            nid = self._nid(NIF_INFO)
            nid.szInfoTitle, nid.szInfo = title[:63], text[:255]
            nid.dwInfoFlags = NIIF_WARNING if warning else NIIF_INFO
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        self.call_soon(do)

    # ---- внутреннее
    def _nid(self, flags: int) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd, nid.uID, nid.uFlags = self.hwnd, 1, flags
        return nid

    def _add_icon(self) -> None:
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        nid.uCallbackMessage, nid.hIcon, nid.szTip = WM_TRAY, self._hicon, self.tooltip
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        nid.uVersion = NOTIFYICON_VERSION_4
        shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))

    def _show_menu(self) -> None:
        items = self.menu()
        hmenu = user32.CreatePopupMenu()
        default_id = None
        for it in items:
            if it is None:
                user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                continue
            cid, text, flags = it
            f = MF_STRING | (MF_CHECKED if "checked" in flags else 0) | (MF_GRAYED if "disabled" in flags else 0)
            user32.AppendMenuW(hmenu, f, cid, text)
            if "default" in flags:
                default_id = cid
        if default_id is not None:
            user32.SetMenuDefaultItem(hmenu, default_id, 0)
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)            # иначе меню не закрывается кликом мимо
        cmd = user32.TrackPopupMenu(hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY, pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(hmenu)
        if cmd:
            self._safe(lambda: self.on_command(int(cmd)))

    @staticmethod
    def _safe(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 — ошибка в обработчике меню не должна ронять цикл сообщений
            import logging
            logging.getLogger("desktop.tray").exception("ошибка обработчика трея")

    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            event = lparam & 0xFFFF                       # NOTIFYICON_VERSION_4: событие в младшем слове
            # версия 4: правый клик приходит как WM_CONTEXTMENU, левый — как NIN_SELECT
            if event == WM_CONTEXTMENU:
                self._show_menu()
            elif event in (NIN_SELECT, NIN_KEYSELECT):
                self._safe(self.on_default)
            elif event == NIN_BALLOONUSERCLICK and self._balloon_click:
                self._safe(self._balloon_click)
            return 0
        if msg == WM_WAKE:
            with self._lock:
                calls, self._calls = self._calls, []
            for fn in calls:
                self._safe(fn)
            return 0
        if msg == WM_TIMER:
            if self.poll and not self.poll():
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            return 0
        if msg == self._taskbar_created and self._taskbar_created:
            self._add_icon()                              # проводник перезапустился — значок пропал
            return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.KillTimer(hwnd, 1)
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(0)))
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc, wc.hInstance, wc.lpszClassName = self._wndproc, hinst, self._class
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd = user32.CreateWindowExW(0, self._class, self.tooltip, 0, 0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self._hicon = user32.LoadImageW(None, self.icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
        self._own_icon = bool(self._hicon)
        if not self._hicon:
            self._hicon = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        self._add_icon()
        if self.poll:
            user32.SetTimer(self.hwnd, 1, self.poll_ms, None)
        with self._lock:
            pending = bool(self._calls)
        if pending:
            user32.PostMessageW(self.hwnd, WM_WAKE, 0, 0)
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self.hwnd = None
        if self._own_icon:
            user32.DestroyIcon(self._hicon)
        user32.UnregisterClassW(self._class, hinst)
