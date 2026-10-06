# PyInstaller spec for FEMTracker.exe (one-folder build: fast start, fewer antivirus false alarms).
# Build with packaging\windows\build.bat, or:
#   pyinstaller --noconfirm packaging/windows/femtracker.spec
import os
import re
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
ICON = os.path.join(SPECPATH, "femtracker.ico")
VERSION = re.search(r'__version__ = "([^"]+)"', open(os.path.join(ROOT, "femtracker", "__init__.py")).read()).group(1)

hidden = ["femtracker.server", "femtracker.export", "femtracker.notebook", "femtracker.agent.run_agent",
         "femtracker.agent.populate", "nh3", "markdown_it", "mdurl"]
for pkg in ("uvicorn", "multipart", "python_multipart", "markdown_it"):
    try:
        hidden += collect_submodules(pkg)
    except Exception:
        pass

# Windows file properties (right-click FEMTracker.exe > Properties > Details)
version_file = None
if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                     VarFileInfo, VarStruct, VSVersionInfo)
    nums = tuple(int(x) for x in (VERSION.split(".") + ["0", "0", "0"])[:4])
    info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=nums, prodvers=nums),
        kids=[StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "FEM Tracker"),
            StringStruct("FileDescription", "FEM Tracker"),
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", "FEMTracker"),
            StringStruct("OriginalFilename", "FEMTracker.exe"),
            StringStruct("ProductName", "FEM Tracker"),
            StringStruct("ProductVersion", VERSION)])]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])])])
    version_file = os.path.join(workpath, "file_version_info.txt")
    with open(version_file, "w", encoding="utf-8") as fh:
        fh.write(str(info))

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, "femtracker", "static"), os.path.join("femtracker", "static"))],
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "numpy", "PIL", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="FEMTracker",
    console=True,           # the console window shows the log; closing it stops FEM Tracker
    icon=ICON if sys.platform == "win32" else None,   # macOS would need an .icns
    version=version_file,
    upx=False,              # UPX-packed exes trip antivirus scanners
)
coll = COLLECT(exe, a.binaries, a.datas, name="FEMTracker", upx=False)
