import csv
import json
import requests
import os
import concurrent.futures
import argparse
from urllib.error import URLError

# Get the absolute path to the parent directory where the CSV is located
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Set up argument parsing
parser = argparse.ArgumentParser(description="Test import CHED CSV data via API.")
parser.add_argument("--host", type=str, default="http://127.0.0.1", help="The API host URL (e.g., http://127.0.0.1 or https://api.example.com)")
parser.add_argument("--port", type=str, default="8000", help="The API port (if any, e.g., 8000)")
parser.add_argument("--csv", type=str, required=True, help="Path to the CSV file to import")
args = parser.parse_args()

CSV_FILE = args.csv
BASE_URL = args.host
if args.port:
    BASE_URL = f"{args.host}:{args.port}"
API_URL = f"{BASE_URL}/PostEnrolleeMatchedCHED"
# Change to the appropriate token once authenticated, or disable auth temporarily if needed for import
# For the sake of this script, we'll assume we can get a token via the token endpoint

import threading

class TokenManager:
    def __init__(self):
        self.access_token = None
        self.refresh_token = None
        self.lock = threading.Lock()
        self.login()

    def login(self):
        print(f"Getting token from {BASE_URL}/token...")
        try:
            response = requests.post(
                f"{BASE_URL}/token",
                data={"username": "admin", "password": "password123"}
            )
            if response.status_code == 200:
                data = response.json()
                self.access_token = data["access_token"]
                self.refresh_token = data.get("refresh_token")
                return
            
            print("Creating admin user...")
            requests.post(f"{BASE_URL}/signup", json={"username": "admin", "password": "password123"})
            response = requests.post(
                f"{BASE_URL}/token",
                data={"username": "admin", "password": "password123"}
            )
            data = response.json()
            self.access_token = data["access_token"]
            self.refresh_token = data.get("refresh_token")
        except requests.exceptions.ConnectionError as e:
            print(f"Error connecting to the API at {BASE_URL}.")
            print("Please ensure the API is running, or specify the correct host and port using --host and --port.")
            print(f"Details: {e}")
            import sys
            sys.exit(1)

    def get_access_token(self):
        with self.lock:
            return self.access_token

    def refresh(self):
        with self.lock:
            if not self.refresh_token:
                self.login()
                return
            
            response = requests.post(
                f"{BASE_URL}/refresh",
                json={"refresh_token": self.refresh_token}
            )
            if response.status_code == 200:
                self.access_token = response.json()["access_token"]
            else:
                self.login()

def process_row(row, token_mgr):
    access_token = token_mgr.get_access_token()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    match_result = row.get("Matching Result Concat", "").strip().upper()
    is_match = (match_result == "MATCH")
    
    # Some cleaning and type casting
    try:
        member_id = int(row.get("Entry_id", 0))
    except ValueError:
        member_id = 0
        
    try:
        year_level = int(row.get("Year level", 0))
    except ValueError:
        year_level = 0
        
    tuloy_id = row.get("dswd_member_id", "").strip()

    payload = {
        "status": is_match,
        "message": "FOUND" if is_match else "NOT FOUND",
        "enrollment_id": 0,  # We don't have this in CSV, default to 0
        "data": {
            "id": tuloy_id,
            "member_id": member_id,
            "student_id": 0, # Not explicitly available in CSV
            "mothers_maiden": None,
            "degree": {
                "program_name": row.get("Degree Program", ""),
                "date_enrolled": None,
                "program_duration": None,
                "academic_year": row.get("Academic Year", ""),
                "year_level": year_level if year_level > 0 else None,
                "semester": 1
            },
            "enrollment_status": "ENROLLED",
            "date_encoded": None,
            "date_updated": None
        } if is_match else None,
        "error_code": None
    }

    resp = requests.post(API_URL, json=payload, headers=headers)
    if resp.status_code == 401:
        token_mgr.refresh()
        headers["Authorization"] = f"Bearer {token_mgr.get_access_token()}"
        resp = requests.post(API_URL, json=payload, headers=headers)

    if resp.status_code != 200:
        print(f"Failed to insert row {tuloy_id}: {resp.status_code} - {resp.text}")
    return resp.status_code == 200

def process_chunk(chunk, token_mgr, chunk_index):
    success_count = 0
    print(f"[Chunk {chunk_index}] Starting chunk of {len(chunk)} rows...")
    
    for i, row in enumerate(chunk):
        if process_row(row, token_mgr):
            success_count += 1
            
        if (i + 1) % 1000 == 0:
            print(f"[Chunk {chunk_index}] Progress: {i + 1} / {len(chunk)} processed")
            
    print(f"[Chunk {chunk_index}] Completed. Success: {success_count} / {len(chunk)}")
    return success_count

def main():
    if not os.path.exists(CSV_FILE):
        print(f"CSV file {CSV_FILE} not found.")
        return
        
    token_mgr = TokenManager()
    print("Token obtained. Starting import...")

    chunks = []
    current_chunk = []
    
    with open(CSV_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if i >= 10:
                break
            current_chunk.append(row)
            if len(current_chunk) == 10000:
                chunks.append(current_chunk)
                current_chunk = []
                
    if current_chunk:
        chunks.append(current_chunk)

    total_rows = sum(len(c) for c in chunks)
    print(f"Loaded {total_rows} rows into {len(chunks)} chunks of up to 10,000 rows. Processing with 5 workers...")

    total_success = 0
    
    # Process using a ThreadPoolExecutor with 5 workers (parallel of 5)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        # submit all chunks
        futures = {executor.submit(process_chunk, chunk, token_mgr, idx): idx for idx, chunk in enumerate(chunks, 1)}
        
        for future in concurrent.futures.as_completed(futures):
            try:
                successes = future.result()
                total_success += successes
            except Exception as e:
                idx = futures[future]
                print(f"[Chunk {idx}] Failed with exception: {e}")

    print(f"Done! Successfully imported {total_success} out of {total_rows} rows.")

if __name__ == "__main__":
    main()
