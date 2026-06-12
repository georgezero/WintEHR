/**
 * Smoke test for the WintEHR -> elvie-case-api -> elvie-viewer launch flow.
 *
 * Expected running services:
 *   WintEHR backend: http://localhost:18000
 *   elvie-case-api: http://localhost:8787
 *   elvie-viewer:   http://localhost:14175/index.html
 *   Orthanc:        http://localhost:18042
 */

const { chromium } = require('playwright');

const BACKEND_URL = process.env.WINTEHR_BACKEND_URL || 'http://localhost:18000';
const CASE_API_URL = process.env.ELVIE_CASE_API_URL || 'http://localhost:8787';
const VIEWER_URL = process.env.ELVIE_VIEWER_URL || 'http://localhost:14175/index.html';
const ORTHANC_URL = process.env.ELVIE_ORTHANC_URL || 'http://localhost:18042';
const DICOMWEB_BASE_URL = process.env.ELVIE_DICOMWEB_URL || '/orthanc/dicom-web';

const CASE_ID = `wintehr-smoke-${Date.now()}`;
const ACCESSION = `smoke-${Date.now()}`;

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`${options.method || 'GET'} ${url} failed: ${response.status} ${text}`);
  }
  return text ? JSON.parse(text) : null;
}

async function assertHttpOk(label, url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`${label} health check failed: ${response.status}`);
  }
  console.log(`ok ${label}: ${response.status}`);
}

async function main() {
  await assertHttpOk('WintEHR backend', `${BACKEND_URL}/health`);
  await assertHttpOk('elvie-case-api', `${CASE_API_URL}/health`);
  await assertHttpOk('Elvie viewer', VIEWER_URL);
  await assertHttpOk('Orthanc DICOMweb', `${ORTHANC_URL}/dicom-web/studies`);

  await fetchJson(`${CASE_API_URL}/api/cases/${CASE_ID}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      caseId: CASE_ID,
      accession: ACCESSION,
      source: 'playwright-smoke',
      study: {
        accession: ACCESSION,
        modality: 'CT',
        studyDescription: 'Playwright smoke study',
        viewerLayout: '1x1',
      },
      report: {
        sourceType: 'playwright-smoke',
        text: 'FINDINGS:\nPlaywright bridge smoke finding.\n\nIMPRESSION:\nSmoke test impression.',
      },
      findings: [],
      negativeFindings: [],
    }),
  });

  const caseByAccession = await fetchJson(`${CASE_API_URL}/api/cases/by-accession/${ACCESSION}`);
  if (caseByAccession.caseId !== CASE_ID) {
    throw new Error(`case-api accession lookup returned ${caseByAccession.caseId}, expected ${CASE_ID}`);
  }

  const launchUrl = `${VIEWER_URL}?${new URLSearchParams({
    caseId: CASE_ID,
    accession: ACCESSION,
    caseApiBaseUrl: CASE_API_URL,
    dicomWebBaseUrl: DICOMWEB_BASE_URL,
  }).toString()}`;

  const browser = await chromium.launch({
    headless: process.env.HEADLESS !== 'false',
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  });

  const page = await browser.newPage();
  const consoleErrors = [];
  const failedRequests = [];

  page.on('console', (message) => {
    if (message.type() === 'error') {
      consoleErrors.push(message.text());
    }
  });
  page.on('requestfailed', (request) => {
    failedRequests.push(`${request.method()} ${request.url()} ${request.failure()?.errorText || ''}`);
  });

  try {
    const caseResponsePromise = page.waitForResponse(
      (response) => response.url().includes(`/api/cases/${CASE_ID}`) && response.status() === 200,
      { timeout: 15000 }
    );

    await page.goto(launchUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await caseResponsePromise;

    await page.waitForFunction(
      ({ caseApiUrl, dicomWebUrl }) => (
        localStorage.getItem('lv_elvie_case_api_url') === caseApiUrl &&
        localStorage.getItem('lv_hermes_dicomweb') === dicomWebUrl
      ),
      { caseApiUrl: CASE_API_URL, dicomWebUrl: DICOMWEB_BASE_URL },
      { timeout: 5000 }
    );

    await page.getByText('Playwright bridge smoke finding').waitFor({ timeout: 15000 });

    const title = await page.title();
    console.log(`ok viewer loaded case: ${CASE_ID}`);
    console.log(`ok page title: ${title}`);

    const severeErrors = consoleErrors.filter((text) => !/favicon|ResizeObserver/i.test(text));
    if (severeErrors.length) {
      throw new Error(`Viewer emitted console errors:\n${severeErrors.join('\n')}`);
    }
    if (failedRequests.length) {
      throw new Error(`Viewer had failed requests:\n${failedRequests.join('\n')}`);
    }
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
