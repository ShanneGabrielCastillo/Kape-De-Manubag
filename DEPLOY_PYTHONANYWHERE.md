# Kape De Manubag — PythonAnywhere Deployment Guide

---

## PHASE 1 — Audit Results

| Item | Status | Notes |
|------|--------|-------|
| DEBUG default | ✅ Fixed | Now defaults to False |
| ALLOWED_HOSTS | ✅ Fixed | Reads from .env |
| CSRF_TRUSTED_ORIGINS | ✅ Fixed | Added, reads from .env |
| STORAGES (WhiteNoise) | ✅ Fixed | Updated to Django 4.2 STORAGES dict |
| requirements.txt | ✅ Fixed | Pinned exact versions |
| SQLite database | ✅ Ready | No changes needed |
| Media files | ✅ Ready | Mapped via PythonAnywhere config |
| Static files | ✅ Ready | WhiteNoise + collectstatic |

---

## PHASE 2 — PythonAnywhere Account Setup

1. Go to **https://www.pythonanywhere.com**
2. Click **Start running Python online in less than a minute** → **Create a Beginner account**
3. Choose a username — this becomes your URL: `yourusername.pythonanywhere.com`
4. Verify your email

---

## PHASE 3 — Upload the Project

### Via GitHub (Recommended)

#### Step 1 — Push to GitHub (do this on your local machine)

Open PowerShell in `C:\Users\Shecile\kape_de_manubag_system` and run:

```powershell
# Initialize git (skip if already done)
git init

# Stage everything
git add .

# First commit
git commit -m "Initial commit — Kape De Manubag system"

# Connect to your GitHub repo
# Create the repo first at https://github.com/new  (name: kape_de_manubag_system)
# Then run:
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/kape_de_manubag_system.git
git branch -M main
git push -u origin main
```

#### Step 2 — Clone on PythonAnywhere

Open a **Bash console** on PythonAnywhere and run:

```bash
cd ~
git clone https://github.com/YOUR_GITHUB_USERNAME/kape_de_manubag_system.git
```

#### Updating later (after code changes)

On your local machine:
```powershell
git add .
git commit -m "your message"
git push
```

On PythonAnywhere Bash console:
```bash
cd ~/kape_de_manubag_system
git pull
source venv/bin/activate
python manage.py migrate       # only if models changed
python manage.py collectstatic --noinput
```
Then click **Reload** in the Web tab.

---

## PHASE 4 — Virtual Environment & Dependencies

In the PythonAnywhere **Bash console**:

```bash
# Navigate to project
cd ~/kape_de_manubag_system

# Create virtual environment (use Python 3.10 or 3.11)
python3.11 -m venv venv

# Activate it
source venv/bin/activate

# Upgrade pip first
pip install --upgrade pip

# Install all dependencies
pip install -r requirements.txt
```

---

## PHASE 5 — Create the .env File

Still in Bash console:

```bash
cd ~/kape_de_manubag_system

# Create the .env file
nano .env
```

Paste this content (replace `yourusername` with your actual PythonAnywhere username):

```
SECRET_KEY=generate-a-50-char-random-string-here
DEBUG=False
ALLOWED_HOSTS=yourusername.pythonanywhere.com
CSRF_TRUSTED_ORIGINS=https://yourusername.pythonanywhere.com
```

To generate a secret key, run this in the Bash console:
```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

Save the file: `Ctrl+O`, Enter, `Ctrl+X`

---

## PHASE 6 — Database Setup

```bash
cd ~/kape_de_manubag_system
source venv/bin/activate

# Run all migrations
python manage.py migrate

# Create admin superuser
python manage.py createsuperuser

# Seed initial data (categories, products, system settings)
python manage.py shell < sample_data/seed_data.py

# Collect static files into /staticfiles/
python manage.py collectstatic --noinput
```

---

## PHASE 7 — Create the Web App

1. Go to the **Web** tab in PythonAnywhere dashboard
2. Click **Add a new web app**
3. Click **Next** → Choose **Manual configuration**
4. Choose **Python 3.11**
5. Click **Next**

---

## PHASE 8 — WSGI Configuration

In the Web tab, click the link to your **WSGI configuration file**
(it looks like `/var/www/yourusername_pythonanywhere_com_wsgi.py`)

**Delete all the existing content** and replace with:

```python
import sys
import os
from dotenv import load_dotenv

# ── Path setup ────────────────────────────────────────────────────────────────
# Add the project directory to sys.path
project_home = '/home/yourusername/kape_de_manubag_system'
if project_home not in sys.path:
    sys.path.insert(0, project_home)

# ── Load environment variables from .env ─────────────────────────────────────
load_dotenv(os.path.join(project_home, '.env'))

# ── Django setup ─────────────────────────────────────────────────────────────
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'kape_de_manubag.settings')

from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()
```

> **Replace `yourusername` with your actual PythonAnywhere username in the path.**

---

## PHASE 9 — Web App Configuration Values

In the **Web** tab, set these fields exactly:

| Setting | Value |
|---------|-------|
| **Source code** | `/home/yourusername/kape_de_manubag_system` |
| **Working directory** | `/home/yourusername/kape_de_manubag_system` |
| **Virtualenv path** | `/home/yourusername/kape_de_manubag_system/venv` |
| **WSGI file** | `/var/www/yourusername_pythonanywhere_com_wsgi.py` |

---

## PHASE 10 — Static & Media File Mappings

In the Web tab, scroll to **Static files** section and add these two entries:

| URL | Directory |
|-----|-----------|
| `/static/` | `/home/yourusername/kape_de_manubag_system/staticfiles` |
| `/media/` | `/home/yourusername/kape_de_manubag_system/media` |

> The `/static/` mapping points to `staticfiles/` (the collectstatic output), NOT the `static/` source folder.

---

## PHASE 11 — Reload and Test

1. Click the green **Reload** button at the top of the Web tab
2. Visit `https://yourusername.pythonanywhere.com`

---

## PHASE 12 — Testing Checklist

Run through each item after deployment:

### Customer-facing
- [ ] Home page loads (`/`) — menu items visible
- [ ] Category filter works
- [ ] Product search works
- [ ] Add to cart works
- [ ] Cart page loads (`/orders/cart/`)
- [ ] Checkout page loads (`/orders/checkout/`)
- [ ] Order type selection shows/hides packaging fee
- [ ] Place order → order success page
- [ ] "Track Your Order Live" button on success page works
- [ ] Order tracker page polls and updates every 5s
- [ ] Queue board at `/orders/queue-board/` loads

### Staff / Cashier
- [ ] Login at `/accounts/login/` — cashier account works
- [ ] Dashboard loads (`/dashboard/`)
- [ ] Order list (`/orders/manage/`) loads and shows orders
- [ ] Quick advance (▶ Preparing) button works
- [ ] POS terminal (`/orders/pos/`) loads and places orders
- [ ] Inventory page loads (`/inventory/`)
- [ ] Restock modal works

### Admin only
- [ ] Login as admin
- [ ] Reports page loads (`/reports/`)
- [ ] Excel export downloads
- [ ] Staff management page loads
- [ ] Product management — add/edit/delete works
- [ ] Category management — packaging flag works
- [ ] System Settings page (`/dashboard/settings/`) works
- [ ] Django admin (`/admin/`) loads with correct styling

### Security
- [ ] Visiting `/orders/manage/1/receipt/` while logged out → redirects to login
- [ ] Visiting same URL as customer role → "Access denied"
- [ ] Visiting same URL as cashier → receipt loads

---

## PHASE 13 — Troubleshooting

### 500 Internal Server Error
Check the error log: **Web tab → Log files → Error log**

Common causes:
```
# ModuleNotFoundError
→ Virtual environment not activated, or pip install missed a package
→ Fix: source venv/bin/activate && pip install -r requirements.txt

# No module named 'kape_de_manubag'
→ WSGI file has wrong project path
→ Fix: check project_home path in WSGI file

# SECRET_KEY or settings error
→ .env file not created or has wrong content
→ Fix: check cat ~/.env and verify content
```

### Static files not loading (CSS broken)
```bash
# Re-run collectstatic
source venv/bin/activate
python manage.py collectstatic --noinput

# Verify the output folder exists
ls ~/kape_de_manubag_system/staticfiles/

# Check the Web tab static files mapping:
# URL: /static/  →  Directory: /home/yourusername/kape_de_manubag_system/staticfiles
```

### Admin panel has no styling
```
Same as above — collectstatic must be run.
Django admin CSS lives in staticfiles/admin/css/
```

### Images not showing
```
Check Web tab → Static files:
URL: /media/  →  Directory: /home/yourusername/kape_de_manubag_system/media

Also verify media/ directory exists:
ls ~/kape_de_manubag_system/media/
```

### CSRF verification failed (403 on forms)
```
.env must contain:
CSRF_TRUSTED_ORIGINS=https://yourusername.pythonanywhere.com

After editing .env, click Reload in the Web tab.
```

### Database errors / table not found
```bash
source venv/bin/activate
python manage.py migrate
```

### Migrations out of order
```bash
source venv/bin/activate
python manage.py showmigrations   # see what's applied
python manage.py migrate --run-syncdb
```

### Permission errors on db.sqlite3 or media/
```bash
chmod 664 ~/kape_de_manubag_system/db.sqlite3
chmod 755 ~/kape_de_manubag_system/media/
```

### After any .env change
Always **click Reload** in the PythonAnywhere Web tab for changes to take effect.

---

## Quick Reference — All Commands in Order

### On your local machine (PowerShell)

```powershell
cd C:\Users\Shecile\kape_de_manubag_system
git init
git add .
git commit -m "Initial commit — Kape De Manubag system"
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/kape_de_manubag_system.git
git branch -M main
git push -u origin main
```

### On PythonAnywhere (Bash console)

```bash
cd ~
git clone https://github.com/YOUR_GITHUB_USERNAME/kape_de_manubag_system.git
cd kape_de_manubag_system
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
nano .env                                  # create .env with your values
python manage.py migrate
python manage.py createsuperuser
python manage.py shell < sample_data/seed_data.py
python manage.py collectstatic --noinput
```

Then in PythonAnywhere Web tab:
1. Set source code, working dir, virtualenv paths
2. Edit WSGI file
3. Add static and media file mappings
4. Click **Reload**
5. Visit `https://yourusername.pythonanywhere.com`

### Future updates

```powershell
# Local — after making changes
git add .
git commit -m "describe your change"
git push
```

```bash
# PythonAnywhere Bash console
cd ~/kape_de_manubag_system
git pull
source venv/bin/activate
python manage.py migrate        # if models changed
python manage.py collectstatic --noinput
# Then click Reload in Web tab
```
