"""
The configuration as a file on disk: how it is written, how an edit
to it reaches a running bot, and which fields refuse to change.
"""

from __future__ import annotations

import dataclasses
import json
import os

from btc5m.config import Config
from btc5m.constants import LOG
from btc5m.profiles import DEFAULT_PROFILE, PROFILES
from btc5m.venue.endpoints import DEFAULT_ENDPOINTS

# Fields that cannot change while the bot is running. Swapping any of these
# mid-flight would leave the process in a state that does not match what it
# already did: a different journal would split one session's record across two
# files, a different key would sign with credentials the open position was not
# opened under. They are read once at startup and ignored on reload.
IMMUTABLE_FIELDS = frozenset({
    "api_key", "api_secret", "db_path", "endpoints", "profile_name",
})


# `live` is reloadable but NOT applied instantly. Switching mode while a
# position is open is incoherent in both directions: a paper position has no
# real order behind it, so live settlement would look for a venue position
# that never existed; and a real position flipped to paper stops being
# tracked while its settlement is simulated. The Trader therefore defers a
# mode change until it is flat, and resets the bankroll baseline on the swap.
DEFERRED_FIELDS = frozenset({"live"})


CONFIG_SCHEMA_NOTE = (
    "Every setting lives here. 'profiles' holds the named strategies, "
    "'active_profile' selects one, and 'overrides' is applied on top of it. "
    "The file is re-read whenever it changes on disk -- no restart needed. "
    "Fields listed in immutable_fields are fixed at startup."
)


def default_config_document(active_profile: str = DEFAULT_PROFILE) -> dict:
    """The full configuration as a plain document, ready to serialise."""
    base = {f.name: f.default for f in dataclasses.fields(Config)
            if f.default is not dataclasses.MISSING}
    # Every immutable field is excluded, not just the secrets. Emitting them
    # would make the file warn "these were ignored" on every single reload,
    # training the reader to ignore a warning that matters when it is real.
    for name in IMMUTABLE_FIELDS:
        base.pop(name, None)
    base = {k: (list(v) if isinstance(v, tuple) else v)
            for k, v in base.items()}
    return {
        "_note": CONFIG_SCHEMA_NOTE,
        "_immutable_fields": sorted(IMMUTABLE_FIELDS),
        "active_profile": active_profile,
        "defaults": base,
        "profiles": {name: dict(values) for name, values in PROFILES.items()},
        "overrides": {},
        "endpoints": {k: list(v) for k, v in DEFAULT_ENDPOINTS.items()},
    }


def _coerce(name: str, value: object) -> object:
    """Match a JSON value to the dataclass field's declared type."""
    hints = {f.name: f.type for f in dataclasses.fields(Config)}
    declared = str(hints.get(name, ""))
    if "tuple" in declared and isinstance(value, list):
        return tuple(tuple(x) if isinstance(x, list) else x for x in value)
    if "int" in declared and "float" not in declared and isinstance(value, float):
        if value != int(value):
            raise ValueError(f"{name} must be a whole number, got {value}")
        return int(value)
    return value


def build_config(document: dict, *, api_key: str, api_secret: str,
                 live: bool | None, db_path: str,
                 profile: str | None = None,
                 overrides: dict | None = None) -> Config:
    """
    Assemble a Config from a configuration document.

    Layering is explicit and one-directional: defaults, then the selected
    profile, then the document's overrides, then any command-line overrides.
    Everything passes through a single mapping so a key can never be supplied
    twice.
    """
    if not isinstance(document, dict):
        raise TypeError("configuration must be a JSON object")

    profiles = document.get("profiles") or {}
    name = profile or document.get("active_profile") or DEFAULT_PROFILE
    if name not in profiles:
        raise ValueError(
            f"unknown profile {name!r}; available: {sorted(profiles) or 'none'}")

    settings: dict = {}
    for layer in (document.get("defaults") or {}, profiles[name],
                  document.get("overrides") or {}, overrides or {}):
        for key, value in layer.items():
            if key.startswith("_"):
                continue
            settings[key] = value

    known = {f.name for f in dataclasses.fields(Config)}
    unknown = set(settings) - known
    if unknown:
        raise ValueError(f"unknown setting(s): {sorted(unknown)}")

    # A pinned value (CLI flag or environment) wins over the file and is not
    # hot-reloadable; without a pin the file governs and can be changed live.
    pinned_live = live is not None
    settings = {k: _coerce(k, v) for k, v in settings.items()
                if k not in IMMUTABLE_FIELDS}
    if pinned_live:
        settings["live"] = live
    elif "live" not in settings:
        settings["live"] = False

    endpoints = dict(DEFAULT_ENDPOINTS)
    for key, value in (document.get("endpoints") or {}).items():
        if key not in endpoints:
            raise ValueError(f"unknown endpoint {key!r}")
        if isinstance(value, str):
            endpoints[key] = (endpoints[key][0], value)
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            method = str(value[0]).upper()
            if method not in ("GET", "POST", "PUT", "DELETE"):
                raise ValueError(f"bad HTTP method for {key}: {value[0]}")
            endpoints[key] = (method, str(value[1]))
        else:
            raise ValueError(f"{key}: expected a path or [method, path]")

    settings.update(api_key=api_key, api_secret=api_secret,
                    db_path=db_path, profile_name=name,
                    endpoints=tuple(endpoints.items()))
    return Config(**settings)


class ConfigStore:
    """
    Holds the live configuration and reloads it when the file changes.

    Reload is atomic and fail-safe: the new document is parsed and a Config is
    fully constructed before anything is swapped, so a malformed or invalid
    file leaves the running bot on its last good configuration rather than
    crashing it mid-position. Immutable fields are ignored on reload and
    reported, so an edit that appears to take effect but cannot is visible
    rather than silent.
    """

    def __init__(self, path: str | None, *, api_key: str, api_secret: str,
                 live: bool | None, db_path: str,
                 profile: str | None = None,
                 overrides: dict | None = None) -> None:
        self._path = path
        self._identity = {"api_key": api_key, "api_secret": api_secret,
                              "live": live, "db_path": db_path, "profile": profile,
                              "overrides": overrides or {}}
        self._mtime: float | None = None
        self._document = self._read()
        self._current = build_config(self._document, **self._as_kwargs())
        self.reload_count = 0

    def _as_kwargs(self) -> dict:
        d = dict(self._identity)
        return {"api_key": d["api_key"], "api_secret": d["api_secret"],
                    "live": d["live"], "db_path": d["db_path"],
                    "profile": d["profile"], "overrides": d["overrides"]}

    def _read(self) -> dict:
        if not self._path:
            return default_config_document()
        with open(self._path, encoding="utf-8") as fh:
            document = json.load(fh)
        self._mtime = os.path.getmtime(self._path)
        return document

    @property
    def current(self) -> Config:
        return self._current

    @property
    def path(self) -> str | None:
        return self._path

    def changed_on_disk(self) -> bool:
        if not self._path:
            return False
        try:
            return os.path.getmtime(self._path) != self._mtime
        except OSError as exc:
            # Expected transiently: many editors replace a file rather than
            # writing in place, so it can vanish for an instant. Logged so a
            # permanently missing file is visible rather than looking like
            # "nothing changed" forever.
            LOG.debug("Config file not readable right now: %s", exc)
            return False

    def maybe_reload(self) -> bool:
        """
        Re-read the file if it changed. Returns True when the config swapped.

        Never raises: a bad edit is reported and the previous configuration
        stays in force.
        """
        if not self.changed_on_disk():
            return False
        try:
            document = self._read()
            candidate = build_config(document, **self._as_kwargs())
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            LOG.error("Config reload REJECTED, keeping the previous one: %s",
                      exc)
            return False

        ignored = self._ignored_edits(document)
        if ignored:
            LOG.warning("These settings cannot change while running and were "
                        "ignored: %s. Restart to apply them.",
                        ", ".join(sorted(ignored)))

        changes = self._diff(self._current, candidate)
        self._document, self._current = document, candidate
        self.reload_count += 1
        if changes:
            LOG.info("Config reload #%d (%d change(s)): %s",
                     self.reload_count, len(changes),
                     "; ".join(changes[:8]))
        else:
            LOG.info("Config file changed but no effective setting differed")
        return True

    @property
    def live_is_pinned(self) -> bool:
        """True when a CLI flag or environment variable fixed the mode."""
        return self._identity["live"] is not None

    def _ignored_edits(self, document: dict) -> set[str]:
        present: set[str] = set()
        for layer in (document.get("defaults") or {},
                      document.get("overrides") or {}):
            present |= set(layer)
        ignored = present & IMMUTABLE_FIELDS
        if "live" in present and self.live_is_pinned:
            ignored = ignored | {"live (pinned by --live/--paper or TRADING_MODE)"}
        return ignored

    @staticmethod
    def _diff(old: Config, new: Config) -> list[str]:
        out = []
        for f in dataclasses.fields(Config):
            if f.name in ("api_key", "api_secret", "endpoints"):
                continue
            a, b = getattr(old, f.name), getattr(new, f.name)
            if a != b:
                out.append(f"{f.name}: {a} -> {b}")
        return out
