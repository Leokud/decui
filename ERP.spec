# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('F:\\projects\\decui\\decui', 'decui')]
binaries = []
hiddenimports = ['sqlalchemy.sql.default_comparator', 'sqlalchemy.ext.declarative', 'sqlalchemy.orm', 'decui', 'decui.fields', 'decui.entity', 'decui.registry', 'decui.commands', 'decui.orm', 'decui.numbering', 'decui.reports', 'decui.entity', 'decui.ui', 'decui.ui.report_view', 'decui.ui.editors', 'decui.ui.form', 'decui.ui.listview', 'decui.ui.navigation', 'decui.app']
tmp_ret = collect_all('sqlalchemy')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['F:\\projects\\decui\\examples\\simple.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name='ERP',
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
)
