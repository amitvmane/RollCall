import os
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    filename="test_rollCall.log",
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'

)

TELEGRAM_TOKEN=os.environ.get("API_KEY")

ADMINS=[int(os.environ.get("ADMIN1")), int(os.environ.get("ADMIN2"))]