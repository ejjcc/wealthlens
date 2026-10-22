#!/usr/bin/env python3

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

import _flex  # noqa: E402
import flex_reconcile  # noqa: E402


SEND_OK = """\
<FlexStatementResponse>
  <Status>Success</Status>
  <ReferenceCode>ref-123</ReferenceCode>
  <Url>https://example.invalid/GetStatement</Url>
</FlexStatementResponse>
"""

PENDING_1019 = """\
<FlexStatementResponse>
  <Status>Warn</Status>
  <ErrorCode>1019</ErrorCode>
  <ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage>
</FlexStatementResponse>
"""

NON_RETRYABLE_ERROR = """\
<FlexStatementResponse>
  <Status>Error</Status>
  <ErrorCode>1012</ErrorCode>
  <ErrorMessage>Token has expired.</ErrorMessage>
</FlexStatementResponse>
"""

VALID_STATEMENT = """\
<FlexQueryResponse queryName="test" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="U123">
      <OpenPositions>
        <OpenPosition symbol="TEST" position="2" costBasisPrice="10"
          costBasisMoney="20" markPrice="11" fifoPnlUnrealized="2" />
      </OpenPositions>
      <Trades>
        <Trade symbol="TEST" quantity="2" tradePrice="10" proceeds="-20"
          ibCommission="-1" tradeDate="20260801" />
      </Trades>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""

EMPTY_STATEMENT = """\
<FlexQueryResponse queryName="test" type="AF">
  <FlexStatements count="1"><FlexStatement accountId="U123" /></FlexStatements>
</FlexQueryResponse>
"""


class FlexValidationTests(unittest.TestCase):
    def test_warn_1019_is_never_parsed_as_an_empty_report(self):
        with self.assertRaises(_flex.FlexStatementPending):
            _flex.parse_open_positions(PENDING_1019)
        with self.assertRaises(_flex.FlexStatementPending):
            _flex.parse_trades(PENDING_1019)

    def test_error_empty_and_malformed_payloads_are_rejected(self):
        for payload in (NON_RETRYABLE_ERROR, "", "not xml", "<FlexQueryResponse />"):
            with self.subTest(payload=payload[:30]):
                with self.assertRaises(_flex.FlexResponseError):
                    _flex.validate_flex_statement(payload)

    @mock.patch.object(_flex.time, "sleep")
    @mock.patch.object(_flex, "_http_get")
    def test_fetch_retries_1019_then_returns_completed_statement(self, http_get, sleep):
        http_get.side_effect = [SEND_OK, PENDING_1019, VALID_STATEMENT]

        result = _flex.fetch_flex_xml("token", "qid", max_wait=6)

        self.assertEqual(VALID_STATEMENT, result)
        self.assertEqual(3, http_get.call_count)
        self.assertEqual([mock.call(3), mock.call(3)], sleep.call_args_list)

    @mock.patch.object(_flex.time, "sleep")
    @mock.patch.object(_flex, "_http_get")
    def test_fetch_does_not_retry_non_retryable_flex_error(self, http_get, sleep):
        http_get.side_effect = [SEND_OK, NON_RETRYABLE_ERROR]

        with self.assertRaisesRegex(_flex.FlexResponseError, "1012"):
            _flex.fetch_flex_xml("token", "qid", max_wait=30)

        self.assertEqual(2, http_get.call_count)
        sleep.assert_called_once_with(3)

    @mock.patch.object(_flex.time, "sleep")
    @mock.patch.object(_flex, "_http_get")
    def test_fetch_times_out_after_repeated_1019(self, http_get, sleep):
        http_get.side_effect = [SEND_OK, PENDING_1019, PENDING_1019]

        with self.assertRaisesRegex(RuntimeError, "timed out after 6s.*1019"):
            _flex.fetch_flex_xml("token", "qid", max_wait=6)

        self.assertEqual(3, http_get.call_count)
        self.assertEqual([mock.call(3), mock.call(3)], sleep.call_args_list)


class FlexCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_dir_patch = mock.patch.object(_flex, "CACHE_DIR", Path(self.tmp.name))
        self.cache_dir_patch.start()

    def tearDown(self):
        self.cache_dir_patch.stop()
        self.tmp.cleanup()

    def test_fresh_invalid_cache_is_discarded_and_replaced(self):
        cache_file = _flex._cache_key("token", "qid")
        cache_file.write_text(PENDING_1019)

        with mock.patch.object(_flex, "fetch_flex_xml", return_value=VALID_STATEMENT) as fetch:
            result = _flex.get_flex_xml("token", "qid")

        self.assertEqual(VALID_STATEMENT, result)
        fetch.assert_called_once_with("token", "qid")
        self.assertEqual(VALID_STATEMENT, cache_file.read_text())

    def test_invalid_fresh_response_is_not_cached(self):
        cache_file = _flex._cache_key("token", "qid")
        with mock.patch.object(_flex, "fetch_flex_xml", return_value=NON_RETRYABLE_ERROR):
            with self.assertRaises(_flex.FlexResponseError):
                _flex.get_flex_xml("token", "qid")

        self.assertFalse(cache_file.exists())


class FlexReconcileExitTests(unittest.TestCase):
    def run_main(self, payload, md_path=None):
        argv = ["flex_reconcile.py", "--symbol", "TEST", "--json"]
        if md_path:
            argv += ["--md-path", md_path]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(flex_reconcile, "get_credentials", return_value=("token", "qid")),
            mock.patch.object(flex_reconcile, "get_flex_xml", return_value=payload),
            mock.patch("builtins.print") as output,
        ):
            with self.assertRaises(SystemExit) as exit_info:
                flex_reconcile.main()
        return exit_info.exception.code, output

    def test_error_payload_exits_one_and_reports_not_reconciled(self):
        code, output = self.run_main(PENDING_1019)

        self.assertEqual(1, code)
        result = json.loads(output.call_args.args[0])
        self.assertFalse(result["reconciled"])
        self.assertIn("1019", result["error"])

    def test_valid_but_incomplete_reconciliation_does_not_exit_zero(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as md:
            md.write("---\nqty: 0\navg_cost: 0\n---\n")
            md_path = md.name
        try:
            code, _ = self.run_main(EMPTY_STATEMENT, md_path)
        finally:
            os.unlink(md_path)

        self.assertEqual(2, code)


if __name__ == "__main__":
    unittest.main()
