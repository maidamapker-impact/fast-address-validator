# FAST Address Validator

A small PDF field verification engine for Salesforce integrations.

## Install

```sh
python -m pip install -r requirements.txt
```

## Validate a PDF

The PDF should contain one `key: value` field per line. The CLI prints a JSON payload and exits with `0` for an all-exact match, `1` for validation failures, or `2` for input/extraction errors.

```sh
python main.py document.pdf '{"Name":"Jane Doe","Address":"123 Main St"}'
```

Use `validate_document` from `main.py` in a task runner, FastAPI endpoint, or Salesforce integration. Field results are explicitly classified as `exact`, `partial`, `missing`, or `mismatch`.

## Validate address correctness

The POC can validate an address with Google's Address Validation API. It uses Google's component and completeness signals only; it does not query or update an application database. Enable the Address Validation API and provide an API key through `GOOGLE_MAPS_API_KEY`.

```sh
export GOOGLE_MAPS_API_KEY="your-key"
python main.py document.pdf '{"Name":"Jane Doe"}' \
	--address-line '1600 Amphitheatre Pkwy' \
	--address-line 'Mountain View, CA 94043' \
	--region-code US
```

The JSON payload includes `address.correct`, the formatted address, validation granularity, and any missing, unconfirmed, or unresolved components. The address is considered correct only when it is complete, confirmed, and validated to `PREMISE` or `SUB_PREMISE` granularity.

## Tests

```sh
pytest
```
