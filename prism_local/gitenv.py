"""Keep Git repository discovery local to the subprocess cwd."""
import os

SELECTORS = frozenset(("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                      "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                      "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
                      "GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
                      "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_NAMESPACE"))


def selects_repository(name: str) -> bool:
    name = name.upper()
    return name in SELECTORS or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))


def git_env(base: dict | None = None) -> dict:
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if not selects_repository(k)}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env
