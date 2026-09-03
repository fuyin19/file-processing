"""Core installation boundaries; all provider work is synthetic or forbidden.

sc-002/003: relocated real pipeline main and real Core, provider-only substitutes.
sc-004/005/007: real CLI failures before config, provider or publication writes.
sc-006: operation binding survives environment changes and nested Markdown use.
sc-010: deterministic generated-client check and matching release metadata.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).absolute().parents[2]
SKILLS = ("markdown-conversion", "pdf-conversion", "file-conversion")
ABI = "anti-entropy-core.runner/v1"
VERSION = "1.2.1"


def _load_client(path: Path):
    name = "installation_core_client"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def installation(tmp_path, monkeypatch):
    configured = os.environ.get("FILE_PROCESSING_REAL_CORE_RUNNER")
    assert configured, "Set FILE_PROCESSING_REAL_CORE_RUNNER to the current Core Candidate runner"
    core = Path(configured).parent.parent
    assert (core / "SKILL.md").is_file(), "The actual installable Core skill is required"
    original = tmp_path / "original"
    shutil.copytree(ROOT / "skills", original / "skills", ignore=shutil.ignore_patterns("__pycache__", "config.json"))
    shutil.copytree(core, original / "skills" / "anti-entropy-core", ignore=shutil.ignore_patterns("__pycache__"))
    moved = tmp_path / "moved installation 中文"
    original.rename(moved)
    monkeypatch.delenv("ANTI_ENTROPY_CORE_RUNNER", raising=False)
    return moved / "skills"


def _runner(path: Path, *, version=VERSION, abi=ABI, malformed=False, log: Path | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    source = "import json, sys\nfrom pathlib import Path\n"
    source += "incoming=json.loads(sys.stdin.readline())\n"
    if log:
        source += f"with Path({str(log)!r}).open('a', encoding='utf-8') as stream: stream.write(incoming['command']+'\\n')\n"
    if malformed:
        source += "print('{}')\n"
    else:
        data = {} if version is None else {"version": version}
        source += f"print(json.dumps({{'abi':{abi!r},'status':'ok','exit_code':0,'command':incoming['command'],'data':{data!r},'issues':[]}}))\n"
    path.write_text(source, encoding="utf-8")
    return path


def _run(command, *, cwd: Path, env=None):
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=90)


def _cli_environment(tmp_path: Path, allowed: list[Path]):
    """Prevent a broken negative test from accidentally launching a provider."""
    guard = tmp_path / "guard"
    guard.mkdir(exist_ok=True)
    (guard / "sitecustomize.py").write_text(
        "import json, os, subprocess, sys\n"
        "allowed=json.loads(os.environ['TEST_CORE_RUNNERS'])\n"
        "commands=[subprocess.list2cmdline([sys.executable,'-I',path]) for path in allowed]\n"
        "def guard(event, args):\n"
        "    if event == 'subprocess.Popen':\n"
        "        argv=args[1]\n"
        "        valid=argv in commands if isinstance(argv,str) else len(argv)==3 and argv[1]=='-I' and argv[2] in allowed\n"
        "        if not valid:\n"
        "            raise RuntimeError('Real provider execution is forbidden in this test')\n"
        "sys.addaudithook(guard)\n", encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(guard)
    env["TEST_CORE_RUNNERS"] = json.dumps([str(path) for path in allowed])
    return env


# This driver leaves pipeline routing, client selection, preflight, real Core,
# staging and publication intact. Only provider extraction/rendering is replaced.
DRIVER = r'''
import builtins, importlib.util, json, os, sys
from pathlib import Path
sys.dont_write_bytecode = True
pipeline_path, expected, source, output, config, mode = sys.argv[1:]
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == "anti_entropy_core" or name.startswith("anti_entropy_core."):
        raise AssertionError("Consumer imported Core implementation")
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
spec = importlib.util.spec_from_file_location("installed_pipeline", pipeline_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
assert Path(module.core.__file__).parent == Path(pipeline_path).parent
observations = []
def observe():
    active = module.core._ACTIVE_BINDING.get()
    if mode == "bundle":
        assert active is not None and active.path == Path(expected)
        observations.append(str(active.path))
        # Later nested helpers must not rebind to this broken override.
        os.environ["ANTI_ENTROPY_CORE_RUNNER"] = str(Path(output) / "missing-core.py")
    else:
        assert active is None
        observations.append("direct")
markdown = getattr(module, "markdown_pipeline", module)
def extraction(request, **kwargs):
    observe()
    result = markdown.markdown_to_canonical("# Synthetic\n\nTest body.\n", request["document_id"], "preserve", "docx")
    result["adapter"] = {"name":"test-provider", "version":"fixture", "limitations":[]}
    return result
markdown._run_provider_worker = extraction
if hasattr(module, "_engine"):
    class Engine:
        settings = module.PdfConversionSettings()
        def convert(self, snapshot, destination, workspace):
            observe()
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(b"%PDF-synthetic-test-only\n")
            return {"pages":1}
    module._engine = lambda args, config: Engine()
    module.validate_pdf = lambda *args: {"pages":1}
    module.verify_validated_pdf = lambda *args: None
argv = [pipeline_path, "--input", source, "--output-dir", output, "--config", config]
if "--ocr" in module.build_parser()._option_string_actions:
    argv += ["--ocr", "off", "--language-normalization", "preserve"]
if mode != "bundle":
    argv += ["--output-mode", mode]
sys.argv = argv
code = module.main()
assert module.core._ACTIVE_BINDING.get() is None
assert observations, "The real main must reach the synthetic provider"
print("OBSERVATIONS=" + json.dumps(observations))
raise SystemExit(code)
'''


@pytest.mark.parametrize("skill", SKILLS)
@pytest.mark.parametrize("override", [False, True])
def test_relocated_pipeline_main_uses_real_core_and_one_binding(installation, tmp_path, skill, override):
    scripts = installation / skill / "scripts"
    expected = installation / "anti-entropy-core" / "scripts" / "knowledge_unit_runner.py"
    env = dict(os.environ)
    default_calls = tmp_path / "default-runner-calls.log"
    if override:
        other = tmp_path / "another skills root" / "anti-entropy-core"
        shutil.copytree(expected.parent.parent, other)
        expected = other / "scripts" / expected.name
        env["ANTI_ENTROPY_CORE_RUNNER"] = str(expected)
        # Both A and B are valid real Core 1.2.1 runners. Instrument A without
        # changing its behavior so even a discarded capabilities probe is seen.
        default = installation / "anti-entropy-core" / "scripts" / expected.name
        source = default.read_text(encoding="utf-8")
        source = source.replace(
            "from __future__ import annotations",
            "from __future__ import annotations\n"
            f"with open({str(default_calls)!r}, 'a', encoding='utf-8') as stream: stream.write('called\\n')",
            1,
        )
        default.write_text(source, encoding="utf-8")
    unrelated = tmp_path / "unrelated cwd"
    unrelated.mkdir()
    _runner(unrelated / "anti-entropy-core" / "scripts" / "knowledge_unit_runner.py", version="wrong-root")
    env["PATH"] = str(unrelated) + os.pathsep + env.get("PATH", "")
    source = tmp_path / "source.docx"
    source.write_bytes(b"Synthetic source; providers do not parse these bytes")
    output = tmp_path / "output"
    config = tmp_path / "new-config.json"
    result = _run([sys.executable, "-I", "-c", DRIVER, str(scripts / "pipeline.py"), str(expected), str(source), str(output), str(config), "bundle"], cwd=unrelated, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OBSERVATIONS=" in result.stdout
    assert (output / "source" / "AGENTS.md").is_file()
    assert (output / "source" / "src" / source.name).read_bytes() == source.read_bytes()
    assert config.is_file()
    assert not default_calls.exists(), "Explicit B must not probe or execute valid default A"


@pytest.mark.parametrize("skill,mode", [("markdown-conversion", "markdown"), ("pdf-conversion", "pdf")])
def test_direct_main_has_no_core_prerequisite(installation, tmp_path, skill, mode):
    env = dict(os.environ, ANTI_ENTROPY_CORE_RUNNER="")
    source = tmp_path / "source.docx"
    source.write_bytes(b"Synthetic input")
    result = _run([sys.executable, "-I", "-c", DRIVER, str(installation / skill / "scripts" / "pipeline.py"), "unused", str(source), str(tmp_path / "out"), str(tmp_path / "config.json"), mode], cwd=tmp_path, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"direct"' in result.stdout


@pytest.mark.parametrize("skill", SKILLS)
@pytest.mark.parametrize("fault", ["empty", "relative", "missing", "directory", "default-missing", "default-directory", "bad-abi", "older", "newer", "missing-version", "invalid-version", "malformed"])
def test_real_cli_rejects_core_before_config_provider_or_output(installation, tmp_path, skill, fault):
    default = installation / "anti-entropy-core" / "scripts" / "knowledge_unit_runner.py"
    selected = tmp_path / "selected.py"
    configured = str(selected)
    if fault == "empty":
        configured = ""
    elif fault == "relative":
        configured = "relative.py"
    elif fault in {"directory", "default-directory"}:
        if fault.startswith("default"):
            default.unlink()
            default.mkdir()
        else:
            selected.mkdir()
    elif fault == "default-missing":
        default.unlink()
    elif fault == "bad-abi":
        _runner(selected, abi="other/v1")
    elif fault in {"older", "newer", "missing-version", "invalid-version"}:
        _runner(selected, version={"older":"1.2.0", "newer":"1.2.2", "missing-version":None, "invalid-version":7}[fault])
    elif fault == "malformed":
        _runner(selected, malformed=True)
    env = _cli_environment(tmp_path, [selected, default])
    if fault.startswith("default"):
        env.pop("ANTI_ENTROPY_CORE_RUNNER", None)
    else:
        env["ANTI_ENTROPY_CORE_RUNNER"] = configured
    source = tmp_path / "synthetic.docx"
    source.write_bytes(b"Never sent to a provider")
    output = tmp_path / "out"
    config = tmp_path / "missing-config.json"
    result = _run([sys.executable, str(installation / skill / "scripts" / "pipeline.py"), "--input", str(source), "--output-dir", str(output), "--config", str(config)], cwd=tmp_path, env=env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "expected ABI=" in result.stderr and VERSION in result.stderr
    assert "Install/update" in result.stderr
    if fault in {"older", "newer", "missing-version", "invalid-version"}:
        assert "actual=" in result.stderr and "version mismatch" in result.stderr
    assert not config.exists() and not output.exists()
    assert not any(tmp_path.glob(".*-stage-*"))
    assert source.read_bytes() == b"Never sent to a provider"


def test_client_alone_and_core_are_relocatable_without_shared(installation, tmp_path, monkeypatch):
    only = tmp_path / "only skills"
    scripts = only / "pdf-conversion" / "scripts"
    scripts.mkdir(parents=True)
    (scripts.parent / "SKILL.md").write_text("test boundary", encoding="utf-8")
    (scripts / "pipeline.py").write_text("# anchor", encoding="utf-8")
    shutil.copyfile(installation / "pdf-conversion" / "scripts" / "anti_entropy_core_adapter.py", scripts / "anti_entropy_core_adapter.py")
    shutil.copytree(installation / "anti-entropy-core", only / "anti-entropy-core")
    code = '''import sys\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nimport anti_entropy_core_adapter as core\nwith core.operation(skill_entrypoint=Path(sys.argv[1])/'pipeline.py',skill_id='pdf-conversion'):\n assert core.capabilities().data['version']=='1.2.1'\n assert not any(n=='anti_entropy_core' or n.startswith('anti_entropy_core.') for n in sys.modules)\n'''
    result = _run([sys.executable, "-I", "-S", "-c", code, str(scripts)], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert not (only / "_shared").exists()


def test_context_preflights_once_restores_and_next_operation_rebinds(tmp_path, monkeypatch):
    core = _load_client(ROOT / "skills" / "pdf-conversion" / "scripts" / "anti_entropy_core_adapter.py")
    first_log, next_log = tmp_path / "first.log", tmp_path / "next.log"
    first = _runner(tmp_path / "first.py", log=first_log)
    later = _runner(tmp_path / "next.py", log=next_log)
    monkeypatch.setenv(core.RUNNER_ENV, str(first))
    with pytest.raises(RuntimeError, match="caller failure"):
        with core.operation() as binding:
            monkeypatch.setenv(core.RUNNER_ENV, str(later))
            assert core.capabilities().data["version"] == VERSION
            with core.operation(runner=later) as nested:
                assert nested is binding
                core.inspect(tmp_path)
            core.validate(tmp_path)
            raise RuntimeError("caller failure")
    assert core._ACTIVE_BINDING.get() is None
    core.capabilities()
    assert first_log.read_text().splitlines() == ["capabilities", "inspect", "validate"]
    assert next_log.read_text().splitlines() == ["capabilities"]


def test_capabilities_has_thirty_second_deadline(tmp_path, monkeypatch):
    core = _load_client(ROOT / "skills" / "pdf-conversion" / "scripts" / "anti_entropy_core_adapter.py")
    runner = _runner(tmp_path / "runner.py")
    monkeypatch.setenv(core.RUNNER_ENV, str(runner))
    def timeout(argv, **kwargs):
        assert argv == [sys.executable, "-I", str(runner)]
        assert kwargs["timeout"] == 30
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
    monkeypatch.setattr(core.subprocess, "run", timeout)
    with pytest.raises(core.CoreAdapterError, match="timed out after 30"):
        core.capabilities()
    assert core._ACTIVE_BINDING.get() is None


def test_reparse_components_are_rejected_without_resolving(tmp_path, monkeypatch):
    core = _load_client(ROOT / "skills" / "pdf-conversion" / "scripts" / "anti_entropy_core_adapter.py")
    runner = _runner(tmp_path / "runner.py")
    original = Path.lstat
    def reparse(path):
        info = original(path)
        if path == tmp_path:
            class Reparse:
                st_mode = info.st_mode
                st_file_attributes = 0x400
            return Reparse()
        return info
    monkeypatch.setattr(Path, "lstat", reparse)
    monkeypatch.setenv(core.RUNNER_ENV, str(runner))
    with pytest.raises(core.CoreAdapterError, match="link/reparse"):
        core.capabilities()


@pytest.mark.parametrize("skill", SKILLS)
def test_help_does_not_require_core_or_create_config(installation, tmp_path, skill):
    env = _cli_environment(tmp_path, [])
    env["ANTI_ENTROPY_CORE_RUNNER"] = ""
    script = installation / skill / "scripts" / "pipeline.py"
    result = _run([sys.executable, str(script), "--help"], cwd=tmp_path, env=env)
    assert result.returncode == 0, result.stderr
    assert not script.with_name("config.json").exists()


@pytest.mark.parametrize("skill", SKILLS)
def test_version_main_does_not_require_core(installation, tmp_path, skill):
    # Version reporting normally probes installed providers; replace that probe,
    # retaining the actual parser/main branch and its Core prerequisite behavior.
    code = '''import importlib.util, sys\nfrom pathlib import Path\nsys.dont_write_bytecode=True\np=Path(sys.argv[1])\ns=importlib.util.spec_from_file_location('version_pipeline',p)\nm=importlib.util.module_from_spec(s)\nsys.modules[s.name]=m\ns.loader.exec_module(m)\nm.show_version=lambda *args: print(m.VERSION)\nsys.argv=[str(p),'--version','--config',sys.argv[2]]\nassert m.main()==0\nassert m.core._ACTIVE_BINDING.get() is None\n'''
    env = dict(os.environ, ANTI_ENTROPY_CORE_RUNNER="")
    result = _run([sys.executable, "-I", "-c", code, str(installation / skill / "scripts" / "pipeline.py"), str(tmp_path / "config.json")], cwd=tmp_path, env=env)
    assert result.returncode == 0, result.stderr


def test_generated_clients_are_identical_and_check_is_read_only():
    source = (ROOT / "skills" / "_shared" / "scripts" / "anti_entropy_core_adapter.py").read_bytes()
    targets = [ROOT / "skills" / skill / "scripts" / "anti_entropy_core_adapter.py" for skill in SKILLS]
    assert all(path.read_bytes() == source for path in targets)
    result = _run([sys.executable, "-I", "-S", str(ROOT / "tools" / "sync_core_clients.py"), "--check"], cwd=ROOT)
    assert result.returncode == 0, result.stderr
    assert all(path.read_bytes() == source for path in targets)
