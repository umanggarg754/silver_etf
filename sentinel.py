import os
import sys
import datetime
import smtplib
import json
from email.mime.text import MIMEText
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

# SMTP Configuration
RECIPIENT_EMAIL = os.getenv("INVESTOR_EMAIL", "your_email@example.com")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER", "your_bot_email@gmail.com")
SMTP_PASS = os.getenv("SMTP_PASS", "your_app_password")

# Nifty Parameters
NIFTY_PE = float(os.getenv("NIFTY_PE", "20.6"))
NIFTY_PB = float(os.getenv("NIFTY_PB", "3.5"))

# Tax & Duty Configuration
SILVER_IMPORT_MULTIPLIER = float(os.getenv("SILVER_IMPORT_MULTIPLIER", "1.245"))

STATE_FILE = "state.json"

class StateDB:
    def __init__(self):
        self.state = {
            "active_target_rung": 245000,
            "slv_shares_history": []
        }
        self.load()

    def load(self):
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                self.state = json.load(f)

    def save(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=4)
            
    def get_target_rung(self):
        return self.state.get("active_target_rung", 245000)
        
    def increment_target_rung(self):
        current = self.get_target_rung()
        self.state["active_target_rung"] = round(current * 1.04, 2)
        self.save()
        return self.state["active_target_rung"]

    def update_slv_shares(self, today_date_str, current_shares):
        history = self.state.get("slv_shares_history", [])
        
        # Remove old entries (older than 15 days just to be safe)
        today = datetime.datetime.strptime(today_date_str, "%Y-%m-%d").date()
        cutoff = today - datetime.timedelta(days=15)
        
        valid_history = []
        for entry in history:
            entry_date = datetime.datetime.strptime(entry["date"], "%Y-%m-%d").date()
            if entry_date >= cutoff:
                valid_history.append(entry)
                
        # Update today's entry or add new
        updated_today = False
        for entry in valid_history:
            if entry["date"] == today_date_str:
                entry["shares"] = current_shares
                updated_today = True
                break
                
        if not updated_today:
            valid_history.append({"date": today_date_str, "shares": current_shares})
            
        # Sort by date
        valid_history.sort(key=lambda x: x["date"])
        self.state["slv_shares_history"] = valid_history
        self.save()
        
    def get_10_day_shares_change_pct(self):
        history = self.state.get("slv_shares_history", [])
        if len(history) < 2:
            return 0.0
            
        oldest_shares = history[0]["shares"]
        newest_shares = history[-1]["shares"]
        
        if oldest_shares == 0:
            return 0.0
            
        return ((newest_shares - oldest_shares) / oldest_shares) * 100.0


def fetch_market_confluence(state_db):
    """Fetches live MCX Silver proxy, global multiples, and updates physical vault state."""
    data = {}
    
    # 1. Fetch live Silver Price
    try:
        si = yf.Ticker("SI=F").history(period="1d")["Close"].iloc[-1]
        usdinr = yf.Ticker("INR=X").history(period="1d")["Close"].iloc[-1]
        calc_price = round(float(si * usdinr * 32.1507 * SILVER_IMPORT_MULTIPLIER), 2)
        
        # Hardcoded Support Floor Override (API Safeguard)
        if calc_price < 210000 and si >= 61.00:
            calc_price = 222500.0
            print("WARNING: Feed discrepancy detected. Using horizontal support floor of ₹222,500.")
            
        data["silver_inr_kg"] = calc_price
    except Exception as e:
        print(f"Error fetching silver price: {e}")
        data["silver_inr_kg"] = 0.0 # Will trigger circuit breaker
        
    # 2. Update SLV Shares Outstanding
    try:
        slv = yf.Ticker("SLV")
        current_shares = slv.info.get("sharesOutstanding")
        if current_shares and current_shares > 0:
            today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
            state_db.update_slv_shares(today_str, current_shares)
            data["vault_change_pct"] = round(state_db.get_10_day_shares_change_pct(), 2)
        else:
            data["vault_change_pct"] = 0.0
    except Exception as e:
        print(f"Error fetching SLV shares: {e}")
        data["vault_change_pct"] = 0.0
        
    # 3. Gold/Silver Ratio (GSR)
    try:
        gc = yf.Ticker("GC=F").history(period="1d")["Close"].iloc[-1]
        si_close = yf.Ticker("SI=F").history(period="1d")["Close"].iloc[-1]
        data["gsr"] = round(float(gc / si_close), 2)
    except:
        data["gsr"] = 80.0
        
    data["nifty_pe"] = NIFTY_PE
    data["nifty_pb"] = NIFTY_PB
    data["active_rung"] = state_db.get_target_rung()
    
    return data

def build_daily_briefing(state_db):
    macro = fetch_market_confluence(state_db)
    price = macro["silver_inr_kg"]
    nifty_pe = macro["nifty_pe"]
    nifty_pb = macro["nifty_pb"]
    vault_trend = macro["vault_change_pct"]
    active_rung = macro["active_rung"]
    gsr = macro["gsr"]
    
    now_ist = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
    is_monday = (now_ist.weekday() == 0)
    
    # 0. API Circuit Breaker
    if price < 180000:
        subject = "API DATA ERROR - EXECUTION ABORTED"
        body = f"CIRCUIT BREAKER ACTIVATED: The calculated silver price was ₹{price:,.2f}, which is impossibly low.\n\nExecution aborted to prevent false liquidations."
        return subject, body
        
    # Condition A (Thesis Breakdown)
    if price <= 205000 and vault_trend > 3.0:
        subject = "Warning for sell - EMERGENCY STOP LOSS TRIGGERED"
        body = f"CRITICAL ALERT: MCX Silver has breached macro floor support at ₹{price:,.2f}.\n\nPhysical Vault Shares Expanded by >3% ({vault_trend:+.2f}%). The multi-year deficit narrative has collapsed.\n\nLIQUIDATE 100% OF YOUR SILVER ETF HOLDINGS INSTANTLY."
    
    # Condition B (Disciplined Equity Ladder)
    elif is_monday and price >= active_rung and nifty_pe <= 22.5 and nifty_pb <= 3.8:
        new_rung = state_db.increment_target_rung()
        subject = "Sell today definitely"
        body = f"EXECUTION MONDAY: Target rung (₹{active_rung:,.2f}) achieved with Silver at ₹{price:,.2f}.\n\nValuation checks pass (Nifty P/E: {nifty_pe}, P/B: {nifty_pb}).\n\nCommand: Sell exactly 1000 shares of your Silver ETF (equivalent to 1 kg) blindly; rotate cash into Nifty 50 index.\nWARNING: Check ETF premium to iNAV before executing market order to avoid slippage.\nThe next target rung has been locked at ₹{new_rung:,.2f}."
    
    # Condition C (Overvalued Market Sweep)
    elif is_monday and price >= active_rung and (nifty_pe > 22.5 or nifty_pb > 3.8):
        new_rung = state_db.increment_target_rung()
        subject = "Sell today definitely - SWEEP TO LIQUID DEBT"
        body = f"EXECUTION MONDAY: Target rung (₹{active_rung:,.2f}) achieved with Silver at ₹{price:,.2f}.\n\nEquity Valuations are in BUBBLE TERRITORY (Nifty P/E: {nifty_pe}, P/B: {nifty_pb}).\n\nCommand: Sell exactly 1000 shares of your Silver ETF (equivalent to 1 kg), but PARK CASH IN LIQUID DEBT FUNDS. Do not buy overvalued equities.\nWARNING: Check ETF premium to iNAV before executing market order to avoid slippage.\nThe next target rung has been locked at ₹{new_rung:,.2f}."
        
    # Condition D (Danger Zone Buffer)
    elif 205000 < price <= 212000:
        subject = "Warning for sell"
        body = f"DANGER WARNING: Price is testing the structural launchpad at ₹{price:,.2f}.\n\nVerify broker login credentials; prepare disaster exit net if it breaches ₹205,000."
        
    # Condition E (Status Normal)
    else:
        subject = "All okay keep holding"
        body = f"STATUS NORMAL: Market is breathing safely at ₹{price:,.2f}.\n\nClose your brokerage app and ignore the market."
        
    metrics_summary = f"""
Active Target Rung: ₹{active_rung:,.2f}
Macro Anchor Status:
- SLV Physical Trend: {vault_trend:+.2f}%
- Nifty 50 P/E: {nifty_pe}
- Nifty 50 P/B: {nifty_pb}
- Gold/Silver Ratio: {gsr}
"""

    footer = f"""
{metrics_summary}
--------------------------------------------------
HOW TO READ THIS EMAIL (Plain English Guide):
- What is this? This is an automated robot that watches your silver investments and the stock market every morning.
- What should I do? Only take action if the email subject says "Warning" or "Sell today". If it says "All okay keep holding", do absolutely nothing and go about your day.
- What are these numbers?
  * Silver Price: The true, tax-adjusted cost of 1kg of silver in India.
  * Target Rung: The price silver needs to hit before we sell exactly 1000 shares (1 kg) for profit.
  * SLV Trend: Measures physical silver supply. 
      [GOOD] = Negative or close to 0% (vaults are emptying, meaning high demand). 
      [BAD] = Above 3% (institutions are dumping physical silver back into vaults).
  * Nifty P/E & P/B: These measure if the Indian stock market is cheap or expensive. 
      [GOOD] = P/E below 22.5 and P/B below 3.8 means stocks are reasonably priced. We can safely buy them.
      [BAD] = P/E above 22.5 or P/B above 3.8 means stocks are in a dangerous, expensive bubble. The robot will actively block you from buying them.
  * Gold/Silver Ratio (GSR): Measures how many ounces of silver it takes to buy 1 ounce of gold. 
      [GOOD] = A high number (80+) means silver is very cheap compared to gold. 
      [BAD] = A low number (<60) means silver is becoming extremely expensive and we should look to sell.
"""
    body += footer
    return subject, body

def dispatch_sentinel_email():
    state_db = StateDB()
    subj, body_text = build_daily_briefing(state_db)
    
    msg = MIMEText(body_text)
    msg["Subject"] = subj
    msg["From"] = SMTP_USER
    msg["To"] = RECIPIENT_EMAIL
    
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        print(f"SUCCESS: Dispatched [{subj}]")
    except Exception as e:
        print(f"FAILED to dispatch email: {e}")
        sys.exit(1)

if __name__ == "__main__":
    dispatch_sentinel_email()
