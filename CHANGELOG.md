# 更新日志（Changelog）

本项目的版本号维护在 `codex_usage_overlay.py` 的 `__version__`，此处按发布/提交时间倒序记录。完整决策过程见 `PROJECT_STATUS.md` 与 git 历史。

## [2.2.5] — 2026-10-07

### 修复
- **胶囊内多字号基线对齐**：金额/百分比（10pt 粗体）与次行文字、速率段（8pt）原先默认垂直居中导致基线错开（如 `$12.90` 与后面文字不平）。全部标签改 `anchor="s"` 底部对齐，共享同一视觉基线；进度条同步下沿对齐。

## [2.2.4] — 2026-10-07

### 变更
- **中转/DeepSeek 视图隐藏进度条**：余额制计费不存在「剩余额度比例」，进度条（尤其无总额时的满格绿条）属误导信息，relay 模式整体隐藏轨道；官方视图保留。切换往返经真 Tk 环境验证不会错位（`before=` 锚定复位）。

## [2.2.3] — 2026-10-07

### 变更
- 速率/缓存常驻段从胶囊最左移到**最右**（次行内容之后）——用户试过左置后按观感选择右置。

## [2.2.2] — 2026-10-07

### 变更
- 速率/缓存常驻段置于胶囊**最左**（主百分比之前）。

## [2.2.1] — 2026-10-07

### 新增
- **胶囊常驻段**：直接在胶囊上显示 `25 tok/s · 95%`（输出速率 + 输入缓存命中率）。速率要求最近响应在 10 分钟内（过期隐藏防误导），命中率为会话累计、有数据即显；无数据自动收起。次行原有内容不变。

## [2.2.0] — 2026-10-06（已发 Release，含免安装 exe）

### 新增
- **模型实时指标**（新模块 `codex_metrics.py`，参考 claude-monitor 系社区方案，落到 Codex 原生日志）：
  - 输出速率：最近一次响应的输出 token ÷ 生成时长（由「工具输出/用户消息 → token_count」时间戳配对，含思考时间）
  - 输入缓存命中率：会话累计 `cached_input_tokens / input_tokens`
  - 当前模型名
  - 数据源为 `~/.codex/sessions/` rollout JSONL（桌面端与 CLI 共写），纯本地零网络，只读 token/时间戳/模型名
  - 多文件择新：跳过「旧会话被打开但无新响应」的日志
- hover 详情面板新增速率/命中率/模型三行。

## [2.1.3] — 2026-10-06（已发 Release，含免安装 exe）

### 新增
- hover 详情面板显示**今日消耗**（绿色，中转数据源）；修复 CCS 直连路径（`fetch_named` 的 `script_path` 分支）复用完整 `/v1/usage` 解析器，补回丢失的 `today_cost`。

### 修复（全项目 bug 排查，共四项）
- **点击与拉取并发**：拉取进行中点左键切换数据源不再静默失效（`_refetch_pending` 补拉标记，完成后 50ms 内重拉）
- **跨源缓存误显**：切源失败不再把上一源的余额当新源缓存值显示（缓存带 `_usage_target` 来源标记，异源显示红错）
- **运行期异常无痕**：接管 `report_callback_exception` 与 `threading.excepthook` 路由进 `crash.log`（原先 pythonw 下凭空消失）
- **今日消耗日期错位**：从 UTC 改本地日期匹配（UTC+8 凌晨 0-8 点会错标昨日为今日）

### 其他
- `AGENTS.md` 项目说明入库（补仓库地址、已验证打包命令、坑清单）

## [2.1.2] — 2026-09-20（已发 Release，含免安装 exe；首个公开发布）

### 修复
- **WinEvent 回调崩溃根治**（用户连丢三个实例后破案）：事件日志锁定 0xc0000409（ucrtbase abort），python.exe 带控制台复现取得遗言 `Fatal Python error: PyEval_RestoreThread ... thread state is NULL`。根因为 hover 面板使回调路径出现 Tcl 调用（`update_idletasks`），在 mainloop 释放 GIL 等事件时重入 Tcl 即致命。修复：回调路径彻底零 Tcl（HWND 与面板尺寸全部在 Tk 上下文缓存）。

### 其他
- **开源上线**：GitHub 公开仓库 [hxw-dream/codex-usage-strip](https://github.com/hxw-dream/codex-usage-strip)，MIT，版权人「你来我往」，提交用 GitHub 匿名邮箱
- 宣传海报（`docs/poster.html` 源文件 + PNG，Edge 无头渲染）
- PyInstaller onefile 免安装 exe（约 12MB，PIL 手绘胶囊图标，未签名）

## [2.1.1] — 2026-09-20

### 修复
- **最小化误判为关闭**：30 分钟驻留自退只针对窗口销毁；最小化/移到其他虚拟桌面（hwnd 仍存活）只隐藏、无限等待恢复（`alive_but_hidden()` 判定）。此前用户最小化半小时后浮窗永久消失。

## [2.1.0] — 2026-09-19/20

### 新增（社区方案调研后引入，参考 Claude-Code-Usage-Monitor / cc-monitor / TrafficMonitor）
- **失败退避**：连续拉取失败间隔指数翻倍（60s→…封顶 600s），成功复位
- **陈旧缓存**：失败时保留上次好数据变灰标注「缓存 X 分钟前」（仅同源），全无数据才红错
- **隐藏时停拉**：Codex 窗口消失期间跳过网络请求
- **配置外置**：`overlay.toml` 六参数（tomllib 读取，越界夹紧，删文件回默认）
- **崩溃日志**：`crash.log`（pythonw 无 stderr），超 128KB 自动截断

## [2.0.0] — 2026-09-19

### 重写（经 grill-me 三轮拷问收敛规格）
- 同名文件整体重写：深色圆角胶囊（Win11 DWM 圆角，region 回退）、顶栏水平居中吸附、WinEvent 事件驱动逐像素跟随、Z 序紧贴 Codex 正上方（非置顶，被盖同隐）
- 交互：左键循环数据源（激活→CCS 中转→官方）· hover 详情面板（350ms 延迟/250ms 宽限）· 右键仅「退出」
- `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`（不抢焦点、不进 Alt-Tab）
- `--instance` 参数隔离测试实例；`--parent-pid` 模式保持严格同生共死
- 验证：居中 719==719、z-above、移动跟随 ≤1px、单实例锁

## [1.x] — 2026-09-18/19（历史）

- 初版悬浮条（顶栏居中、one-api/DeepSeek 数据源、cc-switch 多源切换）
- MCP Apps 插件 UI 方案验证后放弃（桌面端不渲染）；MCP server 转作生命周期宿主+文字查询
- 发现 cc-switch 切 provider 会整体重写 config.toml 抹掉 mcp_servers 注册段（联动路线封存）
- 最终休眠态：纯手动 bat 启动，零常驻

---

## 未发布说明

- v2.2.1 ~ v2.2.5（胶囊指标段与对齐系列）目前只在 main 分支，未发新 Release exe；Release 页最新 exe 为 v2.2.0。
