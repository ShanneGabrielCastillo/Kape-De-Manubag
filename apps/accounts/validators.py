"""
Profile picture upload validation.

Server-side safeguards for the profile picture upload:

* **Byte-size cap** -- blocks storage abuse before any parsing.
* **Extension allowlist** -- only ``jpg``/``jpeg``/``jfif``/``png``/``gif``/
  ``webp`` file names are accepted.
* **Content verification** -- the actual image format is re-checked with
  Pillow and must match the declared extension. This rejects renamed files
  (scripts, binaries or other formats disguised as an allowed image).
* **Dimension cap** -- rejects "decompression bomb" images whose encoded
  size is tiny but whose dimensions would decode to enormous buffers.

Django's ``ImageField`` already verifies with Pillow that an upload is a
real image (surfacing the generic "Upload a valid image" message). This
module layers the specific, user-friendly rules on top.

Only files actually uploaded in the current request are validated: when the
form is submitted without a new file the field holds the stored ``FieldFile``
from the instance, which is left untouched so existing profiles keep saving
exactly as before.
"""

import io

from PIL import Image

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile

# Extension -> the Pillow image format the file must actually contain.
# jfif is a common JPEG variant produced by some cameras/editors -- it is
# JPEG content, so it is mapped to the same format.
ALLOWED_EXTENSIONS = {
    'jpg': 'JPEG',
    'jpeg': 'JPEG',
    'jfif': 'JPEG',
    'png': 'PNG',
    'gif': 'GIF',
    'webp': 'WEBP',
}
ALLOWED_FORMATS = set(ALLOWED_EXTENSIONS.values())

# Size / dimension limits (module-level so tests and settings can reuse them).
# 8000x8000 (64 MP) is below Pillow's own 89 MP decompression-bomb warning
# threshold, so a rejected file is also flagged by Pillow itself.
MAX_SIZE_MB = 5
MAX_SIZE_BYTES = MAX_SIZE_MB * 1024 * 1024
MAX_WIDTH = 8000
MAX_HEIGHT = 8000

SUPPORTED_FORMATS_LABEL = 'JPG, PNG, GIF, or WEBP'

UNSUPPORTED_FORMAT_MESSAGE = (
    f'Unsupported file format. Please upload a {SUPPORTED_FORMATS_LABEL} image.'
)
MISMATCH_MESSAGE = (
    "The file's contents do not match its file name. "
    f'Please upload a valid {SUPPORTED_FORMATS_LABEL} image.'
)
TOO_LARGE_TEMPLATE = (
    'Your image is too large ({size} MB). '
    f'The maximum allowed size is {MAX_SIZE_MB} MB.'
)
DIMENSIONS_TEMPLATE = (
    'Your image dimensions are too large ({width} x {height} pixels). '
    f'The maximum allowed dimensions are {MAX_WIDTH} x {MAX_HEIGHT} pixels.'
)


def _format_mb(size_bytes):
    """Human-friendly megabytes (e.g. ``'4.3'``) for error messages."""
    return f'{size_bytes / (1024 * 1024):.1f}'


def validate_profile_image_upload(value):
    """Django form-field validator for the profile picture upload.

    Only uploads received in the current request are checked; when the form
    is submitted without a new file, ``value`` is the stored ``FieldFile``
    from the instance (or ``None``) and is returned untouched so existing
    profiles continue to save normally.
    """
    if not isinstance(value, UploadedFile):
        return

    # 1) Byte-size cap -- cheapest check, runs before any parsing.
    if value.size > MAX_SIZE_BYTES:
        raise ValidationError(
            TOO_LARGE_TEMPLATE.format(size=_format_mb(value.size)),
            code='profile_image_too_large',
        )

    # 2) Extension allowlist.
    name = value.name or ''
    _, dot, ext = name.rpartition('.')
    expected_format = ALLOWED_EXTENSIONS.get(ext.lower())
    if not dot or expected_format is None:
        raise ValidationError(
            UNSUPPORTED_FORMAT_MESSAGE, code='profile_image_unsupported_format',
        )

    # 3) Content check. ``verify()`` only parses the header and never decodes
    #    pixels, so a decompression bomb cannot be triggered here.
    try:
        value.seek(0)
        probe = io.BytesIO(value.read())
        image = Image.open(probe)
        image.verify()
        detected_format = (image.format or '').upper()
        width, height = image.size
    except Exception:
        raise ValidationError(
            UNSUPPORTED_FORMAT_MESSAGE, code='profile_image_unsupported_format',
        )
    finally:
        value.seek(0)  # rewind the upload so it can be stored afterwards

    if detected_format not in ALLOWED_FORMATS:
        raise ValidationError(
            UNSUPPORTED_FORMAT_MESSAGE, code='profile_image_unsupported_format',
        )
    if detected_format != expected_format:
        raise ValidationError(
            MISMATCH_MESSAGE, code='profile_image_extension_mismatch',
        )
    if width > MAX_WIDTH or height > MAX_HEIGHT:
        raise ValidationError(
            DIMENSIONS_TEMPLATE.format(width=width, height=height),
            code='profile_image_dimensions_too_large',
        )
