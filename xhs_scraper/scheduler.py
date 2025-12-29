import subprocess
import time
import random
import sys
import os
from datetime import datetime

def run_script(script_name):
    """Run a python script and wait for it to finish."""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting {script_name}...")
    try:
        # Use sys.executable to ensure we use the same python interpreter
        result = subprocess.run([sys.executable, script_name], check=True)
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {script_name} finished successfully.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error running {script_name}: {e}")
        return False
    except Exception as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Unexpected error running {script_name}: {e}")
        return False

def main():
    # Ensure we are in the correct directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)
    
    scraper_script = "xiaohongshu_explore_scraper.py"
    email_script = "send_email.py"

    print(f"Starting scheduler in {script_dir}")
    print("Press Ctrl+C to stop the scheduler.")

    try:
        while True:
            # 1. Run the scraper
            if run_script(scraper_script):
                # 2. Run the email sender immediately after scraper finishes
                run_script(email_script)
            else:
                print("Scraper failed, skipping email sending.")

            # 3. Wait for a random time between 1 and 10 minutes (60 to 600 seconds)
            wait_seconds = random.randint(60, 600)
            wait_minutes = wait_seconds / 60
            
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Sleeping for {wait_seconds} seconds ({wait_minutes:.2f} minutes)...")
            print("-" * 50)
            time.sleep(wait_seconds)

    except KeyboardInterrupt:
        print("\nScheduler stopped by user.")

if __name__ == "__main__":
    main()
