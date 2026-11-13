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
from datetime import datetime, timezone
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


def runtime_home() -> Path:
    """Where the managed opencode runtime is kept.

    Its own directory rather than the data directory, because the runtime is
    a platform binary: the one a desktop app downloaded is not the one a
    container can run. Sharing the data directory would have the container
    pick up a binary built for a different operating system and refuse to
    start.
    """
    override = os.environ.get("GRADING_PIPELINE_RUNTIME_HOME", "").strip()
    return Path(override) if override else data_dir()


def secrets_file() -> Path:
    return data_dir() / "secrets.json"


def _opencode_data_homes() -> list[Path]:
    """Where opencode keeps its data: the environment, then the platform.

    opencode reads XDG_DATA_HOME, and falls back to the convention of the
    platform: ~/Library/Application Support on macOS, %APPDATA% on Windows,
    ~/.local/share everywhere else.
    """
    out: list[Path] = []
    override = os.environ.get("XDG_DATA_HOME", "").strip()
    if override:
        out.append(Path(override))
    if platform.system() == "Windows":
        out.append(Path(os.environ.get("APPDATA",
                                       Path.home() / "AppData" / "Roaming")))
    elif platform.system() == "Darwin":
        out.append(Path.home() / "Library" / "Application Support")
    else:
        out.append(Path.home() / ".local" / "share")
    return out


def opencode_data_home() -> Path:
    """The directory opencode keeps its data in."""
    return _opencode_data_homes()[0]


def opencode_home() -> Path:
    """Where the app's own opencode keeps its data.

    Its own store, always. Two opencode processes on one database is what a
    Windows machine with the opencode desktop app installed runs into: the
    second one cannot configure the database and opencode never starts, so
    there are no models to grade with at all. The accounts the person signed
    into are copied in rather than shared, which is all that is needed for
    the models those accounts reach.
    """
    override = os.environ.get("GRADING_PIPELINE_OPENCODE_HOME", "").strip()
    return Path(override) if override else (data_dir() / "opencode-home")


def opencode_stores() -> list[Path]:
    """The opencode stores on this machine, in the order they are looked at.

    opencode keeps its accounts in a store under its data directory: the one
    the environment points at, and the one the platform's own convention
    gives. Both are worth looking at, because a person who has pointed
    XDG_DATA_HOME somewhere for one tool and uses opencode for another ends
    up with the accounts in the place opencode itself would not look.
    """
    out: list[Path] = []
    for base in _opencode_data_homes():
        store = base / "opencode"
        if store not in out:
            out.append(store)
    return out


def opencode_store() -> Path | None:
    """The opencode store of the person using the app, when they have one.

    The accounts someone signed into live in that store — a Go subscription,
    GitHub Copilot — and with them the models they pay for.
    """
    for store in opencode_stores():
        if (store / "opencode.db").exists() or (store / "auth.json").exists():
            return store
    return None


@dataclass
class Account:
    """One account in an opencode store.

    opencode keeps its accounts in a database, and reads them from there; an
    auth file beside it is what it falls back to when there is no database
    yet. Both are copied, because the app's own store may be either.
    """

    provider: str
    payload: dict[str, Any]
    label: str = ""
    row_id: str = ""
    connector_id: str | None = None
    method_id: str | None = None
    active: bool | None = None
    time_created: int = 0
    time_updated: int = 0


def _read_store_accounts(store: Path) -> list[Account]:
    """The accounts in an opencode store: its database and its auth file.

    The database is read first and wins: it is where opencode refreshes a
    token, so it holds the newer of the two. A store that cannot be read —
    because another opencode has it open, or because a newer version wrote
    it — contributes what it can rather than nothing.
    """
    accounts: dict[str, Account] = {}
    for row in _read_credential_rows(store / "opencode.db"):
        accounts[row.provider] = row
    auth = store / "auth.json"
    if auth.exists():
        try:
            data = json.loads(auth.read_text())
            if isinstance(data, dict):
                for name, payload in data.items():
                    if not isinstance(payload, dict):
                        continue
                    accounts.setdefault(str(name), Account(provider=str(name),
                                                           payload=payload))
        except Exception:
            pass
    return list(accounts.values())


def _read_credential_rows(db: Path) -> list[Account]:
    """The credential rows of an opencode database.

    Read-only where possible. A store another opencode is using is still
    worth reading: the rows are what the app needs, and a database that
    cannot be opened at all simply yields nothing.
    """
    import sqlite3

    if not db.exists():
        return []
    for readonly in (True, False):
        con = None
        try:
            if readonly:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            else:
                con = sqlite3.connect(str(db), timeout=3)
            rows = con.execute(
                "SELECT id, integration_id, label, value, connector_id, method_id,"
                " active, time_created, time_updated FROM credential").fetchall()
            out: list[Account] = []
            for (row_id, provider, label, value, connector_id, method_id,
                 active, created, updated) in rows:
                try:
                    payload = json.loads(value)
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue
                out.append(Account(provider=str(provider), payload=payload,
                                    label=str(label or ""), row_id=str(row_id or ""),
                                    connector_id=connector_id, method_id=method_id,
                                    active=active, time_created=int(created or 0),
                                    time_updated=int(updated or 0)))
            return out
        except Exception:
            continue
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass
    return []


def _write_accounts_to_db(db: Path, accounts: list[Account]) -> list[str]:
    """Put the accounts into an opencode database.

    The database is what opencode reads, so this is the copy that counts.
    The auth file is not enough once the store has a database of its own.
    """
    import secrets
    import sqlite3

    if not db.exists():
        return []
    con = None
    try:
        con = sqlite3.connect(str(db), timeout=5)
        written: list[str] = []
        for account in accounts:
            row = con.execute(
                "SELECT id FROM credential WHERE integration_id=?",
                (account.provider,)).fetchone()
            row_id = row[0] if row else (
                account.row_id or "cred_" + secrets.token_hex(12))
            con.execute(
                "INSERT INTO credential (id, integration_id, label, value,"
                " connector_id, method_id, active, time_created, time_updated)"
                " VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET"
                " integration_id=excluded.integration_id, label=excluded.label,"
                " value=excluded.value, connector_id=excluded.connector_id,"
                " method_id=excluded.method_id, active=excluded.active,"
                " time_updated=excluded.time_updated",
                (row_id, account.provider, account.label,
                 json.dumps(account.payload), account.connector_id,
                 account.method_id, account.active, account.time_created,
                 account.time_updated or _now_ms()))
            written.append(account.provider)
        con.commit()
        return written
    except Exception:
        return []
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def copy_opencode_credentials(source: Path, home: Path) -> list[str]:
    """Copy the accounts from one opencode store into another.

    The app keeps a store of its own so that it never opens the database
    the person's own opencode is using: on Windows the second one cannot
    configure it and opencode never starts, so there are no models to grade
    with at all. The accounts are what matter: the models they reach. Their
    sessions are not copied, and are not wanted.

    The person's store is the source of truth, so what is copied replaces
    what was there: a token that was refreshed elsewhere is picked up here
    rather than used from a stale copy. The expiry comes with it, so the
    app does not race the person's own opencode to refresh it.
    """
    accounts = _read_store_accounts(source)
    if not accounts:
        return []
    store = home / "opencode"
    store.mkdir(parents=True, exist_ok=True)
    written = _write_accounts_to_db(store / "opencode.db", accounts)
    written = _write_accounts_to_auth(store / "auth.json", accounts)
    return written


def _write_accounts_to_auth(target: Path, accounts: list[Account]) -> list[str]:
    """Put the accounts into an opencode auth file.

    opencode reads the file when the store has no database yet, which is
    the first time the app runs. The database is the copy that counts after
    that; this one is what makes the first run work.
    """
    existing: dict[str, Any] = {}
    if target.exists():
        try:
            data = json.loads(target.read_text())
            if isinstance(data, dict):
                existing = {k: v for k, v in data.items() if isinstance(v, dict)}
        except Exception:
            existing = {}
    written: list[str] = []
    for account in accounts:
        if existing.get(account.provider) == account.payload:
            continue
        existing[account.provider] = account.payload
        written.append(account.provider)
    if written:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(existing, indent=2) + "\n")
        try:
            target.chmod(0o600)
        except OSError:
            pass
    return written


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


def _stored_secret(name: str) -> str | None:
    """The secret as saved by the app: keychain first, then the secrets file."""
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


def get_secret(name: str) -> str | None:
    env = os.environ.get(name.upper())
    if env:
        return env
    return _stored_secret(name)


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


# How many tokens the grader is allowed to spend on one turn.
DEFAULT_OUTPUT_BUDGET = 4096
MIN_OUTPUT_BUDGET = 1024


@dataclass(frozen=True)
class ModelFacts:
    """What opencode reports about one model it can run.

    The catalogue is whatever the user's credentials reach, so the pipeline
    reads it rather than carrying a list of model names that goes stale. The
    facts are the ones a choice needs: what it costs, how much it can hold,
    and whether it can call a tool at all.
    """

    provider: str
    model: str
    name: str = ""
    input_price: float | None = None      # per million tokens
    output_price: float | None = None
    context: int = 0
    output_limit: int = 0
    tools: bool = False
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    released: int = 0
    family: str = ""

    @property
    def id(self) -> str:
        return f"{self.provider}/{self.model}"

    @property
    def free(self) -> bool:
        return self.output_price == 0

    @property
    def alias(self) -> bool:
        """Whether this entry points at another model rather than being one.

        opencode names a rolling pointer to a model with a tilde
        ("~openai/gpt-sol-latest"). It is the same model under another name,
        so it has no place at the front of a list of models to choose from.
        """
        return self.model.startswith("~")


def model_facts(entry: dict[str, Any]) -> ModelFacts | None:
    """Read one entry of opencode's model catalogue.

    Returns None for an entry that names no provider or no model: a model
    that cannot be named cannot be run.
    """
    provider = str(entry.get("providerID") or "")
    model = str(entry.get("modelID") or entry.get("id") or "")
    if not provider or not model:
        return None
    # Some models are named with their provider already on the front
    # ("openrouter/auto"), and the id is provider plus model, so the prefix
    # would otherwise be doubled.
    prefix = f"{provider}/"
    if model.startswith(prefix):
        model = model[len(prefix):]
    if not model:
        return None
    caps = entry.get("capabilities") or {}
    limit = entry.get("limit") or {}
    # The cost is one entry per variant of the model (a reasoning effort, a
    # provider variant); the cheapest one is what it costs to run it.
    costs = [c for c in (entry.get("cost") or []) if isinstance(c, dict)]
    outs = [c.get("output") for c in costs
            if isinstance(c.get("output"), (int, float))]
    ins = [c.get("input") for c in costs
           if isinstance(c.get("input"), (int, float))]
    released = (entry.get("time") or {}).get("released") or 0
    family = str(entry.get("family") or "")
    # A router is not a free model. The catalogue prices the router itself at
    # nothing because the router chooses the model, and that model's price is
    # what is actually paid. Its price is therefore unknown, and it does not
    # belong in the free tier.
    router = family == "auto" or model.endswith("/auto")
    return ModelFacts(
        provider=provider,
        model=model,
        name=str(entry.get("name") or model),
        input_price=None if router else (min(ins) if ins else None),
        output_price=None if router else (min(outs) if outs else None),
        context=int(limit.get("context") or 0),
        output_limit=int(limit.get("output") or 0),
        tools=bool(caps.get("tools")),
        inputs=tuple(caps.get("input") or ()),
        outputs=tuple(caps.get("output") or ()),
        released=int(released),
        family=family,
    )


# What a model costs, per million output tokens, and what that makes it.
# Bands rather than model names: the catalogue changes under us, a price does
# not.
TIER_ORDER: tuple[str, ...] = ("free", "other", "cheap", "standard", "premium")
TIER_PREMIUM_FROM = 5.0
TIER_STANDARD_FROM = 1.0


def tier_of(facts: ModelFacts) -> str:
    """Which price band a model falls in.

    A model with no price listed is one the provider does not publish a
    price for, which is not the same as it being free.
    """
    if facts.output_price is None:
        return "other"
    if facts.output_price <= 0:
        return "free"
    if facts.output_price >= TIER_PREMIUM_FROM:
        return "premium"
    if facts.output_price >= TIER_STANDARD_FROM:
        return "standard"
    return "cheap"


def can_grade(facts: ModelFacts, budget: int) -> bool:
    """Whether this model can actually do the grading.

    The grader hands the scorecard over with a tool call, so a model without
    tool support cannot finish at all — it reads the work and then stops, and
    the row lands in needs_review with nothing to show for it. A model whose
    output ceiling is below the budget would be cut off part-way through
    writing the scorecard, which is the same failure wearing a different hat.
    """
    return (facts.tools
            and "text" in facts.outputs
            and facts.output_limit >= budget)


def rank_models(facts: list[ModelFacts], budget: int = DEFAULT_OUTPUT_BUDGET,
                prefer_free: bool = True) -> list[ModelFacts]:
    """Order the models the grader can use.

    Cheap first, because a grading run spends money and the free models are
    good enough for most of it. Within a price band the model that can
    actually read what a student submitted comes first — a PDF, a
    screenshot, not just text — then the newest generation, then the more
    expensive variant of that generation (the pro rather than its cut-down
    sibling), then the biggest context. A rolling pointer to another model
    ("~openai/gpt-sol-latest") is the same model under another name, so it
    waits at the back of its band rather than taking a place of its own.

    The order is a pure function of the catalogue, so it does not shuffle
    between refreshes.
    """
    usable = [f for f in facts if can_grade(f, budget)]

    def key(f: ModelFacts) -> tuple[Any, ...]:
        tier = tier_of(f)
        order = TIER_ORDER.index(tier)
        if not prefer_free and tier == "free":
            order = len(TIER_ORDER)
        rich = sum(1 for kind in ("pdf", "image") if kind in f.inputs)
        return (order, 1 if f.alias else 0, -rich, -f.released,
                -(f.output_price or 0.0), -f.context, f.model)

    usable.sort(key=key)
    return usable


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




def candidates_from_catalogue(available: list[ModelFacts],
                              prefer_free: bool = True,
                              budget: int = DEFAULT_OUTPUT_BUDGET) -> list[ModelChoice]:
    """The ordered candidate list, built from what opencode reports.

    Nothing here contacts a provider. `available` is what opencode's model
    catalogue holds, so a model that is not in it cannot be run with the
    credentials opencode already holds.

    The list used to come from a fixed list of model names — a handful of
    direct keys and a few OpenRouter tiers. That list went stale the moment
    the catalogue did, and it could not see a model the user's own
    subscription reached. The tiers are now price bands and the order is
    worked out from the facts.
    """
    return [ModelChoice(f.provider, f.model, tier_of(f), budget)
            for f in rank_models(available, budget=budget, prefer_free=prefer_free)]


def pick_model(available: list[ModelFacts], preferred: str | None = None,
               prefer_free: bool = True,
               budget: int = DEFAULT_OUTPUT_BUDGET) -> ModelChoice | None:
    """Choose a model from opencode's catalogue. No network, no raw keys."""
    if preferred:
        provider, _, model = preferred.partition("/")
        if not model:
            provider, model = "openrouter", preferred
        for f in available:
            if f.provider == provider and f.model == model:
                return ModelChoice(provider, model, "explicit", budget)
        # The same model under another provider is still the model they meant.
        for f in available:
            if f.model == model:
                return ModelChoice(f.provider, f.model, "explicit", budget)
        raise RuntimeError(
            f"{preferred} is not available with the credentials opencode has. "
            "Add a key with `opencode auth login`, or choose another model."
        )
    candidates = candidates_from_catalogue(available, prefer_free=prefer_free,
                                          budget=budget)
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
        # A stored secret wins over the environment. The environment is the
        # headless/Docker route (no Settings step to save through), but when
        # both exist the app's own setting must prevail, otherwise a key
        # exported in the shell (OPENROUTER_API_KEY is very commonly set)
        # silently shadows whatever the user saves or removes in the UI and
        # "Replace or remove key" can never take effect.
        def pick(name: str, *env_names: str) -> str:
            stored = _stored_secret(name)
            if stored:
                return stored
            for env_name in env_names:
                value = os.environ.get(env_name, "").strip()
                if value:
                    return value
            return ""

        return cls(
            canvas_base_url=pick(
                "canvas_base_url",
                "GRADING_PIPELINE_CANVAS_BASE_URL", "CANVAS_BASE_URL",
            ).rstrip("/"),
            canvas_api_token=pick(
                "canvas_api_token",
                "GRADING_PIPELINE_CANVAS_API_TOKEN", "CANVAS_API_TOKEN",
            ),
            openrouter_api_key=pick(
                "openrouter_api_key",
                "OPENROUTER_API_KEY", "GRADING_PIPELINE_OPENROUTER_API_KEY",
            ),
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
            hint = {
                "canvas_base_url": "GRADING_PIPELINE_CANVAS_BASE_URL",
                "canvas_api_token": "GRADING_PIPELINE_CANVAS_API_TOKEN",
            }
            expected = ", ".join(hint[k] for k in missing)
            raise RuntimeError(
                "Missing Canvas credentials: " + ", ".join(missing) +
                f". Set {expected} in the environment, or set them in the "
                "app's Settings step."
            )