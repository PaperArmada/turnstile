"""Starter upgrade planning for `turnstile update` (GH #47).

`turnstile init` copies the bundled starter definitions into .processes/ and
records what it wrote in a manifest (.processes/.starters.lock). `turnstile
update` compares the local copies with a source bundle and replaces only the
copies that are unedited: their content hash matches the manifest or a
released version of that starter. Kept unless forced: edited copies, copies
newer than the source, new starters whose filename another file already
uses, new starters an unreadable extends source may provide, and writes that
would leave an active instance without its definition or state. Before
anything is written, the loader is run in a scratch directory over the
files as they would be after the update, registry edit included; a write
that would leave the definitions unloadable is held back.

The functions here are pure apart from reading and writing files under the
project root, and fetch_git_sources, which fetches git extends sources into
the loader's cache as the loader does; the click command in main.py renders
the plan.
"""

from __future__ import annotations

import difflib
import hashlib
import itertools
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from turnstile_core.loader import _source_label, discover_definitions_full
from turnstile_core.models import ProcessDefinition, RegistryConfig, RegistryExtend
from turnstile_core.persistence import ProcessInstance
from turnstile_core.registry import (
    SourceType,
    cached_git_source,
    parse_source,
    resolve_source,
)

from turnstile_cli.starter_history import RELEASED_STARTER_HASHES

MANIFEST_NAME = ".starters.lock"
MANIFEST_FORMAT = 1
_MANIFEST_COMMENT = (
    "Written by turnstile init and update. Records the starter content "
    "installed in this directory so `turnstile update` can tell edited "
    "copies from unedited ones. Commit this file."
)

# Plan statuses. Only APPLY and NEW write files.
UP_TO_DATE = "up-to-date"
APPLY = "update"
NEW = "new"
EDITED = "edited"
LOCAL_NEWER = "local-newer"
REMOVED = "removed"
BLOCKED = "blocked"
CONFLICT = "conflict"
UNVERIFIED = "unverified"
EXTERNAL = "external"
LOCAL_ONLY = "local-only"
WRITES = (APPLY, NEW)
KEPT = (EDITED, LOCAL_NEWER, BLOCKED, CONFLICT, UNVERIFIED)


def content_hash(text: str) -> str:
    """sha256 of a definition's text, with CRLF normalised to LF.

    Local files are read in text mode, which already turns a Windows
    checkout's CRLF into LF; the replace keeps strings from other sources
    (scripts/gen_starters.py reads release blobs) hashing the same way.
    """
    return hashlib.sha256(text.replace("\r\n", "\n").encode()).hexdigest()


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def load_manifest(proc_dir: Path) -> tuple[dict[str, dict[str, str]], str]:
    """Return (entries, warning). A missing manifest is empty; an unreadable
    one is treated as empty with a warning, since released hashes still
    identify unedited starters."""
    path = proc_dir / MANIFEST_NAME
    if not path.exists():
        return {}, ""
    try:
        data = json.loads(path.read_text())
        files = data["files"]
        entries = {
            name: {"release": str(e["release"]), "sha256": str(e["sha256"])}
            for name, e in files.items()
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        return {}, f"{MANIFEST_NAME} is unreadable ({e}); ignoring it"
    return entries, ""


def _manifest_text(entries: dict[str, dict[str, str]]) -> str:
    data = {
        "_comment": _MANIFEST_COMMENT,
        "format": MANIFEST_FORMAT,
        "files": {name: entries[name] for name in sorted(entries)},
    }
    return json.dumps(data, indent=2) + "\n"


def write_manifest(proc_dir: Path, entries: dict[str, dict[str, str]]) -> None:
    (proc_dir / MANIFEST_NAME).write_text(_manifest_text(entries))


def record_installed(
    proc_dir: Path, bundle: dict[str, str], release: str
) -> int:
    """Record every bundled starter whose local copy equals the bundle.

    Used by `init`: files it wrote and pre-existing identical files are
    recorded; differing files are not, so they count as edited until
    proven otherwise. Existing entries for other files are kept. Returns
    the number of entries added or changed.
    """
    entries, _ = load_manifest(proc_dir)
    recorded = 0
    for filename, content in bundle.items():
        target = proc_dir / filename
        digest = content_hash(content)
        if (
            target.exists()
            and target.read_text() == content
            and entries.get(filename, {}).get("sha256") != digest
        ):
            entries[filename] = {"release": release, "sha256": digest}
            recorded += 1
    if recorded:
        write_manifest(proc_dir, entries)
    return recorded


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass
class PlanEntry:
    name: str
    status: str
    filename: str
    local_version: str = "-"
    source_version: str = "-"
    local_path: Path | None = None
    source_content: str = ""
    detail: str = ""
    forced: bool = False
    # Instances that run this definition (or one extending it) and keep
    # their state after the update.
    affected: list[str] = field(default_factory=list)
    # Instances a forced write leaves without their definition or state.
    stranded: list[str] = field(default_factory=list)
    # The write installs a file for a definition no local file declared, so
    # its stem is added to a non-empty `local:` list.
    register: bool = False


@dataclass
class Plan:
    entries: list[PlanEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Set when --apply must not write: the result cannot be loaded or
    # cannot be checked.
    unsafe: str = ""
    # Set when this preview could not check the plan but --apply can (it
    # fetches git sources first).
    unchecked: str = ""
    # Set by apply_plan(register=True).
    registered: list[str] = field(default_factory=list)
    register_warning: str = ""

    @property
    def writes(self) -> list[PlanEntry]:
        return [e for e in self.entries if e.status in WRITES]

    @property
    def skipped(self) -> list[PlanEntry]:
        return [e for e in self.entries if e.status in KEPT]


def _version_key(version: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return None


def _is_newer(a: str, b: str) -> bool:
    """True when version a is strictly newer than b; unparsable is never."""
    ka, kb = _version_key(a), _version_key(b)
    return ka is not None and kb is not None and ka > kb


def _parse(content: str) -> dict | None:
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _is_override(data: dict) -> bool:
    return "extends" in data and "overrides" in data


def _registry_local(proc_dir: Path) -> list[str]:
    """The registry's `local:` stems, or [] when absent or unreadable."""
    try:
        data = _parse((proc_dir / "registry.yaml").read_text()) or {}
    except (OSError, ValueError):
        return []
    listed = data.get("local") or []
    return [str(n) for n in listed] if isinstance(listed, list) else []


def _read(path: Path) -> str | None:
    """A file's text, or None when it is not a readable UTF-8 file."""
    try:
        return path.read_text() if path.is_file() else None
    except (OSError, ValueError):
        return None


def _snapshot(proc_dir: Path) -> tuple[dict[str, str], list[str]]:
    """The files the loader reads under .processes/, as (relative path ->
    text, relative paths it reads but that are not readable UTF-8 files).

    The loader reads overrides/*.yaml, and either the files the registry's
    `local:` list names or, without such a list, every top-level *.yaml.
    Other top-level files are included too (unread, harmless) so the plan
    can compare starters the list leaves out.
    """
    listed = _registry_local(proc_dir)
    root = proc_dir.resolve()
    read_by_loader = list(proc_dir.glob("overrides/*.yaml")) + [proc_dir / "registry.yaml"]
    if listed:
        for stem in listed:
            path = proc_dir / f"{stem}.yaml"
            if path.resolve().is_relative_to(root):
                read_by_loader.append(path)
    else:
        read_by_loader += proc_dir.glob("*.yaml")
    files: dict[str, str] = {}
    unreadable: list[str] = []
    for path in sorted(set(read_by_loader) | set(proc_dir.glob("*.yaml"))):
        rel = path.relative_to(proc_dir).as_posix()
        text = _read(path)
        if text is not None:
            files[rel] = text
        elif path in read_by_loader and (path.exists() or path.is_symlink()):
            unreadable.append(rel)
    return files, unreadable


def _registry_data(files: dict[str, str]) -> tuple[dict | None, str]:
    """registry.yaml as the loader reads it: absent or empty is the
    default (an empty mapping)."""
    text = files.get("registry.yaml")
    if text is None:
        return {}, ""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return None, f"registry.yaml does not parse: {e}"
    if raw is None:
        return {}, ""
    if not isinstance(raw, dict):
        return None, "registry.yaml is not a mapping"
    try:
        RegistryConfig(**raw)
    except Exception as e:  # noqa: BLE001 - pydantic or type errors
        return None, f"invalid registry.yaml: {e}"
    return dict(raw), ""


@dataclass
class _Source:
    """A registry extends entry as update can read it, without fetching."""

    entry: dict
    label: str  # the loader's label for the entry
    is_git: bool
    directory: Path | None  # None when it cannot be read here
    reason: str = ""  # why it cannot be read


def _cache_dir(registry: dict, project_root: Path) -> Path:
    settings = registry.get("settings")
    state_dir = (
        settings.get("state_dir", ".process-state")
        if isinstance(settings, dict) else ".process-state"
    )
    return project_root / str(state_dir) / ".cache"


def _extends_entries(registry: dict) -> list[dict]:
    extends = registry.get("extends")
    return [
        e for e in (extends if isinstance(extends, list) else [])
        if isinstance(e, dict) and isinstance(e.get("source"), str)
    ]


def _offline_sources(registry: dict, project_root: Path) -> list[_Source]:
    """Resolve the registry's extends entries to local directories with no
    git or network I/O: local paths as given, Python packages this CLI can
    import, and git sources from the loader's cache as of its last fetch."""
    cache_dir = _cache_dir(registry, project_root)
    sources: list[_Source] = []
    for e in _extends_entries(registry):
        source_type, location, _ = parse_source(e["source"])
        source = _Source(
            entry=e,
            label=_source_label(e["source"], source_type),
            is_git=source_type == SourceType.git,
            directory=None,
        )
        if source.is_git:
            source.directory = cached_git_source(location, cache_dir)
            source.reason = "" if source.directory else "not fetched yet"
        else:
            try:
                source.directory, _ = resolve_source(
                    RegistryExtend(source=e["source"]), project_root, cache_dir
                )
            except Exception:  # noqa: BLE001 - import errors of any kind
                source.reason = (
                    "not importable by this CLI; install it alongside the "
                    "CLI (for example with `uvx --with`)"
                    if source_type == SourceType.python else "not found"
                )
        sources.append(source)
    return sources


def fetch_git_sources(proc_dir: Path, on_fetch=None) -> list[tuple[str, str]]:
    """Fetch the registry's git extends sources into the loader's cache, as
    any command that loads the definitions does, calling on_fetch(source)
    before each. Returns (source, error) for each that failed, with any
    credentials in the URL removed."""
    registry, _ = _registry_data(_snapshot(proc_dir)[0])
    project_root = proc_dir.parent
    failed: list[tuple[str, str]] = []
    for e in _extends_entries(registry or {}):
        if parse_source(e["source"])[0] != SourceType.git:
            continue
        if on_fetch:
            on_fetch(e["source"])
        try:
            resolve_source(
                RegistryExtend(source=e["source"]),
                project_root,
                _cache_dir(registry or {}, project_root),
            )
        except Exception as exc:  # noqa: BLE001 - reported to the user
            failed.append((redact(e["source"]), _scrub(str(exc), e["source"])))
    return failed


def _scrub(text: str, source: str) -> str:
    """Remove a source URL's user:password part from text, also in the form
    the loader's cache directory name gives it (git errors print it)."""
    text = redact(text)
    match = re.search(r"://([^/@\s]+)@", source)
    if match:
        info = match.group(1)
        for form in (info, info.replace("/", "_").replace(".", "_")):
            text = text.replace(form + "@", "").replace(form, "***")
    return text


@dataclass
class _Discovery:
    """What the loader makes of a file set. `files` maps each definition
    name to the .processes-relative file it came from ("" when it came from
    an extends source)."""

    definitions: dict[str, ProcessDefinition]
    sources: dict[str, str]
    files: dict[str, str]


def _discover(
    files: dict[str, str], project_root: Path, register: list[str] | None = None
) -> tuple[_Discovery | None, str]:
    """Run the loader over a file set in a scratch project.

    Extends sources are read by _offline_sources, so discovery does no
    network I/O and writes nothing outside the scratch directory; a source
    it cannot read fails discovery. `register` names stems `update` will
    try to add to the registry's `local:` list; the edit register_new would
    make is applied first.
    """
    if register:
        updated, _, _ = _registry_edit(files.get("registry.yaml"), register)
        if updated is not None:
            files = {**files, "registry.yaml": updated}
    registry, error = _registry_data(files)
    if registry is None:
        return None, error
    sources = _offline_sources(registry, project_root)
    for source in sources:
        if source.reason:
            return None, f"extends source {redact(source.entry['source'])}: {source.reason}"
    # Listed files outside .processes/ are read where they are, as the
    # loader reads them (update never writes them).
    real_proc = (project_root / ".processes").resolve()
    if isinstance(registry.get("local"), list):
        registry["local"] = [
            str((real_proc / f"{n}.yaml").resolve().with_suffix(""))
            if not (real_proc / f"{n}.yaml").resolve().is_relative_to(real_proc)
            else n
            for n in registry["local"]
        ]
    settings = registry.get("settings") if isinstance(registry.get("settings"), dict) else {}
    registry["settings"] = {**settings, "state_dir": ".state"}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        proc = root / ".processes"
        proc.mkdir()
        for rel, text in files.items():
            target = proc / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        # Each source becomes a flat directory of links to the *.yaml files
        # the loader would read, so the scratch loader cannot descend into a
        # processes/ subdirectory a second time. The loader keys sources by
        # their string (the last entry for a string wins).
        labels: dict[str, str] = {}
        registry["extends"] = []
        by_string = {s.entry["source"]: s for s in sources}
        for k, source in enumerate(by_string.values()):
            link_dir = root / f"source-{k}"
            link_dir.mkdir()
            for path in source.directory.glob("*.yaml"):
                (link_dir / path.name).symlink_to(path.resolve())
            registry["extends"].append({**source.entry, "source": str(link_dir)})
            labels[_source_label(str(link_dir), SourceType.local)] = source.label
        (proc / "registry.yaml").write_text(yaml.safe_dump(registry))
        quiet = logging.getLogger("turnstile_core")
        level = quiet.level
        quiet.setLevel(logging.CRITICAL)
        try:
            found = discover_definitions_full(root)
        except Exception as e:
            return None, str(e).replace(str(proc) + "/", "")
        finally:
            quiet.setLevel(level)
        rel_files: dict[str, str] = {}
        for name, d in found.items():
            try:
                rel_files[name] = Path(d.source_path).relative_to(proc).as_posix()
            except ValueError:
                rel_files[name] = ""
        return _Discovery(
            definitions={n: d.definition for n, d in found.items()},
            sources={n: labels.get(d.source, d.source) for n, d in found.items()},
            files=rel_files,
        ), ""


def _fallback_local(
    proc_dir: Path,
) -> tuple[dict[tuple[str, bool], Path], dict[tuple[str, bool], list[Path]]]:
    """(name, is override) -> top-level file declaring that name, chosen
    like the loader: the last registry-listed stem, else the last in sort
    order. Used when the loader cannot say (the files do not load, or the
    name it reports comes from a file of the other kind)."""
    listed = _registry_local(proc_dir)
    candidates: dict[tuple[str, bool], list[Path]] = {}
    for path in sorted(proc_dir.glob("*.yaml")):
        data = _parse(_read(path) or "")
        if path.name != "registry.yaml" and data and isinstance(data.get("name"), str):
            candidates.setdefault((data["name"], _is_override(data)), []).append(path)
    chosen: dict[tuple[str, bool], Path] = {}
    for key, paths in candidates.items():
        ranked = [p for stem in listed for p in paths if p.stem == stem]
        chosen[key] = (ranked or paths)[-1]
    return chosen, candidates


def _not_writable(path: Path, proc_dir: Path) -> bool:
    """The path is a symbolic link, resolves outside .processes/ (such as
    through a linked overrides/ directory), or is a file this user may not
    write."""
    return (
        path.is_symlink()
        or not path.resolve().is_relative_to(proc_dir.resolve())
        or (path.exists() and not os.access(path, os.W_OK))
    )


def _local_file(
    name: str, override: bool, proc_dir: Path, files: dict[str, str],
    before: _Discovery | None, fallback: dict[tuple[str, bool], Path],
) -> Path | None:
    """The local file of the same kind (override or not) that defines
    `name`, preferring the one the loader reports."""
    rel = before.files.get(name, "") if before is not None else ""
    if rel and _is_override(_parse(files.get(rel, "")) or {}) == override:
        return proc_dir / rel
    return fallback.get((name, override))


def _ancestry(name: str, discovery: _Discovery, files: dict[str, str]) -> set[str]:
    """`name` and every definition it inherits from, per the loader."""
    chain: set[str] = set()
    while name and name not in chain:
        chain.add(name)
        data = _parse(files.get(discovery.files.get(name, ""), "")) or {}
        name = (
            str(data["extends"]).rsplit("/", 1)[-1] if _is_override(data) else ""
        )
    return chain


def _state_ids(defn: ProcessDefinition | None) -> set[str]:
    return {s.id for s in defn.states} if defn else set()


def plan_update(
    proc_dir: Path,
    bundle: dict[str, str],
    instances: list[ProcessInstance],
    force: frozenset[str] = frozenset(),
    fetched: bool = False,
) -> Plan:
    """Decide what `update --apply` would do for each definition.

    `bundle` maps filename -> content for the source. `fetched` says git
    extends sources were just fetched (otherwise the preview notes that it
    read them as of their last fetch). `force` names
    definitions whose kept status (edited, local-newer, blocked, conflict,
    unverified), removed status, or extended-source status is overridden.
    The plan is then checked by _check_after_apply.
    """
    plan = Plan()
    manifest, warning = load_manifest(proc_dir)
    if warning:
        plan.warnings.append(warning)
    project_root = proc_dir.parent
    files, unreadable_files = _snapshot(proc_dir)
    registry, _ = _registry_data(files)
    sources = _offline_sources(registry or {}, project_root)
    unread = [s for s in sources if s.reason]
    may_provide = [f"{redact(s.entry['source'])} ({s.reason})" for s in unread]
    stale = [redact(s.entry["source"]) for s in sources if s.is_git and not s.reason]
    if stale and not fetched:
        plan.warnings.append(
            f"checked against {', '.join(stale)} as of the last fetch; "
            "--apply fetches and checks again"
        )
    # The check runs only when the loader's whole input can be read here.
    before, before_error = (None, "") if unread or unreadable_files else _discover(
        files, project_root
    )
    if before is None and before_error:
        plan.warnings.append(
            f"the current definitions do not load ({before_error}); "
            "writes are applied only if the result loads"
        )
    fallback, candidates = _fallback_local(proc_dir)

    source: dict[str, tuple[str, str, dict]] = {}  # name -> (file, text, data)
    for filename in sorted(bundle):
        data = _parse(bundle[filename])
        if not data or not isinstance(data.get("name"), str):
            plan.warnings.append(f"source {filename} is not a definition; skipped")
            continue
        source[data["name"]] = (filename, bundle[filename], data)

    seen_paths: set[Path] = set()
    for name, (filename, content, data) in source.items():
        entry = PlanEntry(
            name=name,
            status=UP_TO_DATE,
            filename=filename,
            source_version=str(data.get("version", "?")),
            source_content=content,
        )
        plan.entries.append(entry)
        forced = name in force
        path = _local_file(name, _is_override(data), proc_dir, files, before, fallback)

        if path is None:
            target = proc_dir / filename
            entry.register = True
            if target.exists() or target.is_symlink():
                entry.status = CONFLICT
                entry.local_path = target
                if _not_writable(target, proc_dir) or not target.is_file():
                    entry.detail = f"{filename} exists and is not a regular file"
                    continue  # never written, even when forced
                entry.detail = f"{filename} exists but does not define '{name}'"
            elif filename in manifest:
                entry.status = REMOVED
                entry.detail = "deleted locally; not reinstalled"
            elif before is not None and name in before.sources and not before.files.get(name):
                entry.status = EXTERNAL
                entry.detail = f"provided by {redact(before.sources[name])}"
            elif may_provide:
                entry.status = UNVERIFIED
                entry.detail = "it may come from " + "; ".join(may_provide)
            else:
                entry.status = NEW
            if forced and entry.status in (CONFLICT, REMOVED, EXTERNAL, UNVERIFIED):
                entry.forced = True
                entry.status = APPLY if entry.local_path else NEW
                if entry.local_path:
                    seen_paths.add(entry.local_path)  # replaced, not local-only
            continue

        seen_paths.add(path)
        same = candidates.get((name, _is_override(data)), [])
        if len(same) > 1:
            plan.warnings.append(
                f"{len(same)} files declare '{name}' "
                f"({', '.join(p.name for p in same)}); comparing {path.name}, "
                "the one the loader uses"
            )
        local_text = _read(path) or ""
        local_data = _parse(local_text) or {}
        entry.local_path = path
        entry.local_version = str(local_data.get("version", "?"))
        if local_text == content:
            continue
        if _not_writable(path, proc_dir):
            entry.status = CONFLICT
            entry.detail = (
                f"{path.relative_to(proc_dir)} is a symbolic link, is reached "
                "through one, or is not writable; update does not write it"
            )
            continue  # never written, even when forced

        local_hash = content_hash(local_text)
        # Released hashes are keyed by the bundle's filename, the manifest
        # by the local one (they differ only if the copy was renamed).
        known = set(RELEASED_STARTER_HASHES.get(filename, ()))
        if path.name in manifest:
            known.add(manifest[path.name]["sha256"])
        if local_hash not in known:
            entry.status = EDITED
            entry.detail = "edited locally"
        elif _is_newer(entry.local_version, entry.source_version):
            entry.status = LOCAL_NEWER
            entry.detail = "local copy is newer than the source"
        else:
            entry.status = APPLY
        if entry.status in KEPT and forced:
            entry.forced = True
            entry.status = APPLY

    for (name, _), path in sorted(fallback.items()):
        if name not in source and path not in seen_paths:
            plan.entries.append(
                PlanEntry(
                    name=name,
                    status=LOCAL_ONLY,
                    filename=path.name,
                    local_version=str(
                        (_parse(_read(path) or "") or {}).get("version", "?")
                    ),
                    local_path=path,
                )
            )

    if not plan.writes:
        return plan
    if unreadable_files:
        plan.unsafe = (
            f"cannot read {', '.join(unreadable_files)}, which the loader "
            "reads, so the result cannot be checked"
        )
    elif any(not s.is_git for s in unread):
        plan.unsafe = (
            "the result cannot be checked without reading every extends "
            "source: " + "; ".join(may_provide)
        )
    elif unread:
        plan.unchecked = (
            "not checked: git extends source(s) "
            + ", ".join(redact(s.entry["source"]) for s in unread)
            + " not fetched yet; --apply fetches them and checks before writing"
        )
    else:
        _check_after_apply(plan, proc_dir, files, before, instances, force)
    return plan


def _apply_to(files: dict[str, str], proc_dir: Path, entries: list[PlanEntry]) -> dict[str, str]:
    after = dict(files)
    for e in entries:
        target = e.local_path or proc_dir / e.filename
        after[target.relative_to(proc_dir).as_posix()] = e.source_content
    return after


def _runs(instance: ProcessInstance, discovery: _Discovery) -> bool:
    """The instance's definition loads and has the instance's state."""
    defn = discovery.definitions.get(instance.process_name)
    return instance.current_state in _state_ids(defn)


def _describe(instance: ProcessInstance) -> str:
    return f"{instance.instance_id} ({instance.process_name}) at '{instance.current_state}'"


def _load_culprits(
    writes: list[PlanEntry],
    error: str,
    load,
) -> list[tuple[PlanEntry, str]]:
    """Writes to hold back so the rest loads, each with its load error: the
    one write whose removal makes the plan load, or else every write that
    does not load on top of the writes accepted before it, after retrying
    those in pairs (a parent and an override that only load together)."""
    for e in writes:
        if load([w for w in writes if w is not e])[0] is not None:
            return [(e, error)]
    accepted: list[PlanEntry] = []
    blocked: list[tuple[PlanEntry, str]] = []
    for e in writes:
        found, why = load(accepted + [e])
        if found is None:
            blocked.append((e, why))
        else:
            accepted.append(e)
    retry = True
    while retry:
        retry = False
        for a, b in itertools.combinations(blocked, 2):
            if load(accepted + [a[0], b[0]])[0] is not None:
                accepted += [a[0], b[0]]
                blocked = [x for x in blocked if x is not a and x is not b]
                retry = True
                break
    return blocked


def _check_after_apply(
    plan: Plan,
    proc_dir: Path,
    files: dict[str, str],
    before: _Discovery | None,
    instances: list[ProcessInstance],
    force: frozenset[str],
) -> None:
    """Hold back writes until the post-apply file set loads and strands no
    active instance; record each write's affected and stranded instances.

    The post-apply file set includes the registry edit `update --apply`
    makes. When the current files do not load (`before` is None), the plan
    is applied only if the result loads.
    """

    def target(e: PlanEntry) -> Path:
        return e.local_path or proc_dir / e.filename

    # register_new never edits a linked registry.yaml.
    can_register = not (proc_dir / "registry.yaml").is_symlink()

    def load(entries: list[PlanEntry]) -> tuple[_Discovery | None, str]:
        return _discover(
            _apply_to(files, proc_dir, entries),
            proc_dir.parent,
            [target(e).stem for e in entries if e.register and can_register],
        )

    while True:
        for e in plan.entries:
            e.stranded, e.affected = [], []
        writes = plan.writes
        if not writes:
            return
        after, error = load(writes)
        if after is None:
            culprits = _load_culprits(writes, error, load) if before is not None else []
            if not culprits:
                plan.unsafe = (
                    "the definitions would not load after this update, and "
                    f"no write can be held back to fix that: {error}"
                )
                return
            for e, why in culprits:
                e.status = BLOCKED
                e.forced = False
                e.detail = f"would leave the definitions unloadable: {why}"
            continue

        after_files = _apply_to(files, proc_dir, writes)

        # Each instance's definition and those it inherits from after the
        # update: the writes named for an instance.
        lineage = {
            i.instance_id: _ancestry(i.process_name, after, after_files)
            for i in instances
        }

        stranded = [
            i for i in instances
            if not _runs(i, after) and (before is None or _runs(i, before))
        ]
        held: dict[int, list[str]] = {}
        for i in stranded:
            candidates = [e for e in writes if e.name in lineage[i.instance_id]]
            # A write can also strand an instance it is not named for, such
            # as a forced write over another definition's file; when none
            # is named for it, every write is tested.
            causes = []
            for e in candidates or writes:
                without, _ = load([w for w in writes if w is not e])
                if without is not None and _runs(i, without):
                    causes.append(e)
            causes = causes or candidates
            if not causes and before is None:
                continue  # the instance may have been stranded already
            if not causes:
                plan.unsafe = (
                    f"applying this plan would leave active instance {_describe(i)} "
                    "without its definition or state, and no single write "
                    "accounts for it"
                )
                return
            for e in causes:
                if e.name in force:
                    e.forced = True
                    e.stranded.append(_describe(i))
                else:
                    held.setdefault(id(e), []).append(_describe(i))
        if held:
            for e in writes:
                if id(e) in held:
                    e.status = BLOCKED
                    e.detail = (
                        "active instance(s) that would lose their definition "
                        "or state: " + ", ".join(held[id(e)])
                    )
            continue

        lost = {i.instance_id for i in stranded}
        for e in writes:
            e.affected = [
                i.instance_id for i in instances
                if i.instance_id not in lost and e.name in lineage[i.instance_id]
            ]
        return


def diff(entry: PlanEntry) -> str:
    """Unified diff from the local copy to the source version."""
    local = entry.local_path.read_text() if entry.local_path else ""
    return "".join(
        difflib.unified_diff(
            local.splitlines(keepends=True),
            entry.source_content.splitlines(keepends=True),
            fromfile=f"local/{entry.filename}",
            tofile=f"source/{entry.filename}",
        )
    )


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _commit(writes: list[tuple[Path, str]]) -> None:
    """Write every (path, text) or none of them: each is staged beside its
    target, then all are renamed into place; if any step fails, the files
    already replaced are restored and the error is raised."""
    staged: list[tuple[Path, Path]] = []
    try:
        for path, text in writes:
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
            staged.append((path, Path(tmp)))
            with os.fdopen(fd, "w") as f:
                f.write(text)
            if path.exists():
                shutil.copymode(path, tmp)
            else:
                os.chmod(tmp, 0o666 & ~_umask())
    except BaseException:
        for _, tmp in staged:
            tmp.unlink(missing_ok=True)
        raise
    originals = {path: path.read_bytes() if path.exists() else None for path, _ in staged}
    replaced: list[Path] = []
    try:
        for path, tmp in staged:
            os.replace(tmp, path)
            replaced.append(path)
    except BaseException:
        for path in replaced:
            if originals[path] is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(originals[path])
        for _, tmp in staged:
            tmp.unlink(missing_ok=True)
        raise


def apply_plan(
    proc_dir: Path, plan: Plan, release: str, register: bool = False
) -> list[PlanEntry]:
    """Write every APPLY/NEW entry, record it in the manifest and, with
    `register`, add newly installed files to registry.yaml's `local:` list
    (results in plan.registered and plan.register_warning). All of it is
    written or none of it is.

    Unchanged files that equal the source are recorded too, so the
    manifest describes every unedited starter after an apply.
    """
    entries, _ = load_manifest(proc_dir)
    written: list[PlanEntry] = []
    writes: list[tuple[Path, str]] = []
    for entry in plan.entries:
        if entry.status in WRITES:
            target = entry.local_path or proc_dir / entry.filename
            writes.append((target, entry.source_content))
            entries[target.name] = {
                "release": release,
                "sha256": content_hash(entry.source_content),
            }
            written.append(entry)
        elif entry.status == UP_TO_DATE and entry.local_path is not None:
            # The file equals the source, so this is what it now holds,
            # whatever an older entry says.
            entries[entry.local_path.name] = {
                "release": release,
                "sha256": content_hash(entry.source_content),
            }
    if register:
        stems = [(e.local_path or proc_dir / e.filename).stem for e in written if e.register]
        registry_path = proc_dir / "registry.yaml"
        updated, missing, warning = _registry_edit(_read(registry_path), stems)
        if updated is not None and registry_path.is_symlink():
            updated, warning = None, (
                "registry.yaml is a symbolic link, which update does not write "
                f"through; add {', '.join(missing)} by hand"
            )
        if updated is not None:
            writes.append((registry_path, updated))
            plan.registered = missing
        plan.register_warning = warning
    writes.append((proc_dir / MANIFEST_NAME, _manifest_text(entries)))
    _commit(writes)
    return written


_LOCAL_LIST_RE = re.compile(r"(?m)^local:[ \t]*\n(?P<indent>[ \t]*)-[ \t]")


def _registry_edit(text: str | None, stems: list[str]) -> tuple[str | None, list[str], str]:
    """The registry.yaml text with `stems` added to its `local:` list.

    Returns (updated text, or None when nothing is written; stems added;
    warning). A non-empty list is extended in place, copying its
    indentation, and only if the result still parses with every stem
    listed; an empty or absent list needs nothing.
    """
    if not stems or text is None:
        return None, [], ""
    data = _parse(text) or {}
    listed = data.get("local") or []
    if not isinstance(listed, list) or not listed:
        return None, [], ""
    missing = [s for s in stems if s not in [str(n) for n in listed]]
    if not missing:
        return None, [], ""
    cannot = (
        "registry.yaml lists processes under 'local:' in a form this "
        f"command cannot extend; add {', '.join(missing)} by hand"
    )
    match = _LOCAL_LIST_RE.search(text)
    if not match:
        return None, [], cannot
    # json.dumps yields a valid YAML scalar for any stem.
    addition = "".join(f"{match['indent']}- {json.dumps(s)}\n" for s in missing)
    insert_at = match.start("indent")
    updated = text[:insert_at] + addition + text[insert_at:]
    after = _parse(updated) or {}
    if not set(missing) <= set(after.get("local") or []):
        return None, [], cannot
    return updated, missing, ""


def register_new(proc_dir: Path, stems: list[str]) -> tuple[list[str], str]:
    """Add newly installed files' stems to registry.yaml's `local:` list.

    The loader resolves each `local:` entry as `<entry>.yaml`, and a
    non-empty list disables auto-discovery, so a new starter that is not
    listed cannot be started. A linked registry.yaml is never edited.
    Returns (registered, warning).
    """
    registry_path = proc_dir / "registry.yaml"
    if registry_path.is_symlink():
        updated, missing, warning = _registry_edit(_read(registry_path), stems)
        if updated is None:
            return [], warning
        return [], (
            "registry.yaml is a symbolic link, which update does not write "
            f"through; add {', '.join(missing)} by hand"
        )
    updated, missing, warning = _registry_edit(_read(registry_path), stems)
    if updated is not None:
        registry_path.write_text(updated)
    return missing, warning


# ---------------------------------------------------------------------------
# Re-pinning the generated configs
# ---------------------------------------------------------------------------

# The `--from` spec init writes: turnstile-mcp/-cli @ git+<url>@<ref>#subdir.
_PIN_RE = re.compile(
    r"(turnstile-(?:mcp|cli) @ git\+([^@#\"'\s]+(?:@[^@#\"'\s]+)*?))"
    r"@([^@#\"'\s/]+)"
    r"(#subdirectory=packages/turnstile-(?:mcp|cli))"
)
_RELEASE_REPO = "https://github.com/paperarmada/turnstile"
_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_CONFIGS = (".mcp.json", ".claude/settings.json")


def _tag(ref: str) -> tuple[int, int, int] | None:
    m = _TAG_RE.match(ref)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _is_release_repo(url: str) -> bool:
    url = url.lower().rstrip("/")
    return url.removesuffix(".git") == _RELEASE_REPO


def find_pins(text: str) -> set[str]:
    return {m.group(3) for m in _PIN_RE.finditer(text)}


@dataclass
class PinChange:
    path: str
    old: str
    action: str  # "repin" or "keep"
    reason: str = ""


def redact(text: str) -> str:
    """Drop any user:password@ part of a URL so a token is never printed."""
    return re.sub(r"(?<=://)[^/@\s]+@", "", text)


def _decide(url: str, old: str, target: str) -> tuple[str, str]:
    if not _is_release_repo(url):
        return "keep", (
            f"pins {redact(url)}; update re-pins only "
            "https://github.com/PaperArmada/turnstile.git"
        )
    old_tag, target_tag = _tag(old), _tag(target)
    if target_tag is None:
        return "keep", f"{target} is not a release tag"
    if old_tag is None:
        return "keep", f"{old} is not a release tag"
    if old_tag > target_tag:
        return "keep", f"{old} is newer than {target}"
    return "repin", ""


def _pin_changes(text: str, target: str) -> tuple[str, list[tuple[str, str, str]]]:
    """(rewritten text, [(old ref, action, reason)]) for pins not at target."""
    decisions: list[tuple[str, str, str]] = []

    def replace(m: re.Match) -> str:
        old = m.group(3)
        if old == target:
            return m.group(0)
        action, reason = _decide(m.group(2), old, target)
        decisions.append((old, action, reason))
        if action != "repin":
            return m.group(0)
        return f"{m.group(1)}@{target}{m.group(4)}"

    return _PIN_RE.sub(replace, text), decisions


def plan_repins(root: Path, target: str) -> list[PinChange]:
    """What re-pinning the generated configs to `target` would do.

    Only uvx specs that pin the turnstile release repo to an older release
    tag are re-pinned; forks, branches, commits and newer tags are kept.
    Dev-mode configs carry no pinned spec.
    """
    changes: list[PinChange] = []
    for rel in _CONFIGS:
        path = root / rel
        if path.exists():
            _, decisions = _pin_changes(path.read_text(), target)
            changes += [PinChange(rel, old, a, r) for old, a, r in decisions]
    return changes


def repin(root: Path, target: str) -> list[PinChange]:
    """Apply plan_repins: rewrite the re-pinnable specs, return every
    decision (kept ones included)."""
    changes: list[PinChange] = []
    for rel in _CONFIGS:
        path = root / rel
        if not path.exists():
            continue
        text, decisions = _pin_changes(path.read_text(), target)
        if any(a == "repin" for _, a, _ in decisions):
            path.write_text(text)
        changes += [PinChange(rel, old, a, r) for old, a, r in decisions]
    return changes
