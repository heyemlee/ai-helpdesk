import json
import logging
import unittest
from ai_sql_agent.core.logging_setup import JsonFormatter, KeyValueFormatter
from ai_sql_agent.core.redaction import register_secret


class RedactionTest(unittest.TestCase):
    def test_provider_errors_and_headers_are_redacted_at_both_sinks(self):
        register_secret('fixture-private-key')
        record = logging.LogRecord('ai_sql.test', logging.ERROR, 'test', 1,
            'provider echoed fixture-private-key Cookie: session-secret', (), None)
        for formatter in [JsonFormatter(), KeyValueFormatter()]:
            result = formatter.format(record)
            self.assertNotIn('fixture-private-key', result)
            self.assertNotIn('session-secret', result)
        self.assertEqual(json.loads(JsonFormatter().format(record))['level'], 'ERROR')
