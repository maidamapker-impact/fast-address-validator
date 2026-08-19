"""PDF field extraction and strict validation for Team FAST integrations."""

from __future__ import annotations

import argparse
import json
import logging
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
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


class AddressValidationError(RuntimeError):
    """Raised when Google Address Validation cannot process a request."""


@dataclass(frozen=True)
class AddressValidationResult:
    """Correctness signals returned by Google Address Validation."""

    correct: bool
    formatted_address: str | None
    validation_granularity: str | None
    missing_components: tuple[str, ...]
    unconfirmed_components: tuple[str, ...]
    unresolved_tokens: tuple[str, ...]
    message: str

    def to_payload(self) -> dict[str, object]:
        """Return a JSON-compatible validation result."""

        return asdict(self)


def validate_address(
    address_lines: Sequence[str],
    *,
    region_code: str | None = None,
    api_key: str | None = None,
    timeout: float = 10.0,
) -> AddressValidationResult:
    """Validate address correctness through Google's Address Validation API.

    This checks Google's address components and does not consult or update a local
    database. The API key can be supplied directly or through GOOGLE_MAPS_API_KEY.
    """

    import os

    lines = tuple(line.strip() for line in address_lines if line.strip())
    if not lines:
        raise ValueError("address_lines must contain at least one non-empty line")

    key = api_key or os.getenv("GOOGLE_MAPS_API_KEY")
    if not key:
        raise ValueError("Google API key is required via api_key or GOOGLE_MAPS_API_KEY")

    postal_address: dict[str, object] = {"addressLines": list(lines)}
    if region_code:
        postal_address["regionCode"] = region_code.upper()
    payload = json.dumps({"address": postal_address}).encode("utf-8")
    request = Request(
        "https://addressvalidation.googleapis.com/v1:validateAddress?"
        + urlencode({"key": key}),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            response_payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise AddressValidationError(f"Google Address Validation request failed: {exc}") from exc

    try:
        result = response_payload["result"]
        verdict = result["verdict"]
        address = result.get("address", {})
    except (KeyError, TypeError) as exc:
        raise AddressValidationError("Google returned an unexpected validation response") from exc

    missing = tuple(address.get("missingComponentTypes", ()))
    unconfirmed = tuple(address.get("unconfirmedComponentTypes", ()))
    unresolved = tuple(address.get("unresolvedTokens", ()))
    granularity = verdict.get("validationGranularity")
    correct = bool(
        verdict.get("addressComplete")
        and not verdict.get("hasUnconfirmedComponents")
        and not missing
        and not unconfirmed
        and not unresolved
        and granularity in {"PREMISE", "SUB_PREMISE"}
    )
    message = "Address is correct." if correct else "Address needs review."
    return AddressValidationResult(
        correct=correct,
        formatted_address=address.get("formattedAddress"),
        validation_granularity=granularity,
        missing_components=missing,
        unconfirmed_components=unconfirmed,
        unresolved_tokens=unresolved,
        message=message,
    )


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
    parser.add_argument(
        "--address-line",
        action="append",
        dest="address_lines",
        help="Address line to validate with Google; repeat for multiple lines",
    )
    parser.add_argument("--region-code", help="Optional CLDR country code, such as US")
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
        address_result = None
        if args.address_lines:
            address_result = validate_address(args.address_lines, region_code=args.region_code)
    except (AddressValidationError, PDFExtractionError, ValueError, json.JSONDecodeError) as exc:
        logger.error("Validation failed: %s", exc)
        return 2

    output = report.to_payload()
    if address_result is not None:
        output["address"] = address_result.to_payload()
    print(json.dumps(output, indent=2))
    passed = report.passed and (address_result is None or address_result.correct)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
