"""
archive_data.py — Archive old dated data files to S3.

Scans data/ for files matching *YYYY-MM-DD* patterns older than 70 days,
uploads them to S3 under YYYY/MM/DD/<filename>, verifies the upload,
then deletes the local file.

Env vars required:
  AWS_ACCESS_KEY_ID
  AWS_SECRET_ACCESS_KEY
  AWS_BUCKET_NAME
  AWS_REGION
"""

import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

# Files that must never be archived (stateful, undated files)
NEVER_ARCHIVE = {
    "positions.json",
    "trading_state.json",
    "watchlist.json",
    "alerts_state.json",
    "market_monitor_history.json",
    "paper_stops.json",
}

DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent.parent / "data")))
ARCHIVE_DAYS = 70
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


def check_credentials():
    """Return True if all required AWS env vars are present."""
    required = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_BUCKET_NAME", "AWS_REGION"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print("WARNING: Missing AWS environment variables: " + ", ".join(missing))
        print("Skipping archive run.")
        return False
    return True


def extract_date(filename):
    """Return a date object parsed from the YYYY-MM-DD substring in filename, or None."""
    match = re.search(r"\d{4}-\d{2}-\d{2}", filename)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(), "%Y-%m-%d").date()
    except ValueError:
        return None


def s3_key(file_date, filename):
    """Return the S3 key: YYYY/MM/DD/filename."""
    return "{}/{}/{}/{}".format(
        file_date.strftime("%Y"),
        file_date.strftime("%m"),
        file_date.strftime("%d"),
        filename,
    )


def main():
    if not check_credentials():
        sys.exit(0)

    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    bucket = os.environ["AWS_BUCKET_NAME"]
    region = os.environ["AWS_REGION"]

    s3 = boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    )

    today = date.today()
    cutoff = today.toordinal() - ARCHIVE_DAYS  # files older than this ordinal day

    uploaded = 0
    skipped = 0
    errors = []

    if not DATA_DIR.exists():
        print("data/ directory not found at " + str(DATA_DIR))
        sys.exit(1)

    candidates = sorted(DATA_DIR.iterdir())

    for path in candidates:
        if not path.is_file():
            skipped += 1
            continue

        filename = path.name

        # Never-archive list
        if filename in NEVER_ARCHIVE:
            skipped += 1
            continue

        # Must have a date in the filename
        file_date = extract_date(filename)
        if file_date is None:
            skipped += 1
            continue

        # Only archive if old enough
        if file_date.toordinal() > cutoff:
            skipped += 1
            continue

        key = s3_key(file_date, filename)

        # Upload
        try:
            print("Uploading {} -> s3://{}/{}".format(filename, bucket, key))
            s3.upload_file(str(path), bucket, key)
        except (BotoCoreError, ClientError, OSError) as exc:
            msg = "ERROR uploading {}: {}".format(filename, exc)
            print(msg)
            errors.append(msg)
            continue

        # Verify upload via head_object
        try:
            s3.head_object(Bucket=bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            msg = "ERROR verifying upload for {}: {}".format(filename, exc)
            print(msg)
            errors.append(msg)
            continue

        # Delete local file only after confirmed upload
        try:
            path.unlink()
            print("Deleted local file: " + filename)
        except OSError as exc:
            msg = "ERROR deleting {}: {}".format(filename, exc)
            print(msg)
            errors.append(msg)
            continue

        uploaded += 1

    # Summary
    print("")
    print("=== Archive summary ===")
    print("Uploaded and removed : " + str(uploaded))
    print("Skipped              : " + str(skipped))
    print("Errors               : " + str(len(errors)))
    if errors:
        print("Error details:")
        for e in errors:
            print("  " + e)
        sys.exit(1)


if __name__ == "__main__":
    main()
