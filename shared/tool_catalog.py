"""
NexusAI Tool Catalog
--------------------
Defines every tool the platform can provide to bots during task execution.
Users configure which tools are enabled via the Settings → Tools page.

Tools are grouped into categories so users can enable/disable by domain
(e.g. enable all .NET tools, disable everything IoT-related).

The ``enabled_tools`` setting (stored in ``nexus_settings``) is a list of
tool IDs. The scheduler checks this list before making a tool available to a
bot. Missing entries default to the value of ``default_enabled``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class ToolDefinition:
    """A single installable / configurable tool."""

    id: str
    name: str
    category: str
    description: str
    # Shell / package check used to verify availability at runtime.
    # E.g. "python --version", "dotnet --version", "npm --version".
    check_command: Optional[str] = None
    # Whether to enable this tool by default (first install / no stored setting).
    default_enabled: bool = True
    # Human-readable install hint shown in the UI when the tool is missing.
    install_hint: Optional[str] = None
    # Tags used by UI preset buttons (e.g. "web", "dotnet", "iot").
    presets: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Full tool catalog
# ---------------------------------------------------------------------------

TOOL_CATALOG: List[ToolDefinition] = [
    # ── Workspace ────────────────────────────────────────────────────────────
    ToolDefinition(
        id="filesystem",
        name="Filesystem R/W",
        category="workspace",
        description="Read and write files in the project workspace.",
        default_enabled=True,
        presets=["all"],
    ),
    ToolDefinition(
        id="repo_search",
        name="Semantic Repo Search",
        category="workspace",
        description="Vector-index search across the project repository.",
        default_enabled=True,
        presets=["all"],
    ),
    # ── Research ─────────────────────────────────────────────────────────────
    ToolDefinition(
        id="web_search",
        name="Web Search",
        category="research",
        description="Search the web for documentation, examples, and references.",
        default_enabled=True,
        presets=["all"],
    ),
    ToolDefinition(
        id="vault_search",
        name="Project Vault Search",
        category="research",
        description="Search project-specific documentation stored in the data vault.",
        default_enabled=True,
        presets=["all"],
    ),
    # ── Execution – Language Runtimes ────────────────────────────────────────
    ToolDefinition(
        id="code_exec_python",
        name="Python Execution",
        category="execution",
        description="Run Python scripts and tests via the interpreter in the bot's environment.",
        check_command="python --version",
        default_enabled=True,
        install_hint="Install Python 3.10+ and ensure it is on PATH.",
        presets=["all", "data_science", "web", "ai"],
    ),
    ToolDefinition(
        id="code_exec_dotnet",
        name=".NET / C# Execution",
        category="execution",
        description="Build and run .NET projects (dotnet build / dotnet run).",
        check_command="dotnet --version",
        default_enabled=False,
        install_hint="Install the .NET SDK from https://dotnet.microsoft.com/download",
        presets=["dotnet", "desktop", "web"],
    ),
    ToolDefinition(
        id="code_exec_node",
        name="Node.js / npm Execution",
        category="execution",
        description="Execute JavaScript/TypeScript projects and npm scripts.",
        check_command="node --version",
        default_enabled=False,
        install_hint="Install Node.js LTS from https://nodejs.org/",
        presets=["web", "mobile"],
    ),
    ToolDefinition(
        id="code_exec_rust",
        name="Rust / Cargo",
        category="execution",
        description="Build, run, and test Rust projects via cargo.",
        check_command="cargo --version",
        default_enabled=False,
        install_hint="Install Rust via https://rustup.rs/",
        presets=["systems", "iot"],
    ),
    ToolDefinition(
        id="code_exec_cpp",
        name="C / C++ Build",
        category="execution",
        description="Compile C and C++ projects using gcc, clang, or cmake.",
        check_command="cmake --version",
        default_enabled=False,
        install_hint="Install a C++ toolchain (GCC, Clang, or MSVC) and cmake.",
        presets=["systems", "iot", "game"],
    ),
    ToolDefinition(
        id="code_exec_java",
        name="Java / Maven / Gradle",
        category="execution",
        description="Build and run Java / JVM projects.",
        check_command="java -version",
        default_enabled=False,
        install_hint="Install JDK 17+ and Maven or Gradle.",
        presets=["enterprise", "android"],
    ),
    ToolDefinition(
        id="code_exec_go",
        name="Go Build & Test",
        category="execution",
        description="Build and test Go projects via the go toolchain.",
        check_command="go version",
        default_enabled=False,
        install_hint="Install Go from https://go.dev/dl/",
        presets=["systems", "web"],
    ),
    ToolDefinition(
        id="code_exec_swift",
        name="Swift / Xcode CLI",
        category="execution",
        description="Build and run Swift projects using swift or xcodebuild.",
        check_command="swift --version",
        default_enabled=False,
        install_hint="Install Xcode or Swift toolchain (macOS only).",
        presets=["mobile", "desktop"],
    ),
    ToolDefinition(
        id="code_exec_kotlin",
        name="Kotlin / Gradle",
        category="execution",
        description="Build Kotlin projects via Gradle.",
        check_command="kotlinc -version",
        default_enabled=False,
        install_hint="Install Kotlin and Gradle.",
        presets=["android", "enterprise"],
    ),
    ToolDefinition(
        id="code_exec_php",
        name="PHP Execution",
        category="execution",
        description="Run PHP scripts and Composer-managed projects.",
        check_command="php --version",
        default_enabled=False,
        install_hint="Install PHP 8+ and Composer.",
        presets=["web"],
    ),
    # ── Data ─────────────────────────────────────────────────────────────────
    ToolDefinition(
        id="db_sql",
        name="SQL Database Tools",
        category="data",
        description="Connect to SQL databases (SQLite, PostgreSQL, MySQL, MSSQL) for query and migration.",
        default_enabled=True,
        presets=["all", "web", "enterprise", "data_science"],
    ),
    ToolDefinition(
        id="db_mongo",
        name="MongoDB Tools",
        category="data",
        description="Connect to MongoDB for document query and schema changes.",
        check_command="mongosh --version",
        default_enabled=False,
        install_hint="Install MongoDB Shell (mongosh).",
        presets=["web", "data_science"],
    ),
    ToolDefinition(
        id="db_redis",
        name="Redis Tools",
        category="data",
        description="Connect to Redis for cache inspection and data operations.",
        check_command="redis-cli --version",
        default_enabled=False,
        install_hint="Install Redis and redis-cli.",
        presets=["web", "enterprise"],
    ),
    # ── Testing ──────────────────────────────────────────────────────────────
    ToolDefinition(
        id="test_runner_pytest",
        name="pytest",
        category="testing",
        description="Run Python test suites using pytest.",
        check_command="pytest --version",
        default_enabled=True,
        install_hint="pip install pytest",
        presets=["all", "data_science", "ai"],
    ),
    ToolDefinition(
        id="test_runner_jest",
        name="Jest / Vitest",
        category="testing",
        description="Run JavaScript/TypeScript test suites.",
        check_command="npx jest --version",
        default_enabled=False,
        install_hint="npm install --save-dev jest OR vitest",
        presets=["web", "mobile"],
    ),
    ToolDefinition(
        id="test_runner_dotnet_test",
        name=".NET Test (xUnit / NUnit / MSTest)",
        category="testing",
        description="Run .NET test projects via dotnet test.",
        check_command="dotnet --version",
        default_enabled=False,
        install_hint="Included with the .NET SDK.",
        presets=["dotnet", "desktop"],
    ),
    ToolDefinition(
        id="test_runner_cargo_test",
        name="Cargo Test",
        category="testing",
        description="Run Rust unit and integration tests via cargo test.",
        check_command="cargo --version",
        default_enabled=False,
        install_hint="Included with Rust/cargo.",
        presets=["systems", "iot"],
    ),
    ToolDefinition(
        id="test_runner_gtest",
        name="Google Test (C++)",
        category="testing",
        description="Run C++ unit tests using the GoogleTest framework.",
        default_enabled=False,
        install_hint="Install GoogleTest and configure cmake.",
        presets=["systems", "game"],
    ),
    ToolDefinition(
        id="test_runner_junit",
        name="JUnit / Gradle Test",
        category="testing",
        description="Run Java test suites via Gradle or Maven.",
        check_command="java -version",
        default_enabled=False,
        install_hint="Included with JUnit 5 and a build tool.",
        presets=["enterprise", "android"],
    ),
    # ── UI & UX Testing ──────────────────────────────────────────────────────
    ToolDefinition(
        id="ui_browser",
        name="Browser Automation (Playwright / Puppeteer)",
        category="ui_testing",
        description="Automate and test web UIs using a headless or headed browser.",
        check_command="npx playwright --version",
        default_enabled=False,
        install_hint="npx playwright install",
        presets=["web"],
    ),
    ToolDefinition(
        id="ui_desktop",
        name="Desktop UI Testing (WinForms / WPF / Electron)",
        category="ui_testing",
        description="Test desktop application UIs using WinAppDriver or Electron test tools.",
        default_enabled=False,
        install_hint="Install WinAppDriver (Windows) or equivalent automation driver.",
        presets=["desktop", "dotnet"],
    ),
    ToolDefinition(
        id="ui_mobile",
        name="Mobile Testing (Appium / XCUITest)",
        category="ui_testing",
        description="Test iOS and Android app UIs using Appium or native test frameworks.",
        check_command="appium --version",
        default_enabled=False,
        install_hint="npm install -g appium",
        presets=["mobile"],
    ),
    ToolDefinition(
        id="ui_game",
        name="Game Engine Testing (Unreal / Unity)",
        category="ui_testing",
        description="Automate and test game builds using Unreal Automation or Unity Test Framework.",
        default_enabled=False,
        install_hint="Requires Unreal Engine or Unity with automation plugins installed.",
        presets=["game"],
    ),
    # ── DevOps ───────────────────────────────────────────────────────────────
    ToolDefinition(
        id="container_docker",
        name="Docker Build & Run",
        category="devops",
        description="Build Docker images and run containers for testing and deployment.",
        check_command="docker --version",
        default_enabled=False,
        install_hint="Install Docker Desktop or the Docker Engine.",
        presets=["web", "enterprise", "ai"],
    ),
    ToolDefinition(
        id="devops_git",
        name="Git CLI",
        category="devops",
        description="Run git commands — diff, status, log, commit — within the workspace.",
        check_command="git --version",
        default_enabled=True,
        presets=["all"],
    ),
    # ── IoT ──────────────────────────────────────────────────────────────────
    ToolDefinition(
        id="iot_serial",
        name="IoT / Serial Communication",
        category="iot",
        description="Communicate with embedded devices over serial/UART interfaces.",
        default_enabled=False,
        install_hint="Install pyserial (pip install pyserial) or equivalent.",
        presets=["iot"],
    ),
    ToolDefinition(
        id="iot_cross_compile",
        name="Cross-Compiler Toolchain",
        category="iot",
        description="Compile code for embedded/ARM targets using a cross-compiler.",
        default_enabled=False,
        install_hint="Install arm-none-eabi-gcc or similar cross-compiler.",
        presets=["iot", "systems"],
    ),
    # ── AI / LLM ─────────────────────────────────────────────────────────────
    ToolDefinition(
        id="llm_inference",
        name="LLM Inference (local / cloud)",
        category="ai",
        description="Run LLM inference via Ollama, LM Studio, OpenAI, Claude, or Gemini backends.",
        default_enabled=True,
        presets=["all", "ai"],
    ),
    ToolDefinition(
        id="embedding_model",
        name="Embedding Model",
        category="ai",
        description="Generate text embeddings for semantic search and similarity tasks.",
        default_enabled=True,
        presets=["all", "ai"],
    ),
]

# ---------------------------------------------------------------------------
# Preset groupings (shown as quick-select buttons in the Settings → Tools UI)
# ---------------------------------------------------------------------------

TOOL_PRESETS: dict[str, dict] = {
    "all": {
        "label": "All Tools",
        "description": "Enable every available tool.",
    },
    "web": {
        "label": "Web Development",
        "description": "Node.js, Python, browser testing, SQL, Docker.",
    },
    "dotnet": {
        "label": ".NET / C#",
        "description": ".NET SDK, xUnit testing, WinForms/WPF UI testing.",
    },
    "data_science": {
        "label": "Data Science",
        "description": "Python, pytest, SQL, MongoDB, embeddings.",
    },
    "mobile": {
        "label": "Mobile Development",
        "description": "Node.js, Swift, Kotlin, Appium.",
    },
    "desktop": {
        "label": "Desktop Apps",
        "description": ".NET, Swift, desktop UI testing.",
    },
    "game": {
        "label": "Game Development",
        "description": "C++, cmake, GoogleTest, Unreal/Unity testing.",
    },
    "iot": {
        "label": "IoT / Embedded",
        "description": "C/C++, Rust, serial communication, cross-compiler.",
    },
    "systems": {
        "label": "Systems Programming",
        "description": "C/C++, Rust, Go, cross-compilation.",
    },
    "enterprise": {
        "label": "Enterprise / JVM",
        "description": "Java, Kotlin, SQL, Redis, Docker.",
    },
    "ai": {
        "label": "AI / LLM",
        "description": "Python, LLM inference, embeddings, Docker.",
    },
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TOOL_CATALOG_BY_ID: dict[str, ToolDefinition] = {t.id: t for t in TOOL_CATALOG}

TOOL_CATEGORIES: list[str] = list(
    dict.fromkeys(t.category for t in TOOL_CATALOG)
)

CATEGORY_LABELS: dict[str, str] = {
    "workspace": "Workspace",
    "research": "Research",
    "execution": "Language Runtimes",
    "data": "Data & Databases",
    "testing": "Testing Frameworks",
    "ui_testing": "UI & UX Testing",
    "devops": "DevOps & Containers",
    "iot": "IoT & Embedded",
    "ai": "AI & LLM Inference",
}


def default_enabled_tools() -> list[str]:
    """Return the IDs of all tools that should be enabled on a fresh install."""
    return [t.id for t in TOOL_CATALOG if t.default_enabled]


def tools_for_preset(preset_id: str) -> list[str]:
    """Return the list of tool IDs that belong to a given preset."""
    if preset_id == "all":
        return [t.id for t in TOOL_CATALOG]
    return [t.id for t in TOOL_CATALOG if preset_id in t.presets]
