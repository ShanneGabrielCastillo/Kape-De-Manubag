#!/bin/bash
echo "☕ Setting up Kape De Manubag System..."

# Create virtual environment
python3 -m venv venv

# Activate
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy env file
if [ ! -f .env ]; then
  cp .env.example .env
  echo "✅ .env file created"
fi

# Run migrations
python manage.py migrate

# Seed demo menu data (categories, products, settings). No accounts are created.
python manage.py seed_data

echo ""
echo "✅ Setup complete! No user accounts were created."
echo "Create an administrator with unique credentials:"
echo "  python manage.py createsuperuser"
echo "Run: source venv/bin/activate && python manage.py runserver"
