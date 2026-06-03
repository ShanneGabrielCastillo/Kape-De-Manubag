@echo off
echo Setting up Kape De Manubag System...

python -m venv venv
call venv\Scripts\activate

pip install -r requirements.txt

if not exist .env copy .env.example .env

python manage.py migrate
python manage.py shell < sample_data\seed_data.py

echo.
echo Setup complete!
echo Run: venv\Scripts\activate && python manage.py runserver
