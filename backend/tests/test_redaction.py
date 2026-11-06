import json
import logging

from app.core.redaction import RedactingFormatter, data, register_secret


def test_log_sinks_remove_credentials_and_image_bytes(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'a-private-provider-credential')
    register_secret('saved-model-credential')
    record = logging.LogRecord('test', logging.ERROR, 'test', 1,
        'headers Cookie: session-secret api_key=saved-model-credential error=a-private-provider-credential data:image/png;base64,abcd', (), None)
    result = RedactingFormatter().format(record)
    assert not any(secret in result for secret in ['session-secret','saved-model-credential','a-private-provider-credential','base64,abcd'])
    output = json.dumps(data({'Authorization':'Bearer secret','nested':{'api_key':'value'},'error':'saved-model-credential'}))
    assert 'Bearer secret' not in output and 'saved-model-credential' not in output and 'value' not in output
