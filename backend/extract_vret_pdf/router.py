import csv
import io
import zipfile
from fastapi import APIRouter, File, UploadFile, HTTPException
from fastapi.responses import StreamingResponse

from .extract_pdf import extract_pdf_rows, HEADERS

router = APIRouter(prefix="/api/extract-vret-pdf", tags=["VRET PDF Extraction"])

@router.post("/upload")
async def extract_vret_pdf(file: UploadFile = File(...)):
    if not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only ZIP files are supported.")
    
    file_bytes = await file.read()
    
    csv_buffer = io.StringIO()
    writer = csv.DictWriter(csv_buffer, fieldnames=HEADERS)
    writer.writeheader()
    
    errors = []
    processed_count = 0
    
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".pdf")]
            for name in names:
                source_file = name.rsplit("/", 1)[-1]
                try:
                    pdf_bytes = zf.read(name)
                    rows = extract_pdf_rows(pdf_bytes, source_file)
                    for row in rows:
                        writer.writerow(row)
                    processed_count += 1
                except Exception as e:
                    errors.append(f"{name}: {str(e)}")
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid ZIP file.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        
    csv_buffer.seek(0)
    
    return StreamingResponse(
        iter([csv_buffer.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=extracted_vret.csv",
            "X-Processed-Count": str(processed_count),
            "X-Error-Count": str(len(errors)),
            "Access-Control-Expose-Headers": "X-Processed-Count, X-Error-Count",
        }
    )
