import base64
from unittest.mock import AsyncMock

import pytest

from api.integrations import elvie


def sample_study():
    return {
        "resourceType": "ImagingStudy",
        "id": "study-123",
        "subject": {"reference": "Patient/patient-1"},
        "started": "2026-06-10T12:30:00Z",
        "description": "CT Head without contrast",
        "modality": [{"system": "http://dicom.nema.org/resources/ontology/DCM", "code": "CT"}],
        "bodySite": [{"display": "Head"}],
    }


def sample_report():
    return {
        "resourceType": "DiagnosticReport",
        "id": "report-123",
        "basedOn": [{"reference": "ImagingStudy/study-123"}],
        "presentedForm": [
            {
                "contentType": "text/plain",
                "data": base64.b64encode(b"FINDINGS:\nNo acute hemorrhage.").decode("ascii"),
            }
        ],
        "conclusion": "No acute intracranial abnormality.",
    }


def test_case_payload_uses_imaging_study_id_as_case_accession():
    payload = elvie._case_payload(sample_study(), sample_report())

    assert payload["caseId"] == "wintehr-study-123"
    assert payload["accession"] == "study-123"
    assert payload["patient"]["mrn"] == "patient-1"
    assert payload["study"]["accession"] == "study-123"
    assert payload["study"]["modality"] == "CT"
    assert payload["study"]["bodySite"] == "Head"
    assert payload["report"]["id"] == "report-123"
    assert "No acute hemorrhage" in payload["report"]["text"]
    assert "No acute intracranial abnormality" in payload["report"]["text"]
    assert payload["findings"] == []
    assert payload["negativeFindings"] == []


def test_launch_url_includes_viewer_case_api_and_dicomweb_settings(monkeypatch):
    monkeypatch.setattr(elvie, "VIEWER_URL", "http://viewer.local")
    monkeypatch.setattr(elvie, "CASE_API_BROWSER_URL", "http://case-api.local")
    monkeypatch.setattr(elvie, "DICOMWEB_URL", "/orthanc/dicom-web")

    launch_url = elvie._launch_url("wintehr-study-123", "study-123")

    assert launch_url.startswith("http://viewer.local/index.html?")
    assert "caseId=wintehr-study-123" in launch_url
    assert "accession=study-123" in launch_url
    assert "caseApiBaseUrl=http%3A%2F%2Fcase-api.local" in launch_url
    assert "dicomWebBaseUrl=%2Forthanc%2Fdicom-web" in launch_url


def test_normalize_uid_strips_fhir_urn_oid_prefix():
    assert elvie._normalize_uid("urn:oid:1.2.3.4") == "1.2.3.4"
    assert elvie._normalize_uid("1.2.3.4") == "1.2.3.4"
    assert elvie._normalize_uid(None) is None


def test_demo_accession_mapping_replaces_accession_and_marks_payload(monkeypatch):
    monkeypatch.setenv("ELVIE_DEMO_ACCESSION_MAP", '{"study-123":"NI9f7fae"}')
    payload = elvie._case_payload(sample_study(), sample_report())

    accession = elvie._demo_accession_for_study("study-123")
    elvie._apply_demo_accession_mapping(payload, sample_study(), accession)

    assert payload["accession"] == "NI9f7fae"
    assert payload["source"] == "wintehr-demo-accession-map"
    assert payload["study"]["accession"] == "NI9f7fae"
    assert payload["study"]["sourceImagingStudyId"] == "study-123"
    assert payload["study"]["sourceWintEhrAccession"] == "study-123"
    assert payload["study"]["orthancAccession"] == "NI9f7fae"
    assert payload["study"]["modality"] == "CT"
    assert "DEMO IMAGE MAPPING" in payload["report"]["text"]
    assert "NI9f7fae" in payload["report"]["text"]


@pytest.mark.asyncio
async def test_find_report_matches_based_on_imaging_study_reference():
    fhir = AsyncMock()
    fhir.search.return_value = {
        "resourceType": "Bundle",
        "entry": [
            {"resource": {"id": "other", "basedOn": [{"reference": "ImagingStudy/other"}]}},
            {"resource": sample_report()},
        ],
    }

    report = await elvie._find_report(fhir, sample_study(), report_id=None)

    assert report["id"] == "report-123"
    fhir.search.assert_awaited_once_with("DiagnosticReport", {"patient": "patient-1", "_count": 100})
