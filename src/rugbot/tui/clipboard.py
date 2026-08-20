"""Cross-platform system clipboard reader for terminal input pasting."""

# ruff: noqa: PLC0415, BLE001, S110

from __future__ import annotations

import contextlib
import sys


def get_system_clipboard() -> str:
    """Read plain text from system clipboard across Windows, Linux, and macOS."""
    if sys.platform == "win32":
        try:
            import ctypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            user32.OpenClipboard.argtypes = [ctypes.c_void_p]
            user32.OpenClipboard.restype = ctypes.c_bool
            user32.CloseClipboard.argtypes = []
            user32.CloseClipboard.restype = ctypes.c_bool
            user32.GetClipboardData.argtypes = [ctypes.c_uint]
            user32.GetClipboardData.restype = ctypes.c_void_p
            kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
            kernel32.GlobalUnlock.restype = ctypes.c_bool

            cf_unicodetext = 13
            if user32.OpenClipboard(None):
                try:
                    handle = user32.GetClipboardData(cf_unicodetext)
                    if handle:
                        pointer = kernel32.GlobalLock(handle)
                        if pointer:
                            try:
                                return str(ctypes.c_wchar_p(pointer).value or "")
                            finally:
                                kernel32.GlobalUnlock(handle)
                finally:
                    user32.CloseClipboard()
        except Exception:
            pass

    # Cross-platform fallback: tkinter standard library
    with contextlib.suppress(Exception):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        text = str(root.clipboard_get() or "")
        root.destroy()
        return text

    return ""
