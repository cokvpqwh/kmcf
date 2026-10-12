# -*- mode: python ; coding: utf-8 -*-
"""kmcf 打包配置：PyInstaller onedir（文件夹 + kmcf.exe），无控制台窗口。

不要直接运行，用项目根目录的 build.ps1（它负责同步到 dist\\kmcf 并保留用户数据）：
    powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1

要点：
  - datas：报告模板、离线图表库 echarts.min.js（-> kmcf/assets/）、akshare 的数据文件（ths.js 等）、
    py_mini_racer（V8 动态库 + icudtl.dat）、curl_cffi 的 libcurl；
  - hiddenimports：kmcf 的全部子模块（kmcf.sections.* / kmcf.gui.* / kmcf.ai.* 等有延迟导入）；
  - 排除用不到的大模块（tkinter、matplotlib、Qt3D、QtCharts、QtMultimedia ...）。
    QtWebEngine 依赖 Qt6Quick/Qt6Qml/Qt6WebChannel/Qt6Network/Qt6Positioning/Qt6OpenGL/Qt6PrintSupport 等 DLL，
    这些 DLL 不能去掉；但程序不 import 的 Python 绑定（PySide6.QtQuick/QtQml/QtPositioning/QtOpenGL ...）可以排除——
    DLL 由 PyInstaller 的二进制依赖分析从 Qt6WebEngineCore.dll 收集，并由下面的 PE 导入表闭包保留；
  - 过滤掉 Anaconda\\Library\\bin 里的 icu*.dll：Qt6Core 依赖的是 Windows 自带的 System32\\icuuc.dll，
    打进包里的 ICU 73 会让 Qt 报 “DLL load failed: 找不到指定的程序”。构建结束时会再检查一遍 dist。
"""

import os
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules
from PyInstaller.utils.win32 import versioninfo as vi

ROOT = Path(SPECPATH).resolve()  # noqa: F821  (SPECPATH 由 PyInstaller 注入)
sys.path.insert(0, str(ROOT))

APP_NAME = "kmcf"
PRODUCT = "kmcf A股每日复盘"
_ver = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "kmcf" / "__init__.py").read_text(encoding="utf-8"))
VERSION = _ver.group(1) if _ver else "0.1.0"
VERSION_TUPLE = tuple((list(map(int, re.findall(r"\d+", VERSION)))[:4] + [0, 0, 0, 0])[:4])


# ---------------------------------------------------------------- 构建期修复：让钩子子进程能导入 PySide6
# PyInstaller 的 PySide6 钩子会在子进程里导入 PySide6.QtCore 读取插件/翻译/WebEngine 资源的位置。
# Anaconda 的 python 会把 anaconda3\Library\bin 加进 DLL 搜索目录，子进程加载到 ICU 73 的 icuuc.dll 后
# “DLL load failed while importing QtCore”，钩子就收集不到 Qt 平台插件、QtWebEngineProcess.exe 和 WebEngine 资源，
# 打出来的程序打不开。这里生成一个只在构建期使用的 sitecustomize.py（通过 PYTHONPATH 传给子进程），
# 先按完整路径加载 System32 的 ICU，与运行时的 kmcf.qt_compat.prepare() 做法相同。
_QTFIX_DIR = Path(workpath) / "qtfix"  # noqa: F821  (workpath 由 PyInstaller 注入)
_QTFIX_DIR.mkdir(parents=True, exist_ok=True)
(_QTFIX_DIR / "sitecustomize.py").write_text(
    "# 由 kmcf.spec 生成，仅用于 PyInstaller 构建期的钩子子进程\n"
    "import ctypes as _c, os as _o\n"
    "_d = _o.path.join(_o.environ.get('SystemRoot', r'C:\\Windows'), 'System32')\n"
    "for _n in ('icuuc.dll', 'icuin.dll', 'icu.dll'):\n"
    "    _p = _o.path.join(_d, _n)\n"
    "    if _o.path.exists(_p):\n"
    "        try:\n"
    "            _c.WinDLL(_p)\n"
    "        except OSError:\n"
    "            pass\n",
    encoding="utf-8",
)
os.environ["PYTHONPATH"] = os.pathsep.join([str(_QTFIX_DIR)] + [p for p in [os.environ.get("PYTHONPATH")] if p])
from kmcf import qt_compat as _qt_compat  # noqa: E402  本进程也预加载一次（collect_submodules('kmcf') 会导入 kmcf.gui，进而导入 PySide6）

_qt_compat.prepare()


def _site_dir(pkg: str) -> Path:
    import importlib.util
    spec = importlib.util.find_spec(pkg)
    return Path(spec.origin).resolve().parent


# ---------------------------------------------------------------- 数据文件 / 动态库
datas = [
    (str(ROOT / "kmcf" / "templates"), "kmcf/templates"),
]
binaries = []
hiddenimports = []

# 离线图表库：打进 _internal\kmcf\assets\echarts.min.js（程序首次运行时可从包内复制到 reports\assets）
_ECHARTS = ROOT / "reports" / "assets" / "echarts.min.js"
if _ECHARTS.is_file():
    datas.append((str(_ECHARTS), "kmcf/assets"))
else:
    print(f"[kmcf.spec] 没有找到 {_ECHARTS}，包内不带 kmcf/assets/echarts.min.js")

# akshare：akshare/data/ths.js（同花顺 hexin-v）、file_fold/calendar.json 等，通过 importlib.resources 读取
datas += collect_data_files("akshare")

# mini-racer：pip 包名 mini-racer，import 名 py_mini_racer；mini_racer.dll + icudtl.dat 用 importlib.resources 定位
_d, _b, _h = collect_all("py_mini_racer")
datas += _d
binaries += _b
hiddenimports += _h

# curl_cffi（akshare 导入时需要）：_wrapper.pyd 依赖 delvewheel 放在 site-packages\curl_cffi.libs 的 libcurl-impersonate
_curl_libs = _site_dir("curl_cffi").parent / "curl_cffi.libs"
for dll in sorted(_curl_libs.glob("*.dll")):
    binaries.append((str(dll), "curl_cffi.libs"))
hiddenimports += ["_cffi_backend"]

# 证书（requests/curl_cffi 走 certifi 的 cacert.pem；PyInstaller 自带 certifi 钩子，这里显式再加一次保险）
datas += collect_data_files("certifi")

# kmcf 自己的模块：全部子模块（复盘模块注册表、界面模块含 --selftest 用的 selftest、AI 模块 kmcf.ai.* 等）
def _disk_submodules(pkg: str) -> list[str]:
    """按目录结构列出包内全部模块（不导入）。collect_submodules 导入某个子包失败时会跳过整个子包，这里兜底。"""
    out = []
    for py in sorted((ROOT / pkg).rglob("*.py")):
        parts = py.relative_to(ROOT).with_suffix("").parts
        if "__pycache__" in parts:
            continue
        # 每一级目录都必须是包（有 __init__.py），否则不是可导入的模块
        if not all((ROOT.joinpath(*parts[:i]) / "__init__.py").is_file() for i in range(1, len(parts))):
            continue
        out.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return out


_kmcf_mods = collect_submodules("kmcf")
_kmcf_missing = sorted(set(_disk_submodules("kmcf")) - set(_kmcf_mods))
if _kmcf_missing:
    print("[kmcf.spec] collect_submodules('kmcf') 没收集到（可能导入失败），按目录补充：" + ", ".join(_kmcf_missing))
hiddenimports += _kmcf_mods + _kmcf_missing
if "kmcf.ai" not in hiddenimports:
    raise SystemExit("[kmcf.spec] 没有收集到 kmcf.ai（AI 功能），请检查 kmcf/ai/__init__.py")

# ---------------------------------------------------------------- 排除
EXCLUDES = [
    # 标准库里用不到的 GUI / 开发工具
    "tkinter", "_tkinter", "turtle", "turtledemo", "idlelib", "lib2to3", "pydoc_data",
    # 第三方：交互环境、绘图、其他 Qt 绑定
    "matplotlib", "IPython", "ipykernel", "ipywidgets", "jupyter", "jupyter_client", "jupyter_core", "notebook",
    "jedi", "parso", "pytest", "sphinx", "PIL", "scipy", "PyQt5", "PyQt6", "PySide2", "pip",
    # PySide6 里没用到的模块（只排除 Python 绑定和对应钩子收集的插件/QML；WebEngine 依赖的 DLL 仍按依赖收集）
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender", "PySide6.QtQuick3D", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtGraphs", "PySide6.QtGraphsWidgets", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSerialBus", "PySide6.QtLocation",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtUiTools", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtHttpServer",
    "PySide6.QtWebSockets", "PySide6.QtNetworkAuth", "PySide6.QtAxContainer", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebView", "PySide6.QtAsyncio", "PySide6.QtConcurrent", "PySide6.QtDBus",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.scripts",
    # 下面这些只排除 Python 绑定（.pyd、pyside6qml.abi3.dll 和对应钩子收集的 qml/定位插件），程序不 import 它们。
    # WebEngine 需要的 Qt6Quick/Qt6QuickWidgets/Qt6Qml*/Qt6OpenGL/Qt6Positioning.dll 是 Qt6WebEngineCore.dll /
    # Qt6WebEngineWidgets.dll 的链接期依赖，照样会被收集，并由下面“按 PE 导入表求依赖闭包”保留
    # （已用 pefile 核对：去掉这些 .pyd 作为起点后，闭包里的 Qt6*.dll 一个不少）。
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtPositioning",
    "PySide6.QtQuickWidgets",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

# ---------------------------------------------------------------- 过滤 ICU（关键）
_ICU_RE = re.compile(r"^icu(uc|in|dt|io|tu|test|data|i18n)?\d*\.dll$", re.IGNORECASE)


def _is_icu(entry) -> bool:
    return bool(_ICU_RE.match(os.path.basename(entry[0])))


_icu = [e for e in a.binaries if _is_icu(e)]
if _icu:
    print("[kmcf.spec] 过滤 ICU 动态库（运行时用 System32 自带的）：")
    for e in _icu:
        print(f"    - {e[0]}  <- {e[1]}")
a.binaries = [e for e in a.binaries if not _is_icu(e)]

# ---------------------------------------------------------------- 固定标准库扩展依赖的 DLL 来源（OpenSSL 等）
# PyInstaller 按 PATH 顺序查找 _ssl.pyd、_ctypes.pyd 等依赖的 DLL。从 Git Bash 构建时 PATH 里
# Git 的 mingw64\bin 排在 Anaconda 前面，结果打包了 Git 自带的（MinGW 编译、版本不同的）libssl/libcrypto。
# 改为使用“构建所用的 Python 自己实际加载的那一份”，与 PATH 无关，保证 _ssl/_hashlib 和 OpenSSL 版本匹配。
def _process_modules() -> dict[str, str]:
    import ctypes
    psapi, k32 = ctypes.WinDLL("psapi"), ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.EnumProcessModules.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong,
                                         ctypes.POINTER(ctypes.c_ulong)]
    psapi.GetModuleFileNameExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong]
    proc = k32.GetCurrentProcess()
    arr = (ctypes.c_void_p * 8192)()
    needed = ctypes.c_ulong()
    out = {}
    if psapi.EnumProcessModules(proc, arr, ctypes.sizeof(arr), ctypes.byref(needed)):
        buf = ctypes.create_unicode_buffer(1024)
        for i in range(min(needed.value // ctypes.sizeof(ctypes.c_void_p), len(arr))):
            if psapi.GetModuleFileNameExW(proc, arr[i], buf, 1024):
                out.setdefault(os.path.basename(buf.value).lower(), buf.value)
    return out


import _ssl, _hashlib, _ctypes, _sqlite3, _lzma, _bz2, _decimal, pyexpat  # noqa: E401,E402,F401  让这些 DLL 先加载

_sysroot = os.path.normcase(os.environ.get("SystemRoot", r"C:\Windows"))
_loaded = {k: v for k, v in _process_modules().items()
           if k.endswith(".dll") and not os.path.normcase(v).startswith(_sysroot)}
_PIN = re.compile(r"^(libssl|libcrypto|ffi|libffi|libexpat|liblzma|libbz2|sqlite3|libmpdec|zlib)[-\w.]*\.dll$", re.I)
_fixed = []
for _i, _e in enumerate(a.binaries):
    _k = os.path.basename(_e[0]).lower()
    if (_PIN.match(_k) and _k in _loaded and os.path.normcase(_loaded[_k]) != os.path.normcase(_e[1])
            and "/" not in _e[0].replace("\\", "/")):
        a.binaries[_i] = (_e[0], _loaded[_k], _e[2])
        _fixed.append(f"{_e[0]}: {_e[1]} -> {_loaded[_k]}")
for _f in _fixed:
    print("[kmcf.spec] 改用构建 Python 实际加载的 DLL：" + _f)

# ---------------------------------------------------------------- 精简 Qt（677MB -> 约 300MB）
# QtWebEngineCore 依赖 QtQuick/QtQml，PyInstaller 的 QtQml/QtQuick 钩子会把整个 PySide6\qml 目录
# （Quick3D、Charts、Multimedia、VirtualKeyboard ... 的 QML 插件）连同它们依赖的 Qt6*.dll 都收集进来。
# 本程序只用 QWebEngineView（Widgets），不加载任何 QML，所以：
#   1. 去掉 PySide6/qml/**、QML 调试插件、虚拟键盘输入法插件、PDF 图片插件；
#   2. 去掉 WebEngine 的 *.debug.pak / *.debug.bin（调试版 Qt 才用）和开发者工具资源；
#   3. 翻译只保留 简体/繁体中文 + 英文（qt / qtbase / qtwebengine 和 Chromium 的 locale .pak）；
#   4. 从剩下的 .pyd / 插件 / exe 出发按 PE 导入表求依赖闭包，没被引用的 PySide6\Qt6*.dll 一律去掉
#      （WebEngine 真正依赖的 Qt6Quick/Qt6Qml/Qt6WebChannel/Qt6Network/Qt6Positioning/Qt6OpenGL 等会自动保留）。
_KEEP_LOCALES = {"en-US.pak", "zh-CN.pak", "zh-TW.pak"}
_KEEP_QM = re.compile(r"^(qt|qtbase|qtwebengine)_(zh_CN|zh_TW|en)\.qm$", re.IGNORECASE)
_DROP_PLUGINS = ("PySide6/plugins/qmltooling/", "PySide6/plugins/platforminputcontexts/qtvirtualkeyboardplugin",
                 "PySide6/plugins/imageformats/qpdf")


def _norm(dest: str) -> str:
    return dest.replace("\\", "/")


def _drop(entry) -> bool:
    d = _norm(entry[0])
    name = os.path.basename(d)
    if d.startswith("PySide6/qml/") or d.startswith(_DROP_PLUGINS):
        return True
    if d.startswith("PySide6/resources/") and (".debug." in name or name.startswith("qtwebengine_devtools")):
        return True
    if d.startswith("PySide6/translations/qtwebengine_locales/"):
        return name not in _KEEP_LOCALES
    if d.startswith("PySide6/translations/") and name.endswith(".qm"):
        return not _KEEP_QM.match(name)
    return False


_n_bin, _n_dat = len(a.binaries), len(a.datas)
a.binaries = [e for e in a.binaries if not _drop(e)]
a.datas = [e for e in a.datas if not _drop(e)]


def _pe_imports(path: str) -> set[str]:
    import pefile
    try:
        pe = pefile.PE(path, fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                                               pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
        names = set()
        for attr in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
            for imp in getattr(pe, attr, None) or []:
                names.add(imp.dll.decode("ascii", "ignore").lower())
        pe.close()
        return names
    except Exception as e:  # 解析失败：保守处理，不据此删除任何东西
        print(f"[kmcf.spec] 无法解析 {path}：{e}")
        return {"*"}


_QT_DLL = re.compile(r"^qt6.*\.dll$", re.IGNORECASE)


def _is_qt_dll(entry) -> bool:
    d = _norm(entry[0])
    return d.startswith("PySide6/") and d.count("/") == 1 and bool(_QT_DLL.match(os.path.basename(d)))


_pe_entries = [e for e in list(a.binaries) + list(a.datas) if e[0].lower().endswith((".dll", ".pyd", ".exe"))]
_by_name = {}
for _e in _pe_entries:
    _by_name.setdefault(os.path.basename(_e[0]).lower(), _e)
_queue = [e for e in _pe_entries if not _is_qt_dll(e)]
_seen = {os.path.basename(e[0]).lower() for e in _queue}
_conservative = False
while _queue:
    _e = _queue.pop()
    _deps = _pe_imports(_e[1])
    if "*" in _deps:
        _conservative = True
        break
    for _dep in _deps:
        if _dep in _by_name and _dep not in _seen:
            _seen.add(_dep)
            _queue.append(_by_name[_dep])
if not _conservative:
    _unused = [e for e in a.binaries if _is_qt_dll(e) and os.path.basename(e[0]).lower() not in _seen]
    a.binaries = [e for e in a.binaries if not (_is_qt_dll(e) and os.path.basename(e[0]).lower() not in _seen)]
    print(f"[kmcf.spec] 去掉未被引用的 Qt 动态库 {len(_unused)} 个："
          + ", ".join(sorted(os.path.basename(e[0]) for e in _unused)))
print(f"[kmcf.spec] 精简：binaries {_n_bin} -> {len(a.binaries)}，datas {_n_dat} -> {len(a.datas)}")
print("[kmcf.spec] 保留的 Qt 动态库：" + ", ".join(sorted(os.path.basename(e[0]) for e in a.binaries if _is_qt_dll(e))))

# 用不到的 Qt 工具程序（PySide6 钩子一般不会收集，这里兜底）
_DROP_TOOLS = {"designer.exe", "assistant.exe", "linguist.exe", "lupdate.exe", "lrelease.exe", "uic.exe", "rcc.exe",
               "qmlls.exe", "qmllint.exe", "qmlformat.exe", "qmlcachegen.exe", "balsam.exe", "balsamui.exe",
               "qsb.exe", "svgtoqml.exe", "qmlimportscanner.exe", "qmltyperegistrar.exe"}
a.binaries = [e for e in a.binaries if os.path.basename(e[0]).lower() not in _DROP_TOOLS]

pyz = PYZ(a.pure)  # noqa: F821

version_info = vi.VSVersionInfo(
    ffi=vi.FixedFileInfo(filevers=VERSION_TUPLE, prodvers=VERSION_TUPLE, mask=0x3F, flags=0x0, OS=0x40004,
                         fileType=0x1, subtype=0x0, date=(0, 0)),
    kids=[
        vi.StringFileInfo([
            vi.StringTable("080404B0", [  # 中文（简体）+ Unicode
                vi.StringStruct("CompanyName", "kmcf"),
                vi.StringStruct("FileDescription", PRODUCT),
                vi.StringStruct("FileVersion", VERSION),
                vi.StringStruct("InternalName", APP_NAME),
                vi.StringStruct("LegalCopyright", "仅供复盘参考，不构成投资建议"),
                vi.StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
                vi.StringStruct("ProductName", PRODUCT),
                vi.StringStruct("ProductVersion", VERSION),
            ])
        ]),
        vi.VarFileInfo([vi.VarStruct("Translation", [0x0804, 1200])]),
    ],
)

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "assets" / "kmcf.ico"),
    version=version_info,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)

# ---------------------------------------------------------------- 构建结果检查
_out = Path(DISTPATH) / APP_NAME  # noqa: F821
_bad = sorted(p for p in _out.rglob("*.dll") if _ICU_RE.match(p.name))
if _bad:
    raise SystemExit("[kmcf.spec] 打包结果里仍有 ICU 动态库，会导致 Qt 加载失败：\n  " + "\n  ".join(map(str, _bad)))
_ssl_dlls = [p.name.lower() for p in _out.rglob("*.dll") if p.name.lower().startswith(("libssl", "libcrypto"))]
if not any(n.startswith("libssl") for n in _ssl_dlls) or not any(n.startswith("libcrypto") for n in _ssl_dlls):
    raise SystemExit("[kmcf.spec] 没有打包 libssl/libcrypto，_ssl 无法加载（https 全部失败）")
_ssl_src = sorted({e[1] for e in a.binaries if os.path.basename(e[0]).lower().startswith(("libssl", "libcrypto"))})
print(f"[kmcf.spec] 检查通过：无 icu*.dll；OpenSSL 动态库来自 {'; '.join(_ssl_src)}")
