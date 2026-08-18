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

## Tests

```sh
pytest
```
