# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, copy_metadata
from pathlib import Path

project = Path(SPECPATH).parent
datas = [(str(project / name), '.') for name in ('index.html', 'styles.css', 'app.js')]
binaries = []
hiddenimports = ['socksio', 'backend.main', 'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on']
tmp_ret = collect_all('tzdata')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('fitz')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('docx')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('openpyxl')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
try:
    # paddle.jit.sot 在当前环境（PaddlePaddle 3.3.0 / Python 3.12）导入即段错误，
    # 会导致 PyInstaller 的子模块扫描子进程崩溃退出。运行时不会用到它，故整体排除。
    tmp_ret = collect_all('paddle', filter_submodules=lambda name: not name.startswith('paddle.jit.sot'),
                          exclude_datas=['jit/sot'])
    datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
    for package in ('paddleocr', 'paddlex'):
        tmp_ret = collect_all(package)
        datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
    # paddlex 在运行时通过 importlib.metadata 校验依赖是否可用，而 PyInstaller 默认
    # 不打包 .dist-info，冻结后会误判依赖缺失并在建管线时抛 DependencyError。
    # 故把这三个包及其必需依赖的元数据一并复制进包内。
    import importlib.metadata
    from packaging.requirements import Requirement
    meta_dists = {'paddle', 'paddleocr', 'paddlex'}
    for dist in ('paddlex', 'paddleocr', 'paddlepaddle'):
        for requirement in importlib.metadata.requires(dist) or []:
            # 不区分 extra：base/cv/ocr 等任一 extra 的依赖在校验时同样会被检查，
            # 未安装的会在 copy_metadata 处被跳过。
            meta_dists.add(Requirement(requirement).name)
    for dist in sorted(meta_dists):
        try:
            datas += copy_metadata(dist)
        except Exception:
            pass
    model_root = Path(SPECPATH) / 'models'
    if model_root.exists():
        datas.append((str(model_root), 'models'))
except ImportError:
    pass


a = Analysis(
    ['../desktop_launcher.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'IPython', 'pytest'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='LieBiao',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='LieBiao',
)
