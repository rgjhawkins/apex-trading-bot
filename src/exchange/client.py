from binance import Client
from src.config.settings import API_KEY, SECRET_KEY, USE_TESTNET


def create_client() -> Client:
    client = Client(API_KEY, SECRET_KEY, testnet=USE_TESTNET)
    return client


class BinanceClient:
    def __init__(self):
        self.client = create_client()
        self.testnet = USE_TESTNET

    def test_connection(self) -> bool:
        try:
            self.client.ping()
            return True
        except Exception as e:
            print(f"Connection failed: {e}")
            return False

    def get_server_time(self) -> dict:
        return self.client.get_server_time()

    def get_account(self) -> dict:
        return self.client.get_account()

    def get_balance(self, asset: str) -> dict:
        account = self.get_account()
        for balance in account["balances"]:
            if balance["asset"] == asset:
                return balance
        return {}

    def get_ticker(self, symbol: str) -> dict:
        return self.client.get_symbol_ticker(symbol=symbol)

    def get_order_book(self, symbol: str, limit: int = 10) -> dict:
        return self.client.get_order_book(symbol=symbol, limit=limit)
