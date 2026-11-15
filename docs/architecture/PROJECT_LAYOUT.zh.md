# 当前项目目录

本分支按职责组织。当前有 WSL2 Codex／Claude Code 开发、冻结 Python 程序独立测试、Direct 基线三条实验流程。

| 目录 | 职责 |
|---|---|
| `coding4game_gui/benchmark/code/` | coding-agent Adapter、公开任务模板、多协议模型网关、游戏工具桥、开发会话、程序归档与最终测试 |
| `coding4game_gui/benchmark/direct/` | Direct 分步基线、模型调用和执行记录 |
| `coding4game_gui/benchmark/scenarios/` | 评测场景定义，不包含模型凭据 |
| `coding4game_gui/benchmark/evidence/` | 日志、账本、截图、时间线和查看器 |
| `coding4game_gui/benchmark/windows/` | Windows 宿主上的启动、存档、输入、计时、回合管理和评分 |
| `coding4game_gui/runtime_client/` | 可独立复制到 Linux 的 MCP/HTTP 客户端及通用传输、打包格式 |
| `coding4game_gui/wsl2/` | 唯一 Codex/Claude Code + Python 实验后端、基线克隆、监督器、依赖锁与独立最终测试 |
| `scripts/start.ps1`、`scripts/game_tui.py` | 首次安装预览和操作菜单 |
| `scripts/launchers/` | 安装、开发、独测、补齐独测和 baseline 的通用 PowerShell 入口 |
| `scripts/calibration/` | 录制、校准、存档操作和新游戏只读预检 |
| `scripts/acceptance/` | 显式真实 WSL2/游戏验收 |
| `scripts/diagnostics/` | 宿主和驱动探针 |
| `.agents/skills/` | 游戏接入、菜单录制和运行证据分析 |
| `tests/` | 按 benchmark/code、direct、evidence、game、wsl2、runtime_client、tools 分组 |

## 入口

```powershell
./scripts/start.ps1
python -m coding4game_gui.benchmark --help
python -m coding4game_gui.benchmark develop --help
python -m coding4game_gui.benchmark test --help
python -m coding4game_gui.benchmark direct --help
python scripts/setup_wsl2_runtime.py --help
```

`develop` 和 `test` 默认预览。实际执行需要 `--execute`，开发还需要 `--confirm-paid-run`。Direct 执行需要付费确认。

## 不随源码迁移的数据

`snapshots/` 保存本机存档，`out/` 保存运行与诊断证据。WSL2 基线由操作者单独准备，凭据配置留在宿主。

`games/` 定义游戏启动和输入驱动。`coding4game_gui/benchmark/scenarios/` 定义任务、起点、上限和评分。`coding4game_gui/paths.py` 统一定位仓库数据与包内资源。

## 文档与历史

[文档导航](../README.md)汇总当前说明。[项目目的](../PROJECT_PURPOSE.zh.md) · [原机证据索引](../SUPPORTED_GAMES.md#original-host-evidence)
