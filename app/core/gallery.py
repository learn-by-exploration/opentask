"""Pre-made recipe gallery — curated templates users can install with one tap."""

from __future__ import annotations

# Each entry: { category, name, description, recipe_kwargs }
# recipe_kwargs match save_recipe() parameters.

GALLERY: list[dict] = [
    # ── Synclyf Platform ────────────────────────────────────────────
    {
        "category": "Synclyf",
        "name": "lifeflow",
        "description": "Task manager backend — Node/Express/SQLite",
        "recipe": {
            "triggers": ["lifeflow", "task manager", "tasks api", "habits", "kanban"],
            "agent": "claude",
            "project_dir": "/home/shyam/synclyf/services/lifeflow",
            "prompt_prefix": "You are working on LifeFlow, the task manager backend service of Synclyf.",
        },
    },
    {
        "category": "Synclyf",
        "name": "financeflow",
        "description": "Finance tracking — budgets, accounts, subscriptions",
        "recipe": {
            "triggers": ["financeflow", "finance", "budget", "expense", "subscription", "accounts"],
            "agent": "claude",
            "project_dir": "/home/shyam/synclyf/services/financeflow",
            "prompt_prefix": "You are working on FinanceFlow, the finance service of Synclyf.",
        },
    },
    {
        "category": "Synclyf",
        "name": "dataflow",
        "description": "Encrypted vault — passwords, documents, AES-256-GCM",
        "recipe": {
            "triggers": ["dataflow", "vault", "encrypted", "passwords", "secrets", "documents"],
            "agent": "claude",
            "project_dir": "/home/shyam/synclyf/services/dataflow",
            "prompt_prefix": "You are working on DataFlow, the encrypted vault service of Synclyf. Security is paramount.",
        },
    },
    {
        "category": "Synclyf",
        "name": "mealflow",
        "description": "Meal planning — recipes, grocery lists",
        "recipe": {
            "triggers": ["mealflow", "meal", "recipe", "grocery", "food"],
            "agent": "claude",
            "project_dir": "/home/shyam/synclyf/services/mealflow",
            "prompt_prefix": "You are working on MealFlow, the meal planning service of Synclyf.",
        },
    },
    {
        "category": "Synclyf",
        "name": "healthflow",
        "description": "Health vitals — medications, appointments, emergency cards",
        "recipe": {
            "triggers": ["healthflow", "health", "vitals", "medication", "appointments"],
            "agent": "claude",
            "project_dir": "/home/shyam/synclyf/services/healthflow",
            "prompt_prefix": "You are working on HealthFlow, the health service of Synclyf.",
        },
    },
    {
        "category": "Synclyf",
        "name": "synclyf-app",
        "description": "Flutter app — Dart/Riverpod frontend",
        "recipe": {
            "triggers": ["synclyf app", "flutter", "dart", "riverpod", "mobile app", "synclyf ui"],
            "agent": "claude",
            "project_dir": "/home/shyam/synclyf/app",
            "prompt_prefix": "You are working on the Synclyf Flutter app (Dart, Riverpod, go_router).",
        },
    },
    {
        "category": "Synclyf",
        "name": "synclyf-mcp",
        "description": "MCP server — exposes Synclyf as AI tools",
        "recipe": {
            "triggers": ["synclyf mcp", "mcp server", "synclyf tools"],
            "agent": "claude",
            "project_dir": "/home/shyam/synclyf/services/synclyf-mcp",
            "prompt_prefix": "You are working on the Synclyf MCP server (Model Context Protocol).",
        },
    },
    {
        "category": "Synclyf",
        "name": "synclyf-sdk",
        "description": "TypeScript API client library",
        "recipe": {
            "triggers": ["synclyf sdk", "sdk", "api client", "typescript client"],
            "agent": "claude",
            "project_dir": "/home/shyam/synclyf/services/synclyf-sdk",
            "prompt_prefix": "You are working on the Synclyf TypeScript SDK.",
        },
    },
    {
        "category": "Synclyf",
        "name": "synclyf-infra",
        "description": "Docker, deployment, orchestration",
        "recipe": {
            "triggers": ["synclyf deploy", "docker compose", "synclyf infra", "orchestration"],
            "agent": "claude",
            "project_dir": "/home/shyam/synclyf",
            "prompt_prefix": "You are working on Synclyf infrastructure — Docker, Compose, deployment.",
        },
    },

    # ── AI Powerhouse Tools ─────────────────────────────────────────
    {
        "category": "AI Powerhouse",
        "name": "ecc-skills",
        "description": "Everything Claude Code — 182+ skills library",
        "recipe": {
            "triggers": ["ecc", "everything claude code", "claude skills", "skill library"],
            "agent": "claude",
            "project_dir": "/home/shyam/synclyf/external/ai-powerhouse/everything-claude-code",
            "prompt_prefix": "You are working on the Everything Claude Code skills library.",
        },
    },
    {
        "category": "AI Powerhouse",
        "name": "super-claude",
        "description": "Core Claude Code agents and orchestration",
        "recipe": {
            "triggers": ["super claude", "claude agents", "agent orchestration"],
            "agent": "claude",
            "project_dir": "/home/shyam/synclyf/external/ai-powerhouse/super-claude",
            "prompt_prefix": "You are working on Super Claude — agent definitions and orchestration.",
        },
    },
    {
        "category": "AI Powerhouse",
        "name": "superpowers",
        "description": "Dev workflow skills — TDD, review, debugging, planning",
        "recipe": {
            "triggers": ["superpowers", "tdd skill", "code review skill", "debugging skill"],
            "agent": "claude",
            "project_dir": "/home/shyam/synclyf/external/ai-powerhouse/superpowers",
            "prompt_prefix": "You are working on the Superpowers skill library.",
        },
    },
    {
        "category": "AI Powerhouse",
        "name": "get-shit-done",
        "description": "Spec-driven dev — context engineering, agents, hooks",
        "recipe": {
            "triggers": ["gsd", "get shit done", "spec driven", "context engineering"],
            "agent": "claude",
            "project_dir": "/home/shyam/synclyf/external/ai-powerhouse/get-shit-done",
            "prompt_prefix": "You are working on the GSD (Get Shit Done) framework.",
        },
    },
    {
        "category": "AI Powerhouse",
        "name": "claude-mem",
        "description": "Persistent cross-session memory with vector search",
        "recipe": {
            "triggers": ["claude mem", "memory system", "vector memory", "persistent memory"],
            "agent": "claude",
            "project_dir": "/home/shyam/synclyf/external/ai-powerhouse/claude-mem",
            "prompt_prefix": "You are working on claude-mem — persistent cross-session memory.",
        },
    },

    # ── General Dev Workflows ───────────────────────────────────────
    {
        "category": "Dev Workflows",
        "name": "taskpilot",
        "description": "This project — Telegram bot + task queue",
        "recipe": {
            "triggers": ["taskpilot", "opentask", "telegram bot", "task queue"],
            "agent": "opencode",
            "project_dir": "/home/shyam/taskpilot",
            "prompt_prefix": "You are working on TaskPilot (OpenTask) — the Telegram AI agent bridge.",
        },
    },
    {
        "category": "Dev Workflows",
        "name": "bug-fix",
        "description": "Focused bug fixing with tests",
        "recipe": {
            "triggers": ["bug", "fix", "broken", "crash", "error", "failing"],
            "prompt_prefix": "Focus on finding and fixing the bug. Write a failing test first, then fix.",
            "prompt_suffix": "After fixing, verify all existing tests still pass.",
        },
    },
    {
        "category": "Dev Workflows",
        "name": "code-review",
        "description": "Thorough code review",
        "recipe": {
            "triggers": ["review", "code review", "audit", "check code"],
            "prompt_prefix": "Perform a thorough code review. Check for bugs, security issues, performance problems, and code quality.",
            "prompt_suffix": "Provide findings as: CRITICAL, HIGH, MEDIUM, LOW severity with specific line references.",
        },
    },
    {
        "category": "Dev Workflows",
        "name": "refactor",
        "description": "Safe refactoring with test coverage",
        "recipe": {
            "triggers": ["refactor", "clean up", "simplify", "restructure"],
            "prompt_prefix": "Refactor safely. Ensure all tests pass before and after changes.",
            "prompt_suffix": "No behavior changes — only structural improvements. Run tests to verify.",
        },
    },
    {
        "category": "Dev Workflows",
        "name": "new-feature",
        "description": "TDD feature development",
        "recipe": {
            "triggers": ["new feature", "implement", "add feature", "build"],
            "prompt_prefix": "Use TDD: write tests first (RED), implement to pass (GREEN), then refactor (IMPROVE).",
            "prompt_suffix": "Ensure 80%+ test coverage for new code.",
        },
    },
    {
        "category": "Dev Workflows",
        "name": "security-audit",
        "description": "OWASP Top 10 security review",
        "recipe": {
            "triggers": ["security", "vulnerability", "owasp", "penetration", "pentest"],
            "prompt_prefix": "Perform a security audit following OWASP Top 10. Check for injection, auth bypass, XSS, CSRF, secrets exposure.",
            "prompt_suffix": "Rate each finding as CRITICAL/HIGH/MEDIUM/LOW with remediation steps.",
        },
    },
    {
        "category": "Dev Workflows",
        "name": "docs-update",
        "description": "Documentation and README updates",
        "recipe": {
            "triggers": ["documentation", "readme", "docs", "api docs"],
            "prompt_prefix": "Update documentation to reflect the current state of the codebase.",
            "prompt_suffix": "Ensure examples are runnable and API references are accurate.",
        },
    },
    {
        "category": "Dev Workflows",
        "name": "test-coverage",
        "description": "Fill test coverage gaps",
        "recipe": {
            "triggers": ["test coverage", "missing tests", "coverage gaps", "untested"],
            "prompt_prefix": "Identify and fill test coverage gaps. Focus on untested edge cases and error paths.",
            "prompt_suffix": "Use AAA pattern (Arrange-Act-Assert). Target 80%+ coverage.",
        },
    },
    {
        "category": "Dev Workflows",
        "name": "performance",
        "description": "Performance profiling and optimization",
        "recipe": {
            "triggers": ["performance", "slow", "optimize", "profiling", "bottleneck"],
            "prompt_prefix": "Profile and optimize. Identify bottlenecks with measurements before and after changes.",
            "prompt_suffix": "Show benchmarks proving the improvement. No premature optimization.",
        },
    },
]


def get_gallery_categories() -> list[str]:
    """Return unique category names in order."""
    seen: set[str] = set()
    cats: list[str] = []
    for item in GALLERY:
        c = item["category"]
        if c not in seen:
            seen.add(c)
            cats.append(c)
    return cats


def get_gallery_by_category(category: str) -> list[dict]:
    """Return gallery items for a specific category."""
    return [item for item in GALLERY if item["category"] == category]


def get_gallery_item(name: str) -> dict | None:
    """Return a single gallery item by name."""
    for item in GALLERY:
        if item["name"] == name:
            return item
    return None
