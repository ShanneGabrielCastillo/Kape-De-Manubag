@echo off
echo Setting up Kape De Manubag System...

python -m venv venv
call venv\Scripts\activate

pip install -r requirements.txt

if not exist .env copy .env.example .env

python manage.py migrate
python manage.py seed_data

echo.
echo Setup complete! No user accounts were created.
echo Create an administrator with unique credentials:
echo   python manage.py createsuperuser
echo Run: venv\Scripts\activate && python manage.py runserver
