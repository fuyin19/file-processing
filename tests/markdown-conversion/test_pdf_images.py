"""Semantic acceptance for optional PDF figures, using invented local fixtures.

These focused checks are part of the ordinary Markdown conversion suite. Real
document visual coverage and end-to-end timing require separate task evidence.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "markdown-conversion" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pdf_image_fixtures import DOCUMENT_ID, body_projection, body_result, figure_pdf

SCRIPT = [sys.executable, str(SCRIPTS / "pipeline.py")]
CONFIG_ARG = ["--config", str(Path(__file__).parent / "fixtures" / "test_config.json")]


def _text(value, kind="paragraph"):
    return {"type": kind, "raw_text": value, "text": value,
            "source_locator": {"page_range": [1, 4]}}


def _image(name):
    return {"type": "image", "asset_id": name, "source_locator": {"page": 1}}


def _legacy_replay(base, supports, units):
    from pdf_inspector_adapter import _merge_support_images
    content, counts = base, {}
    for page, support in sorted(supports.items()):
        content, counts[page] = _merge_support_images(content, support, units[page])
    return content, counts


def _cache_cases():
    long_anchor = "twelvelettersanchor"
    yield "duplicate", [_text("A"), _text("A"), _text("B")], {
        1: [_text("A"), _image("ambiguous"), _text("B")]}
    yield "suffix", [_text(long_anchor), _text("prefix" + long_anchor), _text("B")], {
        1: [_text(long_anchor), _image("ambiguous"), _text("B")]}
    yield "prefix", [_text(long_anchor), _text(long_anchor + "suffix")], {
        1: [_image("ambiguous"), _text(long_anchor)]}
    yield "short_suffix", [_text("prefixabcdefghijk"), _text("B")], {
        1: [_text("abcdefghijk"), _image("ambiguous"), _text("B")]}
    yield "reversed", [_text("A"), _text("B")], {
        1: [_text("B"), _image("ambiguous"), _text("A")]}
    yield "multipage_slots", [_text("A"), _text("B"), _text("C")], {
        1: [_text("A"), _image("first"), _image("second"), _text("B")],
        2: [_text("A"), _image("later-page"), _text("B")],
        3: [_image("before-first"), _text("A")],
        4: [_text("C"), _image("after-last")]}
    yield "long_partial", [_text("prefix" + long_anchor), _text(long_anchor + "suffix")], {
        1: [_text(long_anchor), _image("placed"), _text(long_anchor)]}
    yield "empty", [_text("A"), _text("B")], {
        1: [_text("A"), _text(" \n"), _image("placed"), _text("B")],
        2: [_text("C")]}
    same_image = _image("alias")
    yield "image_alias", [_text("A"), _text("B"), _text("C")], {
        1: [_text("A"), same_image, _text("B"), same_image, _text("C")]}
    same_text = _text("A")
    yield "body_alias", [same_text, same_text, _text("B")], {
        1: [_text("A"), _image("ambiguous"), _text("B")]}


@pytest.mark.parametrize("name,base,supports", list(_cache_cases()),
                         ids=lambda value: value if isinstance(value, str) else None)
def test_cached_object_placement_preserves_legacy_semantics(name, base, supports):
    from pdf_images import merge_cached
    units = {page: {"id": f"unit-{page}"} for page in supports}
    before = copy.deepcopy((base, supports, units))
    expected, unpositioned = _legacy_replay(base, supports, units)
    actual, stats = merge_cached(base, supports, units)
    assert actual == expected
    assert stats["unpositioned_images"] == sum(unpositioned.values())
    assert (base, supports, units) == before
    assert [node for node in actual if node["type"] != "image"] == base
    if name == "multipage_slots":
        assert [node["asset_id"] for node in actual if node["type"] == "image"] == [
            "before-first", "later-page", "first", "second", "after-last"]


def test_cached_object_matching_computes_each_fingerprint_and_query_once():
    from pdf_images import merge_cached
    base = [_text(f"Unique body paragraph {index}.") for index in range(400)]
    supports = {page: [_text(base[15]["raw_text"]), *[_image(f"{page}-{i}") for i in range(20)],
                       _text(base[16]["raw_text"])] for page in range(1, 9)}
    units = {page: {"id": f"unit-{page}"} for page in supports}
    content, stats = merge_cached(base, supports, units)
    assert stats["fingerprint_computations"] == 400 + 2 * 8
    assert stats["cache_misses"] == stats["query_cache_entries"] == 2
    assert stats["cache_hits"] == 20 * 8 * 2 - 2
    assert stats["inserted_images"] == 160
    assert len(content) == 560
    changed = [_text("The prior query is absent.")]
    _, next_stats = merge_cached(changed, supports, units)
    assert next_stats["inserted_images"] == 0  # No cache survives a document.


def test_objects_mode_keeps_final_ids_markdown_and_ocr_supplement_order(monkeypatch, tmp_path):
    import canonical
    import pdf_images
    import pdf_inspector_adapter
    import provider_worker
    from PIL import Image

    body = body_result("First unique anchor.\n\nSecond unique anchor.")
    ocr = copy.deepcopy(body["content"][0])
    ocr["id"] = "node-" + "7" * 16
    ocr["source_locator"] = {"source_unit_id": "unit-0000000000000001", "page": 1,
                              "extraction_method": "ocr", "placement": "unanchored_supplement"}
    body["content"].append(ocr)
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    asset_id = "asset-" + "6" * 16
    asset_path = image_dir / f"{asset_id}.png"
    Image.new("RGB", (12, 10), "blue").save(asset_path)
    locator = {"page": 1, "bbox": [50, 50, 100, 100], "source_unit_id": "unit-0000000000000001"}
    image_node = {"id": "node-" + "6" * 16, "type": "image", "asset_id": asset_id, "source_locator": locator}
    support = {"content": [dict(_text("First unique anchor."), source_locator=locator), image_node,
                            dict(_text("Second unique anchor."), source_locator=locator)],
               "assets": [{"asset_id": asset_id, "type": "image", "path": f"assets/images/{asset_id}.png",
                           "sha256": hashlib.sha256(asset_path.read_bytes()).hexdigest(),
                           "media_type": "image/png", "source_locator": locator, "alt": "Synthetic object", "caption": ""}],
               "warnings": []}
    monkeypatch.setattr(pdf_inspector_adapter, "_extract_pdf_image_support", lambda *args, **kwargs: support)
    before = copy.deepcopy(body)
    expected = copy.deepcopy(body)
    expected["content"], ambiguous = pdf_inspector_adapter._merge_support_images(
        expected["content"][:-1], support["content"], expected["source_units"][1])
    expected["content"].append(copy.deepcopy(ocr))
    expected["assets"] = copy.deepcopy(support["assets"])
    pdf_inspector_adapter._reassign_content_ids(expected["content"], expected["tables"], DOCUMENT_ID)
    actual = pdf_images.enhance_pdf_images(tmp_path / "not-read.pdf", body, image_dir,
                                         {"mode": "objects", "document_id": DOCUMENT_ID}, time.monotonic() + 20)
    assert ambiguous == 0 and body == before
    assert {key: value for key, value in actual.items() if key != "image_metrics"} == expected
    assert actual["content"][-1]["source_locator"]["placement"] == "unanchored_supplement"
    expected_document = provider_worker._extraction_document(expected, DOCUMENT_ID)
    actual_document = provider_worker._extraction_document(actual, DOCUMENT_ID)
    canonical.validate_canonical(actual_document)
    assert canonical.render_markdown(actual_document) == canonical.render_markdown(expected_document)


def _enhance(monkeypatch, tmp_path, *, settings=None, **fixture_options):
    import pdf_images
    source, body, items, annotations = figure_pdf(tmp_path / "invented.pdf", **fixture_options)
    position_calls = []

    def positions(_source, pages):
        position_calls.append(list(pages))
        return [item for item in items if item["page"] in pages]

    monkeypatch.setattr(pdf_images, "_position_items", positions)
    before = copy.deepcopy(body)
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    result = pdf_images.enhance_pdf_images(
        str(source), body, image_dir,
        {"mode": "auto", "document_id": DOCUMENT_ID, "normalization": "preserve", **(settings or {})},
        time.monotonic() + 30,
    )
    assert body == before
    assert body_projection(result) == body_projection(before)
    for pages in position_calls:
        assert pages == sorted(set(pages)) and len(pages) <= 32
    assert [p for batch in position_calls for p in batch] == sorted(
        p for batch in position_calls for p in batch)
    return result, annotations, image_dir


def _images(result, placement=None):
    return [node for node in result["content"] if node["type"] == "image"
            and (placement is None or node["source_locator"].get("placement") == placement)]


def _asset_file(result, node, image_dir):
    asset = next(asset for asset in result["assets"] if asset["asset_id"] == node["asset_id"])
    file = image_dir / Path(asset["path"]).name
    assert file.is_file()
    assert hashlib.sha256(file.read_bytes()).hexdigest() == asset["sha256"]
    return file


def _contains(outer, inner, tolerance=2):
    return (outer[0] <= inner[0] + tolerance and outer[1] <= inner[1] + tolerance
            and outer[2] >= inner[2] - tolerance and outer[3] >= inner[3] - tolerance)


def test_complete_figure_is_placed_between_proven_paragraphs(monkeypatch, tmp_path):
    result, annotations, image_dir = _enhance(monkeypatch, tmp_path)
    precise = _images(result, "body_region")
    assert precise, "A clear complete region between unique body anchors must be placed."
    assert any(_contains(node["source_locator"]["bbox"], annotations[1]["bbox"]) for node in precise)
    assert [node["type"] for node in result["content"]] == ["paragraph", "image", "paragraph"]
    assert result["image_metrics"]["render_calls"] == 1
    from PIL import Image
    with Image.open(_asset_file(result, precise[0], image_dir)) as raster:
        assert raster.width > 200 and raster.height > 100
        assert raster.mode in {"RGB", "RGBA"}
        if raster.mode == "RGBA":
            assert raster.getchannel("A").getextrema() == (255, 255)
        colors = list(raster.convert("RGB").getdata())
        assert any(r > 150 and r > g * 1.4 and r > b * 1.4 for r, g, b in colors)
        assert any(b > 150 and b > r * 1.4 and b > g * 1.4 for r, g, b in colors)


def test_complete_table_figure_follows_whole_original_table(monkeypatch, tmp_path):
    result, annotations, _ = _enhance(monkeypatch, tmp_path, table=True)
    precise = _images(result, "body_region")
    assert precise, "Distinct ordered table cells establish the existing table occurrence."
    table_index = next(i for i, node in enumerate(result["content"]) if node["type"] == "table")
    assert result["content"][table_index + 1]["type"] == "image"
    assert any(_contains(node["source_locator"]["bbox"], annotations[1]["bbox"]) for node in precise)


def test_detached_panels_and_offset_caption_are_not_cropped_away(monkeypatch, tmp_path):
    result, annotations, image_dir = _enhance(monkeypatch, tmp_path, detached=True)
    nodes = _images(result)
    assert nodes
    # A crop must cover the source-annotated entire composition. A page preview
    # is acceptable when the detached components make its boundary uncertain.
    assert any(_contains(node["source_locator"]["bbox"], annotations[1]["bbox"]) for node in nodes)
    assert result["image_metrics"]["render_calls"] == 1
    for node in nodes:
        _asset_file(result, node, image_dir)


@pytest.mark.parametrize("caption_style", ["small", "offset"])
def test_matched_multiline_caption_cannot_cut_off_a_detached_panel(monkeypatch, tmp_path, caption_style):
    result, annotations, image_dir = _enhance(monkeypatch, tmp_path, caption_style=caption_style)
    caption = "Figure note for both panels.\nRead the panels together."
    assert any(node.get("raw_text") == caption for node in result["content"])
    nodes = _images(result)
    assert nodes
    # The source composition is one figure, even though the two-line caption
    # matches a complete canonical paragraph between its disconnected panels.
    # No precise crop may split that figure at this apparent prose boundary.
    assert all(_contains(node["source_locator"]["bbox"], annotations[1]["bbox"])
               for node in _images(result, "body_region"))
    assert any(_contains(node["source_locator"]["bbox"], annotations[1]["bbox"]) for node in nodes)
    assert result["image_metrics"]["render_calls"] == 1
    for node in nodes:
        _asset_file(result, node, image_dir)


def test_vector_only_pages_are_candidates_and_rendered_once(monkeypatch, tmp_path):
    result, annotations, _ = _enhance(monkeypatch, tmp_path, vector_only=True, pages=3)
    assert result["image_metrics"]["pages_scanned"] == 3
    assert result["image_metrics"]["candidate_pages"] == [1, 2, 3]
    assert result["image_metrics"]["render_calls"] == 3
    assert {node["source_locator"]["page"] for node in _images(result)} == set(annotations)


def test_repeated_anchors_use_labelled_page_supplements(monkeypatch, tmp_path):
    result, _, _ = _enhance(monkeypatch, tmp_path, ambiguous=True, pages=2)
    assert not _images(result, "body_region")
    supplements = _images(result, "pdf_page_supplement")
    assert [node["source_locator"]["page"] for node in supplements] == [1, 2]
    assert result["content"][-2:] == supplements
    warnings = [warning for warning in result["warnings"] if warning["code"] == "pdf_images_page_supplement"]
    assert warnings and all(not warning.get("content_loss", False) for warning in warnings)
    assert result["image_metrics"]["precise_regions"] == 0


def test_inline_image_glyph_uses_page_preview_without_text_repair(monkeypatch, tmp_path):
    result, _, _ = _enhance(monkeypatch, tmp_path, inline=True)
    assert not _images(result, "body_region")
    assert len(_images(result, "pdf_page_supplement")) == 1
    assert any(warning["code"] == "pdf_inline_image_unrecovered" and warning["content_loss"]
               for warning in result["warnings"])
    assert not any(node["source_locator"].get("extraction_method") == "ocr" for node in result["content"])


def test_rotated_cropbox_preserves_visible_composited_graphics(monkeypatch, tmp_path):
    result, _, image_dir = _enhance(monkeypatch, tmp_path, vector_only=True, rotate_crop=True)
    nodes = _images(result)
    assert nodes and result["image_metrics"]["render_calls"] == 1
    from PIL import Image
    with Image.open(_asset_file(result, nodes[0], image_dir)) as raster:
        colors = list(raster.convert("RGB").getdata())
        assert any(b > 150 and b > r * 1.4 and b > g * 1.4 for r, g, b in colors)
        assert max(raster.size) <= 4096
        assert raster.width > raster.height
        assert raster.width / raster.height == pytest.approx(400 / 360, abs=0.002)


@pytest.mark.parametrize("mode", ["auto", "objects", "off"])
def test_image_settings_default_and_cli_override_are_independent_of_ocr(mode):
    import pipeline
    args = SimpleNamespace(pdf_images=None, pdf_image_timeout=None, ocr="force")
    assert pipeline.resolve_pdf_image_settings(args, {}) == {"mode": "auto", "timeout_seconds": 1000.0}
    args.pdf_images, args.pdf_image_timeout = mode, 9
    assert pipeline.resolve_pdf_image_settings(args, {"pdf_images": {"mode": "off", "timeout_seconds": 3}}) == {
        "mode": mode, "timeout_seconds": 9.0}
    assert pipeline.PROVIDER_TIMEOUT_SECONDS == 1000


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), "bad", None])
def test_invalid_config_image_budget_is_rejected(timeout):
    import pipeline
    with pytest.raises(SystemExit):
        pipeline.resolve_pdf_image_settings(SimpleNamespace(), {"pdf_images": {"timeout_seconds": timeout}})


@pytest.mark.parametrize("entry", [SCRIPTS / "pipeline.py", ROOT / "skills" / "file-conversion" / "scripts" / "pipeline.py"])
def test_public_commands_expose_same_pdf_image_options(entry):
    completed = subprocess.run([sys.executable, str(entry), *CONFIG_ARG, "--help"],
                               capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert completed.returncode == 0, completed.stderr
    assert "--pdf-images" in completed.stdout and "--pdf-image-timeout" in completed.stdout


def _blocking_worker(tmp_path, phase, synthetic_body_path=None):
    """Exercise the real worker under its real supervisor, stalling one phase."""
    marker = tmp_path / "entered-phase.json"
    shim = tmp_path / "blocking-worker.py"
    shim.write_text(
        "import json, os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import provider_worker as worker\n"
        "import pdf_images, pdf_inspector_adapter\n"
        + (f"worker.PdfInspectorAdapter.extract = lambda *a, **k: json.loads(Path({str(synthetic_body_path)!r}).read_text(encoding='utf-8'))\n"
           if synthetic_body_path else "") +
        "if '--image-body' in sys.argv:\n"
        f"    phase = {phase!r}\n"
        "    def block(*args, **kwargs):\n"
        "        data = {'pid': os.getpid()}\n"
        "        if phase == 'write':\n"
        "            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "            data['child_pid'] = child.pid\n"
        f"        Path({str(marker)!r}).write_text(json.dumps(data), encoding='utf-8')\n"
        "        time.sleep(60)\n"
        "        raise RuntimeError('blocked test phase unexpectedly completed')\n"
        "    if phase == 'read': worker._read_image_body = block\n"
        "    elif phase == 'parse': worker.json.loads = block\n"
        "    elif phase == 'clone': worker.copy.deepcopy = block\n"
        "    elif phase == 'positions': pdf_images._position_items = block\n"
        "    elif phase == 'hash': pdf_images.sha256_file = block\n"
        "    elif phase == 'merge': pdf_inspector_adapter._reassign_content_ids = block\n"
        "    elif phase == 'accept': worker._validate_image_candidate = block\n"
        "    elif phase == 'write': worker._write_image_result = block\n"
        "raise SystemExit(worker.main())\n", encoding="utf-8")
    return shim, marker


def _process_is_running(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.parametrize("phase", ["read", "parse", "clone", "positions", "hash", "merge", "accept", "write"])
def test_every_enhancement_phase_is_inside_the_hard_budget(monkeypatch, tmp_path, phase):
    import pipeline
    source, body, _, _ = figure_pdf(tmp_path / "source.pdf")
    body_path = tmp_path / "body.json"
    body_path.write_text(json.dumps({"ok": True, "result": body}), encoding="utf-8")
    before = body_path.read_bytes()
    shim, marker = _blocking_worker(tmp_path, phase)
    monkeypatch.setattr(pipeline, "PROVIDER_WORKER", shim)
    started = time.monotonic()
    accepted = pipeline._run_pdf_image_worker(
        body_path, tmp_path / "candidate.json", tmp_path / "images", str(source),
        {"mode": "auto", "timeout_seconds": 2,
         "document_id": "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()},
    )
    elapsed = time.monotonic() - started
    assert marker.exists(), f"The test did not reach its intended {phase} phase."
    assert accepted is False and elapsed < 5
    assert body_path.read_bytes() == before
    processes = json.loads(marker.read_text(encoding="utf-8"))
    for pid in processes.values():
        assert not _process_is_running(pid), f"Timed-out enhancement process {pid} survived."


def test_timed_out_enhancement_selects_body_and_never_promotes_partial_images(monkeypatch, tmp_path):
    import pipeline
    source, body, _, _ = figure_pdf(tmp_path / "source.pdf")
    synthetic_body = tmp_path / "synthetic-body.json"
    synthetic_body.write_text(json.dumps(body), encoding="utf-8")
    shim, marker = _blocking_worker(tmp_path, "write", synthetic_body)
    monkeypatch.setattr(pipeline, "PROVIDER_WORKER", shim)
    request = {
        "adapter": "pdf_inspector", "source": str(source),
        "document_id": "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
        "mode": "preserve", "ocr_mode": "off", "ocr_settings": {"mode": "off"},
        "asset_dir": str(tmp_path / "assets" / "images"),
        "pdf_images": {"mode": "off", "timeout_seconds": 2},
    }
    baseline = pipeline._run_provider_worker(request, timeout=15)
    request["pdf_images"]["mode"] = "auto"
    result = pipeline._run_provider_worker(request, timeout=15)
    assert marker.exists()
    assert body_projection(result) == body_projection(baseline)
    assert result["assets"] == baseline["assets"] == []
    assert any(warning["code"] == "pdf_image_enhancement_incomplete" and warning["content_loss"]
               for warning in result["warnings"])
    assert not (tmp_path / "assets" / "images").exists()
    assert list((tmp_path / "assets").iterdir()) == []


@pytest.mark.parametrize("kind", ["empty", "image_only", "blank_text"])
def test_images_cannot_make_an_unusable_pdf_body_publishable(kind):
    import provider_worker
    body = body_result("Readable body.")
    if kind == "empty":
        body["content"] = []
    elif kind == "image_only":
        body["content"] = [_image("unusable-preview")]
    else:
        body["content"][0].update(raw_text=" \n", text=" \n", normalized_text=" \n")
    with pytest.raises(Exception, match="no usable content"):
        provider_worker._validate_pdf_body(body, DOCUMENT_ID)


def test_force_ocr_route_has_no_inspector_dependency(monkeypatch):
    import provider_worker
    requested = []
    monkeypatch.setattr(provider_worker, "_require_dependency", lambda name, package: requested.append(name))
    provider_worker._require_pdf_route("force")
    assert "pdf_inspector" not in requested
    assert {"pypdf", "pypdfium2", "rapidocr", "onnxruntime"} == set(requested)


@pytest.mark.parametrize("change", ["body_text", "body_order", "table_cell", "table_order"])
def test_image_candidate_rejects_changes_to_body_or_table_values(tmp_path, change):
    import provider_worker
    body = body_result("First paragraph.\n\n| One | Two |\n| --- | --- |\n| A | B |\n\n"
                       "Last paragraph.\n\n| Red | Blue |\n| --- | --- |\n| C | D |")
    candidate = copy.deepcopy(body)
    if change == "body_text":
        candidate["content"][0]["raw_text"] = "Altered paragraph."
    elif change == "body_order":
        candidate["content"].reverse()
    elif change == "table_cell":
        candidate["tables"][0]["raw_rows"][1][0] = "Altered cell"
    else:
        references = [node for node in candidate["content"] if node["type"] == "table"]
        references[0]["table_id"], references[1]["table_id"] = references[1]["table_id"], references[0]["table_id"]
    with pytest.raises(ValueError, match="changed the authoritative body or tables"):
        provider_worker._validate_image_candidate(body, candidate, tmp_path, DOCUMENT_ID)


def test_image_body_and_metadata_limits_are_checked_before_acceptance(monkeypatch, tmp_path):
    import provider_worker
    assert (provider_worker.IMAGE_BODY_LIMIT, provider_worker.IMAGE_METADATA_LIMIT,
            provider_worker.IMAGE_ASSET_LIMIT, provider_worker.IMAGE_BYTES_LIMIT) == (
                64 * 1024 * 1024, 16 * 1024 * 1024, 4096, 256 * 1024 * 1024)
    body = body_result("A small valid body.")
    body_path = tmp_path / "body.json"
    body_path.write_text(json.dumps({"ok": True, "result": body}), encoding="utf-8")
    monkeypatch.setattr(provider_worker, "IMAGE_BODY_LIMIT", body_path.stat().st_size - 1)
    with pytest.raises(ValueError, match="size or file-type limit"):
        provider_worker._read_image_body(body_path)
    monkeypatch.setattr(provider_worker, "IMAGE_METADATA_LIMIT", 16)
    with pytest.raises(ValueError, match="metadata limit"):
        provider_worker._validate_image_candidate(body, copy.deepcopy(body), tmp_path, DOCUMENT_ID)


def test_image_asset_count_and_byte_limits_do_not_accept_partial_assets(monkeypatch, tmp_path):
    import provider_worker
    body = body_result("A small valid body.")
    candidate = copy.deepcopy(body)
    candidate["assets"] = [{"asset_id": "image-one"}, {"asset_id": "image-two"}]
    monkeypatch.setattr(provider_worker, "IMAGE_ASSET_LIMIT", 1)
    with pytest.raises(ValueError, match="asset count limit"):
        provider_worker._validate_image_candidate(body, candidate, tmp_path, DOCUMENT_ID)
    monkeypatch.setattr(provider_worker, "IMAGE_ASSET_LIMIT", 4096)
    monkeypatch.setattr(provider_worker, "IMAGE_BYTES_LIMIT", 3)
    (tmp_path / "image-one.png").write_bytes(b"1234")
    candidate["assets"] = [{"asset_id": "image-one", "path": "assets/images/image-one.png", "sha256": "0" * 64}]
    with pytest.raises(ValueError, match="byte limit"):
        provider_worker._validate_image_candidate(body, candidate, tmp_path, DOCUMENT_ID)


@pytest.mark.parametrize("limit_name", ["MAX_OBJECTS_PER_PAGE", "MAX_NEIGHBOR_CHECKS"])
def test_page_complexity_cap_uses_one_preview_and_continues(monkeypatch, tmp_path, limit_name):
    import pdf_images
    monkeypatch.setattr(pdf_images, limit_name, 1)
    result, _, _ = _enhance(monkeypatch, tmp_path, vector_only=True, pages=2)
    assert not _images(result, "body_region")
    assert [node["source_locator"]["page"] for node in _images(result, "pdf_page_supplement")] == [1, 2]
    assert result["image_metrics"]["capped_pages"] == [1, 2]
    assert result["image_metrics"]["rendered_pages"] == [1, 2]
    assert result["image_metrics"]["render_calls"] == 2


def test_missing_inspector_positions_preserve_ocr_body_and_page_images(monkeypatch, tmp_path):
    import pdf_images
    source, body, _, _ = figure_pdf(tmp_path / "source.pdf", vector_only=True)
    for node in body["content"]:
        node["source_locator"].update(extraction_method="ocr", page=1, ocr_provider="synthetic")
    baseline = copy.deepcopy(body)

    def unavailable(*args, **kwargs):
        raise ImportError("Optional Inspector positions are absent")

    monkeypatch.setattr(pdf_images, "_position_items", unavailable)
    monkeypatch.setitem(sys.modules, "pdf_inspector", None)
    monkeypatch.setitem(sys.modules, "rapidocr", None)
    result = pdf_images.enhance_pdf_images(source, body, tmp_path / "images",
                                         {"mode": "auto", "document_id": DOCUMENT_ID}, time.monotonic() + 20)
    assert body == baseline and body_projection(result) == body_projection(body)
    assert not _images(result, "body_region")
    assert len(_images(result, "pdf_page_supplement")) == 1
    assert result["image_metrics"]["render_calls"] == 1


def test_missing_pdfium_preserves_body_with_loss_warning(monkeypatch, tmp_path):
    import pdf_images
    body = body_result("Body survives unavailable optional images.")
    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    result = pdf_images.enhance_pdf_images(tmp_path / "unused.pdf", body, tmp_path / "images",
                                         {"mode": "auto", "document_id": DOCUMENT_ID}, time.monotonic() + 20)
    assert body_projection(result) == body_projection(body)
    assert not _images(result)
    assert any(w["code"] == "pdf_images_unavailable" and w["content_loss"] for w in result["warnings"])
    assert not (tmp_path / "images").exists()


def test_image_off_does_not_open_pdf_or_load_optional_capabilities(monkeypatch, tmp_path):
    import pdf_images
    body = body_result("Image-free body.")
    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    monkeypatch.setitem(sys.modules, "pdf_inspector", None)
    result = pdf_images.enhance_pdf_images(tmp_path / "absent.pdf", body, tmp_path / "images",
                                         {"mode": "off", "document_id": DOCUMENT_ID}, time.monotonic() + 20)
    assert result == body and not (tmp_path / "images").exists()


@pytest.mark.parametrize("mode,with_assets", [("off", True), ("auto", False), ("objects", False)])
def test_off_and_markdown_only_requests_skip_image_worker(monkeypatch, tmp_path, mode, with_assets):
    import pipeline
    source, body, _, _ = figure_pdf(tmp_path / "source.pdf")
    synthetic_body = tmp_path / "synthetic-body.json"
    synthetic_body.write_text(json.dumps(body), encoding="utf-8")
    shim, _ = _blocking_worker(tmp_path, "write", synthetic_body)
    monkeypatch.setattr(pipeline, "PROVIDER_WORKER", shim)

    def forbidden(*args, **kwargs):
        raise AssertionError("Image worker must not start for this request")

    monkeypatch.setattr(pipeline, "_run_pdf_image_worker", forbidden)
    result = pipeline._run_provider_worker({
        "adapter": "pdf_inspector", "source": str(source), "document_id": DOCUMENT_ID,
        "mode": "preserve", "ocr_mode": "off", "ocr_settings": {"mode": "off"},
        "asset_dir": str(tmp_path / "images") if with_assets else None,
        "pdf_images": {"mode": mode, "timeout_seconds": 2},
    }, timeout=15)
    assert body_projection(result) == body_projection(body)
    assert not (tmp_path / "images").exists()


def test_multicolumn_conflict_uses_page_supplement(monkeypatch, tmp_path):
    import pdf_images
    from pdf_image_fixtures import draw_text
    from reportlab.pdfgen import canvas
    source = tmp_path / "columns.pdf"
    pdf = canvas.Canvas(str(source), pagesize=(400, 500))
    items = []
    paragraphs = [
        ("Left opening line.\nThe left story starts here.", 25, 455),
        ("Left middle line.\nThe left story continues here.", 25, 315),
        ("Right opening line.\nThe right story starts here.", 220, 455),
        ("Right closing line.\nThe right story ends here.", 220, 120),
    ]
    for text, x, y in paragraphs:
        for index, line in enumerate(text.splitlines()):
            draw_text(pdf, items, line, x, y - 14 * index, size=9)
    pdf.setFillColorRGB(0.2, 0.3, 0.9)
    pdf.rect(230, 270, 95, 80, stroke=0, fill=1)
    pdf.showPage()
    pdf.save()
    body = body_result("\n\n".join(p[0] for p in paragraphs))
    monkeypatch.setattr(pdf_images, "_position_items", lambda source, pages: items)
    result = pdf_images.enhance_pdf_images(source, body, tmp_path / "images",
                                         {"mode": "auto", "document_id": DOCUMENT_ID}, time.monotonic() + 20)
    assert body_projection(result) == body_projection(body)
    assert not _images(result, "body_region")
    assert len(_images(result, "pdf_page_supplement")) == 1


def test_separate_pdf_figure_and_ocr_appendices_render_with_resolved_assets(tmp_path, monkeypatch):
    import canonical
    import provider_worker
    result, _, _ = _enhance(monkeypatch, tmp_path, ambiguous=True)
    ocr = copy.deepcopy(next(node for node in result["content"] if node["type"] == "paragraph"))
    ocr["id"] = "node-" + "8" * 16
    ocr["source_locator"] = {"source_unit_id": "unit-0000000000000001", "page": 1,
                              "extraction_method": "ocr", "placement": "unanchored_supplement"}
    ocr.update(raw_text="Recovered OCR sentence.", text="Recovered OCR sentence.", normalized_text="Recovered OCR sentence.")
    index = next(i for i, node in enumerate(result["content"]) if node["type"] == "image")
    result["content"].insert(index, ocr)
    document = provider_worker._extraction_document(result, DOCUMENT_ID)
    canonical.validate_canonical(document)
    markdown = canonical.render_markdown(document, include_frontmatter=False)
    assert markdown.index("Supplementary OCR (unplaced)") < markdown.index("Supplementary PDF figures")
    assert "Recovered OCR sentence." in markdown
    assert "PDF page 1" in markdown or "Page 1" in markdown
    assert "assets/images/" in markdown


@pytest.mark.parametrize("output_mode", ["markdown", "bundle"])
def test_cli_publication_preserves_body_with_image_timeout_or_markdown_only(tmp_path, output_mode):
    source, body, _, _ = figure_pdf(tmp_path / "source.pdf")
    synthetic_body = tmp_path / "synthetic-body.json"
    synthetic_body.write_text(json.dumps(body), encoding="utf-8")
    worker, marker = _blocking_worker(tmp_path, "write", synthetic_body)
    entry = tmp_path / "pipeline-entry.py"
    entry.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import pipeline\n"
        f"pipeline.PROVIDER_WORKER = {str(worker)!r}\n"
        "raise SystemExit(pipeline.main())\n", encoding="utf-8")
    output = tmp_path / "published"
    output.mkdir()
    destination_args = (["--output-path", str(output / "document.md")] if output_mode == "markdown"
                        else ["--output-dir", str(output)])
    completed = subprocess.run(
        [sys.executable, str(entry), *CONFIG_ARG, "--input", str(source),
         "--output-mode", output_mode, "--pdf-images", "auto", "--pdf-image-timeout", "2",
         "--language-normalization", "preserve", "--timestamp", "2000-01-01", *destination_args],
        capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert completed.returncode == 0, completed.stderr
    if output_mode == "markdown":
        assert [path.name for path in output.iterdir()] == ["document.md"]
        markdown = (output / "document.md").read_text(encoding="utf-8")
        assert not marker.exists()
        assert "assets/images/" not in markdown
    else:
        assert marker.exists()
        bundle = output / source.stem
        data = json.loads((bundle / f"{source.stem}.json").read_text(encoding="utf-8"))
        assert data["quality"]["status"] == "partial"
        assert data["assets"] == []
        assert any(w["code"] == "pdf_image_enhancement_incomplete" for w in data["quality"]["warnings"])
        markdown = (bundle / f"{source.stem}.md").read_text(encoding="utf-8")
        assert (bundle / "src" / source.name).read_bytes() == source.read_bytes()
    for node in body["content"]:
        assert node["normalized_text"] in markdown
def test_straight_vector_axes_are_recovered_without_bitmap_objects(monkeypatch, tmp_path):
    import pdf_images
    from pdf_image_fixtures import draw_text
    from reportlab.pdfgen import canvas
    source = tmp_path / "straight-vectors.pdf"
    pdf = canvas.Canvas(str(source), pagesize=(400, 500))
    items = []
    opening = ["Opening observation for this chart.", "This is the complete introductory paragraph."]
    closing = ["Closing observation for this chart.", "This is the complete concluding paragraph."]
    for lines, baseline in ((opening, 455), (closing, 120)):
        for index, line in enumerate(lines):
            draw_text(pdf, items, line, 35, baseline - index * 15)
    pdf.line(70, 250, 70, 350)
    pdf.line(70, 250, 300, 250)
    pdf.line(70, 270, 130, 310)
    pdf.line(130, 310, 205, 290)
    pdf.line(205, 290, 285, 335)
    draw_text(pdf, items, "Offset chart note", 85, 215)
    pdf.showPage()
    pdf.save()
    body = body_result("\n".join(opening) + "\n\n" + "\n".join(closing))
    monkeypatch.setattr(pdf_images, "_position_items", lambda source, pages: items)
    result = pdf_images.enhance_pdf_images(source, body, tmp_path / "images",
                                         {"mode": "auto", "document_id": DOCUMENT_ID}, time.monotonic() + 20)
    assert body_projection(result) == body_projection(body)
    images = _images(result)
    assert images and result["image_metrics"]["render_calls"] == 1
    box = images[0]["source_locator"]["bbox"]
    assert box[0] <= 70 and box[1] <= 213 and box[2] >= 300 and box[3] >= 350


def test_proven_text_underlines_and_strikeouts_do_not_render_pages(monkeypatch, tmp_path):
    import pdf_images
    from pdf_image_fixtures import draw_text
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas
    source = tmp_path / "text-formatting.pdf"
    pdf = canvas.Canvas(str(source), pagesize=(400, 500))
    items, paragraphs = [], []
    for number in range(4):
        lines = [f"Ordinary paragraph number {number} starts here.", "Its remaining words continue on this line."]
        paragraphs.append("\n".join(lines))
        for index, line in enumerate(lines):
            baseline = 455 - number * 80 - index * 15
            draw_text(pdf, items, line, 35, baseline)
            pdf.setLineWidth(0.5)
            width = stringWidth(line, "Helvetica", 11)
            pdf.line(35, baseline - 1, 35 + width, baseline - 1)
            pdf.line(35, baseline + 4, 35 + width, baseline + 4)
    pdf.showPage()
    pdf.save()
    body = body_result("\n\n".join(paragraphs))
    monkeypatch.setattr(pdf_images, "_position_items", lambda source, pages: items)
    result = pdf_images.enhance_pdf_images(source, body, tmp_path / "images",
                                         {"mode": "auto", "document_id": DOCUMENT_ID}, time.monotonic() + 20)
    assert body_projection(result) == body_projection(body)
    assert not _images(result)
    assert result["image_metrics"]["pages_scanned"] == 1
    assert result["image_metrics"]["render_calls"] == 0


def _enhance_decorated_prose(monkeypatch, tmp_path, case, variant):
    import pdf_images
    from pdf_image_fixtures import decorated_prose_pdf
    source, body, items, required_box = decorated_prose_pdf(tmp_path / "decorated.pdf", case, variant)
    before = copy.deepcopy(body)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(pdf_images, "_position_items", lambda source, pages: items)
    result = pdf_images.enhance_pdf_images(source, body, tmp_path / "images",
                                         {"mode": "auto", "document_id": DOCUMENT_ID}, time.monotonic() + 20)
    assert body == before and body_projection(result) == body_projection(before)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    return result, required_box


@pytest.mark.parametrize("variant", [0, 1])
def test_parallel_header_rules_and_revision_bars_are_not_figures(monkeypatch, tmp_path, variant):
    result, _ = _enhance_decorated_prose(monkeypatch, tmp_path, "page_rules", variant)
    assert not _images(result)
    assert not result["assets"]
    assert result["image_metrics"]["pages_scanned"] == 1
    assert result["image_metrics"]["render_calls"] == 0


@pytest.mark.parametrize("variant", [0, 1])
def test_filled_stroked_body_note_is_retained_as_prose_not_a_figure(monkeypatch, tmp_path, variant):
    result, _ = _enhance_decorated_prose(monkeypatch, tmp_path, "filled_prose_box", variant)
    assert not _images(result)
    assert not result["assets"]
    assert result["image_metrics"]["render_calls"] == 0


@pytest.mark.parametrize("variant", [0, 1])
def test_short_text_marks_with_small_overrun_do_not_create_figures(monkeypatch, tmp_path, variant):
    result, _ = _enhance_decorated_prose(monkeypatch, tmp_path, "short_text_marks", variant)
    assert not _images(result)
    assert not result["assets"]
    assert result["image_metrics"]["render_calls"] == 0


@pytest.mark.parametrize("variant", [0, 1])
def test_inline_name_glyph_connected_to_long_rule_still_reports_loss(monkeypatch, tmp_path, variant):
    result, required_box = _enhance_decorated_prose(monkeypatch, tmp_path, "inline_glyph_with_rule", variant)
    assert not _images(result, "body_region")
    previews = _images(result, "pdf_page_supplement")
    assert len(previews) == 1
    assert _contains(previews[0]["source_locator"]["bbox"], required_box)
    assert any(warning["code"] == "pdf_inline_image_unrecovered" and warning["content_loss"]
               for warning in result["warnings"])
    assert not any(node["source_locator"].get("extraction_method") == "ocr" for node in result["content"])
    assert result["image_metrics"]["render_calls"] == 1


@pytest.mark.parametrize("variant", [0, 1])
def test_connected_text_boxes_and_arrows_remain_a_complete_diagram(monkeypatch, tmp_path, variant):
    result, required_box = _enhance_decorated_prose(monkeypatch, tmp_path, "connected_text_boxes", variant)
    images = _images(result)
    assert images, "Mapped text inside connected boxes must not erase the surrounding diagram."
    assert any(_contains(node["source_locator"]["bbox"], required_box) for node in images)
    assert result["image_metrics"]["render_calls"] == 1
def test_near_duplicate_body_without_matched_endpoints_uses_page_preview(monkeypatch, tmp_path):
    import pdf_images
    source, body, items, _ = figure_pdf(tmp_path / "near-duplicate.pdf")
    paragraphs = [node for node in body["content"] if node["type"] == "paragraph"]
    # Most of both long paragraphs matches the page exactly, but these are
    # different body occurrences: one begins earlier and the other ends later.
    # High coverage alone must not make either a complete placement boundary.
    for field in ("raw_text", "text", "normalized_text"):
        paragraphs[0][field] = "Revised " + paragraphs[0][field]
        paragraphs[-1][field] += " again"
    baseline = copy.deepcopy(body)
    monkeypatch.setattr(pdf_images, "_position_items", lambda source, pages: items)
    result = pdf_images.enhance_pdf_images(source, body, tmp_path / "images",
                                         {"mode": "auto", "document_id": DOCUMENT_ID}, time.monotonic() + 20)
    assert body == baseline and body_projection(result) == body_projection(body)
    assert not _images(result, "body_region")
    assert len(_images(result, "pdf_page_supplement")) == 1


def _figure_with_external_body_node(monkeypatch, tmp_path, case):
    """A complete drawing does not own adjacent Inspector body occurrences."""
    import pdf_images
    from pdf_image_fixtures import draw_text
    from reportlab.pdfgen import canvas

    source = tmp_path / "body-boundary.pdf"
    pdf = canvas.Canvas(str(source), pagesize=(400, 500), pageCompression=0)
    items = []
    opening = ["Opening observation for this chart.", "This is the complete introductory paragraph."]
    closing = ["Closing observation for this chart.", "This is the complete concluding paragraph."]
    for lines, baseline in ((opening, 455), (closing, 120)):
        for index, line in enumerate(lines):
            draw_text(pdf, items, line, 35, baseline - index * 15)
    pdf.setFillColorRGB(0.1, 0.25, 0.9)
    pdf.rect(95, 265, 50, 65, stroke=0, fill=1)
    pdf.line(55, 250, 55, 350)
    pdf.line(55, 250, 300, 250)
    draw_text(pdf, items, "Chart note belonging to this drawing", 65, 215, size=8)
    external_items = []
    if case == "heading":
        external = "The next topic starts an independent section of this report"
        draw_text(pdf, external_items, external, 35, 185)
        middle = "## " + external
    elif case in {"prose_before", "prose_after"}:
        lines = ["Additional context appears here.", "It belongs to the prose."]
        baseline = 400 if case == "prose_before" else 185
        for index, line in enumerate(lines):
            draw_text(pdf, external_items, line, 35, baseline - index * 15)
        middle = "\n".join(lines)
    elif case == "list_intro":
        external = "The following observations explain the reported changes:"
        draw_text(pdf, external_items, external, 35, 185)
        middle = external
    else:
        raise AssertionError(case)
    pdf.showPage()
    pdf.save()
    markdown = "\n".join(opening) + "\n\n" + middle + "\n\n" + "\n".join(closing)
    body = body_result(markdown)
    if case == "list_intro":
        # The following two visual lines are one existing list occurrence.
        # Changing its type here avoids testing Markdown list parsing as well.
        body["content"][-1]["type"] = "list_item"
        body["content"][-1]["ordered"] = False
        body["content"][-1]["level"] = 1
    items.extend(external_items)
    baseline = copy.deepcopy(body)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(pdf_images, "_position_items", lambda source, pages: items)
    image_dir = tmp_path / "images"
    result = pdf_images.enhance_pdf_images(source, body, image_dir,
                                         {"mode": "auto", "document_id": DOCUMENT_ID}, time.monotonic() + 20)
    assert body == baseline and body_projection(result) == body_projection(body)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    assert _images(result), "Uncertain placement must retain the complete drawing."
    assert any(_contains(node["source_locator"]["bbox"], [54, 213, 301, 351]) for node in _images(result))
    assert result["image_metrics"]["render_calls"] == 1
    for node in _images(result):
        _asset_file(result, node, image_dir)
    return result, external_items


@pytest.mark.parametrize("case", ["heading", "prose_before", "prose_after", "list_intro"])
def test_external_body_nodes_are_not_absorbed_as_figure_anchors(monkeypatch, tmp_path, case):
    result, external_items = _figure_with_external_body_node(monkeypatch, tmp_path, case)
    for image in _images(result, "body_region"):
        box = image["source_locator"]["bbox"]
        for item in external_items:
            external_box = [item["x"], item["y"] - item["height"] * 0.25,
                            item["x"] + item["width"], item["y"] + item["height"]]
            assert (box[2] < external_box[0] or box[0] > external_box[2]
                    or box[3] < external_box[1] or box[1] > external_box[3]), case
        image_index = result["content"].index(image)
        # Inspector body order is opening, external node, closing/list item.
        external_index = [i for i, node in enumerate(result["content"]) if node["type"] != "image"][1]
        assert (image_index > external_index) if case == "prose_before" else (image_index < external_index)
    if not _images(result, "body_region"):
        assert len(_images(result, "pdf_page_supplement")) == 1


@pytest.mark.parametrize("tail", ["Shared evidence.", "Shared evidences."])
def test_repeated_adjacent_tail_closes_only_the_exact_body_occurrence(tail):
    import pdf_images

    opening = "This uniquely identified paragraph explains the complete observation."
    expected_tail = "Shared evidence."
    body = body_result(opening + "\n" + expected_tail
                       + "\n\nAn independent retained statement also says " + expected_tail)
    baseline = copy.deepcopy(body)
    items = [
        {"text": opening, "x": 35, "y": 300, "width": 330, "height": 13, "page": 1},
        {"text": tail, "x": 35, "y": 280, "width": 90, "height": 13, "page": 1},
    ]
    mapped = pdf_images.map_page(items, pdf_images.BodyIndex(body))
    block = next(block for block in mapped["blocks"] if block["ordinal"] == 0)
    assert body == baseline
    assert block["head"]
    assert block["complete"] is (tail == expected_tail)
    assert block["tail"] is (tail == expected_tail)
    if tail == expected_tail:
        # The shared final line belongs to this occurrence only because exact
        # adjacent continuation reaches its end; it extends the proven bbox.
        assert block["bbox"][1] <= 280
        assert any(member["text"] == expected_tail for member in block["members"])
    else:
        assert block["bbox"][1] > 280
