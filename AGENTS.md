# AGENTS.md — Codex 用量悬浮条（Codex_Display）

## 项目定位
吸附在 Codex 桌面端窗口顶栏水平居中的实时用量胶囊：显示官方限额剩余百分比或中转余额，窗口移动/缩放时事件驱动逐像素跟随。纯手动启动、零常驻，数据只存本机。

开源仓库：https://github.com/hxw-dream/codex-usage-strip （MIT，Release 挂免安装 exe）

## 技术栈与依赖
- Python 3.12+（用到 tomllib），仅标准库：tkinter / ctypes / urllib / sqlite3。
- 零第三方依赖是硬性设计约束，无需 pip install、无需虚拟环境。
- 仅 Windows 10/11：依赖 WinEvent 钩子、DWM 圆角/cloaked 属性、MSIX 安装路径校验。

## 运行方式
- 前置：Python 装在 `%LOCALAPPDATA%\Programs\Python\Python312`（bat 写死了该路径，无则回退）。
- 启动（二选一）：双击 `启动用量悬浮条.bat`，或 Win 键搜 "codex" 回车。bat 优先用 `%LOCALAPPDATA%\Programs\Python\Python312\codex-usage.exe`（pythonw 的改名副本，任务管理器显示为 codex-usage），缺失则回退 pythonw.exe。免安装用户直接用 Release 里的 exe。
- 交互：左键循环数据源；悬停展开详情面板（含余额/限额/今日消耗）；右键仅「退出」。
- 退出：右键浮条 → 退出。Codex 窗口真正关闭（最小化/移到其他虚拟桌面不算，会一直等待）后驻留 30 分钟自动退出（`overlay.toml: gone_exit_minutes`）。
- 单实例锁（命名互斥体 `Local\codex_usage_overlay`，`--instance` 后缀可隔离测试实例）防重复启动。

## 数据源行为
- 每次取数都重新读 `~/.codex/config.toml` 判断激活 provider（改了即时生效，无需重启）。
- 左键点击循环切换：当前激活 provider → cc-switch 各中转 → OpenAI 官方（仅 `~/.codex/auth.json` 存在时进循环）。
- 激活 provider 指向不提供余额接口的本地代理时，按同名条目到 cc-switch 库解析真实计费地址（只读）。
- 悬停详情面板显示余额/限额构成、重置倒计时、今日消耗（本地日期）、更新时间与版本号。
- 拉取失败保留缓存值变灰标注分钟数（仅同一数据源；切源失败显示红错）；连续失败指数退避封顶 `max_backoff_seconds`；窗口隐藏期停发请求。

## 测试
无测试框架、无 CI（用户明确不要为该项目写测试）。最小静态检查（已验证通过）：

    python -m py_compile codex_usage_overlay.py usage_sources.py mcp_usage_server.py

功能验证靠手动：启动后确认吸附跟随、悬停详情、左键切换数据源、右键退出。改动生命周期逻辑时用内联脚本对 `_tick` 场景做逻辑推演（最小化/关闭/恢复三分支）。

## 构建与打包（已验证，v2.1.2 起Release 在用）
在临时 venv 里装 PyInstaller 打 onefile exe（不污染全局环境）：

    python -m venv "$TEMP/pyi-venv"
    "$TEMP/pyi-venv/Scripts/pip" install pyinstaller
    # 图标用 PIL 画（胶囊进度样式），或跳过 --icon
    "$TEMP/pyi-venv/Scripts/pyinstaller" --onefile --noconsole --name codex-usage-strip --clean codex_usage_overlay.py

产物 `dist/codex-usage-strip.exe`（约 12MB），冒烟测试后用 `gh release upload <tag> <exe> --clobber` 挂到 Release。build/、dist/、*.spec、icon.ico 均被 .gitignore 排除。exe 未签名，SmartScreen 会拦一次。

## 目录结构
- `codex_usage_overlay.py` — 主程序：UI、Win32 吸附跟随、配置加载、crash.log
- `usage_sources.py` — 数据层：官方 /usage、one-api、SAIL 式 /v1/usage、DeepSeek、cc-switch 只读
- `mcp_usage_server.py` — MCP server（进程级联动 + 文字查询），因 cc-switch 切 provider 会抹掉注册段而停用，仅备用
- `启动用量悬浮条.bat` — 手动启动入口
- `overlay.toml` — 用户可调参数（6 项，越界自动夹回 LIMITS 范围）
- `docs/` — 海报（poster.html 源文件 + poster.png，Edge 无头 `--screenshot` 可重渲染）；`crash.log` — 运行时生成，已 gitignore

## 代码约定
- 注释与 docstring 用英文，UI 文案用简体中文；模块顶部 docstring 写行为契约（Contract）。
- 版本号只在 `codex_usage_overlay.py` 的 `__version__` 维护；发版同步 `docs/poster.html` 面板里的版本字符串并打 tag 发 Release。
- 数据源按回退链自动降级（`fetch_relay`：one-api → custom-usage → deepseek）。
- 配置读取：DEFAULTS 内置默认 + LIMITS 逐项夹取；overlay.toml 缺失/损坏静默回默认。
- 密钥运行时从 `~/.codex/auth.json`、config.toml（experimental_bearer_token / env_key）、环境变量解析，绝不硬编码。

## 已知坑与注意事项
- WinEvent 回调运行在 Tcl 消息泵内：禁止调用任何 winfo_* / update_idletasks 等 Tcl 接口（会触发 0xc0000409 崩溃，实测遗言 `PyEval_RestoreThread ... thread state is NULL`），只能做 Win32 调用并读取 Tk 上下文中已缓存的 HWND/尺寸（`self._hwnd/_panel_*`）。
- overrideredirect 窗口的显隐不要用 withdraw/deiconify（与 Tk 冲突），一律用 SetWindowPos。
- pythonw 没有 stderr：所有异常路径（Tk 回调 `report_callback_exception`、线程 `threading.excepthook`、main）必须路由进 `log_crash()` 写入 crash.log（超 128KB 自动截断）。pythonw 静默崩溃用事件日志（Application Error）+ python.exe 带控制台复跑取证。
- 退出用 `os._exit(0)`，不要用 Tk destroy()（回调内不可靠）。
- 吸附目标双重校验：进程名 chatgpt.exe + MSIX 路径含 openai.codex，否则会误吸 ZCode.exe 等窗口；宿主窗口可能是 cloaked 状态，可见性判断要查 DWM 属性 14。
- Python 升级后需重新复制 pythonw.exe 为 codex-usage.exe。
- 今日消耗按本地日期匹配（不是 UTC）。
- 探测/控制脚本要与浮条 DPI 感知一致（`SetProcessDpiAwareness(2)`），否则 GetWindowRect/命中测试坐标全错（175% 缩放实测）。
- 缓存显示必须带来源标记（`_usage_target`）：跨源失败不能把上一源的余额当新源缓存值。

## 禁止事项
- 不引入任何第三方依赖（包括"顺手"加 requests）。
- 不把密钥、令牌写入代码或提交。
- cc-switch 数据库（`~/.cc-switch/cc-switch.db`）只读，绝不写入；不使用其中官方条目的 oauth refresh token（会破坏 CCS 自身令牌轮换）。
- 不提交 build/、dist/、crash.log、*.spec（.gitignore 已覆盖）。
- 不做常驻后台、开机自启等改变"纯手动"定位的改动；浮条生命周期不跟随 cc-switch（已评估否决）。

## 常见任务速查
- 改外观/吸附逻辑：`codex_usage_overlay.py` 顶部常量（TARGET_EXE、BAR_W、配色）+ UsageStrip 渲染方法。
- 改数据源/新增协议：`usage_sources.py`，在 fetch_relay 回退链与 fetch_named 分发处接入。
- 改可调参数：`overlay.toml` + DEFAULTS/LIMITS。
- 排错：先看 `crash.log`，再看 Windows 事件日志 Application Error。
- 发版：bump `__version__` → 同步 poster.html 版本串 → commit + tag → 重新打包 exe → `gh release create`。
