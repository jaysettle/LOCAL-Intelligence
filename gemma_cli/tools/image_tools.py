#!/usr/bin/env python3
"""
Image tools: `view_image` lets the model LOOK at an image — an image file, or a
picture embedded in a Word / PowerPoint / Excel / OpenDocument / EPUB / PDF file.

gemma4 is multimodal, but until this tool it could only see images the user
attached by hand (/image, /paste). Asked to "make a sheet of the tags circled in
red boxes in this .docx", it read the text layer, got three lines, had no way to
know the answer was in a picture, and thrashed for 25 tool calls. Now the model
is told when a document has pictures (`describe_embedded`, used by
read_document) and has a tool that shows them.

Measured on gemma4:12b / Ollama 0.33 before this was written:

* Ollama honours `images` on a `tool` message, so the tool message answering a
  view_image call carries the pictures — no fake user turn. A control turn with
  no image hallucinated; the same turn with the image named exactly the marked
  tags, and prompt_eval_count rose by the image's cost.
* Every image costs ~260 tokens whatever its pixel size: the vision encoder
  resizes to a fixed frame. On a 1920x1080 HMI-style screen with 16 px tag text,
  the full frame found all four red-marked tags but MISREAD digits in two
  (PT-9079 for PT-9779) — plausible, silent, wrong. The same screen as a 2x2
  grid of overlapping tiles read 4/4 exactly. So images whose longer side
  exceeds TILE_ABOVE are sent as an overview plus four zoomed tiles.

Tools return strings (the contract every other tool follows), so this returns a
`ToolOutput`: a `str` subclass that also carries `.images` (base64 PNGs).
Everything that treats a tool result as text keeps working; run_turn looks for
`.images` and attaches them to the tool message.

Every image goes through Pillow first: transparency flattened onto white,
palette/CMYK/16-bit modes to RGB, animated images to their first frame, Word's
EMF/WMF drawings rendered (Windows only), always re-encoded as PNG — a format
Ollama decodes reliably, lossless for the thin lines and small text that
screenshots are made of.
"""

import base64
import io
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".emf", ".wmf")

TILE_ABOVE = 1280          # longer side (px) beyond which an image is also sent as 2x2 tiles
TILE_OVERLAP = 0.08        # tiles overlap so a label on a seam is whole in at least one tile
MAX_SIDE = 1536            # per image sent; the encoder resizes to its own frame anyway
MAX_IMAGE_SLOTS = 5        # images per view_image call: one tiled picture, or up to five small ones
MAX_SOURCE_BYTES = 50 * 1024 * 1024
_NAMES_IN_NOTE = 8
_TILE_NAMES = "top-left, top-right, bottom-left, bottom-right"

# Where each container keeps its pictures. EPUB images live anywhere in the book.
_ZIP_MEDIA_DIRS = {
    "docx": ("word/media/",),
    "pptx": ("ppt/media/",),
    "xlsx": ("xl/media/",),
    "odt": ("Pictures/",), "ods": ("Pictures/",), "odp": ("Pictures/",), "odf": ("Pictures/",),
}


class ToolOutput(str):
    """A tool result that also carries images for the model to look at.

    Still a `str`: rendering, loop detection, transcripts and sessions all keep
    treating it as text. run_turn reads `.images` and attaches them to the tool
    message. String operations return a plain str (the images do not survive
    slicing or concatenation) — pass the object through untouched.
    """

    images: List[str]

    def __new__(cls, text: str, images: Optional[List[str]] = None):
        obj = super().__new__(cls, text)
        obj.images = list(images or [])
        return obj


@dataclass
class EmbeddedImage:
    label: str                      # "image1.png", or "page 3 image 1" for a PDF
    page: int                       # 1-based PDF page; 0 for other containers
    load: Callable[[], Any]         # -> bytes or a PIL.Image, decoded on demand


@dataclass
class Encoded:
    images: List[str]               # base64 PNGs: [overview] or [overview, 4 tiles]
    size: Tuple[int, int]           # original pixel size
    tiled: bool

    def describe(self) -> str:
        w, h = self.size
        if self.tiled:
            return f"{w}x{h}, sent as an overview plus 4 zoomed tiles ({_TILE_NAMES}) so small text stays legible"
        return f"{w}x{h}"


# ---------------------------------------------------------------------------
# Normalising pixels for the model
# ---------------------------------------------------------------------------

def _flatten(source: Any):
    """bytes / path / PIL.Image -> (RGB PIL.Image, original size). ValueError if undecodable."""
    from PIL import Image

    try:
        if isinstance(source, Image.Image):
            im = source
        elif isinstance(source, (bytes, bytearray)):
            im = Image.open(io.BytesIO(bytes(source)))
        else:
            im = Image.open(str(source))
        if (im.format or "").upper() in ("WMF", "EMF"):
            try:
                im.load(dpi=144)    # Word drawings default to a tiny 72 dpi
            except TypeError:
                im.load()
        else:
            im.load()
        if getattr(im, "is_animated", False):
            im.seek(0)
        size = im.size
        if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            flat = Image.new("RGB", rgba.size, "white")
            flat.paste(rgba, mask=rgba.getchannel("A"))
        else:
            flat = im.convert("RGB")
    except Exception as e:
        raise ValueError(_decode_failure(e))
    return flat, size


def _decode_failure(e: Exception) -> str:
    text = str(e) or type(e).__name__
    if "WMF" in text.upper() or "EMF" in text.upper() or "loader" in text.lower():
        return "a vector drawing (EMF/WMF) that cannot be rendered on this system"
    if "cannot identify image file" in text:
        return "not an image format that can be decoded"
    return f"could not be decoded ({text})"


def _png_b64(im) -> str:
    from PIL import Image

    if max(im.size) > MAX_SIDE:
        im = im.copy()
        im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _tiles(im) -> List[Any]:
    """2x2 overlapping crops, in reading order."""
    w, h = im.size
    tw, th = w // 2, h // 2
    ox, oy = int(tw * TILE_OVERLAP), int(th * TILE_OVERLAP)
    out = []
    for r in range(2):
        for c in range(2):
            box = (max(0, c * tw - ox), max(0, r * th - oy),
                   min(w, (c + 1) * tw + ox), min(h, (r + 1) * th + oy))
            out.append(im.crop(box))
    return out


def encode_for_model(source: Any) -> Encoded:
    """Any image -> what to send the model: one PNG, or an overview plus tiles
    when the image is big enough that small text would blur (see module doc)."""
    flat, size = _flatten(source)
    if max(size) <= TILE_ABOVE:
        return Encoded([_png_b64(flat)], size, False)
    return Encoded([_png_b64(flat)] + [_png_b64(t) for t in _tiles(flat)], size, True)


def encode_user_images(paths: Optional[List[Any]]) -> Tuple[List[str], str]:
    """For images the USER attaches (/image, -i, /paste): (base64 list, note).

    The note is appended to the user's message so the model knows what it is
    looking at — that five images are one screenshot in tiles, or that an
    attachment could not be read (without it, a small model describes an image
    it never received).
    """
    images: List[str] = []
    notes: List[str] = []
    for raw in paths or []:
        p = Path(os.path.expanduser(str(raw)))
        name = p.name or str(raw)
        if not p.is_file():
            notes.append(f"[Attached image {name} was not found, so no image is attached.]")
            continue
        if p.stat().st_size > MAX_SOURCE_BYTES:
            notes.append(f"[Attached image {name} is too large to send.]")
            continue
        try:
            enc = encode_for_model(p)
        except ValueError as e:
            notes.append(f"[Attached image {name} is {e}, so no image is attached.]")
            continue
        images.extend(enc.images)
        if enc.tiled:
            notes.append(f"[Attached image {name} ({enc.describe()}).]")
    return images, "\n".join(notes)


# ---------------------------------------------------------------------------
# Pictures inside documents
# ---------------------------------------------------------------------------

def _natural_key(name: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def _is_image_name(name: str) -> bool:
    return name.lower().endswith(IMAGE_EXTS)


def list_embedded_images(path: Path, kind: str) -> List[EmbeddedImage]:
    """Pictures inside a document, in a stable order. Cheap: nothing is decoded
    until an image's `load()` is called. Never raises — an unreadable container
    simply has no images to offer."""
    try:
        if kind == "pdf":
            return _pdf_images(path)
        if kind in _ZIP_MEDIA_DIRS or kind == "epub":
            return _zip_images(path, kind)
    except Exception:
        pass
    return []


def _zip_images(path: Path, kind: str) -> List[EmbeddedImage]:
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
    dirs = _ZIP_MEDIA_DIRS.get(kind)
    chosen = []
    for info in infos:
        name = info.filename
        if info.is_dir() or not _is_image_name(name) or info.file_size > MAX_SOURCE_BYTES:
            continue
        if dirs is not None and not name.startswith(dirs):
            continue
        if dirs is None and name.upper().startswith("META-INF/"):
            continue
        chosen.append(name)
    chosen.sort(key=_natural_key)

    def loader(member: str) -> Callable[[], bytes]:
        def load() -> bytes:
            with zipfile.ZipFile(path) as z:
                return z.read(member)
        return load

    return [EmbeddedImage(label=os.path.basename(n), page=0, load=loader(n)) for n in chosen]


def _pdf_images(path: Path) -> List[EmbeddedImage]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            if reader.decrypt("") == 0:
                return []
        except Exception:
            return []

    found: List[EmbeddedImage] = []
    for page_no, page in enumerate(reader.pages, 1):
        try:
            count = len(page.images)          # names only; nothing decoded yet
        except Exception:
            continue
        for j in range(count):
            def load(p=page, j=j):
                return p.images[j].image      # PIL image, decoded on demand
            found.append(EmbeddedImage(label=f"page {page_no} image {j + 1}", page=page_no, load=load))
    return found


def page_ranges(pages: List[int]) -> str:
    pages = sorted(set(pages))
    out: List[str] = []
    start = prev = None
    for p in pages:
        if start is None:
            start = prev = p
        elif p == prev + 1:
            prev = p
        else:
            out.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = p
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ", ".join(out)


def describe_embedded(images: List[EmbeddedImage], path: Path, kind: str) -> str:
    """The note read_document puts at the top of its output. '' when no images."""
    if not images:
        return ""
    n = len(images)
    if kind == "pdf":
        return (
            f"[Images: this PDF also contains {n} embedded image(s) on page(s) "
            f"{page_ranges([im.page for im in images])} that text extraction cannot see. If the answer "
            f"may be in a picture - a screenshot, a diagram, a marked-up drawing - look at it: call "
            f"view_image with path=\"{path}\" and page=N.]"
        )
    names = ", ".join(im.label for im in images[:_NAMES_IN_NOTE])
    if n > _NAMES_IN_NOTE:
        names += f", ... (+{n - _NAMES_IN_NOTE} more)"
    which = " and index=1" if n > 1 else ""
    return (
        f"[Images: this document also contains {n} embedded image(s) that text extraction cannot "
        f"see ({names}). If the answer may be in a picture - a screenshot, a diagram, tags marked "
        f"up with boxes or circles - look at them: call view_image with path=\"{path}\"{which}.]"
    )


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------

def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _is_image_file(path: Path) -> bool:
    """An image with a missing or misleading extension, identified by content."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return bool(im.format)
    except Exception:
        return False


def view_image(inp: Dict[str, Any]) -> str:
    """Show an image file, or pictures embedded in a document, to the model."""
    raw = str(inp.get("path", "")).strip().strip('"')
    if not raw:
        return "Error: 'path' is required"
    path = Path(os.path.expanduser(raw))
    if not path.exists():
        return f"Error: File not found: {path}"
    if path.is_dir():
        return f"Error: Path is a directory, not a file: {path}"
    index, page = _int(inp.get("index")), _int(inp.get("page"))

    # 1) An image file.
    if path.suffix.lower() in IMAGE_EXTS or _is_image_file(path):
        if path.stat().st_size > MAX_SOURCE_BYTES:
            return f"Error: {path.name} is larger than {MAX_SOURCE_BYTES // (1024 * 1024)} MB."
        try:
            enc = encode_for_model(path)
        except ValueError as e:
            return f"Error: {path.name} is {e}."
        return ToolOutput(f"Showing {path.name} ({enc.describe()}). Look at it and answer the user's request.",
                          enc.images)

    # 2) A document that may contain pictures.
    from .doc_tools import _resolve_kind

    kind, _note = _resolve_kind(path)
    images = list_embedded_images(path, kind)
    if not images:
        if kind == "pdf":
            return (f"{path.name} contains no embedded images that can be extracted. (Vector drawings in "
                    "a PDF are not images and cannot be viewed.) Use read_document for its text.")
        if kind in _ZIP_MEDIA_DIRS or kind == "epub":
            return f"{path.name} contains no embedded images. Use read_document for its text."
        return (f"Error: {path.name} is not an image, and not a document type that holds pictures "
                "(Word, PowerPoint, Excel, OpenDocument, EPUB, PDF).")

    pool = images
    if kind == "pdf" and page:
        pool = [im for im in images if im.page == page]
        if not pool:
            return (f"Page {page} of {path.name} has no images. Images are on page(s): "
                    f"{page_ranges([im.page for im in images])}.")
    total = len(pool)
    if index and not 1 <= index <= total:
        return f"Error: index {index} is out of range - {path.name} has {total} image(s) here (1-{total})."
    candidates = [(index, pool[index - 1])] if index else list(enumerate(pool, 1))

    # Fill up to MAX_IMAGE_SLOTS: one tiled picture uses five, a small one uses one.
    sent: List[str] = []
    lines: List[str] = []
    last = 0
    for i, im in candidates:
        try:
            enc = encode_for_model(im.load())
        except ValueError as e:
            lines.append(f"[{i}] {im.label} - {e}")
            last = i
            continue
        except Exception as e:
            lines.append(f"[{i}] {im.label} - could not be extracted ({e})")
            last = i
            continue
        if sent and len(sent) + len(enc.images) > MAX_IMAGE_SLOTS:
            break
        sent.extend(enc.images)
        lines.append(f"[{i}] {im.label} {enc.describe()}")
        last = i
        if len(sent) >= MAX_IMAGE_SLOTS:
            break

    if not sent:
        return f"Error: none of the requested images in {path.name} could be shown: " + "; ".join(lines)

    scope = f" on page {page}" if (kind == "pdf" and page) else ""
    text = f"Showing images from {path.name}{scope} ({total} in total): " + "; ".join(lines) + "."
    if not index and last < total:
        text += f" More images exist: call view_image again with index={last + 1} to see the next."
    text += " Look at the image(s) and answer the user's request."
    return ToolOutput(text, sent)
