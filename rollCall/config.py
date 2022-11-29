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

TELEGRAM_TOKEN=os.environ.get("API_KEY") #API KEY OF TELEGRAM BOT
CONN_DB=os.environ.get("CONN_DB") #URL OF MONGODB DATABASE, EX: mongodb://USERNAME:PASSWORD@URL/IP

ADMINS=[int(os.environ.get("ADMIN1")), int(os.environ.get("ADMIN2"))]