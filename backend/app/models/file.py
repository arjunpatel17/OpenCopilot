from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class BlobFileInfo(BaseModel):
    name: str
    path: str
    is_folder: bool = False
    size: Optional[int] = None
    last_modified: Optional[datetime] = None
    content_type: Optional[str] = None


class FileTreeNode(BaseModel):
    name: str
    path: str
    is_folder: bool = False
    size: Optional[int] = None
    last_modified: Optional[datetime] = None
    content_type: Optional[str] = None
    children: list["FileTreeNode"] = []


class FileMetadata(BaseModel):
    name: str
    path: str
    size: int
    last_modified: datetime
    content_type: str
    etag: Optional[str] = None
