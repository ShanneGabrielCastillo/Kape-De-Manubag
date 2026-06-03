"""
WSGI configuration for Kape De Manubag on PythonAnywhere.

HOW TO USE:
1. In PythonAnywhere Web tab, click the link to your WSGI file
   (e.g. /var/www/yourusername_pythonanywhere_com_wsgi.py)
2. Delete ALL existing content in that file
3. Paste the contents of THIS file
4. Replace 'yourusername' below with your actual PythonAnywhere username
5. Save the file
6. Click Reload in the Web tab
"""

import sys
import os
from dotenv import load_dotenv

# ── 1. Project path ───────────────────────────────────────────────────────────
# Replace 'yourusername' with your actual PythonAnywhere username
project_home = '/home/yourusername/kape_de_manubag_system'

if project_home not in sys.path:
    sys.path.insert(0, project_home)

# ── 2. Load .env variables ────────────────────────────────────────────────────
load_dotenv(os.path.join(project_home, '.env'))

# ── 3. Point Django at the settings module ────────────────────────────────────
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'kape_de_manubag.settings')

# ── 4. Create the WSGI application ───────────────────────────────────────────
from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()
