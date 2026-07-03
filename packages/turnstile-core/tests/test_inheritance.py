"""Tests for turnstile_core.inheritance (override resolution)."""

from pathlib import Path

import pytest
import yaml

from turnstile_core.errors import InheritanceError
from turnstile_core.inheritance import resolve_inheritance
from turnstile_core.loader import (
    DiscoveredDefinition,
    definition_hash,
    discover_definitions,
    load_definition,
    load_override,
)
from turnstile_core.models import (
    OverrideSpec,
    ParameterOverride,
    ProcessDefinition,
    ProcessOverride,
    ProcessParameter,
    ProcessState,
    StatePatch,
    StateHooksPatch,
    StateType,
    ValidationPatch,
    ValidationRule,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def parent():
    return load_definition(FIXTURES / "parent.yaml")


@pytest.fixture()
def child_override():
    return load_override(FIXTURES / "child-override.yaml")


# ---------------------------------------------------------------------------
# resolve_inheritance
# ---------------------------------------------------------------------------


class TestResolveInheritance:
    def test_basic_merge(self, parent, child_override):
        result = resolve_inheritance(child_override, parent)
        assert result.name == "release"
        assert result.version == "1.1.0"
        assert result.description == "Release process with security scan"
        assert result.extends == "release"

    def test_add_states(self, parent, child_override):
        result = resolve_inheritance(child_override, parent)
        state_ids = [s.id for s in result.states]
        assert "security_scan" in state_ids

    def test_patch_transitions(self, parent, child_override):
        result = resolve_inheritance(child_override, parent)
        test_state = result.get_state("test")
        assert test_state.transitions == ["security_scan"]

    def test_added_state_has_validations(self, parent, child_override):
        result = resolve_inheritance(child_override, parent)
        scan = result.get_state("security_scan")
        assert scan.on_exit is not None
        assert len(scan.on_exit.validations) == 1

    def test_append_parameter(self, parent, child_override):
        result = resolve_inheritance(child_override, parent)
        param_names = [p.name for p in result.parameters]
        assert "branch_name" in param_names
        assert "scan_profile" in param_names

    def test_parent_unchanged(self, parent, child_override):
        """Resolving should not mutate the parent."""
        original_transitions = parent.get_state("test").transitions[:]
        resolve_inheritance(child_override, parent)
        assert parent.get_state("test").transitions == original_transitions

    def test_duplicate_state_raises(self, parent):
        override = ProcessOverride(
            extends="release",
            overrides=OverrideSpec(
                add_states=[
                    ProcessState(
                        id="test",  # already exists
                        transitions=["done"],
                    )
                ]
            ),
        )
        with pytest.raises(InheritanceError, match="already exists"):
            resolve_inheritance(override, parent)

    def test_patch_nonexistent_state_raises(self, parent):
        override = ProcessOverride(
            extends="release",
            overrides=OverrideSpec(
                patch_states=[
                    StatePatch(id="nonexistent", transitions=["done"])
                ]
            ),
        )
        with pytest.raises(InheritanceError, match="not found"):
            resolve_inheritance(override, parent)

    def test_append_duplicate_param_raises(self, parent):
        override = ProcessOverride(
            extends="release",
            overrides=OverrideSpec(
                parameters=ParameterOverride(
                    append=[
                        ProcessParameter(name="branch_name", description="dup")
                    ]
                )
            ),
        )
        with pytest.raises(InheritanceError, match="already exists"):
            resolve_inheritance(override, parent)

    def test_remove_parameter(self, parent):
        override = ProcessOverride(
            extends="release",
            overrides=OverrideSpec(
                parameters=ParameterOverride(remove=["branch_name"])
            ),
        )
        result = resolve_inheritance(override, parent)
        param_names = [p.name for p in result.parameters]
        assert "branch_name" not in param_names

    def test_name_from_parent_when_not_set(self, parent):
        override = ProcessOverride(extends="release")
        result = resolve_inheritance(override, parent)
        assert result.name == "release"

    def test_validation_patch_replace(self, parent):
        new_rule = ValidationRule(
            command="echo replaced", expect="not_empty", message="replaced"
        )
        override = ProcessOverride(
            extends="release",
            overrides=OverrideSpec(
                patch_states=[
                    StatePatch(
                        id="test",
                        on_exit=StateHooksPatch(
                            validate=ValidationPatch(
                                mode="replace", items=[new_rule]
                            )
                        ),
                    )
                ]
            ),
        )
        result = resolve_inheritance(override, parent)
        test_state = result.get_state("test")
        assert len(test_state.on_exit.validations) == 1
        assert test_state.on_exit.validations[0].command == "echo replaced"

    def test_validation_patch_append(self, parent):
        new_rule = ValidationRule(
            command="echo extra", expect="not_empty", message="extra"
        )
        override = ProcessOverride(
            extends="release",
            overrides=OverrideSpec(
                patch_states=[
                    StatePatch(
                        id="test",
                        on_exit=StateHooksPatch(
                            validate=ValidationPatch(
                                mode="append", items=[new_rule]
                            )
                        ),
                    )
                ]
            ),
        )
        result = resolve_inheritance(override, parent)
        test_state = result.get_state("test")
        assert len(test_state.on_exit.validations) == 2  # original + appended

    def test_patch_description(self, parent):
        override = ProcessOverride(
            extends="release",
            overrides=OverrideSpec(
                patch_states=[
                    StatePatch(id="deploy", description="Deploy to staging first")
                ]
            ),
        )
        result = resolve_inheritance(override, parent)
        assert result.get_state("deploy").description == "Deploy to staging first"

    def test_patch_metadata_merges(self, parent):
        override = ProcessOverride(
            extends="release",
            overrides=OverrideSpec(
                patch_states=[
                    StatePatch(id="test", metadata={"env": "staging", "timeout": "30"})
                ]
            ),
        )
        result = resolve_inheritance(override, parent)
        assert result.get_state("test").metadata["env"] == "staging"

    def test_add_hooks_to_bare_state(self, parent):
        """Add on_enter hooks to a state that had none."""
        new_rule = ValidationRule(
            command="echo ready", expect="not_empty", message="ready"
        )
        override = ProcessOverride(
            extends="release",
            overrides=OverrideSpec(
                patch_states=[
                    StatePatch(
                        id="done",
                        on_enter=StateHooksPatch(
                            validate=ValidationPatch(items=[new_rule])
                        ),
                    )
                ]
            ),
        )
        # done is terminal, has no on_enter in parent
        result = resolve_inheritance(override, parent)
        done = result.get_state("done")
        assert done.on_enter is not None
        assert len(done.on_enter.validations) == 1

    def test_invalid_merge_raises(self, parent):
        """Override that produces invalid definition raises InheritanceError."""
        # Add a state that transitions to nonexistent target
        override = ProcessOverride(
            extends="release",
            overrides=OverrideSpec(
                add_states=[
                    ProcessState(
                        id="broken",
                        transitions=["nonexistent_target"],
                    )
                ]
            ),
        )
        with pytest.raises(InheritanceError, match="invalid"):
            resolve_inheritance(override, parent)


# ---------------------------------------------------------------------------
# Loader integration (override files in .processes/overrides/)
# ---------------------------------------------------------------------------


class TestOverrideLoader:
    def test_load_override_file(self):
        override = load_override(FIXTURES / "child-override.yaml")
        assert override.extends == "release"
        assert override.version == "1.1.0"
        assert len(override.overrides.add_states) == 1
        assert len(override.overrides.patch_states) == 1

    def test_discover_with_overrides(self, tmp_path):
        """Override files in .processes/overrides/ are resolved."""
        # Set up shared source with parent
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        parent_data = {
            "name": "release",
            "version": "1.0.0",
            "states": [
                {"id": "start", "type": "initial", "transitions": ["test"]},
                {"id": "test", "transitions": ["done"]},
                {"id": "done", "type": "terminal"},
            ],
        }
        (shared_dir / "release.yaml").write_text(yaml.dump(parent_data))

        # Set up project with registry + override
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        overrides_dir = proc_dir / "overrides"
        overrides_dir.mkdir()

        (proc_dir / "registry.yaml").write_text(yaml.dump({
            "version": "1.0",
            "extends": [{"source": str(shared_dir), "processes": []}],
        }))

        override_data = {
            "extends": "release",
            "version": "1.1.0",
            "overrides": {
                "add_states": [
                    {"id": "lint", "transitions": ["test"]},
                ],
                "patch_states": [
                    {"id": "start", "transitions": ["lint"]},
                ],
            },
        }
        (overrides_dir / "release-custom.yaml").write_text(
            yaml.dump(override_data)
        )

        result = discover_definitions(tmp_path)
        assert "release" in result
        defn, _ = result["release"]
        assert defn.version == "1.1.0"
        state_ids = [s.id for s in defn.states]
        assert "lint" in state_ids

    def test_override_missing_parent_raises(self, tmp_path):
        """Override referencing nonexistent parent raises."""
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        overrides_dir = proc_dir / "overrides"
        overrides_dir.mkdir()

        (proc_dir / "registry.yaml").write_text(yaml.dump({
            "version": "1.0",
            "extends": [],
        }))

        override_data = {
            "extends": "nonexistent",
            "overrides": {},
        }
        (overrides_dir / "bad-override.yaml").write_text(
            yaml.dump(override_data)
        )

        with pytest.raises(InheritanceError, match="was found"):
            discover_definitions(tmp_path)

    def test_override_extends_local_definition(self, tmp_path):
        """Override files in overrides/ can extend local definitions."""
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        overrides_dir = proc_dir / "overrides"
        overrides_dir.mkdir()

        parent_data = {
            "name": "release",
            "version": "1.0.0",
            "states": [
                {"id": "start", "type": "initial", "transitions": ["done"]},
                {"id": "done", "type": "terminal"},
            ],
        }
        (proc_dir / "release.yaml").write_text(yaml.dump(parent_data))

        override_data = {
            "extends": "release",
            "name": "release-custom",
            "version": "1.1.0",
            "overrides": {
                "add_states": [
                    {"id": "lint", "transitions": ["done"]},
                ],
                "patch_states": [
                    {"id": "start", "transitions": ["lint"]},
                ],
            },
        }
        (overrides_dir / "release-custom.yaml").write_text(
            yaml.dump(override_data)
        )

        result = discover_definitions(tmp_path)
        assert "release" in result
        assert "release-custom" in result
        defn, _ = result["release-custom"]
        assert defn.version == "1.1.0"
        assert "lint" in [s.id for s in defn.states]

    def test_registry_local_override_file(self, tmp_path):
        """A top-level override file listed in registry.local resolves
        against a local parent."""
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()

        parent_data = {
            "name": "release",
            "version": "1.0.0",
            "states": [
                {"id": "start", "type": "initial", "transitions": ["done"]},
                {"id": "done", "type": "terminal"},
            ],
        }
        (proc_dir / "release.yaml").write_text(yaml.dump(parent_data))

        override_data = {
            "extends": "release",
            "name": "release-custom",
            "overrides": {
                "add_states": [
                    {"id": "lint", "transitions": ["done"]},
                ],
                "patch_states": [
                    {"id": "start", "transitions": ["lint"]},
                ],
            },
        }
        (proc_dir / "release-custom.yaml").write_text(
            yaml.dump(override_data)
        )

        (proc_dir / "registry.yaml").write_text(yaml.dump({
            "version": "1.0",
            "local": ["release", "release-custom"],
        }))

        result = discover_definitions(tmp_path)
        assert "release" in result
        assert "release-custom" in result
        defn, _ = result["release-custom"]
        assert "lint" in [s.id for s in defn.states]

    def test_unnamed_override_patches_local_parent_in_place(self, tmp_path):
        """An override without a distinct name patches its own local
        parent in place, matching the extended-source behavior."""
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        overrides_dir = proc_dir / "overrides"
        overrides_dir.mkdir()

        parent_data = {
            "name": "release",
            "version": "1.0.0",
            "states": [
                {"id": "start", "type": "initial", "transitions": ["done"]},
                {"id": "done", "type": "terminal"},
            ],
        }
        (proc_dir / "release.yaml").write_text(yaml.dump(parent_data))

        override_data = {
            "extends": "release",
            "version": "2.0.0",
            "overrides": {},
        }
        (overrides_dir / "release.yaml").write_text(yaml.dump(override_data))

        result = discover_definitions(tmp_path)
        defn, _ = result["release"]
        assert defn.version == "2.0.0"

    def test_override_colliding_with_unrelated_local_raises(self, tmp_path):
        """An override whose resolved name matches an unrelated local
        definition is a configuration error, not a silent shadow."""
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        overrides_dir = proc_dir / "overrides"
        overrides_dir.mkdir()

        for name in ("release", "deploy"):
            data = {
                "name": name,
                "version": "1.0.0",
                "states": [
                    {"id": "start", "type": "initial", "transitions": ["done"]},
                    {"id": "done", "type": "terminal"},
                ],
            }
            (proc_dir / f"{name}.yaml").write_text(yaml.dump(data))

        override_data = {
            "extends": "release",
            "name": "deploy",  # collides with the unrelated local 'deploy'
            "overrides": {},
        }
        (overrides_dir / "bad.yaml").write_text(yaml.dump(override_data))

        with pytest.raises(InheritanceError, match="collides"):
            discover_definitions(tmp_path)

    def test_autodiscover_top_level_override_resolves(self, tmp_path):
        """Auto-discovery (no registry.local) resolves a top-level
        override file against a local parent."""
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()

        parent_data = {
            "name": "release",
            "version": "1.0.0",
            "states": [
                {"id": "start", "type": "initial", "transitions": ["done"]},
                {"id": "done", "type": "terminal"},
            ],
        }
        (proc_dir / "release.yaml").write_text(yaml.dump(parent_data))

        override_data = {
            "extends": "release",
            "name": "release-custom",
            "overrides": {
                "add_states": [{"id": "lint", "transitions": ["done"]}],
                "patch_states": [{"id": "start", "transitions": ["lint"]}],
            },
        }
        (proc_dir / "release-custom.yaml").write_text(yaml.dump(override_data))

        result = discover_definitions(tmp_path)
        assert "release-custom" in result
        defn, _ = result["release-custom"]
        assert "lint" in [s.id for s in defn.states]

    def test_autodiscover_stray_override_skipped(self, tmp_path):
        """A stray auto-discovered override with a missing parent is
        skipped with a warning; other definitions still load (fail open)."""
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()

        good_data = {
            "name": "good",
            "version": "1.0.0",
            "states": [
                {"id": "start", "type": "initial", "transitions": ["done"]},
                {"id": "done", "type": "terminal"},
            ],
        }
        (proc_dir / "good.yaml").write_text(yaml.dump(good_data))

        stray_data = {
            "extends": "removed-parent",
            "overrides": {},
        }
        (proc_dir / "stray.yaml").write_text(yaml.dump(stray_data))

        result = discover_definitions(tmp_path)
        assert "good" in result
        assert len(result) == 1

    def test_registry_local_stray_override_raises(self, tmp_path):
        """An override explicitly listed in registry.local with a
        missing parent raises instead of being silently skipped."""
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()

        stray_data = {
            "extends": "removed-parent",
            "overrides": {},
        }
        (proc_dir / "stray.yaml").write_text(yaml.dump(stray_data))

        (proc_dir / "registry.yaml").write_text(yaml.dump({
            "version": "1.0",
            "local": ["stray"],
        }))

        with pytest.raises(InheritanceError, match="was found"):
            discover_definitions(tmp_path)

    def test_full_definition_with_stray_overrides_key_loads(self, tmp_path):
        """A full definition carrying a stray 'overrides' key (but no
        'extends') is not misclassified as an override file."""
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()

        data = {
            "name": "deploy",
            "version": "1.0.0",
            "overrides": {},  # stray key, e.g. leftover from a conversion
            "states": [
                {"id": "start", "type": "initial", "transitions": ["done"]},
                {"id": "done", "type": "terminal"},
            ],
        }
        (proc_dir / "deploy.yaml").write_text(yaml.dump(data))

        (proc_dir / "registry.yaml").write_text(yaml.dump({
            "version": "1.0",
            "local": ["deploy"],
        }))

        result = discover_definitions(tmp_path)
        assert "deploy" in result

    def test_local_replaces_extended_then_override_patches(self, tmp_path):
        """A local full definition replaces the extended parent, and an
        unnamed override then patches the local in place. The override
        always applies to whatever its parent name finally resolves to."""
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        parent_data = {
            "name": "release",
            "version": "1.0.0",
            "states": [
                {"id": "start", "type": "initial", "transitions": ["done"]},
                {"id": "done", "type": "terminal"},
            ],
        }
        (shared_dir / "release.yaml").write_text(yaml.dump(parent_data))

        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        overrides_dir = proc_dir / "overrides"
        overrides_dir.mkdir()

        (proc_dir / "registry.yaml").write_text(yaml.dump({
            "version": "1.0",
            "extends": [{"source": str(shared_dir), "processes": []}],
        }))

        override_data = {
            "extends": "release",
            "version": "2.0.0",
            "overrides": {},
        }
        (overrides_dir / "release-custom.yaml").write_text(
            yaml.dump(override_data)
        )

        # Also put a local definition that should win
        local_data = {
            "name": "release",
            "version": "3.0.0",
            "states": [
                {"id": "start", "type": "initial", "transitions": ["done"]},
                {"id": "done", "type": "terminal"},
            ],
        }
        (proc_dir / "release.yaml").write_text(yaml.dump(local_data))

        result = discover_definitions(tmp_path)
        defn, _ = result["release"]
        assert defn.version == "2.0.0"  # override patched the local
