"""
Project-level Django system checks.

Registers a check that scans every project template for ``{% static %}``
references and verifies the referenced file actually exists in a static
directory. This turns a broken asset reference into a loud, early failure
(``manage.py check`` / deploy pipeline) instead of a silent 404 in
development or a "Missing staticfiles manifest entry" crash in production.

Scope: project HTML templates under ``templates/`` and ``apps/*/templates``
only. It does not inspect the contents of static CSS/JS files themselves.
"""
import re
from pathlib import Path

from django.conf import settings
from django.core.checks import Error, Warning, register, Tags

# Matches {% static 'path' %} and {% static "path" %} (quoted literals),
# including the {% static 'path' as var %} form.
_STATIC_TAG_RE = re.compile(
    r"""\{%\s*static\s+(['"])(.*?)\1(?:\s+as\s+\w+)?\s*%\}"""
)

# Matches hard-coded /static/... URLs inside src/href attributes, which
# bypass manifest hashing and 404 once collectstatic renames files.
_HARDCODED_STATIC_RE = re.compile(
    r"""(?:src|href)\s*=\s*["']/static/[^"']+["']"""
)


def _project_template_dirs():
    """Yield template directories that belong to this project (not the venv)."""
    yield Path(settings.BASE_DIR) / 'templates'
    apps_dir = Path(settings.BASE_DIR) / 'apps'
    if apps_dir.is_dir():
        for app_dir in apps_dir.iterdir():
            if app_dir.is_dir():
                tdir = app_dir / 'templates'
                if tdir.is_dir():
                    yield tdir


@register(Tags.staticfiles)
def check_template_static_references(app_configs, **kwargs):
    """Verify every {% static %} reference in project templates resolves."""
    from django.contrib.staticfiles import finders

    errors = []

    for template_dir in _project_template_dirs():
        for template_path in sorted(template_dir.rglob('*.html')):
            try:
                source = template_path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue

            rel = template_path.relative_to(settings.BASE_DIR)

            for match in _STATIC_TAG_RE.finditer(source):
                asset = match.group(2)
                if asset.startswith(('http://', 'https://', '//')):
                    continue  # external URL, not a project asset
                if not finders.find(asset):
                    errors.append(Error(
                        f"Template '{rel}' references static file '{asset}', "
                        "which does not exist in any static directory.",
                        hint=f"Create 'static/{asset}' or fix the reference.",
                        id='kdm.static.E001',
                    ))

            for match in _HARDCODED_STATIC_RE.finditer(source):
                errors.append(Warning(
                    f"Template '{rel}' uses a hard-coded '/static/...' URL "
                    f"({match.group(0)}). Manifest storage renames collected "
                    "files, so this will 404 in production.",
                    hint="Use the {% static %} template tag instead.",
                    id='kdm.static.W001',
                ))

    return errors
