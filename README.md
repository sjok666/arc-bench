# ARC-Bench Evolution GitHub + Spreadsheet 双题提交包

本包在平台根目录提供 `main.py` 和 `requirements.txt`，收纳两个决赛增量 agent：

- `engines/github/`：来自 `github-agent.zip`
- `engines/sheet/`：`agent.zip`（sheet 题目）的**最新已验证构建**。v1 打包时误用了更早的一份 `agent.zip`（第 6/7 轮），缺少其后的全部修复（模块轮次预算、第二遍重试、覆盖轮闸门、会话/拒绝契约回收、按模块的 ARIA 收窄、以及按进度而非墙钟判断轮次死活），因此 v2 只替换了 `engines/sheet/main.py` 这一个文件，其余条目（含 `engines/github/`、两份 `template/`、全部 `skills/`）与 v1 逐字节相同。

根入口只读取平台本次挂载的 `requirements.yaml`，根据规格中的工作簿、工作表、电子表格等领域词汇选择对应 agent，再将平台参数原样转交。GitHub 需求走 GitHub agent，在线表格需求走 Sheet agent。平台正式运行前会把初赛最佳应用注入输出目录；两个 agent 会保留已有 `frontend/`、`backend/` 和数据库，只在其上实现决赛 Evolution 需求。空输出目录时才使用包内通用模板作为兜底。

## 构建提交 ZIP

在 PowerShell 中运行：

```powershell
.\build-dual-task-package.ps1
```

这个目录本身就是待打包的提交源。打包时应保留根目录的 `main.py`、`requirements.txt`、`engines/` 和 runtime；不要把 `hackathon-evolution-initial-projects/` 中的应用成品打进 agent。

生成包必须在 ZIP 根目录直接含 `main.py`、`requirements.txt`，两个完整引擎位于 `engines/`，GitHub agent 所需的 `arcbench-agent-runtime/` 位于根目录。依赖版本对齐此前 sheet 成功运行记录：`openai==3.18.0`、`PyYAML==6.0.3`，Codex 固定为两份 agent 共同使用的 `0.156.1`。打包脚本会检查这些结构并拒绝覆盖已有输出文件。

## 决赛运行方式

- 官方注入基线后，agent 不复制、不清空、不重置整个输出目录。
- `template/` 仅用于官方没有提供有效基线、且输出目录确实为空的兜底情况。
- GitHub 参考图片位于公开需求目录的 `reference/` 下；模块需求引用相关图片时，模型可以读取对应图片辅助实现界面。
- 只读公开需求和公开参考图片，不探测隐藏测试。
- 如果只冲 GitHub 全量任务，在提交面板中选择 `GitHub Collaboration Platform Core Requirements`，不要选择 Stage 1/2/3。

## GitHub agent 的增量审计策略

GitHub agent 不再只按需求树逐模块生成代码。每个模块回合会先对官方注入的基线做轻量盘点，确认现有前端入口、后端路由、数据库表和权限辅助逻辑，再补齐需求涉及的缺口；它不会把初赛应用复制进输出目录，也不会重建已有项目。

实现提示会把以下跨层契约作为统一验收项：精确的可访问名称和文案、服务端权限与所有权校验、失败时不产生部分写入、数据库持久化及刷新恢复，以及 UI/API/数据库的闭环。所有模块完成后还有一轮低推理强度的契约回收，用于修复跨模块遗漏。小规格模块默认使用 `low` 推理强度，复杂模块沿用 `ARCBENCH_REASONING_EFFORT`，可通过 `ARCBENCH_SIMPLE_MODULE_MAX_CHARS=0` 关闭自动降档；最终回收可通过 `ARCBENCH_FINAL_CONTRACT_AUDIT=0` 关闭。

agent 会在 stdout 中记录带时间戳的运行诊断：模型与推理档位（不输出 key）、模块开始/结束、每次重试及耗时、代理或模型错误摘要、改动文件数量、Git checkpoint、最终契约审计和 preflight 结果。官方日志中看到 `effort=low` 或 `effort=medium` 即可判断该回合实际使用的推理强度。
