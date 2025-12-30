import time
import random
import sys
import os
from datetime import datetime
import xiaohongshu_explore_scraper
import send_email

def run_task(task_func, task_name, *args, **kwargs):
    """Run a python function and handle exceptions."""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting {task_name}...")
    try:
        # Call the function directly
        result = task_func(*args, **kwargs)
        
        # Check if the function returned a non-zero exit code (if it returns an int)
        if isinstance(result, int) and result != 0:
             print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {task_name} failed with exit code {result}.")
             return False

        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {task_name} finished successfully.")
        return True
    except Exception as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error running {task_name}: {e}")
        return False

def main():
    # Ensure we are in the correct directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)
    
    print(f"Starting scheduler in {script_dir}")
    print("Press Ctrl+C to stop the scheduler.")

    try:
        while True:
            # 1. Run the scraper
            # xiaohongshu_explore_scraper.main accepts argv list
            if run_task(xiaohongshu_explore_scraper.main, "scraper", []):
                # 2. Run the email sender immediately after scraper finishes
                run_task(send_email.send_email, "email_sender")
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
