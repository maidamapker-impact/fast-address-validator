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
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

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
    """Correctness result for a billing address."""

    valid: bool
    input_lines: tuple[str, ...]
    formatted_address: str | None
    validation_granularity: str | None
    missing_components: tuple[str, ...]
    unconfirmed_components: tuple[str, ...]
    unresolved_tokens: tuple[str, ...]
    message: str

    @property
    def correct(self) -> bool:
        """Backward-compatible alias for the address validity result."""

        return self.valid

    def to_payload(self) -> dict[str, object]:
        """Return a JSON-compatible address result."""

        return asdict(self)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure application logging without changing existing handlers."""

    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _normalise(value: str | None) -> str:
    """Normalize values for comparison while preserving the original output."""

    return re.sub(r"\s+", " ", value or "").strip().casefold()


def _normalise_address(lines: Sequence[str]) -> str:
    """Normalize address lines while ignoring layout punctuation."""

    return _normalise(" ".join(lines).replace(",", " "))


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
    except ModuleNotFoundError as exc:
        raise PDFExtractionError(
            "pdfplumber is required to read PDFs; install dependencies with "
            "'python3 -m pip install -r requirements.txt'"
        ) from exc

    try:
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


def extract_billing_address(text: str) -> tuple[str, ...]:
    """Extract the lines belonging to a ``Billing Address`` PDF field."""

    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        label, separator, value = line.partition(":")
        if label.strip().casefold() != "billing address":
            continue

        address_lines = [value.strip()] if separator and value.strip() else []
        for following_line in lines[index + 1 :]:
            if not following_line:
                if address_lines:
                    break
                continue
            if address_lines and re.search(r"\b\d{5}(?:-\d{4})?\b", address_lines[-1]):
                break
            if re.match(r"^[A-Za-z][A-Za-z /()#-]{1,60}:\s*", following_line):
                break
            address_lines.append(following_line)
        if address_lines:
            return tuple(address_lines)
        break
    return ()


def validate_address(
    address_lines: Sequence[str],
    *,
    region_code: str | None = None,
    api_key: str | None = None,
    timeout: float = 10.0,
) -> AddressValidationResult:
    """Validate address correctness with Google, without checking a local database."""

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
    request = Request(
        "https://addressvalidation.googleapis.com/v1:validateAddress?" + urlencode({"key": key}),
        data=json.dumps({"address": postal_address}).encode("utf-8"),
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
    valid = bool(
        verdict.get("addressComplete")
        and not verdict.get("hasUnconfirmedComponents")
        and not missing
        and not unconfirmed
        and not unresolved
        and granularity in {"PREMISE", "SUB_PREMISE"}
    )
    return AddressValidationResult(
        valid=valid,
        input_lines=lines,
        formatted_address=address.get("formattedAddress"),
        validation_granularity=granularity,
        missing_components=missing,
        unconfirmed_components=unconfirmed,
        unresolved_tokens=unresolved,
        message="Billing address is valid." if valid else "Billing address needs review.",
    )


def validate_address_fixture(
    address_lines: Sequence[str],
    fixture_path: str | Path = Path(__file__).with_name("address_fixtures.json"),
) -> AddressValidationResult:
    """Validate an address against local POC fixtures without network access."""

    lines = tuple(line.strip() for line in address_lines if line.strip())
    if not lines:
        raise ValueError("address_lines must contain at least one non-empty line")

    try:
        with Path(fixture_path).open(encoding="utf-8") as fixture_file:
            fixtures = json.load(fixture_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise AddressValidationError(f"Unable to read address fixtures: {fixture_path}") from exc

    candidate = _normalise_address(lines)
    for entry in fixtures.get("valid", []):
        fixture_lines = tuple(entry.get("address_lines", ()))
        if candidate == _normalise_address(fixture_lines):
            return AddressValidationResult(
                valid=True,
                input_lines=lines,
                formatted_address=", ".join(fixture_lines),
                validation_granularity="FIXTURE",
                missing_components=(),
                unconfirmed_components=(),
                unresolved_tokens=(),
                message="Billing address matched the local valid-address fixture.",
            )

    return AddressValidationResult(
        valid=False,
        input_lines=lines,
        formatted_address=None,
        validation_granularity="FIXTURE",
        missing_components=(),
        unconfirmed_components=(),
        unresolved_tokens=(),
        message="Billing address did not match a local valid-address fixture.",
    )


def validate_document(
    pdf_path: str | Path,
    expected_fields: Mapping[str, str] | None = None,
    *,
    api_key: str | None = None,
    region_code: str | None = None,
    fixture_path: str | Path = Path(__file__).with_name("address_fixtures.json"),
) -> ValidationReport | AddressValidationResult:
    """Validate expected fields or the PDF's Billing Address."""

    if expected_fields is not None and not expected_fields:
        raise ValueError("expected_fields must contain at least one field")

    text = extract_text(pdf_path)
    if expected_fields is None:
        address_lines = extract_billing_address(text)
        if not address_lines:
            return AddressValidationResult(
                valid=False,
                input_lines=(),
                formatted_address=None,
                validation_granularity=None,
                missing_components=(),
                unconfirmed_components=(),
                unresolved_tokens=(),
                message="Billing Address was not found in the PDF.",
            )
        if api_key:
            return validate_address(address_lines, api_key=api_key, region_code=region_code)
        return validate_address_fixture(address_lines, fixture_path)

    extracted = extract_key_values(text)
    results = tuple(
        validate_field(field, expected, extracted.get(field))
        for field, expected in expected_fields.items()
    )
    logger.info("Validated %s fields from %s", len(results), pdf_path)
    return ValidationReport(str(pdf_path), results)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a Billing Address in a PDF.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument(
        "expected",
        nargs="?",
        help="Optional JSON object containing expected field values",
    )
    parser.add_argument("--api-key", help="Google API key; defaults to GOOGLE_MAPS_API_KEY")
    parser.add_argument("--region-code", help="Optional CLDR country code, such as US")
    parser.add_argument(
        "--fixtures",
        default=str(Path(__file__).with_name("address_fixtures.json")),
        help="Local address fixture JSON used when no Google API key is supplied",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""

    configure_logging()
    args = _build_parser().parse_args(argv)
    try:
        if args.expected:
            expected = json.loads(args.expected)
            if not isinstance(expected, dict) or not all(
                isinstance(value, str) for value in expected.values()
            ):
                raise ValueError("expected must be a JSON object with string values")
            report = validate_document(args.pdf, expected)
        else:
            report = validate_document(
                args.pdf,
                api_key=args.api_key,
                region_code=args.region_code,
                fixture_path=args.fixtures,
            )
    except (AddressValidationError, PDFExtractionError, ValueError, json.JSONDecodeError) as exc:
        logger.error("Validation failed: %s", exc)
        return 2

    payload = report.to_payload()
    if isinstance(report, AddressValidationResult):
        payload = {"valid": report.valid, "billing_address": payload}
        passed = report.valid
    else:
        passed = report.passed
    print(json.dumps(payload, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
