# FAST Address Validator

A small PDF field verification engine for Salesforce integrations.

Automated Document Address Verification Workflow
This project defines and automates the critical process of validating physical addresses on documents uploaded to a CRM.

🌟 The Problem
Manually checking that a customer-uploaded document (like a utility bill) actually contains the correct address is time-consuming and error-prone. This workflow automates that process to improve efficiency and data integrity.

🚀 The Solution
This system uses an automated logic flow to listen for new document uploads, parse the documents, extract the address, and perform a strict validation against an established ground truth.

Here is the high-level system architecture and process logic:

System Architecture Diagram
(Note: Save the diagram above to a ./documentation/ folder in your repo and name it workflow-diagram.png for this link to display the image.)

🛠️ Key Process Steps
The diagram above is broken down into these actionable stages:

1. Document Upload (Trigger)
Source: A file (e.g., PDF) is uploaded directly to a Salesforce CRM.

Mechanism: This action acts as the system trigger.

2. PDF Parsing
A background process automatically detects the new upload.

The system extracts all readable text content from the PDF file.

3. Field Capture
Targeting: Specific logic identifies the target [Address] field within the extracted text.

Extraction: The relevant address value is captured and held in memory.

4. Validation Logic (Strict Exact Match)
The Check: The captured address is compared against the known ground truth.

Critical Requirement: The search MUST be an exact match (operator Q == Q). Contains-searches are not permitted to ensure precision.

5. Outcome Paths
Path A: Match Found (MATCH? -> YES)

Action: The document is marked as "COMPLETE" and "VALIDATED."

Status: The Salesforce record status is updated.

Path B: Match Failed (MATCH? -> NO)

Action: An ERROR ALERT is generated.

Reason: Mismatch detected or address not found. The system alerts the user and records a workflow error.

💻 Tech Stack (Conceptual)
While this repository contains the flow logic and concept, a full implementation would likely use:

CRM Integration: Salesforce Apex, Triggers, or Flow.

Document Intelligence: Salesforce Einstein OCR, AWS Textract, or Google Cloud Vision.

Business Logic: Middleware (MuleSoft, Zapier) or Lambda Functions for orchestration.

🚧 Roadmap
[x] Initial concept diagram and workflow design (Completed!).

[ ] Proof-of-concept for the PDF parsing engine.

[ ] Development of the strict string comparison logic.

[ ] Full Salesforce integration prototype.

👥 Contributing
This is a beginner project, and I am learning! If you see ways to improve the logic, documentation, or conceptual stack, please feel free to open a Pull Request or create an Issue.

## Install

```sh
python -m pip install -r requirements.txt
```

## Validate a PDF

The PDF should contain one `key: value` field per line. The CLI prints a JSON payload and exits with `0` for an all-exact match, `1` for validation failures, or `2` for input/extraction errors.

```sh
python main.py document.pdf '{"Name":"Jane Doe","Address":"123 Main St"}'
```

To validate the PDF's `Billing Address` with the local POC fixtures, omit the expected JSON object:

```sh
python main.py document.pdf
```

The CLI extracts the multiline field labeled `Billing Address`, compares it with `address_fixtures.json`, and returns top-level `valid: true` or `valid: false`. A missing Billing Address also returns `valid: false`; no network or application database is queried.

To use Google later, provide `--api-key` or set `GOOGLE_MAPS_API_KEY`. The fixture file is only used when no API key is supplied.

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
