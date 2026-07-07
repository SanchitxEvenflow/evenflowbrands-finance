import io
import logging
import os
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

# email/ subfolder can't be a package (conflicts with stdlib email module),
# so we add it to sys.path to import email_sender directly from there.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "email"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")

from config import settings
from email_sender import send_email_with_pdfs
from grnpush.router import router as grn_router
from models import DownloadRequest, EmailRequest, EmailResponse
from tds_summary.router import router as tds_router
from zoho_client import MAX_WORKERS, _fetch_one, token_manager

app = FastAPI(title="Zoho Credit Note Bulk Downloader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "https://evenflowbrands-finance.onrender.com",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(grn_router)
app.include_router(tds_router)


@app.get("/health")
def health():
    return {"status": "ok", "token_ready": token_manager.get_token() is not None}


@app.post("/download")
def download_credit_notes(body: DownloadRequest) -> StreamingResponse:
    if not body.credit_note_numbers:
        raise HTTPException(status_code=400, detail="credit_note_numbers list is empty")

    logger.info("Download request — %d credit note(s)", len(body.credit_note_numbers))

    results: list[tuple[str, bytes | None, str | None]] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_one, n): n for n in body.credit_note_numbers}
        for future in as_completed(futures):
            results.append(future.result())

    ok = sum(1 for _, b, _ in results if b is not None)
    errors = len(results) - ok
    logger.info("ZIP ready — %d PDF(s) OK, %d error(s)", ok, errors)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for number, pdf_bytes, error in results:
            safe_name = number.replace("/", "_").replace("\\", "_")
            if pdf_bytes is not None:
                zf.writestr(f"{safe_name}.pdf", pdf_bytes)
            else:
                zf.writestr(f"{safe_name}_ERROR.txt", error or "unknown error")

    zip_buffer.seek(0)
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=credit_notes.zip"},
    )


@app.post("/send-email", response_model=EmailResponse)
def send_credit_notes_email(body: EmailRequest) -> EmailResponse:
    if not body.credit_note_numbers:
        raise HTTPException(status_code=400, detail="credit_note_numbers list is empty")
    if not settings.resend_api_key or not settings.resend_from_email:
        raise HTTPException(status_code=503, detail="RESEND_API_KEY / RESEND_FROM_EMAIL not configured in .env")

    logger.info("Email request — %d note(s) → %s", len(body.credit_note_numbers), body.to_email)

    results: list[tuple[str, bytes | None, str | None]] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_one, n): n for n in body.credit_note_numbers}
        for future in as_completed(futures):
            results.append(future.result())

    pdfs = [
        (n.replace("/", "_").replace("\\", "_"), b)
        for n, b, e in results if b is not None
    ]
    failed = [
        {"number": n, "error": e}
        for n, b, e in results if b is None
    ]

    if not pdfs:
        raise HTTPException(status_code=502, detail=f"All {len(results)} notes failed to fetch")

    emails_sent = send_email_with_pdfs(body.to_email, body.subject, pdfs, body.body)

    logger.info("Email done — %d PDF(s) in %d email(s), %d failed", len(pdfs), emails_sent, len(failed))
    return EmailResponse(to=body.to_email, sent_count=len(pdfs), emails_sent=emails_sent, failed=failed)


_frontend_out = os.path.join(os.path.dirname(__file__), "..", "frontend", "out")
if os.path.isdir(_frontend_out):
    app.mount("/", StaticFiles(directory=_frontend_out, html=True), name="static")
