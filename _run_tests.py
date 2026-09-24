import os, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'kape_de_manubag.settings')
import django
django.setup()
from django.test.utils import get_runner
from django.conf import settings
TestRunner = get_runner(settings)
runner = TestRunner(verbosity=1, keepdb=False)
failures = runner.run_tests(['apps.orders.tests'])
print(f"\nFAILURES: {failures}")
sys.exit(failures)
