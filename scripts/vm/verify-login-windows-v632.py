"""Interactive-session read-only evidence for our named login windows only."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile

user32=ctypes.WinDLL('user32',use_last_error=True)
callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
user32.EnumWindows.argtypes=(callback_type,wintypes.LPARAM)
user32.IsWindowVisible.argtypes=(wintypes.HWND,)
user32.GetWindowTextW.argtypes=(wintypes.HWND,wintypes.LPWSTR,ctypes.c_int)
user32.GetWindowThreadProcessId.argtypes=(wintypes.HWND,ctypes.POINTER(wintypes.DWORD))
windows=[]
@callback_type
def collect(hwnd,param):
    if user32.IsWindowVisible(hwnd):
        title=ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd,title,len(title))
        if title.value.startswith('VK — '):
            pid=wintypes.DWORD();user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
            windows.append({'label':title.value[5:],'pid':pid.value,'visible':True})
    return True
user32.EnumWindows(collect,0)
session=wintypes.DWORD()
ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(),ctypes.byref(session))
out=Path(tempfile.gettempdir())/'vk-window-evidence-v632.json'
out.write_text(json.dumps({'session':session.value,'windows':windows},ensure_ascii=True),encoding='utf-8')
