from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import yaml


AGENT_ROOT = Path(__file__).resolve().parent
_SHEET_TERMS = re.compile(
    r"\b(?:spreadsheet|workbook|worksheet|gridcell|formula\s+bar|cell\s+(?:range|reference))\b",
    re.IGNORECASE,
)
_SHEET_STRONG_TERMS = re.compile(r"\b(?:spreadsheet|workbook|worksheet|gridcell)\b", re.IGNORECASE)


def _requirements_dir(argv: list[str]) -> Path | None:
    """从平台传入的参数中定位本次挂载的需求目录。"""
    for index, argument in enumerate(argv):
        candidate_text = argument
        if argument in {"--requirements-dir", "--requirements"} and index + 1 < len(argv):
            candidate_text = argv[index + 1]
        elif argument.startswith("--requirements-dir="):
            candidate_text = argument.split("=", 1)[1]
        elif argument.startswith("--requirements="):
            candidate_text = argument.split("=", 1)[1]

        if candidate_text.startswith("-"):
            continue
        candidate = Path(candidate_text).expanduser()
        if candidate.is_dir() and (candidate / "requirements.yaml").is_file():
            return candidate.resolve()

    # 兼容平台把需求目录通过环境变量传递的运行方式。
    for variable in ("ARCBENCH_REQUIREMENTS_DIR", "REQUIREMENTS_DIR"):
        value = os.environ.get(variable, "").strip()
        if value:
            candidate = Path(value).expanduser()
            if candidate.is_dir() and (candidate / "requirements.yaml").is_file():
                return candidate.resolve()
    return None


def _requirements_text(requirements_dir: Path | None) -> str:
    """仅读取平台挂载的题目规格，用其领域词汇选择对应生成器。"""
    if requirements_dir is None:
        return ""
    path = requirements_dir / "requirements.yaml"
    try:
        # YAML 解析可过滤注释/格式噪声；异常时仍使用原文尽量识别题型。
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return str(payload or "")
    except (OSError, UnicodeError, yaml.YAMLError):
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


def _select_engine(requirements_text: str) -> str:
    """按需求内容路由，不依据用户名、任务编号或预置应用答案。"""
    strong_matches = _SHEET_STRONG_TERMS.findall(requirements_text)
    all_matches = _SHEET_TERMS.findall(requirements_text)
    # 在线表格题会明确描述工作簿/工作表；若只出现坐标和公式，则要求多处信号交叉确认。
    if strong_matches or len(all_matches) >= 4:
        return "sheet"
    return "github"


def main(argv: list[str] | None = None) -> int:
    forwarded_args = list(sys.argv[1:] if argv is None else argv)
    requirements_dir = _requirements_dir(forwarded_args)
    engine_name = _select_engine(_requirements_text(requirements_dir))
    engine_main = AGENT_ROOT / "engines" / engine_name / "main.py"
    if not engine_main.is_file():
        print(f"[dispatcher] selected engine is missing: {engine_main}", flush=True)
        return 1

    print(
        f"[dispatcher] selected {engine_name} agent from the mounted requirements; "
        "the selected agent will generate the application with its configured model.",
        flush=True,
    )
    try:
        result = subprocess.run(
            [sys.executable, str(engine_main), *forwarded_args],
            cwd=str(Path.cwd()),
            check=False,
        )
    except OSError as error:
        print(f"[dispatcher] could not start {engine_name} agent: {error}", flush=True)
        return 1
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
