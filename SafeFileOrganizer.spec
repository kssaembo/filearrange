# Build on Windows: python -m PyInstaller --noconfirm --clean SafeFileOrganizer.spec
from pathlib import Path
root = Path(SPECPATH)
a = Analysis([str(root / 'main.py')], pathex=[str(root)], binaries=[], datas=[],
             hiddenimports=[], hookspath=[], runtime_hooks=[], excludes=['PySide6.QtWebEngineCore'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='SafeFileOrganizer',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, manifest=str(root / 'app.manifest'))
