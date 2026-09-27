# PyInstaller spec for the portable tray app. Build with: python scripts\build_exe.py
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo)

from clockdisplay import __version__

ROOT = Path(SPECPATH).parent
ver = tuple(int(p) for p in __version__.split(".")) + (0,)

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=ver, prodvers=ver),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("ProductName", "ClockDisplay"),
            StringStruct("FileDescription", "Claude usage on a SmallTV-Ultra display"),
            StringStruct("FileVersion", __version__),
            StringStruct("ProductVersion", __version__),
            StringStruct("OriginalFilename", "ClockDisplay.exe"),
        ])]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

a = Analysis(
    [str(ROOT / "packaging" / "tray_entry.py")],
    pathex=[str(ROOT / "src")],
    # pystray picks its backend at runtime and keyring finds backends via entry points,
    # so static analysis misses both. win32ctypes (keyring's Credential Manager access)
    # likewise picks its ctypes/cffi implementation at runtime.
    hiddenimports=["pystray._win32", "keyring.backends.Windows", *collect_submodules("win32ctypes")],
    datas=copy_metadata("keyring"),
    excludes=["tkinter", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="ClockDisplay",
    console=False,
    upx=False,  # UPX-packed exes trip more antivirus false positives
    icon=str(ROOT / "build" / "clockdisplay.ico"),
    version=version_info,
)
