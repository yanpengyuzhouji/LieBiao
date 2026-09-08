# 猎标 · 招标信息采集管理系统 V1

这是依据《招标信息采集管理系统——分步设计文档》搭建的 V1 单机版。当前版本包含管理端页面和本地后端：SQLite 保存业务数据，文件系统保存公告快照、附件、解压文件和解析结果。

## 运行

仅查看页面时，无需安装依赖，直接双击 `index.html` 即可打开；此方式使用页面内的演示数据。

要使用真实数据库、导入、目录配置和采集 API：

```bash
python3 -m pip install -r requirements.txt
python3 run_app.py
```

程序会自动打开 `http://127.0.0.1:8090`。API 文档位于 `http://127.0.0.1:8090/api/docs`。

Windows 上建议使用 `py run_app.py` 启动。打包时在 Windows 开发机执行 `packaging\\build_windows.bat`，产物位于 `dist\\LieBiao\\LieBiao.exe`。

旧版 `.doc` 解析依赖目标电脑安装 Microsoft Word；`.docx`、`.xlsx` 和文本型 PDF 的解析组件已随程序打包。

程序启动后会通过 GitHub Releases 公开接口检查新版本；检查失败不影响采集。可通过 `LIEBIAO_UPDATE_ENABLED=false` 关闭，或用 `LIEBIAO_UPDATE_URL` 指向其他 HTTPS JSON 更新清单。发布新版本时需在 GitHub 创建对应 Release（例如 `v1.1.0`）并上传安装包。

### 采集范围约束

“回溯天数”按北京时间自然日计算：`0` 表示仅当天，`1` 表示从昨天 00:00 起。采集会在列表元数据和详情数据两个阶段校验发布时间与公告类型；缺少可解析发布时间的记录不会自动入库，并会写入“策略过滤”日志。任务配置的请求间隔会覆盖列表、详情和附件请求，最多尝试次数也会在运行时强制执行。

## 目录

- `index.html`：页面骨架、导航、详情抽屉和导入弹窗
- `styles.css`：V1 管理端视觉样式与响应式布局
- `app.js`：视图渲染、筛选、抽屉、导入和标记交互；后端不可用时保留演示数据
- `backend/main.py`：FastAPI API、静态页面托管和 Windows 单机入口
- `backend/db.py`：SQLite 表结构、索引和基础平台/关键词/任务初始化
- `backend/adapters.py`：平台统一适配器；包含公开列表、详情、附件和健康检查
- `backend/service.py`：公告入库、版本、字段抽取、关键词证据、附件下载和任务运行
- `backend/parsers.py`：PDF、DOCX、XLSX、文本/HTML 解析与 ZIP 安全解压
- `backend/storage.py`：数据目录和文件路径安全控制
- `data/`：本地运行时生成，包含 `scout.db`、`raw`、`extracted`、`preview`、`temp` 和 `logs`

## 真实采集和验收

后端已经提供 `/api/notices`、`/api/notices/{id}`、`/api/notices/{id}/hits`、`/api/notices/{id}/files`、`/api/crawl-jobs`、`/api/imports/url`、`/api/imports/excel`、`/api/imports/file` 和 `/api/settings/storage` 等接口。

已接入南方电网、ECP2.0、国网交易专区、中国石化物资、中国华能和大唐集团。中国石化支持公开列表与正文采集；华能支持公开详情解析；华能、大唐列表若触发平台安全验证会明确失败，不会把验证页误入库。验证方法：

```bash
python -m unittest discover -s tests -v
python scripts/live_acceptance.py
```

详细平台边界和 V2 结论见 [`docs/V2可行性与V1验收说明.md`](docs/V2可行性与V1验收说明.md)。平台采集严格使用公开页面或已授权会话；验证码、短信和 CA 不做绕过。
