from pathlib import Path

import pytest

import main


def test_validate_field_distinguishes_all_statuses():
    assert main.validate_field("name", "Jane Doe", " jane   doe ").status is main.ValidationStatus.EXACT
    assert main.validate_field("name", "Jane Doe", "Jane").status is main.ValidationStatus.PARTIAL
    assert main.validate_field("name", "Jane Doe", None).status is main.ValidationStatus.MISSING
    assert main.validate_field("name", "Jane Doe", "John Smith").status is main.ValidationStatus.MISMATCH


def test_extract_key_values_ignores_malformed_lines():
    text = "Name: Jane Doe\nAddress: 123 Main St\nnot a field\n: no key\nEmpty:   "
    assert main.extract_key_values(text) == {"Name": "Jane Doe", "Address": "123 Main St"}


def test_extract_key_values_supports_custom_separator_and_last_duplicate_wins():
    text = "Name=Jane Doe\nName=Jane Smith\nCity Boston"
    assert main.extract_key_values(text, separator="=") == {"Name": "Jane Smith"}


def test_extract_key_values_rejects_empty_separator():
    with pytest.raises(ValueError, match="separator"):
        main.extract_key_values("Name: Jane", separator="")


def test_extract_text_rejects_missing_pdf(tmp_path: Path):
    with pytest.raises(main.PDFExtractionError, match="not a file"):
        main.extract_text(tmp_path / "missing.pdf")


def test_extract_text_reads_all_pages(monkeypatch, tmp_path: Path):
    pdf_path = tmp_path / "document.pdf"
    pdf_path.write_bytes(b"pdf")

    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class PDF:
        pages = [Page("Name: Jane Doe"), Page("City: Boston")]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("pdfplumber.open", lambda path: PDF())
    assert main.extract_text(pdf_path) == "Name: Jane Doe\nCity: Boston"


def test_extract_text_rejects_empty_pdf(monkeypatch, tmp_path: Path):
    pdf_path = tmp_path / "empty.pdf"
    pdf_path.write_bytes(b"pdf")

    class EmptyPDF:
        pages = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("pdfplumber.open", lambda path: EmptyPDF())
    with pytest.raises(main.PDFExtractionError, match="no extractable text"):
        main.extract_text(pdf_path)


def test_extract_text_wraps_reader_errors(monkeypatch, tmp_path: Path):
    pdf_path = tmp_path / "unreadable.pdf"
    pdf_path.write_bytes(b"pdf")
    monkeypatch.setattr("pdfplumber.open", lambda path: (_ for _ in ()).throw(OSError("bad PDF")))

    with pytest.raises(main.PDFExtractionError, match="Unable to read PDF"):
        main.extract_text(pdf_path)


def test_validate_document_rejects_empty_expected_fields(tmp_path: Path):
    with pytest.raises(ValueError, match="at least one field"):
        main.validate_document(tmp_path / "document.pdf", {})


def test_validate_document_returns_integration_payload(monkeypatch, tmp_path: Path):
    pdf_path = tmp_path / "document.pdf"
    pdf_path.write_bytes(b"pdf")
    monkeypatch.setattr(main, "extract_text", lambda path: "Name: Jane Doe\nCity: Boston")

    report = main.validate_document(pdf_path, {"Name": "Jane Doe", "City": "Austin"})
    payload = report.to_payload()

    assert payload["status"] == "failed"
    assert payload["fields"][0]["status"] == "exact"
    assert payload["fields"][1]["status"] == "mismatch"


def test_validate_address_accepts_complete_confirmed_premise(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{}'

    response = {
        "result": {
            "verdict": {
                "addressComplete": True,
                "hasUnconfirmedComponents": False,
                "validationGranularity": "PREMISE",
            },
            "address": {"formattedAddress": "1600 Amphitheatre Pkwy, Mountain View, CA 94043"},
        }
    }
    monkeypatch.setattr(main, "urlopen", lambda request, timeout: Response())
    monkeypatch.setattr(main.json, "load", lambda file: response)

    result = main.validate_address(["1600 Amphitheatre Pkwy", "Mountain View, CA 94043"], api_key="test")

    assert result.correct is True
    assert result.formatted_address.startswith("1600 Amphitheatre")


def test_validate_address_rejects_unresolved_tokens(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    response = {
        "result": {
            "verdict": {
                "addressComplete": False,
                "hasUnconfirmedComponents": True,
                "validationGranularity": "ROUTE",
            },
            "address": {
                "unresolvedTokens": ["not-an-address"],
                "unconfirmedComponentTypes": ["route"],
            },
        }
    }
    monkeypatch.setattr(main, "urlopen", lambda request, timeout: Response())
    monkeypatch.setattr(main.json, "load", lambda file: response)

    result = main.validate_address(["not-an-address"], api_key="test")

    assert result.correct is False
    assert result.unresolved_tokens == ("not-an-address",)
