# WintEHR to Elvie Viewer Bridge

This document describes the local integration that launches AgentPACS/Elvie viewer from a WintEHR imaging study.

## Repositories Modified

### WintEHR

Worktree used: `/home/george/Downloads/src/faceblur/WintEHR-elvie-bridge`

Modified files:

- `backend/api/integrations/elvie.py`
  - Adds `POST /api/integrations/elvie/launch`.
  - Reads the selected FHIR `ImagingStudy`.
  - Finds the matching `DiagnosticReport` by `basedOn` or `imagingStudy` reference.
  - Builds an Elvie case JSON payload.
  - Writes that case to `elvie-case-api`.
  - Imports generated WintEHR DICOM files into Orthanc.
  - Returns a viewer launch URL.
- `backend/api/integrations/__init__.py`
  - Package marker for integration routers.
- `backend/api/routers/__init__.py`
  - Registers the new Elvie integration router.
- `frontend/src/components/clinical/workspace/tabs/ImagingTab.js`
  - Adds an `Elvie` button to each imaging study card.
  - Calls `/api/integrations/elvie/launch`.
  - Opens the returned viewer URL in a new browser tab.
- `backend/scripts/active/generate_dicom_from_hapi.py`
  - Sets DICOM `AccessionNumber` to the WintEHR `ImagingStudy.id`.
  - Normalizes FHIR `urn:oid:<uid>` values to raw DICOM UIDs for `StudyInstanceUID`.
  - This makes new generated DICOMs queryable by accession in Orthanc.
- `docker-compose.yml`
  - Adds Elvie bridge environment variables to backend dev/prod services.
  - Adds `host.docker.internal:host-gateway` so backend containers can reach host-run AgentPACS services on Linux.
- `.env.example`
  - Documents the Elvie bridge environment variables.

### elvie-case-api

Worktree used: `/home/george/Downloads/src/agentpacs/elvie-case-api-wintehr-bridge`

Modified files:

- `src/index.ts`
  - Adds `PUT /api/cases/:caseId`.
  - Validates `caseId`.
  - Accepts JSON payloads from WintEHR.
  - Writes case files atomically to `CASES_DIR`.
  - Expands CORS methods to include `PUT`.
- `README.md`
  - Documents the write route and WintEHR-style launch URL.

### elvie-viewer

Worktree used: `/home/george/Downloads/src/agentpacs/elvie-viewer-wintehr-bridge`

Modified files:

- `web/index.html`
  - URL launch now accepts `caseApiBaseUrl`, `caseApiUrl`, or `caseApi`.
  - URL launch now accepts `dicomWebBaseUrl`, `dicomWebUrl`, or `dicomweb`.
  - These values are applied before loading the launched case.
  - Values are also stored in viewer localStorage so Settings reflect the launch configuration.

## Runtime Flow

1. User clicks `Elvie` on a WintEHR imaging study card.
2. WintEHR frontend posts:

   ```http
   POST /api/integrations/elvie/launch
   Content-Type: application/json

   { "studyId": "<ImagingStudy.id>" }
   ```

3. WintEHR backend reads `ImagingStudy/<studyId>` from HAPI FHIR.
4. WintEHR backend searches the same patient's `DiagnosticReport` resources and selects a report referencing that study.
5. WintEHR backend builds a case payload:

   ```json
   {
     "caseId": "wintehr-<studyId>",
     "accession": "<studyId>",
     "source": "wintehr",
     "patient": { "id": "<patientId>", "mrn": "<patientId>" },
     "study": {
       "accession": "<studyId>",
       "modality": "CT",
       "studyDescription": "...",
       "studyDate": "...",
       "bodySite": "...",
       "viewerLayout": "1x1"
     },
     "report": {
       "id": "<DiagnosticReport.id>",
       "sourceType": "wintehr-fhir",
       "text": "..."
     },
     "findings": [],
     "negativeFindings": []
   }
   ```

6. WintEHR backend writes the case:

   ```http
   PUT <ELVIE_CASE_API_URL>/api/cases/wintehr-<studyId>
   ```

7. WintEHR backend imports local DICOM files from:

   ```text
   /app/data/generated_dicoms/study_<studyId>
   ```

   into:

   ```text
   <ELVIE_ORTHANC_URL>/instances
   ```

   During import it sets `AccessionNumber=<studyId>` in memory so Elvie can locate the study via DICOMweb.

8. WintEHR backend returns:

   ```json
   {
     "launchUrl": "http://localhost:14175/index.html?caseId=wintehr-<studyId>&accession=<studyId>&caseApiBaseUrl=http%3A%2F%2Flocalhost%3A8787&dicomWebBaseUrl=%2Forthanc%2Fdicom-web",
     "caseId": "wintehr-<studyId>",
     "accession": "<studyId>",
     "importedDicomInstances": 30
   }
   ```

9. Browser opens the returned URL.
10. Elvie viewer reads case JSON from `elvie-case-api`.
11. Elvie viewer loads images from Orthanc DICOMweb by `AccessionNumber`.

## Environment Variables

These are used by the WintEHR backend container:

```env
ELVIE_CASE_API_URL=http://host.docker.internal:8787
ELVIE_ORTHANC_URL=http://host.docker.internal:18042
```

These are embedded in the browser launch URL:

```env
ELVIE_CASE_API_BROWSER_URL=http://localhost:8787
ELVIE_VIEWER_URL=http://localhost:14175
ELVIE_DICOMWEB_URL=/orthanc/dicom-web
```

Optional demo-only mapping from WintEHR ImagingStudy ids to Orthanc accessions:

```env
ELVIE_DEMO_ACCESSION_MAP={"4c059e2f-cf0d-82d3-586c-60ac99629f8d":"NI9f7fae"}
```

This is intentionally a fake transform for local demos where the WintEHR fake patient imaging metadata does not match imported DICOM sample data. The default map launches:

- WintEHR patient: `Patient/7569a069-4ea2-7c1d-9191-7199d9b1c985` (`Tammy740 Deneen201 Abernathy524`)
- WintEHR study: `ImagingStudy/4c059e2f-cf0d-82d3-586c-60ac99629f8d`
- WintEHR procedure: `Plain chest X-ray (procedure)`
- Orthanc sample accession: `NI9f7fae`
- Orthanc sample modality: `CT`

When this mapping is active, the case JSON is marked with:

```json
{
  "source": "wintehr-demo-accession-map",
  "study": {
    "sourceImagingStudyId": "4c059e2f-cf0d-82d3-586c-60ac99629f8d",
    "sourceWintEhrAccession": "4c059e2f-cf0d-82d3-586c-60ac99629f8d",
    "orthancAccession": "NI9f7fae"
  }
}
```

The Elvie report text also includes a `DEMO IMAGE MAPPING` notice so it is clear the displayed images do not clinically match the WintEHR source study.

For Cloudflare tunnel use, the browser-facing values may need to become public tunnel URLs, for example:

```env
ELVIE_CASE_API_BROWSER_URL=https://<case-api-host>
ELVIE_VIEWER_URL=https://<viewer-host>
```

`ELVIE_DICOMWEB_URL=/orthanc/dicom-web` assumes the viewer server proxies Orthanc under the viewer origin.

## Restart Requirements

After merging these changes:

- Recreate/restart WintEHR backend so Docker Compose env and `extra_hosts` are applied.
- Rebuild/restart WintEHR frontend so the `Elvie` button appears.
- Restart `elvie-case-api` so `PUT /api/cases/:caseId` is available.
- Restart or redeploy `elvie-viewer` so URL-provided API settings are honored.

## Debug Checklist

Check case-api health:

```bash
curl http://localhost:8787/health
```

Check Orthanc DICOMweb is reachable from the browser/viewer origin:

```bash
curl http://localhost:18042/dicom-web/studies
```

Check WintEHR backend can reach host services from inside the container:

```bash
docker exec emr-backend curl -sS http://host.docker.internal:8787/health
docker exec emr-backend curl -sS http://host.docker.internal:18042/system
```

Check that a case was written:

```bash
curl http://localhost:8787/api/cases/wintehr-<studyId>
curl http://localhost:8787/api/cases/by-accession/<studyId>
```

Check Orthanc can find the study by accession:

```bash
curl 'http://localhost:18042/dicom-web/studies?AccessionNumber=<studyId>'
```

If the viewer opens the report but not images, the likely issue is Orthanc DICOMweb lookup. Confirm the imported DICOMs have:

- `AccessionNumber=<ImagingStudy.id>`
- valid raw dotted `StudyInstanceUID`, not `urn:oid:<uid>`

If WintEHR returns a bridge error, check backend logs:

```bash
docker logs emr-backend --tail=200
```

Common failure points:

- `ELVIE_CASE_API_URL` is wrong for backend-container-to-host networking.
- `ELVIE_CASE_API_BROWSER_URL` is wrong for the browser.
- `ELVIE_ORTHANC_URL` is wrong for backend-container-to-host networking.
- `ELVIE_DICOMWEB_URL` is not reachable from the viewer origin.
- `elvie-case-api` has not been restarted after adding the `PUT` route.
- `elvie-viewer` has not been restarted after adding URL parameter support.

## Verification Already Performed

- Python compile passed:

  ```bash
  python3 -m py_compile backend/api/integrations/elvie.py backend/scripts/active/generate_dicom_from_hapi.py
  ```

- `git diff --check` passed in all three worktrees.
- `elvie-case-api` functional smoke test passed on a temporary port:
  - `PUT /api/cases/wintehr-test`
  - `GET /api/cases/wintehr-test`
  - `GET /api/cases/by-accession/study-test`

Frontend ESLint/build was not run because local frontend dependencies were not installed in the checked worktrees.
