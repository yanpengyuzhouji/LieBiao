"""Small native Windows notification-area icon without an extra runtime dependency."""
from __future__ import annotations

import ctypes
import os
import webbrowser
from pathlib import Path
from typing import Callable


class WindowsTray:
    def __init__(
        self,
        window,
        data_dir: Path | Callable[[], Path],
        on_exit: Callable[[], None],
        on_open_system: Callable[[], None] | None = None,
        on_open_updates: Callable[[], None] | None = None,
    ) -> None:
        self.window = window
        self.data_dir = data_dir
        self.on_exit = on_exit
        self.on_open_system = on_open_system
        self.on_open_updates = on_open_updates
        self._callback = None
        self._old_proc = 0
        self._nid = None

    def start(self, release_url: str = "https://github.com/yanpengyuzhouji/LieBiao/releases") -> bool:
        if os.name != "nt":
            return False
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        hwnd = self.window.winfo_id()
        WM_TRAY = 0x8001
        WM_COMMAND = 0x0111
        WM_LBUTTONDBLCLK = 0x0203
        WM_RBUTTONUP = 0x0205
        GWL_WNDPROC = -4
        NIM_ADD = 0
        NIM_DELETE = 2
        NIF_MESSAGE = 0x1
        NIF_ICON = 0x2
        NIF_TIP = 0x4
        TPM_RETURNCMD = 0x0100
        TPM_NONOTIFY = 0x0080
        IDI_APPLICATION = 32512
        MENU_OPEN = 1001
        MENU_DATA = 1002
        MENU_RELEASES = 1003
        MENU_EXIT = 1004

        class NotifyIconData(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint), ("hWnd", ctypes.c_void_p), ("uID", ctypes.c_uint),
                ("uFlags", ctypes.c_uint), ("uCallbackMessage", ctypes.c_uint), ("hIcon", ctypes.c_void_p),
                ("szTip", ctypes.c_wchar * 128), ("dwState", ctypes.c_uint), ("dwStateMask", ctypes.c_uint),
                ("szInfo", ctypes.c_wchar * 256), ("uTimeoutOrVersion", ctypes.c_uint),
                ("szInfoTitle", ctypes.c_wchar * 64), ("dwInfoFlags", ctypes.c_uint),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", ctypes.c_void_p),
            ]

        class Point(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.CallWindowProcW.argtypes = [
            ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
        ]
        user32.CallWindowProcW.restype = ctypes.c_ssize_t
        shell32.Shell_NotifyIconW.argtypes = [ctypes.c_uint, ctypes.POINTER(NotifyIconData)]
        shell32.Shell_NotifyIconW.restype = ctypes.c_bool

        WndProc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
        old_proc = user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC)

        def show_window() -> None:
            self.window.deiconify()
            self.window.lift()
            user32.SetForegroundWindow(hwnd)

        def menu_command(command: int) -> None:
            if command == MENU_OPEN:
                if self.on_open_system:
                    self.on_open_system()
                else:
                    show_window()
            elif command == MENU_DATA:
                data_dir = self.data_dir() if callable(self.data_dir) else self.data_dir
                os.startfile(str(data_dir))
            elif command == MENU_RELEASES:
                if self.on_open_updates:
                    self.on_open_updates()
                else:
                    webbrowser.open(release_url)
            elif command == MENU_EXIT:
                self.on_exit()

        def show_menu() -> None:
            menu = user32.CreatePopupMenu()
            for command, text in ((MENU_OPEN, "打开猎标"), (MENU_DATA, "打开数据目录"), (MENU_RELEASES, "检查版本更新"), (MENU_EXIT, "退出系统")):
                user32.AppendMenuW(menu, 0, command, text)
            point = Point()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(hwnd)
            selected = user32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_NONOTIFY, point.x, point.y, 0, hwnd, None)
            user32.DestroyMenu(menu)
            if selected:
                menu_command(selected)

        @WndProc
        def callback(callback_hwnd, message, wparam, lparam):
            if message == WM_TRAY:
                event = int(lparam) & 0xFFFFFFFF
                if event == WM_LBUTTONDBLCLK:
                    show_window()
                elif event == WM_RBUTTONUP:
                    show_menu()
            elif message == WM_COMMAND and (int(wparam) & 0xFFFF) in {MENU_OPEN, MENU_DATA, MENU_RELEASES, MENU_EXIT}:
                menu_command(int(wparam) & 0xFFFF)
            return user32.CallWindowProcW(old_proc, callback_hwnd, message, wparam, lparam)

        self._callback = callback
        user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, ctypes.cast(callback, ctypes.c_void_p).value)
        icon = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        nid = NotifyIconData()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = icon
        nid.szTip = "猎标招标公告采集系统"
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, old_proc)
            self._callback = None
            return False
        self._old_proc = old_proc
        self._nid = nid
        return True

    def hide_window(self) -> None:
        self.window.withdraw()

    def destroy(self) -> None:
        if self._nid is not None:
            ctypes.windll.shell32.Shell_NotifyIconW(2, ctypes.byref(self._nid))
            ctypes.windll.user32.SetWindowLongPtrW(self.window.winfo_id(), -4, self._old_proc)
            self._nid = None
        self._callback = None
