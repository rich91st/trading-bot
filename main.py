import os
import threading
import logging
from flask import Flask, request, jsonify
import discord
from discord.ext import commands
import alpaca_trade_api as tradeapi
import asyncio
import nest_asyncio

# Apply nest_asyncio to allow asyncio to run in multiple threads
nest_asyncio.apply()

# -------------------- CONFIGURATION --------------------
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
ALPACA_API_KEY = os.getenv('ALPACA_API_KEY')
ALPACA_SECRET_KEY = os.getenv('ALPACA_SECRET_KEY')
ALPACA_BASE_URL = os.getenv('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET')
TRADE_PERCENT = 0.3  # 30% of equity per trade

# -------------------- SETUP LOGGING --------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------- ALPACA API --------------------
api = tradeapi.REST(ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version='v2')

# -------------------- DISCORD BOT --------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Global variable to control trading
trading_enabled = False

@bot.event
async def on_ready():
    logger.info(f'Discord bot logged in as {bot.user}')

@bot.command()
async def start(ctx):
    """Enable automated trading"""
    global trading_enabled
    trading_enabled = True
    await ctx.send("✅ Trading started. I will now act on TradingView signals.")

@bot.command()
async def stop(ctx):
    """Disable automated trading"""
    global trading_enabled
    trading_enabled = False
    await ctx.send("🛑 Trading stopped. Ignoring incoming signals.")

@bot.command()
async def status(ctx):
    """Show current account status and whether trading is enabled"""
    try:
        account = api.get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        status_msg = f"**Trading enabled:** {trading_enabled}\n"
        status_msg += f"**Equity:** ${equity:.2f}\n"
        status_msg += f"**Cash:** ${cash:.2f}\n"
        status_msg += f"**Buying power:** ${float(account.buying_power):.2f}\n"
        
        # Get open positions
        positions = api.list_positions()
        if positions:
            status_msg += "\n**Open positions:**\n"
            for pos in positions:
                status_msg += f"{pos.symbol}: {pos.qty} shares @ ${float(pos.avg_entry_price):.2f} (current: ${float(pos.current_price):.2f})\n"
        else:
            status_msg += "\nNo open positions."
        
        await ctx.send(status_msg)
    except Exception as e:
        await ctx.send(f"Error fetching account info: {e}")

def run_discord_bot():
    bot.run(DISCORD_TOKEN)

# -------------------- FLASK WEBHOOK SERVER --------------------
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    global trading_enabled
    # Verify secret if provided
    if WEBHOOK_SECRET:
        secret = request.args.get('secret')
        if secret != WEBHOOK_SECRET:
            return jsonify({'error': 'Invalid secret'}), 403

    if not trading_enabled:
        logger.info("Trading is disabled, ignoring alert.")
        return jsonify({'status': 'ignored', 'reason': 'trading disabled'}), 200

    data = request.get_json()
    if not data:
        logger.error("No JSON payload received")
        return jsonify({'error': 'No JSON'}), 400

    logger.info(f"Received webhook: {data}")

    # Extract information from TradingView alert
    action = data.get('action') or data.get('side') or data.get('order_action')
    symbol = data.get('ticker') or data.get('symbol')
    
    if not action or not symbol:
        logger.error("Missing action or symbol in webhook payload")
        return jsonify({'error': 'Missing action or symbol'}), 400

    try:
        if action.lower() == 'buy':
            # Get account equity and calculate position size
            account = api.get_account()
            equity = float(account.equity)
            cash = float(account.cash)
            # Use 30% of equity, but cannot exceed available cash
            trade_value = equity * TRADE_PERCENT
            trade_value = min(trade_value, cash)

            # Get current price
            last_trade = api.get_last_trade(symbol)
            price = last_trade.price
            qty = int(trade_value // price)
            
            if qty <= 0:
                logger.info(f"Calculated quantity zero (equity=${equity:.2f}, cash=${cash:.2f}, price=${price:.2f})")
                return jsonify({'status': 'skipped', 'reason': 'qty zero'}), 200

            # Submit market buy order
            order = api.submit_order(
                symbol=symbol,
                qty=qty,
                side='buy',
                type='market',
                time_in_force='day'
            )
            logger.info(f"Buy order placed: {order}")
            return jsonify({'status': 'order_placed', 'order_id': order.id, 'qty': qty, 'price': price}), 200

        elif action.lower() == 'sell':
            # Close entire position for the symbol
            try:
                position = api.get_position(symbol)
            except:
                logger.info(f"No position found for {symbol}")
                return jsonify({'status': 'skipped', 'reason': 'no position'}), 200

            qty = abs(float(position.qty))
            if qty <= 0:
                return jsonify({'status': 'skipped', 'reason': 'position zero'}), 200

            # Submit market sell order
            order = api.submit_order(
                symbol=symbol,
                qty=qty,
                side='sell',
                type='market',
                time_in_force='day'
            )
            logger.info(f"Sell order placed: {order}")
            return jsonify({'status': 'order_placed', 'order_id': order.id, 'qty': qty}), 200

        else:
            logger.error(f"Unknown action: {action}")
            return jsonify({'error': 'Unknown action'}), 400

    except Exception as e:
        logger.exception("Error processing webhook")
        return jsonify({'error': str(e)}), 500

def run_flask():
    app.run(host='0.0.0.0', port=5000)

# -------------------- START BOTH SERVICES --------------------
if __name__ == '__main__':
    # Run Flask in a separate thread
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # Run Discord bot in main thread
    run_discord_bot()