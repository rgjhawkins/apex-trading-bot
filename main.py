from src.exchange.client import BinanceClient
from src.config.settings import USE_TESTNET


def main():
    mode = "TESTNET" if USE_TESTNET else "LIVE"
    print(f"Connecting to Binance [{mode}]...")

    bot = BinanceClient()

    if not bot.test_connection():
        print("Failed to connect. Check your API keys in .env")
        return

    server_time = bot.get_server_time()
    print(f"Connected. Server time: {server_time['serverTime']}")

    ticker = bot.get_ticker("BTCUSDT")
    print(f"BTC/USDT price: ${float(ticker['price']):,.2f}")

    account = bot.get_account()
    print(f"Account status: {account['accountType']}")

    # Show non-zero balances
    balances = [b for b in account["balances"] if float(b["free"]) > 0 or float(b["locked"]) > 0]
    if balances:
        print("\nBalances:")
        for b in balances:
            print(f"  {b['asset']}: free={b['free']}, locked={b['locked']}")
    else:
        print("\nNo balances found (add testnet funds at https://testnet.binance.vision/)")


if __name__ == "__main__":
    main()
