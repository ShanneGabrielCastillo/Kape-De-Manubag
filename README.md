# ☕ Kape De Manubag — Food Ordering & Management System

A complete restaurant/cafe management system built with Django.

## 🚀 Quick Setup

### 1. Requirements
- Python 3.10+
- pip

### 2. Install & Run

```bash
# Clone / extract the project
cd kape_de_manubag_system

# Create virtual environment
python -m venv venv

# Activate
# Windows:
venv\Scripts\activate
# Mac/Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy environment file
cp .env.example .env

# Run migrations
python manage.py migrate

# Seed sample data (categories, products, users)
python manage.py shell < sample_data/seed_data.py

# Start the server
python manage.py runserver
```

### 3. Access the System

| URL | Description |
|-----|-------------|
| http://127.0.0.1:8000/ | Customer Menu |
| http://127.0.0.1:8000/accounts/login/ | Staff Login |
| http://127.0.0.1:8000/dashboard/ | Admin/Cashier Dashboard |
| http://127.0.0.1:8000/orders/pos/ | POS Terminal |
| http://127.0.0.1:8000/admin/ | Django Admin |

## 🔑 Default Credentials

| Role | Username | Password |
|------|----------|----------|
| Admin | admin | admin123 |
| Cashier | cashier1 | cashier123 |

> ⚠️ Change passwords in production!

## 👥 User Roles

| Role | Access |
|------|--------|
| **Admin** | Full access: dashboard, menu, orders, inventory, reports, staff |
| **Cashier** | Orders, POS terminal, inventory view, receipts |
| **Customer** | Public menu, cart, checkout |

## 📋 Features

### Customer Side
- Browse full menu by category
- Search products
- Add to cart with size selection
- Dine-in or Take-Out checkout
- Order success page with receipt

### Admin Side
- Sales dashboard with charts
- Product & category management
- Order management with status updates
- Inventory tracking & restocking
- Sales reports with Excel export
- Staff account management

### Cashier Side
- POS terminal for quick order creation
- Payment processing with change calculator
- Receipt printing
- Order status updates

## 🏗️ Project Structure

```
kape_de_manubag_system/
├── manage.py
├── requirements.txt
├── kape_de_manubag/       # Django settings
├── apps/
│   ├── accounts/          # User management
│   ├── menu/              # Products & categories
│   ├── orders/            # Cart, orders, POS
│   ├── inventory/         # Stock management
│   ├── reports/           # Sales analytics
│   └── dashboard/         # Admin dashboard
├── templates/             # HTML templates
├── static/                # CSS, JS, images
├── media/                 # Uploaded images
└── sample_data/           # Seed data script
```

## ⚙️ Configuration (environment variables)

All settings are read from a `.env` file in the project root — copy `.env.example` to `.env` and edit.

| Variable | Development | Production |
|----------|-------------|------------|
| `DEBUG` | `True` recommended (defaults to `False`) | **Must be `False`** |
| `SECRET_KEY` | Optional (safe dev-only fallback + startup warning) | **Required** — startup fails if missing |
| `ALLOWED_HOSTS` | Optional (defaults to `localhost, 127.0.0.1, [::1]`) | **Required** — startup fails if missing or set to `*` |
| `CSRF_TRUSTED_ORIGINS` | Optional | Set to `https://yourdomain.com` (needed for CSRF on HTTPS) |

When `DEBUG=False` (production), the app refuses to start with a clear error message if `SECRET_KEY` or `ALLOWED_HOSTS` is missing, so an insecure configuration can never silently go live. Generate a secret key with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

## 🌐 Deployment

### PythonAnywhere
1. Upload files via Files tab
2. Create a web app (manual config)
3. Set WSGI file to point to `kape_de_manubag/wsgi.py`
4. Set `DEBUG=False` in `.env`
5. Run `python manage.py collectstatic`

### Render / Railway
1. Set `SECRET_KEY`, `DEBUG=False`, `ALLOWED_HOSTS` in environment
2. Build command: `pip install -r requirements.txt`
3. Start command: `python manage.py migrate && gunicorn kape_de_manubag.wsgi`

## 📱 Menu from Menu Images

Products are seeded from the actual menu photos:
- **Coffee**: Salted Caramel, Mocha, White Mocha, Spanish Latte (₱69–89)
- **Milk Tea**: Dark Chocolate, Cookies & Cream, Wintermelon, Okinawa (₱59–99)
- **Non-Coffee**: Matcha Latte, Strawberry Milk, Fruit Sodas (₱29–99)
- **Combo Meals**: 7 combos (₱60–99) with free ice tea for dine-in
- **Pastil Meals**: Chicken, Pork, Tuna Pastil (₱35–105)
- **Burgers**: Pork & Chicken burgers (₱30–60)
- **Appetizers**: Siomai, Squid Roll, Tempura, Fries (₱25–30)
