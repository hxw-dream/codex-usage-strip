# 项目状态（PROJECT_STATUS）

> 快照日期：2026-10-07 · 当前版本：**v2.2.4** · 仓库：[hxw-dream/codex-usage-strip](https://github.com/hxw-dream/codex-usage-strip)（PUBLIC, MIT）

## 一句话定位

吸附在 Codex 桌面端窗口顶栏居中的实时用量胶囊：官方限额剩余 / 中转余额 / 模型输出速率与缓存命中率，事件驱动逐像素跟随，纯手动启动、零常驻、零第三方依赖、数据只存本机。

## 当前运行状态

| 项 | 状态 |
|---|---|
| 生产实例 | v2.2.4 运行中（pythonw 改名 codex-usage.exe，经 bat/开始菜单启动） |
| 远端同步 | main = 本地（`c7bab9c`），工作区干净 |
| Release | v2.2.0（含 exe，Latest）、v2.1.3、v2.1.2；**v2.2.1~2.2.4 尚未打 Release exe** |
| 生命周期 | 手动启动；最小化/跨虚拟桌面=隐藏等待；窗口关闭=驻留 30 分钟自退（可配） |
| 数据源 | 激活 provider（config.toml 实时判定，本地代理自动同名回退 CCS）→ 左键循环 CCS 各中转 → 官方（auth.json 存在时） |

## 架构与职责

```
codex_usage_overlay.py   主程序：胶囊 UI / Win32 吸附跟随(WinEvent+SetWindowPos) / 配置 / crash.log
├── usage_sources.py     余额层：官方 /usage · one-api · SAIL 式 /v1/usage · DeepSeek · CCS 只读 + 同名回退
├── codex_metrics.py     指标层：解析 ~/.codex/sessions rollout JSONL → 输出速率/缓存命中率/模型名
├── overlay.toml         用户可调参数（6 项，越界夹紧，删文件回默认）
├── mcp_usage_server.py  备用：MCP 生命周期联动 + 文字查询（未注册，休眠）
└── 启动用量悬浮条.bat    手动入口（%LOCALAPPDATA% 通用路径 + codex-usage.exe 优先）
```

关键设计约束（红线）：零第三方依赖；绝不写 CCS 数据库/不用其 oauth refresh token；不做常驻/自启；密钥只在运行时解析。

## 胶囊信息布局（v2.2.4 现状）

```
官方视图：  62% [进度条] 3天4小时后重置 · 周余 58% · $4.50 │ 25 tok/s · 95%（常驻段，最右）
中转视图：  $8.44 剩 85% · 今日 $0.85 · SAIL API │ 25 tok/s · 95%   （无进度条——余额制无额度比例）
```

- 常驻段：速率限最近响应 10 分钟内，命中率为会话累计；hover 面板有完整三行（速率含响应 token/耗时、命中率含累计 token、模型名）
- 失败时：同源保留缓存变灰标注分钟数；异源红错；连续失败指数退避封顶 600s

## 已解决的关键问题（经验沉淀）

1. **WinEvent 回调零 Tcl 铁律**：回调在 Tcl 消息泵内、GIL 已释放，任何 Tcl 调用即 `PyEval_RestoreThread` fatal → 0xc0000409 无痕崩溃（v2.1.2 根治，pythonw 崩溃取证路径：事件日志 + python.exe 复跑抓 stderr）
2. **最小化 ≠ 关闭**：驻留计时只认窗口销毁（v2.1.1）
3. **SAIL 激活源走本地代理不转发余额**：同名条目回退到 CCS 解析真实计费地址（只读）
4. **rollout 日志账目**：`total = input + output`、input 已含 cached；速率按边界事件→token_count 配对；按最新响应多文件择新
5. **Git Bash 下 tasklist/taskkill 过滤不可靠**：进程管理一律 PowerShell

## 已知限制（未处理，均为有意取舍）

- 指标口径为「本机最近活跃会话」，无法绑定到浮条吸附的那扇窗口（rollout 无窗口归属）；桌面端与 CLI 并行使用时显示二者中最新响应的一方
- 速率含思考时间（=实际等待口径）；命中率为全会话累计（含恢复的历史线程），非最近 N 次
- exe 未签名，SmartScreen 首次拦截（说明已写入 Release）
- `mcp_usage_server.py` 休眠维护状态：cc-switch 切换会抹注册段的机制未变，恢复联动需重新追加 4 行配置
- 老的提权僵尸进程（若重现）：不持有单实例锁、无碍，重启即清

## 待办 / 可选方向

- [ ] v2.2.1~2.2.4 打包新 Release exe（流程已固化：bump 版本→同步海报→tag→venv 打包→gh release）
- [ ] 可选：命中率口径改「最近 N 次响应」；中转视图去掉「剩 N%」文字段（用户未拍板）
- [ ] 可选：代码签名消除 SmartScreen（需购证书，用户未表态）

## 文档索引

- 使用与配置：`README.md`
- 开发约定与坑：`AGENTS.md`
- 版本历史：`CHANGELOG.md`
- 排错：`crash.log` → Windows 事件日志（Application Error）
