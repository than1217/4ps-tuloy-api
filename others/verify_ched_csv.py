import argparse
import csv
import json
import os
import sys
import tempfile
import time
import threading
import concurrent.futures
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from tqdm import tqdm

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
load_dotenv(dotenv_path=os.path.join(PROJECT_DIR, ".env"), override=True)

CHECKPOINT_COLUMNS = (
    "TULOY ID",
    "MEMBER ID",
    "VERIFY STATUS",
    "CLIENT STATUS",
    "ROSTER STATUS",
    "IMPORT ERROR",
)

VERIFY_STATUS_VALUES = {"Matched", "Not Found", "Multiple Match Found"}


class ApiRequestError(RuntimeError):
    pass


class RequestTimeout(RuntimeError):
    pass


def read_json_response(response):
    return json.loads(response.read().decode("utf-8"))


class ApiClient:
    def __init__(self, base_url, username, password, timeout):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.access_token = None
        self.refresh_token = None
        self.auth_lock = threading.Lock()

    def _request(self, path, body, content_type, authenticated=True, retry_auth=True):
        headers = {"Content-Type": content_type}
        request_token = None
        if authenticated:
            with self.auth_lock:
                if not self.access_token:
                    self.login()
                request_token = self.access_token
            headers["Authorization"] = "Bearer " + str(request_token)

        request = Request(
            f"{self.base_url}/{path.lstrip('/')}",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return read_json_response(response)
        except HTTPError as error:
            response_body = error.read().decode("utf-8", errors="replace")
            if authenticated and error.code == 401 and retry_auth:
                with self.auth_lock:
                    if self.access_token == request_token:
                        self.refresh()
                return self._request(path, body, content_type, retry_auth=False)
            raise ApiRequestError(
                f"POST {path} failed with HTTP {error.code}: {response_body}"
            ) from error
        except TimeoutError as error:
            raise RequestTimeout(
                f"POST {path} timed out after {self.timeout:g} seconds"
            ) from error
        except URLError as error:
            raise RuntimeError(
                f"Could not reach {self.base_url}: {error.reason}"
            ) from error

    def login(self):
        if not self.username or not self.password:
            raise RuntimeError(
                "Set TULOY_API_USERNAME and TULOY_API_PASSWORD, "
                "or pass --username and --password."
            )
        response = self._request(
            "/token",
            urlencode(
                {"username": self.username, "password": self.password}
            ).encode("utf-8"),
            "application/x-www-form-urlencoded",
            authenticated=False,
        )
        self.access_token = response["access_token"]
        self.refresh_token = response.get("refresh_token")

    def refresh(self):
        if not self.refresh_token:
            self.login()
            return

        request = Request(
            f"{self.base_url}/refresh",
            data=json.dumps({"refresh_token": self.refresh_token}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                token_data = read_json_response(response)
                self.access_token = token_data["access_token"]
                self.refresh_token = token_data.get("refresh_token", self.refresh_token)
        except TimeoutError as error:
            raise RequestTimeout(
                f"POST /refresh timed out after {self.timeout:g} seconds"
            ) from error
        except (HTTPError, URLError):
            self.login()

    def post_json(self, path, payload):
        return self._request(
            path,
            json.dumps(payload).encode("utf-8"),
            "application/json",
        )


def verify_payload(row):
    first_name = (row.get("First name") or row.get("MEMBER First name") or "").strip()
    last_name = (row.get("Last name") or row.get("MEMBER Last name") or "").strip()
    birthdate = (row.get("Birthdate") or row.get("Birthday") or "").strip()
    if not first_name or not last_name or not birthdate:
        raise ValueError("First name, Last name, and Birthdate are required")

    for date_format in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            birthdate = datetime.strptime(birthdate, date_format).date().isoformat()
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"Invalid Birthdate: {birthdate!r}")

    return {
        "first_name": first_name,
        "last_name": last_name,
        "date_of_birth": birthdate,
    }


def process_row_task(row, client, args):
    v_succ, fail = 0, 0
    try:
        # Verification Step
        if args.reverify or not (row.get("VERIFY STATUS") or "").strip():
            if args.reverify:
                row["VERIFY STATUS"] = ""
                row["TULOY ID"] = ""
                row["MEMBER ID"] = ""
                row["CLIENT STATUS"] = ""
                row["ROSTER STATUS"] = ""
            try:
                payload = verify_payload(row)
                result = client.post_json("/Verify4psBene", payload)
                status = result.get("status")
                if status not in VERIFY_STATUS_VALUES:
                    raise RuntimeError(f"Unexpected verification response: {result!r}")
                if status == "Matched" and (
                    result.get("tuloy_id") is None or result.get("member_id") is None
                ):
                    raise RuntimeError("Verification returned Matched without both IDs")
                row["VERIFY STATUS"] = status
                tuloy_id = result.get("tuloy_id")
                member_id = result.get("member_id")
                row["TULOY ID"] = "" if tuloy_id is None else str(tuloy_id)
                row["MEMBER ID"] = "" if member_id is None else str(member_id)
                row["CLIENT STATUS"] = result.get("client_status") or ""
                row["ROSTER STATUS"] = result.get("roster_status") or ""
                row["IMPORT ERROR"] = ""
                v_succ = 1
            except (ApiRequestError, RequestTimeout, RuntimeError, ValueError, KeyError) as error:
                row["IMPORT ERROR"] = f"Verification failed: {error}"
                fail = 1

    except Exception as e:
        row["IMPORT ERROR"] = f"Unexpected error: {e}"
        fail = 1

    return v_succ, fail


def _process_and_write_chunk(chunk, client, args, writer, target_file, pbar, stats):
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(process_row_task, r, client, args) for r in chunk]
        
        for future in concurrent.futures.as_completed(futures):
            v_succ, fail = future.result()
            stats["verify_successes"] += v_succ
            stats["failures"] += fail
            
            pbar.set_postfix({
                "Verified": stats["verify_successes"], 
                "Errors": stats["failures"]
            })
            pbar.update(1)
            
    # Rows were modified in-place concurrently; write them in original sequential order
    for row in chunk:
        writer.writerow(row)

    target_file.flush()
    os.fsync(target_file.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify CHED CSV records and submit them through the Tuloy API."
    )
    parser.add_argument("--csv", required=True, help="CSV file to process")
    parser.add_argument(
        "--base-url",
        default=os.getenv("TULOY_API_BASE_URL"),
        help="API root URL (defaults to TULOY_API_BASE_URL in the project .env)",
    )
    parser.add_argument(
        "--username",
        default=os.getenv("TULOY_API_USERNAME"),
        help="API username (or set TULOY_API_USERNAME)",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("TULOY_API_PASSWORD"),
        help="API password (or set TULOY_API_PASSWORD)",
    )
    parser.add_argument("--timeout", type=float, default=30, help="Request timeout in seconds")
    parser.add_argument(
        "--reverify",
        action="store_true",
        help="Re-run verification even when the CSV already has a VERIFY STATUS",
    )
    args = parser.parse_args(argv)

    try:
        if not args.base_url:
            raise RuntimeError(
                "Set TULOY_API_BASE_URL in the project .env file, "
                "or pass --base-url."
            )
        if args.timeout <= 0:
            raise ValueError("--timeout must be greater than zero")

        if not os.path.exists(args.csv):
            print(f"CSV file {args.csv} not found.")
            return 1

        # 1. Count total rows in the source CSV (fast pass)
        with open(args.csv, "r", encoding="utf-8-sig") as f:
            total_rows = sum(1 for _ in f) - 1 # Subtract header
            
        if total_rows <= 0:
            print("CSV contains no data rows.")
            return 0

        # 2. Check for temporary progress file
        temp_csv = args.csv + ".tmp"
        output_csv = args.csv.rsplit(".", 1)[0] + " VERIFIED.csv"
        
        processed_count = 0
        if os.path.exists(temp_csv):
            with open(temp_csv, "r", encoding="utf-8-sig") as f:
                processed_count = sum(1 for _ in f) - 1
            if processed_count < 0:
                processed_count = 0
                
        if processed_count >= total_rows:
            print(f"All rows already processed. Finalizing file replacement to {output_csv}...")
            os.replace(temp_csv, output_csv)
            return 0

        client = ApiClient(args.base_url, args.username, args.password, args.timeout)
        stats = {"verify_successes": 0, "failures": 0}
        CHUNK_SIZE = 10000

        # 3. Stream processing
        with open(args.csv, "r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if not reader.fieldnames:
                raise ValueError("CSV file is empty or has no header row")
                
            header = set(reader.fieldnames)
            has_raw_names = {"First name", "Last name", "Birthdate"} <= header
            has_enriched_names = {
                "MEMBER First name",
                "MEMBER Last name",
                "Birthday",
            } <= header
            
            if not has_raw_names and not has_enriched_names:
                raise ValueError(
                    "CSV must include First name, Last name, and Birthdate "
                    "(or the corresponding MEMBER First name, MEMBER Last name, "
                    "and Birthday columns)."
                )
                
            fieldnames = list(reader.fieldnames)
            for column in CHECKPOINT_COLUMNS:
                if column not in fieldnames:
                    fieldnames.append(column)
                    
            mode = "a" if processed_count > 0 else "w"
            with open(temp_csv, mode, encoding="utf-8-sig", newline="") as target:
                writer = csv.DictWriter(target, fieldnames=fieldnames, extrasaction="ignore")
                if mode == "w":
                    writer.writeheader()
                    
                # Skip already processed rows
                for _ in range(processed_count):
                    try:
                        next(reader)
                    except StopIteration:
                        break
                        
                chunk = []
                pbar = tqdm(total=total_rows, initial=processed_count, desc="Processing CSV rows", unit="row")
                
                # Stream row by row
                for row in reader:
                    chunk.append(row)
                    if len(chunk) >= CHUNK_SIZE:
                        _process_and_write_chunk(chunk, client, args, writer, target, pbar, stats)
                        chunk = []
                        
                if chunk:
                    _process_and_write_chunk(chunk, client, args, writer, target, pbar, stats)
                    
                pbar.close()

        # 4. Atomic file replacement
        for attempt in range(5):
            try:
                os.replace(temp_csv, output_csv)
                break
            except PermissionError as error:
                if attempt < 4:
                    time.sleep(0.25 * (2**attempt))
                    continue
                winerror = getattr(error, "winerror", None)
                windows_error = f" Windows error {winerror}." if winerror else ""
                raise RuntimeError(
                    f"Processing complete, but cannot update CSV checkpoint '{output_csv}'. "
                    f"{windows_error} Close any preview/editor preventing replacement."
                ) from error

        print(
            f"Finished. Verified: {stats['verify_successes']}; "
            f"Failures: {stats['failures']}."
        )
        return 1 if stats['failures'] else 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Import aborted: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
