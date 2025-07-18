import asyncio
import aiohttp
import json
import time
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO, emit
import logging
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import threading

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class ArbitrageOpportunity:
    buy_exchange: str
    sell_exchange: str
    symbol: str
    buy_price: float
    sell_price: float
    profit_percentage: float
    buy_volume: float
    sell_volume: float
    timestamp: datetime

class ExchangeConnector:
    def __init__(self, name: str, base_url: str):
        self.name = name
        self.base_url = base_url
        self.session = None
        self.last_update = 0
        self.prices = {}
        self.volumes = {}
        
    async def create_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
    
    async def close_session(self):
        if self.session:
            await self.session.close()
            self.session = None

class BybitConnector(ExchangeConnector):
    def __init__(self):
        super().__init__("Bybit", "https://api.bybit.com")
    
    async def get_tickers(self) -> Dict[str, Dict]:
        try:
            await self.create_session()
            url = f"{self.base_url}/v5/market/tickers"
            params = {"category": "spot"}
            
            async with self.session.get(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    tickers = {}
                    for item in data.get('result', {}).get('list', []):
                        symbol = item.get('symbol', '').replace('USDT', '/USDT')
                        if symbol and item.get('lastPrice'):
                            tickers[symbol] = {
                                'price': float(item.get('lastPrice', 0)),
                                'volume': float(item.get('volume24h', 0))
                            }
                    return tickers
        except Exception as e:
            logger.error(f"Bybit API error: {e}")
        return {}

class KuCoinConnector(ExchangeConnector):
    def __init__(self):
        super().__init__("KuCoin", "https://api.kucoin.com")
    
    async def get_tickers(self) -> Dict[str, Dict]:
        try:
            await self.create_session()
            url = f"{self.base_url}/api/v1/market/allTickers"
            
            async with self.session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    tickers = {}
                    for item in data.get('data', {}).get('ticker', []):
                        symbol = item.get('symbol', '').replace('-', '/')
                        if symbol and item.get('last'):
                            tickers[symbol] = {
                                'price': float(item.get('last', 0)),
                                'volume': float(item.get('vol', 0))
                            }
                    return tickers
        except Exception as e:
            logger.error(f"KuCoin API error: {e}")
        return {}

class GateConnector(ExchangeConnector):
    def __init__(self):
        super().__init__("Gate.io", "https://api.gateio.ws")
    
    async def get_tickers(self) -> Dict[str, Dict]:
        try:
            await self.create_session()
            url = f"{self.base_url}/api/v4/spot/tickers"
            
            async with self.session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    tickers = {}
                    for item in data:
                        symbol = item.get('currency_pair', '').replace('_', '/')
                        if symbol and item.get('last'):
                            tickers[symbol] = {
                                'price': float(item.get('last', 0)),
                                'volume': float(item.get('base_volume', 0))
                            }
                    return tickers
        except Exception as e:
            logger.error(f"Gate.io API error: {e}")
        return {}

class HTXConnector(ExchangeConnector):
    def __init__(self):
        super().__init__("HTX", "https://api.huobi.pro")
    
    async def get_tickers(self) -> Dict[str, Dict]:
        try:
            await self.create_session()
            url = f"{self.base_url}/market/tickers"
            
            async with self.session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    tickers = {}
                    for item in data.get('data', []):
                        symbol = item.get('symbol', '').upper()
                        if symbol.endswith('USDT'):
                            formatted_symbol = symbol.replace('USDT', '/USDT')
                            if item.get('close'):
                                tickers[formatted_symbol] = {
                                    'price': float(item.get('close', 0)),
                                    'volume': float(item.get('vol', 0))
                                }
                    return tickers
        except Exception as e:
            logger.error(f"HTX API error: {e}")
        return {}

class ArbitrageScanner:
    def __init__(self):
        self.exchanges = {
            'bybit': BybitConnector(),
            'kucoin': KuCoinConnector(),
            'gateio': GateConnector(),
            'htx': HTXConnector()
        }
        self.opportunities = []
        self.running = False
        self.min_profit_percentage = 0.5  # Minimum 0.5% profit
        self.top_n_tokens = 300
        
    async def fetch_all_prices(self):
        """Fetch prices from all exchanges concurrently"""
        logger.info("Starting to fetch prices from all exchanges...")
        tasks = []
        for exchange_name, connector in self.exchanges.items():
            logger.info(f"Creating task for {exchange_name}")
            task = asyncio.create_task(connector.get_tickers())
            tasks.append((exchange_name, task))
        
        results = {}
        for exchange_name, task in tasks:
            try:
                logger.info(f"Waiting for {exchange_name} data...")
                exchange_data = await task
                results[exchange_name] = exchange_data
                logger.info(f"{exchange_name}: Got {len(exchange_data)} symbols")
            except Exception as e:
                logger.error(f"Error fetching {exchange_name}: {e}")
                results[exchange_name] = {}
        
        total_symbols = sum(len(data) for data in results.values())
        logger.info(f"Total symbols fetched across all exchanges: {total_symbols}")
        return results
    
    def find_arbitrage_opportunities(self, all_prices: Dict[str, Dict]) -> List[ArbitrageOpportunity]:
        """Find arbitrage opportunities between exchanges"""
        opportunities = []
        
        # Get all unique symbols across exchanges
        all_symbols = set()
        for exchange_prices in all_prices.values():
            all_symbols.update(exchange_prices.keys())
        
        # Convert to list and sort by volume to get top tokens
        symbol_volumes = {}
        for symbol in all_symbols:
            total_volume = 0
            for exchange_prices in all_prices.values():
                if symbol in exchange_prices:
                    total_volume += exchange_prices[symbol]['volume']
            symbol_volumes[symbol] = total_volume
        
        # Get top N tokens by volume
        top_symbols = sorted(symbol_volumes.keys(), 
                           key=lambda x: symbol_volumes[x], 
                           reverse=True)[:self.top_n_tokens]
        
        for symbol in top_symbols:
            symbol_prices = {}
            symbol_volumes = {}
            
            # Collect prices and volumes for this symbol across exchanges
            for exchange_name, exchange_prices in all_prices.items():
                if symbol in exchange_prices:
                    symbol_prices[exchange_name] = exchange_prices[symbol]['price']
                    symbol_volumes[exchange_name] = exchange_prices[symbol]['volume']
            
            # Need at least 2 exchanges to have arbitrage
            if len(symbol_prices) < 2:
                continue
            
            # Find best buy and sell opportunities
            buy_exchange = min(symbol_prices.keys(), key=lambda x: symbol_prices[x])
            sell_exchange = max(symbol_prices.keys(), key=lambda x: symbol_prices[x])
            
            buy_price = symbol_prices[buy_exchange]
            sell_price = symbol_prices[sell_exchange]
            
            # Calculate profit percentage
            if buy_price > 0:
                profit_percentage = ((sell_price - buy_price) / buy_price) * 100
                
                # SAFETY FILTERS - Prevent false opportunities
                # Filter 1: Reject if profit is unrealistically high (likely different tokens)
                if profit_percentage > 100:  # More than 100% is suspicious
                    continue
                    
                # Filter 2: Reject if price difference is too extreme
                price_ratio = sell_price / buy_price if buy_price > 0 else float('inf')
                if price_ratio > 2.0:  # If one price is more than 2x the other, skip
                    continue
                    
                # Filter 3: Require minimum volume on both exchanges
                min_volume_required = 1000  # Minimum $1000 volume
                if (symbol_volumes.get(buy_exchange, 0) < min_volume_required or 
                    symbol_volumes.get(sell_exchange, 0) < min_volume_required):
                    continue
                
                # Filter 4: Reject if prices are suspiciously low (might be dead tokens)
                if buy_price < 0.000001 or sell_price < 0.000001:  # Less than $0.000001
                    continue
                
                # Only consider if profit is above threshold and passes all filters
                if profit_percentage >= self.min_profit_percentage:
                    opportunity = ArbitrageOpportunity(
                        buy_exchange=buy_exchange.title(),
                        sell_exchange=sell_exchange.title(),
                        symbol=symbol,
                        buy_price=buy_price,
                        sell_price=sell_price,
                        profit_percentage=profit_percentage,
                        buy_volume=symbol_volumes.get(buy_exchange, 0),
                        sell_volume=symbol_volumes.get(sell_exchange, 0),
                        timestamp=datetime.now()
                    )
                    opportunities.append(opportunity)
        
        # Sort by profit percentage (descending)
        opportunities.sort(key=lambda x: x.profit_percentage, reverse=True)
        return opportunities[:50]  # Return top 50 opportunities
    
    async def scan_once(self):
        """Scan for arbitrage opportunities once"""
        try:
            logger.info("Scanning for arbitrage opportunities...")
            start_time = time.time()
            
            # Fetch all prices
            all_prices = await self.fetch_all_prices()
            
            # Find opportunities
            opportunities = self.find_arbitrage_opportunities(all_prices)
            self.opportunities = opportunities
            
            scan_time = time.time() - start_time
            logger.info(f"Scan completed in {scan_time:.2f}s. Found {len(opportunities)} opportunities.")
            
        except Exception as e:
            logger.error(f"Error in scan: {e}")
    
    async def scan_continuously(self):
        """Continuously scan for arbitrage opportunities"""
        while self.running:
            try:
                await self.scan_once()
                # Wait before next scan
                await asyncio.sleep(10)  # Scan every 10 seconds
                
            except Exception as e:
                logger.error(f"Error in scan loop: {e}")
                await asyncio.sleep(5)
    
    async def start_scanning(self):
        """Start the scanning process"""
        self.running = True
        await self.scan_continuously()
    
    def stop_scanning(self):
        """Stop the scanning process"""
        self.running = False
    
    async def cleanup(self):
        """Clean up resources"""
        for connector in self.exchanges.values():
            await connector.close_session()

# Flask app setup
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Global scanner instance
scanner = ArbitrageScanner()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/opportunities')
def get_opportunities():
    """API endpoint to get current arbitrage opportunities"""
    try:
        logger.info("=== API ENDPOINT CALLED ===")
        
        # Create a new event loop for this request
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            # Run a fresh scan
            loop.run_until_complete(scanner.scan_once())
        finally:
            loop.close()
        
        logger.info(f"API: Found {len(scanner.opportunities)} opportunities")
        
        opportunities_data = []
        for opp in scanner.opportunities:
            opportunities_data.append({
                'buy_exchange': opp.buy_exchange,
                'sell_exchange': opp.sell_exchange,
                'symbol': opp.symbol,
                'buy_price': opp.buy_price,
                'sell_price': opp.sell_price,
                'profit_percentage': round(opp.profit_percentage, 2),
                'buy_volume': opp.buy_volume,
                'sell_volume': opp.sell_volume,
                'timestamp': opp.timestamp.isoformat()
            })
        
        logger.info(f"API: Returning {len(opportunities_data)} opportunities")
        return jsonify(opportunities_data)
        
    except Exception as e:
        logger.error(f"API ERROR: {e}")
        import traceback
        logger.error(f"API TRACEBACK: {traceback.format_exc()}")
        
        # Return a proper JSON error instead of HTML
        return jsonify({
            "error": str(e),
            "message": "Failed to fetch arbitrage opportunities"
        }), 500

# Add the missing /api/scan route that your frontend expects
@app.route('/api/scan')
def trigger_scan():
    """API endpoint to trigger a scan (alias for opportunities)"""
    return get_opportunities()

@socketio.on('connect')
def handle_connect():
    print('Client connected')
    emit('connected', {'data': 'Connected to arbitrage scanner'})

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

def run_scanner():
    """Run the scanner in a separate thread"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(scanner.start_scanning())

# Start scanner in background thread
scanner_thread = threading.Thread(target=run_scanner, daemon=True)
scanner_thread.start()

if __name__ == '__main__':
    try:
        port = int(os.environ.get('PORT', 5000))
        socketio.run(app, debug=True, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        print("\nShutting down...")
        scanner.stop_scanning()
        # Clean up
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(scanner.cleanup())
