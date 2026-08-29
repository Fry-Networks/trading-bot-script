#!/usr/bin/env python3
"""
Algorand ASA Trading Bot using Vestige API
==========================================
Automated trading bot for Algorand Standard Assets (ASA) using the Vestige aggregator.
Supports multiple trading strategies, LLM analysis, and comprehensive logging.

Requirements:
    pip install py-algorand-sdk requests numpy pandas colorama ollama --break-system-packages
"""

import os
import sys
import time
import json
import signal
import hashlib
import base64
import threading
import subprocess
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple
from enum import Enum
from decimal import Decimal, ROUND_DOWN
import requests
import numpy as np

try:
    from algosdk import mnemonic, account, transaction
    from algosdk.v2client import algod, indexer
    from colorama import Fore, Back, Style, init as colorama_init
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Please install required packages:")
    print("pip install py-algorand-sdk requests numpy pandas colorama ollama --break-system-packages")
    sys.exit(1)

# Initialize colorama for cross-platform color support
colorama_init(autoreset=True)

# ============================================================================
# VERSION & AUTO-UPDATE
# ============================================================================

VERSION = "1.0.0"
VERSION_DATE = "2025-12-04"

# GitHub repository for updates
GITHUB_REPO = "Fry-Foundation/trading-bot"
GITHUB_RAW_URL = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}"

# Update check interval (24 hours in seconds)
UPDATE_CHECK_INTERVAL = 86400

# ============================================================================
# CONSTANTS
# ============================================================================

VESTIGE_API_BASE = "https://api.vestigelabs.org"
ALGO_ASSET_ID = 0  # Native ALGO
DEFAULT_NETWORK_ID = 0  # Mainnet

# Algorand node endpoints (Nodely free tier)
# Using the recommended nodely.dev endpoints
# Note: Free tier has 50ms artificial latency per request
# Reference: https://nodely.io/docs/free/endpoints/
ALGOD_ADDRESS = "https://mainnet-api.4160.nodely.dev"
INDEXER_ADDRESS = "https://mainnet-idx.4160.nodely.dev"

# Alternative legacy endpoints (same infrastructure, still supported)
ALGOD_ADDRESS_LEGACY = "https://mainnet-api.algonode.cloud"
INDEXER_ADDRESS_LEGACY = "https://mainnet-idx.algonode.cloud"

# Rate limiting settings for free tier
# Being conservative to avoid any potential throttling
RATE_LIMIT_REQUESTS_PER_SECOND = 2  # Max requests per second
RATE_LIMIT_MIN_INTERVAL = 0.5  # Minimum seconds between requests

# ============================================================================
# AUTO-UPDATE MODULE
# ============================================================================

class AutoUpdater:
    """
    Handles automatic update checking and applying updates from GitHub.
    
    Features:
    - Check for updates on startup
    - Check for updates once per day
    - Manual update check option
    - Download and apply updates with backup
    """
    
    # File to store last update check time
    UPDATE_CHECK_FILE = ".last_update_check"
    
    def __init__(self):
        self.current_version = VERSION
        self.last_check_time = self._load_last_check_time()
        self.update_available = False
        self.remote_version = None
        self.update_info = {}
    
    def _load_last_check_time(self) -> Optional[datetime]:
        """Load the last update check timestamp from file."""
        try:
            if os.path.exists(self.UPDATE_CHECK_FILE):
                with open(self.UPDATE_CHECK_FILE, 'r') as f:
                    timestamp = float(f.read().strip())
                    return datetime.fromtimestamp(timestamp)
        except Exception:
            pass
        return None
    
    def _save_last_check_time(self):
        """Save the current time as last update check."""
        try:
            with open(self.UPDATE_CHECK_FILE, 'w') as f:
                f.write(str(datetime.now().timestamp()))
            self.last_check_time = datetime.now()
        except Exception:
            pass
    
    def should_check_for_updates(self) -> bool:
        """Check if enough time has passed since last update check."""
        if self.last_check_time is None:
            return True
        
        elapsed = (datetime.now() - self.last_check_time).total_seconds()
        return elapsed >= UPDATE_CHECK_INTERVAL
    
    def check_for_updates(self, silent: bool = False) -> Dict:
        """
        Check GitHub for available updates.
        
        Args:
            silent: If True, don't print status messages
        
        Returns:
            Dict with update info: {available, current_version, remote_version, changelog}
        """
        result = {
            "available": False,
            "current_version": self.current_version,
            "remote_version": None,
            "changelog": "",
            "error": None
        }
        
        if not silent:
            print(f"{Fore.CYAN}🔄 Checking for updates...{Style.RESET_ALL}")
        
        try:
            # Method 1: Check VERSION file in repo
            version_url = f"{GITHUB_RAW_URL}/VERSION"
            response = requests.get(version_url, timeout=10)
            
            if response.status_code == 200:
                remote_version = response.text.strip().split('\n')[0]
                result["remote_version"] = remote_version
                self.remote_version = remote_version
                
                # Compare versions
                if self._is_newer_version(remote_version, self.current_version):
                    result["available"] = True
                    self.update_available = True
                    
                    # Try to get changelog
                    try:
                        changelog_url = f"{GITHUB_RAW_URL}/CHANGELOG.md"
                        changelog_resp = requests.get(changelog_url, timeout=5)
                        if changelog_resp.status_code == 200:
                            # Get first section of changelog
                            changelog = changelog_resp.text[:1000]
                            result["changelog"] = changelog
                    except Exception:
                        pass
            else:
                # Method 2: Fallback to checking the script's VERSION directly
                script_url = f"{GITHUB_RAW_URL}/algorand_trading_bot.py"
                response = requests.get(script_url, timeout=15)
                
                if response.status_code == 200:
                    # Extract VERSION from remote script
                    for line in response.text.split('\n')[:100]:
                        if line.startswith('VERSION = '):
                            remote_version = line.split('"')[1]
                            result["remote_version"] = remote_version
                            self.remote_version = remote_version
                            
                            if self._is_newer_version(remote_version, self.current_version):
                                result["available"] = True
                                self.update_available = True
                            break
            
            # Save check time
            self._save_last_check_time()
            
            if not silent:
                if result["available"]:
                    print(f"{Fore.GREEN}✓ Update available: v{result['remote_version']} (current: v{self.current_version}){Style.RESET_ALL}")
                else:
                    print(f"{Fore.GREEN}✓ You're running the latest version (v{self.current_version}){Style.RESET_ALL}")
        
        except requests.exceptions.RequestException as e:
            result["error"] = f"Network error: {e}"
            if not silent:
                print(f"{Fore.YELLOW}⚠ Could not check for updates: {e}{Style.RESET_ALL}")
        except Exception as e:
            result["error"] = str(e)
            if not silent:
                print(f"{Fore.YELLOW}⚠ Update check failed: {e}{Style.RESET_ALL}")
        
        self.update_info = result
        return result
    
    def _is_newer_version(self, remote: str, current: str) -> bool:
        """Compare version strings (semantic versioning)."""
        try:
            # Parse versions like "1.0.0", "1.2.3", etc.
            remote_parts = [int(x) for x in remote.replace('v', '').split('.')]
            current_parts = [int(x) for x in current.replace('v', '').split('.')]
            
            # Pad shorter version with zeros
            while len(remote_parts) < 3:
                remote_parts.append(0)
            while len(current_parts) < 3:
                current_parts.append(0)
            
            return remote_parts > current_parts
        except Exception:
            # If parsing fails, do string comparison
            return remote > current
    
    def download_and_apply_update(self, backup: bool = True) -> Tuple[bool, str]:
        """
        Download the latest version and apply the update.
        
        Args:
            backup: If True, create a backup of the current script
        
        Returns:
            Tuple of (success, message)
        """
        print(f"{Fore.CYAN}📥 Downloading update...{Style.RESET_ALL}")
        
        try:
            # Get current script path
            script_path = os.path.abspath(__file__)
            script_dir = os.path.dirname(script_path)
            script_name = os.path.basename(script_path)
            
            # Download new version
            script_url = f"{GITHUB_RAW_URL}/algorand_trading_bot.py"
            response = requests.get(script_url, timeout=30)
            
            if response.status_code != 200:
                return False, f"Failed to download update (HTTP {response.status_code})"
            
            new_content = response.text
            
            # Verify it's valid Python (basic check)
            if "def " not in new_content or "import " not in new_content:
                return False, "Downloaded file doesn't appear to be valid Python"
            
            # Create backup if requested
            if backup:
                backup_path = os.path.join(script_dir, f"{script_name}.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
                try:
                    with open(script_path, 'r') as f:
                        old_content = f.read()
                    with open(backup_path, 'w') as f:
                        f.write(old_content)
                    print(f"{Fore.GREEN}✓ Backup created: {backup_path}{Style.RESET_ALL}")
                except Exception as e:
                    print(f"{Fore.YELLOW}⚠ Could not create backup: {e}{Style.RESET_ALL}")
            
            # Write new version
            with open(script_path, 'w') as f:
                f.write(new_content)
            
            print(f"{Fore.GREEN}✓ Update applied successfully!{Style.RESET_ALL}")
            print(f"{Fore.YELLOW}⚠ Please restart the bot to use the new version.{Style.RESET_ALL}")
            
            return True, "Update applied successfully"
        
        except PermissionError:
            return False, "Permission denied - try running with elevated privileges"
        except Exception as e:
            return False, f"Update failed: {e}"
    
    def prompt_for_update(self) -> bool:
        """
        Prompt user to apply available update.
        
        Returns:
            True if update was applied, False otherwise
        """
        if not self.update_available:
            print(f"{Fore.GREEN}✓ No updates available{Style.RESET_ALL}")
            return False
        
        print(f"\n{Fore.CYAN}{'='*60}")
        print(f"  UPDATE AVAILABLE")
        print(f"{'='*60}{Style.RESET_ALL}\n")
        print(f"  Current version: {Fore.YELLOW}v{self.current_version}{Style.RESET_ALL}")
        print(f"  New version:     {Fore.GREEN}v{self.remote_version}{Style.RESET_ALL}")
        
        if self.update_info.get("changelog"):
            print(f"\n  {Fore.CYAN}Recent changes:{Style.RESET_ALL}")
            # Show first few lines of changelog
            for line in self.update_info["changelog"].split('\n')[:10]:
                if line.strip():
                    print(f"    {line}")
        
        print(f"\n  Repository: {Fore.BLUE}https://github.com/{GITHUB_REPO}{Style.RESET_ALL}")
        print()
        
        try:
            choice = input(f"  Apply update now? (y/n) [{Fore.GREEN}y{Style.RESET_ALL}]: ").strip().lower()
            if choice in ['', 'y', 'yes']:
                success, message = self.download_and_apply_update()
                if success:
                    input(f"\n  Press Enter to exit and restart the bot...")
                    sys.exit(0)
                else:
                    print(f"{Fore.RED}✗ {message}{Style.RESET_ALL}")
                    return False
            else:
                print(f"{Fore.YELLOW}  Update skipped{Style.RESET_ALL}")
                return False
        except KeyboardInterrupt:
            print(f"\n{Fore.YELLOW}  Update cancelled{Style.RESET_ALL}")
            return False
    
    def run_startup_check(self):
        """Run update check on startup (checks daily)."""
        if self.should_check_for_updates():
            result = self.check_for_updates(silent=False)
            if result.get("available"):
                self.prompt_for_update()
        else:
            # Silent check to set state
            if self.last_check_time:
                hours_ago = (datetime.now() - self.last_check_time).total_seconds() / 3600
                print(f"{Fore.CYAN}ℹ Last update check: {hours_ago:.1f} hours ago (next check in {24 - hours_ago:.1f}h){Style.RESET_ALL}")


def check_for_updates_menu():
    """Menu function to manually check for updates."""
    updater = AutoUpdater()
    result = updater.check_for_updates(silent=False)
    
    if result.get("available"):
        updater.prompt_for_update()
    else:
        print(f"\n{Fore.GREEN}✓ You're running the latest version (v{VERSION}){Style.RESET_ALL}")
        print(f"  Repository: {Fore.BLUE}https://github.com/{GITHUB_REPO}{Style.RESET_ALL}")
    
    input(f"\n  Press Enter to continue...")


# ============================================================================
# DATA CLASSES
# ============================================================================

class TradingStrategy(Enum):
    # Non-AI strategies (pure mathematical analysis)
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    BREAKOUT = "breakout"
    SCALPING = "scalping"
    GRID = "grid"
    
    # AI-assisted hybrid strategies (math + LLM confirmation)
    MOMENTUM_AI = "momentum_ai"
    MEAN_REVERSION_AI = "mean_reversion_ai"
    BREAKOUT_AI = "breakout_ai"
    SCALPING_AI = "scalping_ai"
    
    # Pure AI strategy (100% LLM-driven)
    LLM_ASSISTED = "llm_assisted"
    
    # Rug.ninja strategies (Algorand pump.fun equivalent)
    RUG_NINJA_SNIPER = "rug_ninja_sniper"  # Buy newly minted tokens on bonding curve
    RUG_NINJA_GRADUATED = "rug_ninja_graduated"  # Trade tokens that have graduated to DEX
    
    # AlphaArcade strategies (Algorand prediction market)
    ALPHA_ARCADE_VALUE = "alpha_arcade_value"  # Buy undervalued predictions (contrarian)
    ALPHA_ARCADE_MOMENTUM = "alpha_arcade_momentum"  # Follow prediction trends


@dataclass
class TradeRecord:
    """Record of a single trade."""
    timestamp: datetime
    action: str  # BUY, SELL
    asset_id: int
    asset_name: str
    amount_in: float
    amount_out: float
    price: float
    value_algo: float
    txn_id: str
    pnl: float = 0.0
    pnl_percent: float = 0.0


@dataclass
class Position:
    """Current position in an asset."""
    asset_id: int
    asset_name: str
    amount: float
    avg_buy_price: float
    total_invested: float
    current_price: float = 0.0
    current_value: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_percent: float = 0.0
    
    # Profit enhancement tracking
    peak_price: float = 0.0  # Highest price since entry (for trailing stop)
    entry_time: datetime = None  # When position was opened
    trailing_stop_active: bool = False  # Whether trailing stop is activated
    trailing_stop_price: float = 0.0  # Current trailing stop price
    partial_profits_taken: int = 0  # Number of partial profit levels taken (0, 1, 2, 3)
    original_amount: float = 0.0  # Original position size before partial sells
    trade_source: str = ""  # Where opportunity came from (vestige, rug_ninja, alpha_arcade)
    is_imported: bool = False  # Whether this position was imported from existing wallet holdings


@dataclass
class TradingConfig:
    """Trading configuration settings."""
    strategy: TradingStrategy = TradingStrategy.MOMENTUM
    
    # Risk management
    max_position_size_algo: float = 100.0  # Max ALGO per position
    max_total_positions: int = 10  # Increased from 5 for more opportunities
    stop_loss_percent: float = 10.0  # Stop loss percentage
    take_profit_percent: float = 20.0  # Take profit percentage
    max_drawdown_percent: float = 20.0  # Max drawdown before stopping
    
    # === PROFIT ENHANCEMENT SETTINGS ===
    # Trailing Stop Loss - locks in profits as price rises
    trailing_stop_enabled: bool = True  # Enable trailing stops
    trailing_stop_activation_percent: float = 5.0  # Activate trailing stop after this profit %
    trailing_stop_distance_percent: float = 3.0  # Trail this far behind peak price
    
    # Partial Profit Taking - scale out of winning positions
    partial_profit_enabled: bool = True  # Enable partial profit taking
    # partial_profit_levels set in __post_init__: [(10, 25), (20, 50), (50, 25)] means sell 25% at 10%, 50% at 20%, 25% at 50%
    partial_profit_level_1_pct: float = 10.0  # First profit target %
    partial_profit_level_1_sell: float = 25.0  # Sell this % at first target
    partial_profit_level_2_pct: float = 20.0  # Second profit target %
    partial_profit_level_2_sell: float = 50.0  # Sell this % at second target
    partial_profit_level_3_pct: float = 50.0  # Third profit target %
    partial_profit_level_3_sell: float = 25.0  # Sell remaining at third target
    
    # Entry Filters - avoid bad entries
    anti_fomo_enabled: bool = True  # Don't buy after big pumps
    anti_fomo_max_1h_pump: float = 15.0  # Skip if pumped more than this in 1h
    anti_fomo_max_24h_pump: float = 50.0  # Skip if pumped more than this in 24h
    require_pullback: bool = False  # Only buy on pullbacks from highs
    pullback_percent: float = 3.0  # Required pullback from recent high
    
    # Volume Confirmation
    volume_confirmation_enabled: bool = True  # Require volume confirmation
    min_volume_increase: float = 1.5  # Volume must be 1.5x average
    
    # Position Timing
    min_hold_minutes: int = 5  # Minimum hold time before selling (avoid panic sells)
    max_hold_hours: int = 0  # Maximum hold time (0 = disabled)
    
    # Profit Protection - tighten stops when in profit
    profit_protection_enabled: bool = True  # Tighten stops when profitable
    profit_protection_threshold: float = 5.0  # Activate after this % profit
    profit_protection_stop: float = 2.0  # New stop loss when in profit (% from entry)
    
    # Daily Limits
    max_daily_loss_algo: float = 0.0  # Stop trading after this loss (0 = disabled)
    max_daily_trades: int = 0  # Max trades per day (0 = unlimited)
    cooldown_after_loss_minutes: int = 0  # Wait this long after a losing trade
    
    # Win Rate Tracking
    min_win_rate: float = 0.0  # Stop if win rate falls below this (0 = disabled)
    min_trades_for_win_rate: int = 10  # Min trades before checking win rate
    
    # Smart Entry Timing
    buy_the_dip_enabled: bool = False  # Wait for dips before buying
    dip_percent: float = 2.0  # Size of dip to wait for
    dip_timeout_minutes: int = 30  # Give up waiting after this long
    
    # Technical Analysis (from DEX trading bot research)
    use_technical_analysis: bool = True  # Enable RSI, MACD, Bollinger Bands scoring
    ta_rsi_oversold: float = 30.0  # RSI below this = oversold (buy signal)
    ta_rsi_overbought: float = 70.0  # RSI above this = overbought (sell signal)
    ta_require_confirmation: bool = False  # Require multiple TA signals to agree
    
    # Dynamic Position Sizing
    use_dynamic_sizing: bool = True  # Enable Kelly/volatility-based position sizing
    max_portfolio_exposure: float = 0.8  # Max % of balance in positions
    max_single_position_pct: float = 0.2  # Max % of balance per trade
    
    # === END PROFIT ENHANCEMENT ===
    
    # Trading parameters
    min_volume_24h: float = 1000.0  # Minimum 24h volume in ALGO
    min_liquidity: float = 5000.0  # Minimum TVL
    min_confidence: float = 0.5  # Minimum price confidence
    slippage_tolerance: float = 2.0  # Slippage tolerance percentage
    
    # Asset scanning
    scan_all_liquid_asas: bool = True  # Scan ALL ASAs with liquidity on Vestige
    max_assets_to_scan: int = 100  # Max assets to analyze per cycle
    
    # Strategy-specific parameters
    momentum_lookback_hours: int = 24
    momentum_threshold: float = 5.0  # % price change threshold
    mean_reversion_std_multiplier: float = 2.0
    breakout_volume_multiplier: float = 2.0
    grid_levels: int = 5
    grid_spacing_percent: float = 2.0
    scalp_profit_target: float = 1.0  # 1% quick profit
    
    # Timing
    check_interval_seconds: int = 60
    
    # LLM settings
    use_llm: bool = False
    llm_model: str = "llama3.2"  # Default Ollama model (legacy, use multi_llm for granular control)
    
    # Multi-LLM Configuration (different models for different tasks)
    multi_llm_enabled: bool = False
    llm_market_analysis: str = ""  # For analyzing market conditions (needs broad context)
    llm_trade_decisions: str = ""  # For confirming individual trades (needs speed)
    llm_strategy_reasoning: str = ""  # For strategy suggestions, re-evaluation (needs reasoning)
    llm_risk_assessment: str = ""  # For rug pull detection, risk analysis (needs caution)
    
    # Dynamic AI re-evaluation
    ai_dynamic_reeval: bool = False  # Enable AI to periodically re-evaluate strategy
    ai_reeval_interval_minutes: int = 30  # How often to re-evaluate (in minutes)
    ai_reeval_auto_apply: bool = False  # Automatically apply AI suggestions (vs just suggest)
    ai_include_rug_ninja: bool = False  # Include rug.ninja in AI strategy/preset suggestions
    ai_include_alpha_arcade: bool = False  # Include AlphaArcade in AI strategy/preset suggestions
    
    # Rug.ninja settings (Algorand's pump.fun equivalent)
    rug_ninja_enabled: bool = False  # Enable rug.ninja token trading
    rug_ninja_mode: str = "graduated"  # "sniper" (buy new mints), "graduated" (trade bonded), "both"
    rug_ninja_max_buy_algo: float = 5.0  # Max ALGO per rug.ninja trade
    rug_ninja_min_bond_progress: float = 0.0  # Min bonding progress (0.0 = just created)
    rug_ninja_max_bond_progress: float = 1.0  # Max bonding progress (1.0 = fully bonded)
    rug_ninja_auto_sell_on_bond: bool = True  # Auto-sell when token bonds (graduates to DEX)
    rug_ninja_min_holders: int = 5  # Minimum holders before buying
    rug_ninja_max_age_minutes: int = 60  # Max age for sniper mode (only buy recent mints)
    rug_ninja_realtime_sniper: bool = False  # Use real-time block streaming sniper (garbage-cat style)
    
    # AlphaArcade settings (Algorand prediction market)
    alpha_arcade_enabled: bool = False  # Enable AlphaArcade prediction trading
    alpha_arcade_api_key: str = ""  # Partner API key (get from AlphaArcade team)
    alpha_arcade_mode: str = "value"  # "value" (contrarian), "momentum" (trend following)
    alpha_arcade_max_bet_algo: float = 10.0  # Max ALGO per prediction bet
    alpha_arcade_min_volume: float = 100.0  # Minimum market volume to trade
    alpha_arcade_min_liquidity: float = 500.0  # Minimum market liquidity
    alpha_arcade_max_price: float = 0.90  # Don't buy YES/NO above this price (avoid overpaying)
    alpha_arcade_min_price: float = 0.10  # Don't buy YES/NO below this price (too risky)
    alpha_arcade_value_threshold: float = 0.15  # Buy if price differs from estimated prob by this much
    alpha_arcade_momentum_threshold: float = 0.05  # Min price change to trigger momentum buy
    alpha_arcade_categories: List[str] = None  # Filter by categories (e.g., ["sports", "crypto", "politics"])
    alpha_arcade_auto_sell_before_resolution: bool = True  # Sell positions before market resolves
    alpha_arcade_hours_before_resolution: int = 24  # Sell this many hours before resolution
    alpha_arcade_lp_mode: bool = False  # Provide liquidity on both sides for LP rewards
    
    # Auto-stop conditions
    stop_on_loss: bool = True
    max_loss_algo: float = 50.0  # Stop if total loss exceeds this
    
    # Existing wallet positions
    import_existing_positions: bool = True  # Import existing ASA holdings at startup
    min_position_value_algo: float = 1.0  # Minimum value to import (skip dust)
    manage_imported_positions: bool = False  # Apply stop-loss/take-profit to imported positions
    
    # Preset name (for saving/loading)
    preset_name: str = "custom"


@dataclass
class MultiLLMConfig:
    """Configuration for using multiple LLMs for different tasks."""
    market_analysis: Optional[str] = None  # Broad market analysis (benefits from larger context)
    trade_decisions: Optional[str] = None  # Individual trade confirmation (benefits from speed)
    strategy_reasoning: Optional[str] = None  # Strategy/preset suggestions (benefits from reasoning)
    risk_assessment: Optional[str] = None  # Rug detection, risk analysis (benefits from caution)
    
    def get_model(self, task: str, fallback: str = "") -> str:
        """Get the appropriate model for a task, with fallback."""
        model_map = {
            "market": self.market_analysis,
            "trade": self.trade_decisions,
            "strategy": self.strategy_reasoning,
            "risk": self.risk_assessment,
        }
        model = model_map.get(task)
        if model:
            return model
        # Fallback chain: try other models, then fallback
        for m in [self.market_analysis, self.trade_decisions, self.strategy_reasoning, self.risk_assessment]:
            if m:
                return m
        return fallback


@dataclass
class BotState:
    """Current state of the trading bot."""
    running: bool = True
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl_algo: float = 0.0
    total_fees_algo: float = 0.0
    starting_balance_algo: float = 0.0
    current_balance_algo: float = 0.0
    max_balance_algo: float = 0.0
    positions: Dict[int, Position] = field(default_factory=dict)
    trade_history: List[TradeRecord] = field(default_factory=list)
    watched_assets: Dict[int, Dict] = field(default_factory=dict)
    last_ai_reeval_time: Optional[datetime] = None  # Track last AI re-evaluation
    
    # Daily tracking for profit enhancement
    daily_trades: int = 0
    daily_pnl_algo: float = 0.0
    daily_wins: int = 0
    daily_losses: int = 0
    last_trade_time: Optional[datetime] = None
    last_loss_time: Optional[datetime] = None  # For cooldown after loss
    current_day: str = ""  # Track current day for daily reset
    
    # Dip watching
    dip_watch_list: Dict[int, Dict] = field(default_factory=dict)  # Assets waiting for dip
    
    def reset_daily_stats(self):
        """Reset daily statistics."""
        today = datetime.now().strftime("%Y-%m-%d")
        if self.current_day != today:
            self.current_day = today
            self.daily_trades = 0
            self.daily_pnl_algo = 0.0
            self.daily_wins = 0
            self.daily_losses = 0
            log_info(f"📊 Daily stats reset for {today}")
    
    @property
    def win_rate(self) -> float:
        """Calculate current win rate."""
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades
    
    @property
    def daily_win_rate(self) -> float:
        """Calculate daily win rate."""
        if self.daily_trades == 0:
            return 0.0
        return self.daily_wins / self.daily_trades


# ============================================================================
# TRADING PRESETS
# ============================================================================

TRADING_PRESETS = {
    "conservative": {
        "name": "Conservative (Bear Market)",
        "description": "Low risk, tight stops, fewer positions. Best for uncertain or declining markets.",
        "market_conditions": ["bear", "crash", "uncertain", "high fear"],
        "settings": {
            "strategy": TradingStrategy.MEAN_REVERSION,
            "max_position_size_algo": 50.0,
            "max_total_positions": 5,
            "stop_loss_percent": 5.0,
            "take_profit_percent": 10.0,
            "max_drawdown_percent": 10.0,
            "min_volume_24h": 2000.0,
            "min_liquidity": 10000.0,
            "momentum_threshold": 3.0,
            "check_interval_seconds": 120,
            "slippage_tolerance": 1.0,
        }
    },
    "moderate": {
        "name": "Moderate (Sideways Market)",
        "description": "Balanced risk/reward for choppy, range-bound markets.",
        "market_conditions": ["sideways", "consolidation", "neutral", "ranging"],
        "settings": {
            "strategy": TradingStrategy.MEAN_REVERSION,
            "max_position_size_algo": 100.0,
            "max_total_positions": 8,
            "stop_loss_percent": 8.0,
            "take_profit_percent": 15.0,
            "max_drawdown_percent": 15.0,
            "min_volume_24h": 1000.0,
            "min_liquidity": 5000.0,
            "momentum_threshold": 5.0,
            "check_interval_seconds": 90,
            "slippage_tolerance": 2.0,
        }
    },
    "aggressive": {
        "name": "Aggressive (Bull Market)",
        "description": "Higher risk, more positions, ride trends. Best for strong uptrends.",
        "market_conditions": ["bull", "rally", "euphoria", "strong uptrend"],
        "settings": {
            "strategy": TradingStrategy.MOMENTUM,
            "max_position_size_algo": 150.0,
            "max_total_positions": 15,
            "stop_loss_percent": 12.0,
            "take_profit_percent": 30.0,
            "max_drawdown_percent": 25.0,
            "min_volume_24h": 500.0,
            "min_liquidity": 3000.0,
            "momentum_threshold": 7.0,
            "check_interval_seconds": 45,
            "slippage_tolerance": 3.0,
        }
    },
    "scalper": {
        "name": "Scalper (High Volatility)",
        "description": "Quick in-and-out trades, tight targets. For volatile, active markets.",
        "market_conditions": ["volatile", "high volume", "active", "news-driven"],
        "settings": {
            "strategy": TradingStrategy.SCALPING,
            "max_position_size_algo": 75.0,
            "max_total_positions": 10,
            "stop_loss_percent": 3.0,
            "take_profit_percent": 5.0,
            "max_drawdown_percent": 10.0,
            "min_volume_24h": 3000.0,
            "min_liquidity": 8000.0,
            "scalp_profit_target": 1.5,
            "check_interval_seconds": 30,
            "slippage_tolerance": 1.5,
        }
    },
    "breakout_hunter": {
        "name": "Breakout Hunter",
        "description": "Catch big moves early. Best when expecting major price movements.",
        "market_conditions": ["breakout", "accumulation", "pre-pump", "low volatility before move"],
        "settings": {
            "strategy": TradingStrategy.BREAKOUT,
            "max_position_size_algo": 120.0,
            "max_total_positions": 8,
            "stop_loss_percent": 7.0,
            "take_profit_percent": 25.0,
            "max_drawdown_percent": 15.0,
            "min_volume_24h": 1500.0,
            "min_liquidity": 5000.0,
            "breakout_volume_multiplier": 2.5,
            "check_interval_seconds": 60,
            "slippage_tolerance": 2.5,
        }
    },
    "ai_conservative": {
        "name": "AI Conservative",
        "description": "AI-confirmed signals with conservative risk. Best for cautious AI trading.",
        "market_conditions": ["any", "uncertain", "want AI help"],
        "settings": {
            "strategy": TradingStrategy.MOMENTUM_AI,
            "max_position_size_algo": 75.0,
            "max_total_positions": 6,
            "stop_loss_percent": 6.0,
            "take_profit_percent": 12.0,
            "max_drawdown_percent": 12.0,
            "min_volume_24h": 1500.0,
            "min_liquidity": 7000.0,
            "momentum_threshold": 4.0,
            "check_interval_seconds": 90,
            "slippage_tolerance": 2.0,
            "use_llm": True,
        }
    },
    "ai_aggressive": {
        "name": "AI Aggressive",
        "description": "Full AI control with aggressive settings. Maximum AI autonomy.",
        "market_conditions": ["bull", "high conviction", "trust AI"],
        "settings": {
            "strategy": TradingStrategy.LLM_ASSISTED,
            "max_position_size_algo": 125.0,
            "max_total_positions": 12,
            "stop_loss_percent": 10.0,
            "take_profit_percent": 25.0,
            "max_drawdown_percent": 20.0,
            "min_volume_24h": 800.0,
            "min_liquidity": 4000.0,
            "check_interval_seconds": 60,
            "slippage_tolerance": 2.5,
            "use_llm": True,
        }
    },
    "diamond_hands": {
        "name": "Diamond Hands (HODL)",
        "description": "Wide stops, patient holds. For long-term conviction plays.",
        "market_conditions": ["accumulation", "long-term bullish", "high conviction"],
        "settings": {
            "strategy": TradingStrategy.MOMENTUM,
            "max_position_size_algo": 200.0,
            "max_total_positions": 5,
            "stop_loss_percent": 25.0,
            "take_profit_percent": 50.0,
            "max_drawdown_percent": 30.0,
            "min_volume_24h": 2000.0,
            "min_liquidity": 10000.0,
            "momentum_threshold": 10.0,
            "check_interval_seconds": 300,
            "slippage_tolerance": 3.0,
        }
    },
    "degen": {
        "name": "Degen Mode 🎰",
        "description": "YOLO settings. High risk, high reward. Not financial advice!",
        "market_conditions": ["feeling lucky", "meme season", "YOLO"],
        "settings": {
            "strategy": TradingStrategy.MOMENTUM,
            "max_position_size_algo": 250.0,
            "max_total_positions": 20,
            "stop_loss_percent": 20.0,
            "take_profit_percent": 100.0,
            "max_drawdown_percent": 50.0,
            "min_volume_24h": 200.0,
            "min_liquidity": 1000.0,
            "momentum_threshold": 10.0,
            "check_interval_seconds": 30,
            "slippage_tolerance": 5.0,
        }
    },
    
    # === PROFIT ENHANCEMENT PRESETS ===
    "profit_hunter": {
        "name": "Profit Hunter 💰",
        "description": "Optimized for consistent profits with trailing stops, partial profit taking, and anti-FOMO.",
        "market_conditions": ["any", "profit-focused", "risk-managed"],
        "settings": {
            "strategy": TradingStrategy.MOMENTUM,
            "max_position_size_algo": 50.0,
            "max_total_positions": 5,
            "stop_loss_percent": 8.0,
            "take_profit_percent": 30.0,  # Higher target since we take partials
            "max_drawdown_percent": 15.0,
            "min_volume_24h": 2000.0,
            "min_liquidity": 5000.0,
            "momentum_threshold": 3.0,  # Lower threshold, let filters do the work
            "check_interval_seconds": 45,
            "slippage_tolerance": 2.0,
            # Trailing stops
            "trailing_stop_enabled": True,
            "trailing_stop_activation_percent": 5.0,
            "trailing_stop_distance_percent": 3.0,
            # Partial profits
            "partial_profit_enabled": True,
            "partial_profit_level_1_pct": 8.0,   # Take 25% at 8% profit
            "partial_profit_level_1_sell": 25.0,
            "partial_profit_level_2_pct": 15.0,  # Take 50% at 15% profit
            "partial_profit_level_2_sell": 50.0,
            "partial_profit_level_3_pct": 30.0,  # Take rest at 30% profit
            "partial_profit_level_3_sell": 25.0,
            # Anti-FOMO
            "anti_fomo_enabled": True,
            "anti_fomo_max_1h_pump": 10.0,  # Skip if +10% in 1h
            "anti_fomo_max_24h_pump": 30.0, # Skip if +30% in 24h
            # Profit protection
            "profit_protection_enabled": True,
            "profit_protection_threshold": 5.0,
            "profit_protection_stop": 2.0,
            # Volume confirmation
            "volume_confirmation_enabled": True,
            "min_volume_increase": 1.5,
            # Timing
            "min_hold_minutes": 5,
            # Daily limits
            "max_daily_loss_algo": 25.0,
            "cooldown_after_loss_minutes": 10,
        }
    },
    "conservative_profit": {
        "name": "Conservative Profit 🛡️💰",
        "description": "Very safe settings with tight risk management. Slower but steadier gains.",
        "market_conditions": ["uncertain", "capital preservation", "low risk"],
        "settings": {
            "strategy": TradingStrategy.MEAN_REVERSION,
            "max_position_size_algo": 30.0,
            "max_total_positions": 3,
            "stop_loss_percent": 5.0,
            "take_profit_percent": 15.0,
            "max_drawdown_percent": 10.0,
            "min_volume_24h": 5000.0,
            "min_liquidity": 10000.0,
            "check_interval_seconds": 120,
            "slippage_tolerance": 1.0,
            # Trailing stops - tight
            "trailing_stop_enabled": True,
            "trailing_stop_activation_percent": 3.0,
            "trailing_stop_distance_percent": 2.0,
            # Partial profits - early taking
            "partial_profit_enabled": True,
            "partial_profit_level_1_pct": 5.0,
            "partial_profit_level_1_sell": 33.0,
            "partial_profit_level_2_pct": 10.0,
            "partial_profit_level_2_sell": 33.0,
            "partial_profit_level_3_pct": 15.0,
            "partial_profit_level_3_sell": 34.0,
            # Strong anti-FOMO
            "anti_fomo_enabled": True,
            "anti_fomo_max_1h_pump": 5.0,
            "anti_fomo_max_24h_pump": 15.0,
            # Profit protection
            "profit_protection_enabled": True,
            "profit_protection_threshold": 3.0,
            "profit_protection_stop": 1.0,
            # Daily limits
            "max_daily_loss_algo": 15.0,
            "max_daily_trades": 0,  # Unlimited - use other safety features
            "cooldown_after_loss_minutes": 15,
            # Buy the dip
            "buy_the_dip_enabled": True,
            "dip_percent": 3.0,
            "dip_timeout_minutes": 60,
        }
    },
    
    "ta_enhanced": {
        "name": "TA Enhanced 📊",
        "description": "Uses RSI, MACD, Bollinger Bands for optimal entry/exit timing. Research-backed strategy.",
        "market_conditions": ["any", "technical trading", "chart-based"],
        "settings": {
            "strategy": TradingStrategy.MOMENTUM,
            "max_position_size_algo": 50.0,
            "max_total_positions": 5,
            "stop_loss_percent": 7.0,
            "take_profit_percent": 25.0,
            "max_drawdown_percent": 15.0,
            "min_volume_24h": 3000.0,
            "min_liquidity": 8000.0,
            "momentum_threshold": 2.0,  # Lower - let TA do the filtering
            "check_interval_seconds": 60,
            "slippage_tolerance": 2.0,
            # Technical Analysis - fully enabled
            "use_technical_analysis": True,
            "ta_require_confirmation": True,  # Require multiple TA signals
            # Dynamic sizing
            "use_dynamic_sizing": True,
            "max_portfolio_exposure": 0.6,
            "max_single_position_pct": 0.15,
            # Trailing stops - RSI-aware
            "trailing_stop_enabled": True,
            "trailing_stop_activation_percent": 7.0,
            "trailing_stop_distance_percent": 4.0,
            # Partial profits
            "partial_profit_enabled": True,
            "partial_profit_level_1_pct": 10.0,
            "partial_profit_level_1_sell": 30.0,
            "partial_profit_level_2_pct": 18.0,
            "partial_profit_level_2_sell": 40.0,
            "partial_profit_level_3_pct": 25.0,
            "partial_profit_level_3_sell": 30.0,
            # Anti-FOMO - let TA handle timing
            "anti_fomo_enabled": True,
            "anti_fomo_max_1h_pump": 12.0,
            "anti_fomo_max_24h_pump": 40.0,
            # Volume confirmation
            "volume_confirmation_enabled": True,
            "min_volume_increase": 1.3,
            # Risk limits
            "max_daily_loss_algo": 20.0,
            "cooldown_after_loss_minutes": 5,
            "min_hold_minutes": 3,
        }
    },
    
    "optimal_profit": {
        "name": "Optimal Profit 🎯",
        "description": "All profit-optimization features enabled. Based on DEX bot research best practices.",
        "market_conditions": ["any", "maximum optimization", "research-backed"],
        "settings": {
            "strategy": TradingStrategy.MOMENTUM,
            "max_position_size_algo": 40.0,
            "max_total_positions": 4,
            "stop_loss_percent": 6.0,
            "take_profit_percent": 20.0,
            "max_drawdown_percent": 12.0,
            "min_volume_24h": 5000.0,
            "min_liquidity": 10000.0,
            "momentum_threshold": 2.5,
            "check_interval_seconds": 45,
            "slippage_tolerance": 1.5,
            # === ALL PROFIT FEATURES ENABLED ===
            # Technical Analysis
            "use_technical_analysis": True,
            "ta_require_confirmation": True,
            # Dynamic Position Sizing (Kelly + Volatility)
            "use_dynamic_sizing": True,
            "max_portfolio_exposure": 0.5,
            "max_single_position_pct": 0.12,
            # Trailing Stops
            "trailing_stop_enabled": True,
            "trailing_stop_activation_percent": 5.0,
            "trailing_stop_distance_percent": 3.0,
            # Partial Profit Taking
            "partial_profit_enabled": True,
            "partial_profit_level_1_pct": 6.0,
            "partial_profit_level_1_sell": 25.0,
            "partial_profit_level_2_pct": 12.0,
            "partial_profit_level_2_sell": 35.0,
            "partial_profit_level_3_pct": 20.0,
            "partial_profit_level_3_sell": 40.0,
            # Anti-FOMO (don't chase pumps)
            "anti_fomo_enabled": True,
            "anti_fomo_max_1h_pump": 8.0,
            "anti_fomo_max_24h_pump": 25.0,
            # Profit Protection (tighten stops when winning)
            "profit_protection_enabled": True,
            "profit_protection_threshold": 4.0,
            "profit_protection_stop": 1.5,
            # Volume Confirmation
            "volume_confirmation_enabled": True,
            "min_volume_increase": 1.4,
            # Buy the Dip
            "buy_the_dip_enabled": True,
            "dip_percent": 2.5,
            "dip_timeout_minutes": 45,
            # Timing
            "min_hold_minutes": 3,
            # Daily Safety Limits
            "max_daily_loss_algo": 15.0,
            "max_daily_trades": 0,  # Unlimited - use other safety features
            "cooldown_after_loss_minutes": 8,
            "min_win_rate": 0.0,  # Don't block on win rate
            "min_trades_for_win_rate": 15,
        }
    },
    
    "rug_ninja_sniper": {
        "name": "Rug.ninja Sniper 🥷",
        "description": "Snipe newly minted tokens on rug.ninja bonding curve. Very high risk!",
        "market_conditions": ["meme season", "degen hours", "pump.fun style"],
        "settings": {
            "strategy": TradingStrategy.RUG_NINJA_SNIPER,
            "max_position_size_algo": 10.0,
            "max_total_positions": 10,
            "stop_loss_percent": 30.0,
            "take_profit_percent": 100.0,
            "max_drawdown_percent": 50.0,
            "min_volume_24h": 50.0,
            "min_liquidity": 100.0,
            "check_interval_seconds": 15,
            "slippage_tolerance": 10.0,
            "rug_ninja_enabled": True,
            "rug_ninja_mode": "sniper",
            "rug_ninja_max_buy_algo": 5.0,
            "rug_ninja_min_bond_progress": 0.0,
            "rug_ninja_max_bond_progress": 0.8,
            "rug_ninja_auto_sell_on_bond": True,
            "rug_ninja_max_age_minutes": 30,
        }
    },
    "rug_ninja_graduated": {
        "name": "Rug.ninja Graduated 🎓",
        "description": "Trade tokens that graduated from rug.ninja bonding curve to DEX.",
        "market_conditions": ["meme trading", "established memes", "post-bond"],
        "settings": {
            "strategy": TradingStrategy.RUG_NINJA_GRADUATED,
            "max_position_size_algo": 25.0,
            "max_total_positions": 8,
            "stop_loss_percent": 15.0,
            "take_profit_percent": 50.0,
            "max_drawdown_percent": 30.0,
            "min_volume_24h": 500.0,
            "min_liquidity": 1000.0,
            "check_interval_seconds": 45,
            "slippage_tolerance": 5.0,
            "rug_ninja_enabled": True,
            "rug_ninja_mode": "graduated",
            "rug_ninja_max_buy_algo": 25.0,
        }
    },
    
    # AlphaArcade Prediction Market Presets
    # NOTE: Requires partner API key from AlphaArcade team
    # Docs: https://alphaarcade.gitbook.io/alphaarcade-docs
    "alpha_arcade_value": {
        "name": "AlphaArcade Value 🎯",
        "description": "Contrarian betting on undervalued prediction outcomes. Requires API key.",
        "market_conditions": ["prediction markets", "contrarian", "value betting"],
        "settings": {
            "strategy": TradingStrategy.ALPHA_ARCADE_VALUE,
            "max_position_size_algo": 20.0,
            "max_total_positions": 10,
            "stop_loss_percent": 25.0,
            "take_profit_percent": 100.0,
            "max_drawdown_percent": 35.0,
            "min_volume_24h": 100.0,
            "min_liquidity": 500.0,
            "check_interval_seconds": 120,
            "slippage_tolerance": 2.0,
            "alpha_arcade_enabled": True,
            "alpha_arcade_mode": "value",
            "alpha_arcade_max_bet_algo": 20.0,
            "alpha_arcade_value_threshold": 0.15,
        }
    },
    "alpha_arcade_momentum": {
        "name": "AlphaArcade Momentum 🎯",
        "description": "Follow prediction market trends and momentum. Requires API key.",
        "market_conditions": ["prediction markets", "trending", "momentum"],
        "settings": {
            "strategy": TradingStrategy.ALPHA_ARCADE_MOMENTUM,
            "max_position_size_algo": 15.0,
            "max_total_positions": 8,
            "stop_loss_percent": 20.0,
            "take_profit_percent": 50.0,
            "max_drawdown_percent": 30.0,
            "min_volume_24h": 200.0,
            "min_liquidity": 1000.0,
            "check_interval_seconds": 60,
            "slippage_tolerance": 3.0,
            "alpha_arcade_enabled": True,
            "alpha_arcade_mode": "momentum",
            "alpha_arcade_max_bet_algo": 15.0,
            "alpha_arcade_momentum_threshold": 0.05,
        }
    },
    "alpha_arcade_lp": {
        "name": "AlphaArcade LP Bot 🎯💰",
        "description": "Provide liquidity on both sides for LP rewards. Requires API key.",
        "market_conditions": ["prediction markets", "market making", "LP rewards"],
        "settings": {
            "strategy": TradingStrategy.ALPHA_ARCADE_VALUE,
            "max_position_size_algo": 50.0,
            "max_total_positions": 20,
            "stop_loss_percent": 10.0,
            "take_profit_percent": 20.0,
            "max_drawdown_percent": 25.0,
            "min_volume_24h": 100.0,
            "min_liquidity": 500.0,
            "check_interval_seconds": 60,
            "slippage_tolerance": 1.0,
            "alpha_arcade_enabled": True,
            "alpha_arcade_mode": "value",
            "alpha_arcade_max_bet_algo": 30.0,
            "alpha_arcade_lp_mode": True,
        }
    },
    "alpha_arcade_conservative": {
        "name": "AlphaArcade Conservative 🎯",
        "description": "Safe prediction betting on high-probability outcomes. Requires API key.",
        "market_conditions": ["prediction markets", "safe bets", "high confidence"],
        "settings": {
            "strategy": TradingStrategy.ALPHA_ARCADE_VALUE,
            "max_position_size_algo": 10.0,
            "max_total_positions": 5,
            "stop_loss_percent": 15.0,
            "take_profit_percent": 30.0,
            "max_drawdown_percent": 20.0,
            "min_volume_24h": 500.0,
            "min_liquidity": 2000.0,
            "check_interval_seconds": 300,
            "slippage_tolerance": 1.0,
            "alpha_arcade_enabled": True,
            "alpha_arcade_mode": "value",
            "alpha_arcade_max_bet_algo": 10.0,
            "alpha_arcade_min_price": 0.70,
            "alpha_arcade_max_price": 0.95,
            "alpha_arcade_value_threshold": 0.05,
        }
    },
}

# Custom presets file location
CUSTOM_PRESETS_FILE = os.path.expanduser("~/.algorand_bot_presets.json")


def _restrict_to_owner(path: str) -> None:
    """Restrict a file to owner-only read/write.

    Presets can embed a partner API key (alpha_arcade_api_key), so this file
    shouldn't be left group/world-readable under whatever umask the process
    inherited. No-op (best effort) on platforms/filesystems that don't
    support POSIX permission bits.
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def get_available_ollama_models() -> List[Dict[str, str]]:
    """Scan for available Ollama models."""
    models = []
    try:
        import ollama
        response = ollama.list()
        
        # Handle different response formats from ollama.list()
        model_list = response.get("models", [])
        
        # If response is a ListResponse object, iterate directly
        if hasattr(response, 'models'):
            model_list = response.models
        
        for model in model_list:
            # Try different ways to get the model name
            if hasattr(model, 'model'):
                name = model.model
            elif hasattr(model, 'name'):
                name = model.name
            elif isinstance(model, dict):
                name = model.get("model", model.get("name", "unknown"))
            else:
                name = str(model)
            
            # Get size
            if hasattr(model, 'size'):
                size = model.size
            elif isinstance(model, dict):
                size = model.get("size", 0)
            else:
                size = 0
            
            # Convert size to human readable
            if size > 1e9:
                size_str = f"{size / 1e9:.1f}GB"
            elif size > 1e6:
                size_str = f"{size / 1e6:.0f}MB"
            else:
                size_str = f"{size / 1e3:.0f}KB" if size > 0 else "?"
            
            # Get parameter count if available
            if hasattr(model, 'details'):
                details = model.details
                if hasattr(details, 'parameter_size'):
                    params = details.parameter_size
                elif isinstance(details, dict):
                    params = details.get("parameter_size", "")
                else:
                    params = ""
                
                if hasattr(details, 'family'):
                    family = details.family
                elif isinstance(details, dict):
                    family = details.get("family", "")
                else:
                    family = ""
            elif isinstance(model, dict):
                details = model.get("details", {})
                params = details.get("parameter_size", "") if isinstance(details, dict) else ""
                family = details.get("family", "") if isinstance(details, dict) else ""
            else:
                params = ""
                family = ""
            
            models.append({
                "name": name,
                "size": size_str,
                "params": params,
                "family": family,
            })
    except Exception as e:
        log_warning(f"Could not scan Ollama models: {e}")
    
    return models


def load_custom_presets() -> Dict[str, Dict]:
    """Load custom presets from file."""
    if os.path.exists(CUSTOM_PRESETS_FILE):
        try:
            with open(CUSTOM_PRESETS_FILE, "r") as f:
                data = json.load(f)
                # Convert strategy strings back to enums
                for preset_name, preset in data.items():
                    if "settings" in preset and "strategy" in preset["settings"]:
                        strategy_str = preset["settings"]["strategy"]
                        for s in TradingStrategy:
                            if s.value == strategy_str:
                                preset["settings"]["strategy"] = s
                                break
                return data
        except json.JSONDecodeError as e:
            # Corrupted JSON file - backup and reset
            log_warning(f"Custom presets file corrupted: {e}")
            backup_file = CUSTOM_PRESETS_FILE + ".backup"
            try:
                os.rename(CUSTOM_PRESETS_FILE, backup_file)
                log_info(f"Backed up corrupted file to {backup_file}")
            except:
                try:
                    os.remove(CUSTOM_PRESETS_FILE)
                    log_info("Removed corrupted presets file")
                except:
                    pass
        except Exception as e:
            log_warning(f"Could not load custom presets: {e}")
    return {}


def save_custom_preset(name: str, config: TradingConfig, description: str = ""):
    """Save a custom preset to file."""
    presets = load_custom_presets()
    
    # Convert any TradingStrategy enums back to strings for JSON serialization
    for preset_name, preset in presets.items():
        if "settings" in preset and "strategy" in preset["settings"]:
            strategy_val = preset["settings"]["strategy"]
            if isinstance(strategy_val, TradingStrategy):
                preset["settings"]["strategy"] = strategy_val.value
    
    # Convert config to dict - include all relevant settings
    settings = {
        "strategy": config.strategy.value,  # Convert enum to string for JSON
        "max_position_size_algo": config.max_position_size_algo,
        "max_total_positions": config.max_total_positions,
        "stop_loss_percent": config.stop_loss_percent,
        "take_profit_percent": config.take_profit_percent,
        "max_drawdown_percent": config.max_drawdown_percent,
        "min_volume_24h": config.min_volume_24h,
        "min_liquidity": config.min_liquidity,
        "momentum_threshold": config.momentum_threshold,
        "check_interval_seconds": config.check_interval_seconds,
        "slippage_tolerance": config.slippage_tolerance,
        "use_llm": config.use_llm,
        "llm_model": config.llm_model,
        "scalp_profit_target": config.scalp_profit_target,
        "breakout_volume_multiplier": config.breakout_volume_multiplier,
        "mean_reversion_std_multiplier": config.mean_reversion_std_multiplier,
        # Multi-LLM settings
        "multi_llm_enabled": config.multi_llm_enabled,
        "llm_market_analysis": config.llm_market_analysis,
        "llm_trade_decisions": config.llm_trade_decisions,
        "llm_strategy_reasoning": config.llm_strategy_reasoning,
        "llm_risk_assessment": config.llm_risk_assessment,
        # AI dynamic re-evaluation settings
        "ai_dynamic_reeval": config.ai_dynamic_reeval if hasattr(config, 'ai_dynamic_reeval') else False,
        "ai_reeval_interval_minutes": config.ai_reeval_interval_minutes if hasattr(config, 'ai_reeval_interval_minutes') else 30,
        "ai_reeval_auto_apply": config.ai_reeval_auto_apply if hasattr(config, 'ai_reeval_auto_apply') else False,
        "ai_include_rug_ninja": config.ai_include_rug_ninja if hasattr(config, 'ai_include_rug_ninja') else False,
        "ai_include_alpha_arcade": config.ai_include_alpha_arcade if hasattr(config, 'ai_include_alpha_arcade') else False,
        # Position management
        "manage_imported_positions": config.manage_imported_positions if hasattr(config, 'manage_imported_positions') else False,
        # Rug.ninja settings
        "rug_ninja_enabled": config.rug_ninja_enabled,
        "rug_ninja_mode": config.rug_ninja_mode,
        "rug_ninja_max_buy_algo": config.rug_ninja_max_buy_algo,
        "rug_ninja_min_bond_progress": config.rug_ninja_min_bond_progress,
        "rug_ninja_max_bond_progress": config.rug_ninja_max_bond_progress,
        "rug_ninja_auto_sell_on_bond": config.rug_ninja_auto_sell_on_bond,
        "rug_ninja_max_age_minutes": config.rug_ninja_max_age_minutes,
        "rug_ninja_realtime_sniper": config.rug_ninja_realtime_sniper if hasattr(config, 'rug_ninja_realtime_sniper') else False,
        # AlphaArcade settings
        "alpha_arcade_enabled": config.alpha_arcade_enabled,
        "alpha_arcade_api_key": config.alpha_arcade_api_key,  # Save API key in preset
        "alpha_arcade_mode": config.alpha_arcade_mode,
        "alpha_arcade_max_bet_algo": config.alpha_arcade_max_bet_algo,
        "alpha_arcade_min_volume": config.alpha_arcade_min_volume,
        "alpha_arcade_min_liquidity": config.alpha_arcade_min_liquidity,
        "alpha_arcade_value_threshold": config.alpha_arcade_value_threshold,
        "alpha_arcade_momentum_threshold": config.alpha_arcade_momentum_threshold,
        "alpha_arcade_lp_mode": config.alpha_arcade_lp_mode if hasattr(config, 'alpha_arcade_lp_mode') else False,
        "alpha_arcade_auto_sell_before_resolution": config.alpha_arcade_auto_sell_before_resolution if hasattr(config, 'alpha_arcade_auto_sell_before_resolution') else True,
        "alpha_arcade_hours_before_resolution": config.alpha_arcade_hours_before_resolution if hasattr(config, 'alpha_arcade_hours_before_resolution') else 24,
    }
    
    presets[name] = {
        "name": f"Custom: {name}",
        "description": description or f"Custom preset saved on {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "market_conditions": ["custom"],
        "settings": settings,
        "custom": True,
    }
    
    try:
        with open(CUSTOM_PRESETS_FILE, "w") as f:
            json.dump(presets, f, indent=2)
        _restrict_to_owner(CUSTOM_PRESETS_FILE)
        log_success(f"Saved custom preset: {name}")
        return True
    except Exception as e:
        log_error(f"Could not save preset: {e}")
        return False


def delete_custom_preset(name: str) -> bool:
    """Delete a custom preset."""
    presets = load_custom_presets()
    if name in presets:
        del presets[name]
        try:
            with open(CUSTOM_PRESETS_FILE, "w") as f:
                json.dump(presets, f, indent=2)
            _restrict_to_owner(CUSTOM_PRESETS_FILE)
            log_success(f"Deleted preset: {name}")
            return True
        except Exception as e:
            log_error(f"Could not delete preset: {e}")
    return False


def apply_preset_to_config(config: TradingConfig, preset: Dict) -> TradingConfig:
    """Apply a preset's settings to a config object."""
    settings = preset.get("settings", {})
    
    for key, value in settings.items():
        if hasattr(config, key):
            # Handle strategy enum conversion
            if key == "strategy" and isinstance(value, str):
                try:
                    value = TradingStrategy(value)
                except ValueError:
                    # Try to find matching strategy
                    for s in TradingStrategy:
                        if s.value == value or s.name.lower() == value.lower():
                            value = s
                            break
            setattr(config, key, value)
    
    config.preset_name = preset.get("name", "custom")
    return config


def get_ai_preset_suggestion(llm_model: str) -> Optional[str]:
    """Use AI to analyze market and suggest a preset."""
    try:
        import ollama
        
        # Build preset descriptions dynamically
        preset_descriptions = "\n".join([
            f"- {key}: {preset['name']} - {preset['description']} (for: {', '.join(preset['market_conditions'])})"
            for key, preset in TRADING_PRESETS.items()
        ])
        
        # Get all valid preset keys dynamically
        valid_keys = list(TRADING_PRESETS.keys())
        valid_keys_str = ", ".join(valid_keys)
        
        prompt = f"""Choose ONE trading preset for current crypto market conditions.

AVAILABLE PRESETS:
{preset_descriptions}

Consider the current market conditions and select the most appropriate preset.
Include profit-focused presets (profit_hunter, optimal_profit, ta_enhanced) if you think they'd work well.

Reply with JSON only: {{"suggested_preset": "<key>", "reasoning": "<why this preset fits current conditions>"}}

Valid keys: {valid_keys_str}

JSON:"""

        log_info("Asking AI for preset suggestion...")
        
        # Use higher token limit for reasoning models that output <think> blocks
        response = ollama.chat(
            model=llm_model,
            messages=[
                {"role": "system", "content": "You are a trading advisor. Analyze market conditions and recommend the best preset. Reply with JSON only."},
                {"role": "user", "content": prompt}
            ],
            options={"temperature": 0.4, "num_predict": 2000}
        )
        
        content = response["message"]["content"]
        log_info(f"AI response received ({len(content)} chars)")
        
        result = parse_llm_json(content)
        
        if not result:
            log_warning(f"Could not parse AI response as JSON")
            log_info(f"Raw response: {content[:200]}...")
            return None
        
        suggested = result.get("suggested_preset", "").lower().strip()
        reasoning = result.get("reasoning", "")
        
        log_info(f"AI suggested: '{suggested}'")
        
        if suggested in TRADING_PRESETS:
            log_success(f"AI Reasoning: {reasoning}")
            return suggested
        
        # Try to find partial match
        for key in TRADING_PRESETS:
            if key in suggested or suggested in key:
                log_success(f"AI Reasoning: {reasoning}")
                return key
        
        # Try matching preset names
        for key, preset in TRADING_PRESETS.items():
            preset_name = preset['name'].lower()
            if suggested in preset_name or preset_name in suggested:
                log_success(f"AI Reasoning: {reasoning}")
                return key
        
        log_warning(f"AI suggested '{suggested}' which doesn't match any preset")
        return None
        
    except Exception as e:
        log_error(f"AI suggestion failed: {e}")
        import traceback
        traceback.print_exc()
        return None


def get_ai_strategy_suggestion(llm_model: str, include_rug_ninja: bool = False, include_alpha_arcade: bool = False) -> Optional[Dict]:
    """Use AI to analyze market conditions and suggest a trading strategy.
    
    The AI suggests an ASA trading strategy (momentum, mean_reversion, etc.)
    If rug.ninja or AlphaArcade are enabled, they run ALONGSIDE the ASA strategy.
    
    Args:
        llm_model: The Ollama model to use
        include_rug_ninja: Whether to also enable rug.ninja trading alongside ASA
        include_alpha_arcade: Whether to also enable AlphaArcade trading alongside ASA
    """
    try:
        import ollama
        
        # Core ASA strategies - AI picks ONE of these
        asa_strategies = {
            "momentum": "Follow price trends - best when market has clear direction",
            "mean_reversion": "Buy oversold, sell overbought - best for ranging/choppy markets",
            "breakout": "Catch big moves early - best when volatility is low but about to spike",
            "scalping": "Quick small profits - best for high volatility, active markets",
        }
        
        strategy_list = "\n".join([
            f"- {key}: {desc}" for key, desc in asa_strategies.items()
        ])
        
        valid_keys = ", ".join(asa_strategies.keys())
        
        # Build context about additional features
        additional_features = ""
        if include_rug_ninja and include_alpha_arcade:
            additional_features = """
ADDITIONAL CONTEXT:
The bot will ALSO be running:
- Rug.ninja (meme coin sniping) - for high-risk/high-reward meme tokens
- AlphaArcade (prediction markets) - for betting on event outcomes

These run IN ADDITION to the ASA strategy you recommend.
Consider how your ASA strategy choice complements these features."""
        elif include_rug_ninja:
            additional_features = """
ADDITIONAL CONTEXT:
The bot will ALSO be running rug.ninja (meme coin sniping) alongside your recommended ASA strategy.
Consider: If rug.ninja provides high-risk exposure, you might recommend a more conservative ASA strategy for balance.
Or if the user wants maximum aggression, recommend an aggressive ASA strategy too."""
        elif include_alpha_arcade:
            additional_features = """
ADDITIONAL CONTEXT:
The bot will ALSO be running AlphaArcade (prediction market betting) alongside your recommended ASA strategy.
Consider how your ASA strategy choice complements prediction market trading."""
        
        prompt = f"""You are a crypto trading strategy advisor. Recommend ONE ASA (Algorand Standard Asset) trading strategy.

AVAILABLE ASA STRATEGIES (pick ONE):
{strategy_list}
{additional_features}

Consider current market conditions:
1. Overall crypto market sentiment (bullish/bearish/sideways)
2. Volatility levels (high favors scalping, low favors breakout)
3. Risk/reward tradeoffs

Reply with JSON:
{{
    "recommended_strategy": "<exact_key_from_list>",
    "confidence": 0.0-1.0,
    "market_analysis": "<brief current conditions>",
    "why_this_strategy": "<why this ASA strategy is best right now>",
    "risk_notes": "<main risks>"
}}

VALID KEYS (use EXACTLY one): {valid_keys}

JSON:"""

        log_info("AI analyzing market conditions for ASA strategy suggestion...")
        if include_rug_ninja:
            log_info("  → rug.ninja will also be ENABLED")
        if include_alpha_arcade:
            log_info("  → AlphaArcade will also be ENABLED")
        
        response = ollama.chat(
            model=llm_model,
            messages=[
                {"role": "system", "content": "You are a trading strategy advisor. Recommend the best ASA trading strategy for current conditions. Reply with JSON only."},
                {"role": "user", "content": prompt}
            ],
            options={"temperature": 0.5, "num_predict": 2000}
        )
        
        content = response["message"]["content"]
        log_info(f"AI response received, parsing...")
        result = parse_llm_json(content)
        
        if result:
            strategy = result.get("recommended_strategy", "").lower().strip()
            log_info(f"AI suggested ASA strategy: '{strategy}'")
            
            # Validate strategy is one of the ASA strategies
            if strategy in asa_strategies:
                log_success(f"AI recommended ASA strategy: {strategy}")
                return {
                    "strategy": strategy,
                    "confidence": result.get("confidence", 0.5),
                    "analysis": result.get("market_analysis", ""),
                    "why": result.get("why_this_strategy", ""),
                    "risks": result.get("risk_notes", ""),
                    "is_rug_ninja": False,  # ASA strategy, not rug.ninja
                    "is_alpha_arcade": False,  # ASA strategy, not alpha arcade
                    "enable_rug_ninja": include_rug_ninja,  # But enable alongside
                    "enable_alpha_arcade": include_alpha_arcade  # But enable alongside
                }
            
            # Try partial match
            for key in asa_strategies:
                if key in strategy or strategy in key:
                    log_success(f"AI recommended ASA strategy: {key} (matched from '{strategy}')")
                    return {
                        "strategy": key,
                        "confidence": result.get("confidence", 0.5),
                        "analysis": result.get("market_analysis", ""),
                        "why": result.get("why_this_strategy", ""),
                        "risks": result.get("risk_notes", ""),
                        "is_rug_ninja": False,
                        "is_alpha_arcade": False,
                        "enable_rug_ninja": include_rug_ninja,
                        "enable_alpha_arcade": include_alpha_arcade
                    }
            
            log_warning(f"AI suggested unknown strategy: {strategy}, defaulting to momentum")
            return {
                "strategy": "momentum",
                "confidence": 0.5,
                "analysis": result.get("market_analysis", "Could not parse AI suggestion"),
                "why": "Defaulted to momentum as AI suggestion was unclear",
                "risks": result.get("risk_notes", ""),
                "is_rug_ninja": False,
                "is_alpha_arcade": False,
                "enable_rug_ninja": include_rug_ninja,
                "enable_alpha_arcade": include_alpha_arcade
            }
        
        return None
        
    except Exception as e:
        log_warning(f"AI strategy suggestion failed: {e}")
        return None


def get_ai_market_reeval(llm_model: str, current_strategy: str, current_preset: str, 
                         recent_performance: Dict, include_rug_ninja: bool = False,
                         include_alpha_arcade: bool = False) -> Optional[Dict]:
    """
    AI re-evaluates market conditions and suggests strategy/preset changes.
    Called periodically when ai_dynamic_reeval is enabled.
    
    IMPORTANT: This only suggests changes to ASA strategies (momentum, mean_reversion, etc.)
    Rug.ninja and AlphaArcade run as ADDITIONAL FEATURES alongside, not as replacements.
    
    Args:
        llm_model: The Ollama model to use
        current_strategy: Current strategy name
        current_preset: Current preset name
        recent_performance: Dict with win_rate, pnl, num_trades
        include_rug_ninja: Whether rug.ninja is currently enabled as additional feature
        include_alpha_arcade: Whether AlphaArcade is currently enabled as additional feature
        
    Returns:
        Dict with recommendation or None if no change needed
    """
    try:
        import ollama
        
        # ONLY ASA strategies - rug.ninja and AlphaArcade are separate features, not strategies
        valid_strategies = ["momentum", "mean_reversion", "breakout", "scalping"]
        strategy_descriptions = {
            "momentum": "Follow price trends - best in trending markets",
            "mean_reversion": "Buy oversold, sell overbought - best in ranging markets", 
            "breakout": "Catch big moves early - best when volatility is building",
            "scalping": "Quick small profits - best in high volatility",
        }
        
        # Format strategy list for prompt
        strategy_list = "\n".join([f'  "{s}": {strategy_descriptions.get(s, "")}' for s in valid_strategies])
        strategy_keys_str = '" | "'.join(valid_strategies)
        
        # Build preset info from TRADING_PRESETS - exclude rug_ninja and alpha_arcade specific presets
        preset_keys = []
        preset_info_lines = []
        for key, preset in TRADING_PRESETS.items():
            # Skip rug_ninja and alpha_arcade specific presets - those are for dedicated modes
            if "rug_ninja" in key or "alpha_arcade" in key:
                continue
            preset_keys.append(key)
            settings = preset.get("settings", {})
            sl = settings.get("stop_loss_percent", "?")
            tp = settings.get("take_profit_percent", "?")
            if "profit" in key:
                risk_note = " 💰PROFIT-FOCUSED"
            elif "conservative" in key:
                risk_note = " 🛡️SAFE"
            elif "aggressive" in key or "degen" in key:
                risk_note = " ⚡HIGH-RISK"
            else:
                risk_note = ""
            preset_info_lines.append(f'  "{key}": SL {sl}% / TP {tp}%{risk_note}')
        
        preset_list = "\n".join(preset_info_lines)
        
        # Performance data
        win_rate = recent_performance.get('win_rate', 0)
        pnl = recent_performance.get('pnl', 0)
        num_trades = recent_performance.get('num_trades', 0)
        
        # Current additional features context
        additional_context = ""
        if include_rug_ninja or include_alpha_arcade:
            additional_context = "\nADDITIONAL FEATURES RUNNING ALONGSIDE:"
            if include_rug_ninja:
                additional_context += "\n- Rug.ninja (meme coins) is ENABLED"
            if include_alpha_arcade:
                additional_context += "\n- AlphaArcade (predictions) is ENABLED"
            additional_context += "\nThese features run IN ADDITION to the ASA strategy. Focus on optimizing the ASA strategy.\n"
        
        # Get base strategy name (strip _ai suffix if present)
        base_strategy = current_strategy.replace("_ai", "")
        
        prompt = f"""Analyze this trading bot's ASA (Algorand Standard Asset) trading performance and recommend changes.

CURRENT STATE:
- ASA Strategy: {base_strategy}
- Preset: {current_preset}
- Win Rate: {win_rate:.1f}% ({num_trades} trades)
- P/L: {pnl:+.4f} ALGO
{additional_context}
AVAILABLE ASA STRATEGIES (pick ONE):
{strategy_list}

AVAILABLE PRESETS:
{preset_list}

DECISION RULES:
- If performance is GOOD (win rate > 50%, positive P/L): recommend "keep"
- If LOSING MONEY: consider switching to a different ASA strategy or adjusting preset
- If win rate is low with high volatility: consider scalping or breakout
- If market is choppy: consider mean_reversion
- If market is trending: consider momentum

Reply with this JSON only:
{{
    "recommendation": "keep" | "change_strategy" | "change_preset" | "change_both",
    "new_strategy": "{strategy_keys_str}" | null,
    "new_preset": "<preset_key>" | null,
    "confidence": 0.0-1.0,
    "reasoning": "why this change helps the ASA trading",
    "urgency": "low" | "medium" | "high"
}}

JSON:"""

        log_info("AI re-evaluating ASA strategy and preset...")
        
        response = ollama.chat(
            model=llm_model,
            messages=[
                {"role": "system", "content": "You are a trading advisor. Recommend changes to the ASA trading strategy and preset. Reply with valid JSON only."},
                {"role": "user", "content": prompt}
            ],
            options={"temperature": 0.4, "num_predict": 2000}
        )
        
        content = response["message"]["content"]
        result = parse_llm_json(content)
        
        if not result:
            log_warning("AI re-eval: Failed to parse response")
            return None
        
        recommendation = result.get("recommendation", "keep").lower().strip()
        
        # Normalize recommendation
        if "both" in recommendation:
            recommendation = "change_both"
        elif "preset" in recommendation:
            recommendation = "change_preset"
        elif "strat" in recommendation:
            recommendation = "change_strategy"
        elif recommendation not in ["keep", "change_strategy", "change_preset", "change_both"]:
            recommendation = "keep"
        
        if recommendation == "keep":
            return {"recommendation": "keep", "reasoning": result.get("reasoning", "")}
        
        new_strategy = result.get("new_strategy")
        new_preset = result.get("new_preset")
        
        # Clean up values (remove quotes, whitespace)
        if new_strategy:
            new_strategy = str(new_strategy).strip().strip('"').strip("'").lower()
        if new_preset:
            new_preset = str(new_preset).strip().strip('"').strip("'").lower()
        
        # Fuzzy match preset if exact match fails
        if new_preset and new_preset not in preset_keys:
            # Try to find a close match
            for pk in preset_keys:
                if new_preset in pk or pk in new_preset:
                    log_info(f"AI re-eval: Fuzzy matched preset '{new_preset}' -> '{pk}'")
                    new_preset = pk
                    break
            else:
                # No match found
                log_warning(f"AI re-eval: Invalid preset key '{new_preset}', ignoring")
                new_preset = None
        
        # Validate strategy
        if new_strategy and new_strategy not in valid_strategies:
            log_warning(f"AI re-eval: Invalid strategy '{new_strategy}', ignoring")
            new_strategy = None
        
        # Adjust recommendation based on what we actually have
        if recommendation == "change_both":
            if not new_strategy and not new_preset:
                return {"recommendation": "keep", "reasoning": "AI suggested changes but didn't specify valid options"}
            elif not new_strategy:
                recommendation = "change_preset"
            elif not new_preset:
                recommendation = "change_strategy"
        elif recommendation == "change_strategy" and not new_strategy:
            if new_preset:
                recommendation = "change_preset"
            else:
                return {"recommendation": "keep", "reasoning": "AI suggested strategy change but didn't specify valid strategy"}
        elif recommendation == "change_preset" and not new_preset:
            if new_strategy:
                recommendation = "change_strategy"
            else:
                return {"recommendation": "keep", "reasoning": "AI suggested preset change but didn't specify valid preset"}
        
        return {
            "recommendation": recommendation,
            "new_strategy": new_strategy,
            "new_preset": new_preset,
            "confidence": result.get("confidence", 0.5),
            "reasoning": result.get("reasoning", ""),
            "urgency": result.get("urgency", "low")
        }
        
    except Exception as e:
        log_warning(f"AI re-evaluation failed: {e}")
        return None


def parse_llm_json(content: str) -> Optional[Dict]:
    """
    Robustly parse JSON from LLM output, handling common issues:
    - Thinking/reasoning blocks (<think>...</think>, <reasoning>...</reasoning>)
    - Trailing commas
    - Missing values
    - Extra text before/after JSON
    - Newlines in strings
    - Markdown code blocks
    """
    import re
    
    if not content:
        return None
    
    # Remove thinking/reasoning blocks that some models output
    # Handles: <think>...</think>, <reasoning>...</reasoning>, <thought>...</thought>
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL | re.IGNORECASE)
    content = re.sub(r'<reasoning>.*?</reasoning>', '', content, flags=re.DOTALL | re.IGNORECASE)
    content = re.sub(r'<thought>.*?</thought>', '', content, flags=re.DOTALL | re.IGNORECASE)
    content = re.sub(r'<reflection>.*?</reflection>', '', content, flags=re.DOTALL | re.IGNORECASE)
    
    # Remove markdown code blocks (```json ... ```)
    content = re.sub(r'```json\s*', '', content, flags=re.IGNORECASE)
    content = re.sub(r'```\s*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'```', '', content)
    
    # Find JSON object boundaries
    start = content.find("{")
    end = content.rfind("}") + 1
    
    if start < 0 or end <= start:
        return None
    
    json_str = content[start:end]
    
    # Try parsing as-is first
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        pass
    
    # Fix common LLM JSON issues
    fixed_json = json_str
    
    # Remove trailing commas before } or ]
    fixed_json = re.sub(r',(\s*[}\]])', r'\1', fixed_json)
    
    # Remove trailing commas at end of lines before newline + }
    fixed_json = re.sub(r',(\s*\n\s*[}\]])', r'\1', fixed_json)
    
    # Try again after fixing trailing commas
    try:
        return json.loads(fixed_json)
    except json.JSONDecodeError:
        pass
    
    # Try to fix unquoted values (e.g., None instead of null)
    fixed_json = fixed_json.replace(': None', ': null')
    fixed_json = fixed_json.replace(':None', ':null')
    fixed_json = fixed_json.replace(': True', ': true')
    fixed_json = fixed_json.replace(':True', ':true')
    fixed_json = fixed_json.replace(': False', ': false')
    fixed_json = fixed_json.replace(':False', ':false')
    
    try:
        return json.loads(fixed_json)
    except json.JSONDecodeError:
        pass
    
    # Try removing any control characters
    fixed_json = ''.join(char for char in fixed_json if ord(char) >= 32 or char in '\n\r\t')
    
    try:
        return json.loads(fixed_json)
    except json.JSONDecodeError:
        pass
    
    # Last resort: try to extract key-value pairs manually
    try:
        # Simple regex extraction for basic cases
        result = {}
        
        # Look for "key": "value" patterns
        string_matches = re.findall(r'"(\w+)":\s*"([^"]*)"', json_str)
        for key, value in string_matches:
            result[key] = value
        
        # Look for "key": number patterns
        num_matches = re.findall(r'"(\w+)":\s*(\d+\.?\d*)', json_str)
        for key, value in num_matches:
            if key not in result:
                result[key] = float(value) if '.' in value else int(value)
        
        # Look for "key": [array] patterns
        array_matches = re.findall(r'"(\w+)":\s*\[([^\]]*)\]', json_str)
        for key, value in array_matches:
            if key not in result:
                # Try to parse array items
                items = re.findall(r'\{[^}]+\}', value)
                if items:
                    result[key] = [parse_llm_json(item) for item in items]
                    result[key] = [x for x in result[key] if x is not None]
        
        if result:
            return result
            
    except Exception:
        pass
    
    return None


# ============================================================================
# TECHNICAL ANALYSIS MODULE (Based on DEX Trading Bot Best Practices)
# ============================================================================
# Implements indicators from TA-Lib concepts without requiring the library:
# - RSI (Relative Strength Index) for overbought/oversold
# - MACD for trend confirmation
# - Bollinger Bands for volatility and mean reversion
# - Moving Average crossovers for trend direction
# - Volume analysis for confirmation
# - ATR (Average True Range) for volatility-based sizing

class TechnicalAnalysis:
    """
    Technical analysis calculations for trading signals.
    
    Implements key indicators without requiring TA-Lib:
    - RSI: Identifies overbought (>70) and oversold (<30) conditions
    - MACD: Trend confirmation via moving average convergence/divergence
    - Bollinger Bands: Volatility bands for mean reversion signals
    - Moving Averages: SMA/EMA crossovers for trend direction
    - Volume Analysis: Confirms price movements
    - ATR: Measures volatility for position sizing
    """
    
    @staticmethod
    def calculate_sma(prices: List[float], period: int) -> Optional[float]:
        """Calculate Simple Moving Average."""
        if len(prices) < period:
            return None
        return sum(prices[-period:]) / period
    
    @staticmethod
    def calculate_ema(prices: List[float], period: int) -> Optional[float]:
        """Calculate Exponential Moving Average."""
        if len(prices) < period:
            return None
        
        multiplier = 2 / (period + 1)
        ema = prices[0]
        
        for price in prices[1:]:
            ema = (price * multiplier) + (ema * (1 - multiplier))
        
        return ema
    
    @staticmethod
    def calculate_rsi(prices: List[float], period: int = 14) -> Optional[float]:
        """
        Calculate Relative Strength Index (RSI).
        
        RSI < 30: Oversold (potential buy signal)
        RSI > 70: Overbought (potential sell signal)
        RSI 30-70: Neutral
        """
        if len(prices) < period + 1:
            return None
        
        gains = []
        losses = []
        
        for i in range(1, len(prices)):
            change = prices[i] - prices[i-1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
        
        if len(gains) < period:
            return None
        
        # Calculate average gain and loss
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        
        if avg_loss == 0:
            return 100.0  # No losses = RSI 100
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    @staticmethod
    def calculate_macd(prices: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Optional[Dict]:
        """
        Calculate MACD (Moving Average Convergence Divergence).
        
        Returns:
            - macd_line: Fast EMA - Slow EMA
            - signal_line: EMA of MACD line
            - histogram: MACD line - Signal line
            - trend: 'bullish' if MACD > signal, 'bearish' if MACD < signal
        """
        if len(prices) < slow + signal:
            return None
        
        # Calculate EMAs
        fast_ema = TechnicalAnalysis.calculate_ema(prices, fast)
        slow_ema = TechnicalAnalysis.calculate_ema(prices, slow)
        
        if fast_ema is None or slow_ema is None:
            return None
        
        macd_line = fast_ema - slow_ema
        
        # Calculate signal line (EMA of MACD values)
        # We need historical MACD values for this
        macd_values = []
        for i in range(slow, len(prices) + 1):
            subset = prices[:i]
            f_ema = TechnicalAnalysis.calculate_ema(subset, fast)
            s_ema = TechnicalAnalysis.calculate_ema(subset, slow)
            if f_ema and s_ema:
                macd_values.append(f_ema - s_ema)
        
        if len(macd_values) < signal:
            signal_line = macd_line  # Not enough data
        else:
            signal_line = TechnicalAnalysis.calculate_ema(macd_values, signal)
        
        histogram = macd_line - signal_line if signal_line else 0
        
        return {
            'macd_line': macd_line,
            'signal_line': signal_line,
            'histogram': histogram,
            'trend': 'bullish' if histogram > 0 else 'bearish',
            'crossover': 'golden' if histogram > 0 and len(macd_values) > 1 and macd_values[-2] < 0 else
                        'death' if histogram < 0 and len(macd_values) > 1 and macd_values[-2] > 0 else None
        }
    
    @staticmethod
    def calculate_bollinger_bands(prices: List[float], period: int = 20, std_dev: float = 2.0) -> Optional[Dict]:
        """
        Calculate Bollinger Bands.
        
        Returns:
            - middle: SMA (middle band)
            - upper: Middle + (std_dev * standard deviation)
            - lower: Middle - (std_dev * standard deviation)
            - bandwidth: (Upper - Lower) / Middle (volatility measure)
            - position: Where current price is (-1 to 1, negative = near lower band)
        """
        if len(prices) < period:
            return None
        
        recent_prices = prices[-period:]
        middle = sum(recent_prices) / period
        
        # Calculate standard deviation
        variance = sum((p - middle) ** 2 for p in recent_prices) / period
        std = variance ** 0.5
        
        upper = middle + (std_dev * std)
        lower = middle - (std_dev * std)
        
        current_price = prices[-1]
        bandwidth = (upper - lower) / middle if middle > 0 else 0
        
        # Position: -1 at lower band, 0 at middle, +1 at upper band
        band_range = upper - lower
        if band_range > 0:
            position = ((current_price - middle) / (band_range / 2))
            position = max(-1, min(1, position))  # Clamp to [-1, 1]
        else:
            position = 0
        
        return {
            'middle': middle,
            'upper': upper,
            'lower': lower,
            'bandwidth': bandwidth,
            'position': position,
            'is_oversold': current_price < lower,
            'is_overbought': current_price > upper,
            'near_lower': position < -0.8,
            'near_upper': position > 0.8
        }
    
    @staticmethod
    def calculate_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
        """
        Calculate Average True Range (ATR) for volatility.
        
        Used for:
        - Position sizing (larger ATR = smaller position)
        - Stop loss placement (stop at 2-3x ATR from entry)
        """
        if len(highs) < period + 1 or len(lows) < period + 1 or len(closes) < period + 1:
            return None
        
        true_ranges = []
        for i in range(1, len(closes)):
            high = highs[i]
            low = lows[i]
            prev_close = closes[i-1]
            
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )
            true_ranges.append(tr)
        
        if len(true_ranges) < period:
            return None
        
        return sum(true_ranges[-period:]) / period
    
    @staticmethod
    def calculate_volatility(prices: List[float], period: int = 20) -> Optional[float]:
        """
        Calculate price volatility as percentage.
        
        Returns standard deviation of returns as a percentage.
        """
        if len(prices) < period + 1:
            return None
        
        returns = []
        for i in range(1, len(prices)):
            if prices[i-1] > 0:
                ret = (prices[i] - prices[i-1]) / prices[i-1]
                returns.append(ret)
        
        if len(returns) < period:
            return None
        
        recent_returns = returns[-period:]
        mean_return = sum(recent_returns) / len(recent_returns)
        variance = sum((r - mean_return) ** 2 for r in recent_returns) / len(recent_returns)
        
        return (variance ** 0.5) * 100  # As percentage
    
    @staticmethod
    def detect_ma_crossover(prices: List[float], fast_period: int = 10, slow_period: int = 20) -> Optional[Dict]:
        """
        Detect Moving Average crossovers.
        
        Golden Cross: Fast MA crosses above Slow MA (bullish)
        Death Cross: Fast MA crosses below Slow MA (bearish)
        """
        if len(prices) < slow_period + 2:
            return None
        
        # Current MAs
        fast_ma = TechnicalAnalysis.calculate_sma(prices, fast_period)
        slow_ma = TechnicalAnalysis.calculate_sma(prices, slow_period)
        
        # Previous MAs
        prev_fast = TechnicalAnalysis.calculate_sma(prices[:-1], fast_period)
        prev_slow = TechnicalAnalysis.calculate_sma(prices[:-1], slow_period)
        
        if None in [fast_ma, slow_ma, prev_fast, prev_slow]:
            return None
        
        # Detect crossover
        current_above = fast_ma > slow_ma
        prev_above = prev_fast > prev_slow
        
        crossover = None
        if current_above and not prev_above:
            crossover = 'golden'  # Bullish
        elif not current_above and prev_above:
            crossover = 'death'  # Bearish
        
        return {
            'fast_ma': fast_ma,
            'slow_ma': slow_ma,
            'spread': fast_ma - slow_ma,
            'spread_pct': ((fast_ma - slow_ma) / slow_ma * 100) if slow_ma > 0 else 0,
            'trend': 'bullish' if fast_ma > slow_ma else 'bearish',
            'crossover': crossover
        }
    
    @staticmethod
    def calculate_volume_ratio(volumes: List[float], period: int = 20) -> Optional[float]:
        """
        Calculate current volume relative to average.
        
        Returns ratio: 1.0 = average, 2.0 = 2x average, etc.
        High volume confirms price movements.
        """
        if len(volumes) < period + 1:
            return None
        
        avg_volume = sum(volumes[-period-1:-1]) / period
        current_volume = volumes[-1]
        
        if avg_volume <= 0:
            return None
        
        return current_volume / avg_volume
    
    @staticmethod
    def generate_signal(prices: List[float], volumes: List[float] = None) -> Dict:
        """
        Generate comprehensive trading signal from multiple indicators.
        
        Combines RSI, MACD, Bollinger Bands, and MA crossovers into a single signal.
        
        Returns:
            - signal: 'BUY', 'SELL', or 'HOLD'
            - strength: 0-100 (confidence in signal)
            - indicators: Individual indicator values
            - reasons: List of reasons for the signal
        """
        if len(prices) < 30:
            return {
                'signal': 'HOLD',
                'strength': 0,
                'indicators': {},
                'reasons': ['Insufficient price history']
            }
        
        indicators = {}
        buy_signals = 0
        sell_signals = 0
        reasons = []
        
        # RSI
        rsi = TechnicalAnalysis.calculate_rsi(prices)
        if rsi is not None:
            indicators['rsi'] = rsi
            if rsi < 30:
                buy_signals += 2
                reasons.append(f'RSI oversold ({rsi:.1f})')
            elif rsi < 40:
                buy_signals += 1
                reasons.append(f'RSI approaching oversold ({rsi:.1f})')
            elif rsi > 70:
                sell_signals += 2
                reasons.append(f'RSI overbought ({rsi:.1f})')
            elif rsi > 60:
                sell_signals += 1
                reasons.append(f'RSI approaching overbought ({rsi:.1f})')
        
        # MACD
        macd = TechnicalAnalysis.calculate_macd(prices)
        if macd:
            indicators['macd'] = macd
            if macd['trend'] == 'bullish':
                buy_signals += 1
                if macd['crossover'] == 'golden':
                    buy_signals += 2
                    reasons.append('MACD golden cross')
            else:
                sell_signals += 1
                if macd['crossover'] == 'death':
                    sell_signals += 2
                    reasons.append('MACD death cross')
        
        # Bollinger Bands
        bb = TechnicalAnalysis.calculate_bollinger_bands(prices)
        if bb:
            indicators['bollinger'] = bb
            if bb['is_oversold']:
                buy_signals += 2
                reasons.append('Price below Bollinger lower band')
            elif bb['near_lower']:
                buy_signals += 1
                reasons.append('Price near Bollinger lower band')
            elif bb['is_overbought']:
                sell_signals += 2
                reasons.append('Price above Bollinger upper band')
            elif bb['near_upper']:
                sell_signals += 1
                reasons.append('Price near Bollinger upper band')
        
        # MA Crossover
        ma_cross = TechnicalAnalysis.detect_ma_crossover(prices)
        if ma_cross:
            indicators['ma_crossover'] = ma_cross
            if ma_cross['crossover'] == 'golden':
                buy_signals += 2
                reasons.append('MA golden cross (bullish)')
            elif ma_cross['crossover'] == 'death':
                sell_signals += 2
                reasons.append('MA death cross (bearish)')
            elif ma_cross['trend'] == 'bullish' and ma_cross['spread_pct'] > 2:
                buy_signals += 1
                reasons.append(f'Strong bullish trend (MA spread +{ma_cross["spread_pct"]:.1f}%)')
        
        # Volume confirmation
        if volumes and len(volumes) >= 20:
            vol_ratio = TechnicalAnalysis.calculate_volume_ratio(volumes)
            if vol_ratio:
                indicators['volume_ratio'] = vol_ratio
                if vol_ratio > 1.5:
                    # High volume confirms the primary signal
                    if buy_signals > sell_signals:
                        buy_signals += 1
                        reasons.append(f'High volume confirms ({vol_ratio:.1f}x avg)')
                    elif sell_signals > buy_signals:
                        sell_signals += 1
                        reasons.append(f'High volume confirms ({vol_ratio:.1f}x avg)')
        
        # Volatility
        volatility = TechnicalAnalysis.calculate_volatility(prices)
        if volatility:
            indicators['volatility'] = volatility
        
        # Determine final signal
        total_signals = buy_signals + sell_signals
        if total_signals == 0:
            signal = 'HOLD'
            strength = 0
        elif buy_signals > sell_signals:
            signal = 'BUY'
            strength = min(100, (buy_signals / max(1, sell_signals)) * 20 + buy_signals * 10)
        elif sell_signals > buy_signals:
            signal = 'SELL'
            strength = min(100, (sell_signals / max(1, buy_signals)) * 20 + sell_signals * 10)
        else:
            signal = 'HOLD'
            strength = 20  # Conflicting signals
            reasons.append('Conflicting indicators')
        
        return {
            'signal': signal,
            'strength': min(100, strength),
            'indicators': indicators,
            'reasons': reasons
        }


def calculate_volatility_adjusted_size(base_size: float, volatility: float, 
                                        target_volatility: float = 2.0) -> float:
    """
    Calculate position size adjusted for volatility.
    
    Higher volatility = smaller position size (to maintain consistent risk).
    Based on volatility parity principles from the research.
    
    Args:
        base_size: Base position size in ALGO
        volatility: Current asset volatility (%)
        target_volatility: Target volatility level (%)
    
    Returns:
        Adjusted position size
    """
    if volatility <= 0:
        return base_size
    
    # Scale inversely with volatility
    adjustment = target_volatility / volatility
    adjustment = max(0.25, min(2.0, adjustment))  # Clamp between 0.25x and 2x
    
    return base_size * adjustment


def calculate_kelly_position_size(win_rate: float, avg_win: float, avg_loss: float,
                                   max_fraction: float = 0.25) -> float:
    """
    Calculate optimal position size using Kelly Criterion.
    
    Kelly % = W - [(1-W) / R]
    Where W = win rate, R = win/loss ratio
    
    Returns fraction of capital to risk (capped at max_fraction for safety).
    """
    if avg_loss <= 0 or win_rate <= 0 or win_rate >= 1:
        return max_fraction / 2  # Default to half max
    
    win_loss_ratio = avg_win / avg_loss
    kelly = win_rate - ((1 - win_rate) / win_loss_ratio)
    
    # Kelly is aggressive - use half-Kelly for safety
    half_kelly = kelly / 2
    
    # Clamp to reasonable range
    return max(0.05, min(max_fraction, half_kelly))


def score_opportunity_with_ta(opportunity: Dict, price_history: List[float], 
                               volume_history: List[float] = None) -> Dict:
    """
    Score a trading opportunity using technical analysis.
    
    Enhances basic scoring with TA signals for better entry timing.
    """
    # Get TA signal
    ta_signal = TechnicalAnalysis.generate_signal(price_history, volume_history)
    
    # Start with base score
    base_score = opportunity.get('score', 50)
    ta_adjustment = 0
    ta_reasons = []
    
    opp_signal = opportunity.get('signal', 'BUY')
    
    # Adjust score based on TA alignment
    if ta_signal['signal'] == opp_signal:
        # TA confirms the opportunity
        ta_adjustment = ta_signal['strength'] * 0.3  # Up to +30 points
        ta_reasons.append(f"TA confirms {opp_signal} (strength: {ta_signal['strength']:.0f})")
    elif ta_signal['signal'] == 'HOLD':
        # TA is neutral - slight penalty
        ta_adjustment = -10
        ta_reasons.append("TA signals caution (neutral)")
    else:
        # TA contradicts - significant penalty
        ta_adjustment = -30
        ta_reasons.append(f"TA contradicts: suggests {ta_signal['signal']}")
    
    # Additional adjustments from specific indicators
    indicators = ta_signal.get('indicators', {})
    
    # RSI-based adjustments
    rsi = indicators.get('rsi')
    if rsi is not None:
        if opp_signal == 'BUY':
            if rsi < 25:  # Deeply oversold
                ta_adjustment += 15
                ta_reasons.append(f"Deeply oversold (RSI {rsi:.0f})")
            elif rsi > 65:  # Not a good time to buy
                ta_adjustment -= 15
                ta_reasons.append(f"RSI too high for buy ({rsi:.0f})")
        elif opp_signal == 'SELL':
            if rsi > 75:  # Deeply overbought
                ta_adjustment += 15
                ta_reasons.append(f"Deeply overbought (RSI {rsi:.0f})")
    
    # Volatility adjustment
    volatility = indicators.get('volatility')
    if volatility is not None:
        if volatility > 10:  # High volatility
            ta_adjustment -= 10
            ta_reasons.append(f"High volatility ({volatility:.1f}%)")
        elif volatility < 2:  # Low volatility (potential breakout)
            ta_adjustment += 5
            ta_reasons.append("Low volatility (potential breakout)")
    
    # Volume confirmation
    vol_ratio = indicators.get('volume_ratio')
    if vol_ratio is not None:
        if vol_ratio > 2.0:
            ta_adjustment += 10
            ta_reasons.append(f"Strong volume ({vol_ratio:.1f}x)")
        elif vol_ratio < 0.5:
            ta_adjustment -= 10
            ta_reasons.append(f"Weak volume ({vol_ratio:.1f}x)")
    
    # Calculate final score
    final_score = base_score + ta_adjustment
    final_score = max(0, min(100, final_score))
    
    return {
        'score': final_score,
        'base_score': base_score,
        'ta_adjustment': ta_adjustment,
        'ta_signal': ta_signal,
        'ta_reasons': ta_reasons,
        'recommended_action': ta_signal['signal'] if ta_signal['strength'] > 50 else 'HOLD'
    }


# ============================================================================
# MULTI-LLM HELPERS
# ============================================================================

def get_llm_for_task(config: TradingConfig, task: str) -> Optional[str]:
    """
    Get the appropriate LLM model for a specific task.
    
    Tasks:
    - "market": Market analysis (broad context, understanding trends)
    - "trade": Trade decisions (speed, quick confirmation)
    - "strategy": Strategy/preset suggestions (reasoning, planning)
    - "risk": Risk assessment, rug detection (caution, safety)
    
    Returns the configured model for the task, or falls back to:
    1. Other configured multi-LLM models
    2. Legacy single llm_model setting
    3. None if no LLM configured
    """
    if config.multi_llm_enabled:
        task_models = {
            "market": config.llm_market_analysis,
            "trade": config.llm_trade_decisions,
            "strategy": config.llm_strategy_reasoning,
            "risk": config.llm_risk_assessment,
        }
        
        # Try the specific task model
        model = task_models.get(task)
        if model:
            return model
        
        # Fallback to any configured model
        for m in task_models.values():
            if m:
                return m
    
    # Fallback to legacy single model
    if config.llm_model:
        return config.llm_model
    
    return None


def configure_multi_llm() -> Dict[str, str]:
    """Interactive configuration for multi-LLM setup."""
    print(f"\n{Fore.CYAN}{'='*60}")
    print("  MULTI-LLM CONFIGURATION")
    print(f"{'='*60}{Style.RESET_ALL}\n")
    
    print(f"{Fore.WHITE}Configure different LLM models for different tasks.")
    print(f"Leave blank to use a single model for all tasks.{Style.RESET_ALL}\n")
    
    print(f"{Fore.GREEN}Available Task Categories:{Style.RESET_ALL}")
    print(f"  {Fore.CYAN}1. Market Analysis{Style.RESET_ALL} - Analyzing overall market conditions")
    print(f"     Best with: Large context models (llama3.2, qwen2)")
    print(f"  {Fore.CYAN}2. Trade Decisions{Style.RESET_ALL} - Confirming individual trades")
    print(f"     Best with: Fast models (phi3, gemma2)")
    print(f"  {Fore.CYAN}3. Strategy/Reasoning{Style.RESET_ALL} - Strategy suggestions, re-evaluation")
    print(f"     Best with: Reasoning models (deepseek-r1, lobe3)")
    print(f"  {Fore.CYAN}4. Risk Assessment{Style.RESET_ALL} - Rug detection, risk analysis")
    print(f"     Best with: Cautious/safety models\n")
    
    models = get_available_ollama_models()
    
    if not models:
        print(f"{Fore.YELLOW}No Ollama models found. Using manual entry.{Style.RESET_ALL}")
        models = []
    else:
        print(f"{Fore.GREEN}Available Models:{Style.RESET_ALL}")
        for i, m in enumerate(models, 1):
            print(f"  {i}. {m['name']} ({m['params']}) - {m['size']}")
        print(f"  0. Enter manually")
        print()
    
    def select_model(task_name: str) -> str:
        """Helper to select a model for a task."""
        choice = input(f"{Fore.YELLOW}{task_name} model (number or name, Enter to skip): {Style.RESET_ALL}").strip()
        if not choice:
            return ""
        try:
            idx = int(choice)
            if idx == 0:
                return input(f"{Fore.YELLOW}Enter model name: {Style.RESET_ALL}").strip()
            elif 1 <= idx <= len(models):
                return models[idx - 1]["name"]
        except ValueError:
            return choice  # Assume it's a model name
        return ""
    
    config = {
        "market_analysis": select_model("Market Analysis"),
        "trade_decisions": select_model("Trade Decisions"),
        "strategy_reasoning": select_model("Strategy/Reasoning"),
        "risk_assessment": select_model("Risk Assessment"),
    }
    
    # Show summary
    print(f"\n{Fore.GREEN}Multi-LLM Configuration:{Style.RESET_ALL}")
    for task, model in config.items():
        status = model if model else "(using fallback)"
        print(f"  {task.replace('_', ' ').title()}: {status}")
    
    return config


# ============================================================================
# RUG.NINJA FUNCTIONS (Algorand's pump.fun equivalent)
# ============================================================================

def scan_rug_ninja_tokens(min_bond_progress: float = 0.0, 
                          max_bond_progress: float = 1.0,
                          min_volume: float = 100.0,
                          max_age_minutes: int = 60,
                          limit: int = 50) -> List[Dict]:
    """
    Scan for rug.ninja tokens using multiple API sources.
    
    Rug.ninja tokens have a bonding curve that starts at 0% and reaches 100%
    when enough liquidity is added. Once bonded (100%), the token "graduates"
    to a regular DEX pool.
    
    This function tries multiple sources:
    1. Rug.ninja direct API
    2. Vestige API endpoints
    3. Algorand indexer (fallback for graduated tokens)
    
    Args:
        min_bond_progress: Minimum bonding progress (0.0 = just created)
        max_bond_progress: Maximum bonding progress (1.0 = fully bonded/graduated)
        min_volume: Minimum 24h volume in ALGO
        max_age_minutes: Maximum age for the token (for sniping new mints)
        limit: Maximum tokens to return
        
    Returns:
        List of rug.ninja token data
    """
    try:
        # Try multiple API sources in order of preference
        endpoints = [
            # Rug.ninja direct API (if available)
            ("https://api.rug.ninja/tokens", "rug.ninja-direct", {}),
            ("https://rug.ninja/api/tokens", "rug.ninja-api", {}),
            ("https://rug.ninja/api/v1/tokens", "rug.ninja-v1", {}),
            # Vestige APIs
            ("https://free-api.vestige.fi/assets/list", "vestige-free", {"limit": min(limit * 3, 300), "order_by": "created_at", "order_dir": "desc"}),
            ("https://api.vestige.fi/assets/list", "vestige-main", {"limit": min(limit * 3, 300), "order_by": "created_at", "order_dir": "desc"}),
            # Vestige bonding curve specific endpoints
            ("https://free-api.vestige.fi/assets/bonding", "vestige-bonding", {"limit": limit}),
            ("https://api.vestige.fi/assets/bonding", "vestige-bonding-main", {"limit": limit}),
        ]
        
        results = []
        api_worked = False
        api_source = ""
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }
        
        for url, name, params in endpoints:
            try:
                response = requests.get(url, params=params if params else None, headers=headers, timeout=15)
                
                if response.status_code == 200:
                    try:
                        data = response.json()
                        
                        # Handle different response formats
                        if isinstance(data, list):
                            results = data
                        elif isinstance(data, dict):
                            results = data.get("results", data.get("tokens", data.get("data", data.get("assets", []))))
                            if not isinstance(results, list):
                                results = [data] if "asset_id" in data or "id" in data else []
                        
                        if results:
                            log_info(f"🥷 {name} returned {len(results)} assets")
                            api_worked = True
                            api_source = name
                            break
                    except ValueError:
                        pass  # JSON parse failed, try next
                elif response.status_code == 403:
                    log_info(f"🥷 {name}: Cloudflare blocked, trying next...")
                elif response.status_code == 404:
                    pass  # Endpoint doesn't exist
                elif response.status_code == 503:
                    pass  # Service unavailable
            except requests.exceptions.Timeout:
                log_info(f"🥷 {name}: Timeout, trying next...")
            except requests.exceptions.ConnectionError:
                pass  # Network error, try next
            except Exception as e:
                pass  # Unknown error, try next
        
        # If no API worked, try to query recent ASAs from Algorand indexer
        if not api_worked:
            log_info("🥷 API endpoints not responding, trying Algorand indexer...")
            results = _scan_rug_ninja_via_indexer(limit, max_age_minutes)
            if results:
                api_worked = True
                api_source = "algorand-indexer"
                log_info(f"🥷 Indexer found {len(results)} potential rug.ninja tokens")
        
        if not api_worked:
            log_warning("🥷 All rug.ninja data sources unavailable")
            log_info("🥷 Browse rug.ninja tokens at: https://rug.ninja/")
            log_info("🥷 Track bonded tokens on Vestige: https://vestige.fi/")
            return []
        
        rug_ninja_tokens = []
        now = datetime.now()
        tokens_checked = 0
        
        for asset in results:
            tokens_checked += 1
            
            # Check for rug.ninja indicators:
            # bond_progress field indicates a rug.ninja bonding curve token
            bond_progress = asset.get("bond_progress", asset.get("bonding_progress"))
            is_bonding = asset.get("is_bonding", False)
            bonding_curve = asset.get("bonding_curve")
            is_rug_ninja = asset.get("is_rug_ninja", False)
            
            # For graduated mode, accept tokens that have graduated (bond_progress = 1.0 or is_bonded = True)
            is_graduated = asset.get("is_bonded", False) or asset.get("graduated", False)
            
            # If we're looking for graduated tokens (max_bond_progress = 1.0), include graduated ones
            if max_bond_progress >= 1.0 and is_graduated:
                bond_progress = 1.0
            
            # Only process if it has bonding curve indicators OR from rug.ninja source
            if bond_progress is None and not is_bonding and not bonding_curve and not is_rug_ninja and not is_graduated:
                # If from indexer fallback, we include all tokens as potential candidates
                if api_source != "algorand-indexer":
                    continue
                else:
                    bond_progress = 0.5  # Assume mid-progress for indexer results
            
            # Use bond_progress if available
            if bond_progress is not None:
                if bond_progress < min_bond_progress or bond_progress > max_bond_progress:
                    continue
            elif is_bonding or bonding_curve:
                bond_progress = asset.get("bonding_progress", 0.5)
            
            # Check age if filtering
            if max_age_minutes > 0:
                created_at = asset.get("created_at", asset.get("created_round", 0))
                if created_at:
                    try:
                        if isinstance(created_at, str):
                            token_time = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                            token_age = (now - token_time.replace(tzinfo=None)).total_seconds() / 60
                        elif isinstance(created_at, (int, float)) and created_at > 1000000000:
                            # Unix timestamp
                            token_age = (now - datetime.fromtimestamp(created_at)).total_seconds() / 60
                        else:
                            token_age = 0  # Can't determine age
                        if token_age > max_age_minutes and max_age_minutes > 0:
                            continue
                    except:
                        pass
            
            # Get volume and skip if too low
            volume_24h = asset.get("volume1d", asset.get("volume_24h", asset.get("volume", 0)))
            if volume_24h < min_volume and min_volume > 0:
                continue
            
            rug_ninja_tokens.append({
                "asset_id": asset.get("id", asset.get("asset_id", asset.get("index"))),
                "name": asset.get("name", "Unknown"),
                "ticker": asset.get("ticker", asset.get("unit_name", asset.get("unit-name", "???"))),
                "price": asset.get("price", 0),
                "bond_progress": bond_progress if bond_progress is not None else 0.5,
                "bond_progress_pct": (bond_progress * 100) if bond_progress is not None else 50,
                "is_bonded": is_graduated or (bond_progress is not None and bond_progress >= 1.0),
                "is_graduated": is_graduated or (bond_progress is not None and bond_progress >= 1.0),
                "graduated": is_graduated or (bond_progress is not None and bond_progress >= 1.0),
                "volume_24h": volume_24h,
                "tvl": asset.get("tvl", 0),
                "market_cap": asset.get("market_cap", 0),
                "price_change_1h": asset.get("price_change_1h", asset.get("change1h", 0)),
                "price_change_24h": asset.get("price_change_24h", asset.get("change24h", 0)),
                "swaps_24h": asset.get("swaps_24h", asset.get("swaps1d", 0)),
                "holders": asset.get("holders", asset.get("holder_count", 0)),
                "created_at": asset.get("created_at", ""),
                "source": api_source,
            })
            
            if len(rug_ninja_tokens) >= limit:
                break
        
        log_info(f"🥷 Found {len(rug_ninja_tokens)} rug.ninja tokens matching criteria (from {api_source})")
        return rug_ninja_tokens
        
    except Exception as e:
        log_error(f"🥷 Rug.ninja scan failed: {e}")
        import traceback
        traceback.print_exc()
        return []


def _scan_rug_ninja_via_indexer(limit: int = 50, max_age_minutes: int = 60) -> List[Dict]:
    """
    Fallback: Scan for recent ASAs via Algorand indexer that might be rug.ninja tokens.
    
    This queries the rug.ninja app (ID: 2020762574) to find tokens that have
    interacted with the bonding curve contract.
    """
    try:
        # Rug ninja app ID from garbage-cat source
        RUG_NINJA_APP_ID = 2020762574
        
        # Try to get recent transactions involving the rug.ninja app
        indexer_urls = [
            "https://mainnet-idx.algonode.cloud",
            "https://mainnet-idx.4160.nodely.io",
        ]
        
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        }
        
        for indexer_url in indexer_urls:
            try:
                # Query for recent application calls to rug.ninja
                url = f"{indexer_url}/v2/transactions"
                params = {
                    "application-id": RUG_NINJA_APP_ID,
                    "limit": min(limit * 5, 500),
                }
                
                response = requests.get(url, params=params, headers=headers, timeout=15)
                
                if response.status_code == 200:
                    data = response.json()
                    transactions = data.get("transactions", [])
                    
                    if not transactions:
                        continue
                    
                    # Extract unique asset IDs from these transactions
                    asset_ids = set()
                    for tx in transactions:
                        # Look for asset transfers or inner transactions
                        if "asset-transfer-transaction" in tx:
                            asset_id = tx["asset-transfer-transaction"].get("asset-id")
                            if asset_id:
                                asset_ids.add(asset_id)
                        
                        # Check inner transactions
                        inner_txns = tx.get("inner-txns", [])
                        for inner in inner_txns:
                            if "asset-transfer-transaction" in inner:
                                asset_id = inner["asset-transfer-transaction"].get("asset-id")
                                if asset_id:
                                    asset_ids.add(asset_id)
                            if "asset-config-transaction" in inner:
                                asset_id = inner.get("created-asset-index")
                                if asset_id:
                                    asset_ids.add(asset_id)
                    
                    if not asset_ids:
                        continue
                    
                    log_info(f"🥷 Indexer found {len(asset_ids)} assets from rug.ninja app")
                    
                    # Get asset details
                    results = []
                    for asset_id in list(asset_ids)[:limit]:
                        try:
                            asset_url = f"{indexer_url}/v2/assets/{asset_id}"
                            asset_resp = requests.get(asset_url, headers=headers, timeout=5)
                            if asset_resp.status_code == 200:
                                asset_data = asset_resp.json().get("asset", {})
                                params = asset_data.get("params", {})
                                results.append({
                                    "id": asset_id,
                                    "asset_id": asset_id,
                                    "name": params.get("name", "Unknown"),
                                    "unit_name": params.get("unit-name", "???"),
                                    "ticker": params.get("unit-name", "???"),
                                    "is_rug_ninja": True,
                                    "bond_progress": 0.5,  # Unknown, assume mid
                                    "created_at": asset_data.get("created-at-round", 0),
                                })
                        except:
                            pass
                    
                    return results
                    
            except Exception as e:
                continue
        
        return []
        
    except Exception as e:
        return []


def analyze_rug_ninja_opportunity(token: Dict, config: TradingConfig) -> Dict:
    """
    Analyze a rug.ninja token for trading opportunity.
    
    Returns analysis with score and recommendation.
    """
    score = 50.0  # Base score
    reasons = []
    risks = []
    
    bond_progress = token.get("bond_progress", 0)
    volume = token.get("volume_24h", 0)
    price_change_1h = token.get("price_change_1h", 0)
    swaps = token.get("swaps_24h", 0)
    
    # Bonding progress analysis
    if bond_progress < 0.3:
        score += 20  # Very early, high potential
        reasons.append("Early bonding stage (high potential)")
    elif bond_progress < 0.6:
        score += 10
        reasons.append("Mid bonding stage")
    elif bond_progress < 0.9:
        score += 5
        reasons.append("Late bonding stage (close to graduation)")
    else:
        score -= 10  # Already bonded, reduced upside
        reasons.append("Already graduated to DEX")
    
    # Volume analysis
    if volume > 1000:
        score += 15
        reasons.append(f"High volume ({volume:.0f} ALGO)")
    elif volume > 100:
        score += 5
        reasons.append(f"Decent volume ({volume:.0f} ALGO)")
    else:
        score -= 10
        risks.append("Low volume")
    
    # Activity analysis
    if swaps > 50:
        score += 10
        reasons.append(f"High activity ({swaps} swaps)")
    elif swaps < 5:
        score -= 10
        risks.append("Very low activity")
    
    # Price momentum
    if price_change_1h > 10:
        score += 10
        reasons.append(f"Strong momentum (+{price_change_1h:.1f}% 1h)")
    elif price_change_1h < -20:
        score -= 15
        risks.append(f"Dumping ({price_change_1h:.1f}% 1h)")
    
    # Risk factors
    if bond_progress < 0.2 and swaps < 10:
        risks.append("New token with low activity - high rug risk")
        score -= 20
    
    # Cap the score
    score = max(0, min(100, score))
    
    return {
        "asset_id": token["asset_id"],
        "asset_name": token["name"],
        "ticker": token["ticker"],
        "score": score,
        "signal": "BUY" if score >= 60 else ("HOLD" if score >= 40 else "AVOID"),
        "bond_progress": bond_progress,
        "reasons": reasons,
        "risks": risks,
        "current_price": token["price"],
        "is_rug_ninja": True,
    }


def ai_assess_rug_risk(token: Dict, llm_model: str) -> Optional[Dict]:
    """
    Use AI to assess rug pull risk for a token.
    
    This is especially useful for rug.ninja tokens which have higher
    inherent risk due to their pump.fun-like nature.
    """
    try:
        import ollama
        
        prompt = f"""Analyze this Algorand token for rug pull risk:

TOKEN INFO:
- Name: {token.get('name', 'Unknown')}
- Ticker: {token.get('ticker', '???')}
- Bond Progress: {token.get('bond_progress', 0) * 100:.1f}%
- 24h Volume: {token.get('volume_24h', 0):.2f} ALGO
- Price Change 1h: {token.get('price_change_1h', 0):.1f}%
- 24h Swaps: {token.get('swaps_24h', 0)}
- Market Cap: {token.get('market_cap', 0):.2f} ALGO

This is a rug.ninja token (Algorand's pump.fun equivalent).

Assess the risk level and provide a recommendation.

Reply with JSON only:
{{
    "risk_level": "low" | "medium" | "high" | "extreme",
    "confidence": 0.0-1.0,
    "red_flags": ["list of concerns"],
    "positive_signs": ["list of good indicators"],
    "recommendation": "buy" | "avoid" | "small_position",
    "reasoning": "Brief explanation"
}}

JSON:"""

        response = ollama.chat(
            model=llm_model,
            messages=[
                {"role": "system", "content": "You are a crypto risk analyst. Be cautious - most meme coins fail. Reply with JSON only."},
                {"role": "user", "content": prompt}
            ],
            options={"temperature": 0.2, "num_predict": 1500}
        )
        
        content = response["message"]["content"]
        return parse_llm_json(content)
        
    except Exception as e:
        log_warning(f"AI rug risk assessment failed: {e}")
        return None


# ============================================================================
# RUG.NINJA CONSTANTS AND MINT SNIPER (Based on garbage-cat)
# Source: https://github.com/garbagecatio/garbage-cat
# ============================================================================

# Rug Ninja MainNet Application ID (from garbage-cat main.go)
RUG_NINJA_APP_ID = 2020762574

# Rug Ninja Application Address (receives payments)
# Computed at runtime using compute_app_address()
RUG_NINJA_APP_ADDRESS = None  # Will be computed

# Method selectors (base64-encoded ABI method selectors from garbage-cat)
# These identify transaction types in rug.ninja blocks
# NOTE: These values should be verified against the actual garbage-cat source
# The selectors are ARC-4 method selectors for the Rug Ninja contract
# Located in garbage-cat main.go as RugNinjaTokenMint, RugNinjaBuy, RugNinjaSell
RUG_NINJA_TOKEN_MINT = "kUF1qQ=="  # Token mint event - triggers snipe
RUG_NINJA_BUY = "dHJs3A=="  # Buy event (buyCoin method)
RUG_NINJA_SELL = "8NYQHg=="  # Sell event (ignored by sniper)

# Algorand node endpoint for block streaming
ALGORAND_MAINNET_NODE = "https://mainnet-api.algonode.cloud"
ALGORAND_MAINNET_INDEXER = "https://mainnet-idx.algonode.cloud"

# Default purchase amount in microAlgos (1 ALGO = 1,000,000 microAlgos)
DEFAULT_SNIPE_AMOUNT_MICROALGOS = 1_000_000  # 1 ALGO


def compute_app_address(app_id: int) -> str:
    """
    Compute the Algorand application address from app ID.
    
    The app address is derived by hashing "appID" + app_id bytes.
    """
    try:
        import hashlib
        from algosdk import encoding
        
        # Compute app address: SHA512/256("appID" || app_id_bytes)
        prefix = b"appID"
        app_id_bytes = app_id.to_bytes(8, byteorder='big')
        data = prefix + app_id_bytes
        
        h = hashlib.new('sha512_256')
        h.update(data)
        address_bytes = h.digest()
        
        # Encode as Algorand address
        return encoding.encode_address(address_bytes)
    except Exception as e:
        log_warning(f"Failed to compute app address: {e}")
        return ""


def create_box_name(address: str, asset_id: int) -> bytes:
    """
    Create a box name from address and asset ID (matching garbage-cat createBoxName).
    
    Box name format: address bytes (32) + asset ID bytes (8)
    """
    try:
        from algosdk import encoding
        
        # Decode address to bytes
        addr_bytes = encoding.decode_address(address)
        
        # Asset ID as 8 bytes big-endian
        asset_bytes = asset_id.to_bytes(8, byteorder='big')
        
        return addr_bytes + asset_bytes
    except Exception as e:
        log_warning(f"Failed to create box name: {e}")
        return b""


def create_tbox_name(address: str) -> bytes:
    """
    Create a t-box name from address (matching garbage-cat createTBoxName).
    
    T-box holds metadata about the buyer.
    """
    try:
        from algosdk import encoding
        
        # Just the address bytes with 't' prefix
        return b't' + encoding.decode_address(address)
    except Exception as e:
        log_warning(f"Failed to create t-box name: {e}")
        return b""


class RugNinjaMintSniper:
    """
    Real-time mint sniper for rug.ninja tokens (based on garbage-cat).
    
    Streams Algorand blocks, detects new token mints on rug.ninja,
    and instantly purchases them using atomic transactions.
    
    WARNING: This is EXTREMELY risky! Most rug.ninja tokens go to zero.
    Only use with funds you can afford to lose completely.
    """
    
    def __init__(self, 
                 private_key: str,
                 purchase_amount_algo: float = 1.0,
                 algod_url: str = ALGORAND_MAINNET_NODE):
        """
        Initialize the mint sniper.
        
        Args:
            private_key: Algorand account private key (from mnemonic)
            purchase_amount_algo: Amount of ALGO to spend on each mint
            algod_url: Algod node URL for transactions
        """
        self.private_key = private_key
        self.purchase_amount_microalgos = int(purchase_amount_algo * 1_000_000)
        self.algod_url = algod_url
        self.running = False
        self.last_round = 0
        self.mints_detected = 0
        self.purchases_made = 0
        self.purchases_failed = 0
        
        # Compute app address
        self.app_address = compute_app_address(RUG_NINJA_APP_ID)
        
        # Get account address from private key
        try:
            from algosdk import account
            self.address = account.address_from_private_key(private_key)
            log_info(f"🥷 Mint Sniper initialized for wallet: {self.address[:8]}...")
        except Exception as e:
            log_error(f"Failed to get address from private key: {e}")
            self.address = None
    
    def _create_algod_client(self):
        """Create an Algod client."""
        try:
            from algosdk.v2client import algod
            return algod.AlgodClient("", self.algod_url, headers={"User-Agent": "FryNetworks-Bot"})
        except Exception as e:
            log_error(f"Failed to create Algod client: {e}")
            return None
    
    def _is_mint_transaction(self, txn: Dict) -> bool:
        """
        Check if a transaction is a rug.ninja mint event.
        
        Matches garbage-cat's ProcessBlock logic.
        """
        try:
            # Must be an application call
            if txn.get('tx-type') != 'appl' and txn.get('type') != 'appl':
                return False
            
            # Must be to the rug.ninja app
            app_id = txn.get('application-transaction', {}).get('application-id') or txn.get('application-id')
            if app_id != RUG_NINJA_APP_ID:
                return False
            
            # Check the method selector (first app arg)
            app_args = txn.get('application-transaction', {}).get('application-args', []) or txn.get('application-args', [])
            if not app_args:
                return False
            
            # First arg is the method selector (base64 encoded)
            import base64
            try:
                selector = app_args[0]
                # May already be decoded or still base64
                if isinstance(selector, str):
                    # Try to match against known selectors
                    if selector == RUG_NINJA_TOKEN_MINT:
                        return True
                    # Try decoding if it's a different format
                    try:
                        decoded = base64.b64decode(RUG_NINJA_TOKEN_MINT).hex()
                        if base64.b64decode(selector).hex() == decoded:
                            return True
                    except:
                        pass
            except Exception:
                pass
            
            return False
        except Exception as e:
            return False
    
    def _extract_minted_asset(self, txn: Dict) -> Optional[Dict]:
        """
        Extract the newly minted asset info from a mint transaction.
        
        Matches garbage-cat's extraction of LAST_COIN from global delta.
        """
        try:
            # Look for LAST_COIN in global delta
            global_delta = txn.get('global-state-delta', []) or txn.get('eval-delta', {}).get('global-delta', [])
            
            asset_id = None
            for delta in global_delta:
                key = delta.get('key', '')
                # Key might be base64 encoded
                import base64
                try:
                    decoded_key = base64.b64decode(key).decode('utf-8', errors='ignore')
                    if decoded_key == 'LAST_COIN' or 'LAST_COIN' in decoded_key:
                        value = delta.get('value', {})
                        asset_id = value.get('uint')
                        break
                except:
                    if 'LAST_COIN' in key:
                        value = delta.get('value', {})
                        asset_id = value.get('uint')
                        break
            
            if not asset_id:
                return None
            
            # Get asset name from inner transaction
            asset_name = "Unknown"
            inner_txns = txn.get('inner-txns', [])
            if inner_txns:
                first_inner = inner_txns[0]
                asset_params = first_inner.get('txn', {}).get('apar', {}) or first_inner.get('asset-config-transaction', {}).get('params', {})
                asset_name = asset_params.get('an', asset_params.get('name', 'Unknown'))
            
            return {
                'asset_id': asset_id,
                'asset_name': asset_name,
                'txn_id': txn.get('id', 'unknown')
            }
        except Exception as e:
            log_warning(f"Failed to extract minted asset: {e}")
            return None
    
    def _buy_token(self, asset_id: int, asset_name: str) -> bool:
        """
        Purchase a newly minted token using atomic transaction.
        
        Matches garbage-cat's buyToken function:
        1. Create payment to app address
        2. Create method call to buyCoin
        3. Sign and submit atomic group
        """
        try:
            from algosdk import transaction
            from algosdk.v2client import algod
            from algosdk.atomic_transaction_composer import AtomicTransactionComposer, TransactionWithSigner, AccountTransactionSigner
            from algosdk.abi import Contract, Method
            
            client = self._create_algod_client()
            if not client:
                return False
            
            # Get suggested params
            sp = client.suggested_params()
            
            # Create signer
            signer = AccountTransactionSigner(self.private_key)
            
            # 1. Create payment transaction to app address
            payment_txn = transaction.PaymentTxn(
                sender=self.address,
                sp=sp,
                receiver=self.app_address,
                amt=self.purchase_amount_microalgos
            )
            
            # 2. Create the buyCoin method call
            # The method signature is: buyCoin(uint64,uint64)void
            # Arguments: [asset_id, 0]
            atc = AtomicTransactionComposer()
            
            # Add payment as inner transaction
            atc.add_transaction(TransactionWithSigner(payment_txn, signer))
            
            # Create method call transaction
            # Box references needed (matching garbage-cat):
            # 1. Box named with asset (for token data)
            # 2. Box from buyer address + asset ID
            # 3. T-box for buyer metadata
            
            box_name = create_box_name(self.address, asset_id)
            tbox_name = create_tbox_name(self.address)
            
            # Create app call with method
            app_call = transaction.ApplicationCallTxn(
                sender=self.address,
                sp=sp,
                index=RUG_NINJA_APP_ID,
                on_complete=transaction.OnComplete.NoOpOC,
                app_args=[
                    RUG_NINJA_BUY.encode() if isinstance(RUG_NINJA_BUY, str) else RUG_NINJA_BUY,
                    asset_id.to_bytes(8, 'big'),
                    (0).to_bytes(8, 'big')  # Unused second argument
                ],
                foreign_assets=[asset_id],
                boxes=[
                    (RUG_NINJA_APP_ID, asset_name.encode()[:64]),  # Box named with asset
                    (RUG_NINJA_APP_ID, box_name),  # Buyer + asset box
                    (RUG_NINJA_APP_ID, tbox_name),  # T-box for metadata
                ]
            )
            
            atc.add_transaction(TransactionWithSigner(app_call, signer))
            
            # Execute the atomic transaction group
            result = atc.execute(client, 4)
            
            log_success(f"🥷 SNIPED: {asset_name} (ASA {asset_id}) for {self.purchase_amount_microalgos/1_000_000:.2f} ALGO")
            log_info(f"  TX IDs: {[txid for txid in result.tx_ids]}")
            
            self.purchases_made += 1
            return True
            
        except Exception as e:
            log_error(f"🥷 Buy failed for {asset_name} (ASA {asset_id}): {e}")
            self.purchases_failed += 1
            return False
    
    def _process_block(self, block: Dict) -> List[Dict]:
        """
        Process a block and detect any mint events.
        
        Returns list of detected mints.
        """
        mints = []
        
        try:
            # Get transactions from block
            txns = block.get('block', {}).get('txns', []) or block.get('transactions', [])
            
            for txn in txns:
                # Check the main transaction
                if self._is_mint_transaction(txn):
                    asset_info = self._extract_minted_asset(txn)
                    if asset_info:
                        mints.append(asset_info)
                
                # Also check inner transactions
                inner_txns = txn.get('inner-txns', []) or txn.get('dt', {}).get('itx', [])
                for inner in inner_txns:
                    if self._is_mint_transaction(inner):
                        asset_info = self._extract_minted_asset(inner)
                        if asset_info:
                            mints.append(asset_info)
        
        except Exception as e:
            log_warning(f"Error processing block: {e}")
        
        return mints
    
    def stream_and_snipe(self, callback=None):
        """
        Start streaming blocks and sniping new mints.
        
        This is a blocking call that runs until stop() is called.
        
        Args:
            callback: Optional callback function(asset_info) called on each mint
        """
        self.running = True
        log_info(f"🥷 Starting Rug Ninja Mint Sniper...")
        log_info(f"  Purchase amount: {self.purchase_amount_microalgos/1_000_000:.2f} ALGO")
        log_info(f"  Wallet: {self.address}")
        log_warning(f"  ⚠️  WARNING: Most rug.ninja tokens go to zero!")
        
        client = self._create_algod_client()
        if not client:
            log_error("Failed to create Algod client")
            return
        
        # Get current round
        try:
            status = client.status()
            self.last_round = status.get('last-round', 0)
            log_info(f"  Starting at round: {self.last_round}")
        except Exception as e:
            log_error(f"Failed to get node status: {e}")
            return
        
        # Stream blocks
        while self.running:
            try:
                # Wait for next block
                status = client.status_after_block(self.last_round)
                current_round = status.get('last-round', self.last_round + 1)
                
                # Process any missed blocks
                for round_num in range(self.last_round + 1, current_round + 1):
                    try:
                        block = client.block_info(round_num)
                        mints = self._process_block(block)
                        
                        for mint in mints:
                            self.mints_detected += 1
                            log_success(f"🥷 MINT DETECTED: {mint['asset_name']} (ASA {mint['asset_id']})")
                            
                            # Try to buy
                            self._buy_token(mint['asset_id'], mint['asset_name'])
                            
                            # Call callback if provided
                            if callback:
                                callback(mint)
                    
                    except Exception as e:
                        log_warning(f"Error processing round {round_num}: {e}")
                
                self.last_round = current_round
                
            except Exception as e:
                log_warning(f"Stream error: {e}")
                time.sleep(1)  # Brief pause before retry
        
        log_info(f"🥷 Mint Sniper stopped. Stats: {self.mints_detected} detected, {self.purchases_made} bought, {self.purchases_failed} failed")
    
    def stop(self):
        """Stop the sniper."""
        self.running = False
        log_info("🥷 Stopping mint sniper...")
    
    def get_stats(self) -> Dict:
        """Get sniper statistics."""
        return {
            'running': self.running,
            'last_round': self.last_round,
            'mints_detected': self.mints_detected,
            'purchases_made': self.purchases_made,
            'purchases_failed': self.purchases_failed,
            'wallet': self.address
        }

# AlphaArcade Constants
ALPHA_ARCADE_USDC_ASSET_ID = 31566704  # USDC on Algorand
ALPHA_ARCADE_TOKEN_ID = 2726252423  # $ALPHA governance token
ALPHA_ARCADE_API_BASE = "https://partners.alphaarcade.com/api"
ALPHA_ARCADE_BACKEND = "https://alphaarcade.com/api"

# Price/quantity are in micro-units: 550000 = $0.55, 2000000 = 2 shares
ALPHA_ARCADE_MICRO_UNIT = 1_000_000


def scan_alpha_arcade_markets(min_volume: float = 100.0,
                               min_liquidity: float = 500.0,
                               categories: List[str] = None,
                               active_only: bool = True,
                               limit: int = 50,
                               api_key: str = None) -> List[Dict]:
    """
    Scan for AlphaArcade prediction markets using their Partner API.
    
    AlphaArcade is a prediction market on Algorand where users can bet on
    outcomes of events. Each market has YES and NO positions represented
    as ASA tokens. Prices are in micro-units (550000 = 55 cents).
    
    Requires a partner API key from AlphaArcade team.
    Docs: https://alphaarcade.gitbook.io/alphaarcade-docs
    
    Args:
        min_volume: Minimum trading volume (in USDC)
        min_liquidity: Minimum market liquidity
        categories: Filter by categories (sports, crypto, politics, etc.)
        active_only: Only return active/open markets
        limit: Maximum markets to return
        api_key: Partner API key (required)
        
    Returns:
        List of AlphaArcade market data with fields:
        - id, title, yesAssetId, noAssetId, yesProb, noProb
        - volume, spread, midpoint, endTs, feeBasePercent
        - rewardsMinContracts, rewardsSpreadDistance, totalRewards
    """
    try:
        # Build request
        url = f"{ALPHA_ARCADE_API_BASE}/get-markets"
        params = {}
        if active_only:
            params["activeOnly"] = "true"
        
        headers = {
            "Accept": "application/json",
            "User-Agent": "AlgoTradingBot/1.0"
        }
        
        # Add API key if provided
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["X-API-Key"] = api_key
        
        response = requests.get(url, params=params, headers=headers, timeout=30)
        
        if response.status_code == 401:
            log_warning("🎯 AlphaArcade API: Unauthorized - API key required")
            log_info("🎯 Get a partner API key from the AlphaArcade team")
            log_info("🎯 Docs: https://alphaarcade.gitbook.io/alphaarcade-docs")
            return []
        
        if response.status_code == 403:
            log_warning("🎯 AlphaArcade API: Forbidden - invalid API key")
            return []
        
        if response.status_code != 200:
            log_warning(f"🎯 AlphaArcade API returned HTTP {response.status_code}")
            return []
        
        data = response.json()
        markets = data.get("markets", data if isinstance(data, list) else [])
        
        log_info(f"🎯 AlphaArcade API returned {len(markets)} markets")
        
        filtered_markets = []
        
        for market in markets:
            # Extract market data using documented field names
            market_volume = market.get("volume", 0)
            market_spread = market.get("spread", 0)
            
            # Skip if below volume threshold
            if market_volume < min_volume:
                continue
            
            # Parse probabilities (decimal 0-1)
            yes_prob = market.get("yesProb", 0.5)
            no_prob = market.get("noProb", 0.5)
            
            # Convert to price (same as prob for binary markets)
            yes_price = yes_prob
            no_price = no_prob
            
            # Get asset IDs for YES/NO tokens
            yes_asset_id = market.get("yesAssetId")
            no_asset_id = market.get("noAssetId")
            
            # Parse timestamps
            end_ts = market.get("endTs", 0)
            end_date = market.get("endDate", "")
            
            # Get LP reward parameters
            rewards_min_contracts = market.get("rewardsMinContracts", 0)
            rewards_spread_distance = market.get("rewardsSpreadDistance", 0)
            total_rewards = market.get("totalRewards", 0)
            
            filtered_markets.append({
                "market_id": market.get("id"),
                "market_app_id": market.get("marketAppId"),
                "title": market.get("title", "Unknown"),
                "question": market.get("title", "Unknown"),  # Alias
                "description": market.get("rules", ""),
                "categories": market.get("categories", []),
                "yes_price": float(yes_price),
                "no_price": float(no_price),
                "yes_prob": float(yes_prob),
                "no_prob": float(no_prob),
                "yes_asset_id": yes_asset_id,
                "no_asset_id": no_asset_id,
                "volume": float(market_volume),
                "volume_24h": float(market_volume),  # Alias
                "spread": float(market_spread) if market_spread else abs(yes_prob - 0.5) * 2,
                "midpoint": market.get("midpoint", 0.5),
                "end_ts": end_ts,
                "end_date": end_date,
                "resolution_time": end_date,  # Alias
                "fee_base_percent": market.get("feeBasePercent", 7),
                "participant_count": market.get("participantCount", 0),
                "creator": market.get("creator", ""),
                "parent_id": market.get("parentId"),  # For multi-choice markets
                # LP Rewards
                "rewards_min_contracts": rewards_min_contracts,
                "rewards_spread_distance": rewards_spread_distance,
                "total_rewards": total_rewards,
                "status": "active" if not market.get("resolution") else "resolved",
                "is_alpha_arcade": True,
            })
            
            if len(filtered_markets) >= limit:
                break
        
        log_info(f"🎯 Found {len(filtered_markets)} markets meeting criteria")
        return filtered_markets
        
    except requests.exceptions.ConnectionError:
        log_warning("🎯 AlphaArcade API: Connection failed")
        log_info("🎯 Check your internet connection or try again later")
        return []
    except requests.exceptions.Timeout:
        log_warning("🎯 AlphaArcade API: Request timed out")
        return []
    except Exception as e:
        log_warning(f"🎯 AlphaArcade API error: {e}")
        return []


def get_alpha_arcade_market_details(market_id: str, api_key: str = None) -> Optional[Dict]:
    """
    Get detailed information about a specific AlphaArcade market.
    
    Args:
        market_id: The market ID to fetch
        api_key: Partner API key
        
    Returns:
        Market data dict or None
    """
    try:
        url = f"{ALPHA_ARCADE_API_BASE}/get-market"
        params = {"id": market_id}
        
        headers = {
            "Accept": "application/json",
            "User-Agent": "AlgoTradingBot/1.0"
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["X-API-Key"] = api_key
        
        response = requests.get(url, params=params, headers=headers, timeout=30)
        
        if response.status_code != 200:
            return None
        
        market = response.json()
        
        return {
            "market_id": market.get("id"),
            "market_app_id": market.get("marketAppId"),
            "title": market.get("title"),
            "question": market.get("title"),
            "description": market.get("rules", ""),
            "yes_price": float(market.get("yesProb", 0.5)),
            "no_price": float(market.get("noProb", 0.5)),
            "yes_asset_id": market.get("yesAssetId"),
            "no_asset_id": market.get("noAssetId"),
            "volume": float(market.get("volume", 0)),
            "spread": float(market.get("spread", 0)),
            "midpoint": market.get("midpoint", 0.5),
            "end_ts": market.get("endTs"),
            "fee_base_percent": market.get("feeBasePercent", 7),
            "total_rewards": market.get("totalRewards", 0),
            "rewards_min_contracts": market.get("rewardsMinContracts", 0),
            "rewards_spread_distance": market.get("rewardsSpreadDistance", 0),
            "status": "active" if not market.get("resolution") else "resolved",
            "is_alpha_arcade": True,
        }
        
    except Exception as e:
        log_warning(f"🎯 Failed to get AlphaArcade market details: {e}")
        return None


def get_alpha_arcade_wallet_positions(wallet_address: str, api_key: str = None) -> List[Dict]:
    """
    Get AlphaArcade positions for a wallet.
    
    Returns participant data for each market the wallet has traded.
    
    Args:
        wallet_address: Algorand wallet address
        api_key: Partner API key
        
    Returns:
        List of participant/position objects with fields:
        - marketId, marketAppId, yesTokenBalance, noTokenBalance
        - totalInvested, totalReturned, yesCostBasis, noCostBasis
        - totalLpRewards, hasClaimed, amountClaimed
    """
    try:
        url = f"{ALPHA_ARCADE_API_BASE}/get-wallet-participant-data"
        params = {"walletAddress": wallet_address}
        
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["X-API-Key"] = api_key
        
        response = requests.get(url, params=params, headers=headers, timeout=30)
        
        if response.status_code != 200:
            return []
        
        data = response.json()
        return data.get("participants", [])
        
    except Exception as e:
        log_warning(f"🎯 Failed to get wallet positions: {e}")
        return []


def get_alpha_arcade_wallet_orders(wallet_address: str, api_key: str = None) -> List[Dict]:
    """
    Get open AlphaArcade orders for a wallet.
    
    Args:
        wallet_address: Algorand wallet address
        api_key: Partner API key
        
    Returns:
        List of order objects with fields:
        - orderId, marketAppId, marketId, orderPosition (1=YES, 0=NO)
        - orderSide ('buy'/'sell'), orderQuantity, orderQuantityFilled
        - amountCommitted, slippage, status, escrowBalance
    """
    try:
        url = f"{ALPHA_ARCADE_API_BASE}/get-wallet-orders"
        params = {"walletAddress": wallet_address}
        
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["X-API-Key"] = api_key
        
        response = requests.get(url, params=params, headers=headers, timeout=30)
        
        if response.status_code != 200:
            return []
        
        data = response.json()
        return data.get("orders", [])
        
    except Exception as e:
        log_warning(f"🎯 Failed to get wallet orders: {e}")
        return []


def get_alpha_arcade_orderbook(market_app_id: int, api_key: str = None) -> Optional[Dict]:
    """
    Get the order book for an AlphaArcade market.
    
    Args:
        market_app_id: The market's Algorand application ID
        api_key: Partner API key
        
    Returns:
        Order book with bids, asks, lastPrice, spread
    """
    try:
        url = f"{ALPHA_ARCADE_API_BASE}/get-orderbook"
        params = {"marketAppId": market_app_id}
        
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["X-API-Key"] = api_key
        
        response = requests.get(url, params=params, headers=headers, timeout=30)
        
        if response.status_code != 200:
            return None
        
        return response.json()
        
    except Exception as e:
        log_warning(f"🎯 Failed to get order book: {e}")
        return None


def calculate_alpha_arcade_fee(quantity: int, price_micro: int, fee_base: float = 0.07) -> int:
    """
    Calculate the fee for an AlphaArcade trade.
    
    Formula: fee = fee_base × quantity × price × (1 - price)
    
    Args:
        quantity: Number of shares in micro-units (2000000 = 2 shares)
        price_micro: Price in micro-units (550000 = $0.55)
        fee_base: Base fee rate (default 7% = 0.07)
        
    Returns:
        Fee in micro-units
    """
    from decimal import Decimal, ROUND_UP
    
    # Convert to decimals for precision
    q = Decimal(quantity) / Decimal(ALPHA_ARCADE_MICRO_UNIT)  # shares
    p = Decimal(price_micro) / Decimal(ALPHA_ARCADE_MICRO_UNIT)  # price in dollars
    
    # fee = fee_base × quantity × price × (1 - price)
    fee = Decimal(str(fee_base)) * q * p * (Decimal(1) - p)
    
    # Convert back to micro-units and round up
    fee_micro = (fee * Decimal(ALPHA_ARCADE_MICRO_UNIT)).to_integral_value(rounding=ROUND_UP)
    
    return int(fee_micro)


def calculate_alpha_arcade_lp_score(order_price: float, midpoint: float, 
                                    size: int, max_spread_distance: float) -> float:
    """
    Calculate LP reward score for an order.
    
    Formula: score = ((v - s) / v)² × size
    
    Where:
    - v = max spread distance (rewardsSpreadDistance from market)
    - s = distance from midpoint for this order
    - size = order quantity in shares
    
    Args:
        order_price: Order price (0-1)
        midpoint: Market midpoint price
        size: Order size in shares
        max_spread_distance: Maximum allowed distance (rewardsSpreadDistance/1M)
        
    Returns:
        LP score (higher = more rewards)
    """
    if max_spread_distance <= 0:
        return 0
    
    # Calculate distance from midpoint
    distance = abs(order_price - midpoint)
    
    # If outside max spread, no score
    if distance > max_spread_distance:
        return 0
    
    # score = ((v - s) / v)² × size
    ratio = (max_spread_distance - distance) / max_spread_distance
    score = (ratio ** 2) * size
    
    return score


def notify_alpha_arcade_backend(market_id: str = None) -> bool:
    """
    Notify AlphaArcade backend about order changes.
    
    After creating/cancelling orders, you must call this to trigger
    the off-chain matching engine to process the order book.
    
    Args:
        market_id: Optional market ID to requeue
        
    Returns:
        True if notification succeeded
    """
    try:
        url = f"{ALPHA_ARCADE_BACKEND}/requeue-market"
        params = {}
        if market_id:
            params["marketId"] = market_id
        
        response = requests.get(url, params=params, timeout=10)
        return response.status_code == 200
        
    except Exception:
        return False


def analyze_alpha_arcade_opportunity(market: Dict, config: TradingConfig, mode: str = "value") -> Dict:
    """
    Analyze an AlphaArcade market for trading opportunity.
    
    Args:
        market: Market data from scan_alpha_arcade_markets
        config: Trading configuration
        mode: "value" for contrarian betting, "momentum" for trend following
        
    Returns:
        Analysis with score, signal, and recommendation
    """
    score = 50.0  # Base score
    reasons = []
    risks = []
    
    yes_price = market.get("yes_price", 0.5)
    no_price = market.get("no_price", 0.5)
    volume = market.get("volume_24h", 0)
    liquidity = market.get("liquidity", 0)
    total_bets = market.get("total_bets", 0)
    
    # Calculate implied probabilities
    # Price = implied probability in prediction markets
    implied_yes_prob = yes_price
    implied_no_prob = no_price
    
    # Determine recommended position (YES or NO)
    recommended_position = None
    recommended_price = None
    
    if mode == "value":
        # Value betting: Look for mispriced outcomes
        value_threshold = config.alpha_arcade_value_threshold
        
        # Check for value in YES position
        if yes_price < (1 - value_threshold) and yes_price >= config.alpha_arcade_min_price:
            score += 20
            reasons.append(f"YES appears undervalued at {yes_price:.2%}")
            recommended_position = "YES"
            recommended_price = yes_price
        
        # Check for value in NO position
        if no_price < (1 - value_threshold) and no_price >= config.alpha_arcade_min_price:
            if recommended_position is None or no_price < yes_price:
                score += 20
                reasons.append(f"NO appears undervalued at {no_price:.2%}")
                recommended_position = "NO"
                recommended_price = no_price
        
        # Extreme value (very low prices can indicate opportunity)
        if min(yes_price, no_price) < 0.20:
            score += 15
            reasons.append("Extreme value opportunity (price < 20%)")
        
    else:  # momentum mode
        # Momentum betting: Look for trending markets
        momentum_threshold = config.alpha_arcade_momentum_threshold
        
        # Simple momentum: prefer the outcome with more confidence
        if yes_price > 0.5 + momentum_threshold:
            score += 15
            reasons.append(f"YES trending at {yes_price:.2%}")
            recommended_position = "YES"
            recommended_price = yes_price
        elif no_price > 0.5 + momentum_threshold:
            score += 15
            reasons.append(f"NO trending at {no_price:.2%}")
            recommended_position = "NO"
            recommended_price = no_price
    
    # Volume analysis
    if volume > 1000:
        score += 15
        reasons.append(f"High volume ({volume:.0f} ALGO)")
    elif volume > 100:
        score += 5
        reasons.append(f"Decent volume ({volume:.0f} ALGO)")
    else:
        score -= 10
        risks.append("Low volume - may have execution issues")
    
    # Liquidity analysis
    if liquidity > 5000:
        score += 10
        reasons.append(f"High liquidity ({liquidity:.0f} ALGO)")
    elif liquidity < 500:
        score -= 15
        risks.append("Low liquidity - high slippage risk")
    
    # Activity analysis
    if total_bets > 100:
        score += 10
        reasons.append(f"Active market ({total_bets} bets)")
    elif total_bets < 10:
        score -= 10
        risks.append("Low activity market")
    
    # Price boundary checks
    if recommended_price and recommended_price > config.alpha_arcade_max_price:
        score -= 20
        risks.append(f"Price too high ({recommended_price:.2%} > max {config.alpha_arcade_max_price:.2%})")
        recommended_position = None
    
    if recommended_price and recommended_price < config.alpha_arcade_min_price:
        score -= 15
        risks.append(f"Price too low ({recommended_price:.2%} - extremely risky)")
    
    # Check resolution time (avoid betting too close to resolution)
    resolution_time = market.get("resolution_time")
    if resolution_time:
        try:
            if isinstance(resolution_time, str):
                resolve_dt = datetime.fromisoformat(resolution_time.replace("Z", "+00:00"))
            else:
                resolve_dt = datetime.fromtimestamp(resolution_time)
            
            hours_until_resolution = (resolve_dt - datetime.now()).total_seconds() / 3600
            
            if config.alpha_arcade_auto_sell_before_resolution:
                if hours_until_resolution < config.alpha_arcade_hours_before_resolution:
                    score -= 20
                    risks.append(f"Too close to resolution ({hours_until_resolution:.0f}h remaining)")
        except:
            pass
    
    # Cap the score
    score = max(0, min(100, score))
    
    # Determine signal
    if score >= 60 and recommended_position:
        signal = "BUY"
    elif score >= 40:
        signal = "HOLD"
    else:
        signal = "AVOID"
    
    return {
        "market_id": market.get("market_id"),
        "question": market.get("question", "Unknown"),
        "category": market.get("category", "general"),
        "score": score,
        "signal": signal,
        "recommended_position": recommended_position,
        "recommended_price": recommended_price,
        "yes_price": yes_price,
        "no_price": no_price,
        "volume_24h": volume,
        "liquidity": liquidity,
        "reasons": reasons,
        "risks": risks,
        "is_alpha_arcade": True,
    }


def ai_analyze_alpha_arcade_market(market: Dict, llm_model: str) -> Optional[Dict]:
    """
    Use AI to analyze an AlphaArcade prediction market.
    
    The AI evaluates the market question, current prices, and recommends
    whether to take a YES or NO position.
    """
    try:
        import ollama
        
        prompt = f"""Analyze this Algorand prediction market for betting opportunity:

MARKET INFO:
- Question: {market.get('question', 'Unknown')}
- Description: {market.get('description', 'N/A')}
- Category: {market.get('category', 'general')}
- YES Price: {market.get('yes_price', 0.5):.2%} (implied probability)
- NO Price: {market.get('no_price', 0.5):.2%} (implied probability)  
- 24h Volume: {market.get('volume_24h', 0):.2f} ALGO
- Liquidity: {market.get('liquidity', 0):.2f} ALGO
- Total Bets: {market.get('total_bets', 0)}
- Resolution Time: {market.get('resolution_time', 'Unknown')}

This is an AlphaArcade prediction market on Algorand.
Prices represent implied probabilities (e.g., YES at 0.70 means 70% implied probability of YES).

Analyze if the market is mispriced and recommend a position.

Reply with JSON only:
{{
    "recommendation": "YES" | "NO" | "SKIP",
    "confidence": 0.0-1.0,
    "estimated_true_probability": 0.0-1.0,
    "mispricing_detected": true | false,
    "value_edge": 0.0-1.0,
    "reasons": ["list of reasons for recommendation"],
    "risks": ["list of risks"],
    "reasoning": "Brief explanation of analysis"
}}

JSON:"""

        response = ollama.chat(
            model=llm_model,
            messages=[
                {"role": "system", "content": "You are a prediction market analyst. Evaluate markets objectively based on implied probabilities and real-world likelihood. Reply with JSON only."},
                {"role": "user", "content": prompt}
            ],
            options={"temperature": 0.3, "num_predict": 2000}
        )
        
        content = response["message"]["content"]
        return parse_llm_json(content)
        
    except Exception as e:
        log_warning(f"AI AlphaArcade analysis failed: {e}")
        return None


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def log_info(message: str):
    """Log informational message."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{Fore.CYAN}[{timestamp}] {Fore.WHITE}{message}")


def log_success(message: str):
    """Log success message."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{Fore.GREEN}[{timestamp}] ✓ {message}{Style.RESET_ALL}")


def log_warning(message: str):
    """Log warning message."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{Fore.YELLOW}[{timestamp}] ⚠ {message}{Style.RESET_ALL}")


def log_error(message: str):
    """Log error message."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{Fore.RED}[{timestamp}] ✗ {message}{Style.RESET_ALL}")


def log_trade(action: str, asset_name: str, amount: float, price: float, 
              value: float, pnl: float = None, source: str = None):
    """Log a trade with formatting.
    
    Args:
        source: Optional trade source - "rug_ninja", "alpha_arcade", or None
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    if action == "BUY":
        color = Fore.GREEN
        symbol = "📈"
    else:
        color = Fore.RED
        symbol = "📉"
    
    # Add source marker
    source_marker = ""
    if source == "rug_ninja":
        source_marker = f" {Fore.MAGENTA}🥷 RUG.NINJA{Style.RESET_ALL}"
    elif source == "alpha_arcade":
        source_marker = f" {Fore.CYAN}🎯 ALPHA ARCADE{Style.RESET_ALL}"
    
    pnl_str = ""
    if pnl is not None:
        pnl_color = Fore.GREEN if pnl >= 0 else Fore.RED
        pnl_str = f" | P/L: {pnl_color}{pnl:+.4f} ALGO{Style.RESET_ALL}"
    
    print(f"\n{color}{'='*60}")
    print(f"{symbol} {action} - {asset_name}{source_marker}")
    print(f"{'='*60}{Style.RESET_ALL}")
    print(f"  Time:   {timestamp}")
    print(f"  Amount: {amount:.6f}")
    print(f"  Price:  {price:.8f} ALGO")
    print(f"  Value:  {value:.4f} ALGO{pnl_str}")
    print(f"{color}{'='*60}{Style.RESET_ALL}\n")


def format_algo(amount: float) -> str:
    """Format ALGO amount with proper decimals."""
    return f"{amount:.6f} ALGO"


def clear_screen():
    """Clear terminal screen."""
    os.system('cls' if os.name == 'nt' else 'clear')


# ============================================================================
# RATE LIMITER
# ============================================================================

class RateLimiter:
    """
    Rate limiter for API requests to avoid hitting Nodely free tier limits.
    Uses token bucket algorithm with configurable rate.
    """
    
    def __init__(self, requests_per_second: float = RATE_LIMIT_REQUESTS_PER_SECOND,
                 min_interval: float = RATE_LIMIT_MIN_INTERVAL):
        self.requests_per_second = requests_per_second
        self.min_interval = min_interval
        self.last_request_time = 0.0
        self._lock = threading.Lock()
    
    def wait(self):
        """Wait if necessary to respect rate limits."""
        with self._lock:
            now = time.time()
            time_since_last = now - self.last_request_time
            
            if time_since_last < self.min_interval:
                sleep_time = self.min_interval - time_since_last
                time.sleep(sleep_time)
            
            self.last_request_time = time.time()
    
    def __enter__(self):
        self.wait()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


# Global rate limiters for different services
algod_rate_limiter = RateLimiter(requests_per_second=2, min_interval=0.5)
vestige_rate_limiter = RateLimiter(requests_per_second=5, min_interval=0.2)


# ============================================================================
# VESTIGE API CLIENT
# ============================================================================

class VestigeAPI:
    """Client for interacting with the Vestige API."""
    
    def __init__(self, network_id: int = DEFAULT_NETWORK_ID):
        self.base_url = VESTIGE_API_BASE
        self.network_id = network_id
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "AlgorandTradingBot/1.0"
        })
        self.rate_limiter = vestige_rate_limiter
        self.max_retries = 5  # Increased from 3 for better resilience
        self.retry_delay = 1.0
    
    def _request_with_retry(self, method: str, endpoint: str, 
                            params: Dict = None, json_data: Dict = None) -> Optional[Dict]:
        """Make request with rate limiting and retry logic."""
        url = f"{self.base_url}{endpoint}"
        
        for attempt in range(self.max_retries):
            try:
                # Apply rate limiting
                self.rate_limiter.wait()
                
                if method == "GET":
                    response = self.session.get(url, params=params, timeout=30)
                else:
                    response = self.session.post(url, params=params, json=json_data, timeout=30)
                
                # Check for rate limiting response
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", self.retry_delay * (attempt + 1)))
                    log_warning(f"Rate limited. Waiting {retry_after}s before retry...")
                    time.sleep(retry_after)
                    continue
                
                response.raise_for_status()
                return response.json()
            
            except (requests.exceptions.ConnectionError, 
                    requests.exceptions.ChunkedEncodingError,
                    ConnectionResetError) as e:
                # Connection errors - wait longer and recreate session
                wait_time = self.retry_delay * (2 ** attempt) + 2  # Extra 2 seconds
                if attempt < self.max_retries - 1:
                    log_warning(f"Connection error, recreating session and retrying in {wait_time}s...")
                    # Recreate session to get fresh connection
                    self.session = requests.Session()
                    self.session.headers.update({
                        "Accept": "application/json",
                        "User-Agent": "AlgorandTradingBot/1.0"
                    })
                    time.sleep(wait_time)
                else:
                    log_error(f"Connection failed after {self.max_retries} attempts: {e}")
                    return None
                    
            except requests.exceptions.RequestException as e:
                if attempt < self.max_retries - 1:
                    wait_time = self.retry_delay * (2 ** attempt)  # Exponential backoff
                    log_warning(f"Request failed, retrying in {wait_time}s: {e}")
                    time.sleep(wait_time)
                else:
                    log_error(f"API request failed after {self.max_retries} attempts: {e}")
                    return None
        
        return None
    
    def _get(self, endpoint: str, params: Dict = None) -> Optional[Dict]:
        """Make GET request to API."""
        if params is None:
            params = {}
        params["network_id"] = self.network_id
        return self._request_with_retry("GET", endpoint, params=params)
    
    def _post(self, endpoint: str, params: Dict = None, json_data: Dict = None) -> Optional[Dict]:
        """Make POST request to API."""
        return self._request_with_retry("POST", endpoint, params=params, json_data=json_data)
    
    def get_asset_prices(self, asset_ids: List[int]) -> Optional[List[Dict]]:
        """Get current prices for multiple assets."""
        asset_ids_str = ",".join(map(str, asset_ids))
        return self._get("/assets/price", {"asset_ids": asset_ids_str})
    
    def get_asset_price(self, asset_id: int) -> Optional[float]:
        """Get current price for a single asset."""
        result = self.get_asset_prices([asset_id])
        if result and len(result) > 0:
            return result[0].get("price", 0)
        return None
    
    def search_assets(self, query: str = "", limit: int = 50, 
                      min_volume: float = None, min_tvl: float = None,
                      order_by: str = "volume1d") -> Optional[Dict]:
        """Search for tradeable assets."""
        params = {
            "query": query,
            "limit": limit,
            "order_by": order_by,
            "order_dir": "desc"
        }
        if min_volume:
            params["volume1d__gt"] = min_volume
        if min_tvl:
            params["tvl__gt"] = min_tvl
        
        return self._get("/assets/list", params)
    
    def get_all_liquid_assets(self, min_volume: float = 0, min_tvl: float = 0,
                              max_assets: int = 250) -> List[Dict]:
        """
        Get ALL ASAs with liquidity on Algorand via Vestige.
        Uses pagination to fetch all available liquid assets.
        
        Args:
            min_volume: Minimum 24h volume filter
            min_tvl: Minimum TVL filter  
            max_assets: Maximum number of assets to return
            
        Returns:
            List of all liquid assets meeting the criteria
        """
        all_assets = []
        offset = 0
        page_size = 250  # Max allowed by API
        
        log_info(f"Scanning ALL liquid ASAs on Algorand (min vol: {min_volume}, min TVL: {min_tvl})...")
        
        while len(all_assets) < max_assets:
            params = {
                "limit": min(page_size, max_assets - len(all_assets)),
                "offset": offset,
                "order_by": "volume1d",
                "order_dir": "desc"
            }
            
            if min_volume > 0:
                params["volume1d__gt"] = min_volume
            if min_tvl > 0:
                params["tvl__gt"] = min_tvl
            
            result = self._get("/assets/list", params)
            
            if not result or "results" not in result:
                break
            
            assets = result["results"]
            if not assets:
                break
            
            all_assets.extend(assets)
            
            # Check if we've got all assets
            total_count = result.get("count", 0)
            if len(all_assets) >= total_count or len(assets) < params["limit"]:
                break
            
            offset += len(assets)
            
            # Log progress
            log_info(f"  Fetched {len(all_assets)}/{min(total_count, max_assets)} assets...")
        
        log_success(f"Found {len(all_assets)} tradeable ASAs with liquidity")
        return all_assets
    
    def get_asset_details(self, asset_ids: List[int]) -> Optional[List[Dict]]:
        """Get detailed information about assets."""
        asset_ids_str = ",".join(map(str, asset_ids))
        return self._get("/assets", {"asset_ids": asset_ids_str})
    
    def get_asset_candles(self, asset_id: int, interval: int, 
                          start: int, end: int = None) -> Optional[List[Dict]]:
        """Get OHLCV candle data for an asset."""
        params = {
            "interval": interval,
            "start": start
        }
        if end:
            params["end"] = end
        
        return self._get(f"/assets/{asset_id}/candles", params)
    
    def get_asset_history(self, asset_id: int, interval: int, 
                          start: int, end: int = None) -> Optional[List[Dict]]:
        """Get volume and swap history for an asset."""
        params = {
            "interval": interval,
            "start": start
        }
        if end:
            params["end"] = end
        
        return self._get(f"/assets/{asset_id}/history", params)
    
    def get_swap_quote(self, from_asa: int, to_asa: int, 
                       amount: int, mode: str = "sef") -> Optional[Dict]:
        """
        Get swap quote from aggregator.
        mode: 'sef' = sell exact for (selling exact amount), 
              'sfe' = sell for exact (buying exact amount)
        """
        params = {
            "from_asa": from_asa,
            "to_asa": to_asa,
            "amount": amount,
            "mode": mode
        }
        return self._get("/swap/v4", params)
    
    def get_swap_transactions(self, sender: str, slippage: float, 
                              swap_data: Dict) -> Optional[List[Dict]]:
        """Get unsigned transactions for a swap."""
        params = {
            "sender": sender,
            "slippage": slippage
        }
        return self._post("/swap/v4/transactions", params=params, json_data=swap_data)
    
    def get_pools(self, asset_id: int = None, limit: int = 50) -> Optional[Dict]:
        """Get liquidity pools."""
        params = {"limit": limit}
        if asset_id:
            params["asset_1_id"] = asset_id
        return self._get("/pools", params)
    
    def get_wallet_value(self, address: str) -> Optional[Dict]:
        """Get wallet value breakdown."""
        return self._get(f"/wallets/{address}/value")
    
    def get_recent_swaps(self, asset_id: int = None, address: str = None, 
                         limit: int = 50) -> Optional[Dict]:
        """Get recent swaps."""
        params = {"limit": limit}
        if asset_id:
            params["asset_id"] = asset_id
        if address:
            params["address"] = address
        return self._get("/swaps", params)


# ============================================================================
# ALGORAND WALLET
# ============================================================================

class AlgorandWallet:
    """Manages Algorand wallet operations with rate limiting for Nodely free tier."""
    
    def __init__(self, mnemonic_phrase: str):
        """Initialize wallet from mnemonic phrase."""
        try:
            self.private_key = mnemonic.to_private_key(mnemonic_phrase)
            self.address = account.address_from_private_key(self.private_key)
            self.algod_client = algod.AlgodClient("", ALGOD_ADDRESS)
            self.indexer_client = indexer.IndexerClient("", INDEXER_ADDRESS)
            self.rate_limiter = algod_rate_limiter
            self.max_retries = 5  # Increased for better resilience
            self.retry_delay = 1.0
            log_success(f"Wallet initialized: {self.address[:8]}...{self.address[-8:]}")
        except Exception as e:
            log_error(f"Failed to initialize wallet: {e}")
            raise
    
    def _call_with_retry(self, func, *args, **kwargs):
        """Call function with rate limiting and retry logic."""
        for attempt in range(self.max_retries):
            try:
                # Apply rate limiting
                self.rate_limiter.wait()
                return func(*args, **kwargs)
            except Exception as e:
                error_str = str(e).lower()
                
                # Check for rate limiting
                if "429" in error_str or "rate" in error_str or "limit" in error_str:
                    wait_time = self.retry_delay * (2 ** attempt)
                    log_warning(f"Rate limited by Algod. Waiting {wait_time}s...")
                    time.sleep(wait_time)
                
                # Check for connection errors
                elif "connection" in error_str or "reset" in error_str or "aborted" in error_str or "10054" in error_str:
                    wait_time = self.retry_delay * (2 ** attempt) + 2
                    if attempt < self.max_retries - 1:
                        log_warning(f"Connection error, retrying in {wait_time}s...")
                        time.sleep(wait_time)
                    else:
                        raise
                
                elif attempt < self.max_retries - 1:
                    wait_time = self.retry_delay * (attempt + 1)
                    log_warning(f"Algod call failed, retrying in {wait_time}s: {e}")
                    time.sleep(wait_time)
                else:
                    raise
        return None
    
    def get_account_info(self) -> Optional[Dict]:
        """Get account information."""
        try:
            return self._call_with_retry(self.algod_client.account_info, self.address)
        except Exception as e:
            log_error(f"Failed to get account info: {e}")
            return None
    
    def get_algo_balance(self) -> float:
        """Get ALGO balance in standard units."""
        try:
            info = self.get_account_info()
            if info:
                # Convert from microAlgos to Algos
                return info.get("amount", 0) / 1_000_000
            return 0.0
        except Exception as e:
            log_error(f"Failed to get balance: {e}")
            return 0.0
    
    def get_asset_balance(self, asset_id: int) -> float:
        """Get balance of a specific ASA."""
        try:
            info = self.get_account_info()
            if info and "assets" in info:
                for asset in info["assets"]:
                    if asset["asset-id"] == asset_id:
                        # Get asset decimals with rate limiting
                        asset_info = self._call_with_retry(
                            self.algod_client.asset_info, asset_id
                        )
                        decimals = asset_info["params"].get("decimals", 0) if asset_info else 0
                        return asset["amount"] / (10 ** decimals)
            return 0.0
        except Exception as e:
            log_error(f"Failed to get asset balance: {e}")
            return 0.0
    
    def get_asset_info(self, asset_id: int) -> Optional[Dict]:
        """Get ASA information."""
        try:
            return self._call_with_retry(self.algod_client.asset_info, asset_id)
        except Exception as e:
            log_error(f"Failed to get asset info: {e}")
            return None
    
    def get_all_asset_holdings(self) -> List[Dict]:
        """Get all ASA holdings in the wallet with details."""
        holdings = []
        try:
            info = self.get_account_info()
            if info and "assets" in info:
                for asset in info["assets"]:
                    asset_id = asset["asset-id"]
                    amount_raw = asset["amount"]
                    
                    # Skip zero balances
                    if amount_raw == 0:
                        continue
                    
                    # Get asset details
                    try:
                        asset_info = self._call_with_retry(
                            self.algod_client.asset_info, asset_id
                        )
                        if asset_info:
                            params = asset_info.get("params", {})
                            decimals = params.get("decimals", 0)
                            name = params.get("name", "Unknown")
                            unit_name = params.get("unit-name", "???")
                            
                            holdings.append({
                                "asset_id": asset_id,
                                "name": name,
                                "unit_name": unit_name,
                                "decimals": decimals,
                                "amount_raw": amount_raw,
                                "amount": amount_raw / (10 ** decimals) if decimals > 0 else amount_raw
                            })
                    except Exception as e:
                        log_warning(f"Could not get info for asset {asset_id}: {e}")
                        continue
                        
            return holdings
        except Exception as e:
            log_error(f"Failed to get asset holdings: {e}")
            return []
    
    def is_opted_in(self, asset_id: int) -> bool:
        """Check if wallet is opted into an ASA."""
        try:
            info = self.get_account_info()
            if info and "assets" in info:
                for asset in info["assets"]:
                    if asset["asset-id"] == asset_id:
                        return True
            return False
        except Exception as e:
            return False
    
    def opt_in_asset(self, asset_id: int) -> Optional[str]:
        """Opt into an ASA."""
        try:
            if self.is_opted_in(asset_id):
                log_info(f"Already opted into asset {asset_id}")
                return "already_opted_in"
            
            # Get suggested params with rate limiting
            params = self._call_with_retry(self.algod_client.suggested_params)
            if not params:
                log_error("Failed to get transaction params")
                return None
            
            txn = transaction.AssetOptInTxn(
                sender=self.address,
                sp=params,
                index=asset_id
            )
            signed_txn = txn.sign(self.private_key)
            
            # Send transaction with rate limiting
            self.rate_limiter.wait()
            txid = self.algod_client.send_transaction(signed_txn)
            
            # Wait for confirmation
            transaction.wait_for_confirmation(self.algod_client, txid, 4)
            log_success(f"Opted into asset {asset_id}: {txid}")
            return txid
        except Exception as e:
            log_error(f"Failed to opt in to asset: {e}")
            return None
    
    def sign_and_submit_transactions(self, unsigned_txns: List[Dict]) -> Optional[str]:
        """Sign and submit a group of transactions from Vestige."""
        try:
            from algosdk import transaction
            import base64
            import msgpack
            
            log_info(f"Processing {len(unsigned_txns)} transactions...")
            
            signed_txns = []
            
            for i, txn_data in enumerate(unsigned_txns):
                txn_b64 = txn_data["txn"]
                signers = txn_data.get("signers", [])
                
                try:
                    # Fix base64 padding if needed
                    missing_padding = len(txn_b64) % 4
                    if missing_padding:
                        txn_b64 += '=' * (4 - missing_padding)
                    
                    # Decode base64 and msgpack
                    txn_bytes = base64.b64decode(txn_b64)
                    
                    # Use msgpack Unpacker (handles various formats cleanly)
                    unpacker = msgpack.Unpacker(raw=False, strict_map_key=False)
                    unpacker.feed(txn_bytes)
                    txn_dict = unpacker.unpack()
                    
                    if isinstance(txn_dict, dict):
                        if 'txn' in txn_dict:
                            txn = transaction.Transaction.undictify(txn_dict['txn'])
                        else:
                            txn = transaction.Transaction.undictify(txn_dict)
                        signed_txn = txn.sign(self.private_key)
                        signed_txns.append(signed_txn)
                        
                except Exception as e:
                    log_error(f"  Error processing transaction {i+1}: {e}")
                    import traceback
                    traceback.print_exc()
                    continue
            
            if not signed_txns:
                log_error("No transactions to submit")
                return None
            
            log_info(f"Submitting {len(signed_txns)} signed transactions...")
            
            # Submit transactions with rate limiting
            self.rate_limiter.wait()
            
            # Submit transaction(s)
            if len(signed_txns) == 1:
                txid = self.algod_client.send_transaction(signed_txns[0])
            else:
                txid = self.algod_client.send_transactions(signed_txns)
            
            # Wait for confirmation
            log_info(f"Waiting for confirmation...")
            transaction.wait_for_confirmation(self.algod_client, txid, 4)
            return txid
            
        except Exception as e:
            log_error(f"Failed to sign/submit transactions: {e}")
            import traceback
            traceback.print_exc()
            return None


# ============================================================================
# LLM ANALYSIS
# ============================================================================

class LLMAnalyzer:
    """Uses local LLM for trading analysis with multi-model support."""
    
    def __init__(self, model: str = "llama3.2", config: TradingConfig = None):
        self.model = model
        self.config = config
        self.available = self._check_availability()
    
    def _check_availability(self) -> bool:
        """Check if Ollama is available."""
        try:
            import ollama
            ollama.list()
            log_success(f"LLM available: {self.model}")
            return True
        except Exception as e:
            log_warning(f"LLM not available: {e}")
            return False
    
    def _get_model_for_task(self, task: str) -> str:
        """Get the appropriate model for a task, supporting multi-LLM config."""
        if self.config and self.config.multi_llm_enabled:
            model = get_llm_for_task(self.config, task)
            if model:
                return model
        return self.model
    
    def analyze_market(self, asset_data: List[Dict], 
                       candles: List[Dict] = None) -> Optional[Dict]:
        """Analyze market data using LLM."""
        if not self.available:
            return None
        
        try:
            import ollama
            
            # Get the appropriate model for market analysis
            model = self._get_model_for_task("market")
            
            # Prepare market summary
            market_summary = self._prepare_market_summary(asset_data, candles)
            
            prompt = f"""You are a cryptocurrency trading analyst. Analyze the following Algorand ASA market data and provide trading recommendations.

Market Data:
{market_summary}

Provide your analysis in the following JSON format:
{{
    "sentiment": "bullish" | "bearish" | "neutral",
    "confidence": 0.0-1.0,
    "top_buys": [
        {{"asset_id": int, "reason": "string", "entry_price": float, "target": float, "stop_loss": float}}
    ],
    "top_sells": [
        {{"asset_id": int, "reason": "string"}}
    ],
    "market_summary": "Brief market overview",
    "risk_level": "low" | "medium" | "high"
}}

Respond ONLY with valid JSON, no other text."""

            response = ollama.chat(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a trading analyst. Reply with JSON only."},
                    {"role": "user", "content": prompt}
                ],
                options={"temperature": 0.3, "num_predict": 2000}
            )
            
            # Parse JSON response
            content = response["message"]["content"]
            result = parse_llm_json(content)
            return result
            
        except Exception as e:
            log_error(f"LLM analysis failed: {e}")
            return None
    
    def _prepare_market_summary(self, asset_data: List[Dict], 
                                candles: List[Dict] = None) -> str:
        """Prepare market data summary for LLM."""
        summary = []
        
        for asset in asset_data[:10]:  # Top 10 assets
            summary.append(f"""
Asset: {asset.get('name', 'Unknown')} ({asset.get('ticker', 'N/A')})
- ID: {asset.get('id')}
- Price: {asset.get('price', 0):.8f} ALGO
- 24h Change: {((asset.get('price', 0) / asset.get('price1d', 1)) - 1) * 100:.2f}%
- 24h Volume: {asset.get('volume1d', 0):.2f} ALGO
- TVL: {asset.get('tvl', 0):.2f} ALGO
- Market Cap: {asset.get('market_cap', 0):.2f} ALGO
""")
        
        return "\n".join(summary)
    
    def evaluate_trade(self, asset_info: Dict, current_price: float,
                       position: Position = None, candles: List[Dict] = None) -> Optional[Dict]:
        """Evaluate a specific trade opportunity."""
        if not self.available:
            return None
        
        try:
            import ollama
            
            # Get the appropriate model for trade decisions
            model = self._get_model_for_task("trade")
            
            position_info = ""
            if position:
                position_info = f"""
Current Position:
- Holding: {position.amount:.6f}
- Average Buy Price: {position.avg_buy_price:.8f} ALGO
- Total Invested: {position.total_invested:.4f} ALGO
- Current Value: {position.current_value:.4f} ALGO
- Unrealized P/L: {position.unrealized_pnl:.4f} ALGO ({position.unrealized_pnl_percent:.2f}%)
"""
            
            prompt = f"""Evaluate this Algorand ASA trade:

Asset: {asset_info.get('name')} ({asset_info.get('ticker')})
Current Price: {current_price:.8f} ALGO
24h Volume: {asset_info.get('volume1d', 0):.2f} ALGO
24h Change: {((current_price / asset_info.get('price1d', current_price)) - 1) * 100:.2f}%
TVL: {asset_info.get('tvl', 0):.2f} ALGO
{position_info}

Should we BUY, SELL, or HOLD? Provide response in JSON:
{{
    "action": "BUY" | "SELL" | "HOLD",
    "confidence": 0.0-1.0,
    "reason": "Brief explanation",
    "suggested_amount_percent": 0-100
}}

Respond ONLY with valid JSON."""

            response = ollama.chat(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a trading analyst. Reply with JSON only."},
                    {"role": "user", "content": prompt}
                ],
                options={"temperature": 0.3, "num_predict": 1500}
            )
            
            content = response["message"]["content"]
            return parse_llm_json(content)
            
        except Exception as e:
            log_error(f"Trade evaluation failed: {e}")
            return None


# ============================================================================
# PRICE HISTORY TRACKER (For Technical Analysis)
# ============================================================================

class PriceHistoryTracker:
    """
    Tracks price and volume history for technical analysis.
    
    Maintains rolling windows of OHLCV data for each tracked asset,
    enabling TA calculations like RSI, MACD, and Bollinger Bands.
    """
    
    def __init__(self, max_history: int = 100):
        """
        Initialize price history tracker.
        
        Args:
            max_history: Maximum number of price points to keep per asset
        """
        self.max_history = max_history
        self.prices: Dict[int, List[float]] = {}  # asset_id -> [prices]
        self.volumes: Dict[int, List[float]] = {}  # asset_id -> [volumes]
        self.highs: Dict[int, List[float]] = {}  # asset_id -> [high prices]
        self.lows: Dict[int, List[float]] = {}  # asset_id -> [low prices]
        self.timestamps: Dict[int, List[datetime]] = {}  # asset_id -> [timestamps]
        self._lock = threading.Lock()
    
    def update(self, asset_id: int, price: float, volume: float = 0, 
               high: float = None, low: float = None):
        """Add a new price point for an asset."""
        with self._lock:
            if asset_id not in self.prices:
                self.prices[asset_id] = []
                self.volumes[asset_id] = []
                self.highs[asset_id] = []
                self.lows[asset_id] = []
                self.timestamps[asset_id] = []
            
            self.prices[asset_id].append(price)
            self.volumes[asset_id].append(volume)
            self.highs[asset_id].append(high if high else price)
            self.lows[asset_id].append(low if low else price)
            self.timestamps[asset_id].append(datetime.now())
            
            # Trim to max history
            if len(self.prices[asset_id]) > self.max_history:
                self.prices[asset_id] = self.prices[asset_id][-self.max_history:]
                self.volumes[asset_id] = self.volumes[asset_id][-self.max_history:]
                self.highs[asset_id] = self.highs[asset_id][-self.max_history:]
                self.lows[asset_id] = self.lows[asset_id][-self.max_history:]
                self.timestamps[asset_id] = self.timestamps[asset_id][-self.max_history:]
    
    def get_prices(self, asset_id: int) -> List[float]:
        """Get price history for an asset."""
        with self._lock:
            return self.prices.get(asset_id, []).copy()
    
    def get_volumes(self, asset_id: int) -> List[float]:
        """Get volume history for an asset."""
        with self._lock:
            return self.volumes.get(asset_id, []).copy()
    
    def get_ohlcv(self, asset_id: int) -> Dict:
        """Get full OHLCV data for an asset."""
        with self._lock:
            return {
                'prices': self.prices.get(asset_id, []).copy(),
                'volumes': self.volumes.get(asset_id, []).copy(),
                'highs': self.highs.get(asset_id, []).copy(),
                'lows': self.lows.get(asset_id, []).copy(),
                'timestamps': self.timestamps.get(asset_id, []).copy()
            }
    
    def has_sufficient_history(self, asset_id: int, min_points: int = 30) -> bool:
        """Check if we have enough history for TA calculations."""
        with self._lock:
            return len(self.prices.get(asset_id, [])) >= min_points
    
    def get_ta_signal(self, asset_id: int) -> Optional[Dict]:
        """Generate TA signal for an asset if sufficient history exists."""
        prices = self.get_prices(asset_id)
        volumes = self.get_volumes(asset_id)
        
        if len(prices) < 20:
            return None
        
        return TechnicalAnalysis.generate_signal(prices, volumes if volumes else None)


# ============================================================================
# SLIPPAGE CALCULATOR (Smart Order Routing)
# ============================================================================

class SlippageCalculator:
    """
    Calculate and optimize for slippage on AMM trades.
    
    Based on the research: "slippage increases non-linearly with size,
    so two smaller orders often outperform one big order."
    
    Helps find the optimal trade size that maximizes profit after slippage.
    """
    
    @staticmethod
    def estimate_slippage(trade_size_algo: float, liquidity_algo: float, 
                          fee_percent: float = 0.3) -> float:
        """
        Estimate slippage for a trade given pool liquidity.
        
        Uses constant product AMM formula approximation:
        slippage ≈ trade_size / (2 * liquidity)
        
        Args:
            trade_size_algo: Trade size in ALGO
            liquidity_algo: Pool liquidity in ALGO
            fee_percent: DEX fee percentage
        
        Returns:
            Estimated slippage as a percentage
        """
        if liquidity_algo <= 0:
            return 100.0  # No liquidity = infinite slippage
        
        # AMM slippage approximation
        price_impact = (trade_size_algo / (2 * liquidity_algo)) * 100
        
        # Add trading fee
        total_slippage = price_impact + fee_percent
        
        return min(total_slippage, 100.0)
    
    @staticmethod
    def calculate_optimal_size(expected_profit_pct: float, liquidity_algo: float,
                               max_size_algo: float, fee_percent: float = 0.3,
                               min_net_profit_pct: float = 0.5) -> float:
        """
        Calculate the optimal trade size that maximizes net profit after slippage.
        
        Based on the Whack-A-Mole research: profit peaked at a certain trade size
        (~400 USDT) and turned to loss at 900 USDT due to slippage.
        
        Args:
            expected_profit_pct: Expected profit percentage before slippage
            liquidity_algo: Pool liquidity in ALGO
            max_size_algo: Maximum allowed trade size
            fee_percent: DEX fee percentage
            min_net_profit_pct: Minimum acceptable net profit after slippage
        
        Returns:
            Optimal trade size in ALGO
        """
        if expected_profit_pct <= fee_percent:
            return 0  # Not profitable even without slippage
        
        # Binary search for optimal size
        best_size = 0
        best_net_profit = 0
        
        # Test different sizes
        test_sizes = [max_size_algo * (i / 20) for i in range(1, 21)]
        
        for size in test_sizes:
            slippage = SlippageCalculator.estimate_slippage(size, liquidity_algo, fee_percent)
            net_profit_pct = expected_profit_pct - slippage
            
            if net_profit_pct < min_net_profit_pct:
                break  # Past the profitable zone
            
            net_profit_algo = size * (net_profit_pct / 100)
            
            if net_profit_algo > best_net_profit:
                best_net_profit = net_profit_algo
                best_size = size
        
        return best_size
    
    @staticmethod
    def should_split_trade(trade_size_algo: float, liquidity_algo: float,
                           threshold_ratio: float = 0.05) -> bool:
        """
        Determine if a trade should be split to reduce slippage.
        
        Args:
            trade_size_algo: Proposed trade size
            liquidity_algo: Pool liquidity
            threshold_ratio: If trade/liquidity > this, recommend splitting
        
        Returns:
            True if trade should be split
        """
        if liquidity_algo <= 0:
            return True
        
        ratio = trade_size_algo / liquidity_algo
        return ratio > threshold_ratio
    
    @staticmethod
    def calculate_split_sizes(total_size_algo: float, num_splits: int = 3,
                              delay_seconds: int = 30) -> List[Dict]:
        """
        Calculate trade split sizes for TWAP-style execution.
        
        Returns list of {size, delay} for staged execution.
        """
        if num_splits <= 1:
            return [{'size': total_size_algo, 'delay': 0}]
        
        size_per_split = total_size_algo / num_splits
        
        return [
            {'size': size_per_split, 'delay': i * delay_seconds}
            for i in range(num_splits)
        ]


# ============================================================================
# DYNAMIC POSITION SIZER (Risk Management)
# ============================================================================

class DynamicPositionSizer:
    """
    Calculate position sizes based on multiple risk factors.
    
    Implements concepts from the research:
    - Kelly Criterion for optimal sizing
    - Volatility parity (scale inversely with volatility)
    - Confidence-based sizing
    - Maximum exposure limits
    """
    
    def __init__(self, config: TradingConfig, state: BotState):
        self.config = config
        self.state = state
    
    def calculate_size(self, opportunity: Dict, balance_algo: float,
                       price_history: List[float] = None) -> float:
        """
        Calculate optimal position size for an opportunity.
        
        Considers:
        1. Base max position size from config
        2. Confidence/score adjustment
        3. Volatility adjustment
        4. Win rate / Kelly criterion
        5. Current exposure limits
        6. Slippage optimization
        
        Returns:
            Recommended position size in ALGO
        """
        # Start with base size
        base_size = min(self.config.max_position_size_algo, balance_algo * 0.2)  # Max 20% of balance per trade
        
        score = opportunity.get('score', 50)
        liquidity = opportunity.get('liquidity', 0) or opportunity.get('tvl', 0) or 10000
        
        # 1. Confidence adjustment (score-based)
        # Higher score = larger position (but capped)
        confidence_factor = 0.5 + (score / 200)  # 0.5 to 1.0
        confidence_factor = max(0.25, min(1.5, confidence_factor))
        
        adjusted_size = base_size * confidence_factor
        
        # 2. Volatility adjustment
        if price_history and len(price_history) >= 20:
            volatility = TechnicalAnalysis.calculate_volatility(price_history)
            if volatility:
                adjusted_size = calculate_volatility_adjusted_size(
                    adjusted_size, volatility, target_volatility=3.0
                )
        
        # 3. Kelly Criterion adjustment (if we have trade history)
        if self.state.total_trades >= 10:
            win_rate = self.state.win_rate
            
            # Estimate avg win/loss from history
            wins = [t.pnl for t in self.state.trade_history if hasattr(t, 'pnl') and t.pnl and t.pnl > 0]
            losses = [abs(t.pnl) for t in self.state.trade_history if hasattr(t, 'pnl') and t.pnl and t.pnl < 0]
            
            if wins and losses:
                avg_win = sum(wins) / len(wins)
                avg_loss = sum(losses) / len(losses)
                kelly_fraction = calculate_kelly_position_size(win_rate, avg_win, avg_loss)
                
                # Apply Kelly as a cap
                kelly_max = balance_algo * kelly_fraction
                adjusted_size = min(adjusted_size, kelly_max)
        
        # 4. Exposure limit check
        current_exposure = sum(p.total_invested for p in self.state.positions.values())
        max_total_exposure = balance_algo * 0.8  # Don't use more than 80% of balance
        
        if current_exposure + adjusted_size > max_total_exposure:
            adjusted_size = max(0, max_total_exposure - current_exposure)
        
        # 5. Position count limit
        if len(self.state.positions) >= self.config.max_total_positions:
            adjusted_size = 0
        
        # 6. Slippage optimization
        if adjusted_size > 0 and liquidity > 0:
            expected_profit = opportunity.get('price_change_pct', 5) or 5
            optimal_size = SlippageCalculator.calculate_optimal_size(
                expected_profit_pct=expected_profit,
                liquidity_algo=liquidity,
                max_size_algo=adjusted_size,
                fee_percent=0.3
            )
            adjusted_size = min(adjusted_size, optimal_size) if optimal_size > 0 else adjusted_size
        
        # 7. Minimum trade size (to make fees worthwhile)
        min_trade_size = 1.0  # Minimum 1 ALGO
        if 0 < adjusted_size < min_trade_size:
            adjusted_size = 0  # Too small to be profitable after fees
        
        # 8. Rug.ninja limit - only apply to actual rug.ninja tokens (not graduated)
        if opportunity.get('is_rug_ninja'):
            max_rn = opportunity.get('max_buy_algo', self.config.rug_ninja_max_buy_algo)
            adjusted_size = min(adjusted_size, max_rn)
        
        return adjusted_size
    
    def get_sizing_explanation(self, opportunity: Dict, final_size: float,
                                balance_algo: float) -> str:
        """Get a human-readable explanation of sizing decision."""
        base = self.config.max_position_size_algo
        score = opportunity.get('score', 50)
        
        parts = [f"Base: {base:.1f} ALGO"]
        
        # Score adjustment
        if score < 40:
            parts.append(f"↓ Low score ({score})")
        elif score > 70:
            parts.append(f"↑ High score ({score})")
        
        # Win rate
        if self.state.total_trades >= 10:
            wr = self.state.win_rate * 100
            if wr < 40:
                parts.append(f"↓ Low win rate ({wr:.0f}%)")
            elif wr > 60:
                parts.append(f"↑ Good win rate ({wr:.0f}%)")
        
        # Final
        parts.append(f"→ Final: {final_size:.2f} ALGO")
        
        return " | ".join(parts)


# ============================================================================
# CIRCUIT BREAKER (Emergency Stop)
# ============================================================================

class CircuitBreaker:
    """
    Emergency stop mechanism to prevent catastrophic losses.
    
    Based on research: "if the bot's equity curve drops 20% from peak,
    it might cut all positions and go into a safe mode until reviewed."
    """
    
    def __init__(self, config: TradingConfig, state: BotState):
        self.config = config
        self.state = state
        self.triggered = False
        self.trigger_reason = ""
        self.trigger_time: Optional[datetime] = None
        self.cooldown_minutes = 60  # Default 1 hour cooldown
    
    def check(self) -> bool:
        """
        Check if circuit breaker should trigger.
        
        Returns True if trading should be halted.
        """
        if self.triggered:
            # Check if cooldown has passed
            if self.trigger_time:
                elapsed = (datetime.now() - self.trigger_time).total_seconds() / 60
                if elapsed >= self.cooldown_minutes:
                    self.reset()
                    return False
            return True
        
        # Check various trigger conditions
        
        # 1. Max drawdown from peak
        if self.state.max_balance_algo > 0:
            current_drawdown = ((self.state.max_balance_algo - self.state.current_balance_algo) 
                               / self.state.max_balance_algo * 100)
            if current_drawdown >= self.config.max_drawdown_percent:
                self._trigger(f"Max drawdown exceeded: {current_drawdown:.1f}% >= {self.config.max_drawdown_percent}%")
                return True
        
        # 2. Daily loss limit
        if self.config.max_daily_loss_algo > 0:
            if self.state.daily_pnl_algo <= -self.config.max_daily_loss_algo:
                self._trigger(f"Daily loss limit: {self.state.daily_pnl_algo:.2f} ALGO")
                return True
        
        # 3. Consecutive losses (emergency stop after 5 consecutive losses)
        if self.state.total_trades >= 5:
            recent_trades = self.state.trade_history[-5:]
            consecutive_losses = sum(1 for t in recent_trades 
                                    if hasattr(t, 'pnl') and t.pnl and t.pnl < 0)
            if consecutive_losses >= 5:
                self._trigger("5 consecutive losing trades")
                return True
        
        # 4. Win rate collapse (if we have enough trades)
        if self.config.min_win_rate > 0 and self.state.total_trades >= self.config.min_trades_for_win_rate:
            if self.state.win_rate < self.config.min_win_rate:
                self._trigger(f"Win rate below minimum: {self.state.win_rate*100:.1f}% < {self.config.min_win_rate*100:.1f}%")
                return True
        
        return False
    
    def _trigger(self, reason: str):
        """Trigger the circuit breaker."""
        self.triggered = True
        self.trigger_reason = reason
        self.trigger_time = datetime.now()
        log_error(f"🚨 CIRCUIT BREAKER TRIGGERED: {reason}")
        log_warning(f"Trading halted for {self.cooldown_minutes} minutes")
    
    def reset(self):
        """Reset the circuit breaker."""
        if self.triggered:
            log_info("✅ Circuit breaker reset - trading resumed")
        self.triggered = False
        self.trigger_reason = ""
        self.trigger_time = None
    
    def force_reset(self):
        """Force reset (for manual override)."""
        self.reset()
        log_warning("Circuit breaker force reset by user")
    
    def get_status(self) -> Dict:
        """Get circuit breaker status."""
        remaining_cooldown = 0
        if self.triggered and self.trigger_time:
            elapsed = (datetime.now() - self.trigger_time).total_seconds() / 60
            remaining_cooldown = max(0, self.cooldown_minutes - elapsed)
        
        return {
            'triggered': self.triggered,
            'reason': self.trigger_reason,
            'trigger_time': self.trigger_time,
            'remaining_cooldown_minutes': remaining_cooldown
        }


# ============================================================================
# TRADING STRATEGIES
# ============================================================================

class TradingEngine:
    """
    Core trading engine with multiple strategies and profit optimization.
    
    Integrates:
    - Technical Analysis (RSI, MACD, Bollinger Bands)
    - Dynamic Position Sizing (Kelly, volatility-adjusted)
    - Slippage Optimization
    - Circuit Breaker (emergency stop)
    - Price History Tracking
    """
    
    def __init__(self, wallet: AlgorandWallet, api: VestigeAPI, 
                 config: TradingConfig, state: BotState):
        self.wallet = wallet
        self.api = api
        self.config = config
        self.state = state
        
        # Pass config to LLMAnalyzer for multi-LLM support
        self.llm = LLMAnalyzer(config.llm_model, config) if config.use_llm else None
        
        # Profit optimization components
        self.price_history = PriceHistoryTracker(max_history=100)
        self.position_sizer = DynamicPositionSizer(config, state)
        self.circuit_breaker = CircuitBreaker(config, state)
        
        # TA-enhanced trading flag
        self.use_ta = getattr(config, 'use_technical_analysis', True)
    
    def update_price_history(self, asset_id: int, price: float, volume: float = 0):
        """Update price history for an asset (called on each price fetch)."""
        if price and price > 0:
            self.price_history.update(asset_id, price, volume)
    
    def get_ta_enhanced_score(self, opportunity: Dict) -> Dict:
        """
        Enhance opportunity scoring with technical analysis.
        
        Returns enhanced opportunity with TA-adjusted score and signals.
        """
        asset_id = opportunity.get('asset_id')
        if not asset_id:
            return opportunity
        
        prices = self.price_history.get_prices(asset_id)
        volumes = self.price_history.get_volumes(asset_id)
        
        if len(prices) < 20:
            # Not enough history - add current price to start building
            current_price = opportunity.get('current_price', 0)
            if current_price:
                self.price_history.update(asset_id, current_price, 
                                         opportunity.get('volume_24h', 0))
            return opportunity
        
        # Get TA-enhanced scoring
        ta_result = score_opportunity_with_ta(opportunity, prices, volumes)
        
        # Update opportunity with TA data
        enhanced = opportunity.copy()
        enhanced['score'] = ta_result['score']
        enhanced['ta_signal'] = ta_result['ta_signal']
        enhanced['ta_adjustment'] = ta_result['ta_adjustment']
        enhanced['ta_reasons'] = ta_result['ta_reasons']
        
        return enhanced
    
    def calculate_position_size(self, opportunity: Dict) -> float:
        """
        Calculate optimal position size using dynamic sizing.
        
        Considers: score, volatility, Kelly criterion, slippage, exposure limits.
        """
        balance = self.wallet.get_algo_balance()
        
        # Get price history for volatility calculation
        asset_id = opportunity.get('asset_id')
        prices = self.price_history.get_prices(asset_id) if asset_id else []
        
        return self.position_sizer.calculate_size(opportunity, balance, prices)
    
    def should_trade(self) -> Tuple[bool, str]:
        """
        Check if trading is allowed (circuit breaker, daily limits, etc.).
        
        Returns (can_trade, reason) tuple.
        """
        # Check circuit breaker first
        if self.circuit_breaker.check():
            status = self.circuit_breaker.get_status()
            return False, f"Circuit breaker: {status['reason']} ({status['remaining_cooldown_minutes']:.0f}m remaining)"
        
        # Reset daily stats if needed
        self.state.reset_daily_stats()
        
        # Check daily trade limit
        if self.config.max_daily_trades > 0:
            if self.state.daily_trades >= self.config.max_daily_trades:
                return False, f"Daily trade limit reached ({self.state.daily_trades}/{self.config.max_daily_trades})"
        
        # Check daily loss limit
        if self.config.max_daily_loss_algo > 0:
            if self.state.daily_pnl_algo <= -self.config.max_daily_loss_algo:
                return False, f"Daily loss limit reached ({self.state.daily_pnl_algo:.2f} ALGO)"
        
        # Check cooldown after loss
        if self.config.cooldown_after_loss_minutes > 0 and self.state.last_loss_time:
            elapsed = (datetime.now() - self.state.last_loss_time).total_seconds() / 60
            if elapsed < self.config.cooldown_after_loss_minutes:
                remaining = self.config.cooldown_after_loss_minutes - elapsed
                return False, f"Cooldown after loss ({remaining:.0f}m remaining)"
        
        return True, "OK"
    
    def scan_existing_positions(self) -> int:
        """
        Scan wallet for existing ASA holdings and add them as positions.
        Uses current market price as cost basis.
        Returns number of positions imported.
        Also updates starting_balance_algo to include imported positions' value.
        Identifies rug.ninja tokens and AlphaArcade positions.
        """
        if not self.config.import_existing_positions:
            return 0
        
        log_info("Scanning wallet for existing holdings...")
        
        holdings = self.wallet.get_all_asset_holdings()
        
        if not holdings:
            log_info("No existing holdings found")
            return 0
        
        # Get list of rug.ninja tokens for identification (graceful failure)
        rug_ninja_tokens = {}
        try:
            rn_tokens = scan_rug_ninja_tokens(min_bond_progress=0.0, max_bond_progress=1.0, min_volume=0, max_age_minutes=0, limit=500)
            if rn_tokens:
                for token in rn_tokens:
                    asset_id = token.get("asset_id")
                    if asset_id:
                        rug_ninja_tokens[asset_id] = token
                log_info(f"  🥷 Loaded {len(rn_tokens)} known rug.ninja tokens")
        except Exception as e:
            pass  # Silent failure - rug.ninja identification not critical
        
        # AlphaArcade identification - $ALPHA token ASA ID is known
        # Note: AlphaArcade doesn't have a public API, so we use known ASA IDs
        ALPHA_TOKEN_ID = 2726252423  # $ALPHA governance token
        alpha_arcade_assets = {ALPHA_TOKEN_ID: {"market": None, "position": "ALPHA"}}
        # Additional AlphaArcade market YES/NO tokens would be added here if known
        
        imported = 0
        imported_rug_ninja = 0
        imported_alpha_arcade = 0
        skipped_dust = 0
        skipped_no_price = 0
        
        for holding in holdings:
            asset_id = holding["asset_id"]
            
            # Skip if already tracking this position
            if asset_id in self.state.positions:
                continue
            
            # Get current price from Vestige
            price = self.api.get_asset_price(asset_id)
            
            if not price or price <= 0:
                skipped_no_price += 1
                continue
            
            # Calculate current value
            amount = holding["amount"]
            current_value = amount * price
            
            # Skip dust positions
            if current_value < self.config.min_position_value_algo:
                skipped_dust += 1
                continue
            
            # Identify position type
            # Only label as rug.ninja if token is STILL ON BONDING CURVE (not graduated)
            is_rug_ninja_bonding = False
            if asset_id in rug_ninja_tokens:
                token_info = rug_ninja_tokens[asset_id]
                # Check if token is still on bonding curve (not graduated)
                is_graduated = token_info.get("is_graduated", False) or token_info.get("graduated", False)
                bond_progress = token_info.get("bond_progress", token_info.get("bonding_progress", 0))
                # Only label as rug.ninja if NOT graduated and still bonding
                if not is_graduated and bond_progress < 1.0:
                    is_rug_ninja_bonding = True
            
            is_alpha_arcade = asset_id in alpha_arcade_assets
            
            # Create position with current price as cost basis
            asset_name = f"{holding['name']} ({holding['unit_name']})"
            
            # Add marker to asset name - ONLY for tokens still on bonding curve
            if is_rug_ninja_bonding:
                asset_name = f"🥷 {asset_name}"
                imported_rug_ninja += 1
            elif is_alpha_arcade:
                aa_info = alpha_arcade_assets[asset_id]
                asset_name = f"🎯 {asset_name} [{aa_info['position']}]"
                imported_alpha_arcade += 1
            # Note: Graduated rug.ninja tokens are treated as regular ASAs (no label)
            
            self.state.positions[asset_id] = Position(
                asset_id=asset_id,
                asset_name=asset_name,
                amount=amount,
                avg_buy_price=price,  # Use current price as cost basis
                total_invested=current_value,  # Treat current value as investment
                is_imported=True  # Mark as imported position
            )
            
            imported += 1
        
        if imported > 0:
            log_success(f"Imported {imported} existing positions:")
            if imported_rug_ninja > 0:
                log_info(f"  🥷 {imported_rug_ninja} rug.ninja tokens (still bonding)")
            if imported_alpha_arcade > 0:
                log_info(f"  🎯 {imported_alpha_arcade} AlphaArcade positions")
            regular_count = imported - imported_rug_ninja - imported_alpha_arcade
            if regular_count > 0:
                log_info(f"  📊 {regular_count} regular ASAs (includes graduated rug.ninja)")
            
            total_value = 0
            for asset_id, pos in self.state.positions.items():
                value = pos.amount * pos.avg_buy_price
                total_value += value
                log_info(f"  • {pos.asset_name}: {pos.amount:.6f} tokens ({value:.2f} ALGO)")
            log_info(f"  Total existing value: {total_value:.2f} ALGO")
            
            # Update starting balance to include imported positions
            # This ensures ROI calculation is accurate
            self.state.starting_balance_algo += total_value
            self.state.current_balance_algo += total_value
            self.state.max_balance_algo = max(self.state.max_balance_algo, self.state.current_balance_algo)
            
            # IMPORTANT: Auto-adjust max_total_positions if we imported more than allowed
            # This prevents the bot from being stuck unable to trade
            if imported >= self.config.max_total_positions:
                old_max = self.config.max_total_positions
                # Set to imported + headroom for new trades
                new_max = imported + 5
                self.config.max_total_positions = new_max
                log_warning(f"  ⚠️  Imported {imported} positions but max was {old_max}")
                log_success(f"  ✓ Auto-adjusted max positions: {old_max} → {new_max}")
                log_info(f"  ✓ Bot can now open {new_max - imported} new positions")
        
        if skipped_dust > 0:
            log_info(f"  Skipped {skipped_dust} dust positions (< {self.config.min_position_value_algo} ALGO)")
        if skipped_no_price > 0:
            log_info(f"  Skipped {skipped_no_price} assets with no price data")
        
        return imported
    
    def find_opportunities(self) -> List[Dict]:
        """
        Find trading opportunities based on current strategy.
        
        For LLM strategy (100% AI):
        1. Fast math-based screening (no LLM) -> finds ~15 candidates
        2. Single LLM call analyzes all candidates at once -> picks top 3-5
        
        For AI-assisted hybrid strategies:
        1. Run full mathematical strategy analysis
        2. Send top candidates to LLM for confirmation/filtering
        3. Return only LLM-confirmed signals
        
        For pure mathematical strategies:
        Scans ALL ASAs with liquidity on Algorand via Vestige API.
        """
        # RUG.NINJA STRATEGIES - Route FIRST before ASA scanning
        if self.config.strategy in [TradingStrategy.RUG_NINJA_SNIPER, TradingStrategy.RUG_NINJA_GRADUATED]:
            return self._find_rug_ninja_opportunities()
        
        # ALPHA ARCADE STRATEGIES - Route FIRST before ASA scanning
        if self.config.strategy in [TradingStrategy.ALPHA_ARCADE_VALUE, TradingStrategy.ALPHA_ARCADE_MOMENTUM]:
            return self._find_alpha_arcade_opportunities()
        
        # For all other strategies, scan ASAs
        log_info(f"Scanning Algorand ASAs via Vestige...")
        
        # Get ALL tradeable assets (or up to max_assets_to_scan)
        if self.config.scan_all_liquid_asas:
            assets = self.api.get_all_liquid_assets(
                min_volume=self.config.min_volume_24h,
                min_tvl=self.config.min_liquidity,
                max_assets=self.config.max_assets_to_scan
            )
        else:
            # Legacy: just get top 50 by volume
            assets_data = self.api.search_assets(
                min_volume=self.config.min_volume_24h,
                min_tvl=self.config.min_liquidity,
                limit=50
            )
            assets = assets_data.get("results", []) if assets_data else []
        
        if not assets:
            log_warning("No tradeable ASAs found meeting criteria")
            return []
        
        log_info(f"Found {len(assets)} ASAs to analyze")
        
        # PURE LLM STRATEGY (100% AI): Use smart 2-phase approach
        if self.config.strategy == TradingStrategy.LLM_ASSISTED:
            log_info(f"Phase 1: Fast screening {len(assets)} ASAs (no LLM)...")
            candidates = self._screen_candidates_fast(assets)
            log_success(f"Found {len(candidates)} promising candidates")
            
            if not candidates:
                return []
            
            log_info(f"Phase 2: LLM analyzing top {len(candidates)} candidates...")
            opportunities = self._llm_batch_analysis(candidates)
            log_success(f"LLM selected {len(opportunities)} trading opportunities")
            return opportunities
        
        # AI-ASSISTED HYBRID STRATEGIES: Math analysis + LLM confirmation
        ai_hybrid_strategies = {
            TradingStrategy.MOMENTUM_AI: TradingStrategy.MOMENTUM,
            TradingStrategy.MEAN_REVERSION_AI: TradingStrategy.MEAN_REVERSION,
            TradingStrategy.BREAKOUT_AI: TradingStrategy.BREAKOUT,
            TradingStrategy.SCALPING_AI: TradingStrategy.SCALPING,
        }
        
        if self.config.strategy in ai_hybrid_strategies:
            base_strategy = ai_hybrid_strategies[self.config.strategy]
            strategy_name = base_strategy.value.replace("_", " ").title()
            
            log_info(f"Phase 1: Running {strategy_name} analysis on {len(assets)} ASAs...")
            
            # Temporarily switch to base strategy for analysis
            original_strategy = self.config.strategy
            self.config.strategy = base_strategy
            
            opportunities = []
            for i, asset in enumerate(assets):
                if asset["id"] == 0:
                    continue
                if asset.get("confidence", 0) < self.config.min_confidence:
                    continue
                if len(assets) > 20 and (i + 1) % 20 == 0:
                    log_info(f"  Analyzed {i + 1}/{len(assets)} assets...")
                
                opportunity = self._analyze_asset(asset)
                if opportunity:
                    # Store the original asset data for LLM context
                    opportunity["_asset_data"] = asset
                    opportunities.append(opportunity)
            
            # Restore original strategy
            self.config.strategy = original_strategy
            
            # Sort by score
            opportunities.sort(key=lambda x: x.get("score", 0), reverse=True)
            
            log_success(f"Found {len(opportunities)} {strategy_name} signals")
            
            if not opportunities:
                return []
            
            # Phase 2: LLM confirmation
            top_candidates = opportunities[:15]  # Send top 15 to LLM
            log_info(f"Phase 2: LLM confirming top {len(top_candidates)} {strategy_name} signals...")
            
            confirmed = self._ai_confirm_opportunities(top_candidates, strategy_name)
            log_success(f"LLM confirmed {len(confirmed)} trading opportunities")
            
            return confirmed
        
        # PURE MATHEMATICAL STRATEGIES: Analyze each asset individually
        log_info(f"Analyzing {len(assets)} ASAs for trading opportunities...")
        
        opportunities = []
        
        for i, asset in enumerate(assets):
            # Skip ALGO itself
            if asset["id"] == 0:
                continue
            
            # Check confidence threshold
            if asset.get("confidence", 0) < self.config.min_confidence:
                continue
            
            # Progress indicator for large scans
            if len(assets) > 20 and (i + 1) % 20 == 0:
                log_info(f"  Analyzed {i + 1}/{len(assets)} assets...")
            
            opportunity = self._analyze_asset(asset)
            if opportunity:
                opportunities.append(opportunity)
        
        # Sort by score
        opportunities.sort(key=lambda x: x.get("score", 0), reverse=True)
        
        # === TECHNICAL ANALYSIS ENHANCEMENT ===
        # Apply TA scoring to top candidates (if enabled and we have history)
        if self.use_ta and opportunities:
            log_info("📊 Applying technical analysis enhancement...")
            enhanced_opportunities = []
            
            for opp in opportunities[:20]:  # TA on top 20 candidates
                enhanced = self.get_ta_enhanced_score(opp)
                enhanced_opportunities.append(enhanced)
                
                # Update price history for future TA
                asset_id = opp.get('asset_id')
                if asset_id and opp.get('current_price'):
                    self.update_price_history(asset_id, opp['current_price'], 
                                             opp.get('volume_24h', 0))
            
            # Add remaining opportunities (unenhanced)
            enhanced_opportunities.extend(opportunities[20:])
            
            # Re-sort by new scores
            opportunities = sorted(enhanced_opportunities, 
                                  key=lambda x: x.get("score", 0), reverse=True)
            
            # === TA-TRIGGERED SELL SIGNALS FOR HELD POSITIONS ===
            # Check if TA suggests selling any positions we hold
            held_asset_ids = set(self.state.positions.keys())
            ta_sell_signals = []
            
            # Debug: log held positions for TA sell check
            if held_asset_ids:
                log_info(f"📊 Checking TA sell signals for {len(held_asset_ids)} held positions...")
            
            for opp in enhanced_opportunities:
                asset_id = opp.get('asset_id')
                ta_signal = opp.get('ta_signal', {})
                ta_suggestion = ta_signal.get('signal', 'HOLD')
                
                # If TA says SELL and we hold this asset, generate a SELL signal
                if asset_id in held_asset_ids:
                    if ta_suggestion == 'SELL':
                        pos = self.state.positions[asset_id]
                        
                        # Skip imported positions if user didn't consent to managing them
                        if getattr(pos, "is_imported", False) and not self.config.manage_imported_positions:
                            log_info(f"📊 TA suggests SELL for {pos.asset_name} but it's imported (not managed)")
                            continue
                        
                        ta_reasons = opp.get('ta_reasons', [])
                        
                        sell_opp = {
                            "asset_id": asset_id,
                            "asset_name": pos.asset_name,
                            "signal": "SELL",
                            "score": 80,  # High priority for TA-based sells
                            "current_price": opp.get('current_price', pos.current_price),
                            "reason": f"TA SELL signal: {', '.join(ta_reasons[:2])}",
                            "ta_signal": ta_signal,
                            "ta_triggered": True
                        }
                        ta_sell_signals.append(sell_opp)
                        log_success(f"📊 TA SELL signal generated: {pos.asset_name} (RSI: {ta_signal.get('indicators', {}).get('rsi', 'N/A')})")
                    elif ta_suggestion == 'BUY':
                        # TA confirms we should hold/add to position
                        pass
            
            # Add TA sell signals to opportunities
            if ta_sell_signals:
                opportunities.extend(ta_sell_signals)
                log_info(f"📊 Added {len(ta_sell_signals)} TA-triggered SELL signal(s)")
            
            # Log TA adjustments for top opportunities
            ta_adjusted = [o for o in opportunities[:5] if o.get('ta_adjustment')]
            if ta_adjusted:
                for o in ta_adjusted:
                    adj = o.get('ta_adjustment', 0)
                    reasons = o.get('ta_reasons', [])
                    if adj != 0:
                        direction = "↑" if adj > 0 else "↓"
                        log_info(f"  {o.get('asset_name', '?')}: {direction}{abs(adj):.0f} pts ({', '.join(reasons[:2])})")
            
            # === CHECK ALL HELD POSITIONS FOR TA SELL SIGNALS ===
            # Some positions may not be in the opportunity scan (low volume, etc.)
            # but we should still check their TA for potential sells
            scanned_asset_ids = set(opp.get('asset_id') for opp in enhanced_opportunities)
            for asset_id, pos in self.state.positions.items():
                if asset_id in scanned_asset_ids:
                    continue  # Already checked above
                
                # Skip imported positions if not managing them
                if getattr(pos, "is_imported", False) and not self.config.manage_imported_positions:
                    continue
                
                # Get price history for this position
                prices = self.price_history.get_prices(asset_id)
                if len(prices) >= 30:
                    ta_signal = TechnicalAnalysis.generate_signal(prices, None)
                    if ta_signal.get('signal') == 'SELL' and ta_signal.get('strength', 0) > 50:
                        sell_opp = {
                            "asset_id": asset_id,
                            "asset_name": pos.asset_name,
                            "signal": "SELL",
                            "score": 75,
                            "current_price": pos.current_price,
                            "reason": f"TA SELL (off-scan): {', '.join(ta_signal.get('reasons', [])[:2])}",
                            "ta_signal": ta_signal,
                            "ta_triggered": True
                        }
                        opportunities.append(sell_opp)
                        log_success(f"📊 TA SELL for off-scan position: {pos.asset_name}")
        
        # Filter opportunities based on current positions
        filtered_opportunities = self._filter_actionable_opportunities(opportunities)
        
        # Diagnostic logging
        if len(opportunities) == 0:
            strategy_name = self.config.strategy.value.replace("_", " ").title()
            log_info(f"📊 {strategy_name} found no signals in {len(assets)} ASAs (market may be too quiet)")
        elif len(opportunities) > 0 and len(filtered_opportunities) == 0:
            log_info(f"📊 Found {len(opportunities)} potential signals, but all filtered out")
            log_info(f"   (Already held: {len(self.state.positions)}/{self.config.max_total_positions} positions)")
        
        log_info(f"Found {len(filtered_opportunities)} actionable trading opportunities")
        
        # === SCAN RUG.NINJA ALONGSIDE ASA (if enabled but not main strategy) ===
        if self.config.rug_ninja_enabled and self.config.strategy not in [TradingStrategy.RUG_NINJA_SNIPER, TradingStrategy.RUG_NINJA_GRADUATED]:
            try:
                log_info("🥷 Also scanning rug.ninja tokens (enabled alongside ASA)...")
                rn_opportunities = self._find_rug_ninja_opportunities()
                if rn_opportunities:
                    log_success(f"🥷 Found {len(rn_opportunities)} rug.ninja opportunities")
                    # Add to existing opportunities, but limit rug.ninja to not dominate
                    filtered_opportunities.extend(rn_opportunities[:3])
            except Exception as e:
                log_warning(f"🥷 Rug.ninja scan failed: {e}")
        
        # === SCAN ALPHA ARCADE ALONGSIDE ASA (if enabled but not main strategy) ===
        if self.config.alpha_arcade_enabled and self.config.strategy not in [TradingStrategy.ALPHA_ARCADE_VALUE, TradingStrategy.ALPHA_ARCADE_MOMENTUM]:
            try:
                log_info("🎯 Also scanning AlphaArcade markets (enabled alongside ASA)...")
                aa_opportunities = self._find_alpha_arcade_opportunities()
                if aa_opportunities:
                    log_success(f"🎯 Found {len(aa_opportunities)} AlphaArcade opportunities")
                    # Add to existing opportunities
                    filtered_opportunities.extend(aa_opportunities[:3])
            except Exception as e:
                log_warning(f"🎯 AlphaArcade scan failed: {e}")
        
        return filtered_opportunities[:10]  # Top 10 opportunities
    
    def _filter_actionable_opportunities(self, opportunities: List[Dict]) -> List[Dict]:
        """
        Filter opportunities to only show actionable signals with profit enhancement filters:
        - BUY signals: Only for assets NOT already held
        - SELL signals: Only for assets we currently hold
        - Anti-FOMO: Skip assets that pumped too much recently
        - Daily limits: Respect max daily trades and loss limits
        - Cooldown: Wait after losses before buying
        - Volume confirmation: Require adequate volume
        """
        filtered = []
        held_asset_ids = set(self.state.positions.keys())
        now = datetime.now()
        
        # Reset daily stats if new day
        self.state.reset_daily_stats()
        
        # Track which held assets we've seen SELL signals for
        sell_signals_for_held = set()
        
        # === CHECK DAILY LIMITS ===
        daily_limit_reached = False
        if self.config.max_daily_trades > 0 and self.state.daily_trades >= self.config.max_daily_trades:
            log_warning(f"📊 Daily trade limit reached ({self.state.daily_trades}/{self.config.max_daily_trades})")
            daily_limit_reached = True
        
        if self.config.max_daily_loss_algo > 0 and self.state.daily_pnl_algo < -self.config.max_daily_loss_algo:
            log_warning(f"📊 Daily loss limit reached ({self.state.daily_pnl_algo:.2f} ALGO)")
            daily_limit_reached = True
        
        # === CHECK COOLDOWN AFTER LOSS ===
        in_cooldown = False
        if self.config.cooldown_after_loss_minutes > 0 and self.state.last_loss_time:
            cooldown_elapsed = (now - self.state.last_loss_time).total_seconds() / 60
            if cooldown_elapsed < self.config.cooldown_after_loss_minutes:
                remaining = self.config.cooldown_after_loss_minutes - cooldown_elapsed
                log_info(f"⏳ Cooldown after loss: {remaining:.0f} minutes remaining")
                in_cooldown = True
        
        # === CHECK WIN RATE ===
        win_rate_too_low = False
        if self.config.min_win_rate > 0 and self.state.total_trades >= self.config.min_trades_for_win_rate:
            if self.state.win_rate < self.config.min_win_rate:
                log_warning(f"📊 Win rate too low: {self.state.win_rate*100:.1f}% < {self.config.min_win_rate*100:.1f}%")
                win_rate_too_low = True
        
        for opp in opportunities:
            asset_id = opp.get("asset_id")
            signal = opp.get("signal", "BUY")
            
            if signal == "BUY":
                # Only show BUY if we don't already hold it
                if asset_id in held_asset_ids:
                    continue
                
                # Skip buys if daily limits reached, in cooldown, or win rate too low
                if daily_limit_reached or in_cooldown or win_rate_too_low:
                    continue
                
                # === ANTI-FOMO FILTER ===
                if self.config.anti_fomo_enabled:
                    price_change_1h = opp.get("price_change_1h", opp.get("price_change_pct", 0))
                    price_change_24h = opp.get("price_change_24h", opp.get("price_change_pct", 0))
                    
                    # Skip if pumped too much in 1h
                    if price_change_1h > self.config.anti_fomo_max_1h_pump:
                        log_info(f"🚫 Anti-FOMO: Skipping {opp.get('asset_name', asset_id)} (+{price_change_1h:.1f}% in 1h)")
                        continue
                    
                    # Skip if pumped too much in 24h
                    if price_change_24h > self.config.anti_fomo_max_24h_pump:
                        log_info(f"🚫 Anti-FOMO: Skipping {opp.get('asset_name', asset_id)} (+{price_change_24h:.1f}% in 24h)")
                        continue
                
                # === VOLUME CONFIRMATION ===
                if self.config.volume_confirmation_enabled:
                    volume_ratio = opp.get("volume_ratio", 1.0)
                    if volume_ratio < self.config.min_volume_increase:
                        # Volume too low, reduce score significantly
                        opp["score"] = opp.get("score", 50) * 0.5
                        opp["reason"] = opp.get("reason", "") + " (low volume)"
                
                # === PULLBACK REQUIREMENT ===
                if self.config.require_pullback:
                    # Check if price is below recent high
                    high_24h = opp.get("high_24h", 0)
                    current_price = opp.get("current_price", 0)
                    if high_24h > 0 and current_price > 0:
                        pullback_pct = ((high_24h - current_price) / high_24h) * 100
                        if pullback_pct < self.config.pullback_percent:
                            log_info(f"📉 Waiting for pullback: {opp.get('asset_name', asset_id)} (need {self.config.pullback_percent}% dip, at {pullback_pct:.1f}%)")
                            # Add to dip watch list instead of trading
                            self._add_to_dip_watch(opp)
                            continue
                
                filtered.append(opp)
            
            elif signal == "SELL":
                # Only show SELL if we hold it (sells always allowed, no limits)
                if asset_id in held_asset_ids:
                    filtered.append(opp)
                    sell_signals_for_held.add(asset_id)
        
        # Check held positions for potential SELL signals not already captured
        for asset_id, pos in self.state.positions.items():
            if asset_id in sell_signals_for_held:
                continue
            
            # Check if position is showing negative momentum
            if pos.unrealized_pnl_percent < -5:
                filtered.append({
                    "asset_id": asset_id,
                    "asset_name": pos.asset_name,
                    "signal": "SELL",
                    "score": abs(pos.unrealized_pnl_percent) * 5,
                    "current_price": pos.current_price,
                    "price_change_pct": pos.unrealized_pnl_percent,
                    "volume_ratio": 0,
                    "reason": f"Position down {pos.unrealized_pnl_percent:.1f}% - consider selling"
                })
        
        # Check dip watch list for opportunities
        self._check_dip_watch_list(filtered)
        
        # Re-sort by score
        filtered.sort(key=lambda x: x.get("score", 0), reverse=True)
        
        return filtered
    
    def _add_to_dip_watch(self, opp: Dict):
        """Add an opportunity to the dip watch list."""
        asset_id = opp.get("asset_id")
        if asset_id not in self.state.dip_watch_list:
            self.state.dip_watch_list[asset_id] = {
                "opp": opp,
                "added_time": datetime.now(),
                "high_price": opp.get("current_price", 0),
                "target_price": opp.get("current_price", 0) * (1 - self.config.dip_percent / 100)
            }
            log_info(f"👀 Watching for dip: {opp.get('asset_name', asset_id)} (target: ${self.state.dip_watch_list[asset_id]['target_price']:.6f})")
    
    def _check_dip_watch_list(self, filtered: List[Dict]):
        """Check if any watched assets have dipped enough to buy."""
        now = datetime.now()
        to_remove = []
        
        for asset_id, watch in list(self.state.dip_watch_list.items()):
            # Check timeout
            age_minutes = (now - watch["added_time"]).total_seconds() / 60
            if age_minutes > self.config.dip_timeout_minutes:
                to_remove.append(asset_id)
                log_info(f"⏰ Dip watch timeout: {watch['opp'].get('asset_name', asset_id)}")
                continue
            
            # Get current price
            price = self.api.get_asset_price(asset_id)
            if price is None:
                continue
            
            # Check if dip target reached
            if price <= watch["target_price"]:
                log_success(f"📉 Dip detected! {watch['opp'].get('asset_name', asset_id)} hit target ${price:.6f}")
                # Add to opportunities with bonus score
                opp = watch["opp"].copy()
                opp["current_price"] = price
                opp["score"] = opp.get("score", 50) + 20  # Bonus for buying the dip
                opp["reason"] = opp.get("reason", "") + " (bought the dip!)"
                filtered.append(opp)
                to_remove.append(asset_id)
        
        # Clean up expired watches
        for asset_id in to_remove:
            del self.state.dip_watch_list[asset_id]
    
    def _find_rug_ninja_opportunities(self) -> List[Dict]:
        """
        Find trading opportunities on rug.ninja (Algorand's pump.fun).
        
        Sniper mode: Buy newly minted tokens on bonding curve
        - API Scanning: Poll for recent mints
        - Real-time: Stream blocks for instant sniping (garbage-cat style)
        Graduated mode: Trade tokens that have graduated to DEX
        """
        opportunities = []
        
        is_sniper = self.config.strategy == TradingStrategy.RUG_NINJA_SNIPER
        is_graduated = self.config.strategy == TradingStrategy.RUG_NINJA_GRADUATED
        
        # Check for real-time sniper mode
        if is_sniper and getattr(self.config, 'rug_ninja_realtime_sniper', False):
            # Real-time sniping is handled by the RugNinjaMintSniper class
            # running in a separate thread in AlgorandTradingBot
            log_info("🥷 Real-time sniper mode active - purchases happen automatically in background")
            log_info("🥷 Checking for existing positions to potentially sell...")
            # Continue to scan for existing positions we might want to sell
        
        # Determine bond progress filter based on mode
        if is_sniper:
            # Sniper: Look for tokens still on bonding curve (0-99% bonded)
            min_bond = self.config.rug_ninja_min_bond_progress
            max_bond = min(self.config.rug_ninja_max_bond_progress, 0.99)
            log_info(f"🥷 RUG.NINJA Sniper: Scanning new token mints ({min_bond*100:.0f}-{max_bond*100:.0f}% bonded)...")
        else:
            # Graduated: Look for tokens that have graduated (100% bonded)
            min_bond = 1.0
            max_bond = 1.0
            log_info(f"🥷 RUG.NINJA Graduated: Scanning graduated tokens (100% bonded)...")
        
        # Scan for rug.ninja tokens
        tokens = scan_rug_ninja_tokens(
            min_bond_progress=min_bond,
            max_bond_progress=max_bond,
            min_volume=self.config.min_volume_24h / 10,  # Lower volume threshold for rug.ninja
            max_age_minutes=self.config.rug_ninja_max_age_minutes if is_sniper else 0,
            limit=50
        )
        
        if not tokens:
            log_info("🥷 No rug.ninja tokens found matching criteria")
            return []
        
        log_info(f"🥷 Found {len(tokens)} rug.ninja tokens, analyzing...")
        
        for token in tokens:
            # Basic analysis
            analysis = analyze_rug_ninja_opportunity(token, self.config)
            
            # Skip if score too low
            if analysis["score"] < 40:
                continue
            
            # AI risk assessment if available
            risk_model = get_llm_for_task(self.config, "risk")
            if risk_model:
                risk_assessment = ai_assess_rug_risk(token, risk_model)
                if risk_assessment:
                    risk_level = risk_assessment.get("risk_level", "unknown")
                    
                    # Adjust score based on AI risk assessment
                    if risk_level == "extreme":
                        analysis["score"] -= 40
                        analysis["risks"].append(f"AI: Extreme risk - {risk_assessment.get('reasoning', '')}")
                    elif risk_level == "high":
                        analysis["score"] -= 20
                        analysis["risks"].append(f"AI: High risk")
                    elif risk_level == "low":
                        analysis["score"] += 10
                        analysis["reasons"].append("AI: Low risk assessment")
                    
                    analysis["ai_risk_assessment"] = risk_assessment
            
            # Check if we already hold this
            if token["asset_id"] in self.state.positions:
                # Check if we should sell on bond completion
                if self.config.rug_ninja_auto_sell_on_bond and token["is_bonded"]:
                    analysis["signal"] = "SELL"
                    analysis["reasons"].append("Token graduated - auto-sell triggered")
                else:
                    continue  # Skip, already holding
            
            if analysis["signal"] == "BUY" and analysis["score"] >= 50:
                opportunities.append({
                    "asset_id": analysis["asset_id"],
                    "asset_name": analysis["asset_name"],
                    "signal": "BUY",
                    "score": analysis["score"],
                    "current_price": analysis["current_price"],
                    "bond_progress": analysis["bond_progress"],
                    "reason": f"Rug.ninja: {', '.join(analysis['reasons'][:2])}",
                    "risks": analysis.get("risks", []),
                    "is_rug_ninja": True,
                    "max_buy_algo": self.config.rug_ninja_max_buy_algo,
                })
            elif analysis["signal"] == "SELL":
                opportunities.append({
                    "asset_id": analysis["asset_id"],
                    "asset_name": analysis["asset_name"],
                    "signal": "SELL",
                    "score": analysis["score"],
                    "current_price": analysis["current_price"],
                    "reason": f"Rug.ninja: {', '.join(analysis['reasons'][:2])}",
                    "is_rug_ninja": True,
                })
        
        # Sort by score
        opportunities.sort(key=lambda x: x.get("score", 0), reverse=True)
        
        # Filter actionable
        filtered = self._filter_actionable_opportunities(opportunities)
        
        log_success(f"🥷 Found {len(filtered)} actionable rug.ninja opportunities")
        
        return filtered[:10]
    
    def _find_alpha_arcade_opportunities(self) -> List[Dict]:
        """
        Find trading opportunities on AlphaArcade (Algorand prediction market).
        
        Uses the AlphaArcade Partner API to scan prediction markets.
        Requires an API key configured in alpha_arcade_api_key.
        
        Docs: https://alphaarcade.gitbook.io/alphaarcade-docs
        """
        is_value = self.config.strategy == TradingStrategy.ALPHA_ARCADE_VALUE
        is_momentum = self.config.strategy == TradingStrategy.ALPHA_ARCADE_MOMENTUM
        
        mode = "value" if is_value else "momentum"
        log_info(f"🎯 ALPHA ARCADE {mode.title()}: Scanning prediction markets...")
        
        # Check for API key
        api_key = self.config.alpha_arcade_api_key if hasattr(self.config, 'alpha_arcade_api_key') else ""
        
        if not api_key:
            log_warning("🎯 AlphaArcade: No API key configured")
            log_info("🎯 Set alpha_arcade_api_key in config to enable market scanning")
            log_info("🎯 Get a partner API key from the AlphaArcade team")
            log_info("🎯 Docs: https://alphaarcade.gitbook.io/alphaarcade-docs")
            log_info("🎯 Tip: You can trade $ALPHA token (ASA 2726252423) via regular strategies")
            return []
        
        # Scan for AlphaArcade markets using Partner API
        markets = scan_alpha_arcade_markets(
            min_volume=self.config.alpha_arcade_min_volume,
            min_liquidity=self.config.alpha_arcade_min_liquidity,
            categories=self.config.alpha_arcade_categories,
            active_only=True,
            limit=50,
            api_key=api_key
        )
        
        if not markets:
            log_info("🎯 No AlphaArcade markets found matching criteria")
            return []
        
        log_info(f"🎯 Found {len(markets)} markets, analyzing for {mode} opportunities...")
        
        opportunities = []
        
        for market in markets:
            # Basic analysis
            analysis = analyze_alpha_arcade_opportunity(market, self.config, mode=mode)
            
            # Skip if score too low
            if analysis["score"] < 40:
                continue
            
            # AI analysis if available
            strategy_model = get_llm_for_task(self.config, "strategy")
            if strategy_model:
                ai_analysis = ai_analyze_alpha_arcade_market(market, strategy_model)
                if ai_analysis:
                    recommendation = ai_analysis.get("recommendation", "SKIP")
                    confidence = ai_analysis.get("confidence", 0)
                    
                    # Adjust score based on AI analysis
                    if recommendation == "SKIP" or confidence < 0.4:
                        analysis["score"] -= 20
                        analysis["risks"].append("AI recommends skipping")
                    elif recommendation in ["YES", "NO"]:
                        analysis["score"] += int(confidence * 20)
                        analysis["reasons"].append(f"AI: {recommendation} with {confidence:.0%} confidence")
                        if analysis["recommended_position"] and recommendation != analysis["recommended_position"]:
                            analysis["risks"].append("AI disagrees with technical analysis")
                        else:
                            analysis["recommended_position"] = recommendation
                    
                    analysis["ai_analysis"] = ai_analysis
            
            # Check if we should take a position
            if analysis["signal"] == "BUY" and analysis["score"] >= 50 and analysis["recommended_position"]:
                # Determine which asset ID to trade based on position
                if analysis["recommended_position"] == "YES":
                    asset_id = market.get("yes_asset_id")
                    asset_name = f"🎯 {market.get('title', 'Unknown')[:35]}... (YES)"
                    current_price = analysis["yes_price"]
                else:
                    asset_id = market.get("no_asset_id")
                    asset_name = f"🎯 {market.get('title', 'Unknown')[:35]}... (NO)"
                    current_price = analysis["no_price"]
                
                if asset_id:
                    opportunities.append({
                        "asset_id": asset_id,
                        "asset_name": asset_name,
                        "signal": "BUY",
                        "score": analysis["score"],
                        "current_price": current_price,
                        "position_type": analysis["recommended_position"],
                        "market_id": analysis["market_id"],
                        "market_app_id": market.get("market_app_id"),
                        "question": market.get("title", analysis.get("question", "Unknown")),
                        "category": market.get("categories", ["general"])[0] if market.get("categories") else "general",
                        "reason": f"AlphaArcade {mode}: {', '.join(analysis['reasons'][:2])}",
                        "risks": analysis.get("risks", []),
                        "is_alpha_arcade": True,
                        "max_buy_algo": self.config.alpha_arcade_max_bet_algo,
                        "volume": market.get("volume", 0),
                        "spread": market.get("spread", 0),
                        "end_ts": market.get("end_ts"),
                        "total_rewards": market.get("total_rewards", 0),
                    })
        
        # Sort by score
        opportunities.sort(key=lambda x: x.get("score", 0), reverse=True)
        
        # Filter actionable
        filtered = self._filter_actionable_opportunities(opportunities)
        
        log_success(f"🎯 Found {len(filtered)} actionable AlphaArcade opportunities")
        
        return filtered[:10]
    
    def _analyze_asset(self, asset: Dict) -> Optional[Dict]:
        """Analyze a single asset based on the selected strategy."""
        asset_id = asset["id"]
        
        # Get recent candles for technical analysis
        now = int(time.time())
        start = now - (self.config.momentum_lookback_hours * 3600)
        candles = self.api.get_asset_candles(
            asset_id=asset_id,
            interval=3600,  # 1 hour candles
            start=start,
            end=now
        )
        
        if self.config.strategy == TradingStrategy.MOMENTUM:
            return self._momentum_analysis(asset, candles)
        elif self.config.strategy == TradingStrategy.MEAN_REVERSION:
            return self._mean_reversion_analysis(asset, candles)
        elif self.config.strategy == TradingStrategy.BREAKOUT:
            return self._breakout_analysis(asset, candles)
        elif self.config.strategy == TradingStrategy.SCALPING:
            return self._scalping_analysis(asset, candles)
        elif self.config.strategy == TradingStrategy.LLM_ASSISTED:
            return self._llm_analysis(asset, candles)
        else:
            return self._momentum_analysis(asset, candles)
    
    def _momentum_analysis(self, asset: Dict, candles: List[Dict]) -> Optional[Dict]:
        """Momentum strategy: Buy assets with strong upward momentum."""
        if not candles or len(candles) < 2:
            return None
        
        # Calculate price change
        current_price = asset.get("price", 0)
        price_24h_ago = asset.get("price1d", current_price)
        
        if price_24h_ago <= 0:
            return None
        
        price_change_pct = ((current_price / price_24h_ago) - 1) * 100
        
        # Calculate volume change
        volume_24h = asset.get("volume1d", 0)
        volume_7d = asset.get("volume7d", 0)
        avg_daily_volume = volume_7d / 7 if volume_7d > 0 else 0
        volume_ratio = volume_24h / avg_daily_volume if avg_daily_volume > 0 else 1
        
        # Momentum score
        if price_change_pct > self.config.momentum_threshold:
            signal = "BUY"
            score = min(100, price_change_pct * 5 + volume_ratio * 10)
        elif price_change_pct < -self.config.momentum_threshold:
            signal = "SELL"
            score = min(100, abs(price_change_pct) * 5)
        else:
            return None
        
        return {
            "asset_id": asset["id"],
            "asset_name": f"{asset.get('name', 'Unknown')} ({asset.get('ticker', 'N/A')})",
            "signal": signal,
            "score": score,
            "current_price": current_price,
            "price_change_pct": price_change_pct,
            "volume_ratio": volume_ratio,
            "reason": f"Momentum: {price_change_pct:.2f}% price change, {volume_ratio:.1f}x volume"
        }
    
    def _mean_reversion_analysis(self, asset: Dict, candles: List[Dict]) -> Optional[Dict]:
        """Mean reversion strategy: Buy oversold, sell overbought."""
        if not candles or len(candles) < 10:
            return None
        
        # Calculate price statistics
        closes = [c["close"] for c in candles]
        mean_price = np.mean(closes)
        std_price = np.std(closes)
        
        current_price = asset.get("price", 0)
        
        if std_price <= 0:
            return None
        
        z_score = (current_price - mean_price) / std_price
        
        if z_score < -self.config.mean_reversion_std_multiplier:
            signal = "BUY"
            score = min(100, abs(z_score) * 30)
            reason = f"Oversold: {z_score:.2f} std below mean"
        elif z_score > self.config.mean_reversion_std_multiplier:
            signal = "SELL"
            score = min(100, abs(z_score) * 30)
            reason = f"Overbought: {z_score:.2f} std above mean"
        else:
            return None
        
        return {
            "asset_id": asset["id"],
            "asset_name": f"{asset.get('name', 'Unknown')} ({asset.get('ticker', 'N/A')})",
            "signal": signal,
            "score": score,
            "current_price": current_price,
            "z_score": z_score,
            "mean_price": mean_price,
            "reason": reason
        }
    
    def _breakout_analysis(self, asset: Dict, candles: List[Dict]) -> Optional[Dict]:
        """Breakout strategy: Buy on volume breakout above resistance."""
        if not candles or len(candles) < 20:
            return None
        
        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        volumes = [c["volume"] for c in candles]
        
        current_price = asset.get("price", 0)
        recent_high = max(highs[-20:])
        avg_volume = np.mean(volumes[:-1])  # Exclude current
        current_volume = asset.get("volume1d", 0)
        
        volume_breakout = current_volume > avg_volume * self.config.breakout_volume_multiplier
        price_breakout = current_price > recent_high
        
        if price_breakout and volume_breakout:
            signal = "BUY"
            score = 70 + (current_volume / avg_volume - self.config.breakout_volume_multiplier) * 10
            reason = f"Breakout: New high {current_price:.8f}, {current_volume/avg_volume:.1f}x volume"
        else:
            return None
        
        return {
            "asset_id": asset["id"],
            "asset_name": f"{asset.get('name', 'Unknown')} ({asset.get('ticker', 'N/A')})",
            "signal": signal,
            "score": min(100, score),
            "current_price": current_price,
            "breakout_level": recent_high,
            "volume_multiplier": current_volume / avg_volume if avg_volume > 0 else 0,
            "reason": reason
        }
    
    def _scalping_analysis(self, asset: Dict, candles: List[Dict]) -> Optional[Dict]:
        """Scalping strategy: Quick small profits on volatility."""
        if not candles or len(candles) < 5:
            return None
        
        # Calculate recent volatility
        closes = [c["close"] for c in candles[-5:]]
        volatility = np.std(closes) / np.mean(closes) * 100 if np.mean(closes) > 0 else 0
        
        current_price = asset.get("price", 0)
        
        # Need minimum volatility for scalping (lowered from 1.0%)
        if volatility < 0.5:  # Less than 0.5% volatility
            return None
        
        # Check for quick reversal opportunity
        price_change_1h = ((current_price / closes[-2]) - 1) * 100 if closes[-2] > 0 else 0
        price_change_short = ((current_price / closes[-1]) - 1) * 100 if closes[-1] > 0 else 0
        
        # Multiple scalping entry conditions (more flexible)
        signal = None
        reason = None
        score = 0
        
        if price_change_1h < -1.0:  # Down more than 1.0% in last hour (lowered from 1.5%)
            signal = "BUY"
            score = min(100, volatility * 20 + abs(price_change_1h) * 10)
            reason = f"Scalp dip: {price_change_1h:.2f}% drop, {volatility:.2f}% volatility"
        elif price_change_short < -0.5 and volatility > 1.0:  # Quick dip with high volatility
            signal = "BUY"
            score = min(80, volatility * 15)
            reason = f"Scalp volatility: {price_change_short:.2f}% quick dip, {volatility:.2f}% vol"
        elif volatility > 2.0 and price_change_1h > -0.5 and price_change_1h < 0.5:  # High vol, stable price (range scalp)
            signal = "BUY"
            score = min(70, volatility * 10)
            reason = f"Range scalp: {volatility:.2f}% volatility, stable price"
        
        if not signal:
            return None
        
        return {
            "asset_id": asset["id"],
            "asset_name": f"{asset.get('name', 'Unknown')} ({asset.get('ticker', 'N/A')})",
            "signal": signal,
            "score": score,
            "current_price": current_price,
            "volatility": volatility,
            "target_profit": self.config.scalp_profit_target,
            "reason": reason
        }
    
    def _llm_analysis(self, asset: Dict, candles: List[Dict]) -> Optional[Dict]:
        """
        LLM-assisted analysis - but this is only called for pre-screened candidates.
        The main LLM analysis happens in find_opportunities() for efficiency.
        """
        # For individual asset analysis, just use momentum as pre-filter
        # The real LLM magic happens in the batch analysis
        return self._momentum_analysis(asset, candles)
    
    def _screen_candidates_fast(self, assets: List[Dict]) -> List[Dict]:
        """
        Fast math-based screening to find candidates for LLM analysis.
        No LLM calls here - pure speed.
        """
        candidates = []
        
        for asset in assets:
            if asset["id"] == 0:  # Skip ALGO
                continue
            
            if asset.get("confidence", 0) < self.config.min_confidence:
                continue
            
            # Quick momentum/volume scoring (no API calls needed - data already in asset)
            current_price = asset.get("price", 0)
            price_24h_ago = asset.get("price1d", current_price)
            
            if price_24h_ago <= 0 or current_price <= 0:
                continue
            
            # Calculate metrics
            price_change_pct = ((current_price / price_24h_ago) - 1) * 100
            volume_24h = asset.get("volume1d", 0)
            volume_7d = asset.get("volume7d", 0)
            avg_daily_volume = volume_7d / 7 if volume_7d > 0 else 0
            volume_ratio = volume_24h / avg_daily_volume if avg_daily_volume > 0 else 1
            tvl = asset.get("tvl", 0)
            
            # Score based on multiple factors
            score = 0
            
            # Momentum scoring
            if abs(price_change_pct) > 3:
                score += min(30, abs(price_change_pct) * 3)
            
            # Volume spike scoring
            if volume_ratio > 1.5:
                score += min(30, volume_ratio * 10)
            
            # Liquidity scoring (prefer liquid assets)
            if tvl > 10000:
                score += 20
            elif tvl > 5000:
                score += 10
            
            # Add to candidates if score is meaningful
            if score > 20:
                candidates.append({
                    "asset": asset,
                    "score": score,
                    "price_change_pct": price_change_pct,
                    "volume_ratio": volume_ratio,
                    "current_price": current_price
                })
        
        # Sort by score and return top candidates
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:15]  # Top 15 for LLM to analyze
    
    def _llm_batch_analysis(self, candidates: List[Dict]) -> List[Dict]:
        """
        Single LLM call to analyze all candidates at once.
        Much more efficient than calling LLM for each asset.
        """
        if not self.llm or not self.llm.available:
            log_warning("LLM not available, falling back to momentum scores")
            return self._convert_candidates_to_opportunities(candidates)
        
        if not candidates:
            return []
        
        log_info(f"Sending {len(candidates)} candidates to LLM for analysis...")
        
        # Prepare market data summary for LLM
        market_summary = self._prepare_candidates_summary(candidates)
        
        # Prepare current positions summary
        positions_summary = self._prepare_positions_summary()
        
        try:
            import ollama
            
            prompt = f"""You are an expert cryptocurrency trader analyzing Algorand ASAs.

CURRENT PORTFOLIO (assets I currently hold):
{positions_summary}

MARKET CANDIDATES ({len(candidates)} pre-screened assets):
{market_summary}

YOUR TASK:
1. FIRST: Check if any of my CURRENT POSITIONS should be SOLD based on market conditions
2. SECOND: Identify good BUY opportunities from the market candidates (only if not already holding)

IMPORTANT RULES:
- Only recommend SELL for assets I currently HOLD (listed in Current Portfolio)
- Only recommend BUY for assets I do NOT currently hold
- Check my held positions for negative momentum or concerning signals
- Prioritize protecting existing positions over new buys

Consider:
- Price momentum and direction
- Volume spikes (volume_ratio > 2 is significant)
- Risk/reward ratio
- Position P/L for held assets

Respond with ONLY valid JSON in this exact format:
{{
    "analysis_summary": "Brief 1-2 sentence market overview",
    "recommendations": [
        {{
            "asset_id": <integer>,
            "action": "BUY" or "SELL",
            "confidence": <0.0-1.0>,
            "reason": "Brief explanation",
            "priority": <1-5, where 1 is highest>
        }}
    ]
}}

Return 3-5 recommendations maximum. Prioritize SELL signals for held positions showing weakness.
Respond with ONLY the JSON object, no other text."""

            log_info("Waiting for LLM response (this may take 30-60 seconds)...")
            
            # Call LLM with timeout handling
            import concurrent.futures
            
            # Get the appropriate model for market analysis (uses multi-LLM if configured)
            market_model = self.llm._get_model_for_task("market")
            
            def call_llm():
                return ollama.chat(
                    model=market_model,
                    messages=[
                        {"role": "system", "content": "You are a trading analyst. Reply with JSON only."},
                        {"role": "user", "content": prompt}
                    ],
                    options={"temperature": 0.3, "num_predict": 2000}
                )
            
            # Use ThreadPoolExecutor for timeout
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(call_llm)
                try:
                    # 300 second (5 min) timeout for LLM response
                    response = future.result(timeout=300)
                except concurrent.futures.TimeoutError:
                    log_warning("LLM took too long (>5 min), using momentum scores instead")
                    return self._convert_candidates_to_opportunities(candidates)
            
            content = response["message"]["content"]
            log_success("LLM response received")
            
            # Parse JSON from response using robust parser
            result = parse_llm_json(content)
            
            if result:
                if "analysis_summary" in result:
                    log_info(f"LLM Analysis: {result['analysis_summary']}")
                
                return self._process_llm_recommendations(result, candidates)
            
            log_warning("Could not parse LLM response, using momentum scores")
            return self._convert_candidates_to_opportunities(candidates)
            
        except Exception as e:
            log_error(f"LLM batch analysis failed: {e}")
            return self._convert_candidates_to_opportunities(candidates)
    
    def _prepare_positions_summary(self) -> str:
        """Prepare a summary of current held positions for the LLM."""
        if not self.state.positions:
            return "No current positions (portfolio is empty)"
        
        lines = []
        for asset_id, pos in self.state.positions.items():
            # Get current price
            current_price = self.api.get_asset_price(asset_id)
            if current_price:
                current_value = pos.amount * current_price
                pnl = current_value - pos.total_invested
                pnl_pct = (pnl / pos.total_invested * 100) if pos.total_invested > 0 else 0
            else:
                current_value = pos.current_value if hasattr(pos, 'current_value') else 0
                pnl = 0
                pnl_pct = 0
            
            lines.append(
                f"- {pos.asset_name} (ID: {asset_id}): "
                f"Amount: {pos.amount:.4f}, "
                f"Invested: {pos.total_invested:.2f} ALGO, "
                f"Current Value: {current_value:.2f} ALGO, "
                f"P/L: {pnl:+.2f} ALGO ({pnl_pct:+.2f}%)"
            )
        
        return "\n".join(lines)
    
    def _ai_confirm_opportunities(self, opportunities: List[Dict], strategy_name: str) -> List[Dict]:
        """
        Use LLM to confirm/filter opportunities from mathematical strategies.
        This is the hybrid approach: Math finds signals, AI confirms them.
        """
        if not self.llm or not self.llm.available:
            log_warning("LLM not available, returning unconfirmed signals")
            return opportunities[:5]
        
        if not opportunities:
            return []
        
        # Prepare the opportunity summary for LLM
        opp_summary = self._prepare_opportunities_summary(opportunities)
        positions_summary = self._prepare_positions_summary()
        
        try:
            import ollama
            import concurrent.futures
            
            prompt = f"""You are an expert cryptocurrency trader reviewing {strategy_name} trading signals for Algorand ASAs.

A mathematical {strategy_name} strategy has identified these potential trades. Your job is to CONFIRM or REJECT each signal.

CURRENT PORTFOLIO (assets I currently hold):
{positions_summary}

{strategy_name.upper()} SIGNALS TO REVIEW ({len(opportunities)} candidates):
{opp_summary}

YOUR TASK:
1. Review each {strategy_name} signal critically
2. CONFIRM signals that look strong and well-supported
3. REJECT signals that seem weak, risky, or poorly timed
4. Check if any held positions should be SOLD (add new SELL signals if needed)

CONFIRMATION CRITERIA:
- For MOMENTUM: Confirm if trend is strong, volume supports the move
- For MEAN_REVERSION: Confirm if oversold/overbought is extreme enough
- For BREAKOUT: Confirm if breakout has sufficient volume and follow-through
- For SCALPING: Confirm if volatility and liquidity are adequate

IMPORTANT RULES:
- Only CONFIRM BUY signals for assets I do NOT already hold
- You can ADD new SELL signals for held positions showing weakness
- Be selective - confirm only the best 3-5 signals
- Reject marginal or borderline signals

Respond with ONLY valid JSON in this exact format:
{{
    "analysis_summary": "Brief assessment of the {strategy_name} signals",
    "confirmed_signals": [
        {{
            "asset_id": <integer>,
            "action": "BUY" or "SELL",
            "original_signal": "BUY" or "SELL" or "NEW" (if you're adding a SELL),
            "confidence": <0.0-1.0>,
            "reason": "Why confirmed/added",
            "priority": <1-5, where 1 is highest>
        }}
    ],
    "rejected_signals": [
        {{
            "asset_id": <integer>,
            "reason": "Why rejected"
        }}
    ]
}}

Return 3-5 confirmed signals maximum. Quality over quantity.
Respond with ONLY the JSON object, no other text."""

            log_info("Waiting for LLM confirmation (this may take 30-60 seconds)...")
            
            # Get the appropriate model for trade decisions (uses multi-LLM if configured)
            trade_model = self.llm._get_model_for_task("trade")
            
            def call_llm():
                return ollama.chat(
                    model=trade_model,
                    messages=[
                        {"role": "system", "content": "You are a trading analyst. Reply with JSON only."},
                        {"role": "user", "content": prompt}
                    ],
                    options={"temperature": 0.3, "num_predict": 2000}
                )
            
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(call_llm)
                try:
                    response = future.result(timeout=300)
                except concurrent.futures.TimeoutError:
                    log_warning("LLM took too long, returning unconfirmed signals")
                    return opportunities[:5]
            
            content = response["message"]["content"]
            log_success("LLM confirmation received")
            
            # Parse JSON from response using robust parser
            result = parse_llm_json(content)
            
            if result:
                if "analysis_summary" in result:
                    log_info(f"LLM Assessment: {result['analysis_summary']}")
                
                # Log rejections
                rejected = result.get("rejected_signals", [])
                if rejected:
                    log_info(f"LLM rejected {len(rejected)} signals")
                
                return self._process_confirmed_signals(result, opportunities)
            
            log_warning("Could not parse LLM response, returning unconfirmed signals")
            return opportunities[:5]
            
        except Exception as e:
            log_error(f"LLM confirmation failed: {e}")
            return opportunities[:5]
    
    def _prepare_opportunities_summary(self, opportunities: List[Dict]) -> str:
        """Prepare a summary of mathematical strategy opportunities for LLM review."""
        lines = []
        for i, opp in enumerate(opportunities, 1):
            asset_name = opp.get("asset_name", "Unknown")
            asset_id = opp.get("asset_id", 0)
            signal = opp.get("signal", "BUY")
            score = opp.get("score", 0)
            price = opp.get("current_price", 0)
            price_change = opp.get("price_change_pct", 0)
            volume_ratio = opp.get("volume_ratio", 1)
            reason = opp.get("reason", "No reason provided")
            
            # Check if we already hold this asset
            held = " [ALREADY HELD]" if asset_id in self.state.positions else ""
            
            lines.append(
                f"{i}. {asset_name}{held} [ID: {asset_id}]\n"
                f"   Signal: {signal} | Score: {score:.0f}/100\n"
                f"   Price: {price:.8f} ALGO | 24h Change: {price_change:+.2f}%\n"
                f"   Volume Ratio: {volume_ratio:.1f}x\n"
                f"   Reason: {reason}"
            )
        return "\n\n".join(lines)
    
    def _process_confirmed_signals(self, result: Dict, original_opportunities: List[Dict]) -> List[Dict]:
        """Process LLM confirmation results and return confirmed opportunities."""
        confirmed = []
        confirmed_signals = result.get("confirmed_signals", [])
        
        # Create lookup for original opportunities
        opp_lookup = {opp["asset_id"]: opp for opp in original_opportunities}
        
        for sig in confirmed_signals:
            asset_id = sig.get("asset_id")
            action = sig.get("action", "BUY")
            original_signal = sig.get("original_signal", action)
            confidence = sig.get("confidence", 0.5)
            reason = sig.get("reason", "Confirmed by AI")
            
            # Handle NEW sell signals for held positions
            if original_signal == "NEW" and action == "SELL" and asset_id in self.state.positions:
                pos = self.state.positions[asset_id]
                current_price = self.api.get_asset_price(asset_id)
                if current_price is None:
                    current_price = pos.avg_buy_price
                
                pnl_pct = ((current_price / pos.avg_buy_price) - 1) * 100 if pos.avg_buy_price > 0 else 0
                
                confirmed.append({
                    "asset_id": asset_id,
                    "asset_name": pos.asset_name,
                    "signal": "SELL",
                    "score": confidence * 100,
                    "current_price": current_price,
                    "price_change_pct": pnl_pct,
                    "volume_ratio": 0,
                    "reason": f"AI Added ({confidence:.0%}): {reason}"
                })
                continue
            
            # Handle confirmed signals from original opportunities
            if asset_id in opp_lookup:
                opp = opp_lookup[asset_id]
                
                # Skip BUY signals for already-held assets
                if action == "BUY" and asset_id in self.state.positions:
                    log_info(f"Skipping confirmed BUY for {opp.get('asset_name')} - already held")
                    continue
                
                confirmed.append({
                    "asset_id": asset_id,
                    "asset_name": opp.get("asset_name", "Unknown"),
                    "signal": action,
                    "score": confidence * 100,
                    "current_price": opp.get("current_price", 0),
                    "price_change_pct": opp.get("price_change_pct", 0),
                    "volume_ratio": opp.get("volume_ratio", 1),
                    "reason": f"AI Confirmed ({confidence:.0%}): {reason}"
                })
        
        # Sort by score
        confirmed.sort(key=lambda x: x["score"], reverse=True)
        return confirmed
    
    def _prepare_candidates_summary(self, candidates: List[Dict]) -> str:
        """Prepare a concise summary of candidates for the LLM."""
        lines = []
        for i, c in enumerate(candidates, 1):
            asset = c["asset"]
            lines.append(
                f"{i}. {asset.get('name', 'Unknown')} ({asset.get('ticker', 'N/A')}) "
                f"[ID: {asset['id']}]\n"
                f"   Price: {c['current_price']:.8f} ALGO | "
                f"24h Change: {c['price_change_pct']:+.2f}% | "
                f"Volume Ratio: {c['volume_ratio']:.1f}x | "
                f"TVL: {asset.get('tvl', 0):.0f} ALGO | "
                f"Pre-score: {c['score']:.0f}"
            )
        return "\n".join(lines)
    
    def _process_llm_recommendations(self, result: Dict, candidates: List[Dict]) -> List[Dict]:
        """Convert LLM recommendations to opportunity format."""
        opportunities = []
        recommendations = result.get("recommendations", [])
        
        # Create lookup for candidates by asset_id
        candidate_lookup = {c["asset"]["id"]: c for c in candidates}
        
        for rec in recommendations:
            asset_id = rec.get("asset_id")
            action = rec.get("action", "BUY")
            
            # Handle SELL signals for held positions (may not be in candidates)
            if action == "SELL" and asset_id in self.state.positions:
                pos = self.state.positions[asset_id]
                current_price = self.api.get_asset_price(asset_id)
                if current_price is None:
                    current_price = pos.avg_buy_price
                
                pnl_pct = ((current_price / pos.avg_buy_price) - 1) * 100 if pos.avg_buy_price > 0 else 0
                
                opportunities.append({
                    "asset_id": asset_id,
                    "asset_name": pos.asset_name,
                    "signal": "SELL",
                    "score": rec.get("confidence", 0.5) * 100,
                    "current_price": current_price,
                    "price_change_pct": pnl_pct,
                    "volume_ratio": 0,
                    "reason": f"LLM ({rec.get('confidence', 0.5):.0%} confidence): {rec.get('reason', 'Sell recommended')}"
                })
                continue
            
            # Handle BUY signals from candidates
            if asset_id not in candidate_lookup:
                continue
            
            c = candidate_lookup[asset_id]
            asset = c["asset"]
            
            opportunities.append({
                "asset_id": asset_id,
                "asset_name": f"{asset.get('name', 'Unknown')} ({asset.get('ticker', 'N/A')})",
                "signal": action,
                "score": rec.get("confidence", 0.5) * 100,
                "current_price": c["current_price"],
                "price_change_pct": c["price_change_pct"],
                "volume_ratio": c["volume_ratio"],
                "reason": f"LLM ({rec.get('confidence', 0.5):.0%} confidence): {rec.get('reason', 'Recommended')}"
            })
        
        # Sort by priority/score
        opportunities.sort(key=lambda x: x["score"], reverse=True)
        return opportunities
    
    def _convert_candidates_to_opportunities(self, candidates: List[Dict]) -> List[Dict]:
        """Convert pre-screened candidates to opportunities (fallback when LLM unavailable)."""
        opportunities = []
        for c in candidates[:10]:
            asset = c["asset"]
            
            # Determine signal based on momentum
            if c["price_change_pct"] > 3:
                signal = "BUY"
            elif c["price_change_pct"] < -5:
                signal = "SELL"
            else:
                signal = "BUY" if c["volume_ratio"] > 2 else "HOLD"
            
            if signal == "HOLD":
                continue
            
            opportunities.append({
                "asset_id": asset["id"],
                "asset_name": f"{asset.get('name', 'Unknown')} ({asset.get('ticker', 'N/A')})",
                "signal": signal,
                "score": c["score"],
                "current_price": c["current_price"],
                "price_change_pct": c["price_change_pct"],
                "volume_ratio": c["volume_ratio"],
                "reason": f"Momentum: {c['price_change_pct']:+.2f}%, Volume: {c['volume_ratio']:.1f}x avg"
            })
        
        return opportunities
    
    def execute_buy(self, opportunity: Dict, amount_algo: float) -> Optional[TradeRecord]:
        """Execute a buy trade."""
        asset_id = opportunity["asset_id"]
        asset_name = opportunity["asset_name"]
        
        # Respect rug.ninja max buy limit ONLY for actual rug.ninja tokens (not graduated)
        # is_rug_ninja flag is only set for tokens still on bonding curve
        if opportunity.get("is_rug_ninja"):
            max_buy = opportunity.get("max_buy_algo", self.config.rug_ninja_max_buy_algo)
            if amount_algo > max_buy:
                log_info(f"🥷 Limiting rug.ninja buy to {max_buy} ALGO (was {amount_algo:.2f})")
                amount_algo = max_buy
        
        log_info(f"Executing BUY for {asset_name}...")
        
        # Check balance with buffer for fees (opt-in + swap fees)
        balance = self.wallet.get_algo_balance()
        required_balance = amount_algo + 2  # Keep 2 ALGO for fees (opt-in + network fees)
        
        if balance < required_balance:
            log_warning(f"Insufficient balance: {balance:.4f} ALGO (need {required_balance:.4f})")
            return None
        
        # Ensure opted into asset
        if not self.wallet.is_opted_in(asset_id):
            opt_in_result = self.wallet.opt_in_asset(asset_id)
            if not opt_in_result:
                log_error(f"Failed to opt into asset {asset_id}")
                return None
            
            # Re-check balance after opt-in (it costs ~0.1 ALGO min balance + 0.001 fee)
            balance = self.wallet.get_algo_balance()
            if balance < amount_algo + 1:
                log_warning(f"Insufficient balance after opt-in: {balance:.4f} ALGO")
                return None
        
        # Reduce amount slightly to account for network fees in the swap
        # Vestige swaps can have multiple transactions with fees
        fee_buffer = 0.01  # 0.01 ALGO buffer for transaction fees
        adjusted_amount = amount_algo - fee_buffer
        
        # Get swap quote (ALGO -> ASA)
        amount_microalgos = int(adjusted_amount * 1_000_000)
        quote = self.api.get_swap_quote(
            from_asa=ALGO_ASSET_ID,
            to_asa=asset_id,
            amount=amount_microalgos,
            mode="sef"  # Sell exact for
        )
        
        if not quote:
            log_error("Failed to get swap quote")
            return None
        
        amount_out = quote.get("amount_out", 0)
        price_impact = quote.get("price_impact", 0)
        network_fee = quote.get("network_fee", 0) / 1_000_000  # Convert to ALGO
        
        # Final balance check including network fees from quote
        total_cost = adjusted_amount + network_fee + 0.01  # Extra buffer
        if balance < total_cost:
            log_warning(f"Insufficient balance for swap: need {total_cost:.4f}, have {balance:.4f}")
            return None
        
        if price_impact > self.config.slippage_tolerance:
            log_warning(f"Price impact too high: {price_impact:.2f}%")
            return None
        
        # Get transactions
        txns = self.api.get_swap_transactions(
            sender=self.wallet.address,
            slippage=self.config.slippage_tolerance / 100,
            swap_data=quote
        )
        
        if not txns:
            log_error("Failed to get swap transactions")
            return None
        
        # Sign and submit
        txid = self.wallet.sign_and_submit_transactions(txns)
        
        if not txid:
            log_error("Transaction failed")
            return None
        
        # Get asset decimals
        asset_info = self.wallet.get_asset_info(asset_id)
        decimals = asset_info["params"].get("decimals", 6) if asset_info else 6
        amount_received = amount_out / (10 ** decimals)
        
        current_price = opportunity["current_price"]
        
        # Determine trade source for logging
        # Only label as rug_ninja if this is actually a rug.ninja token (still on bonding curve)
        trade_source = None
        if opportunity.get("is_rug_ninja"):
            trade_source = "rug_ninja"
        elif opportunity.get("is_alpha_arcade"):
            trade_source = "alpha_arcade"
        
        # Log the trade (use adjusted_amount which is what we actually spent)
        log_trade("BUY", asset_name, amount_received, current_price, adjusted_amount, source=trade_source)
        
        # Create trade record
        trade = TradeRecord(
            timestamp=datetime.now(),
            action="BUY",
            asset_id=asset_id,
            asset_name=asset_name,
            amount_in=adjusted_amount,
            amount_out=amount_received,
            price=current_price,
            value_algo=adjusted_amount,
            txn_id=txid
        )
        
        # Update position
        if asset_id in self.state.positions:
            pos = self.state.positions[asset_id]
            new_amount = pos.amount + amount_received
            new_invested = pos.total_invested + adjusted_amount
            pos.amount = new_amount
            pos.total_invested = new_invested
            pos.avg_buy_price = new_invested / new_amount if new_amount > 0 else 0
            # Update original amount for partial profit calculations
            pos.original_amount = new_amount
        else:
            self.state.positions[asset_id] = Position(
                asset_id=asset_id,
                asset_name=asset_name,
                amount=amount_received,
                avg_buy_price=current_price,
                total_invested=adjusted_amount,
                # Profit enhancement fields
                peak_price=current_price,
                entry_time=datetime.now(),
                trailing_stop_active=False,
                trailing_stop_price=0.0,
                partial_profits_taken=0,
                original_amount=amount_received,
                trade_source=trade_source or "vestige"
            )
        
        self.state.trade_history.append(trade)
        self.state.total_trades += 1
        self.state.daily_trades += 1
        self.state.last_trade_time = datetime.now()
        
        log_success(f"BUY complete: {txid[:16]}...")
        
        return trade
    
    def execute_sell(self, asset_id: int, amount: float = None, 
                     reason: str = "Signal") -> Optional[TradeRecord]:
        """Execute a sell trade."""
        if asset_id not in self.state.positions:
            log_warning(f"No position in asset {asset_id}")
            return None
        
        pos = self.state.positions[asset_id]
        
        log_info(f"Executing SELL for {pos.asset_name}...")
        
        # Get asset decimals first
        asset_info = self.wallet.get_asset_info(asset_id)
        decimals = asset_info["params"].get("decimals", 6) if asset_info else 6
        
        # Get ACTUAL on-chain balance (not just tracked position)
        actual_balance = self.wallet.get_asset_balance(asset_id)
        
        if actual_balance <= 0:
            log_warning(f"No actual balance for {pos.asset_name}")
            # Remove from positions since we don't have any
            del self.state.positions[asset_id]
            return None
        
        # Use the minimum of tracked position and actual balance
        # Apply a small buffer (0.1%) to avoid underflow due to rounding
        max_sellable = actual_balance * 0.999  # 99.9% of actual balance
        
        # Determine amount to sell
        if amount is None or amount >= pos.amount:
            # Selling full position - use actual balance with buffer
            sell_amount = min(pos.amount, max_sellable)
            close_position = True
        else:
            sell_amount = min(amount, max_sellable)
            close_position = False
        
        if sell_amount <= 0:
            log_warning(f"Nothing to sell for {pos.asset_name}")
            return None
        
        log_info(f"  Selling {sell_amount:.6f} of {actual_balance:.6f} available")
        
        # Convert to micro units
        amount_micro = int(sell_amount * (10 ** decimals))
        
        # Double-check: ensure we're not trying to sell more than actual balance in micro units
        actual_micro = int(actual_balance * (10 ** decimals))
        if amount_micro > actual_micro:
            amount_micro = int(actual_micro * 0.999)  # 99.9% of actual
            sell_amount = amount_micro / (10 ** decimals)
            log_info(f"  Adjusted to {sell_amount:.6f} to avoid underflow")
        
        # Get swap quote (ASA -> ALGO)
        quote = self.api.get_swap_quote(
            from_asa=asset_id,
            to_asa=ALGO_ASSET_ID,
            amount=amount_micro,
            mode="sef"
        )
        
        if not quote:
            log_error("Failed to get swap quote")
            return None
        
        amount_out = quote.get("amount_out", 0)
        amount_algo = amount_out / 1_000_000  # Convert from microAlgos
        
        # Get transactions
        txns = self.api.get_swap_transactions(
            sender=self.wallet.address,
            slippage=self.config.slippage_tolerance / 100,
            swap_data=quote
        )
        
        if not txns:
            log_error("Failed to get swap transactions")
            return None
        
        # Sign and submit
        txid = self.wallet.sign_and_submit_transactions(txns)
        
        if not txid:
            log_error("Transaction failed")
            return None
        
        # Calculate P/L (use original position amount for proper calculation)
        cost_basis = (sell_amount / pos.amount) * pos.total_invested if pos.amount > 0 else 0
        pnl = amount_algo - cost_basis
        pnl_percent = (pnl / cost_basis * 100) if cost_basis > 0 else 0
        
        # Get current price
        price_data = self.api.get_asset_price(asset_id)
        current_price = price_data if price_data else pos.avg_buy_price
        
        # Determine trade source for logging
        # Use the position's trade_source if available, or check asset name for emoji markers
        trade_source = getattr(pos, 'trade_source', None)
        if not trade_source:
            # Check if asset name has rug.ninja marker (still on bonding curve)
            if pos.asset_name.startswith("🥷"):
                trade_source = "rug_ninja"
            elif pos.asset_name.startswith("🎯"):
                trade_source = "alpha_arcade"
        
        # Log the trade
        log_trade("SELL", pos.asset_name, sell_amount, current_price, amount_algo, pnl, source=trade_source)
        
        # Create trade record
        trade = TradeRecord(
            timestamp=datetime.now(),
            action="SELL",
            asset_id=asset_id,
            asset_name=pos.asset_name,
            amount_in=sell_amount,
            amount_out=amount_algo,
            price=current_price,
            value_algo=amount_algo,
            txn_id=txid,
            pnl=pnl,
            pnl_percent=pnl_percent
        )
        
        # Update position
        if close_position:
            del self.state.positions[asset_id]
        else:
            pos.amount -= sell_amount
            pos.total_invested -= cost_basis
        
        # Update state
        self.state.trade_history.append(trade)
        self.state.total_trades += 1
        self.state.total_pnl_algo += pnl
        self.state.daily_trades += 1
        self.state.daily_pnl_algo += pnl
        self.state.last_trade_time = datetime.now()
        
        if pnl >= 0:
            self.state.winning_trades += 1
            self.state.daily_wins += 1
        else:
            self.state.losing_trades += 1
            self.state.daily_losses += 1
            self.state.last_loss_time = datetime.now()
        
        log_success(f"SELL complete: {txid[:16]}... | P/L: {pnl:+.4f} ALGO")
        
        return trade
    
    def check_stop_conditions(self) -> bool:
        """Check if any stop conditions are met."""
        # Check total loss limit
        if self.config.stop_on_loss and self.state.total_pnl_algo < -self.config.max_loss_algo:
            log_warning(f"Stop loss triggered: Total P/L {self.state.total_pnl_algo:.4f} ALGO")
            return True
        
        # Check drawdown
        current_value = self.state.current_balance_algo
        if self.state.max_balance_algo > 0:
            drawdown = (self.state.max_balance_algo - current_value) / self.state.max_balance_algo * 100
            if drawdown > self.config.max_drawdown_percent:
                log_warning(f"Max drawdown triggered: {drawdown:.2f}%")
                return True
        
        return False
    
    def check_position_stops(self):
        """
        Check stop loss, take profit, trailing stops, and partial profits for all positions.
        
        Enhanced profit protection features:
        - Trailing stop loss to lock in profits
        - Partial profit taking at multiple levels
        - Profit protection (tightened stops when in profit)
        - Minimum hold time to avoid panic sells
        """
        now = datetime.now()
        
        for asset_id, pos in list(self.state.positions.items()):
            # Skip imported positions if user didn't consent to managing them
            if getattr(pos, "is_imported", False) and not self.config.manage_imported_positions:
                continue
            
            # Get current price
            price = self.api.get_asset_price(asset_id)
            if price is None:
                continue
            
            pos.current_price = price
            pos.current_value = pos.amount * price
            pos.unrealized_pnl = pos.current_value - pos.total_invested
            pos.unrealized_pnl_percent = (pos.unrealized_pnl / pos.total_invested * 100) if pos.total_invested > 0 else 0
            
            # Initialize tracking fields if not set
            if pos.peak_price == 0:
                pos.peak_price = pos.avg_buy_price
            if pos.original_amount == 0:
                pos.original_amount = pos.amount
            if pos.entry_time is None:
                pos.entry_time = now
            
            # Update peak price (for trailing stop)
            if price > pos.peak_price:
                pos.peak_price = price
                # Update trailing stop price if active
                if pos.trailing_stop_active and self.config.trailing_stop_enabled:
                    pos.trailing_stop_price = pos.peak_price * (1 - self.config.trailing_stop_distance_percent / 100)
            
            # Check minimum hold time
            hold_minutes = (now - pos.entry_time).total_seconds() / 60 if pos.entry_time else 999
            if hold_minutes < self.config.min_hold_minutes:
                # Only allow sells if stop loss is hit badly (emergency exit)
                if pos.unrealized_pnl_percent > -self.config.stop_loss_percent * 1.5:
                    continue  # Still in hold period, skip non-emergency exits
            
            # === TRAILING STOP LOGIC ===
            if self.config.trailing_stop_enabled and pos.unrealized_pnl_percent > 0:
                # Activate trailing stop once we hit activation threshold
                if not pos.trailing_stop_active and pos.unrealized_pnl_percent >= self.config.trailing_stop_activation_percent:
                    pos.trailing_stop_active = True
                    pos.trailing_stop_price = pos.peak_price * (1 - self.config.trailing_stop_distance_percent / 100)
                    log_info(f"📈 Trailing stop activated for {pos.asset_name} at ${pos.trailing_stop_price:.6f}")
                
                # Check if trailing stop is hit
                if pos.trailing_stop_active and price <= pos.trailing_stop_price:
                    profit_pct = ((pos.trailing_stop_price / pos.avg_buy_price) - 1) * 100
                    log_success(f"📈 Trailing stop hit for {pos.asset_name}! Locking in ~{profit_pct:.1f}% profit")
                    self.execute_sell(asset_id, reason="Trailing Stop")
                    continue
            
            # === PARTIAL PROFIT TAKING ===
            if self.config.partial_profit_enabled and pos.unrealized_pnl_percent > 0:
                partial_levels = [
                    (self.config.partial_profit_level_1_pct, self.config.partial_profit_level_1_sell),
                    (self.config.partial_profit_level_2_pct, self.config.partial_profit_level_2_sell),
                    (self.config.partial_profit_level_3_pct, self.config.partial_profit_level_3_sell),
                ]
                
                # Check if we should take partial profit
                for level_idx, (profit_target, sell_percent) in enumerate(partial_levels):
                    if pos.partial_profits_taken <= level_idx and pos.unrealized_pnl_percent >= profit_target:
                        # Calculate amount to sell
                        sell_amount = pos.original_amount * (sell_percent / 100)
                        
                        if sell_amount >= pos.amount * 0.1:  # At least 10% of current position
                            actual_sell = min(sell_amount, pos.amount * 0.9)  # Keep at least 10%
                            
                            if actual_sell > 0:
                                log_success(f"💰 Partial profit #{level_idx+1}: Selling {sell_percent:.0f}% of {pos.asset_name} at +{pos.unrealized_pnl_percent:.1f}%")
                                self.execute_partial_sell(asset_id, actual_sell, reason=f"Partial Profit {level_idx+1}")
                                pos.partial_profits_taken = level_idx + 1
                        break  # Only take one level at a time
            
            # === PROFIT PROTECTION (tighten stops when in profit) ===
            effective_stop_loss = self.config.stop_loss_percent
            if self.config.profit_protection_enabled and pos.unrealized_pnl_percent >= self.config.profit_protection_threshold:
                # We're in profit - use tighter stop
                effective_stop_loss = self.config.profit_protection_stop
                # Don't let tightened stop go below breakeven
                if pos.unrealized_pnl_percent > effective_stop_loss:
                    # Ensure we at least break even
                    pass  # effective_stop_loss is already set
            
            # === STOP LOSS CHECK ===
            if pos.unrealized_pnl_percent < -effective_stop_loss:
                log_warning(f"🛑 Stop loss triggered for {pos.asset_name}: {pos.unrealized_pnl_percent:.2f}%")
                self.execute_sell(asset_id, reason="Stop Loss")
                continue
            
            # === TAKE PROFIT CHECK (full exit) ===
            # Only trigger if partial profits aren't enabled or we've taken all partials
            if pos.unrealized_pnl_percent > self.config.take_profit_percent:
                if not self.config.partial_profit_enabled or pos.partial_profits_taken >= 3:
                    log_success(f"🎯 Take profit triggered for {pos.asset_name}: {pos.unrealized_pnl_percent:.2f}%")
                    self.execute_sell(asset_id, reason="Take Profit")
                    continue
            
            # === MAX HOLD TIME CHECK ===
            if self.config.max_hold_hours > 0:
                hold_hours = hold_minutes / 60
                if hold_hours >= self.config.max_hold_hours:
                    log_info(f"⏰ Max hold time reached for {pos.asset_name} ({hold_hours:.1f}h)")
                    self.execute_sell(asset_id, reason="Max Hold Time")
                    continue
    
    def execute_partial_sell(self, asset_id: int, amount: float, reason: str = "Partial Profit"):
        """Execute a partial sell of a position."""
        if asset_id not in self.state.positions:
            return False
        
        pos = self.state.positions[asset_id]
        
        if amount >= pos.amount:
            # Selling all - use regular sell
            return self.execute_sell(asset_id, reason=reason)
        
        try:
            # Calculate proportional investment
            sell_ratio = amount / pos.amount
            sell_investment = pos.total_invested * sell_ratio
            sell_value = amount * pos.current_price
            
            # Execute the partial sell via DEX
            success = self._execute_swap(
                from_asset_id=asset_id,
                to_asset_id=0,  # ALGO
                amount=amount,
                is_sell=True
            )
            
            if success:
                # Update position
                pos.amount -= amount
                pos.total_invested -= sell_investment
                pos.current_value = pos.amount * pos.current_price
                
                # Calculate and record profit
                pnl = sell_value - sell_investment
                
                # Record partial trade
                self.state.total_trades += 1
                self.state.daily_trades += 1
                
                if pnl > 0:
                    self.state.winning_trades += 1
                    self.state.daily_wins += 1
                else:
                    self.state.losing_trades += 1
                    self.state.daily_losses += 1
                
                self.state.total_pnl_algo += pnl
                self.state.daily_pnl_algo += pnl
                self.state.last_trade_time = datetime.now()
                
                if pnl < 0:
                    self.state.last_loss_time = datetime.now()
                
                log_success(f"💰 Partial sell: {amount:.2f} {pos.asset_name} for {sell_value:.4f} ALGO (P/L: {pnl:+.4f})")
                return True
            
            return False
            
        except Exception as e:
            log_error(f"Partial sell failed: {e}")
            return False
    
    def update_state(self):
        """Update bot state with current balances."""
        self.state.current_balance_algo = self.wallet.get_algo_balance()
        
        # Add position values
        for pos in self.state.positions.values():
            price = self.api.get_asset_price(pos.asset_id)
            if price:
                pos.current_price = price
                pos.current_value = pos.amount * price
                self.state.current_balance_algo += pos.current_value
        
        # Update max balance
        if self.state.current_balance_algo > self.state.max_balance_algo:
            self.state.max_balance_algo = self.state.current_balance_algo


# ============================================================================
# USER INTERFACE
# ============================================================================

class TradingBotUI:
    """User interface for the trading bot."""
    
    @staticmethod
    def get_wallet_phrase() -> str:
        """Securely get wallet mnemonic phrase."""
        print(f"\n{Fore.CYAN}{'='*60}")
        print("  ALGORAND ASA TRADING BOT")
        print(f"{'='*60}{Style.RESET_ALL}\n")
        
        print(f"{Fore.YELLOW}⚠ WARNING: Never share your mnemonic phrase with anyone!")
        print(f"  Your phrase will not be stored or logged.{Style.RESET_ALL}\n")
        
        # Ask if user wants to see the phrase while typing (helps with paste issues)
        show_phrase = input(f"{Fore.YELLOW}Show phrase while typing? (helps with paste issues) (y/n) [n]: {Style.RESET_ALL}").strip().lower()
        
        if show_phrase == 'y':
            print(f"{Fore.YELLOW}Enter your 25-word wallet mnemonic (visible):{Style.RESET_ALL}")
            phrase = input("> ")
        else:
            import getpass
            try:
                phrase = getpass.getpass("Enter your 25-word wallet mnemonic (hidden): ")
            except Exception:
                # Fallback if getpass fails (can happen in some terminals)
                print(f"{Fore.YELLOW}Hidden input failed, using visible input:{Style.RESET_ALL}")
                phrase = input("Enter your 25-word wallet mnemonic: ")
        
        # Validate phrase
        words = phrase.strip().split()
        if len(words) != 25:
            log_error(f"Invalid mnemonic: Expected 25 words, got {len(words)}")
            print(f"{Fore.YELLOW}Tip: Make sure words are separated by spaces and try the visible input option.{Style.RESET_ALL}")
            sys.exit(1)
        
        return phrase.strip()
    
    @staticmethod
    def get_trading_config() -> TradingConfig:
        """Get trading configuration from user with preset support."""
        config = TradingConfig()
        
        print(f"\n{Fore.CYAN}{'='*60}")
        print("  TRADING CONFIGURATION")
        print(f"{'='*60}{Style.RESET_ALL}\n")
        
        # Load custom presets
        custom_presets = load_custom_presets()
        all_presets = {**TRADING_PRESETS, **custom_presets}
        
        # Configuration mode selection
        print(f"{Fore.GREEN}Configuration Mode:{Style.RESET_ALL}")
        print("  1. Use a preset (recommended)")
        print("  2. Custom configuration (manual)")
        print("  3. AI suggests preset based on market")
        print("  4. AI suggests trading strategy (then choose settings)")
        
        if custom_presets:
            print(f"\n  {Fore.CYAN}You have {len(custom_presets)} saved custom preset(s){Style.RESET_ALL}")
        
        mode = input(f"\n{Fore.YELLOW}Select mode (1-4) [1]: {Style.RESET_ALL}").strip() or "1"
        
        selected_llm_model = None
        ai_suggested_strategy = None
        
        # === MODE 4: AI STRATEGY SUGGESTION ===
        if mode == "4":
            selected_llm_model = TradingBotUI._select_ollama_model()
            if selected_llm_model:
                # Ask about rug.ninja inclusion
                print(f"\n{Fore.YELLOW}Include rug.ninja tokens (meme coins) in AI suggestions?{Style.RESET_ALL}")
                print(f"  {Fore.RED}⚠️  WARNING: Rug.ninja tokens are EXTREMELY risky!{Style.RESET_ALL}")
                print(f"  Most meme coins go to zero. Only enable if you accept high risk.")
                include_rug = input(f"{Fore.YELLOW}Include rug.ninja? (y/n) [n]: {Style.RESET_ALL}").strip().lower() == 'y'
                config.ai_include_rug_ninja = include_rug  # Save preference for future AI re-eval
                
                # Ask about AlphaArcade inclusion
                print(f"\n{Fore.CYAN}Include AlphaArcade (prediction markets) in AI suggestions?{Style.RESET_ALL}")
                print(f"  {Fore.YELLOW}🎯 AlphaArcade is a prediction market on Algorand{Style.RESET_ALL}")
                print(f"  Bet on outcomes of events (sports, politics, crypto, etc.)")
                include_alpha = input(f"{Fore.CYAN}Include AlphaArcade? (y/n) [n]: {Style.RESET_ALL}").strip().lower() == 'y'
                config.ai_include_alpha_arcade = include_alpha  # Save preference for future AI re-eval
                
                suggestion = get_ai_strategy_suggestion(selected_llm_model, include_rug_ninja=include_rug, include_alpha_arcade=include_alpha)
                if suggestion:
                    strategy_map = {
                        "momentum": TradingStrategy.MOMENTUM,
                        "mean_reversion": TradingStrategy.MEAN_REVERSION,
                        "breakout": TradingStrategy.BREAKOUT,
                        "scalping": TradingStrategy.SCALPING,
                        "rug_ninja_sniper": TradingStrategy.RUG_NINJA_SNIPER,
                        "rug_ninja_graduated": TradingStrategy.RUG_NINJA_GRADUATED,
                        "alpha_arcade_value": TradingStrategy.ALPHA_ARCADE_VALUE,
                        "alpha_arcade_momentum": TradingStrategy.ALPHA_ARCADE_MOMENTUM,
                    }
                    
                    print(f"\n{Fore.GREEN}{'='*60}")
                    print("  AI STRATEGY RECOMMENDATION")
                    print(f"{'='*60}{Style.RESET_ALL}\n")
                    
                    strategy_name = suggestion['strategy'].replace('_', ' ').title()
                    confidence_pct = suggestion['confidence'] * 100
                    enable_rug_ninja = suggestion.get('enable_rug_ninja', False)
                    enable_alpha_arcade = suggestion.get('enable_alpha_arcade', False)
                    
                    print(f"{Fore.CYAN}Recommended ASA Strategy:{Style.RESET_ALL} {Fore.GREEN}{strategy_name}{Style.RESET_ALL}")
                    print(f"{Fore.CYAN}Confidence:{Style.RESET_ALL} {confidence_pct:.0f}%")
                    
                    # Show additional features that will be enabled
                    if enable_rug_ninja or enable_alpha_arcade:
                        print(f"\n{Fore.YELLOW}Additional Features (will run alongside):{Style.RESET_ALL}")
                        if enable_rug_ninja:
                            print(f"  {Fore.RED}🥷 Rug.ninja{Style.RESET_ALL} - Meme coin sniping (HIGH RISK)")
                        if enable_alpha_arcade:
                            print(f"  {Fore.CYAN}🎯 AlphaArcade{Style.RESET_ALL} - Prediction market betting")
                    
                    print(f"\n{Fore.CYAN}Market Analysis:{Style.RESET_ALL}")
                    print(f"  {suggestion.get('analysis', 'N/A')}")
                    if suggestion.get('why'):
                        print(f"\n{Fore.CYAN}Why This Strategy:{Style.RESET_ALL}")
                        print(f"  {suggestion['why']}")
                    print(f"\n{Fore.YELLOW}Risk Notes:{Style.RESET_ALL}")
                    print(f"  {suggestion.get('risks', 'N/A')}")
                    
                    print(f"\n{Fore.GREEN}Options:{Style.RESET_ALL}")
                    feature_str = ""
                    if enable_rug_ninja and enable_alpha_arcade:
                        feature_str = " + Rug.ninja + AlphaArcade"
                    elif enable_rug_ninja:
                        feature_str = " + Rug.ninja"
                    elif enable_alpha_arcade:
                        feature_str = " + AlphaArcade"
                    print(f"  1. Use {strategy_name}{feature_str} (AI recommended)")
                    print(f"  2. Use {strategy_name} + AI confirmation (hybrid){feature_str}")
                    print("  3. Choose a different strategy manually")
                    
                    choice = input(f"\n{Fore.YELLOW}Select option (1-3) [1]: {Style.RESET_ALL}").strip() or "1"
                    
                    if choice == "1":
                        config.strategy = strategy_map.get(suggestion['strategy'], TradingStrategy.MOMENTUM)
                        ai_suggested_strategy = suggestion['strategy']
                        log_success(f"Using AI-recommended ASA strategy: {strategy_name}")
                        # Enable additional features
                        if enable_rug_ninja:
                            config.rug_ninja_enabled = True
                            config.rug_ninja_mode = "sniper"  # Default to sniper mode
                            log_success("  + Rug.ninja ENABLED (sniper mode)")
                        if enable_alpha_arcade:
                            config.alpha_arcade_enabled = True
                            config.alpha_arcade_mode = "value"  # Default to value mode
                            log_success("  + AlphaArcade ENABLED (value mode)")
                        mode = "2"  # Go to settings configuration
                    elif choice == "2":
                        # Use AI-assisted version
                        hybrid_map = {
                            "momentum": TradingStrategy.MOMENTUM_AI,
                            "mean_reversion": TradingStrategy.MEAN_REVERSION_AI,
                            "breakout": TradingStrategy.BREAKOUT_AI,
                            "scalping": TradingStrategy.SCALPING_AI,
                        }
                        config.strategy = hybrid_map.get(suggestion['strategy'], TradingStrategy.MOMENTUM_AI)
                        config.use_llm = True
                        config.llm_model = selected_llm_model
                        ai_suggested_strategy = suggestion['strategy']
                        log_success(f"Using AI-recommended strategy with AI confirmation: {strategy_name} + AI")
                        # Enable additional features
                        if enable_rug_ninja:
                            config.rug_ninja_enabled = True
                            config.rug_ninja_mode = "sniper"
                            log_success("  + Rug.ninja ENABLED (sniper mode)")
                        if enable_alpha_arcade:
                            config.alpha_arcade_enabled = True
                            config.alpha_arcade_mode = "value"
                            log_success("  + AlphaArcade ENABLED (value mode)")
                        mode = "2"  # Go to settings configuration
                    else:
                        mode = "2"  # Manual strategy selection
                else:
                    print(f"{Fore.YELLOW}AI couldn't analyze market. Proceeding with manual selection...{Style.RESET_ALL}")
                    mode = "2"
            else:
                print(f"{Fore.YELLOW}No LLM available. Proceeding with manual selection...{Style.RESET_ALL}")
                mode = "2"
        
        # === MODE 3: AI PRESET SUGGESTION ===
        if mode == "3":
            # First need to select an LLM
            selected_llm_model = TradingBotUI._select_ollama_model()
            if selected_llm_model:
                suggested = get_ai_preset_suggestion(selected_llm_model)
                if suggested and suggested in all_presets:
                    preset = all_presets[suggested]
                    print(f"\n{Fore.GREEN}AI suggests: {preset['name']}{Style.RESET_ALL}")
                    print(f"  {preset['description']}")
                    
                    use_suggestion = input(f"\n{Fore.YELLOW}Use this preset? (y/n) [y]: {Style.RESET_ALL}").strip().lower()
                    if use_suggestion != 'n':
                        config = apply_preset_to_config(config, preset)
                        log_success(f"Applied preset: {preset['name']}")
                        mode = "preset_applied"
                    else:
                        mode = "1"  # Show all presets
                else:
                    # Preset suggestion failed - offer alternatives
                    print(f"\n{Fore.YELLOW}AI couldn't suggest a preset. What would you like to do?{Style.RESET_ALL}")
                    print("  1. Show all presets")
                    print("  2. Have AI suggest a trading strategy instead")
                    print("  3. Configure manually")
                    
                    fallback = input(f"\n{Fore.YELLOW}Select option (1-3) [1]: {Style.RESET_ALL}").strip() or "1"
                    
                    if fallback == "2":
                        # Try strategy suggestion instead
                        # Ask about rug.ninja inclusion
                        print(f"\n{Fore.YELLOW}Include rug.ninja tokens (meme coins) in AI suggestions?{Style.RESET_ALL}")
                        print(f"  {Fore.RED}⚠️  WARNING: Rug.ninja tokens are EXTREMELY risky!{Style.RESET_ALL}")
                        include_rug = input(f"{Fore.YELLOW}Include rug.ninja? (y/n) [n]: {Style.RESET_ALL}").strip().lower() == 'y'
                        config.ai_include_rug_ninja = include_rug  # Save preference for future AI re-eval
                        
                        # Ask about AlphaArcade inclusion
                        print(f"\n{Fore.CYAN}Include AlphaArcade (prediction markets) in AI suggestions?{Style.RESET_ALL}")
                        print(f"  {Fore.YELLOW}🎯 Bet on outcomes of events (sports, politics, crypto){Style.RESET_ALL}")
                        include_alpha = input(f"{Fore.CYAN}Include AlphaArcade? (y/n) [n]: {Style.RESET_ALL}").strip().lower() == 'y'
                        config.ai_include_alpha_arcade = include_alpha  # Save preference for future AI re-eval
                        
                        suggestion = get_ai_strategy_suggestion(selected_llm_model, include_rug_ninja=include_rug, include_alpha_arcade=include_alpha)
                        if suggestion:
                            strategy_map = {
                                "momentum": TradingStrategy.MOMENTUM,
                                "mean_reversion": TradingStrategy.MEAN_REVERSION,
                                "breakout": TradingStrategy.BREAKOUT,
                                "scalping": TradingStrategy.SCALPING,
                                "rug_ninja_sniper": TradingStrategy.RUG_NINJA_SNIPER,
                                "rug_ninja_graduated": TradingStrategy.RUG_NINJA_GRADUATED,
                                "alpha_arcade_value": TradingStrategy.ALPHA_ARCADE_VALUE,
                                "alpha_arcade_momentum": TradingStrategy.ALPHA_ARCADE_MOMENTUM,
                            }
                            
                            print(f"\n{Fore.GREEN}{'='*60}")
                            print("  AI STRATEGY RECOMMENDATION")
                            print(f"{'='*60}{Style.RESET_ALL}\n")
                            
                            strategy_name = suggestion['strategy'].replace('_', ' ').title()
                            confidence_pct = suggestion['confidence'] * 100
                            enable_rug_ninja = suggestion.get('enable_rug_ninja', False)
                            enable_alpha_arcade = suggestion.get('enable_alpha_arcade', False)
                            
                            print(f"{Fore.CYAN}Recommended ASA Strategy:{Style.RESET_ALL} {Fore.GREEN}{strategy_name}{Style.RESET_ALL}")
                            print(f"{Fore.CYAN}Confidence:{Style.RESET_ALL} {confidence_pct:.0f}%")
                            
                            if enable_rug_ninja or enable_alpha_arcade:
                                print(f"\n{Fore.YELLOW}Additional Features (will run alongside):{Style.RESET_ALL}")
                                if enable_rug_ninja:
                                    print(f"  {Fore.RED}🥷 Rug.ninja{Style.RESET_ALL} - Meme coin sniping")
                                if enable_alpha_arcade:
                                    print(f"  {Fore.CYAN}🎯 AlphaArcade{Style.RESET_ALL} - Prediction markets")
                            
                            print(f"\n{Fore.CYAN}Market Analysis:{Style.RESET_ALL}")
                            print(f"  {suggestion.get('analysis', 'N/A')}")
                            if suggestion.get('why'):
                                print(f"\n{Fore.CYAN}Why This Strategy:{Style.RESET_ALL}")
                                print(f"  {suggestion['why']}")
                            print(f"\n{Fore.YELLOW}Risk Notes:{Style.RESET_ALL}")
                            print(f"  {suggestion.get('risks', 'N/A')}")
                            
                            feature_str = ""
                            if enable_rug_ninja and enable_alpha_arcade:
                                feature_str = " + Rug.ninja + AlphaArcade"
                            elif enable_rug_ninja:
                                feature_str = " + Rug.ninja"
                            elif enable_alpha_arcade:
                                feature_str = " + AlphaArcade"
                            
                            print(f"\n{Fore.GREEN}Options:{Style.RESET_ALL}")
                            print(f"  1. Use {strategy_name}{feature_str}")
                            print(f"  2. Use {strategy_name} + AI confirmation{feature_str}")
                            print("  3. Choose a different strategy manually")
                            
                            choice = input(f"\n{Fore.YELLOW}Select option (1-3) [1]: {Style.RESET_ALL}").strip() or "1"
                            
                            if choice == "1":
                                config.strategy = strategy_map.get(suggestion['strategy'], TradingStrategy.MOMENTUM)
                                ai_suggested_strategy = suggestion['strategy']
                                log_success(f"Using ASA strategy: {strategy_name}")
                                if enable_rug_ninja:
                                    config.rug_ninja_enabled = True
                                    config.rug_ninja_mode = "sniper"
                                    log_success("  + Rug.ninja ENABLED")
                                if enable_alpha_arcade:
                                    config.alpha_arcade_enabled = True
                                    config.alpha_arcade_mode = "value"
                                    log_success("  + AlphaArcade ENABLED")
                                mode = "2"
                            elif choice == "2":
                                hybrid_map = {
                                    "momentum": TradingStrategy.MOMENTUM_AI,
                                    "mean_reversion": TradingStrategy.MEAN_REVERSION_AI,
                                    "breakout": TradingStrategy.BREAKOUT_AI,
                                    "scalping": TradingStrategy.SCALPING_AI,
                                }
                                config.strategy = hybrid_map.get(suggestion['strategy'], TradingStrategy.MOMENTUM_AI)
                                config.use_llm = True
                                config.llm_model = selected_llm_model
                                ai_suggested_strategy = suggestion['strategy']
                                log_success(f"Using: {strategy_name} + AI")
                                if enable_rug_ninja:
                                    config.rug_ninja_enabled = True
                                    config.rug_ninja_mode = "sniper"
                                    log_success("  + Rug.ninja ENABLED")
                                if enable_alpha_arcade:
                                    config.alpha_arcade_enabled = True
                                    config.alpha_arcade_mode = "value"
                                    log_success("  + AlphaArcade ENABLED")
                                mode = "2"
                            else:
                                mode = "2"
                        else:
                            print(f"{Fore.YELLOW}Strategy suggestion also failed. Showing presets...{Style.RESET_ALL}")
                            mode = "1"
                    elif fallback == "3":
                        mode = "2"
                    else:
                        mode = "1"
            else:
                print(f"{Fore.YELLOW}No LLM available. Showing all presets...{Style.RESET_ALL}")
                mode = "1"
        
        # === MODE 1: PRESET SELECTION ===
        if mode == "1":
            print(f"\n{Fore.GREEN}Available Presets:{Style.RESET_ALL}\n")
            
            preset_keys = list(all_presets.keys())
            for i, key in enumerate(preset_keys, 1):
                preset = all_presets[key]
                is_custom = preset.get("custom", False)
                prefix = f"{Fore.MAGENTA}[CUSTOM]{Style.RESET_ALL} " if is_custom else ""
                print(f"  {Fore.CYAN}{i:2}.{Style.RESET_ALL} {prefix}{preset['name']}")
                print(f"      {Fore.WHITE}{preset['description']}{Style.RESET_ALL}")
                print(f"      {Fore.YELLOW}Best for: {', '.join(preset['market_conditions'])}{Style.RESET_ALL}\n")
            
            choice = input(f"{Fore.YELLOW}Select preset (1-{len(preset_keys)}) [1]: {Style.RESET_ALL}").strip() or "1"
            
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(preset_keys):
                    preset_key = preset_keys[idx]
                    preset = all_presets[preset_key]
                    config = apply_preset_to_config(config, preset)
                    log_success(f"Applied preset: {preset['name']}")
                    mode = "preset_applied"
                else:
                    log_warning("Invalid selection, using default (Moderate)")
                    config = apply_preset_to_config(config, TRADING_PRESETS["moderate"])
                    mode = "preset_applied"
            except ValueError:
                log_warning("Invalid input, using default (Moderate)")
                config = apply_preset_to_config(config, TRADING_PRESETS["moderate"])
                mode = "preset_applied"
        
        # === MODE 2 or TWEAKING: CUSTOM/MANUAL CONFIGURATION ===
        if mode == "2" or mode == "preset_applied":
            if mode == "preset_applied":
                print(f"\n{Fore.GREEN}Preset Applied: {config.preset_name}{Style.RESET_ALL}")
                print(f"  Strategy: {TradingBotUI._get_strategy_display_name(config.strategy)}")
                print(f"  Stop Loss: {config.stop_loss_percent}% | Take Profit: {config.take_profit_percent}%")
                print(f"  Max Positions: {config.max_total_positions}")
                
                # Show LLM config if preset had it
                if config.multi_llm_enabled:
                    print(f"\n  {Fore.CYAN}Multi-LLM Configuration (from preset):{Style.RESET_ALL}")
                    if config.llm_market_analysis:
                        print(f"    Market Analysis: {config.llm_market_analysis}")
                    if config.llm_trade_decisions:
                        print(f"    Trade Decisions: {config.llm_trade_decisions}")
                    if config.llm_strategy_reasoning:
                        print(f"    Strategy Reasoning: {config.llm_strategy_reasoning}")
                    if config.llm_risk_assessment:
                        print(f"    Risk Assessment: {config.llm_risk_assessment}")
                    # Set selected_llm_model to the first available model for compatibility
                    for model in [config.llm_market_analysis, config.llm_trade_decisions, 
                                  config.llm_strategy_reasoning, config.llm_risk_assessment]:
                        if model:
                            selected_llm_model = model
                            break
                elif config.use_llm and config.llm_model and config.llm_model != "llama3.2":
                    # Show single LLM model from preset
                    print(f"\n  {Fore.CYAN}LLM Model (from preset): {config.llm_model}{Style.RESET_ALL}")
                    selected_llm_model = config.llm_model
                
                # Show AI re-eval config if preset had it
                if config.ai_dynamic_reeval:
                    print(f"\n  {Fore.CYAN}AI Re-evaluation (from preset):{Style.RESET_ALL}")
                    print(f"    Interval: {config.ai_reeval_interval_minutes} minutes")
                    print(f"    Auto-apply: {'Yes' if config.ai_reeval_auto_apply else 'No'}")
                    if config.ai_include_rug_ninja:
                        print(f"    {Fore.RED}⚠️  Includes rug.ninja suggestions{Style.RESET_ALL}")
                    if config.ai_include_alpha_arcade:
                        print(f"    🎯 Includes AlphaArcade suggestions")
                
                # Show rug.ninja if enabled from preset
                if config.rug_ninja_enabled:
                    print(f"\n  {Fore.RED}🥷 Rug.ninja: ENABLED ({config.rug_ninja_mode} mode){Style.RESET_ALL}")
                
                # Show AlphaArcade if enabled from preset
                if config.alpha_arcade_enabled:
                    print(f"\n  {Fore.CYAN}🎯 AlphaArcade: ENABLED ({config.alpha_arcade_mode} mode){Style.RESET_ALL}")
                
                print(f"\n{Fore.YELLOW}What would you like to do?{Style.RESET_ALL}")
                print("  1. Use preset as-is")
                print("  2. Change strategy only")
                print("  3. Have AI suggest a strategy")
                print("  4. Tweak all settings")
                print("  5. Enable AI dynamic re-evaluation")
                print("  6. Configure multi-LLM (different models for different tasks)")
                
                tweak_choice = input(f"\n{Fore.YELLOW}Select option (1-6) [1]: {Style.RESET_ALL}").strip() or "1"
                
                if tweak_choice == "1":
                    # Use as-is - respect existing multi-LLM and AI re-eval from preset
                    
                    # If preset has multi-LLM, use those models
                    if config.multi_llm_enabled:
                        # Use first available model as selected_llm_model for compatibility
                        for model in [config.llm_market_analysis, config.llm_trade_decisions, 
                                      config.llm_strategy_reasoning, config.llm_risk_assessment]:
                            if model:
                                selected_llm_model = model
                                config.llm_model = model
                                break
                        log_success(f"Using multi-LLM configuration from preset")
                    elif config.use_llm:
                        # Check if preset already has an LLM model configured
                        if config.llm_model and config.llm_model != "llama3.2":
                            # Preset has a specific LLM model saved
                            selected_llm_model = config.llm_model
                            log_success(f"Using LLM from preset: {config.llm_model}")
                        elif not selected_llm_model:
                            # Need to select an LLM model
                            selected_llm_model = TradingBotUI._select_ollama_model()
                            if selected_llm_model:
                                config.llm_model = selected_llm_model
                        
                        # Offer multi-LLM configuration only if not already set up
                        multi_llm = input(f"\n{Fore.YELLOW}Configure different LLMs for different tasks? (y/n) [n]: {Style.RESET_ALL}").strip().lower()
                        if multi_llm == 'y':
                            llm_config = configure_multi_llm()
                            config.multi_llm_enabled = True
                            config.llm_market_analysis = llm_config.get("market_analysis", "")
                            config.llm_trade_decisions = llm_config.get("trade_decisions", "")
                            config.llm_strategy_reasoning = llm_config.get("strategy_reasoning", "")
                            config.llm_risk_assessment = llm_config.get("risk_assessment", "")
                    
                    # If preset has AI re-eval configured, skip asking
                    if config.ai_dynamic_reeval:
                        log_success(f"AI re-evaluation enabled from preset (every {config.ai_reeval_interval_minutes} min)")
                    else:
                        # Ask about AI re-eval
                        config, selected_llm_model = TradingBotUI._ask_ai_reeval(config, selected_llm_model)
                    
                    TradingBotUI._offer_save_preset(config)
                    return config
                
                elif tweak_choice == "2":
                    # Change strategy only
                    config, selected_llm_model = TradingBotUI._select_strategy(config, selected_llm_model, show_current=True)
                    
                    # Ask about AI re-eval
                    config, selected_llm_model = TradingBotUI._ask_ai_reeval(config, selected_llm_model)
                    
                    TradingBotUI._offer_save_preset(config)
                    return config
                
                elif tweak_choice == "5":
                    # Enable AI dynamic re-evaluation
                    if not selected_llm_model:
                        selected_llm_model = TradingBotUI._select_ollama_model()
                    
                    if selected_llm_model:
                        config.llm_model = selected_llm_model
                        config.ai_dynamic_reeval = True
                        
                        reeval_interval = input(f"{Fore.YELLOW}Re-evaluation interval (minutes) [{config.ai_reeval_interval_minutes}]: {Style.RESET_ALL}").strip()
                        if reeval_interval:
                            config.ai_reeval_interval_minutes = int(reeval_interval)
                        
                        auto_apply = input(f"{Fore.YELLOW}Auto-apply AI suggestions without asking? (y/n) [n]: {Style.RESET_ALL}").strip().lower()
                        config.ai_reeval_auto_apply = (auto_apply == 'y')
                        
                        log_success(f"AI re-evaluation enabled (every {config.ai_reeval_interval_minutes} min)")
                    else:
                        log_warning("No LLM selected - AI re-evaluation not enabled")
                    
                    TradingBotUI._offer_save_preset(config)
                    return config
                
                elif tweak_choice == "6":
                    # Configure multi-LLM
                    llm_config = configure_multi_llm()
                    config.multi_llm_enabled = True
                    config.llm_market_analysis = llm_config.get("market_analysis", "")
                    config.llm_trade_decisions = llm_config.get("trade_decisions", "")
                    config.llm_strategy_reasoning = llm_config.get("strategy_reasoning", "")
                    config.llm_risk_assessment = llm_config.get("risk_assessment", "")
                    
                    # Set a default llm_model from any configured model
                    for model in [config.llm_market_analysis, config.llm_trade_decisions, 
                                  config.llm_strategy_reasoning, config.llm_risk_assessment]:
                        if model:
                            config.llm_model = model
                            selected_llm_model = model
                            break
                    
                    log_success("Multi-LLM configuration saved")
                    
                    # Ask about AI re-eval now that we have LLMs
                    config, selected_llm_model = TradingBotUI._ask_ai_reeval(config, selected_llm_model)
                    
                    TradingBotUI._offer_save_preset(config)
                    return config
                
                elif tweak_choice == "3":
                    # AI suggest strategy
                    if not selected_llm_model:
                        selected_llm_model = TradingBotUI._select_ollama_model()
                    
                    if selected_llm_model:
                        # Ask about rug.ninja inclusion
                        print(f"\n{Fore.YELLOW}Include rug.ninja tokens (meme coins) in AI suggestions?{Style.RESET_ALL}")
                        print(f"  {Fore.RED}⚠️  WARNING: Rug.ninja tokens are EXTREMELY risky!{Style.RESET_ALL}")
                        include_rug = input(f"{Fore.YELLOW}Include rug.ninja? (y/n) [n]: {Style.RESET_ALL}").strip().lower() == 'y'
                        config.ai_include_rug_ninja = include_rug  # Save preference for future AI re-eval
                        
                        # Ask about AlphaArcade inclusion
                        print(f"\n{Fore.CYAN}Include AlphaArcade (prediction markets) in AI suggestions?{Style.RESET_ALL}")
                        print(f"  {Fore.YELLOW}🎯 Bet on outcomes of events (sports, politics, crypto){Style.RESET_ALL}")
                        include_alpha = input(f"{Fore.CYAN}Include AlphaArcade? (y/n) [n]: {Style.RESET_ALL}").strip().lower() == 'y'
                        config.ai_include_alpha_arcade = include_alpha  # Save preference for future AI re-eval
                        
                        suggestion = get_ai_strategy_suggestion(selected_llm_model, include_rug_ninja=include_rug, include_alpha_arcade=include_alpha)
                        if suggestion:
                            strategy_map = {
                                "momentum": TradingStrategy.MOMENTUM,
                                "mean_reversion": TradingStrategy.MEAN_REVERSION,
                                "breakout": TradingStrategy.BREAKOUT,
                                "scalping": TradingStrategy.SCALPING,
                                "rug_ninja_sniper": TradingStrategy.RUG_NINJA_SNIPER,
                                "rug_ninja_graduated": TradingStrategy.RUG_NINJA_GRADUATED,
                                "alpha_arcade_value": TradingStrategy.ALPHA_ARCADE_VALUE,
                                "alpha_arcade_momentum": TradingStrategy.ALPHA_ARCADE_MOMENTUM,
                            }
                            
                            print(f"\n{Fore.GREEN}{'='*60}")
                            print("  AI STRATEGY RECOMMENDATION")
                            print(f"{'='*60}{Style.RESET_ALL}\n")
                            
                            strategy_name = suggestion['strategy'].replace('_', ' ').title()
                            confidence_pct = suggestion['confidence'] * 100
                            enable_rug_ninja = suggestion.get('enable_rug_ninja', False)
                            enable_alpha_arcade = suggestion.get('enable_alpha_arcade', False)
                            
                            print(f"{Fore.CYAN}Recommended ASA Strategy:{Style.RESET_ALL} {Fore.GREEN}{strategy_name}{Style.RESET_ALL}")
                            print(f"{Fore.CYAN}Confidence:{Style.RESET_ALL} {confidence_pct:.0f}%")
                            
                            if enable_rug_ninja or enable_alpha_arcade:
                                print(f"\n{Fore.YELLOW}Additional Features (will run alongside):{Style.RESET_ALL}")
                                if enable_rug_ninja:
                                    print(f"  {Fore.RED}🥷 Rug.ninja{Style.RESET_ALL} - Meme coin sniping")
                                if enable_alpha_arcade:
                                    print(f"  {Fore.CYAN}🎯 AlphaArcade{Style.RESET_ALL} - Prediction markets")
                            
                            print(f"\n{Fore.CYAN}Market Analysis:{Style.RESET_ALL}")
                            print(f"  {suggestion.get('analysis', 'N/A')}")
                            if suggestion.get('why'):
                                print(f"\n{Fore.CYAN}Why This Strategy:{Style.RESET_ALL}")
                                print(f"  {suggestion['why']}")
                            print(f"\n{Fore.YELLOW}Risk Notes:{Style.RESET_ALL}")
                            print(f"  {suggestion.get('risks', 'N/A')}")
                            
                            feature_str = ""
                            if enable_rug_ninja and enable_alpha_arcade:
                                feature_str = " + Rug.ninja + AlphaArcade"
                            elif enable_rug_ninja:
                                feature_str = " + Rug.ninja"
                            elif enable_alpha_arcade:
                                feature_str = " + AlphaArcade"
                            
                            print(f"\n{Fore.GREEN}Options:{Style.RESET_ALL}")
                            print(f"  1. Use {strategy_name}{feature_str}")
                            print(f"  2. Use {strategy_name} + AI confirmation{feature_str}")
                            print("  3. Keep current strategy")
                            print("  4. Choose manually")
                            
                            ai_choice = input(f"\n{Fore.YELLOW}Select option (1-4) [1]: {Style.RESET_ALL}").strip() or "1"
                            
                            if ai_choice == "1":
                                config.strategy = strategy_map.get(suggestion['strategy'], TradingStrategy.MOMENTUM)
                                log_success(f"Using ASA strategy: {strategy_name}")
                                if enable_rug_ninja:
                                    config.rug_ninja_enabled = True
                                    config.rug_ninja_mode = "sniper"
                                    log_success("  + Rug.ninja ENABLED")
                                if enable_alpha_arcade:
                                    config.alpha_arcade_enabled = True
                                    config.alpha_arcade_mode = "value"
                                    log_success("  + AlphaArcade ENABLED")
                            elif ai_choice == "2":
                                hybrid_map = {
                                    "momentum": TradingStrategy.MOMENTUM_AI,
                                    "mean_reversion": TradingStrategy.MEAN_REVERSION_AI,
                                    "breakout": TradingStrategy.BREAKOUT_AI,
                                    "scalping": TradingStrategy.SCALPING_AI,
                                }
                                config.strategy = hybrid_map.get(suggestion['strategy'], TradingStrategy.MOMENTUM_AI)
                                config.use_llm = True
                                config.llm_model = selected_llm_model
                                log_success(f"Using: {strategy_name} + AI")
                                if enable_rug_ninja:
                                    config.rug_ninja_enabled = True
                                    config.rug_ninja_mode = "sniper"
                                    log_success("  + Rug.ninja ENABLED")
                                if enable_alpha_arcade:
                                    config.alpha_arcade_enabled = True
                                    config.alpha_arcade_mode = "value"
                                    log_success("  + AlphaArcade ENABLED")
                            elif ai_choice == "4":
                                config, selected_llm_model = TradingBotUI._select_strategy(config, selected_llm_model, show_current=True)
                        else:
                            print(f"{Fore.YELLOW}AI suggestion failed. Choose manually:{Style.RESET_ALL}")
                            config, selected_llm_model = TradingBotUI._select_strategy(config, selected_llm_model, show_current=True)
                    else:
                        print(f"{Fore.YELLOW}No LLM available. Choose manually:{Style.RESET_ALL}")
                        config, selected_llm_model = TradingBotUI._select_strategy(config, selected_llm_model, show_current=True)
                    
                    # Ask about AI re-eval
                    config, selected_llm_model = TradingBotUI._ask_ai_reeval(config, selected_llm_model)
                    
                    TradingBotUI._offer_save_preset(config)
                    return config
                
                # else tweak_choice == "4" - fall through to full config
            
            # Check if strategy was already set by AI suggestion
            strategy_already_set = ai_suggested_strategy is not None
            
            if not strategy_already_set:
                # Full manual configuration - strategy selection
                config, selected_llm_model = TradingBotUI._select_strategy(config, selected_llm_model, show_current=(mode == "preset_applied"))
            
            # Risk settings
            print(f"\n{Fore.GREEN}Risk Management:{Style.RESET_ALL}")
            
            max_pos = input(f"{Fore.YELLOW}Max position size (ALGO) [{config.max_position_size_algo}]: {Style.RESET_ALL}").strip()
            if max_pos:
                config.max_position_size_algo = float(max_pos)
            
            max_positions = input(f"{Fore.YELLOW}Max total positions [{config.max_total_positions}]: {Style.RESET_ALL}").strip()
            if max_positions:
                config.max_total_positions = int(max_positions)
            
            stop_loss = input(f"{Fore.YELLOW}Stop loss % [{config.stop_loss_percent}]: {Style.RESET_ALL}").strip()
            if stop_loss:
                config.stop_loss_percent = float(stop_loss)
            
            take_profit = input(f"{Fore.YELLOW}Take profit % [{config.take_profit_percent}]: {Style.RESET_ALL}").strip()
            if take_profit:
                config.take_profit_percent = float(take_profit)
            
            max_drawdown = input(f"{Fore.YELLOW}Max drawdown % before stop [{config.max_drawdown_percent}]: {Style.RESET_ALL}").strip()
            if max_drawdown:
                config.max_drawdown_percent = float(max_drawdown)
            
            # Auto-stop settings
            print(f"\n{Fore.GREEN}Auto-Stop Settings:{Style.RESET_ALL}")
            
            stop_on_loss = input(f"{Fore.YELLOW}Stop on max loss? (y/n) [{'y' if config.stop_on_loss else 'n'}]: {Style.RESET_ALL}").strip().lower()
            if stop_on_loss:
                config.stop_on_loss = stop_on_loss == 'y'
            
            if config.stop_on_loss:
                max_loss = input(f"{Fore.YELLOW}Max loss (ALGO) before stop [{config.max_loss_algo}]: {Style.RESET_ALL}").strip()
                if max_loss:
                    config.max_loss_algo = float(max_loss)
            
            # Trading parameters
            print(f"\n{Fore.GREEN}Trading Parameters:{Style.RESET_ALL}")
            
            scan_all = input(f"{Fore.YELLOW}Scan ALL liquid ASAs on Algorand? (y/n) [{'y' if config.scan_all_liquid_asas else 'n'}]: {Style.RESET_ALL}").strip().lower()
            if scan_all:
                config.scan_all_liquid_asas = scan_all == 'y'
            
            if config.scan_all_liquid_asas:
                max_scan = input(f"{Fore.YELLOW}Max ASAs to scan per cycle [{config.max_assets_to_scan}]: {Style.RESET_ALL}").strip()
                if max_scan:
                    config.max_assets_to_scan = int(max_scan)
            
            min_vol = input(f"{Fore.YELLOW}Min 24h volume (ALGO) [{config.min_volume_24h}]: {Style.RESET_ALL}").strip()
            if min_vol:
                config.min_volume_24h = float(min_vol)
            
            min_liq = input(f"{Fore.YELLOW}Min liquidity/TVL (ALGO) [{config.min_liquidity}]: {Style.RESET_ALL}").strip()
            if min_liq:
                config.min_liquidity = float(min_liq)
            
            slippage = input(f"{Fore.YELLOW}Slippage tolerance % [{config.slippage_tolerance}]: {Style.RESET_ALL}").strip()
            if slippage:
                config.slippage_tolerance = float(slippage)
            
            interval = input(f"{Fore.YELLOW}Check interval (seconds) [{config.check_interval_seconds}]: {Style.RESET_ALL}").strip()
            if interval:
                config.check_interval_seconds = int(interval)
            
            # AI Dynamic Re-evaluation (always available)
            print(f"\n{Fore.GREEN}AI Dynamic Re-evaluation:{Style.RESET_ALL}")
            print(f"  {Fore.WHITE}When enabled, AI will periodically reassess market conditions")
            print(f"  and suggest strategy/preset changes as the market evolves.{Style.RESET_ALL}")
            
            enable_reeval = input(f"{Fore.YELLOW}Enable AI dynamic re-evaluation? (y/n) [{('y' if config.ai_dynamic_reeval else 'n')}]: {Style.RESET_ALL}").strip().lower()
            if enable_reeval == 'y':
                config.ai_dynamic_reeval = True
                
                # Need an LLM for re-evaluation - select one if not already selected
                if not config.llm_model and not selected_llm_model:
                    print(f"\n{Fore.YELLOW}AI re-evaluation requires an LLM model.{Style.RESET_ALL}")
                    selected_llm_model = TradingBotUI._select_ollama_model()
                
                if selected_llm_model:
                    config.llm_model = selected_llm_model
                elif not config.llm_model:
                    print(f"{Fore.RED}No LLM selected - AI re-evaluation disabled.{Style.RESET_ALL}")
                    config.ai_dynamic_reeval = False
                
                if config.ai_dynamic_reeval:
                    reeval_interval = input(f"{Fore.YELLOW}Re-evaluation interval (minutes) [{config.ai_reeval_interval_minutes}]: {Style.RESET_ALL}").strip()
                    if reeval_interval:
                        config.ai_reeval_interval_minutes = int(reeval_interval)
                    
                    auto_apply = input(f"{Fore.YELLOW}Auto-apply AI suggestions without asking? (y/n) [{('y' if config.ai_reeval_auto_apply else 'n')}]: {Style.RESET_ALL}").strip().lower()
                    config.ai_reeval_auto_apply = (auto_apply == 'y')
                    
                    log_success(f"AI re-evaluation enabled (every {config.ai_reeval_interval_minutes} min, auto-apply: {config.ai_reeval_auto_apply})")
            elif enable_reeval == 'n':
                config.ai_dynamic_reeval = False
        
        # Offer to save as custom preset
        TradingBotUI._offer_save_preset(config)
        
        return config
    
    @staticmethod
    def _select_ollama_model() -> Optional[str]:
        """Scan and select an Ollama model by number."""
        print(f"\n{Fore.GREEN}Scanning available Ollama models...{Style.RESET_ALL}")
        
        models = get_available_ollama_models()
        
        if not models:
            print(f"{Fore.YELLOW}No Ollama models found. Make sure Ollama is running.{Style.RESET_ALL}")
            manual = input(f"{Fore.YELLOW}Enter model name manually (or press Enter to skip): {Style.RESET_ALL}").strip()
            return manual if manual else None
        
        print(f"\n{Fore.GREEN}Available LLM Models:{Style.RESET_ALL}\n")
        
        for i, model in enumerate(models, 1):
            name_str = model['name'] if model['name'] else "unknown"
            params_str = f" ({model['params']})" if model['params'] else ""
            family_str = f" [{model['family']}]" if model['family'] else ""
            print(f"  {Fore.CYAN}{i:2}.{Style.RESET_ALL} {name_str}{params_str} - {model['size']}{family_str}")
        
        print(f"\n  {Fore.CYAN} 0.{Style.RESET_ALL} Enter model name manually")
        
        choice = input(f"\n{Fore.YELLOW}Select model (0-{len(models)}) [1]: {Style.RESET_ALL}").strip() or "1"
        
        try:
            idx = int(choice)
            if idx == 0:
                manual = input(f"{Fore.YELLOW}Enter model name: {Style.RESET_ALL}").strip()
                return manual if manual else models[0]["name"]
            elif 1 <= idx <= len(models):
                selected = models[idx - 1]["name"]
                log_success(f"Selected model: {selected}")
                return selected
            else:
                return models[0]["name"]
        except ValueError:
            # Maybe they typed a model name directly
            if any(choice in m["name"] for m in models):
                return choice
            return models[0]["name"] if models else None
    
    @staticmethod
    def _select_strategy(config: TradingConfig, selected_llm_model: Optional[str], show_current: bool = False) -> tuple:
        """Display strategy selection menu and update config. Returns (config, selected_llm_model)."""
        print(f"\n{Fore.GREEN}Strategy Selection:{Style.RESET_ALL}")
        print(f"\n{Fore.CYAN}  Pure Mathematical (No AI):{Style.RESET_ALL}")
        print("  1. Momentum       - Follow price trends and volume")
        print("  2. Mean Reversion - Buy oversold, sell overbought")
        print("  3. Breakout       - Trade on price/volume breakouts")
        print("  4. Scalping       - Quick small profits on volatility")
        
        print(f"\n{Fore.CYAN}  AI-Assisted Hybrid (Math + LLM confirmation):{Style.RESET_ALL}")
        print("  5. Momentum + AI       - Momentum signals confirmed by AI")
        print("  6. Mean Reversion + AI - Mean reversion with AI filtering")
        print("  7. Breakout + AI       - Breakout signals validated by AI")
        print("  8. Scalping + AI       - Scalp opportunities verified by AI")
        
        print(f"\n{Fore.CYAN}  Pure AI (100% LLM-driven):{Style.RESET_ALL}")
        print("  9. Full AI Analysis    - Complete AI-driven trading decisions")
        
        print(f"\n{Fore.MAGENTA}  Rug.ninja (Algorand's pump.fun):{Style.RESET_ALL}")
        print("  10. Rug.ninja Sniper   - Buy new mints on bonding curve ⚠️")
        print("  11. Rug.ninja Graduated - Trade bonded tokens on DEX")
        
        print(f"\n{Fore.BLUE}  AlphaArcade (Prediction Markets):{Style.RESET_ALL}")
        print("  12. AlphaArcade Value    - Contrarian betting on undervalued outcomes 🎯")
        print("  13. AlphaArcade Momentum - Follow prediction market trends 🎯")
        
        current_strat = ""
        if show_current:
            current_strat = f" [current: {TradingBotUI._get_strategy_display_name(config.strategy)}]"
        
        strategy_choice = input(f"\n{Fore.YELLOW}Select strategy (1-13){current_strat}: {Style.RESET_ALL}").strip()
        
        if strategy_choice:
            strategies = {
                "1": TradingStrategy.MOMENTUM,
                "2": TradingStrategy.MEAN_REVERSION,
                "3": TradingStrategy.BREAKOUT,
                "4": TradingStrategy.SCALPING,
                "5": TradingStrategy.MOMENTUM_AI,
                "6": TradingStrategy.MEAN_REVERSION_AI,
                "7": TradingStrategy.BREAKOUT_AI,
                "8": TradingStrategy.SCALPING_AI,
                "9": TradingStrategy.LLM_ASSISTED,
                "10": TradingStrategy.RUG_NINJA_SNIPER,
                "11": TradingStrategy.RUG_NINJA_GRADUATED,
                "12": TradingStrategy.ALPHA_ARCADE_VALUE,
                "13": TradingStrategy.ALPHA_ARCADE_MOMENTUM,
            }
            if strategy_choice in strategies:
                config.strategy = strategies[strategy_choice]
                
                # Enable AlphaArcade if selecting AlphaArcade strategy
                if strategy_choice in ["12", "13"]:
                    config.alpha_arcade_enabled = True
                    config.alpha_arcade_mode = "value" if strategy_choice == "12" else "momentum"
                    log_info(f"🎯 AlphaArcade prediction market mode enabled ({config.alpha_arcade_mode})")
        
        # Enable LLM for AI strategies
        ai_strategies = [
            TradingStrategy.MOMENTUM_AI,
            TradingStrategy.MEAN_REVERSION_AI,
            TradingStrategy.BREAKOUT_AI,
            TradingStrategy.SCALPING_AI,
            TradingStrategy.LLM_ASSISTED
        ]
        
        if config.strategy in ai_strategies:
            config.use_llm = True
            if not selected_llm_model:
                selected_llm_model = TradingBotUI._select_ollama_model()
            if selected_llm_model:
                config.llm_model = selected_llm_model
            
            # Offer multi-LLM configuration for AI strategies
            multi_llm = input(f"\n{Fore.YELLOW}Configure different LLMs for different tasks? (y/n) [n]: {Style.RESET_ALL}").strip().lower()
            if multi_llm == 'y':
                llm_config = configure_multi_llm()
                config.multi_llm_enabled = True
                config.llm_market_analysis = llm_config.get("market_analysis", "")
                config.llm_trade_decisions = llm_config.get("trade_decisions", "")
                config.llm_strategy_reasoning = llm_config.get("strategy_reasoning", "")
                config.llm_risk_assessment = llm_config.get("risk_assessment", "")
        
        # Rug.ninja specific configuration
        if config.strategy in [TradingStrategy.RUG_NINJA_SNIPER, TradingStrategy.RUG_NINJA_GRADUATED]:
            config.rug_ninja_enabled = True
            print(f"\n{Fore.MAGENTA}🥷 Rug.ninja Configuration:{Style.RESET_ALL}")
            
            if config.strategy == TradingStrategy.RUG_NINJA_SNIPER:
                print(f"{Fore.YELLOW}⚠️  WARNING: Sniping new tokens is EXTREMELY risky!{Style.RESET_ALL}")
                print(f"{Fore.WHITE}Most rug.ninja tokens go to zero. Only risk what you can lose.{Style.RESET_ALL}")
                
                # Real-time sniper option (garbage-cat style)
                print(f"\n{Fore.CYAN}Sniper Mode Options:{Style.RESET_ALL}")
                print(f"  1. {Fore.WHITE}API Scanning{Style.RESET_ALL} - Poll for recent mints (safer, less latency-sensitive)")
                print(f"  2. {Fore.RED}Real-time Block Streaming{Style.RESET_ALL} - Garbage-cat style instant sniping")
                print(f"     {Fore.YELLOW}⚠️  Real-time mode buys IMMEDIATELY on mint detection!{Style.RESET_ALL}")
                
                sniper_mode = input(f"\n{Fore.YELLOW}Select sniper mode (1-2) [1]: {Style.RESET_ALL}").strip() or "1"
                if sniper_mode == "2":
                    config.rug_ninja_realtime_sniper = True
                    print(f"\n{Fore.RED}⚠️  REAL-TIME SNIPER ENABLED - Buys will happen INSTANTLY!{Style.RESET_ALL}")
                    print(f"{Fore.WHITE}The bot will stream Algorand blocks and buy the moment a mint is detected.{Style.RESET_ALL}")
                    print(f"{Fore.WHITE}Based on garbage-cat: https://github.com/garbagecatio/garbage-cat{Style.RESET_ALL}")
                else:
                    config.rug_ninja_realtime_sniper = False
                
                max_buy = input(f"\n{Fore.YELLOW}Max ALGO per rug.ninja trade [{config.rug_ninja_max_buy_algo}]: {Style.RESET_ALL}").strip()
                if max_buy:
                    config.rug_ninja_max_buy_algo = float(max_buy)
                
                if not config.rug_ninja_realtime_sniper:
                    max_age = input(f"{Fore.YELLOW}Max token age (minutes) for sniping [{config.rug_ninja_max_age_minutes}]: {Style.RESET_ALL}").strip()
                    if max_age:
                        config.rug_ninja_max_age_minutes = int(max_age)
                
                auto_sell = input(f"{Fore.YELLOW}Auto-sell when token bonds/graduates? (y/n) [y]: {Style.RESET_ALL}").strip().lower()
                config.rug_ninja_auto_sell_on_bond = auto_sell != 'n'
            else:
                max_buy = input(f"\n{Fore.YELLOW}Max ALGO per trade [{config.rug_ninja_max_buy_algo}]: {Style.RESET_ALL}").strip()
                if max_buy:
                    config.rug_ninja_max_buy_algo = float(max_buy)
            
            # Offer AI risk assessment
            use_ai_risk = input(f"\n{Fore.YELLOW}Use AI for rug risk assessment? (y/n) [y]: {Style.RESET_ALL}").strip().lower()
            if use_ai_risk != 'n':
                if not selected_llm_model:
                    selected_llm_model = TradingBotUI._select_ollama_model()
                if selected_llm_model:
                    config.llm_model = selected_llm_model
                    config.llm_risk_assessment = selected_llm_model
                    config.use_llm = True
                    
                    # Offer multi-LLM configuration for rug.ninja with AI
                    multi_llm = input(f"\n{Fore.YELLOW}Configure different LLMs for different tasks? (y/n) [n]: {Style.RESET_ALL}").strip().lower()
                    if multi_llm == 'y':
                        llm_config = configure_multi_llm()
                        config.multi_llm_enabled = True
                        config.llm_market_analysis = llm_config.get("market_analysis", "")
                        config.llm_trade_decisions = llm_config.get("trade_decisions", "")
                        config.llm_strategy_reasoning = llm_config.get("strategy_reasoning", "")
                        config.llm_risk_assessment = llm_config.get("risk_assessment", "") or selected_llm_model
        
        # AlphaArcade specific configuration
        if config.strategy in [TradingStrategy.ALPHA_ARCADE_VALUE, TradingStrategy.ALPHA_ARCADE_MOMENTUM]:
            config.alpha_arcade_enabled = True
            print(f"\n{Fore.CYAN}🎯 AlphaArcade Configuration:{Style.RESET_ALL}")
            print(f"{Fore.WHITE}AlphaArcade is a prediction market on Algorand.{Style.RESET_ALL}")
            print(f"{Fore.WHITE}Bet on outcomes of events (sports, politics, crypto, etc.){Style.RESET_ALL}\n")
            
            # API Key prompt
            print(f"{Fore.YELLOW}AlphaArcade requires a Partner API key for market scanning.{Style.RESET_ALL}")
            print(f"{Fore.WHITE}Get your API key from the AlphaArcade team.{Style.RESET_ALL}")
            print(f"{Fore.WHITE}Docs: https://alphaarcade.gitbook.io/alphaarcade-docs{Style.RESET_ALL}\n")
            
            api_key = input(f"{Fore.CYAN}Enter AlphaArcade API key (or press Enter to skip): {Style.RESET_ALL}").strip()
            if api_key:
                config.alpha_arcade_api_key = api_key
                log_success("AlphaArcade API key configured")
            else:
                log_warning("No API key provided - AlphaArcade market scanning will be limited")
                print(f"{Fore.YELLOW}Tip: You can still trade the $ALPHA token (ASA 2726252423) via regular strategies{Style.RESET_ALL}")
            
            # Mode-specific config
            if config.strategy == TradingStrategy.ALPHA_ARCADE_VALUE:
                print(f"\n{Fore.GREEN}Value Strategy: Buy undervalued predictions (contrarian){Style.RESET_ALL}")
                
                max_bet = input(f"{Fore.CYAN}Max ALGO per prediction bet [{config.alpha_arcade_max_bet_algo}]: {Style.RESET_ALL}").strip()
                if max_bet:
                    config.alpha_arcade_max_bet_algo = float(max_bet)
                
                threshold = input(f"{Fore.CYAN}Value threshold (price diff from estimated prob) [{config.alpha_arcade_value_threshold}]: {Style.RESET_ALL}").strip()
                if threshold:
                    config.alpha_arcade_value_threshold = float(threshold)
            else:
                print(f"\n{Fore.GREEN}Momentum Strategy: Follow prediction market trends{Style.RESET_ALL}")
                
                max_bet = input(f"{Fore.CYAN}Max ALGO per prediction bet [{config.alpha_arcade_max_bet_algo}]: {Style.RESET_ALL}").strip()
                if max_bet:
                    config.alpha_arcade_max_bet_algo = float(max_bet)
                
                threshold = input(f"{Fore.CYAN}Momentum threshold (min price change) [{config.alpha_arcade_momentum_threshold}]: {Style.RESET_ALL}").strip()
                if threshold:
                    config.alpha_arcade_momentum_threshold = float(threshold)
            
            # Auto-sell before resolution
            auto_sell = input(f"\n{Fore.CYAN}Auto-sell positions before market resolution? (y/n) [y]: {Style.RESET_ALL}").strip().lower()
            config.alpha_arcade_auto_sell_before_resolution = auto_sell != 'n'
            
            if config.alpha_arcade_auto_sell_before_resolution:
                hours = input(f"{Fore.CYAN}Hours before resolution to sell [{config.alpha_arcade_hours_before_resolution}]: {Style.RESET_ALL}").strip()
                if hours:
                    config.alpha_arcade_hours_before_resolution = int(hours)
            
            # LP mode option
            print(f"\n{Fore.CYAN}LP Mode: Provide liquidity on both sides for LP rewards{Style.RESET_ALL}")
            print(f"{Fore.WHITE}Earn rewards by maintaining orders within spread distance.{Style.RESET_ALL}")
            lp_mode = input(f"{Fore.CYAN}Enable LP mode? (y/n) [n]: {Style.RESET_ALL}").strip().lower()
            config.alpha_arcade_lp_mode = (lp_mode == 'y')
            if config.alpha_arcade_lp_mode:
                log_info("LP mode enabled - will place orders on both sides for rewards")
        
        return config, selected_llm_model
    
    @staticmethod
    def _ask_ai_reeval(config: TradingConfig, selected_llm_model: Optional[str]) -> tuple:
        """Ask user if they want to enable AI dynamic re-evaluation."""
        print(f"\n{Fore.GREEN}AI Dynamic Re-evaluation:{Style.RESET_ALL}")
        print(f"  {Fore.WHITE}AI can periodically reassess market conditions and suggest")
        print(f"  strategy changes as the market evolves.{Style.RESET_ALL}")
        
        enable = input(f"{Fore.YELLOW}Enable AI dynamic re-evaluation? (y/n) [n]: {Style.RESET_ALL}").strip().lower()
        
        if enable == 'y':
            # Need LLM for this
            if not selected_llm_model and not config.llm_model:
                print(f"\n{Fore.YELLOW}AI re-evaluation requires an LLM model.{Style.RESET_ALL}")
                selected_llm_model = TradingBotUI._select_ollama_model()
            
            if selected_llm_model or config.llm_model:
                if selected_llm_model:
                    config.llm_model = selected_llm_model
                config.ai_dynamic_reeval = True
                
                reeval_interval = input(f"{Fore.YELLOW}Re-evaluation interval (minutes) [{config.ai_reeval_interval_minutes}]: {Style.RESET_ALL}").strip()
                if reeval_interval:
                    config.ai_reeval_interval_minutes = int(reeval_interval)
                
                auto_apply = input(f"{Fore.YELLOW}Auto-apply AI suggestions without asking? (y/n) [n]: {Style.RESET_ALL}").strip().lower()
                config.ai_reeval_auto_apply = (auto_apply == 'y')
                
                # Ask about including rug.ninja in AI suggestions
                print(f"\n{Fore.YELLOW}Include rug.ninja tokens (meme coins) in AI suggestions?{Style.RESET_ALL}")
                print(f"  {Fore.RED}⚠️  WARNING: Rug.ninja tokens are EXTREMELY risky!{Style.RESET_ALL}")
                print(f"  {Fore.WHITE}If enabled, AI may suggest switching to rug.ninja strategies.{Style.RESET_ALL}")
                include_rug = input(f"{Fore.YELLOW}Include rug.ninja in AI suggestions? (y/n) [n]: {Style.RESET_ALL}").strip().lower()
                config.ai_include_rug_ninja = (include_rug == 'y')
                if config.ai_include_rug_ninja:
                    log_warning("Rug.ninja included in AI suggestions - EXTREME RISK enabled!")
                
                # Ask about including AlphaArcade in AI suggestions
                print(f"\n{Fore.CYAN}Include AlphaArcade (prediction markets) in AI suggestions?{Style.RESET_ALL}")
                print(f"  {Fore.YELLOW}🎯 AlphaArcade is a prediction market on Algorand{Style.RESET_ALL}")
                print(f"  {Fore.WHITE}If enabled, AI may suggest switching to prediction market strategies.{Style.RESET_ALL}")
                include_alpha = input(f"{Fore.CYAN}Include AlphaArcade in AI suggestions? (y/n) [n]: {Style.RESET_ALL}").strip().lower()
                config.ai_include_alpha_arcade = (include_alpha == 'y')
                if config.ai_include_alpha_arcade:
                    log_info("AlphaArcade included in AI suggestions")
                
                # Offer multi-LLM if not already configured
                if not config.multi_llm_enabled:
                    multi_llm = input(f"{Fore.YELLOW}Configure different LLMs for different tasks? (y/n) [n]: {Style.RESET_ALL}").strip().lower()
                    if multi_llm == 'y':
                        llm_config = configure_multi_llm()
                        config.multi_llm_enabled = True
                        config.llm_market_analysis = llm_config.get("market_analysis", "")
                        config.llm_trade_decisions = llm_config.get("trade_decisions", "")
                        config.llm_strategy_reasoning = llm_config.get("strategy_reasoning", "")
                        config.llm_risk_assessment = llm_config.get("risk_assessment", "")
                
                log_success(f"AI re-evaluation enabled (every {config.ai_reeval_interval_minutes} min)")
            else:
                log_warning("No LLM available - AI re-evaluation not enabled")
        
        return config, selected_llm_model
    
    @staticmethod
    def _offer_save_preset(config: TradingConfig):
        """Offer to save current configuration as a custom preset."""
        save = input(f"\n{Fore.YELLOW}Save this configuration as a custom preset? (y/n) [n]: {Style.RESET_ALL}").strip().lower()
        
        if save == 'y':
            name = input(f"{Fore.YELLOW}Preset name: {Style.RESET_ALL}").strip()
            if name:
                # Clean the name for use as a key
                key = name.lower().replace(" ", "_")
                desc = input(f"{Fore.YELLOW}Description (optional): {Style.RESET_ALL}").strip()
                save_custom_preset(key, config, desc or f"Custom preset: {name}")
    
    @staticmethod
    def manage_presets():
        """Manage custom presets (view, delete)."""
        print(f"\n{Fore.CYAN}{'='*60}")
        print("  MANAGE CUSTOM PRESETS")
        print(f"{'='*60}{Style.RESET_ALL}\n")
        
        custom_presets = load_custom_presets()
        
        if not custom_presets:
            print(f"{Fore.YELLOW}No custom presets saved.{Style.RESET_ALL}")
            return
        
        print(f"{Fore.GREEN}Your Custom Presets:{Style.RESET_ALL}\n")
        
        preset_keys = list(custom_presets.keys())
        for i, key in enumerate(preset_keys, 1):
            preset = custom_presets[key]
            print(f"  {i}. {preset['name']}")
            print(f"     {preset['description']}")
            settings = preset.get('settings', {})
            strategy = settings.get('strategy', 'unknown')
            if hasattr(strategy, 'value'):
                strategy = strategy.value
            print(f"     Strategy: {strategy} | Stop Loss: {settings.get('stop_loss_percent', '?')}% | Take Profit: {settings.get('take_profit_percent', '?')}%")
            
            # Show LLM settings
            if settings.get('multi_llm_enabled'):
                llms = []
                if settings.get('llm_market_analysis'):
                    llms.append(f"Market: {settings['llm_market_analysis']}")
                if settings.get('llm_trade_decisions'):
                    llms.append(f"Trade: {settings['llm_trade_decisions']}")
                if settings.get('llm_strategy_reasoning'):
                    llms.append(f"Strategy: {settings['llm_strategy_reasoning']}")
                if settings.get('llm_risk_assessment'):
                    llms.append(f"Risk: {settings['llm_risk_assessment']}")
                if llms:
                    print(f"     {Fore.CYAN}Multi-LLM: {', '.join(llms)}{Style.RESET_ALL}")
            elif settings.get('use_llm') and settings.get('llm_model') and settings.get('llm_model') != "llama3.2":
                print(f"     {Fore.CYAN}LLM: {settings['llm_model']}{Style.RESET_ALL}")
            
            # Show AI re-eval
            if settings.get('ai_dynamic_reeval'):
                print(f"     {Fore.GREEN}AI Re-eval: Every {settings.get('ai_reeval_interval_minutes', 30)} min{Style.RESET_ALL}")
            
            # Show rug.ninja / AlphaArcade
            extras = []
            if settings.get('rug_ninja_enabled'):
                extras.append(f"🥷 Rug.ninja ({settings.get('rug_ninja_mode', 'sniper')})")
            if settings.get('alpha_arcade_enabled'):
                extras.append(f"🎯 AlphaArcade ({settings.get('alpha_arcade_mode', 'value')})")
            if extras:
                print(f"     {', '.join(extras)}")
            
            print()
        
        action = input(f"{Fore.YELLOW}Delete a preset? Enter number (or press Enter to go back): {Style.RESET_ALL}").strip()
        
        if action:
            try:
                idx = int(action) - 1
                if 0 <= idx < len(preset_keys):
                    key = preset_keys[idx]
                    confirm = input(f"{Fore.RED}Delete '{custom_presets[key]['name']}'? (y/n): {Style.RESET_ALL}").strip().lower()
                    if confirm == 'y':
                        delete_custom_preset(key)
            except ValueError:
                pass
    
    @staticmethod
    def _get_strategy_display_name(strategy: TradingStrategy) -> str:
        """Get a user-friendly display name for the strategy."""
        names = {
            TradingStrategy.MOMENTUM: "Momentum",
            TradingStrategy.MEAN_REVERSION: "Mean Reversion",
            TradingStrategy.BREAKOUT: "Breakout",
            TradingStrategy.SCALPING: "Scalping",
            TradingStrategy.GRID: "Grid",
            TradingStrategy.MOMENTUM_AI: "Momentum + AI",
            TradingStrategy.MEAN_REVERSION_AI: "Mean Reversion + AI",
            TradingStrategy.BREAKOUT_AI: "Breakout + AI",
            TradingStrategy.SCALPING_AI: "Scalping + AI",
            TradingStrategy.LLM_ASSISTED: "Full AI Analysis",
            TradingStrategy.RUG_NINJA_SNIPER: "Rug.ninja Sniper 🥷",
            TradingStrategy.RUG_NINJA_GRADUATED: "Rug.ninja Graduated 🎓",
            TradingStrategy.ALPHA_ARCADE_VALUE: "AlphaArcade Value 🎯",
            TradingStrategy.ALPHA_ARCADE_MOMENTUM: "AlphaArcade Momentum 🎯",
        }
        return names.get(strategy, strategy.value)
    
    @staticmethod
    def display_status(state: BotState, config: TradingConfig):
        """Display current bot status."""
        print(f"\n{Fore.CYAN}{'='*60}")
        print("  BOT STATUS")
        print(f"{'='*60}{Style.RESET_ALL}")
        
        print(f"\n{Fore.GREEN}Balance:{Style.RESET_ALL}")
        print(f"  Starting:  {format_algo(state.starting_balance_algo)}")
        print(f"  Current:   {format_algo(state.current_balance_algo)}")
        
        pnl_color = Fore.GREEN if state.total_pnl_algo >= 0 else Fore.RED
        print(f"  Total P/L: {pnl_color}{state.total_pnl_algo:+.4f} ALGO{Style.RESET_ALL}")
        
        print(f"\n{Fore.GREEN}Trading Stats:{Style.RESET_ALL}")
        print(f"  Total Trades: {state.total_trades}")
        print(f"  Winning:      {state.winning_trades}")
        print(f"  Losing:       {state.losing_trades}")
        
        win_rate = (state.winning_trades / state.total_trades * 100) if state.total_trades > 0 else 0
        print(f"  Win Rate:     {win_rate:.1f}%")
        
        # Daily stats
        if state.daily_trades > 0:
            daily_color = Fore.GREEN if state.daily_pnl_algo >= 0 else Fore.RED
            daily_wr = (state.daily_wins / state.daily_trades * 100) if state.daily_trades > 0 else 0
            print(f"\n{Fore.GREEN}Today's Stats ({state.current_day}):{Style.RESET_ALL}")
            print(f"  Trades: {state.daily_trades} | Wins: {state.daily_wins} | Losses: {state.daily_losses}")
            print(f"  Daily P/L: {daily_color}{state.daily_pnl_algo:+.4f} ALGO{Style.RESET_ALL} | Win Rate: {daily_wr:.1f}%")
        
        # Show preset and strategy
        preset_info = f" (Preset: {config.preset_name})" if config.preset_name != "custom" else ""
        print(f"\n{Fore.GREEN}Strategy: {TradingBotUI._get_strategy_display_name(config.strategy)}{preset_info}{Style.RESET_ALL}")
        print(f"  Scanning: {'ALL liquid ASAs' if config.scan_all_liquid_asas else 'Top 50 by volume'}")
        print(f"  Min Volume: {config.min_volume_24h} ALGO | Min TVL: {config.min_liquidity} ALGO")
        print(f"  Max Positions: {len(state.positions)}/{config.max_total_positions}")
        print(f"  Stop Loss: {config.stop_loss_percent}% | Take Profit: {config.take_profit_percent}%")
        print(f"  Check Interval: {config.check_interval_seconds}s")
        
        # Profit enhancement features
        profit_features = []
        if getattr(config, 'trailing_stop_enabled', False):
            profit_features.append(f"Trailing@{config.trailing_stop_activation_percent}%")
        if getattr(config, 'partial_profit_enabled', False):
            profit_features.append("Partial$")
        if getattr(config, 'anti_fomo_enabled', False):
            profit_features.append("Anti-FOMO")
        if getattr(config, 'profit_protection_enabled', False):
            profit_features.append("Protect")
        if getattr(config, 'buy_the_dip_enabled', False):
            profit_features.append("BuyDip")
        if getattr(config, 'use_technical_analysis', True):
            profit_features.append("TA")
        if getattr(config, 'use_dynamic_sizing', True):
            profit_features.append("DynSize")
        
        if profit_features:
            print(f"  {Fore.YELLOW}💰 Profit Enhancement: {', '.join(profit_features)}{Style.RESET_ALL}")
        
        # Daily limits
        limits = []
        if getattr(config, 'max_daily_loss_algo', 0) > 0:
            limits.append(f"MaxLoss:{config.max_daily_loss_algo}")
        if getattr(config, 'max_daily_trades', 0) > 0:
            limits.append(f"MaxTrades:{config.max_daily_trades}")
        if getattr(config, 'cooldown_after_loss_minutes', 0) > 0:
            limits.append(f"Cooldown:{config.cooldown_after_loss_minutes}m")
        
        if limits:
            print(f"  {Fore.CYAN}📊 Daily Limits: {', '.join(limits)}{Style.RESET_ALL}")
        
        # LLM info
        if config.use_llm or config.llm_model:
            if config.multi_llm_enabled:
                print(f"  {Fore.CYAN}Multi-LLM Enabled:{Style.RESET_ALL}")
                if config.llm_market_analysis:
                    print(f"    Market: {config.llm_market_analysis}")
                if config.llm_trade_decisions:
                    print(f"    Trade: {config.llm_trade_decisions}")
                if config.llm_strategy_reasoning:
                    print(f"    Strategy: {config.llm_strategy_reasoning}")
                if config.llm_risk_assessment:
                    print(f"    Risk: {config.llm_risk_assessment}")
            else:
                print(f"  LLM Model: {config.llm_model}")
        
        # AI re-eval info
        if config.ai_dynamic_reeval:
            next_reeval = ""
            if state.last_ai_reeval_time:
                elapsed = (datetime.now() - state.last_ai_reeval_time).total_seconds() / 60
                remaining = config.ai_reeval_interval_minutes - elapsed
                if remaining > 0:
                    next_reeval = f" (next in {remaining:.0f}m)"
            auto_str = "auto" if config.ai_reeval_auto_apply else "ask"
            print(f"  {Fore.MAGENTA}AI Re-eval: Every {config.ai_reeval_interval_minutes}m ({auto_str}){next_reeval}{Style.RESET_ALL}")
        
        # Rug.ninja info
        if config.rug_ninja_enabled:
            mode_str = config.rug_ninja_mode.title()
            print(f"  {Fore.YELLOW}Rug.ninja: {mode_str} mode | Max {config.rug_ninja_max_buy_algo} ALGO/trade{Style.RESET_ALL}")
            if config.strategy == TradingStrategy.RUG_NINJA_SNIPER:
                print(f"    Bond Range: {config.rug_ninja_min_bond_progress*100:.0f}%-{config.rug_ninja_max_bond_progress*100:.0f}%")
                print(f"    Max Age: {config.rug_ninja_max_age_minutes}m | Auto-sell on bond: {'Yes' if config.rug_ninja_auto_sell_on_bond else 'No'}")
        
        if state.positions:
            print(f"\n{Fore.GREEN}Open Positions:{Style.RESET_ALL}")
            for pos in state.positions.values():
                pnl_color = Fore.GREEN if pos.unrealized_pnl >= 0 else Fore.RED
                print(f"  • {pos.asset_name}")
                print(f"    Amount: {pos.amount:.6f} | Value: {pos.current_value:.4f} ALGO")
                print(f"    P/L: {pnl_color}{pos.unrealized_pnl:+.4f} ALGO ({pos.unrealized_pnl_percent:+.2f}%){Style.RESET_ALL}")
                
                # Show profit enhancement status
                status_parts = []
                if getattr(pos, 'trailing_stop_active', False):
                    status_parts.append(f"📈 Trail@{pos.trailing_stop_price:.6f}")
                if getattr(pos, 'partial_profits_taken', 0) > 0:
                    status_parts.append(f"💰 Partials: {pos.partial_profits_taken}/3")
                if getattr(pos, 'peak_price', 0) > pos.avg_buy_price:
                    peak_gain = ((pos.peak_price / pos.avg_buy_price) - 1) * 100
                    status_parts.append(f"🏔️ Peak: +{peak_gain:.1f}%")
                
                if status_parts:
                    print(f"    {Fore.YELLOW}{' | '.join(status_parts)}{Style.RESET_ALL}")
        
        # Dip watch list
        if hasattr(state, 'dip_watch_list') and state.dip_watch_list:
            print(f"\n{Fore.CYAN}Watching for Dips ({len(state.dip_watch_list)}):{Style.RESET_ALL}")
            for asset_id, watch in list(state.dip_watch_list.items())[:3]:  # Show max 3
                opp = watch.get('opp', {})
                target = watch.get('target_price', 0)
                print(f"  • {opp.get('asset_name', asset_id)} - Target: ${target:.6f}")
        
        print(f"\n{Fore.CYAN}{'='*60}{Style.RESET_ALL}\n")
    
    @staticmethod
    def display_opportunities(opportunities: List[Dict]):
        """Display trading opportunities."""
        if not opportunities:
            log_info("No actionable trading opportunities")
            return
        
        buy_count = sum(1 for o in opportunities if o["signal"] == "BUY")
        sell_count = sum(1 for o in opportunities if o["signal"] == "SELL")
        
        # Check if any are rug.ninja
        rug_ninja_count = sum(1 for o in opportunities if o.get("is_rug_ninja"))
        title = f"ACTIONABLE OPPORTUNITIES ({buy_count} BUY, {sell_count} SELL)"
        if rug_ninja_count > 0:
            title = f"RUG.NINJA OPPORTUNITIES ({buy_count} BUY, {sell_count} SELL)"
        
        print(f"\n{Fore.CYAN}{'='*60}")
        print(f"  {title}")
        print(f"{'='*60}{Style.RESET_ALL}\n")
        
        for i, opp in enumerate(opportunities[:10], 1):  # Show up to 10 opportunities
            signal_color = Fore.GREEN if opp["signal"] == "BUY" else Fore.RED
            action_word = "→ BUY" if opp["signal"] == "BUY" else "→ SELL (held)"
            
            # Special display for rug.ninja
            if opp.get("is_rug_ninja"):
                bond_pct = opp.get("bond_progress", 0) * 100
                bond_info = f" | Bond: {bond_pct:.0f}%"
                print(f"{signal_color}{i}. 🥷 {opp['asset_name']} {action_word}{Style.RESET_ALL}")
                print(f"   Score: {opp['score']:.1f} | Price: {opp['current_price']:.8f} ALGO{bond_info}")
            else:
                print(f"{signal_color}{i}. {opp['asset_name']} {action_word}{Style.RESET_ALL}")
                print(f"   Score: {opp['score']:.1f} | Price: {opp['current_price']:.8f} ALGO")
            
            print(f"   {opp.get('reason', 'N/A')}")
            
            # Show risks for rug.ninja
            if opp.get("risks"):
                risks = opp["risks"][:2]  # Show max 2 risks
                print(f"   {Fore.YELLOW}⚠️ Risks: {', '.join(risks)}{Style.RESET_ALL}")
            print()


# ============================================================================
# MAIN BOT CLASS
# ============================================================================

class AlgorandTradingBot:
    """Main trading bot orchestrator."""
    
    def __init__(self):
        self.wallet: Optional[AlgorandWallet] = None
        self.api: Optional[VestigeAPI] = None
        self.config: Optional[TradingConfig] = None
        self.state: Optional[BotState] = None
        self.engine: Optional[TradingEngine] = None
        self.ui = TradingBotUI()
        self._shutdown_event = threading.Event()
    
    def initialize(self):
        """Initialize the trading bot."""
        # Get wallet phrase
        phrase = self.ui.get_wallet_phrase()
        
        # Initialize wallet
        try:
            self.wallet = AlgorandWallet(phrase)
        except Exception as e:
            log_error(f"Failed to initialize wallet: {e}")
            sys.exit(1)
        
        # Initialize API
        self.api = VestigeAPI()
        
        # Test API connection
        log_info("Testing Vestige API connection...")
        if self.api._get("/ping") is not None:
            log_success("Vestige API connected")
        else:
            log_error("Failed to connect to Vestige API")
            sys.exit(1)
        
        # Get configuration
        self.config = self.ui.get_trading_config()
        
        # Initialize state
        self.state = BotState()
        self.state.starting_balance_algo = self.wallet.get_algo_balance()
        self.state.current_balance_algo = self.state.starting_balance_algo
        self.state.max_balance_algo = self.state.starting_balance_algo
        
        log_info(f"Starting balance: {format_algo(self.state.starting_balance_algo)}")
        
        # Initialize trading engine
        self.engine = TradingEngine(
            wallet=self.wallet,
            api=self.api,
            config=self.config,
            state=self.state
        )
        
        # Scan for existing wallet positions
        if self.config.import_existing_positions:
            print(f"\n{Fore.YELLOW}Do you want to import existing holdings from your wallet?{Style.RESET_ALL}")
            print(f"  {Fore.WHITE}• Regular ASAs (Vestige-tracked){Style.RESET_ALL}")
            print(f"  {Fore.MAGENTA}• 🥷 Rug.ninja tokens{Style.RESET_ALL}")
            print(f"  {Fore.CYAN}• 🎯 AlphaArcade positions{Style.RESET_ALL}")
            print(f"{Fore.WHITE}(These will be tracked and managed by the bot){Style.RESET_ALL}")
            import_choice = input(f"{Fore.CYAN}Import existing positions? (y/n): {Style.RESET_ALL}").strip().lower()
            
            if import_choice == 'y':
                imported = self.engine.scan_existing_positions()
                if imported > 0:
                    log_success(f"Will manage {imported} existing positions")
                    
                    # Ask about applying stop-loss/take-profit to imported positions
                    print(f"\n{Fore.YELLOW}Apply stop-loss/take-profit to imported positions?{Style.RESET_ALL}")
                    print(f"  {Fore.WHITE}• YES: Bot will sell imported positions if they hit stop-loss or take-profit{Style.RESET_ALL}")
                    print(f"  {Fore.WHITE}• NO: Bot only tracks imported positions, won't sell them automatically{Style.RESET_ALL}")
                    print(f"  {Fore.CYAN}Note: Uses import price as baseline (current market price){Style.RESET_ALL}")
                    manage_choice = input(f"{Fore.CYAN}Apply SL/TP to imported positions? (y/n) [n]: {Style.RESET_ALL}").strip().lower()
                    
                    if manage_choice == 'y':
                        self.config.manage_imported_positions = True
                        log_success(f"✓ Will apply stop-loss/take-profit to imported positions")
                        log_warning(f"  Stop Loss: {self.config.stop_loss_percent}% | Take Profit: {self.config.take_profit_percent}%")
                    else:
                        self.config.manage_imported_positions = False
                        log_info("Imported positions will be tracked but not auto-sold")
            else:
                log_info("Skipping existing position import - only new trades will be tracked")
        
        # Setup signal handlers
        # SIGINT (Ctrl+C) works on all platforms
        signal.signal(signal.SIGINT, self._signal_handler)
        
        # SIGTERM is not available on Windows, so we wrap it in try-except
        try:
            signal.signal(signal.SIGTERM, self._signal_handler)
        except (AttributeError, OSError):
            # SIGTERM not available on Windows
            pass
        
        # Initialize real-time mint sniper if configured (garbage-cat style)
        self._realtime_sniper = None
        self._sniper_thread = None
        
        if self.config.strategy == TradingStrategy.RUG_NINJA_SNIPER and getattr(self.config, 'rug_ninja_realtime_sniper', False):
            try:
                from algosdk import mnemonic
                private_key = mnemonic.to_private_key(phrase)
                
                self._realtime_sniper = RugNinjaMintSniper(
                    private_key=private_key,
                    purchase_amount_algo=self.config.rug_ninja_max_buy_algo
                )
                
                log_info("🥷 Real-time mint sniper initialized (garbage-cat style)")
                log_warning("⚠️  Sniper will buy IMMEDIATELY when mints are detected!")
            except Exception as e:
                log_error(f"Failed to initialize real-time sniper: {e}")
                self._realtime_sniper = None
    
    def _check_ai_reeval(self):
        """Check if it's time for AI to re-evaluate market conditions."""
        now = datetime.now()
        
        # Check if enough time has passed since last evaluation
        if self.state.last_ai_reeval_time:
            elapsed = (now - self.state.last_ai_reeval_time).total_seconds() / 60
            if elapsed < self.config.ai_reeval_interval_minutes:
                return  # Not time yet
        
        # Gather recent performance data
        recent_trades = [t for t in self.state.trade_history[-20:]]  # Last 20 trades
        recent_wins = sum(1 for t in recent_trades if t.pnl > 0)
        recent_pnl = sum(t.pnl for t in recent_trades)
        
        performance = {
            "win_rate": (recent_wins / len(recent_trades) * 100) if recent_trades else 0,
            "pnl": recent_pnl,
            "num_trades": len(recent_trades),
            "open_positions": len(self.state.positions),
            "total_pnl": self.state.total_pnl_algo
        }
        
        # Get current strategy name
        current_strategy = self.config.strategy.value
        current_preset = self.config.preset_name
        
        # Get the appropriate LLM for strategy reasoning (uses multi-LLM if configured)
        strategy_llm = get_llm_for_task(self.config, "strategy")
        if not strategy_llm:
            strategy_llm = self.config.llm_model
        
        if not strategy_llm:
            log_warning("No LLM configured for AI re-evaluation")
            return
        
        # Call AI for re-evaluation
        result = get_ai_market_reeval(
            strategy_llm,
            current_strategy,
            current_preset,
            performance,
            include_rug_ninja=self.config.ai_include_rug_ninja,
            include_alpha_arcade=self.config.ai_include_alpha_arcade
        )
        
        self.state.last_ai_reeval_time = now
        
        if not result:
            return
        
        recommendation = result.get("recommendation", "keep")
        
        if recommendation == "keep":
            log_info(f"AI Re-eval: Keeping current configuration ({result.get('reasoning', 'performing well')})")
            return
        
        # AI suggests a change
        urgency = result.get("urgency", "low")
        urgency_color = Fore.RED if urgency == "high" else (Fore.YELLOW if urgency == "medium" else Fore.WHITE)
        
        # Determine title based on recommendation type
        if recommendation == "change_both":
            title = "AI STRATEGY & PRESET RE-EVALUATION"
        elif recommendation == "change_preset":
            title = "AI PRESET RE-EVALUATION"
        else:
            title = "AI STRATEGY RE-EVALUATION"
        
        print(f"\n{Fore.CYAN}{'='*60}")
        print(f"  {title}")
        print(f"{'='*60}{Style.RESET_ALL}\n")
        
        print(f"{Fore.CYAN}Current ASA Strategy:{Style.RESET_ALL} {current_strategy}")
        print(f"{Fore.CYAN}Current Preset:{Style.RESET_ALL} {current_preset}")
        
        # Show additional features status
        if self.config.rug_ninja_enabled or self.config.alpha_arcade_enabled:
            print(f"{Fore.CYAN}Additional Features:{Style.RESET_ALL}")
            if self.config.rug_ninja_enabled:
                print(f"  🥷 Rug.ninja: {Fore.GREEN}ACTIVE{Style.RESET_ALL} ({self.config.rug_ninja_mode} mode)")
            if self.config.alpha_arcade_enabled:
                print(f"  🎯 AlphaArcade: {Fore.GREEN}ACTIVE{Style.RESET_ALL} ({self.config.alpha_arcade_mode} mode)")
        
        print(f"\n{Fore.CYAN}Recommendation:{Style.RESET_ALL} {recommendation.replace('_', ' ').title()}")
        print(f"{Fore.CYAN}Urgency:{Style.RESET_ALL} {urgency_color}{urgency.upper()}{Style.RESET_ALL}")
        print(f"{Fore.CYAN}Confidence:{Style.RESET_ALL} {result.get('confidence', 0)*100:.0f}%")
        print(f"\n{Fore.CYAN}Reasoning:{Style.RESET_ALL}")
        print(f"  {result.get('reasoning', 'N/A')}")
        
        new_strategy = result.get("new_strategy")
        new_preset = result.get("new_preset")
        
        print(f"\n{Fore.GREEN}Suggested Changes:{Style.RESET_ALL}")
        if new_strategy:
            print(f"  ASA Strategy: {current_strategy.replace('_ai', '')} → {Fore.GREEN}{new_strategy}{Style.RESET_ALL}")
        if new_preset:
            preset_info = TRADING_PRESETS.get(new_preset, {})
            preset_name = preset_info.get("name", new_preset)
            print(f"  Preset: {current_preset} → {Fore.GREEN}{preset_name}{Style.RESET_ALL}")
            # Show what the preset changes
            settings = preset_info.get("settings", {})
            if settings:
                sl = settings.get("stop_loss_percent")
                tp = settings.get("take_profit_percent")
                if sl and tp:
                    print(f"    (Stop Loss: {sl}% | Take Profit: {tp}%)")
        
        # Note that additional features remain active
        if self.config.rug_ninja_enabled or self.config.alpha_arcade_enabled:
            features = []
            if self.config.rug_ninja_enabled:
                features.append("🥷 Rug.ninja")
            if self.config.alpha_arcade_enabled:
                features.append("🎯 AlphaArcade")
            print(f"\n{Fore.CYAN}Note:{Style.RESET_ALL} {', '.join(features)} will continue running alongside")
        
        # Auto-apply or ask for confirmation
        if self.config.ai_reeval_auto_apply:
            log_info("Auto-applying AI recommendation...")
            self._apply_ai_recommendation(result)
        else:
            print(f"\n{Fore.YELLOW}Options:{Style.RESET_ALL}")
            print("  1. Apply AI recommendation")
            print("  2. Keep current configuration")
            print("  3. Disable future re-evaluations")
            
            try:
                choice = input(f"\n{Fore.YELLOW}Select option (1-3) [2]: {Style.RESET_ALL}").strip() or "2"
                
                if choice == "1":
                    self._apply_ai_recommendation(result)
                elif choice == "3":
                    self.config.ai_dynamic_reeval = False
                    log_info("AI dynamic re-evaluation disabled")
                else:
                    log_info("Keeping current configuration")
            except EOFError:
                # Non-interactive mode, keep current
                log_info("Keeping current configuration (non-interactive)")
    
    def _apply_ai_recommendation(self, result: Dict):
        """Apply the AI's strategy/preset recommendation.
        
        Note: AI only recommends ASA strategies (momentum, mean_reversion, etc.)
        Rug.ninja and AlphaArcade remain as additional features if already enabled.
        """
        new_strategy = result.get("new_strategy")
        new_preset = result.get("new_preset")
        changes_made = []
        
        # Preserve rug.ninja and AlphaArcade state - they're additional features
        rug_ninja_was_enabled = self.config.rug_ninja_enabled
        rug_ninja_mode = self.config.rug_ninja_mode
        alpha_arcade_was_enabled = self.config.alpha_arcade_enabled
        alpha_arcade_mode = self.config.alpha_arcade_mode
        
        # Preserve max_total_positions if we have more positions than preset allows
        current_positions = len(self.state.positions)
        current_max_positions = self.config.max_total_positions
        
        # Apply preset first (it may change strategy)
        if new_preset and new_preset in TRADING_PRESETS:
            preset = TRADING_PRESETS[new_preset]
            old_preset = self.config.preset_name
            self.config = apply_preset_to_config(self.config, preset)
            changes_made.append(f"Preset: {old_preset} → {preset['name']}")
            log_success(f"Applied preset: {preset['name']}")
            
            # Show key risk parameter changes
            settings = preset.get("settings", {})
            log_info(f"  Stop Loss: {settings.get('stop_loss_percent', '?')}%")
            log_info(f"  Take Profit: {settings.get('take_profit_percent', '?')}%")
            log_info(f"  Max Positions: {settings.get('max_total_positions', '?')}")
            
            # Restore max_total_positions if we have more positions (from imports)
            preset_max = self.config.max_total_positions
            if current_positions >= preset_max:
                # Keep the higher limit to avoid blocking trades
                self.config.max_total_positions = max(current_max_positions, current_positions + 5)
                log_info(f"  ⚠️  Keeping max positions at {self.config.max_total_positions} (have {current_positions} positions)")
        
        # Apply strategy change - ONLY ASA strategies
        if new_strategy:
            strategy_map = {
                "momentum": TradingStrategy.MOMENTUM,
                "mean_reversion": TradingStrategy.MEAN_REVERSION,
                "breakout": TradingStrategy.BREAKOUT,
                "scalping": TradingStrategy.SCALPING,
            }
            if new_strategy in strategy_map:
                old_strategy = self.config.strategy.value.replace("_ai", "")
                self.config.strategy = strategy_map[new_strategy]
                if old_strategy != new_strategy:
                    changes_made.append(f"Strategy: {old_strategy} → {new_strategy}")
                    log_success(f"Changed ASA strategy to: {new_strategy}")
        
        # Restore rug.ninja and AlphaArcade state - they run alongside ASA strategy
        if rug_ninja_was_enabled:
            self.config.rug_ninja_enabled = True
            self.config.rug_ninja_mode = rug_ninja_mode
            log_info(f"  🥷 Rug.ninja still active ({rug_ninja_mode} mode)")
        
        if alpha_arcade_was_enabled:
            self.config.alpha_arcade_enabled = True
            self.config.alpha_arcade_mode = alpha_arcade_mode
            log_info(f"  🎯 AlphaArcade still active ({alpha_arcade_mode} mode)")
        
        # Re-initialize engine with new config
        self.engine.config = self.config
        
        if changes_made:
            log_success(f"AI recommendation applied: {', '.join(changes_made)}")
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        log_warning("\nShutdown signal received...")
        self.state.running = False
        self._shutdown_event.set()
        
        # Stop real-time sniper if running
        if self._realtime_sniper and self._realtime_sniper.running:
            log_info("🥷 Stopping real-time mint sniper...")
            self._realtime_sniper.stop()
        
        # On Windows, force exit after second Ctrl+C
        if hasattr(self, '_shutdown_requested') and self._shutdown_requested:
            log_warning("Force exiting...")
            os._exit(0)
        self._shutdown_requested = True
    
    def run(self):
        """Main trading loop."""
        log_success("Trading bot started!")
        print(f"\n{Fore.YELLOW}Press Ctrl+C to stop the bot (press twice to force quit){Style.RESET_ALL}\n")
        
        # Start real-time sniper thread if configured
        if self._realtime_sniper and not self._sniper_thread:
            log_info("🥷 Starting real-time mint sniper thread...")
            self._sniper_thread = threading.Thread(
                target=self._realtime_sniper.stream_and_snipe,
                name="MintSniper",
                daemon=True
            )
            self._sniper_thread.start()
            log_success("🥷 Real-time sniper running in background")
        
        iteration = 0
        last_update_check = datetime.now()
        
        while self.state.running:
            try:
                iteration += 1
                log_info(f"=== Trading Cycle {iteration} ===")
                
                # Periodic update check (once per day during trading)
                if (datetime.now() - last_update_check).total_seconds() >= UPDATE_CHECK_INTERVAL:
                    try:
                        updater = AutoUpdater()
                        result = updater.check_for_updates(silent=True)
                        if result.get("available"):
                            log_info(f"🔄 Update available: v{result['remote_version']} (current: v{VERSION})")
                            log_info(f"   Use menu option 4 or restart bot to update")
                        last_update_check = datetime.now()
                    except Exception:
                        pass  # Silent failure for update check
                
                # Update state
                self.engine.update_state()
                
                # Check stop conditions
                if self.engine.check_stop_conditions():
                    log_warning("Stop condition met - shutting down")
                    break
                
                # Check position stop losses and take profits
                self.engine.check_position_stops()
                
                # Display status every 5 iterations
                if iteration % 5 == 1:
                    self.ui.display_status(self.state, self.config)
                
                # AI Dynamic Re-evaluation (if enabled and LLM available)
                if self.config.ai_dynamic_reeval:
                    strategy_llm = get_llm_for_task(self.config, "strategy")
                    if strategy_llm or self.config.llm_model:
                        self._check_ai_reeval()
                
                # Find opportunities
                opportunities = self.engine.find_opportunities()
                
                if opportunities:
                    self.ui.display_opportunities(opportunities)
                    
                    # Separate buy and sell opportunities
                    buy_opps = [o for o in opportunities if o["signal"] == "BUY"]
                    sell_opps = [o for o in opportunities if o["signal"] == "SELL"]
                    
                    # Execute sells FIRST (to free up positions and capital)
                    sells_executed = 0
                    for opp in sell_opps:
                        if not self.state.running:
                            break
                        if opp["asset_id"] in self.state.positions:
                            # Use specific reason from opportunity if available
                            sell_reason = opp.get("reason", "Sell Signal")
                            if opp.get("ta_triggered"):
                                sell_reason = "TA Sell Signal"
                            self.engine.execute_sell(opp["asset_id"], reason=sell_reason)
                            sells_executed += 1
                            time.sleep(2)
                    
                    if sells_executed > 0:
                        log_success(f"Executed {sells_executed} sell(s)")
                    
                    # Then execute buys
                    buys_executed = 0
                    buys_skipped_max_pos = 0
                    buys_skipped_held = 0
                    
                    for opp in buy_opps[:5]:  # Max 5 new trades per cycle
                        # Check if bot is still running
                        if not self.state.running:
                            break
                        
                        # Check if we already have this position
                        if opp["asset_id"] in self.state.positions:
                            buys_skipped_held += 1
                            continue
                        
                        # Check max positions
                        if len(self.state.positions) >= self.config.max_total_positions:
                            buys_skipped_max_pos += 1
                            continue
                        
                        # Calculate position size
                        available = self.wallet.get_algo_balance() - 5  # Keep 5 ALGO reserve for fees
                        position_size = min(
                            self.config.max_position_size_algo,
                            available * 0.20  # Max 20% of available per trade (more conservative)
                        )
                        
                        if position_size < 1:  # Minimum 1 ALGO
                            log_warning("Insufficient balance for new trades")
                            break
                        
                        # Round down to avoid dust issues
                        position_size = float(int(position_size * 1000)) / 1000
                        
                        # Execute buy
                        self.engine.execute_buy(opp, position_size)
                        buys_executed += 1
                        
                        # Small delay between trades
                        time.sleep(2)
                    
                    # Provide feedback on why no trades happened
                    if buys_executed == 0 and sells_executed == 0:
                        if buys_skipped_max_pos > 0:
                            log_info(f"Max positions ({len(self.state.positions)}/{self.config.max_total_positions}) reached")
                            log_info(f"  → Waiting for sells or stop-loss/take-profit to free up slots")
                        elif len(buy_opps) == 0 and len(sell_opps) == 0:
                            log_info("No actionable opportunities this cycle")
                else:
                    log_info("No trading opportunities found this cycle")
                
                # Check if we should exit
                if not self.state.running:
                    break
                
                # Wait for next cycle
                log_info(f"Next check in {self.config.check_interval_seconds} seconds...")
                
                # Use event wait instead of sleep for responsive shutdown
                if self._shutdown_event.wait(timeout=self.config.check_interval_seconds):
                    break
                
            except KeyboardInterrupt:
                log_warning("Keyboard interrupt received")
                break
            except Exception as e:
                log_error(f"Error in trading cycle: {e}")
                import traceback
                traceback.print_exc()
                
                if not self.state.running:
                    break
                    
                time.sleep(10)  # Wait before retrying
        
        self._shutdown()
    
    def _shutdown(self):
        """Graceful shutdown."""
        log_info("Shutting down trading bot...")
        
        # Final status
        self.engine.update_state()
        self.ui.display_status(self.state, self.config)
        
        # Summary
        print(f"\n{Fore.CYAN}{'='*60}")
        print("  TRADING SESSION SUMMARY")
        print(f"{'='*60}{Style.RESET_ALL}\n")
        
        print(f"Session Duration: {datetime.now() - self.state.trade_history[0].timestamp if self.state.trade_history else 'N/A'}")
        print(f"Total Trades: {self.state.total_trades}")
        print(f"Winning Trades: {self.state.winning_trades}")
        print(f"Losing Trades: {self.state.losing_trades}")
        
        win_rate = (self.state.winning_trades / self.state.total_trades * 100) if self.state.total_trades > 0 else 0
        print(f"Win Rate: {win_rate:.1f}%")
        
        # P/L from actual trades
        pnl_color = Fore.GREEN if self.state.total_pnl_algo >= 0 else Fore.RED
        print(f"\n{Fore.CYAN}Performance:{Style.RESET_ALL}")
        print(f"  Realized P/L: {pnl_color}{self.state.total_pnl_algo:+.4f} ALGO{Style.RESET_ALL}")
        
        # Calculate unrealized P/L from open positions
        unrealized_pnl = sum(pos.unrealized_pnl for pos in self.state.positions.values())
        unrealized_color = Fore.GREEN if unrealized_pnl >= 0 else Fore.RED
        print(f"  Unrealized P/L: {unrealized_color}{unrealized_pnl:+.4f} ALGO{Style.RESET_ALL}")
        
        total_pnl = self.state.total_pnl_algo + unrealized_pnl
        total_color = Fore.GREEN if total_pnl >= 0 else Fore.RED
        print(f"  Total P/L: {total_color}{total_pnl:+.4f} ALGO{Style.RESET_ALL}")
        
        # ROI based on actual P/L vs starting capital
        if self.state.starting_balance_algo > 0:
            roi = (total_pnl / self.state.starting_balance_algo) * 100
            roi_color = Fore.GREEN if roi >= 0 else Fore.RED
            print(f"  ROI: {roi_color}{roi:+.2f}%{Style.RESET_ALL}")
        
        print(f"\n{Fore.CYAN}Balances:{Style.RESET_ALL}")
        print(f"  Starting: {self.state.starting_balance_algo:.4f} ALGO")
        print(f"  Current:  {self.state.current_balance_algo:.4f} ALGO")
        
        # List recent trades
        if self.state.trade_history:
            print(f"\n{Fore.GREEN}Recent Trades:{Style.RESET_ALL}")
            for trade in self.state.trade_history[-10:]:
                pnl_str = f" | P/L: {trade.pnl:+.4f}" if trade.pnl != 0 else ""
                print(f"  {trade.timestamp.strftime('%H:%M:%S')} | {trade.action} {trade.asset_name[:20]} | {trade.value_algo:.4f} ALGO{pnl_str}")
        
        print(f"\n{Fore.GREEN}Goodbye!{Style.RESET_ALL}\n")


# ============================================================================
# ENTRY POINT
# ============================================================================

def main():
    """Main entry point."""
    print(f"""
{Fore.CYAN}╔════════════════════════════════════════════════════════════════════════════════════════════════════╗
║                                                                                                    ║
║   {Fore.WHITE}█████╗ ██╗      ██████╗  ██████╗ ██████╗  █████╗ ███╗   ██╗██████╗ {Fore.CYAN}                            ║
║  {Fore.WHITE}██╔══██╗██║     ██╔════╝ ██╔═══██╗██╔══██╗██╔══██╗████╗  ██║██╔══██╗{Fore.CYAN}                            ║
║  {Fore.WHITE}███████║██║     ██║  ███╗██║   ██║██████╔╝███████║██╔██╗ ██║██║  ██║{Fore.CYAN}                            ║
║  {Fore.WHITE}██╔══██║██║     ██║   ██║██║   ██║██╔══██╗██╔══██║██║╚██╗██║██║  ██║{Fore.CYAN}                            ║
║  {Fore.WHITE}██║  ██║███████╗╚██████╔╝╚██████╔╝██║  ██║██║  ██║██║ ╚████║██████╔╝{Fore.CYAN}                            ║
║  {Fore.WHITE}╚═╝  ╚═╝╚══════╝ ╚═════╝  ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝╚═════╝ {Fore.CYAN}                            ║
║                                                                                                    ║
╠════════════════════════════════════════════════════════════════════════════════════════════════════╣
║                                                                  {Fore.WHITE}@@@@@@{Fore.CYAN}                            ║
║                                                                 {Fore.WHITE}@@@@@@@@{Fore.CYAN}                           ║
║                                                                 {Fore.WHITE}@@@@@@@@{Fore.CYAN}                           ║
║                                                                {Fore.WHITE}@@@@@@@@{Fore.CYAN}                            ║
║                                                    {Fore.WHITE}@@@@@{Fore.CYAN}     {Fore.WHITE}@@@@@{Fore.CYAN}                                 ║
║                                      {Fore.WHITE}@@@@{Fore.CYAN}         {Fore.WHITE}@@@@@@@@@@@@@@{Fore.CYAN}                                   ║
║                                   {Fore.WHITE}@@@@@@{Fore.CYAN}                {Fore.WHITE}@@@@@@@@@{Fore.CYAN}  {Fore.WHITE}@@{Fore.CYAN}                              ║
║                                {Fore.WHITE}@@@@@@@{Fore.CYAN}                  {Fore.WHITE}@@@@{Fore.CYAN} {Fore.WHITE}@@@@@@@{Fore.CYAN}                               ║
║                              {Fore.WHITE}@@@@@{Fore.CYAN}                    {Fore.WHITE}@@@@{Fore.CYAN}   {Fore.RED}@@@%@@@@@{Fore.CYAN}                             ║
║                             {Fore.WHITE}@@@@{Fore.CYAN}                     {Fore.WHITE}@@@@{Fore.CYAN}  {Fore.RED}@%%%%%@@@@@@@{Fore.CYAN}                           ║
║                           {Fore.WHITE}@@@@{Fore.CYAN}                          {Fore.RED}@%%%%%%%%@{Fore.CYAN}   {Fore.WHITE}@@@@{Fore.CYAN}                          ║
║                         {Fore.WHITE}@@@@@{Fore.CYAN}                        {Fore.RED}@%%%%%%#%%%@{Fore.CYAN}     {Fore.WHITE}@@@@{Fore.CYAN}                         ║
║                       {Fore.WHITE}@@@@@@{Fore.CYAN}                      {Fore.RED}@%%%%%%%##%%%@{Fore.CYAN}        {Fore.WHITE}@@@@{Fore.CYAN}                       ║
║                     {Fore.WHITE}@@@@@@{Fore.CYAN}                     {Fore.RED}@%%%%%%%%%#%%%%%{Fore.CYAN}          {Fore.WHITE}@@@@{Fore.CYAN}                      ║
║                   {Fore.WHITE}@@@@@@@{Fore.CYAN}                   {Fore.RED}@%%%%%%%%%%##%%%%%@{Fore.CYAN}           {Fore.WHITE}@@@{Fore.CYAN}                  {Fore.WHITE}@{Fore.CYAN}   ║
║                 {Fore.WHITE}@@@@@@@@@{Fore.CYAN}                 {Fore.RED}%%%##%%%%%%%*%%%%%%@{Fore.CYAN}            {Fore.WHITE}@@@{Fore.CYAN}               {Fore.WHITE}@@@@@@@{Fore.CYAN}║
║               {Fore.WHITE}@@@@@{Fore.CYAN}  {Fore.WHITE}@@@{Fore.CYAN}               {Fore.RED}%%%#####%%%%%##%%%%%%@{Fore.CYAN}                              {Fore.WHITE}@@@@{Fore.CYAN} {Fore.WHITE}@@@{Fore.CYAN}║
║              {Fore.WHITE}@@@@{Fore.CYAN}   {Fore.WHITE}@@@@{Fore.CYAN}             {Fore.RED}@%########%%%%####%%%%@{Fore.CYAN}                                {Fore.WHITE}@@@@@@@{Fore.CYAN}║
║            {Fore.WHITE}@@@@@{Fore.CYAN}    {Fore.WHITE}@@@{Fore.CYAN}           {Fore.RED}@%###########%%#########%{Fore.CYAN}                               {Fore.WHITE}@@@@@@@@@{Fore.CYAN}║
║          {Fore.WHITE}@@@@@{Fore.CYAN}      {Fore.WHITE}@@@{Fore.CYAN}        {Fore.RED}@%########################%@{Fore.CYAN}                             {Fore.WHITE}@@@@@{Fore.CYAN}      ║
║        {Fore.WHITE}@@@@@{Fore.CYAN}        {Fore.WHITE}@@{Fore.CYAN}      {Fore.RED}@%##########################%%%{Fore.CYAN}                 {Fore.WHITE}@@@{Fore.CYAN}       {Fore.WHITE}@@@@@{Fore.CYAN}        ║
║      {Fore.WHITE}@@@@@{Fore.CYAN}               {Fore.RED}@%%############################%%%%%{Fore.CYAN}               {Fore.WHITE}@@@{Fore.CYAN}     {Fore.WHITE}@@@@@@{Fore.CYAN}         ║
║ {Fore.WHITE}@@@@@@@@{Fore.CYAN}                {Fore.RED}@##############################%%%%#%%%{Fore.CYAN}             {Fore.WHITE}@@@{Fore.CYAN}    {Fore.WHITE}@@@@@{Fore.CYAN}           ║
║{Fore.WHITE}@@@@@@@{Fore.CYAN}                    {Fore.RED}@%##########################%%%%%%*%%%%{Fore.CYAN}          {Fore.WHITE}@@@@{Fore.CYAN}  {Fore.WHITE}@@@@@{Fore.CYAN}             ║
║{Fore.WHITE}@@@{Fore.CYAN} {Fore.WHITE}@@@@{Fore.CYAN}                {Fore.WHITE}@{Fore.CYAN}    {Fore.RED}%%#######################%%%%%%%%#%%%%@{Fore.CYAN}        {Fore.WHITE}@@@{Fore.CYAN} {Fore.WHITE}@@@@@{Fore.CYAN}               ║
║{Fore.WHITE}@@@@@@@{Fore.CYAN}                {Fore.WHITE}@@@{Fore.CYAN}      {Fore.RED}%%####################%%%%%######%#%%@{Fore.CYAN}     {Fore.WHITE}@@@@@@@@{Fore.CYAN}                 ║
║  {Fore.WHITE}@@@{Fore.CYAN}                  {Fore.WHITE}@@@@{Fore.CYAN}       {Fore.RED}@%%################%###############%%%{Fore.CYAN}  {Fore.WHITE}@@@@@@@{Fore.CYAN}                   ║
║                        {Fore.WHITE}@@@@{Fore.CYAN}         {Fore.RED}@##############%###################%@@@@@@{Fore.CYAN}                     ║
║                         {Fore.WHITE}@@@@{Fore.CYAN}           {Fore.RED}%##########%#####################%@@@{Fore.CYAN}                       ║
║                          {Fore.WHITE}@@@@@{Fore.CYAN}           {Fore.RED}@#######%######################%@{Fore.CYAN}                         ║
║                            {Fore.WHITE}@@@@{Fore.CYAN}             {Fore.RED}@##########################@{Fore.CYAN}                           ║
║                             {Fore.WHITE}@@@@@{Fore.CYAN}        {Fore.WHITE}@@@@{Fore.CYAN} {Fore.RED}%%%####################@@{Fore.CYAN}                            ║
║                               {Fore.WHITE}@@@@@@{Fore.CYAN}   {Fore.WHITE}@@@@@{Fore.CYAN}                   {Fore.WHITE}@@@@@@{Fore.CYAN}                              ║
║                                 {Fore.WHITE}@@@@@@@@@@{Fore.CYAN}                   {Fore.WHITE}@@@@@@{Fore.CYAN}                                ║
║                                    {Fore.WHITE}@@@@@@@@@@@@@@{Fore.CYAN}          {Fore.WHITE}@@@@@{Fore.CYAN}                                   ║
║                                   {Fore.WHITE}@@@@@@@@@@@@@@@{Fore.CYAN}         {Fore.WHITE}@@{Fore.CYAN}                                       ║
║                                 {Fore.WHITE}@@@@@{Fore.CYAN}                                                              ║
║                            {Fore.WHITE}@@@@@@@@{Fore.CYAN}                                                                ║
║                           {Fore.WHITE}@@@@@@@@{Fore.CYAN}                                                                 ║
║                           {Fore.WHITE}@@@@@@@@{Fore.CYAN}                                                                 ║
║                            {Fore.WHITE}@@@@@@{Fore.CYAN}                                                                  ║
║                                                                                                    ║
╠════════════════════════════════════════════════════════════════════════════════════════════════════╣
║                                                                                                    ║
║                        {Fore.WHITE}███████╗██████╗ ██╗   ██╗    ███╗   ██╗███████╗████████╗██╗    ██╗ ██████╗ ██████╗ ██╗  ██╗███████╗{Fore.CYAN}  ║
║                        {Fore.WHITE}██╔════╝██╔══██╗╚██╗ ██╔╝    ████╗  ██║██╔════╝╚══██╔══╝██║    ██║██╔═══██╗██╔══██╗██║ ██╔╝██╔════╝{Fore.CYAN}  ║
║                        {Fore.WHITE}█████╗  ██████╔╝ ╚████╔╝     ██╔██╗ ██║█████╗     ██║   ██║ █╗ ██║██║   ██║██████╔╝█████╔╝ ███████╗{Fore.CYAN}  ║
║                        {Fore.WHITE}██╔══╝  ██╔══██╗  ╚██╔╝      ██║╚██╗██║██╔══╝     ██║   ██║███╗██║██║   ██║██╔══██╗██╔═██╗ ╚════██║{Fore.CYAN}  ║
║                        {Fore.WHITE}██║     ██║  ██║   ██║       ██║ ╚████║███████╗   ██║   ╚███╔███╔╝╚██████╔╝██║  ██║██║  ██╗███████║{Fore.CYAN}  ║
║                        {Fore.WHITE}╚═╝     ╚═╝  ╚═╝   ╚═╝       ╚═╝  ╚═══╝╚══════╝   ╚═╝    ╚══╝╚══╝  ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝{Fore.CYAN}  ║
║                                                                                                    ║
║                                  {Fore.YELLOW}ASA TRADING BOT • Algorand • Vestige • AI-Powered{Fore.CYAN}                  ║
║                                                                                                    ║
╚════════════════════════════════════════════════════════════════════════════════════════════════════╝{Style.RESET_ALL}
""")
    
    # Display version
    print(f"{Fore.CYAN}Version: {Fore.WHITE}v{VERSION}{Fore.CYAN} ({VERSION_DATE}){Style.RESET_ALL}")
    print(f"{Fore.CYAN}Repository: {Fore.BLUE}https://github.com/{GITHUB_REPO}{Style.RESET_ALL}")
    print()
    
    # Check for updates on startup (daily)
    updater = AutoUpdater()
    updater.run_startup_check()
    print()
    
    # Startup menu
    while True:
        print(f"{Fore.GREEN}What would you like to do?{Style.RESET_ALL}")
        print("  1. Start trading bot")
        print("  2. Manage custom presets")
        print("  3. View available LLM models")
        print("  4. Check for updates")
        print("  5. Exit")
        
        choice = input(f"\n{Fore.YELLOW}Select option (1-5) [1]: {Style.RESET_ALL}").strip() or "1"
        
        if choice == "1":
            bot = AlgorandTradingBot()
            bot.initialize()
            bot.run()
            break
        elif choice == "2":
            TradingBotUI.manage_presets()
        elif choice == "3":
            models = get_available_ollama_models()
            if models:
                print(f"\n{Fore.GREEN}Available Ollama Models:{Style.RESET_ALL}\n")
                for i, model in enumerate(models, 1):
                    name_str = model['name'] if model['name'] else "unknown"
                    params_str = f" ({model['params']})" if model['params'] else ""
                    family_str = f" [{model['family']}]" if model['family'] else ""
                    print(f"  {i}. {name_str}{params_str} - {model['size']}{family_str}")
                print()
            else:
                print(f"\n{Fore.YELLOW}No Ollama models found. Is Ollama running?{Style.RESET_ALL}\n")
        elif choice == "4":
            check_for_updates_menu()
        elif choice == "5":
            print(f"{Fore.CYAN}Goodbye!{Style.RESET_ALL}")
            break
        else:
            print(f"{Fore.YELLOW}Invalid option.{Style.RESET_ALL}\n")


if __name__ == "__main__":
    main()
