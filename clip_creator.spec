# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Clip Creator.

Build:  .venv\\Scripts\\pyinstaller.exe clip_creator.spec --clean
Output: dist\\ClipCreator.exe (single file, console attached)

ffmpeg/, uploads/, output/, config.json are NOT bundled — they live next to
the .exe at runtime.
"""
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
    ],
    hiddenimports=collect_submodules('flask') + collect_submodules('werkzeug'),
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # heavyweight stuff in the venv we don't use
        'torch', 'torchvision', 'transformers', 'tensorflow',
        'matplotlib', 'pandas', 'scipy', 'numpy', 'cv2',
        'PIL', 'sklearn', 'IPython', 'jupyter_client', 'notebook',
        'pytest', 'tkinter', 'customtkinter', 'pygame',
        'openai', 'anthropic', 'langchain', 'langchain_core',
        'discord', 'aiohttp', 'sqlalchemy', 'alembic',
    ],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ClipCreator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,        # cmd window stays open while the server runs
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
