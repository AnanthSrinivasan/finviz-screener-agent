"""Ad-hoc share helper — uploads a report HTML to S3 under `share/<date>/`
and prints a TinyURL-shortened 7-day presigned URL.

Usage:
    python utils/share_via_s3.py                       # latest daily chart grid (default)
    python utils/share_via_s3.py --weekly              # latest weekly review only
    python utils/share_via_s3.py --date 2026-05-21     # specific day's daily chart grid
    python utils/share_via_s3.py --both                # latest daily + latest weekly
    python utils/share_via_s3.py path/to/x.html ...    # explicit files
    python utils/share_via_s3.py --no-short            # skip TinyURL, long URL only

Each file goes to `share/<YYYY-MM-DD>/<basename>` keyed by today's date.
Underlying presigned URL is valid for 7 days (SigV4 maximum).
"""
from __future__ import annotations

import os
import sys
import glob
import urllib.parse
import urllib.request
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


def _weekly_files() -> list[str]:
    return sorted(
        f for f in glob.glob("data/finviz_weekly_*.html") if "persistence" not in f
    )


def _arg_value(argv: list[str], flag: str) -> str | None:
    """Extract value after a flag, supporting both `--flag value` and `--flag=value`."""
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def _resolve_args(argv: list[str]) -> list[str]:
    """Convert flags + paths into a concrete file list.

    Defaults to **latest daily chart grid only**. Flags:
      --weekly         → latest weekly review only
      --date YYYY-MM-DD → that specific day's chart grid
      --both           → latest daily + latest weekly
      Explicit paths   → pass through as-is.
    """
    want_weekly = "--weekly" in argv
    want_both = "--both" in argv
    explicit_date = _arg_value(argv, "--date")

    # Strip flag values too so they aren't treated as paths
    skip_next = False
    paths: list[str] = []
    for a in argv:
        if skip_next:
            skip_next = False
            continue
        if a in ("--date",):
            skip_next = True
            continue
        if a.startswith("--"):
            continue
        paths.append(a)

    if explicit_date:
        target = f"data/finviz_chart_grid_{explicit_date}.html"
        if os.path.isfile(target):
            paths.append(target)
        else:
            print(f"WARN: {target} not found", file=sys.stderr)

    if want_weekly or want_both:
        wf = _weekly_files()
        if wf:
            paths.append(wf[-1])
        else:
            print("WARN: no finviz_weekly_*.html found", file=sys.stderr)

    # Default: latest daily — only when nothing else was requested.
    nothing_requested = (
        not want_weekly and not explicit_date and not paths and not want_both
    )
    if nothing_requested or want_both:
        d = _latest("finviz_chart_grid_*.html")
        if d and d not in paths:
            paths.append(d)
        elif not d:
            print("WARN: no finviz_chart_grid_*.html found", file=sys.stderr)

    return paths


def shorten(url: str) -> str | None:
    """TinyURL shortener — returns None on any error so caller can fall back."""
    try:
        api = "https://tinyurl.com/api-create.php?url=" + urllib.parse.quote(url, safe="")
        with urllib.request.urlopen(api, timeout=10) as r:
            short = r.read().decode().strip()
        return short if short.startswith("http") else None
    except Exception as e:
        print(f"WARN: TinyURL failed ({e}) — falling back to long URL", file=sys.stderr)
        return None


def main(argv: list[str]) -> int:
    paths = _resolve_args(argv)
    if not paths:
        print("usage: python utils/share_via_s3.py [--weekly | --date YYYY-MM-DD | --both | <file.html> ...]",
              file=sys.stderr)
        return 1
    no_short = "--no-short" in argv
    for path in paths:
        try:
            long_url = upload_and_sign(path)
            print(f"\n{os.path.basename(path)} ↓")
            if not no_short:
                short = shorten(long_url)
                if short:
                    print(short)
                    continue
            print(long_url)
        except Exception as e:
            print(f"ERROR {path}: {e}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
