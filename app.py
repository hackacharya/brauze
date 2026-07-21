from __future__ import annotations

import io
import json
import logging
import os
import posixpath
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.dom import minidom

from flask import Flask, abort, g, redirect, render_template, request, send_file, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.utils import safe_join


IGNORE_MARKER = ".brauze-ignore"
DEFAULT_ROOT = "/data"
DEFAULT_WORKSPACE_HEADER = "X-Workspace"
DEFAULT_USERID_HEADER = "X-Userid"
TEXT_VIEW_EXTENSIONS = {
    ".log",
    ".md",
    ".text",
    ".txt",
    ".yaml",
    ".yml",
    ".json",
    ".csv",
    ".tsv",
    ".xml",
    ".ccd",
    ".ini",
    ".cfg",
    ".conf",
    ".hl7",
    ".er7",
    ".py",
    ".js",
    ".ts",
    ".html",
    ".css",
    ".sh",
    ".sql",
}
PDF_VIEW_EXTENSIONS = {".pdf"}
MAX_INLINE_VIEW_BYTES = 512 * 1024

app = Flask(__name__)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
        }
        if isinstance(record.msg, dict):
            payload.update(record.msg)
        else:
            payload["message"] = record.getMessage()
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def configure_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    logger = logging.getLogger("brauze")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    app.logger.handlers.clear()
    app.logger.propagate = False
    logging.getLogger("werkzeug").handlers.clear()
    logging.getLogger("werkzeug").setLevel(logging.CRITICAL)
    logging.getLogger("gunicorn.access").handlers.clear()
    logging.getLogger("gunicorn.access").setLevel(logging.CRITICAL)
    logging.getLogger("gunicorn.error").handlers.clear()
    logging.getLogger("gunicorn.error").setLevel(logging.CRITICAL)

    return logger


logger = configure_logging()


@dataclass
class Entry:
    name: str
    rel_path: str
    is_dir: bool
    size: int | None = None
    can_view_inline: bool = False


@dataclass
class CompareDocument:
    name: str
    rel_path: str
    size: int
    content: str


def get_root_path() -> Path:
    return Path(os.environ.get("BRAUZE_ROOT", DEFAULT_ROOT)).resolve()


def get_workspace_header_name() -> str:
    return os.environ.get("BRAUZE_WORKSPACE_HEADER", DEFAULT_WORKSPACE_HEADER)


def get_userid_header_name() -> str:
    return os.environ.get("BRAUZE_USERID_HEADER", DEFAULT_USERID_HEADER)


def normalize_rel_path(raw_path: str) -> str:
    cleaned = posixpath.normpath("/" + raw_path.strip("/"))
    normalized = cleaned.lstrip("/")
    return "" if normalized == "." else normalized


def resolve_path(rel_path: str) -> Path:
    root = get_root_path()
    normalized = normalize_rel_path(rel_path)
    joined = safe_join(str(root), normalized)
    if joined is None:
        abort(404)
    target = Path(joined).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        abort(404)
    return target


def is_hidden_dir(path: Path) -> bool:
    return path.is_dir() and (path / IGNORE_MARKER).exists()


def has_hidden_ancestor(path: Path, root: Path) -> bool:
    if path == root:
        return False
    for parent in [path] + list(path.parents):
        if parent == root:
            return False
        if is_hidden_dir(parent):
            return True
    return False


def format_size(size: int | None) -> str:
    if size is None:
        return "--"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def iter_entries(target: Path, root: Path) -> list[Entry]:
    entries: list[Entry] = []
    for item in sorted(target.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name.lower())):
        if item.name == IGNORE_MARKER:
            continue
        if item.is_dir() and is_hidden_dir(item):
            continue
        rel_path = item.relative_to(root).as_posix()
        if item.is_dir():
            entries.append(Entry(name=item.name, rel_path=rel_path, is_dir=True))
            continue
        entries.append(
            Entry(
                name=item.name,
                rel_path=rel_path,
                is_dir=False,
                size=item.stat().st_size,
                can_view_inline=can_view_inline(item),
            )
        )
    return entries


def auto_descend_folder(target: Path, root: Path) -> Path:
    current = target
    seen: set[Path] = set()
    while current not in seen:
        seen.add(current)
        entries = iter_entries(current, root)
        if len(entries) != 1 or not entries[0].is_dir:
            break
        child = resolve_path(entries[0].rel_path)
        if not child.exists() or not child.is_dir() or has_hidden_ancestor(child, root):
            break
        current = child
    return current


def can_view_inline(path: Path) -> bool:
    if path.suffix.lower() in TEXT_VIEW_EXTENSIONS:
        return True
    return "hl7" in path.name.lower()


def can_view_pdf(path: Path) -> bool:
    return path.suffix.lower() in PDF_VIEW_EXTENSIONS


def is_xml_file(path: Path) -> bool:
    return path.suffix.lower() in {".xml", ".ccd"}


def is_json_file(path: Path) -> bool:
    return path.suffix.lower() == ".json"


def read_text_for_view(path: Path) -> str:
    data = path.read_bytes()
    if len(data) > MAX_INLINE_VIEW_BYTES:
        abort(400, description=f"File is too large for inline view. Limit is {MAX_INLINE_VIEW_BYTES} bytes.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        abort(400, description="File is not UTF-8 text and cannot be rendered inline.")


def render_xml_for_view(path: Path) -> str:
    raw_text = read_text_for_view(path)
    try:
        return minidom.parseString(raw_text.encode("utf-8")).toprettyxml(indent="  ")
    except Exception:
        return raw_text


def render_json_for_view(path: Path) -> str:
    raw_text = read_text_for_view(path)
    try:
        return json.dumps(json.loads(raw_text), indent=2, ensure_ascii=False)
    except Exception:
        return raw_text


def get_compare_targets() -> tuple[str | None, str | None]:
    left = normalize_rel_path(request.args.get("compare_left", ""))
    right = normalize_rel_path(request.args.get("compare_right", ""))
    return (left or None, right or None)


def build_compare_document(rel_path: str, root: Path) -> CompareDocument:
    target = resolve_path(rel_path)
    if not target.exists() or not target.is_file() or has_hidden_ancestor(target.parent, root):
        abort(404)
    if not can_view_inline(target):
        abort(400, description="Compare view is supported only for text-like files.")
    return CompareDocument(
        name=target.name,
        rel_path=normalize_rel_path(rel_path),
        size=target.stat().st_size,
        content=read_text_for_view(target),
    )


def build_compare_context(root: Path) -> dict[str, object]:
    compare_left, compare_right = get_compare_targets()
    context = {
        "compare_left": compare_left,
        "compare_right": compare_right,
        "compare_docs": None,
    }
    if not compare_left or not compare_right:
        return context
    context["compare_docs"] = {
        "left": build_compare_document(compare_left, root),
        "right": build_compare_document(compare_right, root),
    }
    return context


def build_breadcrumbs(rel_path: str) -> list[dict[str, str]]:
    crumbs = [{"label": "Home", "href": ""}]
    if not rel_path:
        return crumbs
    parts = rel_path.split("/")
    current = []
    for part in parts:
        current.append(part)
        crumbs.append({"label": part, "href": "/".join(current)})
    return crumbs


def zip_directory(directory: Path, root: Path) -> io.BytesIO:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for current_path, dirnames, filenames in os.walk(directory):
            current_dir = Path(current_path)
            dirnames[:] = [
                dirname
                for dirname in dirnames
                if not is_hidden_dir(current_dir / dirname)
            ]
            for filename in filenames:
                if filename == IGNORE_MARKER:
                    continue
                file_path = current_dir / filename
                if has_hidden_ancestor(file_path.parent, root):
                    continue
                archive.write(file_path, arcname=file_path.relative_to(directory))
    stream.seek(0)
    return stream


def request_identity() -> dict[str, str | None]:
    return {
        "workspace_name": request.headers.get(get_workspace_header_name()),
        "user_name": request.headers.get(get_userid_header_name()),
    }


def client_ip() -> str | None:
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.remote_addr


def log_event(event_type: str, **fields: object) -> None:
    payload = {
        "event": event_type,
        "method": request.method,
        "route": request.path,
        "client_ip": client_ip(),
        "user_agent": request.headers.get("User-Agent"),
        **request_identity(),
        **fields,
    }
    logger.info(payload)


@app.before_request
def begin_request() -> None:
    g.started_at = time.perf_counter()
    g.log_context = {}
    g.error_logged = False


@app.after_request
def emit_request_log(response):
    if request.endpoint in {"browse", "view_file", "view_pdf", "download_file", "download_folder"} and not g.error_logged:
        duration_ms = round((time.perf_counter() - g.started_at) * 1000, 2)
        if request.endpoint in {"browse", "view_file", "view_pdf"}:
            event_type = "document_access"
        else:
            event_type = "document_download"
        log_event(
            event_type,
            action=request.endpoint,
            status_code=response.status_code,
            duration_ms=duration_ms,
            **g.log_context,
        )
    return response


@app.get("/")
@app.get("/browse/")
@app.get("/browse/<path:rel_path>")
def browse(rel_path: str = ""):
    root = get_root_path()
    if not root.exists():
        abort(500, description=f"BRAUZE_ROOT does not exist: {root}")

    target = resolve_path(rel_path)
    if not target.exists() or not target.is_dir() or has_hidden_ancestor(target, root):
        abort(404)

    target = auto_descend_folder(target, root)
    normalized_target_path = target.relative_to(root).as_posix() if target != root else ""
    if normalized_target_path != normalize_rel_path(rel_path):
        g.log_context = {
            "resource_path": normalize_rel_path(rel_path) or "/",
            "resource_type": "folder",
            "auto_descended_to": normalized_target_path or "/",
        }
        return redirect(url_for("browse", rel_path=normalized_target_path, **request.args), code=302)

    entries = iter_entries(target, root)
    compare_context = build_compare_context(root)
    g.log_context = {
        "resource_path": normalized_target_path or "/",
        "resource_type": "folder",
        "entry_count": len(entries),
    }
    if compare_context["compare_docs"]:
        g.log_context["compare_left"] = compare_context["compare_left"]
        g.log_context["compare_right"] = compare_context["compare_right"]
    return render_template(
        "index.html",
        root_path=str(root),
        current_path=normalized_target_path,
        breadcrumbs=build_breadcrumbs(normalized_target_path),
        entries=entries,
        format_size=format_size,
        viewer_file=None,
        viewer_content=None,
        viewer_mode=None,
        viewer_rendered=None,
        viewer_error=None,
        **compare_context,
    )


@app.get("/view/file/<path:rel_path>")
def view_file(rel_path: str):
    root = get_root_path()
    target = resolve_path(rel_path)
    if not target.exists() or not target.is_file() or has_hidden_ancestor(target.parent, root):
        abort(404)
    if not can_view_inline(target):
        abort(400, description="Inline view is supported only for text-like files.")

    parent_rel_path = target.parent.relative_to(root).as_posix() if target.parent != root else ""
    entries = iter_entries(target.parent, root)
    viewer_mode = "text"
    viewer_rendered = None
    viewer_content = None
    if is_xml_file(target):
        viewer_mode = "xml"
        viewer_rendered = render_xml_for_view(target)
    elif is_json_file(target):
        viewer_mode = "json"
        viewer_rendered = render_json_for_view(target)
    else:
        viewer_content = read_text_for_view(target)
    normalized_rel_path = normalize_rel_path(rel_path)
    compare_context = build_compare_context(root)
    g.log_context = {
        "resource_path": normalized_rel_path,
        "resource_type": "file",
        "size_bytes": target.stat().st_size,
        "view_mode": viewer_mode,
    }
    if compare_context["compare_docs"]:
        g.log_context["compare_left"] = compare_context["compare_left"]
        g.log_context["compare_right"] = compare_context["compare_right"]
    return render_template(
        "index.html",
        root_path=str(root),
        current_path=parent_rel_path,
        breadcrumbs=build_breadcrumbs(parent_rel_path),
        entries=entries,
        format_size=format_size,
        viewer_file={
            "name": target.name,
            "rel_path": normalized_rel_path,
            "size": target.stat().st_size,
        },
        viewer_content=viewer_content,
        viewer_mode=viewer_mode,
        viewer_rendered=viewer_rendered,
        viewer_embed_url=None,
        viewer_error=None,
        **compare_context,
    )


@app.get("/view/pdf/<path:rel_path>")
def view_pdf(rel_path: str):
    root = get_root_path()
    target = resolve_path(rel_path)
    if not target.exists() or not target.is_file() or has_hidden_ancestor(target.parent, root):
        abort(404)
    if not can_view_pdf(target):
        abort(400, description="PDF viewer is supported only for PDF files.")

    parent_rel_path = target.parent.relative_to(root).as_posix() if target.parent != root else ""
    entries = iter_entries(target.parent, root)
    normalized_rel_path = normalize_rel_path(rel_path)
    g.log_context = {
        "resource_path": normalized_rel_path,
        "resource_type": "file",
        "size_bytes": target.stat().st_size,
        "view_mode": "pdf",
    }
    return render_template(
        "index.html",
        root_path=str(root),
        current_path=parent_rel_path,
        breadcrumbs=build_breadcrumbs(parent_rel_path),
        entries=entries,
        format_size=format_size,
        viewer_file={
            "name": target.name,
            "rel_path": normalized_rel_path,
            "size": target.stat().st_size,
        },
        viewer_content=None,
        viewer_mode="pdf",
        viewer_rendered=None,
        viewer_embed_url=url_for("raw_pdf", rel_path=normalized_rel_path),
        viewer_error=None,
        **build_compare_context(root),
    )


@app.get("/raw/pdf/<path:rel_path>")
def raw_pdf(rel_path: str):
    root = get_root_path()
    target = resolve_path(rel_path)
    if not target.exists() or not target.is_file() or has_hidden_ancestor(target.parent, root):
        abort(404)
    if not can_view_pdf(target):
        abort(400, description="PDF viewer is supported only for PDF files.")
    response = send_file(
        target,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=target.name,
    )
    response.headers["Content-Disposition"] = "inline"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/download/file/<path:rel_path>")
def download_file(rel_path: str):
    root = get_root_path()
    target = resolve_path(rel_path)
    if not target.exists() or not target.is_file() or has_hidden_ancestor(target.parent, root):
        abort(404)
    g.log_context = {
        "resource_path": normalize_rel_path(rel_path),
        "resource_type": "file",
        "size_bytes": target.stat().st_size,
    }
    return send_file(target, as_attachment=True, download_name=target.name)


@app.get("/download/folder/<path:rel_path>")
def download_folder(rel_path: str):
    root = get_root_path()
    target = resolve_path(rel_path)
    if not target.exists() or not target.is_dir() or has_hidden_ancestor(target, root):
        abort(404)

    archive = zip_directory(target, root)
    archive_name = f"{target.name or 'archive'}.zip"
    g.log_context = {
        "resource_path": normalize_rel_path(rel_path),
        "resource_type": "folder",
        "archive_name": archive_name,
    }
    return send_file(
        archive,
        mimetype="application/zip",
        as_attachment=True,
        download_name=archive_name,
    )


@app.errorhandler(404)
def not_found(error):
    g.error_logged = True
    log_event(
        "document_error",
        action=request.endpoint,
        status_code=404,
        error_type="not_found",
        error_message=getattr(error, "description", "Not found"),
        **getattr(g, "log_context", {}),
    )
    return render_template("error.html", title="Not Found", message="That file or folder is unavailable."), 404


@app.errorhandler(400)
def bad_request(error):
    g.error_logged = True
    message = getattr(error, "description", "The request could not be completed.")
    log_event(
        "document_error",
        action=request.endpoint,
        status_code=400,
        error_type="bad_request",
        error_message=message,
        **getattr(g, "log_context", {}),
    )
    return render_template("error.html", title="Cannot View File", message=message), 400


@app.errorhandler(500)
def internal_error(error):
    message = getattr(error, "description", "The server could not complete the request.")
    g.error_logged = True
    log_event(
        "document_error",
        action=request.endpoint,
        status_code=500,
        error_type="internal_error",
        error_message=message,
        **getattr(g, "log_context", {}),
    )
    return render_template("error.html", title="Server Error", message=message), 500


@app.errorhandler(Exception)
def unhandled_error(error: Exception):
    if isinstance(error, HTTPException):
        raise error
    g.error_logged = True
    log_event(
        "document_error",
        action=request.endpoint,
        status_code=500,
        error_type=error.__class__.__name__,
        error_message=str(error),
        **getattr(g, "log_context", {}),
    )
    return render_template("error.html", title="Server Error", message="The server could not complete the request."), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
