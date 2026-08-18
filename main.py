"""PDF field extraction and strict validation for Team FAST integrations."""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

logger = logging.getLogger(__name__)


class ValidationStatus(str, Enum):
    """The outcome of validating one expected field."""

    EXACT = "exact"
    PARTIAL = "partial"
    MISSING = "missing"
    MISMATCH = "mismatch"


@dataclass(frozen=True)
class FieldValidation:
    """Validation result for one field, suitable for JSON serialization."""

    field: str
    expected: str
    actual: str | None
    status: ValidationStatus
    message: str

    @property
    def passed(self) -> bool:
        """Return whether the value exactly matched the expected value."""

        return self.status is ValidationStatus.EXACT


@dataclass(frozen=True)
class ValidationReport:
    """Complete validation output for an extracted document."""

    source: str | None
    fields: tuple[FieldValidation, ...]

    @property
    def passed(self) -> bool:
        """Return whether every expected field matched exactly."""

        return bool(self.fields) and all(result.passed for result in self.fields)

    def to_payload(self) -> dict[str, object]:
        """Return a stable payload for Salesforce or another external API."""

        return {
            "source": self.source,
            "passed": self.passed,
            "status": "passed" if self.passed else "failed",
            "fields": [
                {**asdict(result), "status": result.status.value, "passed": result.passed}
                for result in self.fields
            ],
        }


class PDFExtractionError(RuntimeError):
    """Raised when a PDF cannot be opened or read."""


def configure_logging(level: int = logging.INFO) -> None:
    """Configure application logging without changing existing handlers."""

    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _normalise(value: str | None) -> str:
    """Normalize values for comparison while preserving the original output."""

    return re.sub(r"\s+", " ", value or "").strip().casefold()


def validate_field(field: str, expected: str, actual: str | None) -> FieldValidation:
    """Classify a value as exact, partial, missing, or completely mismatched."""

    expected_normalized = _normalise(expected)
    actual_normalized = _normalise(actual)

    if not actual_normalized:
        status = ValidationStatus.MISSING
        message = "Field was not found or contained no value."
    elif actual_normalized == expected_normalized:
        status = ValidationStatus.EXACT
        message = "Field matched exactly."
    elif expected_normalized and (
        expected_normalized in actual_normalized or actual_normalized in expected_normalized
    ):
        status = ValidationStatus.PARTIAL
        message = "Field contained a partial match."
    else:
        status = ValidationStatus.MISMATCH
        message = "Field did not match the expected value."

    return FieldValidation(field, expected, actual, status, message)


def extract_text(pdf_path: str | Path) -> str:
    """Extract all text from a PDF, raising a useful domain error on failure."""

    path = Path(pdf_path)
    if not path.is_file():
        raise PDFExtractionError(f"PDF is not a file: {path}")

    try:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages).strip()
    except Exception as exc:
        logger.exception("Unable to read PDF %s", path)
        raise PDFExtractionError(f"Unable to read PDF {path}: {exc}") from exc

    if not text:
        raise PDFExtractionError(f"PDF contained no extractable text: {path}")
    return text


def extract_key_values(text: str, separator: str = ":") -> dict[str, str]:
    """Extract ``key: value`` lines from PDF text, ignoring malformed lines."""

    if not separator:
        raise ValueError("separator must not be empty")
    if not text or not text.strip():
        return {}

    fields: dict[str, str] = {}
    for line in text.splitlines():
        if separator not in line:
            continue
        key, value = line.split(separator, 1)
        key, value = key.strip(), value.strip()
        if key and value:
            fields[key] = value
    return fields


def validate_document(
    pdf_path: str | Path,
    expected_fields: Mapping[str, str],
) -> ValidationReport:
    """Extract and validate expected fields from a PDF document."""

    if not expected_fields:
        raise ValueError("expected_fields must contain at least one field")

    extracted = extract_key_values(extract_text(pdf_path))
    results = tuple(
        validate_field(field, expected, extracted.get(field))
        for field, expected in expected_fields.items()
    )
    logger.info("Validated %s fields from %s", len(results), pdf_path)
    return ValidationReport(str(pdf_path), results)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate key-value fields in a PDF.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("expected", help="JSON object containing expected field values")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""

    configure_logging()
    args = _build_parser().parse_args(argv)
    try:
        expected = json.loads(args.expected)
        if not isinstance(expected, dict) or not all(isinstance(value, str) for value in expected.values()):
            raise ValueError("expected must be a JSON object with string values")
        report = validate_document(args.pdf, expected)
    except (PDFExtractionError, ValueError, json.JSONDecodeError) as exc:
        logger.error("Validation failed: %s", exc)
        return 2

    print(json.dumps(report.to_payload(), indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
