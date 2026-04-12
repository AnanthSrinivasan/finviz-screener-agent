"""
Unit tests for archive_data.py.

Run: python -m unittest tests.test_archive -v

No real S3 calls — boto3.client is mocked throughout.
No AWS credentials required.
"""

import os
import sys
import tempfile
import unittest
import unittest.mock
from datetime import date, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_old_date(days_ago=80):
    """Return a date string YYYY-MM-DD that is `days_ago` days before today."""
    d = date.today() - timedelta(days=days_ago)
    return d.strftime("%Y-%m-%d")


def _make_recent_date(days_ago=1):
    """Return a date string YYYY-MM-DD that is `days_ago` days before today."""
    d = date.today() - timedelta(days=days_ago)
    return d.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Tests for pure helper functions (no S3, no filesystem)
# ---------------------------------------------------------------------------

class TestExtractDate(unittest.TestCase):

    def setUp(self):
        from utils import archive_data
        self.extract_date = archive_data.extract_date

    def test_extract_date_valid(self):
        """Filename containing YYYY-MM-DD returns the correct date object."""
        result = self.extract_date("finviz_screeners_2025-11-03.csv")
        self.assertEqual(result, date(2025, 11, 3))

    def test_extract_date_none(self):
        """Filename without any date pattern returns None."""
        result = self.extract_date("positions.json")
        self.assertIsNone(result)

    def test_extract_date_none_partial(self):
        """Filename with a partial / non-date number string returns None."""
        result = self.extract_date("report_2025.csv")
        self.assertIsNone(result)

    def test_extract_date_picks_first_match(self):
        """When multiple dates appear, the first one is used."""
        result = self.extract_date("backup_2024-01-15_to_2024-02-20.json")
        self.assertEqual(result, date(2024, 1, 15))


class TestS3KeyFormat(unittest.TestCase):

    def setUp(self):
        from utils import archive_data
        self.s3_key = archive_data.s3_key

    def test_s3_key_format(self):
        """s3_key returns YYYY/MM/DD/filename."""
        file_date = date(2025, 3, 7)
        filename = "finviz_screeners_2025-03-07.csv"
        key = self.s3_key(file_date, filename)
        self.assertEqual(key, "2025/03/07/finviz_screeners_2025-03-07.csv")

    def test_s3_key_zero_pads_month_and_day(self):
        """Single-digit months and days are zero-padded."""
        file_date = date(2025, 1, 5)
        key = self.s3_key(file_date, "market_monitor_2025-01-05.json")
        self.assertEqual(key, "2025/01/05/market_monitor_2025-01-05.json")


# ---------------------------------------------------------------------------
# Tests for check_credentials / missing env vars
# ---------------------------------------------------------------------------

class TestMissingCredentials(unittest.TestCase):

    def test_missing_credentials_exits_clean(self):
        """When AWS env vars are absent, main() calls sys.exit(0) before importing boto3."""
        # Remove all AWS env vars for this test
        env_patch = {
            "AWS_ACCESS_KEY_ID": "",
            "AWS_SECRET_ACCESS_KEY": "",
            "AWS_BUCKET_NAME": "",
            "AWS_REGION": "",
        }
        with unittest.mock.patch.dict(os.environ, env_patch, clear=False):
            # Also make sure the vars are truly absent (not just empty)
            for key in env_patch:
                os.environ.pop(key, None)

            from utils import archive_data
            with self.assertRaises(SystemExit) as ctx:
                archive_data.main()

            self.assertEqual(ctx.exception.code, 0)

    def test_check_credentials_returns_false_when_missing(self):
        """check_credentials() returns False when any required var is absent."""
        from utils import archive_data
        keys_to_remove = [
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
            "AWS_BUCKET_NAME", "AWS_REGION",
        ]
        env_without = {k: "" for k in keys_to_remove}
        with unittest.mock.patch.dict(os.environ, env_without):
            for k in keys_to_remove:
                os.environ.pop(k, None)
            result = archive_data.check_credentials()
        self.assertFalse(result)

    def test_check_credentials_returns_true_when_present(self):
        """check_credentials() returns True when all required vars are set."""
        from utils import archive_data
        env_with = {
            "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
            "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "AWS_BUCKET_NAME": "my-test-bucket",
            "AWS_REGION": "us-east-1",
        }
        with unittest.mock.patch.dict(os.environ, env_with):
            result = archive_data.check_credentials()
        self.assertTrue(result)


# ---------------------------------------------------------------------------
# Tests for main() — filesystem + S3 behaviour
# ---------------------------------------------------------------------------

AWS_ENV = {
    "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
    "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "AWS_BUCKET_NAME": "test-bucket",
    "AWS_REGION": "us-east-1",
}


def _build_mock_s3():
    """Return a Mock that behaves like a healthy boto3 S3 client."""
    mock_s3 = unittest.mock.MagicMock()
    mock_s3.upload_file.return_value = None
    mock_s3.head_object.return_value = {"ContentLength": 42}
    return mock_s3


class TestNeverArchiveFilesSkipped(unittest.TestCase):

    def test_never_archive_files_skipped(self):
        """Files in NEVER_ARCHIVE are never uploaded to S3."""
        from utils import archive_data

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            data_dir.mkdir()

            # Write a NEVER_ARCHIVE file — give it an old date so age is not
            # the reason it would be skipped
            old_date = _make_old_date(80)
            # positions.json has no date, but the NEVER_ARCHIVE check fires
            # before the date check, so just write the file as-is
            never_file = data_dir / "positions.json"
            never_file.write_text('{"test": true}')

            mock_s3 = _build_mock_s3()

            with unittest.mock.patch.dict(os.environ, AWS_ENV):
                with unittest.mock.patch("utils.archive_data.DATA_DIR", data_dir):
                    with unittest.mock.patch("boto3.client", return_value=mock_s3):
                        archive_data.main()

            mock_s3.upload_file.assert_not_called()
            # Local file must still exist
            self.assertTrue(never_file.exists())


class TestRecentFileNotArchived(unittest.TestCase):

    def test_recent_file_not_archived(self):
        """A file dated yesterday (< 70 days old) is not uploaded."""
        from utils import archive_data

        recent_str = _make_recent_date(1)
        filename = "finviz_screeners_{}.csv".format(recent_str)

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            data_dir.mkdir()
            (data_dir / filename).write_text("col1,col2\nA,B\n")

            mock_s3 = _build_mock_s3()

            with unittest.mock.patch.dict(os.environ, AWS_ENV):
                with unittest.mock.patch("utils.archive_data.DATA_DIR", data_dir):
                    with unittest.mock.patch("boto3.client", return_value=mock_s3):
                        archive_data.main()

            mock_s3.upload_file.assert_not_called()


class TestOldFileArchived(unittest.TestCase):

    def test_old_file_archived(self):
        """A file dated 80 days ago is uploaded to S3 and the local copy deleted."""
        from utils import archive_data

        old_str = _make_old_date(80)
        filename = "finviz_screeners_{}.csv".format(old_str)
        expected_key = "{}/{}/{}/{}".format(
            old_str[:4], old_str[5:7], old_str[8:10], filename
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            data_dir.mkdir()
            local_file = data_dir / filename
            local_file.write_text("col1,col2\nA,B\n")

            mock_s3 = _build_mock_s3()

            with unittest.mock.patch.dict(os.environ, AWS_ENV):
                with unittest.mock.patch("utils.archive_data.DATA_DIR", data_dir):
                    with unittest.mock.patch("boto3.client", return_value=mock_s3):
                        archive_data.main()

            mock_s3.upload_file.assert_called_once_with(
                str(local_file), "test-bucket", expected_key
            )
            mock_s3.head_object.assert_called_once_with(
                Bucket="test-bucket", Key=expected_key
            )
            # Local file must have been deleted
            self.assertFalse(local_file.exists())


class TestUploadFailureNoLocalDelete(unittest.TestCase):

    def test_upload_failure_no_local_delete(self):
        """If upload_file raises ClientError the local file is NOT deleted."""
        from utils import archive_data
        from botocore.exceptions import ClientError

        old_str = _make_old_date(80)
        filename = "market_monitor_{}.json".format(old_str)

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            data_dir.mkdir()
            local_file = data_dir / filename
            local_file.write_text('{"state": "GREEN"}')

            error_response = {"Error": {"Code": "NoSuchBucket", "Message": "bucket gone"}}
            mock_s3 = _build_mock_s3()
            mock_s3.upload_file.side_effect = ClientError(error_response, "PutObject")

            with unittest.mock.patch.dict(os.environ, AWS_ENV):
                with unittest.mock.patch("utils.archive_data.DATA_DIR", data_dir):
                    with unittest.mock.patch("boto3.client", return_value=mock_s3):
                        # main() exits with code 1 when there are errors
                        with self.assertRaises(SystemExit) as ctx:
                            archive_data.main()
                        self.assertEqual(ctx.exception.code, 1)

            # head_object should NOT have been called (upload failed first)
            mock_s3.head_object.assert_not_called()
            # Local file must still be present
            self.assertTrue(local_file.exists())


class TestVerifyFailureNoLocalDelete(unittest.TestCase):

    def test_verify_failure_no_local_delete(self):
        """If head_object raises ClientError the local file is NOT deleted."""
        from utils import archive_data
        from botocore.exceptions import ClientError

        old_str = _make_old_date(80)
        filename = "daily_quality_{}.json".format(old_str)

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            data_dir.mkdir()
            local_file = data_dir / filename
            local_file.write_text('{"quality": 75}')

            error_response = {"Error": {"Code": "404", "Message": "Not Found"}}
            mock_s3 = _build_mock_s3()
            mock_s3.head_object.side_effect = ClientError(error_response, "HeadObject")

            with unittest.mock.patch.dict(os.environ, AWS_ENV):
                with unittest.mock.patch("utils.archive_data.DATA_DIR", data_dir):
                    with unittest.mock.patch("boto3.client", return_value=mock_s3):
                        with self.assertRaises(SystemExit) as ctx:
                            archive_data.main()
                        self.assertEqual(ctx.exception.code, 1)

            # upload_file was called, but head_object failed → no delete
            mock_s3.upload_file.assert_called_once()
            self.assertTrue(local_file.exists())


class TestNoDateFileSkipped(unittest.TestCase):

    def test_no_date_file_skipped(self):
        """A file whose name contains no YYYY-MM-DD pattern is skipped entirely."""
        from utils import archive_data

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            data_dir.mkdir()
            undated_file = data_dir / "some_report.csv"
            undated_file.write_text("col1,col2\nA,B\n")

            mock_s3 = _build_mock_s3()

            with unittest.mock.patch.dict(os.environ, AWS_ENV):
                with unittest.mock.patch("utils.archive_data.DATA_DIR", data_dir):
                    with unittest.mock.patch("boto3.client", return_value=mock_s3):
                        archive_data.main()

            mock_s3.upload_file.assert_not_called()
            self.assertTrue(undated_file.exists())


if __name__ == "__main__":
    unittest.main()
