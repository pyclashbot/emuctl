from pathlib import Path

import pytest

from emuctl.config import (
    EmuctlConfig,
    RendererMode,
    ResolvedConfig,
    resolve_config,
)
from emuctl.errors import ConfigError, ConfigValidationError, InvalidArgumentError


def _env_without_home() -> dict[str, str]:
    return {}


def test_defaults_when_no_layers(tmp_path: Path) -> None:
    resolved = resolve_config(flags={}, env=_env_without_home(), cwd=tmp_path)
    assert resolved == ResolvedConfig(config=EmuctlConfig(), warnings=())
    assert resolved.warnings == ()


def test_purity_equal_inputs_equal_results(tmp_path: Path) -> None:
    first = resolve_config(flags={"display.width": 800}, env={"EMUCTL_LOG_LEVEL": "info"}, cwd=tmp_path)
    second = resolve_config(flags={"display.width": 800}, env={"EMUCTL_LOG_LEVEL": "info"}, cwd=tmp_path)
    assert first == second
    assert first is not second


def test_flags_layer_applies(tmp_path: Path) -> None:
    resolved = resolve_config(flags={"display.width": 800}, env={}, cwd=tmp_path)
    assert resolved.config.display.width == 800
    assert resolved.warnings == ()


def test_env_flat_leaf_grammar(tmp_path: Path) -> None:
    resolved = resolve_config(
        env={
            "EMUCTL_DISPLAY_WIDTH": "800",
            "PATH": "/usr/bin",
        },
        cwd=tmp_path,
    )
    assert resolved.config.display.width == 800


def test_unshipped_vendor_leaf_is_unknown_and_warns(tmp_path: Path) -> None:
    resolved = resolve_config(env={"EMUCTL_BLUESTACKS_INSTANCE_NAME": "bot-1"}, cwd=tmp_path)
    assert resolved.config.memu is None
    assert len(resolved.warnings) == 1
    assert resolved.warnings[0].layer == "env"


def test_memu_and_resource_env_leaves_are_recognized(tmp_path: Path) -> None:
    resolved = resolve_config(
        env={
            "EMUCTL_MEMU_VM_NAME": "pyclashbot-136",
            "EMUCTL_MEMU_SETTINGS_HIDE_TOOLBAR": "true",
            "EMUCTL_RESOURCES_CPUS": "2",
            "EMUCTL_DISPLAY_RENDERER": "auto",
            "EMUCTL_DISPLAY_FPS": "40",
        },
        cwd=tmp_path,
    )
    assert resolved.warnings == ()
    assert resolved.config.memu is not None
    assert resolved.config.memu.vm_name == "pyclashbot-136"
    assert resolved.config.memu.settings.hide_toolbar is True
    assert resolved.config.resources.cpus == 2
    assert resolved.config.display.renderer == RendererMode.AUTO
    assert resolved.config.display.fps == 40


def test_memu_toml_table_builds_config(tmp_path: Path) -> None:
    (tmp_path / "emuctl.toml").write_text(
        "[display]\nwidth = 419\nheight = 633\nrenderer = 'opengl'\n"
        "[resources]\ncpus = 2\nmemory_mb = 2048\n"
        "[memu]\nmemuc_path = '/opt/memu/memuc'\nvm_name = 'bot-1'\n"
        "[memu.settings]\nturbo = false\nwindow_mode = 'custom'\nscale_percent = 75\n",
        encoding="utf-8",
    )
    resolved = resolve_config(env={}, cwd=tmp_path)
    assert resolved.config.display.renderer == RendererMode.OPENGL
    assert resolved.config.resources.memory_mb == 2048
    assert resolved.config.memu is not None
    assert resolved.config.memu.memuc_path == "/opt/memu/memuc"
    assert resolved.config.memu.settings.turbo is False
    assert resolved.config.memu.settings.window_mode == "custom"
    assert resolved.config.memu.settings.scale_percent == 75
    assert resolved.warnings == ()


def test_env_unknown_name_warns_with_hint(tmp_path: Path) -> None:
    resolved = resolve_config(env={"EMUCTL_DISPALY_WIDTH": "800"}, cwd=tmp_path)
    assert resolved.config.display.width is None
    assert len(resolved.warnings) == 1
    warning = resolved.warnings[0]
    assert warning.layer == "env"
    assert warning.nearest == "display.width"
    assert "display.width" in str(warning)


def test_env_unknown_name_without_hint(tmp_path: Path) -> None:
    resolved = resolve_config(env={"EMUCTL_ZZZ_QQQ": "1"}, cwd=tmp_path)
    assert len(resolved.warnings) == 1
    assert resolved.warnings[0].nearest is None


def test_env_bad_value_is_strict_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigValidationError) as excinfo:
        resolve_config(env={"EMUCTL_DISPLAY_WIDTH": "wide"}, cwd=tmp_path)
    assert excinfo.value.layer == "env"
    with pytest.raises(ConfigValidationError):
        resolve_config(env={"EMUCTL_ADB_PORT": "99999"}, cwd=tmp_path)


def test_project_file_authoritative(tmp_path: Path) -> None:
    (tmp_path / "emuctl.toml").write_text("[display]\nwidth = 800\nheight = 600\n", encoding="utf-8")
    resolved = resolve_config(env={}, cwd=tmp_path)
    assert resolved.config.display.width == 800
    assert resolved.config.display.height == 600


def test_nearest_project_dir_wins_outer_ignored(tmp_path: Path) -> None:
    (tmp_path / "emuctl.toml").write_text("[display]\nwidth = 800\n", encoding="utf-8")
    inner = tmp_path / "sub" / "inner"
    inner.mkdir(parents=True)
    (tmp_path / "sub" / "emuctl.toml").write_text("[display]\nwidth = 1024\n", encoding="utf-8")
    resolved = resolve_config(env={}, cwd=inner)
    assert resolved.config.display.width == 1024


def test_outer_project_used_when_inner_has_none(tmp_path: Path) -> None:
    (tmp_path / "emuctl.toml").write_text("[display]\nwidth = 800\n", encoding="utf-8")
    inner = tmp_path / "sub"
    inner.mkdir()
    resolved = resolve_config(env={}, cwd=inner)
    assert resolved.config.display.width == 800


def test_emuctl_toml_wins_over_same_dir_tool_table(tmp_path: Path) -> None:
    (tmp_path / "emuctl.toml").write_text("[display]\nwidth = 800\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.emuctl.display]\nwidth = 1024\n", encoding="utf-8")
    resolved = resolve_config(env={}, cwd=tmp_path)
    assert resolved.config.display.width == 800


def test_tool_table_used_when_no_emuctl_toml(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.emuctl.display]\nwidth = 1024\n", encoding="utf-8")
    resolved = resolve_config(env={}, cwd=tmp_path)
    assert resolved.config.display.width == 1024


def test_pyproject_without_tool_table_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    inner = tmp_path / "sub"
    inner.mkdir()
    (inner / "emuctl.toml").write_text("[display]\nwidth = 640\n", encoding="utf-8")
    resolved = resolve_config(env={}, cwd=inner)
    assert resolved.config.display.width == 640


def test_precedence_flags_over_env_over_project_over_user(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config" / "emuctl").mkdir(parents=True)
    (home / ".config" / "emuctl" / "emuctl.toml").write_text("[display]\nwidth = 100\n", encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "emuctl.toml").write_text("[display]\nwidth = 200\n", encoding="utf-8")
    env = {"HOME": str(home), "EMUCTL_DISPLAY_WIDTH": "300"}

    assert resolve_config(env=env, cwd=proj).config.display.width == 300
    assert resolve_config(flags={"display.width": 400}, env=env, cwd=proj).config.display.width == 400
    assert resolve_config(env={"HOME": str(home)}, cwd=proj).config.display.width == 200
    assert resolve_config(env={"HOME": str(home)}, cwd=tmp_path).config.display.width == 100


def test_unknown_toml_key_warns_with_hint_and_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "emuctl.toml").write_text("[display]\nwdith = 800\nwidth = 640\n", encoding="utf-8")
    resolved = resolve_config(env={}, cwd=tmp_path)
    assert resolved.config.display.width == 640
    assert len(resolved.warnings) == 1
    warning = resolved.warnings[0]
    assert warning.layer == "project"
    assert warning.path == "display.wdith"
    assert warning.nearest == "display.width"


def test_unknown_toml_value_is_strict_error(tmp_path: Path) -> None:
    (tmp_path / "emuctl.toml").write_text("[display]\nwidth = 'wide'\n", encoding="utf-8")
    with pytest.raises(ConfigValidationError) as excinfo:
        resolve_config(env={}, cwd=tmp_path)
    assert excinfo.value.layer == "project"
    assert excinfo.value.path == "display.width"


def test_out_of_range_names_winning_layer(tmp_path: Path) -> None:
    (tmp_path / "emuctl.toml").write_text("[adb]\nport = 99999\n", encoding="utf-8")
    with pytest.raises(ConfigValidationError) as excinfo:
        resolve_config(env={}, cwd=tmp_path)
    assert excinfo.value.layer == "project"
    assert excinfo.value.path == "adb.port"


def test_schema_version_absent_minor_and_major(tmp_path: Path) -> None:
    (tmp_path / "emuctl.toml").write_text("[display]\nwidth = 1\n", encoding="utf-8")
    assert resolve_config(env={}, cwd=tmp_path).warnings == ()

    (tmp_path / "emuctl.toml").write_text("schema_version = 1.1\n[display]\nwidth = 1\n", encoding="utf-8")
    assert resolve_config(env={}, cwd=tmp_path).config.display.width == 1

    (tmp_path / "emuctl.toml").write_text("schema_version = 2\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        resolve_config(env={}, cwd=tmp_path)
    assert excinfo.value.reason == "bad_version"
    assert excinfo.value.value == 2
    assert excinfo.value.layer == "project"

    (tmp_path / "emuctl.toml").write_text("schema_version = '2.0'\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo2:
        resolve_config(env={}, cwd=tmp_path)
    assert excinfo2.value.reason == "bad_version"


def test_unreadable_toml_is_config_error(tmp_path: Path) -> None:
    (tmp_path / "emuctl.toml").write_text("[display\nwidth = ", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        resolve_config(env={}, cwd=tmp_path)
    assert excinfo.value.reason == "unreadable"
    assert excinfo.value.layer == "project"


def test_config_file_replaces_project_and_user(tmp_path: Path) -> None:
    (tmp_path / "emuctl.toml").write_text("[display]\nwidth = 800\n", encoding="utf-8")
    other = tmp_path / "other.toml"
    other.write_text("[display]\nwidth = 320\n", encoding="utf-8")
    home = tmp_path / "home"
    (home / ".config" / "emuctl").mkdir(parents=True)
    (home / ".config" / "emuctl" / "emuctl.toml").write_text("[display]\nwidth = 100\n", encoding="utf-8")
    resolved = resolve_config(env={"HOME": str(home)}, cwd=tmp_path, config_file=other)
    assert resolved.config.display.width == 320


def test_no_config_skips_project_and_user(tmp_path: Path) -> None:
    (tmp_path / "emuctl.toml").write_text("[display]\nwidth = 800\n", encoding="utf-8")
    resolved = resolve_config(env={}, cwd=tmp_path, no_config=True)
    assert resolved.config.display.width is None


def test_config_file_missing_is_unreadable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        resolve_config(env={}, cwd=tmp_path, config_file=tmp_path / "absent.toml")
    assert excinfo.value.reason == "unreadable"


def test_config_file_and_no_config_conflict(tmp_path: Path) -> None:
    with pytest.raises(InvalidArgumentError):
        resolve_config(env={}, cwd=tmp_path, config_file=tmp_path / "x.toml", no_config=True)


def test_explicit_null_resets_to_default(tmp_path: Path) -> None:
    resolved = resolve_config(
        flags={"display.width": None},
        env={"EMUCTL_DISPLAY_WIDTH": "800"},
        cwd=tmp_path,
    )
    assert resolved.config.display.width is None


def test_resolved_config_warnings_coerced_to_tuple() -> None:
    from emuctl.config import ConfigWarning

    resolved = ResolvedConfig(
        config=EmuctlConfig(),
        warnings=[ConfigWarning(layer="env", path="EMUCTL_X")],  # ty: ignore[invalid-argument-type]
    )
    assert resolved.warnings == (ConfigWarning(layer="env", path="EMUCTL_X"),)
