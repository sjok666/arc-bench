# ARC-Bench Agent

This agent owns a single entrypoint (`main.py`) and a generic fallback scaffold (`template/`).
During Hackathon Evolution, ARC-Bench normally injects the team's previous best application into
the output directory before this agent starts. The agent preserves that baseline and asks Codex to
implement the evolution requirements incrementally in bounded, sequential turns. A genuinely
empty output may still use the scaffold as a fallback.

## What to Edit

- `main.py`: the entrypoint and the whole agent. It is task-agnostic: it reads whatever ROOT
  requirement is mounted and drives Codex to implement it.
- `template/`: the generic starter scaffold (see below).

## The scaffold is generic, not task-specific

`template/` contains only a generic initialization framework for the target stack:

- Vite + React build tooling and application shell (`frontend/`);
- an Express bootstrap with a health endpoint, static hosting and a SQLite connection helper
  (`backend/`);
- lint/test configuration and lockfiles.

It contains **no** feature pages, **no** domain routes, **no** business logic and **no** data of
any kind. There is no seeded content, no pre-written page for any scenario, and no code that
special-cases a particular task. Every feature, page, route, table, permission rule and record the
task needs is implemented by the model from the requirements. `select_template_dir()` returns this
one scaffold for every task, because it is independent of the task content.

## Entrypoint Contract

ARC-Bench runs your agent like this:

```bash
python3 main.py /path/to/requirements --output-dir /path/to/output --type web
```

The input directory must contain `requirements.yaml` with `id: ROOT`. The agent preserves an
already prepared application, or copies only missing scaffold files when the output is empty,
then records the complete requirement tree, initializes
`.arc/traceability`, makes an initial Git checkpoint, and then sends ROOT-child subtrees to Codex
in sequence, splitting oversized subtrees into smaller requirement units when needed. Each turn
also receives compact ROOT-level rules/data, and split leaves retain their ancestor descriptions,
dependencies, and other non-child fields. Codex edits the same output directory throughout. A
formal platform submission invokes the same task-agnostic ZIP for both task requirement trees;
this optimization focuses on the GitHub task without adding any GitHub-specific code to the
scaffold.

The bundled `skills/` directory is copied to `.codex/skills/` in the output project. Codex is told
where to find the skills and can use their scripts for runtime progress, traceability, and git
checkpoints when those actions are useful.

## The agent always calls the model

The scaffold has no task implementation, so there is no path that produces an application without
the model: every implementation turn is a real Codex turn and the turns collectively cover the
complete requirement tree. After the turns finish, the agent runs a local
preflight (frontend/backend layout, `package.json` scripts, a backend syntax check, and a frontend
build when dependencies are present); if it fails, it makes **one** focused repair turn. If the
preflight still fails it keeps the model's code and logs what is still failing — it never falls back
to shipping a pre-built answer, because there is none to fall back to.

## Built-in quality and cost discipline

The scaffold carries no answers, so the pass rate depends entirely on how faithfully the model
implements the requirement scenarios. Every module turn is therefore instructed to:

- treat each scenario's GIVEN/WHEN/THEN steps as the acceptance criteria;
- treat ROOT-level description/data as the authoritative shared fixture, permission, and reset
  contract; use runner-provisioned records where specified and seed only records the task leaves to
  the application, idempotently and without overwriting later user changes;
- reproduce every exact quoted label, button name, message, count and value verbatim;
- enforce validation, permissions and state transitions on the server, not only in the UI;
- persist authoritative state so it survives a reload and a fresh browser session.

The same prompts cut avoidable token spend without reducing the work done:

- the compact ROOT context and each module's own requirement subtree are passed inline, so the
  model is told **not** to read the full `requirements.yaml`; ancestor constraints are preserved
  when a large module is split, without repeating the ancestor's child subtrees;
- the text remains authoritative, but the model may open only a public reference PNG linked by
  the current module when a visual detail is ambiguous; unrelated images and hidden tests are
  never inspected;
- each module prompt carries a short source-file manifest of the current project, so the model
  does not spend turns discovering what already exists;
- the model is told not to add npm dependencies unless strictly necessary, because a dependency
  that fails to install breaks the official build and forfeits every test;
- intermediate turns run only focused checks for changed code; the agent runs backend syntax and,
  when dependencies are already installed, one final frontend build after all implementation
  turns. The official Runner performs its normal build if dependencies are not present yet;
- the agent uses `openai-codex` directly and does not pin the separate Python `openai` SDK, avoiding
  an unnecessary downgrade of the Runner's preinstalled SDK.

## Resilience: one bad turn must not destroy the run

A real platform run crashed 2h37m in: the fifth module's Codex turn died on a remote context
compaction failure (`remote compaction v2 expected exactly one compaction output item, got 0`),
and because the module loop raised, the whole submission exited 1 and five completed modules were
thrown away. The engine now treats model/infrastructure failures as recoverable:

- **Oversized modules are split, then packed back into bounded turns.** Any ROOT child whose
  inline spec exceeds `ARCBENCH_MODULE_SPLIT_CHARS` (default 45000) is replaced by its own
  children, recursively (max depth 3); consecutive small pieces are then greedily packed so a
  turn's spec stays under the same limit. A turn also contains at most two sibling pieces from the
  same oversized ROOT module. Each leaf retains the descriptions/dependencies of split ancestors,
  and every turn gets the compact ROOT rules and fixture contract. The current GitHub requirements
  therefore run in **8 full implementation turns**; the four sibling pieces that previously
  shared one failing turn now run as two smaller turns. This is adaptive context management, not a
  cap on how much the model may implement.
- **Each failed turn is retried on a fresh thread.** `ARCBENCH_MODULE_ATTEMPTS` (default 2, max 4); a
  retry never reuses the thread whose context just failed; retries use a short exponential backoff
  to avoid immediately re-hitting a transient proxy/API failure.
- **Modules in a turn that still fails are marked failed**, and later turns continue.
- **The run always ends by running the preflight** (plus one repair turn, which is itself guarded)
  and **always exits 0** while there is generated output, so the official deploy and Playwright
  tests still run over whatever was built. A partial app that builds and serves scores strictly
  more than a crashed run, which scores zero.
- **A total time budget** (`ARCBENCH_TIME_BUDGET_MINUTES`, default 720, 0 = unlimited) stops
  implementation when it is exceeded, keeps what was built, and goes straight to the preflight, so
  a pathological run cannot burn the platform's 48-hour limit.

## The agent records its own process evidence

The platform's process graph (Interfaces: UI/API/FUNC/DB, Tests: E2E/INTEGRATION/UNIT) only shows
what has been recorded, and previously that depended on the model voluntarily calling the
traceability skill — which in practice almost never happened, so the graph stayed empty. The agent
now records it deterministically after each module turn, from real evidence:

- it snapshots the git HEAD before the turn, then takes `git diff --name-only` afterwards, so only
  files the model actually wrote in that turn are considered;
- from those files it extracts backend routes (`router.get("/api/...")`), database tables
  (`CREATE TABLE ...`) and frontend page routes (`path="..."`), and upserts them as implemented
  interfaces attributed to that module (`REQ-3.API.GET-api-repositories`, …);
- new `*.test.*` / `*.spec.*` files are recorded as unit/e2e tests attributed to that module, with
  `passed` left unknown — the agent never invents a pass result, because the official tests run
  after the agent exits;
- each turn ends with a git checkpoint (`implement REQ-...`), which doubles as the audit trail.

This is the agent's own bookkeeping about work the model already did; it implements nothing and is
therefore fully within the competition rules. It also saves tokens: the model is told it does not
need to record interfaces itself.

## Model Variables

The runner injects:

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `MODEL`
- `VISUAL_API_KEY` (when the submission configures a visual model)
- `VISUAL_BASE_URL` (when the submission configures a visual model)
- `VISUAL_MODEL` (the separately selected image-capable model)

The agent calls the injected model through `openai-codex`; the API credentials are provided by the
runner and are not stored in the ZIP. `MODEL` controls the main coding turn, while
`VISUAL_MODEL` is a separate platform-provided visual configuration. The agent logs whether the
visual configuration is present without printing credentials, and explicitly lists the public
reference PNGs linked by each requirement turn. No separate Python `openai` SDK is needed by the
agent.

## API 配置与调用成本控制

API 密钥不要写进智能体 ZIP，也不要提交到 Git。使用官方本地模拟器时，配置文件应放在
`hackathon-local-simulation/.env`，然后通过 `local_submit.py --env-file .env` 传入容器。
服务器上的对应位置可以使用 `/root/hackathon-local-simulation/.env`。最小配置如下：

```dotenv
OPENAI_API_KEY=your-key
OPENAI_BASE_URL=https://your-provider.example/v1
MODEL=your-model
# Optional visual configuration supplied by the platform:
VISUAL_API_KEY=your-visual-key
VISUAL_BASE_URL=https://your-visual-provider.example/v1
VISUAL_MODEL=your-visual-model
```

如果直接运行 `main.py`，则在启动进程的环境中配置同名变量。例如 PowerShell：

```powershell
$env:OPENAI_API_KEY = "your-key"
$env:OPENAI_BASE_URL = "https://your-provider.example/v1"
$env:MODEL = "your-model"
python .\main.py .\hackathon--github --output-dir .\local-output --type web
```

智能体默认采用以下成本控制策略（只影响调用方式，不影响「必须调用模型」这一前提）：

- 每个实现回合使用独立 Codex 线程（一个回合可包含多个相关需求子模块），避免后续请求重复携带前序回合的工具输出和对话历史；
- 推理强度默认为 `medium`，在保留复杂代码任务质量的同时避免默认使用更高成本档位；
- 模块需求 JSON 使用紧凑编码，减少重复输入 token；
- 构建失败后的单次定向修复默认使用 `low` 推理强度。

可以通过环境变量调整策略：

```dotenv
# 可选：none/minimal/low/medium/high/xhigh/max/ultra，默认 medium
ARCBENCH_REASONING_EFFORT=medium

# 可选：true/false，默认 true；false 恢复所有模块共用一个线程
ARCBENCH_ISOLATE_MODULE_THREADS=true

# 可选：修复构建错误时的推理强度，默认 low
ARCBENCH_REPAIR_REASONING_EFFORT=low

# 可选：最终跨模块契约审计的推理强度，默认 medium；设为 low 可节省一次审计成本
ARCBENCH_FINAL_AUDIT_REASONING_EFFORT=medium

# 可选：超过该字符数的 ROOT 子模块拆成其子节点再实现，默认 45000，0 关闭拆分；同一大模块每回合最多合并两个拆分子项
ARCBENCH_MODULE_SPLIT_CHARS=45000

# 可选：每个实现回合最多尝试次数（重试换新线程），默认 2，上限 4
ARCBENCH_MODULE_ATTEMPTS=2

# 可选：实现阶段总时间预算（分钟），默认 720，0 表示不限制
ARCBENCH_TIME_BUDGET_MINUTES=720

# 可选：任务类型标签，默认 web，仅用于运行记录
ARCBENCH_TASK_TYPE=web
```

降低推理强度或关闭线程隔离可能减少或改变成本，但会增加实现质量和上下文污染风险。

## Local validation

Install the Python dependencies and validate the submission contract without calling Codex:

```bash
python -m pip install -r requirements.txt
python main.py /path/to/requirements --output-dir /tmp/arcbench-output --type web --dry-run
```

The dry run copies the scaffold, initializes `.arc/traceability`, creates a Git checkpoint, stores
the complete requirement tree, and prints the ROOT modules that would be sent to Codex. A normal run
omits `--dry-run`; ARC-Bench supplies `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `MODEL` at runtime.
For local step-by-step validation, `--max-modules 1` runs only the first ROOT module; do not pass
this limit in the competition run.

## Official local simulation compatibility

The implementation follows the contract documented by the official
[`hackathon-local-simulation`](https://github.com/code-philia/hackathon-local-simulation) project:

- the ZIP root contains `main.py` and `requirements.txt`;
- the agent writes the final application into `--output-dir`;
- the generated output keeps `frontend/package.json` and `backend/package.json`;
- the runner builds the frontend, then starts the backend with `HOST=0.0.0.0 PORT=3000`;
- Playwright runs inside the same container against `http://127.0.0.1:3000`.

The same archive is evaluated against every task: the platform submits one ZIP per team and runs it
once per task, each time with only that task's requirements mounted. Because the agent is driven
entirely by the mounted requirements and the scaffold carries no task content, no per-task routing
or per-task asset is needed.

Do not rely on a custom `deploy.sh` as the only deployment path: the local simulator supports that
convenience path, but the official runner requires the standard `frontend` + `backend` layout. The
simulator's public Playwright tests are useful for debugging, while the competition may also run
hidden tests.

To create a clean upload archive from PowerShell:

```powershell
.\package.ps1 -OutputZip "$PWD\..\agent-codex-based-optimized.zip"
tar -tf "$PWD\..\agent-codex-based-optimized.zip" | Select-Object -First 20
```

## Compliance with the competition notice (参赛须知)

The competition notice (三、2 and 三、3) allows a generic application scaffold and shared component
library, but forbids shipping pages, business logic or feature components that are pre-implemented
for the current task, and forbids an agent that completes the task without actually calling the
model or by otherwise bypassing the model's work.

This archive is built to that rule:

- `template/` is a generic starter scaffold only — no task pages, no domain routes, no business
  logic, no seed data;
- there is no per-task code, no dispatch table keyed on the task name, and no bundled per-task
  implementation;
- the agent has no "no model" mode and no environment switch that skips the model; all
  requirements are implemented through real Codex turns, and a failed preflight is repaired, not
  rolled back to a pre-built answer.

Legacy local reference files are outside the package allowlist and are not included in the
submission ZIP.
