# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

from legal_payload import filter_binaries, generate_payload


root = Path(SPECPATH)
datas = [
    (str(root / "sdctoolbox" / "mdib_bootstrap.xml"), "sdctoolbox"),
    (str(root / "presets"), "presets"),
]
datas += collect_data_files("sdc11073", includes=["xsd/*.xsd"])

analysis = Analysis(
    [str(root / "run_toolbox.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
analysis.binaries = filter_binaries(analysis.binaries)
analysis.datas += generate_payload(
    root,
    root / "build" / "legal-payload",
    analysis.pure,
    analysis.binaries,
    analysis.datas,
)
pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="SDC-Testing-Toolbox",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    contents_directory=".",
)
bundle = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="SDC-Testing-Toolbox",
)
