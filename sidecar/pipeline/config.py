"""Runtime configuration.

Two rules shape this module:

1. The pipeline owns the Canvas credential and nothing else. Model credentials
   belong to opencode, which stores them in its own auth store. The pipeline
   never holds a model API key and never calls a model provider directly —
   all agentic work happens inside the opencode process.

2. Secrets are never stored in the project directory. They are read from, in
   order: the environment, the OS keychain, then a user-local secrets file
   outside any git-tracked path.
"""

from __future__ import annotations

import json
import os
import platform
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "grading-pipeline"

# Generated artefacts are written with explicit newlines. Text mode would
# otherwise translate them to CRLF on Windows, so the same report would differ
# byte-for-byte by platform and its digest would change with the host.
NEWLINE = "\n"


def data_dir() -> Path:
    """Per-user writable directory for database, secrets and caches."""
    if platform.system() == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif platform.system() == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return Path(os.environ.get("GRADING_PIPELINE_HOME", base / APP_NAME))


def workspace_dir() -> Path:
    """Where collected submissions and rendered reports are written."""
    override = os.environ.get("GRADING_PIPELINE_WORKSPACE")
    return Path(override) if override else (Path.home() / "grading-pipeline-workspace")


def secrets_file() -> Path:
    return data_dir() / "secrets.json"


SECRET_KEYS = ("canvas_base_url", "canvas_api_token", "openrouter_api_key")


# --------------------------------------------------------------- secrets

def _keychain_get(name: str) -> str | None:
    try:
        import keyring  # type: ignore
        return keyring.get_password(APP_NAME, name)
    except Exception:
        return None


def _keychain_set(name: str, value: str) -> bool:
    try:
        import keyring  # type: ignore
        keyring.set_password(APP_NAME, name, value)
        return True
    except Exception:
        return False


def _keychain_delete(name: str) -> bool:
    try:
        import keyring  # type: ignore
        keyring.delete_password(APP_NAME, name)
        return True
    except Exception:
        return False


def get_secret(name: str) -> str | None:
    env = os.environ.get(name.upper())
    if env:
        return env
    from_keychain = _keychain_get(name)
    if from_keychain:
        return from_keychain
    path = secrets_file()
    if path.exists():
        try:
            return json.loads(path.read_text()).get(name)
        except Exception:
            return None
    return None


def set_secret(name: str, value: str) -> None:
    """Persist to the OS keychain, falling back to the user-local secrets file."""
    if _keychain_set(name, value):
        return
    path = secrets_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception:
            data = {}
    data[name] = value
    path.write_text(json.dumps(data, indent=2), newline=NEWLINE)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def delete_secret(name: str) -> None:
    """Remove a stored secret, so a key can be cleared as well as set."""
    if _keychain_delete(name):
        return
    path = secrets_file()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except Exception:
        return
    if name not in data:
        return
    data.pop(name, None)
    path.write_text(json.dumps(data, indent=2), newline=NEWLINE)


def all_secrets() -> dict[str, str | None]:
    return {k: get_secret(k) for k in SECRET_KEYS}


# --------------------------------------------------------- model choice

@dataclass
class ModelChoice:
    """A model opencode can run, chosen from opencode's own catalogue."""

    provider: str
    model: str
    tier: str = "unknown"
    max_output_tokens: int = 4096

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}"


# Direct provider keys are a deliberate, paid commitment. If opencode reports
# one of these as available it is used ahead of everything else.
TIER_DIRECT: tuple[ModelChoice, ...] = (
    ModelChoice("anthropic", "claude-opus-4-20250514", "premium"),
    ModelChoice("openai", "gpt-5", "premium"),
    ModelChoice("anthropic", "claude-sonnet-4-20250514", "premium"),
)

# Strong OpenRouter models, tried after the free tier so a key with credits
# does not get downgraded unnecessarily.
OPENROUTER_PREFERRED: tuple[str, ...] = (
    "anthropic/claude-opus-4.5",
    "anthropic/claude-sonnet-4.5",
    "deepseek/deepseek-v4-pro",
    "qwen/qwen3.8-max",
    "z-ai/glm-5.3",
    "moonshotai/kimi-k3",
    "openai/gpt-4o-mini",
)

# Flash-tier models: cheap enough per token that a key with a small credit
# ceiling can still reserve a usable output budget.
OPENROUTER_CHEAP: tuple[str, ...] = (
    "deepseek/deepseek-v4.1-flash",
    "qwen/qwen3.8-flash",
    "z-ai/glm-5.3-flash",
    "moonshotai/kimi-k2.7-code",
    "stepfun/step-3.7-flash",
    "google/gemini-3.1-flash-lite",
    "openai/gpt-4o-mini",
    "google/gemini-2.5-flash-lite",
)

# Free-tier preference order. OpenRouter's free pool is small and churns, so
# this is intersected with what opencode reports. Nemotron leads: the large
# variants are the strongest free models currently offered.
FREE_PREFERENCE: tuple[str, ...] = (
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3.5-lightning:free",
    "thinkingmachines/inkling:free",
    "thinkingmachines/inkling-small:free",
    "dots-studio/dots-3-note-preview:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "apodex/apodex-1.1-mini:free",
    "poolside/laguna-s-2.1:free",
    "poolside/laguna-xs-2.1:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "cohere/north-mini-code:free",
    "liquid/lfm-2.5-2.6b:free",
)

DEFAULT_OUTPUT_BUDGET = 4096
MIN_OUTPUT_BUDGET = 1024

AFFORDABLE_RE = re.compile(r"can only afford ([\d,]+)", re.I)
# Only a token-budget refusal should change the cap. A rate limit mentioning
# "credits" must not be mistaken for one, or the budget shrinks on every retry.
RATE_LIMIT_WORDS = ("rate limit", "rate-limit", "too many requests", "requests per")
BUDGET_WORDS = ("max_tokens", "max tokens", "afford", "in-flight", "maximum context")


def budget_from_error(message: str, current: int) -> int:
    """Read the ceiling out of a provider refusal, or halve and retry.

    OpenRouter words this as "...you requested up to N tokens, but can only
    afford M", which names M directly. When it does not, halve.
    """
    if not message:
        return current
    low = message.lower()
    if any(w in low for w in RATE_LIMIT_WORDS):
        return current
    if not any(w in low for w in BUDGET_WORDS) and "credit" not in low:
        return current
    m = AFFORDABLE_RE.search(message)
    if m:
        try:
            return max(MIN_OUTPUT_BUDGET, int(m.group(1).replace(",", "")))
        except ValueError:
            pass
    return max(MIN_OUTPUT_BUDGET, current // 2)


def rank_free_models(available: list[str]) -> list[str]:
    """Order the live :free models by our preference for grading work."""
    live = {m for m in available if m.endswith(":free")}
    preferred = [m for m in FREE_PREFERENCE if m in live]
    return preferred + sorted(live.difference(FREE_PREFERENCE))


def candidates_from_catalogue(available: dict[str, list[str]],
                              prefer_free: bool = True) -> list[ModelChoice]:
    """Build the ordered candidate list from what opencode reports.

    Nothing here contacts a provider. `available` is the output of opencode's
    `/config/providers`, so a model that is not in it cannot be run with the
    credentials opencode already holds.
    """
    out: list[ModelChoice] = []
    routers = available.get("openrouter", [])
    seen: set[str] = set()

    def add(model: str, tier: str) -> None:
        if model in routers and model not in seen:
            seen.add(model)
            out.append(ModelChoice("openrouter", model, tier))

    for choice in TIER_DIRECT:
        if choice.model in available.get(choice.provider, []):
            out.append(choice)

    if routers and prefer_free:
        for m in rank_free_models(routers):
            add(m, "free")

    for m in OPENROUTER_CHEAP:
        add(m, "cheap")

    for m in OPENROUTER_PREFERRED:
        add(m, "standard")

    if routers and not prefer_free:
        for m in rank_free_models(routers):
            add(m, "free")

    return out


def pick_model(available: dict[str, list[str]], preferred: str | None = None,
               prefer_free: bool = True) -> ModelChoice | None:
    """Choose a model from opencode's catalogue. No network, no raw keys."""
    if preferred:
        provider, _, model = preferred.partition("/")
        if not model:
            provider, model = "openrouter", preferred
        if model in available.get(provider, []):
            return ModelChoice(provider, model, "explicit")
        for p, models in available.items():
            if model in models:
                return ModelChoice(p, model, "explicit")
        raise RuntimeError(
            f"{preferred} is not available with the credentials opencode has. "
            "Add a key with `opencode auth login`, or choose another model."
        )
    candidates = candidates_from_catalogue(available, prefer_free=prefer_free)
    return candidates[0] if candidates else None


# --------------------------------------------------------------- settings

def write_opencode_auth(api_key: str, data_home: Path) -> Path:
    """Write opencode's auth.json under an app-owned data directory.

    opencode reads credentials from ``<XDG_DATA_HOME>/opencode/auth.json``.
    Pointing XDG_DATA_HOME at a directory the app owns keeps the bundled
    opencode independent of whatever the user has configured for their own
    opencode install, which matters when the app ships its own binary.

    The environment variable OPENROUTER_API_KEY is set as well and is the
    primary route: it is honoured by opencode on every platform, whereas
    XDG_DATA_HOME handling on Windows is not something to bet a release on.
    """
    target = data_home / "opencode" / "auth.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if target.exists():
        try:
            existing = json.loads(target.read_text())
        except Exception:
            existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing["openrouter"] = {"type": "api", "key": api_key}
    target.write_text(json.dumps(existing, indent=2) + "\n")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


@dataclass
class Settings:
    canvas_base_url: str = ""
    canvas_api_token: str = ""
    # Held so it can be handed to opencode. The pipeline never talks to a model
    # provider itself; this exists only to configure the opencode process.
    openrouter_api_key: str = ""
    model: str | None = None
    workers: int = 3
    data: Path = field(default_factory=data_dir)
    workspace: Path = field(default_factory=workspace_dir)

    @classmethod
    def load(cls, model: str | None = None) -> "Settings":
        return cls(
            canvas_base_url=get_secret("canvas_base_url") or "",
            canvas_api_token=get_secret("canvas_api_token") or "",
            openrouter_api_key=get_secret("openrouter_api_key") or "",
            model=model,
            workers=int(os.environ.get("GRADING_PIPELINE_WORKERS", "3")),
        )

    @property
    def db_path(self) -> Path:
        return self.data / "pipeline.db"

    def run_dir(self, course_id: int, assignment_id: int) -> Path:
        return self.workspace / f"{course_id}-{assignment_id}"

    def check_canvas(self) -> None:
        missing = [k for k in ("canvas_base_url", "canvas_api_token") if not getattr(self, k)]
        if missing:
            raise RuntimeError(
                "Missing Canvas credentials: " + ", ".join(missing) +
                ". Set them in the app's Settings step."
            )