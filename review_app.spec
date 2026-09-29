# -*- mode: python ; coding: utf-8 -*-

import re
import sys
from pathlib import Path

# Single source of truth for name + version is src/constants.py. Read it without
# importing (the module runs DPI/windll detection at import time we don't want here).
_constants_src = Path('src/constants.py').read_text(encoding='utf-8')
APP_NAME = re.search(r'APP_NAME\s*=\s*["\']([^"\']+)["\']', _constants_src).group(1)
APP_VERSION = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', _constants_src).group(1)
# e.g. "DFET Annotation tool 1.0" -> dist/DFET Annotation tool 1.0.exe
BUILD_NAME = f"{APP_NAME} {APP_VERSION}"

# Conda keeps these DLLs in <env>/Library/bin, where PyInstaller doesn't find them on
# its own. Resolve them from whichever env runs the build (no machine-specific paths).
_CONDA_BIN = Path(sys.prefix) / 'Library' / 'bin'
_CONDA_DLLS = [
    (str(_CONDA_BIN / name), '.')
    for name in ('libssl-3-x64.dll', 'libcrypto-3-x64.dll', 'libexpat.dll', 'libmpdec-4.dll',
                 'zstd.dll', 'liblzma.dll', 'libbz2.dll', 'ffi.dll')
    if (_CONDA_BIN / name).exists()
]

a = Analysis(
    ['review.py'],
    pathex=['src'],
    binaries=_CONDA_DLLS,
    datas=[
        ('src/assets/checkbox_checked.svg', 'assets'),
        ('src/assets/edit_icon.svg', 'assets'),
        ('src/assets/undo_icon.svg', 'assets'),
        ('src/assets/redo_icon.svg', 'assets'),
        ('src/assets/app_icon.png', 'assets'),
        ('src/assets/app_icon.ico', 'assets'),
        ('src/class_colors.json', '.'),
        ('src/class_colors_default.json', '.'),
    ],
    hiddenimports=['src.constants', 'src.utils', 'src.data_loading', 'src.qt_main', 'src.rle', 'src.applog', 'numpy', 'cv2'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=BUILD_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='src/assets/app_icon.ico',
)
