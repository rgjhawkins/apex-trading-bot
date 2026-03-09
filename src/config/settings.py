import os
from dotenv import load_dotenv

load_dotenv()

USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"
API_KEY = os.getenv("BINANCE_TESTNET_API_KEY", "")
SECRET_KEY = os.getenv("BINANCE_TESTNET_SECRET_KEY", "")
