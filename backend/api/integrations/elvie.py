"""Bridge WintEHR ImagingStudy reports into AgentPACS/Elvie cases."""

import base64
import io
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services.hapi_fhir_client import HAPIFHIRClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/integrations/elvie")

CASE_API_URL = os.getenv("ELVIE_CASE_API_URL", "http://host.docker.internal:8787").rstrip("/")
CASE_API_BROWSER_URL = os.getenv("ELVIE_CASE_API_BROWSER_URL", "http://localhost:8787").rstrip("/")
VIEWER_URL = os.getenv("ELVIE_VIEWER_URL", "http://localhost:14175").rstrip("/")
DICOMWEB_URL = os.getenv("ELVIE_DICOMWEB_URL", "/orthanc/dicom-web").rstrip("/")
ORTHANC_URL = os.getenv("ELVIE_ORTHANC_URL", "http://host.docker.internal:18042").rstrip("/")
DICOM_BASE_DIR = Path(os.getenv("DICOM_BASE_DIR", "/app/data/generated_dicoms"))
DEFAULT_DEMO_ACCESSION_MAP = {
    # WintEHR sample chest X-ray for Tammy Abernathy -> imported RSNA ICH CT study.
    "4c059e2f-cf0d-82d3-586c-60ac99629f8d": "NI9f7fae",
}


class ElvieLaunchRequest(BaseModel):
    study_id: str = Field(..., alias="studyId")
    report_id: Optional[str] = Field(default=None, alias="reportId")


class ElvieLaunchResponse(BaseModel):
    launchUrl: str
    caseId: str
    accession: str
    importedDicomInstances: int
    demoMappedAccession: Optional[str] = None


def _resource_entries(bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        entry.get("resource", {})
        for entry in bundle.get("entry", [])
        if entry.get("resource")
    ]


def _patient_id_from_study(study: Dict[str, Any]) -> Optional[str]:
    ref = study.get("subject", {}).get("reference", "")
    if ref.startswith("Patient/"):
        return ref.split("/", 1)[1]
    return None


def _coding_display(value: Any) -> str:
    if isinstance(value, list) and value:
        return _coding_display(value[0])
    if isinstance(value, dict):
        if value.get("display"):
            return str(value["display"])
        coding = value.get("coding") or []
        if coding:
            return _coding_display(coding[0])
        return str(value.get("code") or value.get("text") or "")
    return ""


def _modality_code(study: Dict[str, Any]) -> str:
    modality = study.get("modality") or []
    if modality:
        first = modality[0]
        if isinstance(first, dict):
            return str(first.get("code") or first.get("display") or "OT")

    for series in study.get("series") or []:
        series_modality = series.get("modality") or {}
        if isinstance(series_modality, dict):
            return str(series_modality.get("code") or series_modality.get("display") or "OT")

    return "OT"


def _decode_presented_form(report: Dict[str, Any]) -> str:
    for form in report.get("presentedForm") or []:
        data = form.get("data")
        if not data:
            continue
        try:
            return base64.b64decode(data).decode("utf-8", errors="replace").strip()
        except Exception:
            logger.warning("Failed to decode DiagnosticReport/%s presentedForm", report.get("id"))
    return ""


def _report_text(report: Optional[Dict[str, Any]], study: Dict[str, Any]) -> str:
    if not report:
        return (
            f"{study.get('description') or 'Imaging Study'}\n\n"
            "No DiagnosticReport was found for this imaging study."
        )

    chunks = []
    presented = _decode_presented_form(report)
    if presented:
        chunks.append(presented)

    conclusion = (report.get("conclusion") or "").strip()
    if conclusion and conclusion not in presented:
        chunks.append(f"IMPRESSION:\n{conclusion}")

    return "\n\n".join(chunks).strip() or "No report text is available."


def _demo_accession_map() -> Dict[str, str]:
    raw_map = os.getenv("ELVIE_DEMO_ACCESSION_MAP")
    if not raw_map:
        return DEFAULT_DEMO_ACCESSION_MAP

    try:
        parsed = json.loads(raw_map)
    except json.JSONDecodeError:
        logger.warning("ELVIE_DEMO_ACCESSION_MAP is not valid JSON; ignoring demo accession map")
        return {}

    if not isinstance(parsed, dict):
        logger.warning("ELVIE_DEMO_ACCESSION_MAP must be a JSON object; ignoring demo accession map")
        return {}

    return {str(key): str(value) for key, value in parsed.items() if value}


def _demo_accession_for_study(study_id: str) -> Optional[str]:
    return _demo_accession_map().get(study_id)


def _apply_demo_accession_mapping(payload: Dict[str, Any], study: Dict[str, Any], accession: str) -> None:
    source_accession = payload["accession"]
    payload["accession"] = accession
    payload["source"] = "wintehr-demo-accession-map"
    payload["study"]["accession"] = accession
    payload["study"]["sourceImagingStudyId"] = study["id"]
    payload["study"]["sourceWintEhrAccession"] = source_accession
    payload["study"]["orthancAccession"] = accession
    payload["study"]["studyDescription"] = (
        f"Demo mapped Orthanc CT accession {accession} "
        f"for WintEHR {payload['study']['studyDescription']}"
    )
    payload["study"]["modality"] = "CT"

    mapped_note = (
        "DEMO IMAGE MAPPING:\n"
        f"This WintEHR ImagingStudy ({study['id']}) is mapped to Orthanc accession {accession} "
        "so Elvie can launch real imported sample DICOM images. The WintEHR report/study metadata "
        "does not clinically match the displayed sample images."
    )
    report = payload.setdefault("report", {})
    report["text"] = f"{mapped_note}\n\n{report.get('text') or _report_text(None, study)}"


async def _find_report(
    fhir: HAPIFHIRClient,
    study: Dict[str, Any],
    report_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    if report_id:
        return await fhir.read("DiagnosticReport", report_id)

    patient_id = _patient_id_from_study(study)
    if not patient_id:
        return None

    bundle = await fhir.search("DiagnosticReport", {"patient": patient_id, "_count": 100})
    study_refs = {f"ImagingStudy/{study['id']}", study["id"]}

    for report in _resource_entries(bundle):
        refs = []
        refs.extend(ref.get("reference") for ref in report.get("basedOn") or [])
        refs.extend(ref.get("reference") for ref in report.get("imagingStudy") or [])
        if any(ref in study_refs for ref in refs if ref):
            return report

    return None


def _case_payload(study: Dict[str, Any], report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    study_id = study["id"]
    patient_id = _patient_id_from_study(study) or "unknown"
    description = (
        study.get("description")
        or _coding_display(study.get("procedureCode"))
        or "WintEHR Imaging Study"
    )

    return {
        "caseId": f"wintehr-{study_id}",
        "accession": study_id,
        "source": "wintehr",
        "patient": {
            "id": patient_id,
            "mrn": patient_id,
        },
        "study": {
            "accession": study_id,
            "modality": _modality_code(study),
            "studyDescription": description,
            "studyDate": study.get("started"),
            "bodySite": _coding_display(study.get("bodySite")),
            "viewerLayout": "1x1",
        },
        "report": {
            "id": report.get("id") if report else None,
            "sourceType": "wintehr-fhir",
            "text": _report_text(report, study),
        },
        "findings": [],
        "negativeFindings": [],
    }


async def _upsert_case(payload: Dict[str, Any]) -> None:
    case_id = payload["caseId"]
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.put(f"{CASE_API_URL}/api/cases/{case_id}", json=payload)
        response.raise_for_status()


def _normalize_uid(value: Any) -> Optional[str]:
    if not value:
        return None
    uid = str(value)
    if uid.startswith("urn:oid:"):
        uid = uid.removeprefix("urn:oid:")
    return uid or None


async def _import_dicom_to_orthanc(study_id: str) -> int:
    study_dir = DICOM_BASE_DIR / f"study_{study_id}"
    if not study_dir.exists():
        logger.info("No generated DICOM directory found for ImagingStudy/%s", study_id)
        return 0

    try:
        import pydicom
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="pydicom is not installed") from exc

    imported = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        for dcm_path in study_dir.rglob("*.dcm"):
            try:
                ds = pydicom.dcmread(str(dcm_path))
                ds.AccessionNumber = study_id
                normalized_uid = _normalize_uid(getattr(ds, "StudyInstanceUID", None))
                if normalized_uid:
                    ds.StudyInstanceUID = normalized_uid

                buffer = io.BytesIO()
                ds.save_as(buffer, write_like_original=False)
                response = await client.post(
                    f"{ORTHANC_URL}/instances",
                    content=buffer.getvalue(),
                    headers={"Content-Type": "application/dicom"},
                )
                response.raise_for_status()
                imported += 1
            except Exception as exc:
                logger.warning("Failed to import %s into Orthanc: %s", dcm_path, exc)

    return imported


def _launch_url(case_id: str, accession: str) -> str:
    query = urlencode(
        {
            "caseId": case_id,
            "accession": accession,
            "caseApiBaseUrl": CASE_API_BROWSER_URL,
            "dicomWebBaseUrl": DICOMWEB_URL,
        }
    )
    return f"{VIEWER_URL}/index.html?{query}"


@router.post("/launch", response_model=ElvieLaunchResponse)
async def launch_elvie_case(request: ElvieLaunchRequest) -> ElvieLaunchResponse:
    fhir = HAPIFHIRClient()

    try:
        study = await fhir.read("ImagingStudy", request.study_id)
        report = await _find_report(fhir, study, request.report_id)
        payload = _case_payload(study, report)
        demo_accession = _demo_accession_for_study(request.study_id)
        if demo_accession:
            _apply_demo_accession_mapping(payload, study, demo_accession)
        await _upsert_case(payload)
        imported = 0 if demo_accession else await _import_dicom_to_orthanc(request.study_id)
    except httpx.HTTPStatusError as exc:
        logger.error("Elvie bridge HTTP error: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Elvie bridge upstream error: {exc.response.status_code}",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to launch Elvie case for ImagingStudy/%s", request.study_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return ElvieLaunchResponse(
        launchUrl=_launch_url(payload["caseId"], payload["accession"]),
        caseId=payload["caseId"],
        accession=payload["accession"],
        importedDicomInstances=imported,
        demoMappedAccession=demo_accession,
    )
