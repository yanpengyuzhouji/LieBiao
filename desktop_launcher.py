"""Windows packaged launcher. Configuration is separate from development installs."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser


def configure_data(settings, value):
    from backend.config import normalize_data_dir
    root = normalize_data_dir(value)
    if root == Path(root.anchor):
        raise ValueError('请选择专用数据文件夹，不能直接使用磁盘根目录')
    if getattr(sys, 'frozen', False):
        install = Path(sys.executable).resolve().parent
        if root == install or install in root.parents:
            raise ValueError('数据目录必须放在程序安装目录之外，防止卸载误删数据')
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=root):
        pass
    db = root / 'scout.db'
    if db.exists():
        c = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
        try:
            if c.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or not c.execute("SELECT 1 FROM sqlite_master WHERE name='notices'").fetchone():
                raise ValueError('该目录中的数据库不是可用的猎标数据库')
        finally:
            c.close()
    settings.data_dir = root
    settings.ensure_dirs()
    settings.persist_data_dir()


def health(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=1) as response:
            data = json.load(response)
            return data if data.get('service') == 'lieBiao' else None
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--configure-data')
    parser.add_argument('--config-dir')
    parser.add_argument('--port',type=int,default=8090)
    parser.add_argument('--headless',action='store_true')
    parser.add_argument('--no-browser',action='store_true')
    args = parser.parse_args()
    config_dir = Path(args.config_dir or str(Path(os.environ.get('LOCALAPPDATA',str(Path.home()))) / 'LieBiaoDesktop'))
    os.environ['LIEBIAO_CONFIG_DIR'] = str(config_dir)
    config_dir.mkdir(parents=True,exist_ok=True)
    log_handler = RotatingFileHandler(config_dir/'desktop.log',maxBytes=2_000_000,backupCount=3,encoding='utf-8')
    logging.basicConfig(level=logging.INFO,handlers=[log_handler])
    from backend.config import settings
    settings.config_dir = config_dir
    if args.configure_data:
        configure_data(settings,args.configure_data)
        return
    configured = settings.load_persisted_data_dir()
    if not configured:
        if args.headless:
            raise ValueError('请先选择数据目录')
        from tkinter import Tk, filedialog
        dialog = Tk();dialog.withdraw()
        chosen = filedialog.askdirectory(title='首次使用：选择数据存放目录',initialdir=str(config_dir))
        dialog.destroy()
        if not chosen:
            return
        configure_data(settings,chosen)
    mutex = None
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p,ctypes.c_bool,ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        mutex = kernel.CreateMutexW(None,False,'Local\\LieBiaoDesktop-' + hashlib.sha256(str(config_dir.resolve()).encode()).hexdigest()[:20])
        if not mutex:
            raise OSError('无法创建应用实例锁')
        if ctypes.get_last_error() == 183:
            state = config_dir/'desktop-state.json'
            if state.exists() and not args.no_browser:
                port = json.loads(state.read_text(encoding='utf-8'))['port']
                if health(port): webbrowser.open(f'http://127.0.0.1:{port}')
            return
    port = args.port
    for candidate in range(args.port,args.port+20):
        existing=health(candidate)
        if existing and Path(existing.get('storage_root','')).resolve() == settings.data_dir.resolve():
            if not args.no_browser: webbrowser.open(f'http://127.0.0.1:{candidate}')
            return
        with socket.socket() as probe:
            try: probe.bind(('127.0.0.1',candidate))
            except OSError: continue
        port=candidate;break
    else:
        raise RuntimeError('没有可用的本机端口，请关闭占用端口的程序后重试')
    settings.port = port
    settings.host = '127.0.0.1'
    import uvicorn
    from backend.main import app
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_config=None,log_level='info'))
    if args.headless:
        server.run();return
    import tkinter as tk
    from tkinter import messagebox
    window=tk.Tk();window.title('猎标 · 招标公告采集系统');window.geometry('540x230');window.resizable(False,False)
    label=tk.StringVar(value='正在启动本地服务…')
    tk.Label(window,text='猎标 · 招标公告采集系统',font=('Microsoft YaHei UI',16)).pack(pady=(20,10))
    tk.Label(window,textvariable=label,wraplength=500).pack(pady=8)
    tk.Label(window,text=f'数据目录：{settings.data_dir}',wraplength=500).pack(pady=8)
    url=f'http://127.0.0.1:{port}'
    tk.Button(window,text='打开系统',command=lambda:webbrowser.open(url)).pack(pady=8)
    worker=threading.Thread(target=server.run,daemon=True);worker.start()
    deadline=time.monotonic()+60
    def ready():
        if server.started:
            label.set('运行中。最小化此窗口可继续定时采集；关闭窗口将退出服务。')
            (config_dir/'desktop-state.json').write_text(json.dumps({'port':port}),encoding='utf-8')
            if not args.no_browser: webbrowser.open(url)
        elif time.monotonic()>deadline or not worker.is_alive():
            label.set('启动失败，请查看配置目录中的 desktop.log')
        else: window.after(300,ready)
    def close():
        if not messagebox.askyesno('退出系统','退出后将停止定时采集。确认退出吗？'): return
        server.should_exit=True
        label.set('正在等待当前请求结束…')
        def stopped():
            if worker.is_alive(): window.after(300,stopped)
            else: window.destroy()
        stopped()
    window.protocol('WM_DELETE_WINDOW',close)
    window.after(300,ready);window.mainloop()


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        logging.exception('Desktop startup failed')
        if '--headless' not in sys.argv and '--configure-data' not in sys.argv:
            import tkinter as tk
            from tkinter import messagebox
            root=tk.Tk();root.withdraw();messagebox.showerror('猎标启动失败',str(exc));root.destroy()
        sys.exit(1)
