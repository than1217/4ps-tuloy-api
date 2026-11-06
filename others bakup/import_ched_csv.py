import csv
import json
import requests
import os
import time
import concurrent.futures
from urllib.error import URLError

# Get the absolute path to the parent directory where the CSV is located
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(SCRIPT_DIR, "CHED-Matching.csv")
API_URL = "http://127.0.0.1:8000/PostEnrolleeMatchedCHED"
# Change to the appropriate token once authenticated, or disable auth temporarily if needed for import
# For the sake of this script, we'll assume we can get a token via the token endpoint

def get_token():
    # Credentials come from the environment; there is no default account and no auto-signup.
    username = os.environ.get("IMPORT_API_USERNAME")
    password = os.environ.get("IMPORT_API_PASSWORD")
    if not username or not password:
        raise SystemExit("Set IMPORT_API_USERNAME and IMPORT_API_PASSWORD for an existing API user.")
    print("Getting token...")
    response = requests.post("http://127.0.0.1:8000/token", data={"username": username, "password": password})
    if response.status_code != 200:
        raise SystemExit(f"Login failed with HTTP {response.status_code}.")
    return response.json()["access_token"]

def process_row(row, token):
    headers = {
        "Authorization": f"Bearer {token}",
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
            "mothers_maiden": {
                "first_name": row.get("GRANTEE FIRST_NAME", ""),
                "middle_name": row.get("GRANTEE MIDDLE_NAME", ""),
                "last_name": row.get("GRANTEE LAST_NAME", "")
            },
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

    # 429 is answered before the request reaches the handler, so retrying it cannot double-insert.
    # Other failures are not retried: the endpoint is not idempotent.
    for _attempt in range(5):
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=60)
        if resp.status_code != 429:
            break
        time.sleep(int(resp.headers.get("Retry-After", "5")))
    if resp.status_code != 200:
        # No identifiers or response body in the output: they can carry personal data.
        print(f"Failed to insert a row: HTTP {resp.status_code}, request_id={resp.headers.get('X-Request-ID')}")
    return resp.status_code == 200

def process_chunk(chunk, token, chunk_index):
    success_count = 0
    print(f"[Chunk {chunk_index}] Starting chunk of {len(chunk)} rows...")
    
    for i, row in enumerate(chunk):
        if process_row(row, token):
            success_count += 1
            
        if (i + 1) % 1000 == 0:
            print(f"[Chunk {chunk_index}] Progress: {i + 1} / {len(chunk)} processed")
            
    print(f"[Chunk {chunk_index}] Completed. Success: {success_count} / {len(chunk)}")
    return success_count

def main():
    if not os.path.exists(CSV_FILE):
        print(f"CSV file {CSV_FILE} not found.")
        return
        
    token = get_token()
    print("Token obtained. Starting import...")

    chunks = []
    current_chunk = []
    
    with open(CSV_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
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
        futures = {executor.submit(process_chunk, chunk, token, idx): idx for idx, chunk in enumerate(chunks, 1)}
        
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
