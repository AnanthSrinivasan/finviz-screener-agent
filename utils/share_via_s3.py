"""Ad-hoc share helper — uploads a report HTML to S3 under `share/<date>/`
and prints a 7-day presigned URL.

Usage:
    python utils/share_via_s3.py --daily            # latest daily chart grid only
    python utils/share_via_s3.py --weekly           # latest weekly review only
    python utils/share_via_s3.py --daily --weekly   # both (also: no args = both)
    python utils/share_via_s3.py path/to/x.html ... # explicit files

Each file goes to `share/<YYYY-MM-DD>/<basename>` keyed by today's date.
Returns a presigned URL valid for 7 days (the SigV4 maximum).
"""
from __future__ import annotations

import os
import sys
import glob
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


def _latest(pattern: str) -> str | None:
    """Return the latest file matching `data/<pattern>` by sorted filename."""
    files = sorted(glob.glob(os.path.join("data", pattern)))
    return files[-1] if files else None


def _resolve_args(argv: list[str]) -> list[str]:
    """Convert flags + paths into a concrete file list.
    No args / --daily --weekly → both.
    --daily only → latest daily.
    --weekly only → latest weekly.
    Explicit paths pass through.
    """
    want_daily = "--daily" in argv
    want_weekly = "--weekly" in argv
    paths = [a for a in argv if not a.startswith("--")]
    if not want_daily and not want_weekly and not paths:
        want_daily = want_weekly = True
    if want_daily:
        d = _latest("finviz_chart_grid_*.html")
        if d:
            paths.append(d)
        else:
            print("WARN: no finviz_chart_grid_*.html found", file=sys.stderr)
    if want_weekly:
        candidates = sorted(
            f for f in glob.glob("data/finviz_weekly_*.html")
            if "persistence" not in f
        )
        if candidates:
            paths.append(candidates[-1])
        else:
            print("WARN: no finviz_weekly_*.html found", file=sys.stderr)
    return paths


def main(argv: list[str]) -> int:
    paths = _resolve_args(argv)
    if not paths:
        print("usage: python utils/share_via_s3.py [--daily] [--weekly] [<file.html> ...]",
              file=sys.stderr)
        return 1
    for path in paths:
        try:
            url = upload_and_sign(path)
            print(f"\n{os.path.basename(path)} ↓\n{url}\n")
        except Exception as e:
            print(f"ERROR {path}: {e}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
