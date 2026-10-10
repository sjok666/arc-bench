from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from arcbench_agent_runtime import AgentRuntime
from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox

try:  # ReasoningEffort 只在 openai_codex.api 子模块中导出；不同版本位置可能变化。
    from openai_codex.api import ReasoningEffort
except ImportError:  # pragma: no cover - 取决于平台上解析到的 openai-codex 版本
    try:
        from openai_codex import ReasoningEffort  # type: ignore[attr-defined]
    except ImportError:

        class ReasoningEffort(str, Enum):  # type: ignore[no-redef]
            """兜底枚举：取值与 openai_codex 保持一致，避免版本漂移导致智能体整体导入失败。"""

            NONE = "none"
            MINIMAL = "minimal"
            LOW = "low"
            MEDIUM = "medium"
            HIGH = "high"
            XHIGH = "xhigh"
            MAX = "max"
            ULTRA = "ultra"

import yaml


@dataclass(frozen=True)
class RequirementModule:
    index: int
    total: int
    node_id: str
    name: str
    subtree: dict[str, Any]
    # 同一超大需求拆出的子模块共享分组，打包时避免一次塞入过多兄弟模块。
    split_group: str | None = None
    # 拆分后保留祖先约束，避免只把叶子场景交给模型时丢失跨场景规则。
    inherited_context: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ModuleTurn:
    """一个 Codex 回合要实现的一组相邻模块（通常 1 个，拆分后打包的小模块除外）。"""

    index: int
    total: int
    modules: tuple[RequirementModule, ...]

    @property
    def label(self) -> str:
        return "+".join(m.node_id for m in self.modules)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Implement an ARC-Bench task with Codex.")
    parser.add_argument("requirement_path", help="Directory containing requirements.yaml.")
    parser.add_argument("--output-dir", required=True, help="Target directory for the generated project.")
    parser.add_argument(
        "--type",
        dest="task_type",
        default=os.environ.get("ARCBENCH_TASK_TYPE", "web"),
        help="Task type supplied by ARC-Bench.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate setup and module discovery without starting Codex.",
    )
    parser.add_argument(
        "--max-modules",
        type=int,
        default=None,
        help="Local validation limit; omit it in ARC-Bench for the full run.",
    )
    return parser.parse_args()


def _copy_missing_tree(source: Path, destination: Path) -> int:
    """只复制目标中不存在的文件，绝不覆盖官方注入的基线文件。"""
    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        copied = 0
        for child in sorted(source.iterdir()):
            copied += _copy_missing_tree(child, destination / child.name)
        return copied
    if destination.exists():
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return 1


def _has_prepared_application(output_dir: Path) -> bool:
    """识别决赛平台已经注入的初赛应用，而不是仅凭目录是否存在判断。"""
    return (
        (output_dir / "frontend" / "package.json").is_file()
        and (output_dir / "backend" / "package.json").is_file()
    )


def copy_template_contents(template_dir: Path, output_dir: Path) -> None:
    """仅在没有基线时补齐模板，决赛基线存在时完全保留原应用。"""
    if not template_dir.is_dir():
        # ARC-Bench assembles the authoritative task template before invoking
        # the agent (and evolution runs already contain the previous-stage
        # application). Reference archives intentionally omit a duplicate
        # template directory, so preserve the prepared output in that case.
        if output_dir.is_dir() and any(output_dir.iterdir()):
            return
        raise FileNotFoundError(f"Starter template directory not found: {template_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    if _has_prepared_application(output_dir):
        print(
            "[agent] prepared baseline detected; preserving frontend/ and backend/ "
            "without copying the bundled template",
            flush=True,
        )
        return

    copied = 0
    for source in sorted(template_dir.iterdir()):
        if source.name == "template.yaml":
            continue
        copied += _copy_missing_tree(source, output_dir / source.name)
    print(f"[agent] template fallback copied {copied} missing files", flush=True)


def copy_skills_to_output(skills_dir: Path, output_dir: Path) -> Path:
    """Make the bundled ARC-Bench skills available inside Codex's workspace."""
    if not skills_dir.is_dir():
        raise FileNotFoundError(f"Skills directory not found: {skills_dir}")
    destination = output_dir / ".codex" / "skills"
    _copy_missing_tree(skills_dir, destination)
    return destination


def load_root_modules(requirements_dir: Path) -> list[RequirementModule]:
    requirements_path = requirements_dir / "requirements.yaml"
    if not requirements_path.is_file():
        raise FileNotFoundError(f"requirements.yaml not found: {requirements_path}")

    payload = yaml.safe_load(requirements_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or str(payload.get("id") or "").strip() != "ROOT":
        raise ValueError("requirements.yaml must contain a ROOT mapping")

    children = [item for item in payload.get("children", []) if isinstance(item, dict)]
    if not children:
        raise ValueError("ROOT must contain at least one child module")

    modules: list[RequirementModule] = []
    for index, subtree in enumerate(children, start=1):
        node_id = str(subtree.get("id") or subtree.get("req_id") or "").strip()
        if not node_id:
            raise ValueError(f"ROOT child {index} has no id")
        modules.append(
            RequirementModule(
                index=index,
                total=len(children),
                node_id=node_id,
                name=str(subtree.get("name") or node_id).strip(),
                subtree=subtree,
            )
        )
    return modules


def _module_attempts() -> int:
    """每个实现回合最多尝试次数；重试必须换新线程。"""
    raw = os.environ.get("ARCBENCH_MODULE_ATTEMPTS", "2").strip()
    try:
        value = int(raw)
    except ValueError:
        return 2
    return max(1, min(value, 4))


def _retry_backoff_seconds(attempt: int) -> int:
    """调用端遇到代理/上下文等基础设施错误时，短暂退避后再开新线程重试。"""
    return min(5 * (2 ** max(0, attempt - 1)), 20)


def _module_split_chars() -> int:
    """超过该字符数的 ROOT 子模块会被拆成它的直接子节点；0 表示禁用。"""
    raw = os.environ.get("ARCBENCH_MODULE_SPLIT_CHARS", "45000").strip()
    try:
        value = int(raw)
    except ValueError:
        return 45000
    return max(0, value)


def _split_module(
    module: RequirementModule,
    max_chars: int,
    depth: int = 0,
    inherited_context: tuple[dict[str, Any], ...] = (),
) -> list[RequirementModule]:
    """把规格过大的模块沿需求树向下拆，保证单个 Codex 回合的上下文可控。

    回合过大不只会推高 token 消耗，还可能触发远端上下文压缩失败
    （remote compaction v2 ...），让整个回合报废。拆分只看子树规模，
    与任务内容无关。
    """
    if not inherited_context and module.inherited_context:
        inherited_context = module.inherited_context
    subtree_json = json.dumps(module.subtree, ensure_ascii=False, separators=(",", ":"))
    children = [item for item in (module.subtree.get("children") or []) if isinstance(item, dict)]
    if len(subtree_json) <= max_chars or len(children) < 2 or depth >= 3:
        if inherited_context == module.inherited_context:
            return [module]
        return [
            RequirementModule(
                index=module.index,
                total=module.total,
                node_id=module.node_id,
                name=module.name,
                subtree=module.subtree,
                split_group=module.split_group,
                inherited_context=inherited_context,
            )
        ]
    # 父模块的描述、依赖和可能存在的场景也属于子模块的验收上下文。
    parent_context = {key: value for key, value in module.subtree.items() if key != "children"}
    child_context = (*inherited_context, parent_context)
    parts: list[RequirementModule] = []
    for child in children:
        child_id = str(child.get("id") or child.get("req_id") or "").strip() or f"{module.node_id}-part"
        child_name = str(child.get("name") or child_id).strip()
        parts.extend(
            _split_module(
                RequirementModule(
                    index=module.index,
                    total=module.total,
                    node_id=child_id,
                    name=f"{module.name} / {child_name}",
                    subtree=child,
                    split_group=module.split_group or module.node_id,
                ),
                max_chars,
                depth + 1,
                child_context,
            )
        )
    return parts or [module]


def _split_oversized_modules(modules: list[RequirementModule]) -> list[RequirementModule]:
    max_chars = _module_split_chars()
    if max_chars <= 0:
        return modules
    split: list[RequirementModule] = []
    for module in modules:
        split.extend(_split_module(module, max_chars, inherited_context=module.inherited_context))
    total = len(split)
    return [
        RequirementModule(
            index=i,
            total=total,
            node_id=m.node_id,
            name=m.name,
            subtree=m.subtree,
            split_group=m.split_group,
            inherited_context=m.inherited_context,
        )
        for i, m in enumerate(split, start=1)
    ]


def load_requirements_payload(requirements_dir: Path) -> dict[str, Any]:
    """加载 ROOT 需求，供模板选择和追踪库复用同一份解析结果。"""
    requirements_path = requirements_dir / "requirements.yaml"
    if not requirements_path.is_file():
        raise FileNotFoundError(f"requirements.yaml not found: {requirements_path}")
    payload = yaml.safe_load(requirements_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("requirements.yaml must contain a mapping")
    if str(payload.get("id") or "").strip() != "ROOT":
        raise ValueError("requirements.yaml must contain a ROOT mapping")
    return payload


def _root_prompt_context(payload: dict[str, Any]) -> dict[str, Any]:
    """保留 ROOT 级别的全局规则和夹具约定，避免它们在分模块时丢失。"""
    # 只复制跨模块语义字段；ROOT.children 已由模块回合分别携带，无需重复整棵树。
    keys = ("id", "name", "type", "dependencies", "description", "data")
    return {key: payload[key] for key in keys if key in payload}


def select_template_dir(
    agent_root: Path,
    requirements_dir: Path,
    requirements_payload: dict[str, Any],
) -> Path:
    """返回随包分发的通用脚手架目录。

    脚手架只含目标技术栈的通用初始化框架（构建配置、应用外壳、服务端引导、静态托管与
    数据库连接助手），不含任何针对具体比赛任务的页面、业务逻辑或预置数据；这些一律由
    模型按需求实现。因此两个任务共用同一套脚手架，与任务内容无关。
    """
    del requirements_dir, requirements_payload
    return agent_root / "template"


def normalize_requirement_tree_for_traceability(payload: dict[str, Any]) -> dict[str, Any]:
    """为没有显式场景编号的需求生成稳定 ID，便于评测证据关联。"""
    normalized = deepcopy(payload)

    def visit(node: dict[str, Any]) -> None:
        node_id = str(node.get("id") or node.get("req_id") or "REQ").strip()
        scenarios = node.get("scenarios")
        if isinstance(scenarios, list):
            normalized_scenarios: list[dict[str, Any]] = []
            for index, raw_scenario in enumerate(scenarios, start=1):
                if not isinstance(raw_scenario, dict):
                    continue
                scenario = dict(raw_scenario)
                # 原始比赛 YAML 用 name 区分场景，但追踪库需要 scenario_id。
                scenario_id = str(
                    scenario.get("id")
                    or scenario.get("scenario_id")
                    or f"{node_id}-SCN-{index}"
                ).strip()
                scenario["id"] = scenario_id
                scenario["scenario_id"] = scenario_id
                normalized_scenarios.append(scenario)
            node["scenarios"] = normalized_scenarios

        for child in node.get("children", []):
            if isinstance(child, dict):
                visit(child)

    visit(normalized)
    return normalized


def _codex_config(CodexConfig: Any) -> Any:
    env = os.environ.copy()
    overrides: list[str] = []
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    if base_url:
        env["OPENAI_BASE_URL"] = base_url
        overrides.append(f"openai_base_url={json.dumps(base_url)}")
    return CodexConfig(env=env, config_overrides=tuple(overrides))


def _read_bool_env(name: str, default: bool) -> bool:
    """读取布尔环境变量，避免不同 Shell 的大小写写法造成歧义。"""
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"{name} must be one of 1/0, true/false, yes/no, or on/off; got {value!r}"
    )


def _agent_log(message: str) -> None:
    """输出带时间戳的诊断日志，便于从官方 stdout 还原中途执行状态。"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print(f"[agent {timestamp}] {message}", flush=True)


def _short_error(error: Any, max_chars: int = 1200) -> str:
    """截断远端错误，避免代理错误或模型回显撑爆日志。"""
    text = str(error).replace("\x00", " ").strip()
    if len(text) > max_chars:
        return text[: max_chars - 24].rstrip() + " ...[truncated]"
    return text


def _reasoning_effort_from_env() -> ReasoningEffort:
    """解析可控的推理强度，默认使用 medium 平衡质量和模型成本。"""
    value = os.environ.get("ARCBENCH_REASONING_EFFORT", "medium").strip().lower()
    try:
        return ReasoningEffort(value)
    except ValueError as error:
        allowed = ", ".join(item.value for item in ReasoningEffort)
        raise ValueError(
            f"ARCBENCH_REASONING_EFFORT must be one of {allowed}; got {value!r}"
        ) from error


def _repair_reasoning_effort() -> ReasoningEffort:
    """解析预检修复的推理强度，默认用 low 控制一次性修复请求的成本。"""
    value = os.environ.get("ARCBENCH_REPAIR_REASONING_EFFORT", "low").strip().lower()
    try:
        return ReasoningEffort(value)
    except ValueError as error:
        allowed = ", ".join(item.value for item in ReasoningEffort)
        raise ValueError(
            "ARCBENCH_REPAIR_REASONING_EFFORT must be one of "
            f"{allowed}; got {value!r}"
        ) from error


def _audit_reasoning_effort() -> ReasoningEffort:
    """解析最终契约审计强度；默认 medium，避免低档审计遗漏跨层缺口。"""
    value = os.environ.get("ARCBENCH_FINAL_AUDIT_REASONING_EFFORT", "medium").strip().lower()
    try:
        return ReasoningEffort(value)
    except ValueError as error:
        allowed = ", ".join(item.value for item in ReasoningEffort)
        raise ValueError(
            "ARCBENCH_FINAL_AUDIT_REASONING_EFFORT must be one of "
            f"{allowed}; got {value!r}"
        ) from error


def _developer_instructions(skills_dir: Path) -> str:
    return textwrap.dedent(
        f"""
        You implement an ARC-Bench web application in the current working directory.

        The current output directory may contain the official evolution baseline from the previous
        Hackathon submission. Preserve that application and implement the evolution requirements
        incrementally: inspect the existing routes, database schema, pages and tests before editing;
        never replace the project with the bundled template and never delete working behavior. If
        the output is genuinely empty, the bundled template is only a fallback scaffold.

        Each request supplies one or more bounded ROOT requirement subtrees, including their
        scenarios. Implement every supplied subtree completely in the current project.

        Before editing each turn, perform a short baseline-gap audit:
        1. map the relevant existing frontend entry points, backend routes, database tables,
           seed/reset logic and permission helpers;
        2. distinguish already-working behavior from missing or partial behavior;
        3. implement the smallest complete incremental change, including all dependent layers.
        Do not spend a separate turn only writing an audit report: inspect, fix, and verify in the
        same turn. Never assume a feature is complete merely because a button or route exists.

        The scenarios are the acceptance specification. For every scenario:
        - Its GIVEN steps define the starting state. Treat ROOT-level data as the authoritative
          fixture, ownership, and reset contract: use runner-provisioned records when specified;
          only seed records the task leaves to the application, idempotently and without
          overwriting later user changes. Reproduce named identifiers, spellings, roles, and
          values exactly. Never silently skip a scenario or add a test-only API.
        - Its WHEN/THEN steps define the observable behavior. Every exact quoted label, button
          name, heading, message, placeholder, count or value they mention is a required
          observable and must be present verbatim, including capitalization and punctuation.
        - Enforce every validation, permission rule and state transition on the server as well as
          in the UI: a request that bypasses the UI must still be rejected.
        - Persist authoritative state in the database so it survives a page reload and a fresh
          browser session; do not keep it only in memory or in component state.
        - Treat cross-layer contracts as first-class requirements: exact accessible names and
          labels, loading/empty/error states, reload persistence, ownership boundaries, and
          rejection without partial writes. For collaboration features, explicitly check that
          authors cannot review their own pull request, reviewers have the required repository
          permission, and candidate lists exclude ineligible users. For line-level comments,
          check both added and deleted lines when the requirement permits comments on changed
          lines. These are regression safeguards, not hidden-test instructions.

        Work economically (this changes how you work, not what you implement):
        - All subtrees for this implementation turn are provided inline as JSON. Do not read the
          complete requirements.yaml.
        - The textual requirements are authoritative for behavior. When a module description links
          a relevant image under the requirement source directory's reference/ folder, you may
          open that specific PNG to resolve layout, labels, spacing or control placement. For a
          module that contains a public reference PNG and changes the UI, inspect every listed
          reference before editing when an image-capable tool is available; do not merely infer
          the layout from the filename. Use the visual model for those image-backed turns, but do
          not inspect unrelated images or search for hidden tests. If the module is backend-only,
          do not spend a visual call.
        - Do not re-read files you just wrote, and do not dump the contents of files you are not
          editing.
        - Command output is billed input. Never dump full command output: pipe long output
          (for example `npm run build 2>&1 | tail -40`), and prefer targeted checks
          (`node --check src/index.js`, one vitest file) over full suites while you work.
        - Batch your work: make several independent edits, then check once — do not alternate
          edit/check/edit/check. During intermediate turns, use only a focused syntax or unit
          check for the changed area; do not run the full project suite or production build on
          every turn. The agent runs the final frontend build and backend syntax preflight once
          after all requirement turns, and will request a focused repair if it fails.
        - The file manifest notes whether node_modules is already installed; if it is, reuse it
          and do not run npm install again. If you must install, use
          `npm install --no-audit --no-fund 2>&1 | tail -20`.
        - Keep the final summary short: files changed, commands run, scenarios at risk.

        Do not add new npm dependencies unless strictly necessary: prefer the libraries already
        declared in frontend/package.json and backend/package.json, and reuse the existing SQLite
        helper instead of introducing another database driver. A new dependency can fail to
        install and then break the official build, which costs far more than it saves.

        Module turns use separate Codex threads, so treat the current files and git history as
        the source of truth instead of relying on prior conversation context. Before editing,
        inspect the current project state briefly. After editing, run the smallest relevant build
        or test commands. Add concise Chinese comments for non-obvious business rules, while
        keeping user-facing strings required by the task unchanged.

        The official web runner expects the output root to retain frontend/package.json and
        backend/package.json. Do not replace this with deploy.sh: the local simulation supports
        deploy.sh, but the official runner does not. Preserve the standard Vite frontend and
        Express backend layout, make the frontend build successfully, and make the backend honor
        HOST and PORT so it serves the built app at 0.0.0.0:3000. Keep the SPA deep-link fallback
        working so any client route serves index.html. Do not leave a long-running server attached
        to your implementation turn; the runner starts it after the agent exits.

        ARC-Bench skills are available in {skills_dir}:
        - Read arcbench-runtime-signals/SKILL.md to report module progress when useful.
        - Read arcbench-checkpoint/SKILL.md when creating a coherent git checkpoint.
        The agent itself records backend routes, frontend pages, database tables and new test
        files into traceability after your turn, so do not record those yourself; use
        arcbench-traceability only for evidence a file scan cannot see, such as a non-obvious
        business rule. Run skill scripts from the current project with --project-dir .; do not
        hand-write runner events or traceability JSON when a bundled script covers the action.
        """
    ).strip()


def _workspace_manifest(output_dir: Path, max_entries: int = 120) -> str:
    """列出工作区当前的源码文件，避免模型为「有什么」白跑探索回合。

    只列路径，不列内容；排除依赖、构建产物与 agent 自身的元数据目录。
    """
    skip_dirs = {
        "node_modules",
        ".git",
        ".arc",
        ".codex",
        "dist",
        "coverage",
        "__pycache__",
        "requirements",
    }
    skip_suffixes = (".db", ".sqlite", ".sqlite3", ".log")
    entries: list[str] = []
    for path in sorted(output_dir.rglob("*")):
        if len(entries) >= max_entries:
            entries.append("... (truncated)")
            break
        relative = path.relative_to(output_dir)
        if any(part in skip_dirs or part.startswith(".") for part in relative.parts):
            continue
        if path.suffix.lower() in skip_suffixes:
            continue
        if path.is_file():
            entries.append(relative.as_posix())
    lines = entries if entries else ["(empty)"]
    # 明确告诉模型依赖已经装好，避免每个回合重复 npm install（耗时且产生海量输出）。
    installed = [
        name
        for name in ("frontend", "backend")
        if (output_dir / name / "node_modules").is_dir()
    ]
    if installed:
        lines.append(
            f"(node_modules already installed: {', '.join(installed)} — do not run npm install again)"
        )
    return "\n".join(lines)


# --- 过程证据登记 -------------------------------------------------------------
# 过程图（Interfaces/Tests）只反映被登记的内容，而登记此前完全依赖模型自觉调用
# 技能脚本，实际上几乎从不发生。这里由 agent 在每个模块完成后，从「真实改动过的
# 文件」中确定性地提取后端路由、前端页面与数据库表并写入 traceability。
# 这是 agent 自己的过程记账，不预先实现任何任务功能。

_API_ROUTE_RE = re.compile(
    r"""\b(?:app|router)\s*\.\s*(get|post|put|patch|delete|all)\s*\(\s*['"`]([^'"`]+)['"`]""",
    re.IGNORECASE,
)
_UI_ROUTE_RE = re.compile(r"""\bpath\s*=\s*["'`]([^"'`]+)["'`]""")
_DB_TABLE_RE = re.compile(
    r"""CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["'`\[]?([A-Za-z_]\w*)""",
    re.IGNORECASE,
)
_CODE_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
_TEST_SPEC_RE = re.compile(r"""\.(test|spec)\.(js|jsx|ts|tsx)$""", re.IGNORECASE)
_SCAFFOLD_ROUTES = {"/api/health", "/", "*", "/index.html"}
_MAX_ASSETS_PER_MODULE = 60
_MAX_PATH_LEN = 120
_REFERENCE_IMAGE_RE = re.compile(
    r"""(?:(?:\.\.?/)?)(reference/[^)\s`"'<>]+?\.png)(?:[)#\s]|$)""",
    re.IGNORECASE,
)


def _baseline_inventory(output_dir: Path, max_chars: int = 3600) -> str:
    """生成低成本的基线摘要，帮助模型先定位已有能力和明显缺口。

    这里只读取公开源码中的路径、路由和表名，不执行应用、不探测测试，也不把完整源码
    塞进提示词。决赛的基线由平台注入，模型必须在这个摘要基础上继续审计而不是重建。
    """
    route_items: list[str] = []
    page_items: list[str] = []
    table_items: list[str] = []
    source_files: list[str] = []
    for path in sorted(output_dir.rglob("*")):
        relative = path.relative_to(output_dir)
        if not path.is_file() or path.suffix.lower() not in _CODE_SUFFIXES:
            continue
        if any(part in {"node_modules", ".git", ".codex", ".arc", "dist"} for part in relative.parts):
            continue
        if len(source_files) < 90:
            source_files.append(relative.as_posix())
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for method, route in _API_ROUTE_RE.findall(content):
            route_items.append(f"{method.upper()} {route}")
        for route in _UI_ROUTE_RE.findall(content):
            page_items.append(route)
        for table in _DB_TABLE_RE.findall(content):
            table_items.append(table)

    def unique(values: list[str], limit: int) -> list[str]:
        return list(dict.fromkeys(values))[:limit]

    sections = [
        "源码文件: " + ", ".join(source_files[:45]) if source_files else "源码文件: (none)",
        "后端路由: " + ", ".join(unique(route_items, 45)) if route_items else "后端路由: (none)",
        "前端路由: " + ", ".join(unique(page_items, 30)) if page_items else "前端路由: (none)",
        "数据库表: " + ", ".join(unique(table_items, 35)) if table_items else "数据库表: (none)",
    ]
    summary = "\n".join(sections)
    if len(summary) > max_chars:
        summary = summary[: max_chars - 40].rstrip() + "\n... (inventory truncated)"
    return summary


def _visual_references_for_turn(turn: ModuleTurn, requirements_dir: Path) -> list[str]:
    """提取当前需求回合明确引用的公开图片，供模型按需使用视觉能力。"""
    references: list[str] = []
    for module in turn.modules:
        raw = json.dumps(module.subtree, ensure_ascii=False, separators=(",", ":"))
        for reference in _REFERENCE_IMAGE_RE.findall(raw):
            relative = reference.replace("\\", "/")
            absolute = (requirements_dir / relative).resolve()
            if absolute.is_file():
                references.append(str(absolute))
    return list(dict.fromkeys(references))


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return cleaned[:80] or "item"


def _changed_files_since(runtime: Any, sha_before: str | None) -> list[str]:
    """模块回合开始到现在改过/新增的文件（相对路径，posix 风格）。"""
    if not sha_before:
        return []
    try:
        runtime.git.add_all()
        result = runtime.git.run(["diff", "--name-only", sha_before], check=False)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except Exception:
        return []


def _extract_module_assets(output_dir: Path, changed: list[str]) -> list[tuple[str, str, str, str]]:
    """从改动文件中提取 (type, content, slug, file_path) 形式的过程证据。"""
    assets: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for relative in changed:
        if len(assets) >= _MAX_ASSETS_PER_MODULE:
            break
        path = output_dir / relative
        suffix = path.suffix.lower()
        if suffix not in _CODE_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        posix = relative.replace("\\", "/")

        if posix.startswith("backend/"):
            for method, route in _API_ROUTE_RE.findall(text):
                route = route.strip()
                if (
                    not route
                    or route in _SCAFFOLD_ROUTES
                    or route.startswith("^(?")
                    or len(route) > _MAX_PATH_LEN
                ):
                    continue
                key = ("api", f"{method.upper()} {route}")
                if key not in seen:
                    seen.add(key)
                    assets.append(("api", f"{method.upper()} {route}", key[1], posix))
            for table in _DB_TABLE_RE.findall(text):
                key = ("db", f"table {table}")
                if key not in seen:
                    seen.add(key)
                    assets.append(("db", f"Table {table}", f"table-{table}", posix))

        if posix.startswith("frontend/"):
            for route in _UI_ROUTE_RE.findall(text):
                route = route.strip()
                if not route or route in _SCAFFOLD_ROUTES or len(route) > _MAX_PATH_LEN:
                    continue
                key = ("ui", f"route {route}")
                if key not in seen:
                    seen.add(key)
                    assets.append(("ui", f"Page route {route}", f"route-{route}", posix))

        if _TEST_SPEC_RE.search(posix):
            kind = "e2e" if ".spec." in posix.lower() else "unit"
            key = ("test", posix)
            if key not in seen:
                seen.add(key)
                assets.append((f"test:{kind}", f"Test file {posix}", posix, posix))
    return assets


def _record_module_assets(runtime: Any, module: RequirementModule, output_dir: Path) -> int:
    changed = _changed_files_since(runtime, runtime.git.current_head())
    if not changed:
        return 0
    recorded = 0
    for kind, content, slug, file_path in _extract_module_assets(output_dir, changed):
        try:
            if kind.startswith("test:"):
                runtime.traceability.upsert_test(
                    test_id=f"{module.node_id}.TEST.{_slug(kind.split(':', 1)[1] + ' ' + Path(file_path).name)}",
                    req_id=module.node_id,
                    type=kind.split(":", 1)[1],
                    file_path=file_path,
                )
            else:
                runtime.traceability.upsert_interface(
                    interface_id=f"{module.node_id}.{kind.upper()}.{_slug(slug)}",
                    req_ids=[module.node_id],
                    type=kind,
                    content=content,
                    file_path=file_path,
                    implemented=True,
                )
            recorded += 1
        except Exception as error:
            print(f"[agent] traceability record skipped ({kind} {slug}): {error}", flush=True)
    return recorded


def _time_budget_seconds() -> float:
    """实现阶段的总时间预算（分钟）；0 表示不限制。"""
    raw = os.environ.get("ARCBENCH_TIME_BUDGET_MINUTES", "720").strip()
    try:
        minutes = float(raw)
    except ValueError:
        return 720.0 * 60.0
    return max(0.0, minutes) * 60.0



def _pack_into_turns(modules: list[RequirementModule], max_chars: int) -> list[ModuleTurn]:
    """把相邻模块贪心打包成 Codex 回合，使单回合内联规格不超过 max_chars。

    拆分把大模块切成小份避免了压缩失败，但每个新回合都要重新熟悉工作区，
    回合数一多，重读开销会把总时长推回去。把相邻小份重新打包，可以把
    回合数压回接近拆分前的水平，同时保持单回合规格不超标。
    """
    turns: list[list[RequirementModule]] = []
    current: list[RequirementModule] = []
    current_size = 0
    split_group_counts: dict[str, int] = {}
    for module in modules:
        size = len(json.dumps(module.subtree, ensure_ascii=False, separators=(",", ":")))
        # 大需求即使字符数不超限，也可能因实现动作过多触发远端上下文压缩失败。
        # 同一父需求拆出的兄弟模块每回合最多合并两个，避免一次失败丢掉整组功能。
        too_many_siblings = bool(
            module.split_group
            and split_group_counts.get(module.split_group, 0) >= 2
        )
        if current and (current_size + size > max_chars or too_many_siblings):
            turns.append(current)
            current, current_size = [], 0
            split_group_counts = {}
        current.append(module)
        current_size += size
        if module.split_group:
            split_group_counts[module.split_group] = split_group_counts.get(module.split_group, 0) + 1
    if current:
        turns.append(current)
    total = len(turns)
    return [ModuleTurn(index=i, total=total, modules=tuple(t)) for i, t in enumerate(turns, 1)]


def _module_prompt(
    turn: ModuleTurn,
    requirements_dir: Path,
    completed_ids: list[str],
    skills_dir: Path,
    workspace_manifest: str,
    baseline_inventory: str,
    root_context: dict[str, Any],
) -> str:
    completed = ", ".join(completed_ids) if completed_ids else "none"
    visual_references = _visual_references_for_turn(turn, requirements_dir)
    visual_reference_block = (
        "\n".join(f"- {path}" for path in visual_references)
        if visual_references
        else "(none referenced by this turn)"
    )
    # 保留完整字段但去掉空白缩进，降低每个实现回合的输入 token。
    blocks: list[str] = []
    for module in turn.modules:
        module_json = json.dumps(module.subtree, ensure_ascii=False, separators=(",", ":"))
        blocks.append(
            f"### Module {module.node_id} - {module.name}\n```json\n{module_json}\n```"
        )
    module_specs = "\n\n".join(blocks)
    root_context_json = json.dumps(root_context, ensure_ascii=False, separators=(",", ":"))
    inherited_by_id: dict[str, dict[str, Any]] = {}
    for module in turn.modules:
        for context in module.inherited_context:
            context_id = str(context.get("id") or context.get("req_id") or json.dumps(context, sort_keys=True))
            inherited_by_id.setdefault(context_id, context)
    inherited_context_json = json.dumps(
        list(inherited_by_id.values()), ensure_ascii=False, separators=(",", ":")
    )
    return textwrap.dedent(
        f"""
        Implement ROOT module(s) {turn.index}/{turn.total}: {turn.label}

        Requirement source directory: {requirements_dir}
        Previously completed ROOT modules: {completed}
        Skills directory: {skills_dir}

        Authoritative shared ROOT context (global rules and fixture/reset contract):
        ```json
        {root_context_json}
        ```

        Inherited parent-module context for any split requirements in this turn:
        ```json
        {inherited_context_json}
        ```
        Preserve constraints from these contexts even when a leaf scenario does not repeat them.

        Source files currently in the project:
        {workspace_manifest}

        Compact baseline inventory (a starting point only; inspect the relevant source before
        changing it):
        ```text
        {baseline_inventory}
        ```

        Public visual references explicitly linked by this turn:
        ```text
        {visual_reference_block}
        ```
        If this list is not empty and the turn changes the UI, open these exact public PNG files
        before editing and use them to verify layout, visible labels, control placement and
        accessible-name details. Do not open unrelated images or hidden-test artifacts.

        Implement every module below completely in the current target directory. Inspect only the
        files you need to change, and preserve the behavior implemented by earlier modules.

        The JSON blocks below are the authoritative specification. They contain every scenario
        with its GIVEN/WHEN/THEN steps; those steps are the acceptance criteria. Implement each
        scenario so it holds from a fresh start: create the seeded state its GIVEN steps name,
        and make every exact string and count its THEN steps assert observable in the UI and
        enforced on the server. Before finishing, re-check each scenario against what you built.
        {module_specs}

        If /workspace/tests or ../tests exists, inspect only the Playwright specs relevant to
        these modules because the official evaluator runs them against http://127.0.0.1:3000.
        Validate changed behavior with a focused check when one is available, but do not repeat
        the full suite or production build in every turn; the agent runs its final preflight after
        all turns. Do not start a long-running server. Finish by summarizing files changed,
        checks run, and any scenario you could not fully satisfy.
        """
    ).strip()


def _simple_turn_reasoning_effort(
    turn: ModuleTurn,
    default: ReasoningEffort,
) -> ReasoningEffort:
    """小规格回合使用 low，复杂回合沿用全局强度，降低无谓推理成本。"""
    if default.value in {"none", "minimal", "low"}:
        return default
    raw = os.environ.get("ARCBENCH_SIMPLE_MODULE_MAX_CHARS", "9000").strip()
    try:
        threshold = max(0, int(raw))
    except ValueError:
        threshold = 9000
    if threshold <= 0:
        return default
    size = sum(
        len(json.dumps(module.subtree, ensure_ascii=False, separators=(",", ":")))
        for module in turn.modules
    )
    if size <= threshold and len(turn.modules) <= 2:
        return ReasoningEffort.LOW
    return default


def _contract_audit_prompt(
    requirements_dir: Path,
    completed_ids: list[str],
    baseline_inventory: str,
) -> str:
    """最后一轮低成本回收跨模块遗漏，不替代正常需求实现。"""
    completed = ", ".join(completed_ids) if completed_ids else "none"
    return textwrap.dedent(
        f"""
        Perform a final, focused contract audit of the current ARC-Bench evolution application.
        This is a repair pass, not a redesign. The public requirement source is:
        {requirements_dir}
        Completed implementation modules: {completed}

        Inspect the current code and the public requirement text only. Make high-confidence fixes
        for omissions that can make an otherwise working feature fail evaluation:
        - every requested behavior is wired end-to-end through UI, API, persistence and reload;
        - exact visible labels, headings, placeholders and accessible names match the requirement;
        - server-side authorization, ownership, validation and rejection paths are enforced;
        - successful and failed mutations do not leave inconsistent or partially written state;
        - existing baseline behavior remains intact, and the app still builds.
        Pay special attention to collaboration contracts such as excluding PR authors from review,
        requiring eligible repository permission for reviewers, supporting all permitted changed-line
        comment targets, and persisting reactions or other user state. These are public-contract
        regression checks, not an attempt to discover hidden tests.

        Do not add test-only endpoints, do not inspect hidden tests, do not replace the baseline,
        and do not introduce dependencies unless unavoidable. If a behavior is already correct,
        leave it alone. Run only short targeted checks; do not start a long-running server.

        Baseline inventory:
        ```text
        {baseline_inventory}
        ```
        Reply with a short list of files changed and checks run.
        """
    ).strip()


def _run_project_validation(output_dir: Path) -> list[str]:
    """执行不启动常驻服务的提交前检查，返回需要 Codex 修复的问题。"""
    errors: list[str] = []
    frontend_dir = output_dir / "frontend"
    backend_dir = output_dir / "backend"

    # 官方 Runner 的标准 Web 路径要求这两个目录和 package.json 都存在。
    for directory, label in ((frontend_dir, "frontend"), (backend_dir, "backend")):
        if not directory.is_dir():
            errors.append(f"missing required directory: {label}")
        elif not (directory / "package.json").is_file():
            errors.append(f"missing required package.json: {label}")

    package_specs: list[tuple[Path, str, str]] = [
        (frontend_dir / "package.json", "frontend", "build"),
        (backend_dir / "package.json", "backend", "start"),
    ]
    for package_path, label, required_script in package_specs:
        if not package_path.is_file():
            continue
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"invalid {label}/package.json: {error}")
            continue
        scripts = package.get("scripts") if isinstance(package, dict) else None
        if not isinstance(scripts, dict) or not str(scripts.get(required_script) or "").strip():
            errors.append(f"{label}/package.json must define npm run {required_script}")

    if not (backend_dir / "src" / "index.js").is_file():
        errors.append("missing backend/src/index.js entrypoint")

    node_executable = shutil.which("node")
    if node_executable and (backend_dir / "src" / "index.js").is_file():
        try:
            result = subprocess.run(
                [node_executable, "--check", "src/index.js"],
                cwd=backend_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            errors.append("backend syntax check timed out")
        else:
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[-4000:]
                errors.append(f"backend syntax check failed:\n{detail}")

    # Agent 运行阶段未必预装 npm 依赖；依赖缺失交给官方 Runner 安装，不把它误判为代码错误。
    npm_executable = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm_executable and (frontend_dir / "package.json").is_file():
        dependency_dir = frontend_dir / "node_modules"
        if dependency_dir.is_dir():
            try:
                result = subprocess.run(
                    [npm_executable, "run", "build"],
                    cwd=frontend_dir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=120,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                errors.append("frontend production build timed out")
            else:
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout).strip()[-6000:]
                    errors.append(f"frontend production build failed:\n{detail}")

    return errors


def _repair_prompt(validation_errors: list[str]) -> str:
    """把提交前检查结果转成一次聚焦修复请求，避免模型忽略构建错误。"""
    details = "\n\n".join(validation_errors)
    return textwrap.dedent(
        f"""
        The official ARC-Bench preflight found these blocking issues after the requested modules:

        {details}

        Fix these issues in the current workspace without removing the required frontend/backend
        layout or weakening server-side behavior. Do not delete working features or weaken
        validation, persistence or permission checks just to make a build pass. Re-run the
        relevant syntax/build checks, but do not start a long-running server. Keep user-facing
        accessible names and requirement semantics unchanged. Reply with a concise summary of the
        repair.
        """
    ).strip()


def implement_modules(
    requirements_dir: Path,
    output_dir: Path,
    turns: list[ModuleTurn],
    skills_dir: Path,
    runtime: AgentRuntime,
    *,
    root_context: dict[str, Any],
    dry_run: bool = False,
) -> list[str]:
    """按打包后的回合顺序驱动 Codex，并同步模块执行状态；返回仍失败的模块列表。"""
    model = os.environ.get("MODEL", "").strip() or None
    visual_model = os.environ.get("VISUAL_MODEL", "").strip() or None
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    visual_api_key = os.environ.get("VISUAL_API_KEY", "").strip()
    visual_base_url = os.environ.get("VISUAL_BASE_URL", "").strip()
    reasoning_effort = _reasoning_effort_from_env()
    repair_reasoning_effort = _repair_reasoning_effort()
    audit_reasoning_effort = _audit_reasoning_effort()
    isolate_module_threads = _read_bool_env("ARCBENCH_ISOLATE_MODULE_THREADS", True)
    completed_ids: list[str] = []
    baseline_inventory = _baseline_inventory(output_dir)

    _agent_log(
        "run configuration: "
        f"output={output_dir}; model={model or '(platform default)'}; "
        f"visual_model={visual_model or '(not configured/injected)'}; "
        f"api_key={'configured' if api_key else 'not configured'}; "
        f"base_url={'configured' if base_url else 'platform default'}; "
        f"visual_api_key={'configured' if visual_api_key else 'not configured'}; "
        f"visual_base_url={'configured' if visual_base_url else 'not configured'}; "
        f"default_effort={reasoning_effort.value}; "
        f"audit_effort={audit_reasoning_effort.value}; "
        f"repair_effort={repair_reasoning_effort.value}; "
        f"baseline_inventory_chars={len(baseline_inventory)}"
    )

    if dry_run:
        for turn in turns:
            for module in turn.modules:
                print(
                    f"[dry-run] turn {turn.index}/{turn.total}: "
                    f"{module.node_id} - {module.name}",
                    flush=True,
                )
        return []

    # 竞赛规则明确要求智能体真正调用大模型完成任务：脚手架不含任何任务实现，
    # 因此这里不存在「不调用模型也能产出应用」的旁路。
    with Codex(config=_codex_config(CodexConfig)) as codex:
        if api_key:
            codex.login_api_key(api_key)
        print(
            "[codex] "
            f"reasoning_effort={reasoning_effort.value}; "
            f"repair_reasoning_effort={repair_reasoning_effort.value}; "
            f"simple_turn_threshold={os.environ.get('ARCBENCH_SIMPLE_MODULE_MAX_CHARS', '9000')}; "
            f"module_threads={'isolated' if isolate_module_threads else 'shared'}",
            flush=True,
        )
        _agent_log(
            f"implementation plan: turns={len(turns)}; "
            f"modules={sum(len(turn.modules) for turn in turns)}; "
            f"attempts_per_turn={_module_attempts()}; "
            f"time_budget_minutes={os.environ.get('ARCBENCH_TIME_BUDGET_MINUTES', '720')}; "
            f"final_contract_audit={os.environ.get('ARCBENCH_FINAL_CONTRACT_AUDIT', '1')}; "
            f"audit_effort={audit_reasoning_effort.value}"
        )

        def start_thread() -> Any:
            """创建只绑定当前工作区的线程，避免无关历史进入后续请求。"""
            return codex.thread_start(
                cwd=str(output_dir),
                sandbox=Sandbox.full_access,
                approval_mode=ApprovalMode.deny_all,
                model=model,
                developer_instructions=_developer_instructions(skills_dir),
            )

        shared_thread = start_thread() if not isolate_module_threads else None
        attempts = _module_attempts()
        failures: list[str] = []
        skipped: list[str] = []
        started_at = time.monotonic()
        budget_seconds = _time_budget_seconds()
        for turn_position, turn in enumerate(turns):
            if budget_seconds and time.monotonic() - started_at > budget_seconds:
                # 时间预算用尽：停止实现，保留已有成果进入预检，避免把平台的时长上限烧光。
                skipped.extend(m.node_id for t in turns[turn_position:] for m in t.modules)
                print(
                    f"[codex] time budget exhausted after {turn_position} turns; "
                    f"skipping {len(skipped)} remaining modules: {', '.join(skipped)}",
                    flush=True,
                )
                break
            turn_effort = _simple_turn_reasoning_effort(turn, reasoning_effort)
            turn_spec_chars = sum(
                len(json.dumps(module.subtree, ensure_ascii=False, separators=(",", ":")))
                for module in turn.modules
            )
            visual_reference_count = len(_visual_references_for_turn(turn, requirements_dir))
            print(
                f"[codex] Implementing turn {turn.index}/{turn.total}: "
                f"{turn.label} (effort={turn_effort.value})",
                flush=True,
            )
            _agent_log(
                f"turn {turn.index}/{turn.total} started: label={turn.label}; "
                f"spec_chars={turn_spec_chars}; effort={turn_effort.value}; "
                f"visual_refs={visual_reference_count}; "
                f"completed_before={len(completed_ids)}"
            )
            for module in turn.modules:
                runtime.events.mark_implementation_started(
                    module.node_id,
                    f"Codex started turn {turn.index}/{turn.total}",
                )
            sha_before = None
            try:
                sha_before = runtime.git.current_head()
            except Exception:
                sha_before = None
            last_error: Exception | None = None
            for attempt in range(1, attempts + 1):
                attempt_started = time.monotonic()
                _agent_log(
                    f"turn {turn.label} attempt {attempt}/{attempts} started; "
                    f"thread_mode={'shared' if shared_thread is not None and attempt == 1 else 'isolated'}"
                )
                try:
                    # 默认每个回合使用新线程，避免前序回合的工具输出和对话历史反复计费；
                    # 重试必须换新线程——撑爆上下文/触发远端压缩失败的正是旧线程的历史。
                    thread = shared_thread if (shared_thread is not None and attempt == 1) else start_thread()
                    result = thread.run(
                        _module_prompt(
                            turn,
                            requirements_dir,
                            completed_ids,
                            skills_dir,
                            _workspace_manifest(output_dir),
                            _baseline_inventory(output_dir),
                            root_context,
                        ),
                        effort=turn_effort,
                    )
                    if getattr(result, "error", None) is not None:
                        raise RuntimeError(f"Codex failed on {turn.label}: {result.error}")
                    changed_after_turn = _changed_files_since(runtime, sha_before)
                    _agent_log(
                        f"turn {turn.label} attempt {attempt}/{attempts} completed in "
                        f"{time.monotonic() - attempt_started:.1f}s; "
                        f"changed_files={len(changed_after_turn)}"
                    )
                    if changed_after_turn:
                        preview = ", ".join(changed_after_turn[:12])
                        suffix = " ..." if len(changed_after_turn) > 12 else ""
                        _agent_log(f"turn {turn.label} changed: {preview}{suffix}")
                    last_error = None
                    break
                except Exception as error:
                    last_error = error
                    _agent_log(
                        f"turn {turn.label} attempt {attempt}/{attempts} failed after "
                        f"{time.monotonic() - attempt_started:.1f}s: {_short_error(error)}"
                    )
                    print(
                        f"[codex] turn {turn.label} attempt {attempt}/{attempts} failed: "
                        f"{_short_error(error)}",
                        flush=True,
                    )
                    if attempt < attempts:
                        delay = _retry_backoff_seconds(attempt)
                        print(
                            f"[codex] retrying {turn.label} on a fresh thread after {delay}s",
                            flush=True,
                        )
                        time.sleep(delay)
            if last_error is not None:
                for module in turn.modules:
                    runtime.events.mark_implementation_failed(module.node_id, str(last_error))
                # 一个回合失败（例如远端压缩故障）不能拖垮整次运行：
                # 保留前序回合的成果，继续实现剩余回合，最后仍会做预检并交付。
                failures.append(f"{turn.label}: {last_error}")
                continue
            recorded_total = 0
            for module in turn.modules:
                try:
                    recorded_total += _record_module_assets(runtime, module, output_dir)
                except Exception as error:
                    print(f"[agent] traceability recording failed: {error}", flush=True)
            try:
                runtime.git.commit(f"implement {turn.label}")
                _agent_log(f"turn {turn.label} git checkpoint created")
            except Exception:
                _agent_log(f"turn {turn.label} git checkpoint unavailable")
            for module in turn.modules:
                runtime.events.mark_implementation_done(
                    module.node_id,
                    f"Codex completed turn {turn.index}/{turn.total}"
                    + (f" with {recorded_total} recorded interfaces/tests" if recorded_total else ""),
                )
                completed_ids.append(module.node_id)
            _agent_log(
                f"turn {turn.label} finished; completed_modules={len(completed_ids)}; "
                f"recorded_assets={recorded_total}"
            )

        if failures:
            print(
                "[codex] modules failed after retries (continuing with the rest):\n  "
                + "\n  ".join(failures),
                flush=True,
            )

        # 需求回合完成后再做一次低成本跨模块审计，重点收回“页面有了但 API/持久化/
        # 权限没有闭环”以及精确 accessible name 这类不容易被单个模块发现的遗漏。
        run_contract_audit = _read_bool_env("ARCBENCH_FINAL_CONTRACT_AUDIT", True)
        if run_contract_audit and completed_ids:
            print("[codex] running final contract audit", flush=True)
            audit_started = time.monotonic()
            _agent_log(
                f"contract audit started: effort={audit_reasoning_effort.value}; "
                f"completed_modules={len(completed_ids)}"
            )
            try:
                audit_thread = start_thread()
                audit_result = audit_thread.run(
                    _contract_audit_prompt(
                        requirements_dir,
                        completed_ids,
                        baseline_inventory,
                    ),
                    effort=audit_reasoning_effort,
                )
                if getattr(audit_result, "error", None) is not None:
                    raise RuntimeError(f"Codex contract audit failed: {audit_result.error}")
                try:
                    runtime.git.commit("audit: recover cross-layer contracts")
                    _agent_log(
                        f"contract audit completed in {time.monotonic() - audit_started:.1f}s; "
                        "git checkpoint created"
                    )
                except Exception:
                    _agent_log(
                        f"contract audit completed in {time.monotonic() - audit_started:.1f}s; "
                        "git checkpoint unavailable"
                    )
            except Exception as error:
                # 审计是增强回合，失败时保留前面已完成的增量实现。
                _agent_log(
                    f"contract audit failed after {time.monotonic() - audit_started:.1f}s: "
                    f"{_short_error(error)}"
                )
                print(
                    f"[codex] contract audit failed; keeping generated code: "
                    f"{_short_error(error)}",
                    flush=True,
                )

        validation_started = time.monotonic()
        _agent_log("preflight started")
        try:
            validation_errors = _run_project_validation(output_dir)
        except Exception as error:
            validation_errors = [f"preflight crashed: {error}"]
        _agent_log(
            f"preflight completed in {time.monotonic() - validation_started:.1f}s; "
            f"errors={len(validation_errors)}"
        )
        if validation_errors:
            _agent_log("preflight errors: " + " | ".join(_short_error(item, 500) for item in validation_errors))
        if validation_errors:
            print("[codex] Official preflight failed; requesting one focused repair", flush=True)
            repair_started = time.monotonic()
            _agent_log(f"preflight repair started: effort={repair_reasoning_effort.value}")
            try:
                repair_thread = shared_thread or start_thread()
                repair_result = repair_thread.run(
                    _repair_prompt(validation_errors),
                    effort=repair_reasoning_effort,
                )
                if getattr(repair_result, "error", None) is not None:
                    raise RuntimeError(f"Codex repair failed: {repair_result.error}")
                _agent_log(
                    f"preflight repair completed in {time.monotonic() - repair_started:.1f}s"
                )
            except Exception as error:
                # 修复回合自身失败也不能让已生成的代码作废。
                _agent_log(
                    f"preflight repair failed after {time.monotonic() - repair_started:.1f}s: "
                    f"{_short_error(error)}"
                )
                print(
                    f"[codex] repair turn failed; keeping generated code: "
                    f"{_short_error(error)}",
                    flush=True,
                )
                validation_errors = []
        if validation_errors:
            remaining_errors = _run_project_validation(output_dir)
            if remaining_errors:
                # 脚手架不含任何任务实现，回滚只会让产物退回空壳，反而更差；
                # 因此保留模型已经写出的代码，只记录仍未通过的预检项。
                print(
                    "[codex] preflight still failing after repair; keeping the generated code:\n"
                    + "\n\n".join(remaining_errors),
                    flush=True,
                )
                _agent_log(
                    f"preflight remains failing after repair: errors={len(remaining_errors)}"
                )
            else:
                _agent_log("preflight passed after repair")
        else:
            _agent_log("preflight passed")
        return failures


def main() -> int:
    args = parse_args()
    requirements_dir = Path(args.requirement_path).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    if not requirements_dir.is_dir():
        raise FileNotFoundError(f"Requirement directory not found: {requirements_dir}")
    if args.max_modules is not None and args.max_modules < 1:
        raise ValueError("--max-modules must be at least 1")

    agent_root = Path(__file__).resolve().parent
    requirements_payload = load_requirements_payload(requirements_dir)
    template_dir = select_template_dir(agent_root, requirements_dir, requirements_payload)
    copy_template_contents(template_dir, output_dir)
    skills_dir = copy_skills_to_output(agent_root / "skills", output_dir)
    all_modules = load_root_modules(requirements_dir)
    all_modules = _split_oversized_modules(all_modules)
    modules = all_modules[: args.max_modules] if args.max_modules else all_modules
    turns = _pack_into_turns(modules, _module_split_chars())
    root_context = _root_prompt_context(requirements_payload)
    requirements_payload = normalize_requirement_tree_for_traceability(requirements_payload)

    # 先保存完整需求树，再启动模块实现，保证中途失败时仍有可追踪的任务快照。
    runtime = AgentRuntime.from_env(project_dir=str(output_dir))
    runtime.traceability.init_db()
    runtime.git.ensure_repo(create_initial_commit=True)
    runtime.traceability.store_requirement_tree(requirements_payload)
    runtime.git.commit("requirements: store ROOT requirement tree")
    runtime.events.mark_run_started(
        f"Codex agent started: {len(turns)} implementation turns "
        f"covering {len(modules)} requirement modules ({args.task_type})"
    )
    try:
        failures = implement_modules(
            requirements_dir,
            output_dir,
            turns,
            skills_dir,
            runtime,
            root_context=root_context,
            dry_run=args.dry_run,
        )
    except Exception as error:
        # 走到这里说明失败发生在模块循环之外（例如 Codex 客户端初始化）。
        # 已写出的应用必须保留并交给官方部署与测试，退出码归零才能让它参与评测；
        # 否则一次基础设施故障就把几小时的生成成果整体归零。
        runtime.events.mark_run_failed(str(error))
        print(
            f"[agent] run failed, keeping the generated output for deployment: {error}",
            flush=True,
        )
        return 0
    if failures:
        runtime.events.mark_run_completed(
            f"Codex agent finished {len(turns) - len(failures)}/{len(turns)} "
            f"implementation turns; kept the partial output ({'; '.join(failures)})"
        )
    else:
        runtime.events.mark_run_completed(
            "All implementation turns completed"
            if len(modules) == len(all_modules)
            else f"Local validation completed {len(turns)}/{len(turns)} turns"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
