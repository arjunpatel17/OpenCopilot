import os
import mimetypes
from pathlib import Path
from datetime import datetime, timezone
from app.config import settings
from app.models.file import BlobFileInfo, FileTreeNode, FileMetadata
from io import BytesIO
import zipfile

# ---------- backend selection ----------
_use_azure = bool(settings.azure_storage_connection_string)


# ========== Local filesystem backend ==========

def _local_root() -> Path:
    p = Path(settings.workspace_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _local_path(blob_path: str) -> Path:
    # Prevent path traversal
    clean = Path(blob_path)
    if clean.is_absolute() or ".." in clean.parts:
        raise ValueError("Invalid blob path")
    return _local_root() / clean


def _local_list_blobs(prefix: str = "") -> list[BlobFileInfo]:
    root = _local_root()
    search_dir = root / prefix if prefix else root
    items: list[BlobFileInfo] = []
    if not search_dir.exists():
        return items
    for entry in sorted(search_dir.iterdir()):
        rel = str(entry.relative_to(root))
        if entry.is_dir():
            items.append(BlobFileInfo(name=entry.name, path=rel + "/", is_folder=True))
        else:
            stat = entry.stat()
            ct = mimetypes.guess_type(entry.name)[0] or "application/octet-stream"
            items.append(BlobFileInfo(
                name=entry.name, path=rel, is_folder=False,
                size=stat.st_size,
                last_modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                content_type=ct,
            ))
    return items


def _local_get_file_tree(prefix: str = "") -> list[FileTreeNode]:
    root = _local_root()
    search_dir = root / prefix if prefix else root

    def _build(directory: Path, depth: int = 0) -> list[FileTreeNode]:
        nodes: list[FileTreeNode] = []
        if not directory.exists():
            return nodes
        entries = sorted(directory.iterdir(), key=lambda e: (not e.is_dir(), e.name))
        for entry in entries:
            rel = str(entry.relative_to(root))
            if entry.is_dir():
                children = _build(entry, depth + 1)
                nodes.append(FileTreeNode(name=entry.name, path=rel + "/", is_folder=True, children=children))
            else:
                stat = entry.stat()
                ct = mimetypes.guess_type(entry.name)[0] or "application/octet-stream"
                nodes.append(FileTreeNode(
                    name=entry.name, path=rel, is_folder=False,
                    size=stat.st_size,
                    last_modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                    content_type=ct,
                ))
        return nodes

    return _build(search_dir)


def _local_get_blob_content(path: str) -> bytes:
    fp = _local_path(path)
    return fp.read_bytes()


def _local_get_blob_metadata(path: str) -> FileMetadata:
    fp = _local_path(path)
    stat = fp.stat()
    ct = mimetypes.guess_type(fp.name)[0] or "application/octet-stream"
    return FileMetadata(
        name=fp.name, path=path, size=stat.st_size,
        last_modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        content_type=ct,
    )


def _local_upload_blob(path: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    fp = _local_path(path)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(data)
    return path


def _local_delete_blob(path: str) -> None:
    fp = _local_path(path)
    if path.endswith("/"):
        import shutil
        if fp.exists():
            shutil.rmtree(fp)
    else:
        if fp.exists():
            fp.unlink()


def _local_download_folder_as_zip(prefix: str) -> bytes:
    root = _local_root()
    search_dir = root / prefix
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        if search_dir.exists():
            for fp in search_dir.rglob("*"):
                if fp.is_file():
                    arcname = str(fp.relative_to(search_dir))
                    zf.writestr(arcname, fp.read_bytes())
    buffer.seek(0)
    return buffer.read()


# ========== Azure Blob Storage backend ==========

def _get_container_client():
    from azure.storage.blob import BlobServiceClient
    blob_service = BlobServiceClient.from_connection_string(settings.azure_storage_connection_string)
    container = blob_service.get_container_client(settings.azure_storage_container)
    try:
        container.get_container_properties()
    except Exception:
        container.create_container()
    return container


def _azure_list_blobs(prefix: str = "") -> list[BlobFileInfo]:
    container = _get_container_client()
    items: list[BlobFileInfo] = []
    seen_folders: set[str] = set()
    for blob in container.list_blobs(name_starts_with=prefix or None):
        rel = blob.name[len(prefix):] if prefix else blob.name
        parts = rel.split("/")
        if len(parts) > 1:
            folder_name = parts[0]
            folder_path = f"{prefix}{folder_name}/"
            if folder_path not in seen_folders:
                seen_folders.add(folder_path)
                items.append(BlobFileInfo(name=folder_name, path=folder_path, is_folder=True))
        else:
            items.append(BlobFileInfo(
                name=blob.name.rsplit("/", 1)[-1], path=blob.name, is_folder=False,
                size=blob.size, last_modified=blob.last_modified,
                content_type=blob.content_settings.content_type if blob.content_settings else None,
            ))
    return items


def _azure_get_file_tree(prefix: str = "") -> list[FileTreeNode]:
    container = _get_container_client()
    root_nodes: dict[str, FileTreeNode] = {}
    for blob in container.list_blobs(name_starts_with=prefix or None):
        rel = blob.name[len(prefix):] if prefix else blob.name
        parts = rel.split("/")
        current_level = root_nodes
        for i, part in enumerate(parts):
            is_last = i == len(parts) - 1
            path_so_far = prefix + "/".join(parts[: i + 1]) + ("" if is_last else "/")
            if part not in current_level:
                node = FileTreeNode(
                    name=part, path=path_so_far, is_folder=not is_last,
                    size=blob.size if is_last else None,
                    last_modified=blob.last_modified if is_last else None,
                    content_type=(blob.content_settings.content_type if blob.content_settings else None) if is_last else None,
                )
                current_level[part] = node
            else:
                node = current_level[part]
            if not is_last:
                if not hasattr(node, "_children_dict"):
                    node._children_dict = {}  # type: ignore
                current_level = node._children_dict  # type: ignore

    def _flatten(nodes: dict[str, FileTreeNode]) -> list[FileTreeNode]:
        result = []
        for node in sorted(nodes.values(), key=lambda n: (not n.is_folder, n.name)):
            if hasattr(node, "_children_dict"):
                node.children = _flatten(node._children_dict)  # type: ignore
                delattr(node, "_children_dict")
            result.append(node)
        return result

    return _flatten(root_nodes)


def _azure_get_blob_content(path: str) -> bytes:
    container = _get_container_client()
    return container.download_blob(path).readall()


def _azure_get_blob_metadata(path: str) -> FileMetadata:
    container = _get_container_client()
    props = container.get_blob_client(path).get_blob_properties()
    ct = props.content_settings.content_type if props.content_settings else "application/octet-stream"
    return FileMetadata(
        name=path.rsplit("/", 1)[-1], path=path, size=props.size,
        last_modified=props.last_modified,
        content_type=ct or "application/octet-stream",
        etag=props.etag,
    )


def _azure_upload_blob(path: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    from azure.storage.blob import ContentSettings
    container = _get_container_client()
    container.upload_blob(
        name=path, data=data, overwrite=True,
        content_settings=ContentSettings(content_type=content_type),
    )
    return path


def _azure_delete_blob(path: str) -> None:
    container = _get_container_client()
    if path.endswith("/"):
        for blob in container.list_blobs(name_starts_with=path):
            container.delete_blob(blob.name)
    else:
        container.delete_blob(path)


def _azure_download_folder_as_zip(prefix: str) -> bytes:
    container = _get_container_client()
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for blob in container.list_blobs(name_starts_with=prefix):
            data = container.download_blob(blob.name).readall()
            arcname = blob.name[len(prefix):] if prefix else blob.name
            zf.writestr(arcname, data)
    buffer.seek(0)
    return buffer.read()


# ========== Public API (delegates to local or Azure) ==========

if _use_azure:
    list_blobs = _azure_list_blobs
    get_file_tree = _azure_get_file_tree
    get_blob_content = _azure_get_blob_content
    get_blob_metadata = _azure_get_blob_metadata
    upload_blob = _azure_upload_blob
    delete_blob = _azure_delete_blob
    download_folder_as_zip = _azure_download_folder_as_zip
else:
    list_blobs = _local_list_blobs
    get_file_tree = _local_get_file_tree
    get_blob_content = _local_get_blob_content
    get_blob_metadata = _local_get_blob_metadata
    upload_blob = _local_upload_blob
    delete_blob = _local_delete_blob
    download_folder_as_zip = _local_download_folder_as_zip


def sync_workspace_to_storage() -> int:
    """Scan the local workspace directory and upload any files to blob storage.
    Skips hidden directories (except .github) and the sessions/ prefix.
    Returns the number of files synced."""
    if not _use_azure:
        return 0
    workspace = Path(settings.workspace_dir)
    if not workspace.exists():
        return 0
    count = 0
    for fp in workspace.rglob("*"):
        if not fp.is_file():
            continue
        rel = fp.relative_to(workspace)
        parts = rel.parts
        # Skip hidden dirs (except .github), sessions, and __pycache__
        if any((p.startswith(".") and p != ".github") or p == "__pycache__" for p in parts):
            continue
        if parts[0] == "sessions":
            continue
        rel_str = str(rel)
        ct = mimetypes.guess_type(fp.name)[0] or "application/octet-stream"
        data = fp.read_bytes()
        _azure_upload_blob(rel_str, data, ct)
        count += 1
    return count


def restore_workspace_from_storage() -> int:
    """Download all files from blob storage into the local workspace directory.
    This restores agents, skills, data files, and tools that were previously
    synced to blob storage. Skips sessions/ prefix.
    Called at startup to ensure the workspace has the latest persisted state.
    Returns the number of files restored."""
    if not _use_azure:
        return 0
    workspace = Path(settings.workspace_dir)
    workspace.mkdir(parents=True, exist_ok=True)
    container = _get_container_client()
    count = 0
    for blob in container.list_blobs():
        name = blob.name
        parts = name.split("/")
        # Skip sessions and __pycache__
        if parts[0] == "sessions" or "__pycache__" in parts:
            continue
        local_path = workspace / name
        local_path.parent.mkdir(parents=True, exist_ok=True)
        data = container.download_blob(name).readall()
        local_path.write_bytes(data)
        count += 1
    return count


def restore_data_from_storage() -> int:
    """Download data files (data/ prefix) from blob storage to local workspace.
    Called before agent runs to ensure the agent always sees the latest
    persisted data regardless of the local file state.
    Returns the number of files restored."""
    if not _use_azure:
        return 0
    workspace = Path(settings.workspace_dir)
    workspace.mkdir(parents=True, exist_ok=True)
    container = _get_container_client()
    count = 0
    for blob in container.list_blobs(name_starts_with="data/"):
        local_path = workspace / blob.name
        local_path.parent.mkdir(parents=True, exist_ok=True)
        data = container.download_blob(blob.name).readall()
        local_path.write_bytes(data)
        count += 1
    return count
