# -*- mode: python ; coding: utf-8 -*-

import re
from pathlib import Path

# Single source of truth for name + version is src/constants.py. Read it without
# importing (the module runs DPI/windll detection at import time we don't want here).
_constants_src = Path('src/constants.py').read_text(encoding='utf-8')
APP_NAME = re.search(r'APP_NAME\s*=\s*["\']([^"\']+)["\']', _constants_src).group(1)
APP_VERSION = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', _constants_src).group(1)
# e.g. "Annotation Workbench 1.1" -> dist/Annotation Workbench 1.1.exe
BUILD_NAME = f"{APP_NAME} {APP_VERSION}"

a = Analysis(
    ['review.py'],
    pathex=['src'],
    binaries=[('<USER_HOME>/.conda/envs/review/Library/bin/libssl-3-x64.dll', '.'), ('<USER_HOME>/.conda/envs/review/Library/bin/libcrypto-3-x64.dll', '.'), ('<USER_HOME>/.conda/envs/review/Library/bin/libexpat.dll', '.'), ('<USER_HOME>/.conda/envs/review/Library/bin/libmpdec-4.dll', '.'), ('<USER_HOME>/.conda/envs/review/Library/bin/zstd.dll', '.'), ('<USER_HOME>/.conda/envs/review/Library/bin/liblzma.dll', '.'), ('<USER_HOME>/.conda/envs/review/Library/bin/libbz2.dll', '.'), ('<USER_HOME>/.conda/envs/review/Library/bin/ffi.dll', '.')],
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
