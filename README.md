# Python 项目启动器

Windows 本机的 Python 项目管理器。打开网页点一下就能启动项目，不用开终端、找目录、回忆启动命令。

```
Web UI  →  Flask API  →  subprocess + psutil  →  本机 Python 项目
```

## 功能

- **项目管理** —— 增删改查，配置持久化到 SQLite；每次启动自动保留上一代数据库备份（`data/projects.db.bak`）
- **数据备份** —— 一键下载全部项目配置为 JSON 文件；导入恢复（按名称合并，绝不删除现有项目）
- **进程控制** —— 启动 / 停止 / 重启，停止时连带整个进程树
- **状态监控** —— PID、CPU、内存、运行时间，2 秒刷新
- **日志** —— stdout / stderr 分流显示，每次运行独立文件，项目停止后仍可回看
- **自动启动** —— 管理器启动时自动拉起标记了 `auto_start` 的项目
- **崩溃感知** —— 进程被外部杀掉或异常退出，页面状态会自动跟上
- **重启不杀子进程** —— 管理器自己重启后，通过 PID + 进程创建时间重新接管仍在运行的项目

## 安装与启动

用 `uv`（推荐，仓库已配好）：

```bash
uv sync
uv run python app.py
```

或者用标准 Python：

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

然后打开 <http://127.0.0.1:5000>

数据库和日志目录首次启动时自动创建，不需要手动初始化。

### 可选环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PPM_HOST` | `127.0.0.1` | 监听地址。改之前请读下面的安全说明 |
| `PPM_PORT` | `5000` | 监听端口 |
| `PPM_DEBUG` | `0` | 设 `1` 打开调试日志 |
| `PPM_MONITOR_INTERVAL` | `1.5` | 状态采样间隔（秒） |
| `PPM_AUTO_START_INTERVAL` | `0.8` | 自动启动的项目之间的间隔（秒） |
| `PPM_DB_PATH` | `data/projects.db` | 数据库位置 |

例如换端口：

```bash
PPM_PORT=6000 uv run python app.py
```

## 使用

添加项目时填四样东西：

| 字段 | 例子 | 说明 |
| --- | --- | --- |
| 项目名称 | `PC TTS` | 随便起 |
| 工作目录 | `D:\Python\Project\pc_tts` | 启动时的当前目录，等同于手动 `cd` 进去 |
| 启动命令 | `uv run python server.py` | 任何 Windows 能执行的命令 |
| 端口 | `8774` | 选填。填了卡片上会有可点击链接，并显示端口是否已监听 |

启动命令不限于 `python xxx.py`，下面这些都可以：

```
python app.py
uv run python server.py
.venv\Scripts\python.exe app.py
python -m myapp
npm run dev
```

环境变量按需添加。管理器默认会给子进程设 `PYTHONUNBUFFERED=1` 和
`PYTHONIOENCODING=utf-8`（让日志即时可见、中文不乱码），你可以配置同名变量覆盖它们。

## 备份与恢复

顶栏右侧：

- **下载数据** —— 把全部项目配置导出为 `projects-backup-年月日-时分秒.json`，存到浏览器默认下载目录。建议加完项目就点一次，改动多的话定期点。
- **导入数据** —— 选之前下载的备份文件恢复。规则：
  - 按名称匹配：已有同名项目 → 更新配置；没有 → 新增
  - **不会删除任何现有项目**（导入只做加法和更新）
  - 文件里某条数据不合法只跳过那一条，原因显示在结果里

  恢复的是配置，不包含运行状态——导入后项目都是停止状态，需要的话手动启动。

配合启动时的自动备份（`data/projects.db.bak`），同一份配置有三层：当前库、上代备份、你手里下载的 JSON 文件。

## 安全

**默认只监听 `127.0.0.1`，请保持这个设置。**

这个工具的核心功能就是执行用户配置的任意命令，而 V1 没有任何身份验证。
把 `PPM_HOST` 改成 `0.0.0.0` 等于允许局域网内任何人在你的机器上执行任意代码。
真的需要局域网访问时，请先自己加上认证和 HTTPS。

改成非回环地址时，启动横幅会打印一条警告。

## API

统一返回格式：

```json
{ "success": true, "message": "项目启动成功", "data": {} }
{ "success": false, "message": "项目已经在运行" }
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/projects` | 列表 + 实时状态 + 运行计数 |
| GET | `/api/projects/{id}` | 单个项目详情 |
| POST | `/api/projects` | 创建 |
| PUT | `/api/projects/{id}` | 修改（运行中不会自动重启） |
| DELETE | `/api/projects/{id}` | 删除（运行中返回 409） |
| GET | `/api/projects/export` | 下载全部配置的 JSON 备份文件 |
| POST | `/api/projects/import` | 导入备份（按名称合并） |
| POST | `/api/projects/{id}/start` | 启动 |
| POST | `/api/projects/{id}/stop` | 停止 |
| POST | `/api/projects/{id}/restart` | 重启 |
| GET | `/api/projects/{id}/status` | 状态 |
| GET | `/api/projects/{id}/logs` | 日志（支持 `run_id`、`stdout_at`、`stderr_at`） |

状态码：`200` 成功、`400` 参数错误、`404` 项目不存在、`409` 状态冲突、`500` 服务端错误。

## 项目结构

```
python_manage/
├── app.py                      入口，装配 Flask
├── config.py                   配置（可用环境变量覆盖）
├── requirements.txt
├── manager/
│   ├── db.py                   SQLite 连接与建表
│   ├── project_manager.py      项目 CRUD 与校验
│   ├── process_manager.py      进程生命周期与状态采集（核心）
│   └── logger.py               日志文件读写与编码兜底
├── models/
│   └── project.py              Project 数据类与状态枚举
├── routes/
│   ├── projects.py             CRUD 接口
│   ├── processes.py            控制与日志接口
│   └── helpers.py              统一响应格式
├── templates/index.html
├── static/css/style.css
├── static/js/app.js
├── tests/                      40 个测试
├── data/projects.db            自动创建
└── logs/project_{id}/          自动创建
```

## 状态说明

| 状态 | 界面 | 含义 |
| --- | --- | --- |
| `RUNNING` | 🟢 运行中 | 正常运行 |
| `STARTING` | 🟡 启动中 | 进程刚创建，还在 2 秒观察期内 |
| `STOPPING` | 🔵 停止中 | 正在终止进程树 |
| `STOPPED` | ⚪ 已停止 | 未启动，或正常退出（退出码 0），或用户手动停止 |
| `ERROR` | 🔴 异常退出 | 启动失败，或非 0 退出码 |

## 测试

```bash
uv run python -m pytest
```

40 个测试全部通过（约 70 秒，因为会真的创建子进程）：

- `test_projects.py` —— CRUD、字段校验、排序
- `test_processes.py` —— 启停重启、重复启动拦截、外部杀进程、命令错误、目录不存在、多项目并行
- `test_logs.py` —— 中文与 GBK 编码、stdout/stderr 分流、增量读取、历史运行、路径穿越防护
- `test_recovery.py` —— 管理器重启后接管、PID 复用防护、自动启动

## 已知限制

1. **`shell=True` 带来一层 cmd.exe**
   为了支持 `npm run dev`、`.bat` 这类需要 shell 解析的命令，启动时走 `shell=True`，
   因此记录的 PID 是 cmd.exe，真正的 python.exe 是它的子进程。CPU/内存按整棵进程树
   汇总，停止时递归杀整棵树，所以功能上没有影响，只是任务管理器里会多一个 cmd.exe。

2. **被接管的进程拿不到退出码**
   管理器重启后重新接管的进程，没有 `Popen` 句柄，退出时无法读到退出码，
   一律记为 `STOPPED` 而不是 `ERROR`。

3. **外部杀进程会显示为「异常退出」**
   从任务管理器结束任务时退出码非 0，因此状态是 `ERROR` 而不是 `STOPPED`。
   从「是否在运行」的角度看两者等价，都会显示启动按钮。

4. **CPU 百分比是进程树之和，100% = 占满一个核心**
   多线程项目可能超过 100%。这是 psutil 的原生语义，没有按核心数归一化。

5. **端口监听检测可能不完整**
   `psutil.net_connections()` 在权限受限时会失败，此时端口状态一律显示「未监听」，
   不影响其他功能。

6. **日志不自动清理**
   每次启动生成一个文件，不会自动删除。长期使用需要手动清理 `logs/`。

7. **「清空当前日志」只清视图**
   进程可能正持有文件句柄，截断文件会让写入位置错乱，所以按钮只清空浏览器里的显示，
   磁盘上的日志文件保留。

8. **单进程 Flask 开发服务器**
   本机自用够了，没有引入 waitress/gunicorn。多标签页同时打开也没问题。
