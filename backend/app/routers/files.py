from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import Response
from app.auth import get_current_user
from app.models.file import BlobFileInfo, FileTreeNode, FileMetadata
from app.services import blob_storage

router = APIRouter(prefix="/api/files", tags=["files"])


@router.get("", response_model=list[BlobFileInfo])
async def list_files(
    prefix: str = Query("", description="Folder prefix to list"),
    user: dict = Depends(get_current_user),
):
    return blob_storage.list_blobs(prefix)


@router.get("/tree", response_model=list[FileTreeNode])
async def get_file_tree(
    prefix: str = Query("", description="Root prefix for tree"),
    user: dict = Depends(get_current_user),
):
    return blob_storage.get_file_tree(prefix)


@router.get("/content/{path:path}")
async def get_file_content(path: str, user: dict = Depends(get_current_user)):
    try:
        data = blob_storage.get_blob_content(path)
    except Exception:
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    meta = blob_storage.get_blob_metadata(path)
    return Response(
        content=data,
        media_type=meta.content_type,
        headers={"Content-Disposition": f'inline; filename="{meta.name}"'},
    )


@router.get("/meta/{path:path}", response_model=FileMetadata)
async def get_file_metadata(path: str, user: dict = Depends(get_current_user)):
    try:
        return blob_storage.get_blob_metadata(path)
    except Exception:
        raise HTTPException(status_code=404, detail=f"File not found: {path}")


@router.get("/download/{path:path}")
async def download_file(path: str, user: dict = Depends(get_current_user)):
    if path.endswith("/"):
        # Download folder as zip
        try:
            zip_data = blob_storage.download_folder_as_zip(path)
        except Exception:
            raise HTTPException(status_code=404, detail=f"Folder not found: {path}")
        folder_name = path.rstrip("/").rsplit("/", 1)[-1]
        return Response(
            content=zip_data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{folder_name}.zip"'},
        )
    else:
        try:
            data = blob_storage.get_blob_content(path)
        except Exception:
            raise HTTPException(status_code=404, detail=f"File not found: {path}")
        filename = path.rsplit("/", 1)[-1]
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    path: str = Query("", description="Destination path prefix"),
    user: dict = Depends(get_current_user),
):
    data = await file.read()
    dest = f"{path}{file.filename}" if path.endswith("/") else (f"{path}/{file.filename}" if path else file.filename)
    content_type = file.content_type or "application/octet-stream"
    blob_path = blob_storage.upload_blob(dest, data, content_type)
    return {"path": blob_path, "size": len(data)}


@router.delete("/{path:path}", status_code=204)
async def delete_file(path: str, user: dict = Depends(get_current_user)):
    try:
        blob_storage.delete_blob(path)
    except Exception:
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
