"""Ad-hoc share helper — uploads a report HTML to S3 under `share/<date>/`
and prints a 7-day presigned URL.

Usage:
    python utils/share_via_s3.py path/to/report.html [more.html ...]

Uses the `personal-stack-admin` profile if AWS_PROFILE not set. Each file goes
to `share/<YYYY-MM-DD>/<basename>` keyed by today's date. Returns a presigned
URL valid for 7 days (the SigV4 maximum).
"""
from __future__ import annotations

import os
import sys
import boto3
from botocore.exceptions import ClientError
from datetime import date

BUCKET = "screener-data-repository"
REGION = "eu-central-1"
EXPIRY_SECONDS = 7 * 24 * 3600  # 7 days (SigV4 max)
PREFIX = "share"


def upload_and_sign(local_path: str, today: str | None = None) -> str:
    if not os.path.isfile(local_path):
        raise FileNotFoundError(local_path)
    today = today or date.today().isoformat()
    key = f"{PREFIX}/{today}/{os.path.basename(local_path)}"

    session = boto3.Session(
        profile_name=os.environ.get("AWS_SHARE_PROFILE", "personal-090960193599"),
        region_name=REGION,
    )
    s3 = session.client("s3")

    # Upload with text/html content-type so browser renders inline
    ct = "text/html; charset=utf-8" if local_path.endswith(".html") else "application/octet-stream"
    try:
        s3.upload_file(
            Filename=local_path,
            Bucket=BUCKET,
            Key=key,
            ExtraArgs={"ContentType": ct},
        )
    except ClientError as e:
        raise RuntimeError(f"upload failed for {local_path}: {e}") from e

    url = s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=EXPIRY_SECONDS,
    )
    return url


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python utils/share_via_s3.py <file.html> [<file2.html> ...]",
              file=sys.stderr)
        return 1
    for path in argv:
        try:
            url = upload_and_sign(path)
            print(f"\n{os.path.basename(path)} ↓\n{url}\n")
        except Exception as e:
            print(f"ERROR {path}: {e}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
