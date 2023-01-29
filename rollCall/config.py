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

commands = ['/src', '/in', '/out', '/maybe', '/set_in_for', '/set_out_for','/sif', '/sof', '/smf', '/start', '/help', '/set_admins', '/unset_admins', '/broadcast', '/timezone', '/version', '/rollcalls', '/start_roll_call', '/set_rollcall_time', '/set_rollcall_reminder', '/event_fee', '/individual_fee', '/when', '/location', '/set_limit', '/delete_user', '/shh', '/louder', '/set_maybe_for', '/whos_in', '/whos_out', '/whos_maybe', '/whos_waiting', '/set_title', '/end_roll_call', '/src', '/erc']