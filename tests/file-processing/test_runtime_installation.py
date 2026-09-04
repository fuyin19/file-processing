"""Relocatable unified-installation coverage for the conversion runtime carrier."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest


ROOT = Path(__file__).absolute().parents[2]
WORKFLOWS = ("markdown-conversion", "pdf-conversion", "file-conversion")
CARRIER_FILES = {
    "anti_entropy_core_adapter.py",
    "conversion_runtime.py",
    "libreoffice_pdf.py",
    "native_paths.py",
    "pdf_validation_worker.py",
    "runtime_layout.py",
}


def _copy_discoverable_skills(source: Path, destination: Path) -> tuple[str, ...]:
    destination.mkdir(parents=True)
    copied = []
    for candidate in sorted(source.iterdir(), key=lambda item: item.name):
        info = candidate.lstat()
        if not stat.S_ISDIR(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
            continue
        boundary = candidate / "SKILL.md"
        try:
            boundary_info = boundary.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(boundary_info.st_mode) or not stat.S_ISREG(boundary_info.st_mode):
            continue
        shutil.copytree(
            candidate,
            destination / candidate.name,
            ignore=shutil.ignore_patterns("__pycache__", "config.json"),
        )
        copied.append(candidate.name)
    return tuple(copied)


@pytest.fixture
def installation(tmp_path: Path) -> Path:
    original = tmp_path / "original"
    copied = _copy_discoverable_skills(ROOT / "skills", original / "skills")
    assert {"file-processing", *WORKFLOWS} <= set(copied)
    moved = tmp_path / "moved installation 中文"
    original.rename(moved)
    return moved / "skills"


def _run(command: list[object], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["ANTI_ENTROPY_CORE_RUNNER"] = ""
    return subprocess.run(
        [str(item) for item in command],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )


def _tree_snapshot(root: Path) -> tuple[tuple[str, str, str], ...]:
    rows = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
            rows.append(("link", relative, os.readlink(path)))
        elif stat.S_ISDIR(info.st_mode):
            rows.append(("dir", relative, ""))
        elif stat.S_ISREG(info.st_mode):
            rows.append(("file", relative, hashlib.sha256(path.read_bytes()).hexdigest()))
        else:
            rows.append(("other", relative, str(info.st_mode)))
    return tuple(rows)


VERSION_CONFIG_DRIVER = r"""
import importlib.util, pathlib, sys
sys.dont_write_bytecode = True
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
pipeline_path = pathlib.Path(sys.argv[1])
expected_arg = sys.argv[2]
expected_probe = sys.argv[3]
cli = sys.argv[4:]
spec = importlib.util.spec_from_file_location("version_config_pipeline", pipeline_path)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
original = module.load_config
calls = []
def observed(path=None):
    actual = "<none>" if path is None else str(path)
    assert actual == expected_arg, (actual, expected_arg)
    calls.append(actual)
    loaded = original(path)
    if expected_probe != "<none>":
        assert loaded.get("probe") == expected_probe
    return loaded
module.load_config = observed
module.show_version = lambda *args: print("VERSION_OK")
sys.argv = [str(pipeline_path), *cli]
code = module.main()
assert calls == [expected_arg]
raise SystemExit(code)
"""


def test_carrier_is_discoverable_complete_and_has_no_conversion_cli():
    carrier = ROOT / "skills" / "file-processing"
    skill = carrier / "SKILL.md"
    assert skill.is_file()
    text = skill.read_text(encoding="utf-8")
    assert "name: file-processing" in text
    assert "version: 1.1.0" in text
    assert "read-only" in text.lower()
    assert {path.name for path in (carrier / "scripts").iterdir() if path.is_file()} == CARRIER_FILES
    assert not (carrier / "scripts" / "pipeline.py").exists()
    assert not (ROOT / "skills" / "_shared").exists()
    assert json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"] == "7.2.0"
    assert json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"] == "7.2.0"


@pytest.mark.parametrize("skill", ["markdown-conversion", "file-conversion"])
def test_private_preflight_reports_missing_carrier_as_installation_error(
    tmp_path: Path, skill: str,
) -> None:
    script = tmp_path / "skills" / skill / "scripts" / "pipeline.py"
    script.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "skills" / skill / "scripts" / "pipeline.py", script)
    result = _run(
        [sys.executable, "-I", "-B", script, "--runtime-preflight-json", "--required-suffix", ".pdf"],
        tmp_path,
    )
    assert result.returncode == 1 and result.stderr == ""
    assert json.loads(result.stdout) == {
        "schema_version": 1, "status": "error", "scope": "installation",
        "code": "conversion_runtime_unavailable",
    }


def test_relocated_isolated_help_is_read_only_and_ignores_cwd_decoys(
    installation: Path, tmp_path: Path
):
    unrelated = tmp_path / "unrelated cwd"
    unrelated.mkdir()
    marker = unrelated / "decoy-loaded"
    (unrelated / "native_paths.py").write_text(
        "raise RuntimeError('cwd native_paths decoy loaded')\n", encoding="utf-8"
    )
    decoy = unrelated / "file-processing" / "scripts"
    decoy.mkdir(parents=True)
    (decoy / "runtime_layout.py").write_text(
        "from pathlib import Path\n"
        + "Path("
        + repr(str(marker))
        + ").write_text('loaded', encoding='utf-8')\n",
        encoding="utf-8",
    )
    sentinels = {}
    for skill in WORKFLOWS:
        sentinel = installation / skill / "scripts" / "config.json"
        sentinel.write_bytes(b"\xffhelp sentinel must remain unread")
        sentinels[skill] = sentinel
    before = _tree_snapshot(installation)
    for skill in WORKFLOWS:
        script = installation / skill / "scripts" / "pipeline.py"
        result = _run([sys.executable, "-I", "-B", script, "--help"], unrelated)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "usage:" in result.stdout
        assert result.stderr == ""
    assert _tree_snapshot(installation) == before
    assert not marker.exists()
    assert all(
        sentinel.read_bytes() == b"\xffhelp sentinel must remain unread"
        for sentinel in sentinels.values()
    )
    assert not list(tmp_path.glob(".*-stage-*"))


@pytest.mark.parametrize("skill", WORKFLOWS)
def test_relocated_version_omits_config_io_and_ignores_fixed_sentinel(
    installation: Path, tmp_path: Path, skill: str
):
    script = installation / skill / "scripts" / "pipeline.py"
    sentinel = script.with_name("config.json")
    sentinel.write_bytes(b"\xfffixed sentinel must remain unread")
    before = _tree_snapshot(installation)

    result = _run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            VERSION_CONFIG_DRIVER,
            script,
            "<none>",
            "<none>",
            "--version",
        ],
        tmp_path,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "VERSION_OK"
    assert result.stderr == ""
    assert sentinel.read_bytes() == b"\xfffixed sentinel must remain unread"
    assert _tree_snapshot(installation) == before


@pytest.mark.parametrize("skill", WORKFLOWS)
@pytest.mark.parametrize("location", ["outside", "inside"])
def test_relocated_version_reads_preexisting_explicit_config(
    installation: Path, tmp_path: Path, skill: str, location: str
):
    script = installation / skill / "scripts" / "pipeline.py"
    config = (
        tmp_path / f"{skill}.json"
        if location == "outside"
        else script.with_name("explicit-test-config.json")
    )
    config.write_text('{"probe":"observed"}', encoding="utf-8")

    result = _run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            VERSION_CONFIG_DRIVER,
            script,
            config,
            "observed",
            "--version",
            "--config",
            config,
        ],
        tmp_path,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "VERSION_OK"
    assert result.stderr == ""
    assert config.read_text(encoding="utf-8") == '{"probe":"observed"}'


@pytest.mark.parametrize(
    ("skill", "payload"),
    [
        ("markdown-conversion", '{"pdf_ocr":[]}'),
        ("pdf-conversion", '{"pdf_conversion":{"validation":[]}}'),
        ("file-conversion", '{"pdf_images":[]}'),
    ],
)
def test_relocated_version_rejects_invalid_explicit_config_before_output(
    installation: Path, tmp_path: Path, skill: str, payload: str
):
    script = installation / skill / "scripts" / "pipeline.py"
    config = tmp_path / f"invalid-{skill}.json"
    config.write_text(payload, encoding="utf-8")

    result = _run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            VERSION_CONFIG_DRIVER,
            script,
            config,
            "<none>",
            "--version",
            "--config",
            config,
        ],
        tmp_path,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "ERROR:" in result.stderr
    assert "Traceback" not in result.stderr
    assert config.read_text(encoding="utf-8") == payload


@pytest.mark.parametrize("skill", WORKFLOWS)
def test_relocated_version_rejects_missing_explicit_config_without_creating_it(
    installation: Path, tmp_path: Path, skill: str
):
    script = installation / skill / "scripts" / "pipeline.py"
    config = tmp_path / f"missing-{skill}.json"

    result = _run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            VERSION_CONFIG_DRIVER,
            script,
            config,
            "<none>",
            "--version",
            "--config",
            config,
        ],
        tmp_path,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "ERROR:" in result.stderr
    assert "Traceback" not in result.stderr
    assert not config.exists()


ORIGIN_DRIVER = r"""
import importlib.util, json, pathlib, sys
pipeline_path = pathlib.Path(sys.argv[1])
skills_root = pathlib.Path(sys.argv[2]).resolve()
spec = importlib.util.spec_from_file_location("installed_origin_pipeline", pipeline_path)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
modules = {
    "entry": module,
    "runtime_layout": module._runtime_layout,
    "native_paths": module.np,
    "conversion_runtime": module._conversion_runtime,
    "core_client": module.core,
    "knowledge_unit": module.knowledge_unit,
}
if hasattr(module, "_libreoffice_pdf"):
    modules["libreoffice_pdf"] = module._libreoffice_pdf
if hasattr(module, "markdown_pipeline"):
    modules["markdown_pipeline"] = module.markdown_pipeline
origins = {}
for name, loaded in modules.items():
    origin = pathlib.Path(loaded.__file__).resolve()
    assert origin.is_relative_to(skills_root), (name, origin, skills_root)
    origins[name] = str(origin)
assert pathlib.Path(module.np.__file__).resolve() == (
    skills_root / "file-processing" / "scripts" / "native_paths.py"
).resolve()
assert pathlib.Path(module._conversion_runtime.__file__).resolve() == (
    skills_root / "file-processing" / "scripts" / "conversion_runtime.py"
).resolve()
print(json.dumps(origins, ensure_ascii=True, sort_keys=True))
"""


@pytest.mark.parametrize("skill", WORKFLOWS)
def test_project_module_origins_stay_in_relocated_installation(
    installation: Path, tmp_path: Path, skill: str
):
    unrelated = tmp_path / "origin cwd"
    unrelated.mkdir()
    result = _run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            ORIGIN_DRIVER,
            installation / skill / "scripts" / "pipeline.py",
            installation,
        ],
        unrelated,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    origins = json.loads(result.stdout)
    assert origins
    assert all(str(installation.resolve()) in origin for origin in origins.values())


@pytest.mark.parametrize(
    ("skill", "fault"),
    [
        *[(skill, "carrier") for skill in WORKFLOWS],
        *[(skill, "runtime") for skill in WORKFLOWS],
        ("markdown-conversion", "worker"),
        ("pdf-conversion", "worker"),
        ("file-conversion", "worker"),
        ("pdf-conversion", "markdown"),
        ("file-conversion", "markdown"),
    ],
)
def test_incomplete_installation_fails_before_config_provider_stage_or_output(
    installation: Path, tmp_path: Path, skill: str, fault: str
):
    carrier = installation / "file-processing"
    if fault == "carrier":
        shutil.rmtree(carrier)
    elif fault == "runtime":
        (carrier / "scripts" / "conversion_runtime.py").unlink()
    elif fault == "worker":
        if skill == "markdown-conversion":
            (installation / skill / "scripts" / "provider_worker.py").unlink()
        else:
            (carrier / "scripts" / "pdf_validation_worker.py").unlink()
    elif fault == "markdown":
        shutil.rmtree(installation / "markdown-conversion")
    else:
        raise AssertionError(fault)

    unrelated = tmp_path / "failure cwd"
    unrelated.mkdir()
    marker = unrelated / "decoy-loaded"
    decoy = unrelated / "file-processing" / "scripts"
    decoy.mkdir(parents=True)
    (decoy / "runtime_layout.py").write_text(
        "from pathlib import Path\n"
        + "Path("
        + repr(str(marker))
        + ").write_text('loaded', encoding='utf-8')\n",
        encoding="utf-8",
    )
    source = tmp_path / "source.docx"
    source.write_bytes(b"provider must not receive these bytes")
    config = tmp_path / "new-config.json"
    output = tmp_path / "output"
    script = installation / skill / "scripts" / "pipeline.py"
    result = _run(
        [
            sys.executable,
            "-I",
            "-B",
            script,
            "--config",
            config,
            "--input",
            source,
            "--output-dir",
            output,
        ],
        unrelated,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    error = result.stderr.lower()
    assert skill in error
    assert "skills root:" in error
    assert "required path:" in error
    assert "restore the complete unified installation" in error
    assert not config.exists()
    assert not output.exists()
    assert not marker.exists()
    assert source.read_bytes() == b"provider must not receive these bytes"
    assert not list(tmp_path.glob(".*-stage-*"))


@pytest.mark.parametrize("skill", WORKFLOWS)
def test_runtime_symlink_escape_is_rejected_before_business_writes(
    installation: Path, tmp_path: Path, skill: str
):
    runtime = installation / "file-processing" / "scripts" / "conversion_runtime.py"
    outside = tmp_path / "outside-runtime.py"
    outside.write_bytes(runtime.read_bytes())
    runtime.unlink()
    try:
        runtime.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    config = tmp_path / "new-config.json"
    output = tmp_path / "output"
    source = tmp_path / "source.docx"
    source.write_bytes(b"not converted")
    result = _run(
        [
            sys.executable,
            "-I",
            "-B",
            installation / skill / "scripts" / "pipeline.py",
            "--config",
            config,
            "--input",
            source,
            "--output-dir",
            output,
        ],
        tmp_path,
    )
    assert result.returncode == 1
    assert "link or windows reparse point is forbidden" in result.stderr.lower()
    assert str(installation) in result.stderr
    assert not config.exists() and not output.exists()
    assert source.read_bytes() == b"not converted"

def _make_junction(link: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode:
        pytest.skip(f"directory junctions unavailable: {completed.stderr}")


@pytest.mark.parametrize("skill", WORKFLOWS)
def test_runtime_carrier_junction_is_rejected_before_business_writes(
    installation: Path, tmp_path: Path, skill: str
):
    carrier = installation / "file-processing"
    outside = tmp_path / "outside-carrier"
    carrier.rename(outside)
    _make_junction(carrier, outside)

    config = tmp_path / "new-config.json"
    output = tmp_path / "output"
    source = tmp_path / "source.docx"
    source.write_bytes(b"not converted")
    result = _run(
        [
            sys.executable,
            "-I",
            "-B",
            installation / skill / "scripts" / "pipeline.py",
            "--config",
            config,
            "--input",
            source,
            "--output-dir",
            output,
        ],
        tmp_path,
    )
    assert result.returncode == 1
    assert "link or windows reparse point is forbidden" in result.stderr.lower()
    assert str(carrier) in result.stderr
    assert not config.exists() and not output.exists()
    assert source.read_bytes() == b"not converted"
