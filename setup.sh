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

# Seed data
python manage.py shell < sample_data/seed_data.py

echo ""
echo "✅ Setup complete!"
echo "Run: source venv/bin/activate && python manage.py runserver"
