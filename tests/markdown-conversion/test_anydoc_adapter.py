"""Deterministic AnyDoc adapter contract tests (no native dependency required)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import zipfile
from types import SimpleNamespace
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "markdown-conversion" / "scripts"
sys.path.insert(0, str(_SCRIPTS))


class _Inline:
    def __init__(self, kind, text=None, alt=None, source=None, content=None, target=None):
        self.kind = kind
        self.text = text
        self.alt = alt
        self.source = source
        self.content = content
        self.target = target


class _ImageSource:
    def __init__(self, kind="asset", asset_id=0):
        self.kind = kind
        self.asset_id = asset_id
        self.url = None


class _Block:
    def __init__(self, kind, content=None, level=None, table=None, lang=None, text=None):
        self.kind = kind
        self.content = content
        self.level = level
        self.table = table
        self.lang = lang
        self.text = text
        self.list = None
        self.blocks = None


class _Asset:
    def __init__(self, index=0, media_type="image/png", data=b"\x89PNG\r\n\x1a\nPNG", origin_part="word/media/image1.png"):
        self.id = index
        self.media_type = media_type
        self.data = data
        self.origin_part = origin_part


class _Document:
    def __init__(self):
        image = _Inline("image", alt="logo", source=_ImageSource())
        self.blocks = [
            _Block("heading", [_Inline("text", text="Title")], level=1),
            _Block("paragraph", [_Inline("text", text="Before "), image, _Inline("text", text=" after")]),
        ]
        self.notes = []
        self.assets = [_Asset()]


def _write_ooxml_image_fixture(path: Path, *, referenced: bool = True) -> None:
    drawing = (
        '<w:r><w:drawing><wp:inline><a:graphic><a:graphicData><a:blip r:embed="rId1"/>'
        '</a:graphicData></a:graphic></wp:inline></w:drawing></w:r>'
        if referenced else ''
    )
    document = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<w:body><w:p>{drawing}</w:p></w:body></w:document>'
    )
    relationships = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="media/image1.png"/></Relationships>'
    )
    with zipfile.ZipFile(path, 'w') as package:
        package.writestr('word/document.xml', document)
        if referenced:
            package.writestr('word/_rels/document.xml.rels', relationships)
        package.writestr('word/media/image1.png', b'\x89PNG\r\n\x1a\nPNG')


class _FakeAnyDoc:
    Document = Block = Inline = List = ListItem = Table = CellSlot = Cell = Note = Asset = ImageSource = LinkTarget = Style = type
    ConvertError = RuntimeError

    def format_from_bytes(self, data):
        return "docx"

    def format_from_extension(self, extension):
        return "docx"

    def to_document(self, data, format=None):
        assert format == "docx"
        return _Document()


@pytest.fixture()
def fake_anydoc(monkeypatch):
    import adapters

    fake = _FakeAnyDoc()
    fake.__file__ = str(Path(adapters.__file__).resolve())
    monkeypatch.setattr(adapters, "_ANYDOC", fake)
    real_version = adapters.importlib.metadata.version
    class _Distribution:
        files = ()

        def locate_file(self, value):
            return Path(adapters.__file__).resolve().parent

    monkeypatch.setattr(adapters.importlib.metadata, "distribution", lambda name: _Distribution())
    monkeypatch.setattr(adapters.importlib.metadata, "packages_distributions", lambda: {"anydoc": ["firecrawl-anydoc"]})
    monkeypatch.setattr(
        adapters.importlib.metadata,
        "version",
        lambda name: "0.1.3" if name == "firecrawl-anydoc" else real_version(name),
    )
    return adapters


def test_anydoc_adapter_maps_blocks_and_embedded_asset(tmp_path, fake_anydoc):
    source = tmp_path / "source.docx"
    source.write_bytes(b"PK fake")
    output_assets = tmp_path / "assets" / "images"

    result = fake_anydoc.AnyDocAdapter().extract(
        str(source), "sha256:" + "a" * 64, "preserve", output_assets
    )

    assert result["adapter"]["name"] == "anydoc"
    assert [node["type"] for node in result["content"]] == ["heading", "paragraph", "image", "paragraph"]
    assert len(result["assets"]) == 1
    asset = result["assets"][0]
    published = tmp_path / asset["path"]
    png = b"\x89PNG\r\n\x1a\nPNG"
    assert published.read_bytes() == png
    assert asset["sha256"] == hashlib.sha256(png).hexdigest()
    assert any(node.get("asset_id") == asset["asset_id"] for node in result["content"])


def test_anydoc_adapter_rejects_signature_mismatch(tmp_path, fake_anydoc, monkeypatch):
    source = tmp_path / "source.docx"
    source.write_bytes(b"not a docx")
    monkeypatch.setattr(fake_anydoc._ANYDOC, "format_from_bytes", lambda data: "xlsx")
    with pytest.raises(RuntimeError, match="detected xlsx"):
        fake_anydoc.AnyDocAdapter().extract(str(source), "sha256:" + "a" * 64, "preserve")


def test_anydoc_word_revisions_use_accepted_snapshot_without_mutating_source(tmp_path, fake_anydoc):
    import io

    source = tmp_path / "reviewed.docx"
    document_xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>Before</w:t></w:r>'
        '<w:del><w:r><w:delText>Deleted</w:delText></w:r></w:del>'
        '<w:ins><w:r><w:t>Inserted</w:t></w:r></w:ins>'
        '<w:r><w:instrText>TOC \\o "1-3"</w:instrText></w:r>'
        '</w:p></w:body></w:document>'
    )
    with zipfile.ZipFile(source, "w") as package:
        package.writestr("word/document.xml", document_xml)
    original = source.read_bytes()

    def convert(data, format=None):
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            transformed = package.read("word/document.xml").decode("utf-8")
        assert "Deleted" not in transformed
        assert "Inserted" in transformed
        assert "}ins" not in transformed and "<w:ins" not in transformed
        return SimpleNamespace(
            blocks=[_Block("paragraph", [_Inline("text", text="Before Inserted")])],
            notes=[],
            assets=[],
        )

    fake_anydoc._ANYDOC.to_document = convert
    result = fake_anydoc.AnyDocAdapter().extract(
        str(source), "sha256:" + hashlib.sha256(original).hexdigest(), "preserve", tmp_path / "assets"
    )
    assert source.read_bytes() == original
    assert any(node.get("text") == "Before Inserted" for node in result["content"])
    warnings = [item for item in result["warnings"] if item["code"] == "office_revisions_flattened_to_accepted_view"]
    assert len(warnings) == 1 and warnings[0]["content_loss"] is True


def test_only_typed_max_xml_nodes_uses_ordered_docx_capacity_recovery(tmp_path, fake_anydoc, monkeypatch):
    import io
    from xml.etree import ElementTree
    import docx_sharding

    class ConvertError(Exception):
        pass

    fake_anydoc._ANYDOC.ConvertError = ConvertError
    monkeypatch.setattr(docx_sharding, "TARGET_NODES", 8)
    source = tmp_path / "capacity.docx"
    paragraphs = (
        '<w:p><w:ins><w:r><w:t>Paragraph 1</w:t></w:r></w:ins></w:p>'
        + ''.join(
            f'<w:p><w:r><w:t>Paragraph {index}</w:t></w:r></w:p>' for index in range(2, 7)
        )
    )
    with zipfile.ZipFile(source, "w") as package:
        package.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body>{paragraphs}</w:body></w:document>',
        )

    calls = []

    def convert(data, format=None):
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            root = ElementTree.fromstring(package.read("word/document.xml"))
        values = [item.text for item in root.iter() if item.tag.endswith('}t')]
        calls.append(values)
        if len(values) == 6:
            raise ConvertError("max_xml_nodes exceeded")
        return SimpleNamespace(
            blocks=[_Block("paragraph", [_Inline("text", text=value)]) for value in values],
            notes=[],
            assets=[],
        )

    fake_anydoc._ANYDOC.to_document = convert
    result = fake_anydoc.AnyDocAdapter().extract(
        str(source), "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(), "preserve", tmp_path / "assets"
    )
    assert [item["text"] for item in result["content"]] == [f"Paragraph {index}" for index in range(1, 7)]
    assert len(calls) == 4
    fallback = [item for item in result["warnings"] if item["code"] == "adapter_fallback_used"]
    assert len(fallback) == 1 and fallback[0]["content_loss"] is False
    assert "limit=max_xml_nodes" in fallback[0]["message"]
    revision = [item for item in result["warnings"] if item["code"] == "office_revisions_flattened_to_accepted_view"]
    assert len(revision) == 1 and revision[0]["content_loss"] is True


def test_anydoc_ooxml_orphan_provider_asset_is_not_published(tmp_path, fake_anydoc):
    source = tmp_path / 'orphan.docx'
    _write_ooxml_image_fixture(source, referenced=False)
    output_assets = tmp_path / 'assets' / 'images'

    result = fake_anydoc.AnyDocAdapter().extract(
        str(source), 'sha256:' + hashlib.sha256(source.read_bytes()).hexdigest(), 'preserve', output_assets
    )

    assert result['assets'] == []
    assert not any(item.get('type') == 'image' for item in result['content'])
    assert not any(item.get('type') == 'image_occurrence' for item in result['relationships'])
    assert not list(output_assets.glob('*'))
    assert any(item['code'] == 'office_image_relationship_unproved' for item in result['warnings'])


def test_anydoc_ooxml_unresolved_occurrence_retains_asset_without_guessing_node(tmp_path, fake_anydoc):
    source = tmp_path / 'unresolved.docx'
    _write_ooxml_image_fixture(source, referenced=True)
    fake_anydoc._ANYDOC.to_document = lambda data, format=None: SimpleNamespace(
        blocks=[_Block('paragraph', [_Inline('text', text='Visible text')])],
        notes=[],
        assets=[_Asset()],
    )

    result = fake_anydoc.AnyDocAdapter().extract(
        str(source), 'sha256:' + hashlib.sha256(source.read_bytes()).hexdigest(), 'preserve', tmp_path / 'assets' / 'images'
    )

    assert len(result['assets']) == 1
    assert not any(item.get('type') == 'image' for item in result['content'])
    occurrences = [item for item in result['relationships'] if item.get('type') == 'image_occurrence']
    assert len(occurrences) == 1
    assert occurrences[0]['placement'] == 'unresolved'
    assert 'content_node_id' not in occurrences[0]
    assert occurrences[0]['package_part'] == 'word/media/image1.png'
    warning = [item for item in result['warnings'] if item['code'] == 'office_image_position_unresolved']
    assert len(warning) == 1 and warning[0]['content_loss'] is True


def test_anydoc_ooxml_surplus_provider_occurrence_is_discarded(tmp_path, fake_anydoc):
    source = tmp_path / 'surplus.docx'
    _write_ooxml_image_fixture(source, referenced=True)
    image_one = _Inline('image', alt='first', source=_ImageSource())
    image_two = _Inline('image', alt='surplus', source=_ImageSource())
    fake_anydoc._ANYDOC.to_document = lambda data, format=None: SimpleNamespace(
        blocks=[_Block('paragraph', [image_one, image_two])],
        notes=[],
        assets=[_Asset()],
    )

    result = fake_anydoc.AnyDocAdapter().extract(
        str(source), 'sha256:' + hashlib.sha256(source.read_bytes()).hexdigest(), 'preserve', tmp_path / 'assets' / 'images'
    )

    images = [item for item in result['content'] if item.get('type') == 'image']
    occurrences = [item for item in result['relationships'] if item.get('type') == 'image_occurrence']
    assert len(images) == len(occurrences) == 1
    assert occurrences[0]['content_node_id'] == images[0]['id']
    assert occurrences[0]['placement'] == 'resolved'
    warning = [item for item in result['warnings'] if item['code'] == 'office_image_occurrence_unproved']
    assert len(warning) == 1 and warning[0]['content_loss'] is True


def test_runtimeerror_lookalike_does_not_use_capacity_recovery(tmp_path, fake_anydoc):
    source = tmp_path / "lookalike.docx"
    source.write_bytes(b"PK fake")
    fake_anydoc._ANYDOC.ConvertError = type("ConvertError", (Exception,), {})
    fake_anydoc._ANYDOC.to_document = lambda data, format=None: (_ for _ in ()).throw(
        RuntimeError("max_xml_nodes exceeded")
    )
    with pytest.raises(RuntimeError, match="AnyDoc could not convert"):
        fake_anydoc.AnyDocAdapter().extract(
            str(source), "sha256:" + "a" * 64, "preserve", tmp_path / "assets"
        )


def test_anydoc_markdown_only_omits_assets_and_image_nodes(tmp_path, fake_anydoc):
    source = tmp_path / "source.docx"
    source.write_bytes(b"PK fake")
    result = fake_anydoc.AnyDocAdapter().extract(str(source), "sha256:" + "a" * 64, "preserve", None)
    assert result["assets"] == []
    assert not any(node["type"] == "image" for node in result["content"])
    assert any("logo" in node.get("text", "") for node in result["content"])
    assert any(item["code"] == "anydoc_markdown_asset_omitted" for item in result["warnings"])


def test_anydoc_model_validation_rejects_cycle_and_oversized_text():
    import adapters

    document = _Document()
    document.blocks[0].blocks = [document.blocks[0]]
    with pytest.raises(RuntimeError, match="cycle"):
        adapters._validate_anydoc_document(document)

    document = _Document()
    document.blocks[0].content[0].text = "x" * (adapters._ANYDOC_MAX_CHARS_PER_FIELD + 1)
    with pytest.raises(RuntimeError, match="characters"):
        adapters._validate_anydoc_document(document)


def test_anydoc_asset_magic_mismatch_is_warning_and_no_dangling_asset(tmp_path, fake_anydoc):
    source = tmp_path / "source.docx"
    source.write_bytes(b"PK fake")
    fake_anydoc._ANYDOC.to_document = lambda data, format=None: type(
        "BadDocument", (), {
            "blocks": [_Block("paragraph", [_Inline("image", alt="bad", source=_ImageSource())])],
            "notes": [],
            "assets": [_Asset(data=b"not-png")],
        },
    )()
    result = fake_anydoc.AnyDocAdapter().extract(str(source), "sha256:" + "a" * 64, "preserve", tmp_path / "assets")
    assert result["assets"] == []
    assert not any(node["type"] == "image" for node in result["content"])
    assert any(item["code"] == "anydoc_image_magic_mismatch" for item in result["warnings"])


def test_anydoc_flattening_warnings_and_model_paths(tmp_path, fake_anydoc):
    source = tmp_path / "source.docx"
    source.write_bytes(b"PK fake")

    class Note:
        kind = "footnote"
        id = "n1"
        blocks = [_Block("paragraph", [_Inline("text", text="note body")])]

    class ListModel:
        marker = "decimal"
        start = 0
        items = []

    list_block = _Block("list")
    list_block.list = ListModel()
    rule = _Block("rule")
    note_ref = _Block("paragraph", [_Inline("note_ref")])

    fake_anydoc._ANYDOC.to_document = lambda data, format=None: type(
        "FeatureDocument", (), {
            "blocks": [rule, list_block, note_ref],
            "notes": [Note()],
            "assets": [],
        },
    )()
    result = fake_anydoc.AnyDocAdapter().extract(str(source), "sha256:" + "a" * 64, "preserve", tmp_path / "assets")
    codes = {item["code"] for item in result["warnings"]}
    assert "anydoc_rule_omitted" in codes
    assert "anydoc_list_start_clamped" in codes
    assert "anydoc_note_reference_omitted" in codes
    assert "anydoc_notes_flattened" in codes
    assert any(node["source_locator"]["model_path"] == ["notes", 1] for node in result["content"])
    assert all("[^" not in node.get("text", "") for node in result["content"])


def test_anydoc_compatible_version_is_not_a_quality_warning(tmp_path, fake_anydoc, monkeypatch):
    source = tmp_path / "source.docx"
    source.write_bytes(b"PK fake")
    monkeypatch.setattr(
        fake_anydoc.importlib.metadata,
        "version",
        lambda name: "0.1.4" if name == "firecrawl-anydoc" else "0.0",
    )
    result = fake_anydoc.AnyDocAdapter().extract(str(source), "sha256:" + "a" * 64, "preserve", None)
    assert result["adapter"]["version"] == "0.1.4"
    assert "anydoc_untested_version" not in {item["code"] for item in result["warnings"]}


def test_anydoc_heading_preserves_text_image_interleaving_and_warns_on_split(tmp_path, fake_anydoc):
    import adapters

    source = tmp_path / "heading.docx"
    source.write_bytes(b"PK fake")
    image = _Inline("image", alt="figure", source=_ImageSource())
    document = SimpleNamespace(
        blocks=[_Block("heading", [_Inline("text", text="Before "), image, _Inline("text", text=" after")], level=2)],
        notes=[],
        assets=[_Asset()],
    )
    fake_anydoc._ANYDOC.to_document = lambda data, format=None: document
    result = adapters.AnyDocAdapter().extract(str(source), "sha256:" + "a" * 64, "preserve", tmp_path / "assets")
    assert [node["type"] for node in result["content"]] == ["heading", "image", "paragraph"]
    assert result["content"][0]["text"] == "Before"
    assert result["content"][2]["text"] == "after"
    assert any(item["code"] == "anydoc_inline_structure_flattened" for item in result["warnings"])


def test_anydoc_list_locator_preserves_marker_nesting_task_and_label(tmp_path, fake_anydoc):
    import adapters

    source = tmp_path / "list.docx"
    source.write_bytes(b"PK fake")
    nested_list = _Block("list")
    nested_list.list = SimpleNamespace(marker="bullet", start=1, items=[])
    item = SimpleNamespace(
        blocks=[_Block("paragraph", [_Inline("text", text="Task item")]), nested_list],
        checked=True,
        marker_label="3-a)",
    )
    outer_list = _Block("list")
    outer_list.list = SimpleNamespace(marker="decimal", start=3, items=[item])
    document = SimpleNamespace(blocks=[outer_list], notes=[], assets=[])
    fake_anydoc._ANYDOC.to_document = lambda data, format=None: document
    result = adapters.AnyDocAdapter().extract(str(source), "sha256:" + "a" * 64, "preserve", None)
    node = next(node for node in result["content"] if node["type"] == "list_item")
    locator = node["source_locator"]
    assert node["text"] == "Task item"
    assert node["ordinal"] == 3 and node["ordered"] is True
    assert locator["marker"] == "decimal"
    assert locator["nesting"] == 0
    assert locator["task"] is True and locator["checked"] is True
    assert locator["marker_label"] == "3-a)"


def test_anydoc_nested_table_values_are_row_major_and_not_dropped(tmp_path, fake_anydoc):
    import adapters

    source = tmp_path / "nested-table.docx"
    source.write_bytes(b"PK fake")

    def origin(blocks):
        return SimpleNamespace(kind="origin", cell=SimpleNamespace(blocks=blocks, row_span=1, col_span=1))

    inner = _Block(
        "table",
        table=SimpleNamespace(
            kind="data",
            header_rows=0,
            grid=[
                [origin([_Block("paragraph", [_Inline("text", text="Inner A")])])],
                [origin([_Block("paragraph", [_Inline("text", text="Inner B")])])],
            ],
        ),
    )
    outer = _Block(
        "table",
        table=SimpleNamespace(
            kind="data",
            header_rows=0,
            grid=[[origin([_Block("paragraph", [_Inline("text", text="Outer")]), inner])]],
        ),
    )
    document = SimpleNamespace(blocks=[outer], notes=[], assets=[])
    fake_anydoc._ANYDOC.to_document = lambda data, format=None: document
    result = adapters.AnyDocAdapter().extract(str(source), "sha256:" + "a" * 64, "preserve", None)
    value = result["tables"][0]["raw_rows"][0][0]
    assert value.index("Outer") < value.index("Inner A") < value.index("Inner B")
    assert "anydoc_nested_table_flattened" in {item["code"] for item in result["warnings"]}


def test_anydoc_anchor_link_target_emits_specific_warning(tmp_path, fake_anydoc):
    import adapters

    source = tmp_path / "anchor.docx"
    source.write_bytes(b"PK fake")
    target = SimpleNamespace(kind="anchor", value="section-2")
    link = _Inline("link", content=[_Inline("text", text="Jump")], target=target)
    document = SimpleNamespace(
        blocks=[_Block("paragraph", [link])],
        notes=[],
        assets=[],
    )
    fake_anydoc._ANYDOC.to_document = lambda data, format=None: document
    result = adapters.AnyDocAdapter().extract(str(source), "sha256:" + "a" * 64, "preserve", None)
    assert any(item["code"] == "anydoc_link_target_flattened" for item in result["warnings"])


def test_anydoc_common_inline_emphasis_is_not_reported_as_content_loss(tmp_path, fake_anydoc):
    source = tmp_path / "bold.docx"
    source.write_bytes(b"PK fake")
    inline = _Inline("text", text="Bold but present")
    inline.style = SimpleNamespace(bold=True, italic=False, strike=False, code=False)
    fake_anydoc._ANYDOC.to_document = lambda data, format=None: SimpleNamespace(
        blocks=[_Block("paragraph", [inline])], notes=[], assets=[]
    )
    result = fake_anydoc.AnyDocAdapter().extract(
        str(source), "sha256:" + "a" * 64, "preserve", tmp_path / "assets"
    )
    style_warning = next(item for item in result["warnings"] if item["code"] == "anydoc_inline_style_flattened")
    assert style_warning["content_loss"] is False
    assert "anydoc_rich_inline_flattened" not in {item["code"] for item in result["warnings"]}


def test_anydoc_format_mapping_is_immutable_and_single_source():
    import adapters
    from types import MappingProxyType

    assert isinstance(adapters.ANYDOC_FORMAT_BY_EXTENSION, MappingProxyType)
    assert adapters.anydoc_format_for_path("report.docm") == "docx"
    with pytest.raises(TypeError):
        adapters.ANYDOC_FORMAT_BY_EXTENSION[".new"] = "docx"


def test_missing_anydoc_cli_fails_closed_without_publication(tmp_path):
    if importlib.util.find_spec("anydoc") is not None:
        pytest.skip("requires an absent AnyDoc runtime")
    source = tmp_path / "missing.docx"
    source.write_bytes(b"PK fake")
    script = _SCRIPTS / "pipeline.py"
    config = Path(__file__).parent / "fixtures" / "test_config.json"
    import subprocess

    result = subprocess.run(
        [sys.executable, str(script), "--config", str(config), "--input", str(source)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert 'pip install firecrawl-anydoc' in result.stderr
    assert not (tmp_path / "missing").exists()


def test_anydoc_missing_dependency_has_unpinned_install_command(monkeypatch):
    import adapters

    monkeypatch.setattr(adapters, "_ANYDOC", None)
    monkeypatch.setattr(adapters.importlib.metadata, "version", lambda name: (_ for _ in ()).throw(adapters.importlib.metadata.PackageNotFoundError(name)))
    with pytest.raises(RuntimeError, match=r'pip install firecrawl-anydoc'):
        adapters.anydoc_capability_check()


_REAL_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "anydoc"
_REAL_MANIFEST = _REAL_FIXTURE_DIR / "MANIFEST.json"
_REAL_BASE_FIXTURES = {
    ".doc": "text.doc",
    ".docx": "text.docx",
    ".docm": "text.docx",
    ".ppt": "pres.ppt",
    ".pps": "pres.ppt",
    ".pot": "pres.ppt",
    ".pptx": "pres.pptx",
    ".pptm": "pres.pptx",
    ".ppsx": "pres.pptx",
    ".ppsm": "pres.pptx",
    ".xls": "sheet.xls",
    ".xlsx": "sheet.xlsx",
    ".xlsm": "sheet.xlsx",
    ".xlsb": "any_sheets.xlsb",
    ".odt": "text.odt",
    ".ods": "sheet.ods",
    ".odp": "pres.odp",
    ".rtf": "text.rtf",
    ".epub": "book.epub",
    ".csv": "sheet.csv",
}


def test_real_anydoc_fixture_manifest_is_provenance_and_hash_complete():
    """The committed real corpus must remain tied to immutable upstream bytes."""
    assert _REAL_MANIFEST.is_file()
    manifest = json.loads(_REAL_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["upstream"] == "https://github.com/firecrawl/anydoc"
    assert manifest["commit"] == "6eac2b2774df8707d83a3d8b19223d7718469254"
    assert manifest["spdx_license"] == "MIT"
    assert (_REAL_FIXTURE_DIR / manifest["license_file"]).is_file()
    entries = {item["local"]: item for item in manifest["entries"]}
    for local, item in entries.items():
        path = _REAL_FIXTURE_DIR / local
        assert path.is_file(), local
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"], local
        assert item.get("spdx_license", manifest["spdx_license"]) == "MIT"
    assert set(_REAL_BASE_FIXTURES.values()).issubset(entries)


@pytest.mark.parametrize("suffix", sorted(_REAL_BASE_FIXTURES))
def test_real_anydoc_fixture_converts_every_mapped_extension(tmp_path, suffix):
    """Exercise every mapping key through the installed capability-compatible wheel."""
    import adapters

    capability = adapters.anydoc_capability_check()
    assert capability["version"] == adapters.anydoc_version()
    assert "tested_version" not in capability and "untested" not in capability
    expected = adapters.ANYDOC_FORMAT_BY_EXTENSION[suffix]
    base = _REAL_FIXTURE_DIR / _REAL_BASE_FIXTURES[suffix]
    assert base.is_file(), base
    source = tmp_path / f"fixture{suffix}"
    source.write_bytes(base.read_bytes())
    result = adapters.AnyDocAdapter().extract(
        str(source),
        "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
        "preserve",
        tmp_path / "assets" / "images",
    )
    assert result["adapter"]["name"] == "anydoc"
    assert adapters.anydoc_format_for_path(source) == expected
    assert result["content"] or result["tables"]


_REAL_ABUSE_FIXTURES = (
    "deepnest--errors.ppt",
    "deepxml--errors.docx",
    "emptyrowrepeat--errors.ods",
    "hugerepeat--errors.ods",
    "hugespan--errors.ods",
    "hugespan--errors.pptx",
    "imagebomb--errors.docx",
    "zipbomb--errors.docx",
)


@pytest.mark.parametrize("name", _REAL_ABUSE_FIXTURES)
def test_real_anydoc_abuse_fixture_fails_closed_without_asset_publication(tmp_path, name):
    """Upstream resource-limit fixtures must fail before any asset is published."""
    import adapters

    source = _REAL_FIXTURE_DIR / "abuse" / name
    output_assets = tmp_path / "assets" / "images"
    with pytest.raises(RuntimeError) as failure:
        adapters.AnyDocAdapter().extract(
            str(source),
            "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
            "preserve",
            output_assets,
        )
    assert str(failure.value).startswith(("AnyDoc could not convert", "Office "))
    assert not output_assets.exists() or not any(output_assets.rglob("*"))


def test_real_anydoc_external_relationship_never_becomes_asset_or_url_read(tmp_path):
    """External relationship canaries remain visible text, never fetched assets."""
    import adapters

    source = _REAL_FIXTURE_DIR / "relations" / "handmade-links.pptx"
    output_assets = tmp_path / "assets" / "images"
    result = adapters.AnyDocAdapter().extract(
        str(source),
        "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
        "preserve",
        output_assets,
    )
    assert result["assets"] == []
    assert not output_assets.exists() or not any(output_assets.rglob("*"))
    warnings = {item["code"] for item in result["warnings"]}
    assert "anydoc_link_target_flattened" in warnings


def test_real_anydoc_internal_image_relationship_uses_package_bytes(tmp_path):
    """Embedded image relationships resolve to in-memory package bytes only."""
    import adapters

    source = _REAL_FIXTURE_DIR / "text.docx"
    with zipfile.ZipFile(source) as archive:
        package_images = {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
            if name.startswith("word/media/")
        }
    assert package_images
    result = adapters.AnyDocAdapter().extract(
        str(source),
        "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
        "preserve",
        tmp_path / "assets" / "images",
    )
    assert result["assets"]
    assert {asset["sha256"] for asset in result["assets"]} <= set(package_images.values())
    assert all("url" not in asset["source_locator"] for asset in result["assets"])
