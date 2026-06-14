# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['review.py'],
    pathex=['src'],
    binaries=[('<USER_HOME>/.conda/envs/review/Library/bin/libssl-3-x64.dll', '.'), ('<USER_HOME>/.conda/envs/review/Library/bin/libcrypto-3-x64.dll', '.'), ('<USER_HOME>/.conda/envs/review/Library/bin/libexpat.dll', '.'), ('<USER_HOME>/.conda/envs/review/Library/bin/libmpdec-4.dll', '.'), ('<USER_HOME>/.conda/envs/review/Library/bin/zstd.dll', '.'), ('<USER_HOME>/.conda/envs/review/Library/bin/liblzma.dll', '.'), ('<USER_HOME>/.conda/envs/review/Library/bin/libbz2.dll', '.'), ('<USER_HOME>/.conda/envs/review/Library/bin/ffi.dll', '.')],
    datas=[
        ('src/assets/checkbox_checked.svg', 'assets'),
        ('src/assets/edit_icon.svg', 'assets'),
        ('src/class_colors.json', '.'),
        ('src/class_colors_default.json', '.'),
    ],
    hiddenimports=['src.constants', 'src.utils', 'src.data_loading', 'src.qt_main', 'src.rle', 'numpy', 'cv2'],
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
    name='review_app',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
