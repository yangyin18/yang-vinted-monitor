# -*- mode: python ; coding: utf-8 -*-
"""Vinted 多账号监控 客户端打包配置。

用法(在项目根目录):
    pip install pyinstaller customtkinter requests
    pyinstaller vinted_client.spec

产物: dist/VintedMonitor/VintedMonitor.exe(windowed,无黑窗)。
注意: 不能 exclude tkinter(tk/ttk 是 ttk.Treeview 的底层)。
"""
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = collect_data_files('customtkinter')          # 主题 JSON 必须带上
hiddenimports = collect_submodules('customtkinter') + ['darkdetect', 'packaging', 'requests']

# Anaconda 把一堆运行时 DLL 放在 Library/bin,PyInstaller 钩子找不到,必须显式带上。
# 缺了下面任何一个,exe 启动/联网时会报 "DLL load failed":
#   tcl86t.dll / tk86t.dll     tkinter(没它 _tkinter 起不来)
#   ffi.dll                    ctypes(Anaconda 的 _ctypes 依赖它)
#   libssl-3-x64.dll / libcrypto-3-x64.dll   ssl(requests 走 HTTPS 时 _ssl 需要)
import os
import sys
_extra_dlls = []
_dll_names = ["tcl86t.dll", "tk86t.dll", "ffi.dll", "libssl-3-x64.dll", "libcrypto-3-x64.dll"]
_candidates = [os.path.dirname(sys.executable),            # python 根目录(标准 CPython)
               os.path.join(sys.prefix, "Library", "bin"),  # Anaconda
               os.path.join(sys.prefix, "DLLs")]
for _d in _candidates:
    for _n in _dll_names:
        _p = os.path.join(_d, _n)
        if os.path.exists(_p):
            _extra_dlls.append((_p, "."))   # 放到打包根目录,按相对路径找到

a = Analysis(
    ['vinted_client.py'],
    pathex=[],
    binaries=_extra_dlls,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'curl_cffi', 'fastapi', 'uvicorn', 'starlette', 'pydantic',
        'numpy', 'pandas', 'matplotlib', 'PIL', 'PyQt5', 'PySide6', 'pytest',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VintedMonitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,                 # windowed:不弹黑色控制台
    icon='assets/app.ico',
)

coll = COLLECT(exe, a.binaries, a.datas, name='VintedMonitor')
