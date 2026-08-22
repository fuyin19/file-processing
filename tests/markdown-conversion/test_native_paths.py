"""Windows long-path and exact publication acceptance coverage."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest


_SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "markdown-conversion" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import native_paths as np
import pipeline


def _long_parent(tmp_path: Path) -> Path:
    parent = tmp_path
    for index in range(3):
        parent = parent / (f"segment-{index}-" + "x" * 74)
    np.mkdir(parent, parents=True, exist_ok=True)
    assert len(str(parent)) > 260
    return parent


def _args(source: Path, output: Path, *extra: str):
    args = pipeline.build_parser().parse_args([
        "--input", str(source), "--output-dir", str(output), *extra,
    ])
    pipeline.precheck(args)
    args.timestamp = "2026-08-20"
    return args


def _install_conversion_double(monkeypatch):
    calls = []

    def worker(request, timeout=pipeline.PROVIDER_TIMEOUT_SECONDS):
        calls.append(request)
        result = pipeline.markdown_to_canonical(
            "# Deterministic\n\nLong-path body",
            request["document_id"],
            request["mode"],
            "file",
        )
        result["adapter"] = {"name": "test-double", "version": "1", "limitations": []}
        return result

    monkeypatch.setattr(pipeline, "_run_provider_worker", worker)
    return calls


def _make_junction(link: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows registry/extended-path acceptance")
def test_real_windows_long_paths_disabled_and_bundle_markdown_collision_overwrite(tmp_path, monkeypatch):
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SYSTEM\CurrentControlSet\Control\FileSystem",
    ) as key:
        value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
    assert value == 0

    long_parent = _long_parent(tmp_path)
    source = long_parent / "source.txt"
    np.write_text(source, "first", encoding="utf-8")
    output = long_parent / "outputs"
    calls = _install_conversion_double(monkeypatch)

    target, _, _ = pipeline.convert_one(_args(source, output), str(source))
    assert np.read_text(target / "src" / source.name) == "first"
    data = json.loads(np.read_text(target / "source.json"))
    assert data["source"]["locator"] == str(np.logical(source))
    assert "\\\\?\\" not in data["source"]["locator"]
    assert calls and calls[0]["source"].startswith("\\\\?\\")

    calls.clear()
    with pytest.raises(pipeline.OutputCollision):
        pipeline.convert_one(_args(source, output), str(source))
    assert calls == []

    np.write_text(source, "second", encoding="utf-8")
    pipeline.convert_one(_args(source, output, "--overwrite"), str(source))
    assert np.read_text(target / "src" / source.name) == "second"

    markdown_output = long_parent / "markdown"
    markdown_target, _, _ = pipeline.convert_one(
        _args(source, markdown_output, "--output-mode", "markdown"), str(source)
    )
    assert np.is_file(markdown_target)
    assert "Long-path body" in np.read_text(markdown_target)


@pytest.mark.skipif(os.name != "nt", reason="Windows long component staging acceptance")
def test_short_stage_accepts_legal_220_character_stem_beyond_max_path(tmp_path, monkeypatch):
    long_parent = _long_parent(tmp_path)
    stem = "s" * 220
    source = long_parent / f"{stem}.txt"
    output = long_parent / "outputs"
    np.write_text(source, "long component", encoding="utf-8")
    _install_conversion_double(monkeypatch)

    old_stage_name = f".{stem}.staging-{'0' * 32}"
    assert len(old_stage_name) > 255
    assert len(str(source)) > 260

    target, _, _ = pipeline.convert_one(_args(source, output), str(source))

    assert target.name == stem
    assert np.read_text(target / "src" / source.name) == "long component"
    assert np.is_file(target / f"{stem}.json")
    assert np.is_file(target / f"{stem}.md")
    assert not list(output.glob(".mc-stage-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows long-path containment acceptance")
def test_verified_bundle_file_accepts_long_ordinary_component_chain(tmp_path):
    root = _long_parent(tmp_path) / "bundle"
    child = root / "assets" / "images" / "asset.bin"
    np.mkdir(child.parent, parents=True, exist_ok=True)
    np.write_bytes(child, b"asset")

    verified = np.verified_bundle_file(root, child)

    assert verified == np.logical(child)
    assert len(str(verified)) > 260


def test_verified_bundle_file_rejects_symlink_ancestor_lstat(tmp_path, monkeypatch):
    root = tmp_path / "bundle"
    ancestor = root / "assets"
    child = ancestor / "asset.bin"
    child.parent.mkdir(parents=True)
    child.write_bytes(b"asset")
    real_lstat = np.lstat

    def symlink_ancestor(path):
        info = real_lstat(path)
        if np.paths_equal(path, ancestor):
            values = list(info)
            values[0] = stat.S_IFLNK | 0o777
            return os.stat_result(values)
        return info

    monkeypatch.setattr(np, "lstat", symlink_ancestor)
    with pytest.raises(np.UnsafeContainmentError, match="link or reparse point"):
        np.verified_bundle_file(root, child)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction containment acceptance")
@pytest.mark.parametrize("artifact", ["asset", "markdown"])
def test_canonical_validation_rejects_junction_ancestor(tmp_path, monkeypatch, artifact):
    source = tmp_path / "source.txt"
    source.write_text("body", encoding="utf-8")
    output = tmp_path / "out"
    _install_conversion_double(monkeypatch)
    bundle, _, _ = pipeline.convert_one(_args(source, output), str(source))
    data = json.loads(np.read_text(bundle / "source.json"))
    external = tmp_path / "external"
    external.mkdir()
    linked = bundle / "linked"
    _make_junction(linked, external)

    if artifact == "asset":
        escaped = external / "asset.png"
        escaped.write_bytes(b"external asset")
        digest = np.sha256_file(escaped)
        item = {
            "asset_id": "asset-external",
            "path": "linked/asset.png",
            "sha256": digest,
            "source_locator": {},
        }
        data["assets"] = [item]
        data["outputs"]["assets"] = [{"path": item["path"], "sha256": digest}]
        message = "asset path is not safely contained"
    else:
        escaped = external / "escaped.md"
        escaped.write_text("external markdown", encoding="utf-8")
        data["outputs"]["markdown"] = {
            "path": "linked/escaped.md",
            "sha256": np.sha256_file(escaped),
        }
        message = "Markdown output is not safely contained"

    with pytest.raises(pipeline.CanonicalValidationError, match=message):
        pipeline.validate_canonical(data, bundle, validate_schema=False)


class _FixedUuid:
    hex = "f" * 32


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC native-path acceptance")
def test_unc_logical_and_native_forms_remain_separate():
    logical = np.logical(r"\\?\UNC\server\share\folder\file.txt")
    assert str(logical) == r"\\server\share\folder\file.txt"
    assert np.native(logical) == r"\\?\UNC\server\share\folder\file.txt"


@pytest.mark.parametrize(
    ("factory", "prefix", "suffix"),
    [
        (np.create_owned_dir, ".mc-stage-", ""),
        (np.create_owned_dir, ".mc-backup-", ""),
        (np.create_owned_file, ".mc-stage-", ".md"),
    ],
)
def test_owned_creation_has_exact_finite_collision_exhaustion(
    tmp_path, monkeypatch, factory, prefix, suffix
):
    collision = tmp_path / f"{prefix}{_FixedUuid.hex}{suffix}"
    if suffix:
        collision.write_text("protected", encoding="utf-8")
    else:
        collision.mkdir()
        (collision / "protected.txt").write_text("protected", encoding="utf-8")
    calls = []

    def fixed():
        calls.append(1)
        return _FixedUuid()

    monkeypatch.setattr(np.uuid, "uuid4", fixed)
    with pytest.raises(RuntimeError, match="after 32 attempts"):
        factory(tmp_path, prefix, suffix) if suffix else factory(tmp_path, prefix)
    assert len(calls) == 32
    protected = collision if suffix else collision / "protected.txt"
    assert protected.read_text(encoding="utf-8") == "protected"


def test_atomic_no_replace_preserves_late_foreign_target(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_text("owned", encoding="utf-8")
    destination.write_text("foreign", encoding="utf-8")

    with pytest.raises(FileExistsError):
        np.rename_no_replace(source, destination)

    assert source.read_text(encoding="utf-8") == "owned"
    assert destination.read_text(encoding="utf-8") == "foreign"


def test_backup_collision_exhaustion_preserves_target_stage_and_collision(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    collision = tmp_path / f".mc-backup-{_FixedUuid.hex}"
    collision.mkdir()
    (collision / "protected.txt").write_text("protected", encoding="utf-8")
    calls = []

    def fixed():
        calls.append(1)
        return _FixedUuid()

    monkeypatch.setattr(np.uuid, "uuid4", fixed)
    with pytest.raises(pipeline.PipelineError, match="after 32 attempts"):
        pipeline._publish_directory(stage, target, True)

    assert len(calls) == 32
    assert (target / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"
    assert (collision / "protected.txt").read_text(encoding="utf-8") == "protected"


def test_source_beneath_bundle_target_is_rejected_before_adapter(tmp_path, monkeypatch):
    target = tmp_path / "source"
    target.mkdir()
    source = target / "source.txt"
    source.write_text("body", encoding="utf-8")
    args = _args(source, tmp_path, "--overwrite")
    monkeypatch.setattr(
        pipeline,
        "_build_document",
        lambda *args, **kwargs: pytest.fail("adapter was called"),
    )

    with pytest.raises(pipeline.PipelineError, match="contains the local source"):
        pipeline.convert_one(args, str(source))

    assert source.read_text(encoding="utf-8") == "body"


def test_late_default_collision_preserves_foreign_target_and_cleans_exact_stage(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.txt"
    source.write_text("body", encoding="utf-8")
    output = tmp_path / "out"
    target = output / "source"
    real_build = pipeline._build_document

    def raced_build(*args, **kwargs):
        document = real_build(*args, **kwargs)
        target.mkdir()
        (target / "foreign.txt").write_text("foreign", encoding="utf-8")
        return document

    _install_conversion_double(monkeypatch)
    monkeypatch.setattr(pipeline, "_build_document", raced_build)
    with pytest.raises(pipeline.OutputCollision):
        pipeline.convert_one(_args(source, output), str(source))

    assert (target / "foreign.txt").read_text(encoding="utf-8") == "foreign"
    assert not list(output.glob(".mc-stage-*"))


def test_source_copy_failure_occurs_before_adapter_and_preserves_overwrite_target(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.txt"
    source.write_text("new", encoding="utf-8")
    output = tmp_path / "out"
    target = output / "source"
    target.mkdir(parents=True)
    (target / "old.txt").write_text("old", encoding="utf-8")
    monkeypatch.setattr(
        np,
        "copy_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("copy failed")),
    )
    monkeypatch.setattr(
        pipeline,
        "_build_document",
        lambda *args, **kwargs: pytest.fail("adapter was called"),
    )

    with pytest.raises(OSError, match="copy failed"):
        pipeline.convert_one(_args(source, output, "--overwrite"), str(source))

    assert (target / "old.txt").read_text(encoding="utf-8") == "old"
    assert not list(output.glob(".mc-stage-*"))


def test_mutating_adapter_is_detected_before_old_overwrite_target_moves(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("original", encoding="utf-8")
    output = tmp_path / "out"
    target = output / "source"
    target.mkdir(parents=True)
    (target / "old.txt").write_text("old", encoding="utf-8")

    def mutating_worker(request, timeout=pipeline.PROVIDER_TIMEOUT_SECONDS):
        result = pipeline.markdown_to_canonical(
            "Body", request["document_id"], request["mode"], "file"
        )
        result["adapter"] = {"name": "mutator", "version": "1", "limitations": []}
        np.write_text(request["source"], "mutated", encoding="utf-8")
        return result

    monkeypatch.setattr(pipeline, "_run_provider_worker", mutating_worker)
    with pytest.raises(pipeline.PipelineError, match="hash mismatch after conversion"):
        pipeline.convert_one(_args(source, output, "--overwrite"), str(source))

    assert (target / "old.txt").read_text(encoding="utf-8") == "old"
    assert not list(output.glob(".mc-backup-*"))


def test_overwrite_move_reported_failed_after_displacement_restores_same_old_identity(
    tmp_path, monkeypatch
):
    target = tmp_path / "target"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    old = np.EntryIdentity.capture(target)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    real_move = np.rename_no_replace
    calls = []

    def moved_then_raised(source, destination):
        calls.append((source, destination))
        real_move(source, destination)
        if len(calls) == 1:
            raise OSError("reported after old move")

    monkeypatch.setattr(np, "rename_no_replace", moved_then_raised)
    with pytest.raises(OSError, match="reported after old move"):
        pipeline._publish_directory(stage, target, True)

    assert old.matches(target)
    assert (target / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".mc-backup-*"))


def test_overwrite_commit_reported_failed_after_move_is_verified_as_committed(
    tmp_path, monkeypatch
):
    target = tmp_path / "target"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    real_move = np.rename_no_replace
    calls = []

    def moved_then_raised(source, destination):
        calls.append((source, destination))
        real_move(source, destination)
        if len(calls) == 2:
            raise OSError("reported after commit")

    monkeypatch.setattr(np, "rename_no_replace", moved_then_raised)
    pipeline._publish_directory(stage, target, True)

    assert (target / "new.txt").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".mc-backup-*"))


def test_failed_commit_with_foreign_target_retains_exact_recovery_backup(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    real_move = np.rename_no_replace
    calls = []

    def raced(source, destination):
        calls.append((source, destination))
        if len(calls) == 2:
            Path(destination).mkdir()
            (Path(destination) / "foreign.txt").write_text("foreign", encoding="utf-8")
            raise OSError("commit race")
        return real_move(source, destination)

    monkeypatch.setattr(np, "rename_no_replace", raced)
    with pytest.raises(pipeline.PipelineError, match="retained exact recovery backup") as exc_info:
        pipeline._publish_directory(stage, target, True)

    backups = list(tmp_path.glob(".mc-backup-*"))
    assert len(backups) == 1
    assert str(backups[0]) in str(exc_info.value)
    assert (backups[0] / "original" / "old.txt").read_text(encoding="utf-8") == "old"
    assert (target / "foreign.txt").read_text(encoding="utf-8") == "foreign"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction acceptance")
def test_overwrite_backup_cleanup_unlinks_junction_without_touching_external_target(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_text("keep", encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()
    junction = target / "external-link"
    _make_junction(junction, external)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")

    pipeline._publish_directory(stage, target, True)

    assert (target / "new.txt").read_text(encoding="utf-8") == "new"
    assert (external / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not list(tmp_path.glob(".mc-backup-*"))
