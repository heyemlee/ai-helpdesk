"""
从页面配置模型连接（`/admin/model`）：存在哪、谁压过谁、不重启怎么生效、谁能改、Key 会不会漏。

这个功能改的是「每个问题连同表结构和数据发给谁」，所以这里测的大多是**不该发生的事**：

- 现有的 Key 被发到一个新的地址；
- 页面换了主链路的 Key，Embedding / Rerank 用的凭证跟着变；
- 没开鉴权的服务被局域网里的人、被打开的恶意网页改掉；
- Key 出现在响应、报错或日志里；
- 「试一下」把正在用的连接记成已被拒绝，或者悄悄保存了。

模型服务一律是假的：进程共用的 HTTP Client 换成 MockTransport，地址用 `.invalid`，
一个字节都出不了本机。
"""
import json
import logging
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from ai_sql_agent.core import CIRCUIT_REGISTRY, MODEL_ACCESS, get_settings, reload_settings
from ai_sql_agent.core import transport as transport_module
from ai_sql_agent.core.model_access import AccessKey
from ai_sql_agent.core.model_settings import (
    ModelSettingsError,
    SettingsBinding,
    bump_connection_version,
    clean_api_key,
    clean_model_name,
    clear_store,
    key_hint,
    load_store,
    origin_of,
    same_address,
    save_store,
    validate_base_url,
)
from ai_sql_agent.core.settings import reload_model_settings
from ai_sql_agent.core.transport import upstream_error_message

ENV_KEY = "sk-env-key-0000000000000000aaaa"
PAGE_KEY = "sk-page-key-1111111111111111bbbb"
OTHER_KEY = "sk-other-key-2222222222222222cccc"
SECRETS = (ENV_KEY, PAGE_KEY, OTHER_KEY)

RELAY = "https://relay.invalid"
OTHER = "https://other.invalid"

LOCAL = {"base_url": "http://localhost:8000", "client": ("127.0.0.1", 50001)}
REMOTE = {"base_url": "http://192.168.1.20:8000", "client": ("192.168.1.77", 50002)}


def chat_reply(_request=None, _index=0):
    return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})


class Service:
    """假的模型服务：记下每一次真的发出去的请求（发往哪台主机、带的哪把 Key），按脚本应答。"""

    def __init__(self, respond=chat_reply):
        self.respond = respond
        self.requests = []
        self._lock = threading.Lock()

    def __call__(self, request):
        with self._lock:
            self.requests.append(request)
            index = len(self.requests)
        return self.respond(request, index)

    @property
    def calls(self):
        return len(self.requests)

    def sent(self):
        """每个请求的（主机，Key，模型名）。"""
        pairs = []
        for request in self.requests:
            key = request.headers.get("x-api-key") or request.headers.get("authorization", "")[7:]
            pairs.append((request.url.host, key, json.loads(request.content).get("model")))
        return pairs


class _Environment(unittest.TestCase):
    """
    一个干净的进程环境：临时目录当工作目录（那里没有 `.env`），模型连接相关的环境变量
    全部显式给定，页面保存的文件指到临时目录里。不依赖 conftest——CI 走的是 unittest。
    """

    EXTRA_ENV = {}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name).resolve()
        self.store_path = self.directory / "state" / "model_settings.json"
        env = {
            "AI_SQL_MODEL_PROVIDER": "anthropic",
            "AI_SQL_ANTHROPIC_API_KEY": "",
            "AI_SQL_ANTHROPIC_BASE_URL": "https://api.anthropic.com",
            "AI_SQL_OPENAI_API_KEY": "",
            "AI_SQL_OPENAI_BASE_URL": "https://api.openai.com/v1",
            "AI_SQL_CHAT_URL": "",
            "AI_SQL_CHAT_MODEL": "",
            "AI_SQL_CODER_MODEL": "",
            "AI_SQL_EMBEDDING_URL": "",
            "AI_SQL_EMBEDDING_API_KEY": "",
            "AI_SQL_RERANK_API_KEY": "",
            "AI_SQL_DASHSCOPE_WORKSPACE_ID": "",
            "AI_SQL_SEMANTIC_DIR": "",
            "AI_SQL_AUTH_MODE": "disabled",
            "AI_SQL_API_KEYS": "",
            "AI_SQL_API_KEYS_FILE": "",
            "AI_SQL_PGVECTOR_DSN": "",
            "AI_SQL_HTTP_MAX_RETRIES": "0",
            "AI_SQL_CERTIFIED_SQL_ENABLED": "false",
            "AI_SQL_MODEL_SETTINGS_ENABLED": "true",
            "AI_SQL_MODEL_SETTINGS_PATH": str(self.store_path),
            "AI_SQL_MEMORY_DB_PATH": str(self.directory / "memory.db"),
            "AI_SQL_AUDIT_DB_PATH": str(self.directory / "audit.db"),
            **self.EXTRA_ENV,
        }
        self._saved = {key: os.environ.get(key) for key in env}
        self._cwd = os.getcwd()
        os.chdir(self.directory)
        os.environ.update(env)
        reload_settings()

        self.service = Service(self.respond)
        self._saved_client = transport_module._shared_client
        transport_module._shared_client = httpx.Client(transport=httpx.MockTransport(self.service))
        MODEL_ACCESS.reset_all()
        CIRCUIT_REGISTRY.reset_all()
        self.addCleanup(self._restore)

    def respond(self, request, index):
        return chat_reply()

    def _restore(self):
        transport_module._shared_client = self._saved_client
        MODEL_ACCESS.reset_all()
        CIRCUIT_REGISTRY.reset_all()
        os.chdir(self._cwd)
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reload_settings()
        bump_connection_version()
        self._tmp.cleanup()

    def setenv(self, **values):
        os.environ.update(values)
        for key in values:
            self._saved.setdefault(key, None)
        return reload_settings()

    def write_store(self, **values):
        save_store(str(self.store_path), values, saved_by="test")
        return reload_settings()

    def app_client(self, **where):
        from ai_sql_agent.api.app import app

        context = TestClient(app, **(where or LOCAL))
        client = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return client

    def assert_no_secret(self, *texts):
        everything = "\n".join(str(text) for text in texts)
        for secret in SECRETS:
            self.assertNotIn(secret, everything)
            self.assertNotIn(secret[:14], everything)


def page_body(**overrides):
    body = {
        "provider": "anthropic",
        "base_url": RELAY,
        "api_key": PAGE_KEY,
        "chat_model": "claude-sonnet-5",
        "coder_model": "claude-opus-5-5",
    }
    body.update(overrides)
    return body


# ----------------------------------------------------------------------
# 地址和 Key 的规则
# ----------------------------------------------------------------------


class AddressRulesTest(unittest.TestCase):
    def test_ordinary_addresses_are_accepted_and_normalised(self):
        for typed, expected in [
            ("https://api.anthropic.com", "https://api.anthropic.com"),
            ("  https://Relay.Example.com/v1/  ", "https://relay.example.com/v1"),
            ("https://relay.example.com:8443/proxy", "https://relay.example.com:8443/proxy"),
            ("http://localhost:11434/v1", "http://localhost:11434/v1"),
            ("http://127.0.0.1:9000", "http://127.0.0.1:9000"),
            ("http://10.20.30.40:8000/v1", "http://10.20.30.40:8000/v1"),
            ("http://[::1]:9000/v1", "http://[::1]:9000/v1"),
        ]:
            with self.subTest(typed=typed):
                self.assertEqual(validate_base_url(typed), (expected, []))

    def test_plain_http_to_a_public_host_is_allowed_with_a_warning(self):
        # 有的中转站只有 http：照收，但要说出来——Key 是明文在路上走的
        self.assertEqual(
            validate_base_url("http://relay.example.com/v1"),
            ("http://relay.example.com/v1", ["plaintext_http"]),
        )
        self.assertEqual(validate_base_url("http://8.8.8.8:8000")[1], ["plaintext_http"])
        for private in ("http://vllm:8000/v1", "http://models.internal/v1", "http://192.168.1.9:8000"):
            self.assertEqual(validate_base_url(private)[1], [], private)

    def test_addresses_the_service_must_not_send_a_key_to_are_refused(self):
        refused = {
            "": "invalid_base_url",
            "relay.example.com": "invalid_base_url",
            "ftp://relay.example.com": "invalid_base_url",
            "file:///etc/passwd": "invalid_base_url",
            "https://": "invalid_base_url",
            "https://user:secret@relay.example.com": "invalid_base_url",
            "https://relay.example.com/v1?key=1": "invalid_base_url",
            "https://relay.example.com/v1#frag": "invalid_base_url",
            "https://relay.example.com:99999": "invalid_base_url",
            "https://relay.example.com/a b": "invalid_base_url",
            "https://" + "a" * 400 + ".example.com": "invalid_base_url",
            # 云主机的元数据服务、链路本地、未指定地址、组播
            "http://169.254.169.254/latest/meta-data": "forbidden_address",
            "http://[fe80::1]:8000": "forbidden_address",
            "http://[::ffff:169.254.169.254]": "forbidden_address",
            "http://0.0.0.0:8000": "forbidden_address",
            "http://224.0.0.1": "forbidden_address",
            "http://metadata.google.internal/computeMetadata": "forbidden_address",
            # 系统解析器当成 IP、`ipaddress` 却不认的写法（169.254.169.254 的十进制 / 十六进制）
            "http://2852039166": "forbidden_address",
            "http://0xa9fea9fe": "forbidden_address",
            "http://169.254.43518": "forbidden_address",
        }
        for typed, code in refused.items():
            with self.subTest(typed=typed):
                with self.assertRaises(ModelSettingsError) as raised:
                    validate_base_url(typed)
                self.assertEqual(raised.exception.code, code)

    def test_a_key_belongs_to_a_host_not_to_a_path(self):
        self.assertTrue(same_address("https://relay.example.com", "https://relay.example.com/v1/messages"))
        self.assertTrue(same_address("https://RELAY.example.com:443/v1", "https://relay.example.com"))
        self.assertTrue(same_address("http://localhost:80", "http://localhost/v1"))
        for left, right in [
            ("https://relay.example.com", "https://relay.example.org"),
            ("https://relay.example.com", "http://relay.example.com"),
            ("https://relay.example.com", "https://relay.example.com:8443"),
            ("https://relay.example.com", "https://evil.relay.example.com"),
            ("https://relay.example.com", ""),
            ("", ""),
        ]:
            self.assertFalse(same_address(left, right), (left, right))
        self.assertEqual(origin_of("https://Relay.Example.com:443/v1/messages"), "https://relay.example.com")

    def test_keys_and_model_names_are_checked_before_they_reach_a_header_or_a_file(self):
        self.assertEqual(clean_api_key("  sk-abc_DEF-123  "), "sk-abc_DEF-123")
        self.assertEqual(clean_api_key(""), "")
        self.assertEqual(clean_api_key(None), "")
        for bad in ("sk-abc def", "sk-abc\r\nX-Evil: 1", "sk-密钥", "k" * 600):
            with self.assertRaises(ModelSettingsError) as raised:
                clean_api_key(bad)
            self.assertEqual(raised.exception.code, "invalid_api_key")
            # 报错里不带提交的 Key
            self.assertNotIn(bad.strip()[:8], raised.exception.message)

        for good in ("claude-sonnet-5", "gpt-5.6-luna", "anthropic/claude-opus-5-5", "qwen2.5:14b", "ft:gpt-5@v2"):
            self.assertEqual(clean_model_name(good, "规划模型"), good)
        for bad in ("claude sonnet", "模型", "-leading", "a" * 200, 'x"y', "a\nb"):
            with self.assertRaises(ModelSettingsError):
                clean_model_name(bad, "规划模型")

    def test_the_hint_shows_four_characters_of_a_long_key_and_nothing_of_a_short_one(self):
        self.assertEqual(key_hint(PAGE_KEY), "bbbb")
        self.assertEqual(key_hint("short-key"), "")
        self.assertEqual(key_hint(""), "")

    def test_only_the_sentence_meant_for_people_is_taken_from_an_upstream_error(self):
        self.assertEqual(
            upstream_error_message('{"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}'),
            "invalid x-api-key",
        )
        self.assertEqual(upstream_error_message('{"error": "API key is  disabled\\n"}'), "API key is disabled")
        self.assertEqual(upstream_error_message('{"message": "no such model"}'), "no such model")
        self.assertEqual(len(upstream_error_message(json.dumps({"error": {"message": "x" * 900}}))), 300)
        for unreadable in ("<html><body>502 Bad Gateway</body></html>", "[1, 2]", '{"error": {"code": 7}}', ""):
            self.assertEqual(upstream_error_message(unreadable), "")


# ----------------------------------------------------------------------
# 存取
# ----------------------------------------------------------------------


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "nested" / "model_settings.json"

    def values(self, **overrides):
        values = {
            "model_provider": "anthropic",
            "anthropic_api_key": PAGE_KEY,
            "anthropic_base_url": RELAY,
            "chat_model": "claude-sonnet-5",
            "coder_model": "claude-opus-5-5",
        }
        values.update(overrides)
        return values

    def test_what_was_saved_is_what_is_loaded(self):
        save_store(str(self.path), self.values(), saved_by="ops")
        loaded = load_store(str(self.path))
        self.assertEqual(loaded["values"], self.values())
        self.assertEqual(loaded["saved_by"], "ops")
        self.assertTrue(loaded["saved_at"])

    def test_the_file_is_readable_by_the_service_account_only(self):
        save_store(str(self.path), self.values())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        # 换一份再存：权限不变，目录里也不留下写到一半的临时文件
        save_store(str(self.path), self.values(chat_model="claude-opus-5-5"))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual([item.name for item in self.path.parent.iterdir()], ["model_settings.json"])

    def test_a_missing_or_unconfigured_file_is_simply_no_override(self):
        self.assertEqual(load_store(str(self.path)), {})
        self.assertEqual(load_store(""), {})

    def test_a_damaged_file_is_reported_not_trusted(self):
        self.path.parent.mkdir(parents=True)
        for content in ("{not json", "[]", '{"values": 3}', '{"values": {"model_provider": "llama"}}'):
            self.path.write_text(content, encoding="utf-8")
            loaded = load_store(str(self.path))
            self.assertNotIn("values", loaded, content)
            self.assertTrue(loaded["problem"], content)

    def test_an_address_without_its_key_is_not_a_usable_override(self):
        # 只有地址没有 Key（或反过来）的文件，会让文件里的地址配上环境变量里的 Key
        self.path.parent.mkdir(parents=True)
        for missing in ("anthropic_api_key", "anthropic_base_url"):
            values = self.values()
            del values[missing]
            self.path.write_text(json.dumps({"values": values}), encoding="utf-8")
            self.assertIn("problem", load_store(str(self.path)), missing)

    def test_fields_of_the_other_provider_and_unknown_fields_are_not_read(self):
        self.path.parent.mkdir(parents=True)
        values = self.values(openai_api_key="sk-smuggled", openai_base_url="https://evil.invalid",
                             embedding_api_key="sk-nope", chat_url="https://evil.invalid/x")
        self.path.write_text(json.dumps({"values": values}), encoding="utf-8")
        self.assertEqual(load_store(str(self.path))["values"], self.values())

    def test_clearing_removes_the_file_and_says_whether_there_was_one(self):
        save_store(str(self.path), self.values())
        self.assertTrue(clear_store(str(self.path)))
        self.assertFalse(self.path.exists())
        self.assertFalse(clear_store(str(self.path)))
        self.assertFalse(clear_store(""))


# ----------------------------------------------------------------------
# 谁压过谁
# ----------------------------------------------------------------------


class PrecedenceTest(_Environment):
    def test_without_a_saved_connection_the_server_configuration_is_used(self):
        settings = self.setenv(AI_SQL_ANTHROPIC_API_KEY=ENV_KEY)
        self.assertEqual(settings.resolved_chat_api_key(), ENV_KEY)
        self.assertEqual(settings.chat_url, "https://api.anthropic.com/v1/messages")
        self.assertIs(settings.server_side, settings)
        self.assertEqual(settings.page_store, {})

    def test_the_saved_connection_replaces_provider_address_key_and_models_together(self):
        self.setenv(AI_SQL_MODEL_PROVIDER="openai", AI_SQL_OPENAI_API_KEY=ENV_KEY,
                    AI_SQL_CHAT_MODEL="gpt-5.6-terra")
        settings = self.write_store(model_provider="anthropic", anthropic_api_key=PAGE_KEY,
                                    anthropic_base_url=RELAY, chat_model="claude-opus-5-5")
        self.assertEqual(settings.model_provider, "anthropic")
        self.assertEqual(settings.chat_url, f"{RELAY}/v1/messages")
        self.assertEqual(settings.resolved_chat_api_key(), PAGE_KEY)
        self.assertEqual(settings.chat_model, "claude-opus-5-5")
        # 没存 SQL 模型：取这家协议的默认值，不沿用服务端给另一家协议配的名字
        self.assertEqual(settings.coder_model, "claude-sonnet-5")
        self.assertEqual(settings.server_side.model_provider, "openai")
        self.assertEqual(settings.server_side.resolved_chat_api_key(), ENV_KEY)

    def test_an_explicit_server_endpoint_does_not_receive_the_page_key(self):
        self.setenv(AI_SQL_MODEL_PROVIDER="openai", AI_SQL_OPENAI_API_KEY=ENV_KEY,
                    AI_SQL_CHAT_URL="http://vllm.invalid:9000/custom/chat")
        self.assertEqual(get_settings().chat_url, "http://vllm.invalid:9000/custom/chat")
        settings = self.write_store(model_provider="openai", openai_api_key=PAGE_KEY,
                                    openai_base_url=f"{RELAY}/v1")
        self.assertEqual(settings.chat_url, f"{RELAY}/v1/chat/completions")

    def test_embedding_and_rerank_keep_the_server_credentials(self):
        self.setenv(AI_SQL_MODEL_PROVIDER="openai", AI_SQL_OPENAI_API_KEY=ENV_KEY,
                    AI_SQL_OPENAI_BASE_URL="https://bailian.invalid/v1")
        before = get_settings()
        self.assertEqual(before.embedding_url, "https://bailian.invalid/v1/embeddings")

        settings = self.write_store(model_provider="openai", openai_api_key=PAGE_KEY,
                                    openai_base_url=f"{RELAY}/v1")
        self.assertEqual(settings.resolved_chat_api_key(), PAGE_KEY)
        self.assertEqual(settings.embedding_url, "https://bailian.invalid/v1/embeddings")
        self.assertEqual(settings.resolved_embedding_api_key(), ENV_KEY)
        self.assertEqual(settings.resolved_rerank_api_key(), ENV_KEY)

    def test_embedding_does_not_pick_up_the_page_key_when_the_server_had_none(self):
        # 服务端没配 OpenAI 兼容凭证：页面的 Key 不该「回落」给默认的 Embedding 地址
        settings = self.write_store(model_provider="openai", openai_api_key=PAGE_KEY,
                                    openai_base_url=f"{RELAY}/v1")
        self.assertEqual(settings.resolved_embedding_api_key(), "")
        self.assertEqual(settings.resolved_rerank_api_key(), "")

    def test_switching_the_feature_off_ignores_what_was_saved(self):
        self.write_store(model_provider="anthropic", anthropic_api_key=PAGE_KEY, anthropic_base_url=RELAY)
        for off in ({"AI_SQL_MODEL_SETTINGS_ENABLED": "false"}, {"AI_SQL_MODEL_SETTINGS_PATH": ""}):
            settings = self.setenv(**off)
            self.assertEqual(settings.model_settings_file, "", off)
            self.assertEqual(settings.resolved_chat_api_key(), "", off)
            self.assertEqual(settings.chat_url, "https://api.anthropic.com/v1/messages", off)
            self.setenv(AI_SQL_MODEL_SETTINGS_ENABLED="true", AI_SQL_MODEL_SETTINGS_PATH=str(self.store_path))

    def test_an_unusable_file_leaves_the_server_configuration_and_says_why(self):
        self.store_path.parent.mkdir(parents=True)
        self.store_path.write_text("{broken", encoding="utf-8")
        settings = self.setenv(AI_SQL_ANTHROPIC_API_KEY=ENV_KEY)
        self.assertEqual(settings.resolved_chat_api_key(), ENV_KEY)
        self.assertTrue(settings.page_store["problem"])

    def test_applying_a_saved_connection_does_not_reread_the_environment(self):
        # 服务起来之后有人改了环境里别的配置：保存模型连接不该把它们带进来一半
        self.assertEqual(get_settings().max_rows, 1000)
        os.environ["AI_SQL_MAX_ROWS"] = "7"
        self._saved.setdefault("AI_SQL_MAX_ROWS", None)
        save_store(str(self.store_path), {"model_provider": "anthropic", "anthropic_api_key": PAGE_KEY,
                                          "anthropic_base_url": RELAY})
        settings = reload_model_settings()
        self.assertEqual(settings.resolved_chat_api_key(), PAGE_KEY)
        self.assertEqual(settings.max_rows, 1000)

        clear_store(str(self.store_path))
        settings = reload_model_settings()
        self.assertEqual(settings.resolved_chat_api_key(), "")
        self.assertEqual(settings.page_store, {})
        self.assertEqual(settings.max_rows, 1000)


# ----------------------------------------------------------------------
# 已经建好的模型 Client 跟不跟得上
# ----------------------------------------------------------------------


class ClientsFollowTest(_Environment):
    def switch_to(self, base_url, key, **models):
        save_store(str(self.store_path), {"model_provider": "anthropic", "anthropic_api_key": key,
                                          "anthropic_base_url": base_url, **models})
        reload_model_settings()
        bump_connection_version()

    def test_answers_from_mock_follows_the_saved_connection(self):
        # 语义自检靠它判断「这次是 Mock 在答」：启动时没 Key、之后在页面保存了，
        # 下一次自检就得真做，不能一直停在「不适用」。
        from ai_sql_agent.planning import ThinkingModelClient, ThinkingModelConfig

        thinking = ThinkingModelClient()
        strict = ThinkingModelClient(ThinkingModelConfig(use_mock_when_no_api_key=False))
        self.assertTrue(thinking.answers_from_mock())
        self.assertFalse(strict.answers_from_mock(), "不允许走 Mock 时没有 Key 就是报错，不是 Mock")

        self.switch_to(RELAY, PAGE_KEY)
        self.assertFalse(thinking.answers_from_mock())
        self.assertEqual(self.service.calls, 0, "问一句「是不是 Mock」不该调用模型")

    def test_a_client_built_without_a_key_starts_calling_the_model_once_one_is_saved(self):
        from ai_sql_agent.generation.coder_client import CoderModelClient
        from ai_sql_agent.planning import ThinkingModelClient

        thinking, coder = ThinkingModelClient(), CoderModelClient()
        thinking.generate("# 用户Query\n你好\n")
        coder.generate_sql("on_time_rate")
        self.assertEqual(self.service.calls, 0, "没有 Key 时走的是 Mock")

        self.switch_to(RELAY, PAGE_KEY, chat_model="claude-opus-5-5", coder_model="claude-haiku-5-5")
        self.assertEqual(thinking.generate("ping"), "ok")
        self.assertEqual(coder.generate_sql("ping"), "ok")
        self.assertEqual(self.service.sent(), [
            ("relay.invalid", PAGE_KEY, "claude-opus-5-5"),
            ("relay.invalid", PAGE_KEY, "claude-haiku-5-5"),
        ])
        self.assertEqual(self.service.requests[0].url.path, "/v1/messages")

    def test_removing_the_saved_connection_takes_the_key_away_again(self):
        from ai_sql_agent.planning import ThinkingModelClient, ThinkingModelConfig
        from ai_sql_agent.retrieval.keyword_extractor_client import AliyunKeywordExtractor

        self.switch_to(RELAY, PAGE_KEY)
        strict = ThinkingModelClient(ThinkingModelConfig(use_mock_when_no_api_key=False))
        extractor = AliyunKeywordExtractor()
        self.assertEqual(strict.generate("ping"), "ok")
        self.assertEqual(self.service.calls, 1)

        clear_store(str(self.store_path))
        reload_model_settings()
        bump_connection_version()
        with self.assertRaises(ValueError):
            strict.generate("ping")
        with self.assertRaises(ValueError):
            extractor.extract("准时率低于0.9的供应商")
        self.assertEqual(self.service.calls, 1, "Key 撤掉之后不拿空 Key 去撞")

    def test_values_given_to_the_constructor_are_never_replaced(self):
        from ai_sql_agent.planning import ThinkingModelClient, ThinkingModelConfig

        pinned = ThinkingModelClient(ThinkingModelConfig(
            api_key=OTHER_KEY, base_url=f"{OTHER}/v1/messages", model="pinned-model", provider="anthropic"))
        self.switch_to(RELAY, PAGE_KEY)
        pinned.generate("ping")
        self.assertEqual(self.service.sent(), [("other.invalid", OTHER_KEY, "pinned-model")])

    def test_only_the_values_left_blank_follow_the_configuration(self):
        from ai_sql_agent.planning import ThinkingModelClient, ThinkingModelConfig

        self.switch_to(RELAY, PAGE_KEY)
        client = ThinkingModelClient(ThinkingModelConfig(model="pinned-model"))
        self.switch_to(OTHER, OTHER_KEY, chat_model="claude-opus-5-5")
        client.generate("ping")
        self.assertEqual(self.service.sent(), [("other.invalid", OTHER_KEY, "pinned-model")])

    def test_an_old_key_is_never_sent_to_a_new_address(self):
        """
        一边不停地调模型，一边在两份连接之间来回切。发出去的每一个请求，
        主机和 Key 都必须属于同一份连接——不能有「新地址 + 旧 Key」。
        """
        from ai_sql_agent.generation.coder_client import CoderModelClient
        from ai_sql_agent.planning import ThinkingModelClient

        self.switch_to(RELAY, PAGE_KEY)
        clients = [ThinkingModelClient(), CoderModelClient()]
        stop = threading.Event()
        errors = []

        def hammer(client):
            call = getattr(client, "generate", None) or client.generate_sql
            while not stop.is_set():
                try:
                    call("ping")
                except Exception as exc:  # pragma: no cover - 失败时留给断言说明
                    errors.append(exc)
                    return

        threads = [threading.Thread(target=hammer, args=(client,)) for client in clients for _ in range(2)]
        for thread in threads:
            thread.start()
        for turn in range(40):
            if turn % 2:
                self.switch_to(RELAY, PAGE_KEY)
            else:
                self.switch_to(OTHER, OTHER_KEY)
        stop.set()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        pairs = {(host, key) for host, key, _model in self.service.sent()}
        self.assertGreater(self.service.calls, 40)
        self.assertLessEqual(pairs, {("relay.invalid", PAGE_KEY), ("other.invalid", OTHER_KEY)})
        self.assertEqual(len(pairs), 2, "两份连接都真的被用到过")

    def test_a_binding_only_tracks_what_was_blank_when_it_was_made(self):
        class Config:
            api_key = ""
            model = "kept"

        config = Config()
        binding = SettingsBinding(config, {"api_key": lambda s: s["key"], "model": lambda s: s["model"]})
        self.assertTrue(binding.refresh(lambda: {"key": "first", "model": "ignored"}))
        self.assertEqual((config.api_key, config.model), ("first", "kept"))
        self.assertFalse(binding.refresh(lambda: {"key": "unseen", "model": "ignored"}), "连接没换过就不重取")
        bump_connection_version()
        self.assertTrue(binding.refresh(lambda: {"key": "second", "model": "ignored"}))
        self.assertEqual((config.api_key, config.model), ("second", "kept"))


# ----------------------------------------------------------------------
# 接口：谁能改
# ----------------------------------------------------------------------


class LocalOnlyWithoutAuthTest(_Environment):
    """没开鉴权的实例：只有本机直接发来的请求能看全、能改。"""

    def test_a_request_from_this_machine_may_read_and_change_it(self):
        client = self.app_client()
        status = client.get("/admin/model")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.headers["cache-control"], "no-store")
        self.assertEqual((status.json()["editable"], status.json()["reason"]), (True, ""))

        saved = client.put("/admin/model", json=page_body(), headers={"Origin": "http://localhost:8000"})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(get_settings().resolved_chat_api_key(), PAGE_KEY)

    def test_the_other_loopback_spellings_count_as_this_machine(self):
        # （测试客户端自己解析不了带方括号的地址，所以 Host 用请求头给）
        for peer, host in (
            ("127.0.0.1", "127.0.0.1:8000"),
            ("::1", "[::1]:8000"),
            ("::ffff:127.0.0.1", "console.localhost:8000"),
            ("127.0.0.1", "LOCALHOST"),
        ):
            with self.subTest(peer=peer, host=host):
                client = self.app_client(base_url="http://localhost:8000", client=(peer, 50001))
                status = client.get("/admin/model", headers={"Host": host}).json()
                self.assertTrue(status["editable"])
                origin = "http://" + host.lower()
                saved = client.put("/admin/model", json=page_body(), headers={"Host": host, "Origin": origin})
                self.assertEqual(saved.status_code, 200, saved.text)

    def test_someone_else_on_the_network_sees_a_reduced_status_and_cannot_change_it(self):
        self.write_store(model_provider="anthropic", anthropic_api_key=PAGE_KEY, anthropic_base_url=RELAY)
        client = self.app_client(**REMOTE)

        status = client.get("/admin/model")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json(), {
            "editable": False, "reason": "local_only", "source": "page", "provider": "anthropic",
            "chat_model": "claude-sonnet-5", "coder_model": "claude-sonnet-5", "key_configured": True,
            "embedding": {
                "source": "server", "backend": "local", "model": "local_hash", "dimensions": 1024,
                "key_configured": False, "fallback": "",
                "active": {"backend": "local", "model": "local_hash", "dimensions": 1024},
            },
        })
        # 接口地址、Key 的末四位、谁保存的，都不给
        self.assertNotIn("relay.invalid", status.text)
        self.assertNotIn(PAGE_KEY[-4:], status.text)

        for response in (
            client.put("/admin/model", json=page_body(api_key=OTHER_KEY, base_url=OTHER)),
            client.post("/admin/model/test", json=page_body()),
            client.delete("/admin/model"),
        ):
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["detail"]["code"], "local_only")
        self.assertEqual(get_settings().resolved_chat_api_key(), PAGE_KEY, "什么都没改")
        self.assertEqual(self.service.calls, 0, "也没有替他去调模型")

    def test_requests_that_only_look_local_are_refused(self):
        client = self.app_client()
        disguises = {
            # DNS 重绑定：连接来自本机的浏览器，但页面是别人的域名
            "rebound host": {"Host": "attacker.example:8000"},
            # 被打开的恶意网页往 localhost 发请求
            "cross-site page": {"Origin": "https://attacker.example"},
            "another local port": {"Origin": "http://localhost:3000"},
            "sandboxed frame": {"Origin": "null"},
            "https page to http service": {"Origin": "https://localhost:8000"},
            "browser says cross-site": {"Sec-Fetch-Site": "cross-site"},
            "browser says same-site": {"Sec-Fetch-Site": "same-site"},
            # 同一台机器上的反向代理 / 隧道
            "x-forwarded-for": {"X-Forwarded-For": "203.0.113.9"},
            "spoofed x-forwarded-for": {"X-Forwarded-For": "127.0.0.1"},
            "x-forwarded-host": {"X-Forwarded-Host": "sql.example.com"},
            "x-real-ip": {"X-Real-IP": "203.0.113.9"},
            "forwarded": {"Forwarded": "for=203.0.113.9"},
            "via": {"Via": "1.1 proxy"},
        }
        for name, headers in disguises.items():
            with self.subTest(name=name):
                refused = client.put("/admin/model", json=page_body(), headers=headers)
                self.assertEqual(refused.status_code, 403, name)
                self.assertEqual(refused.json()["detail"]["code"], "local_only")
                self.assertFalse(client.get("/admin/model", headers=headers).json()["editable"])
        self.assertFalse(self.store_path.exists())

    def test_a_refused_attempt_is_logged_with_the_check_that_failed(self):
        client = self.app_client()
        with self.assertLogs("ai_sql.api.model_admin", level=logging.WARNING) as captured:
            client.put("/admin/model", json=page_body(), headers={"Origin": "https://attacker.example"})
        (record,) = captured.records
        self.assertEqual(record.getMessage(), "model_settings.denied")
        self.assertEqual(record.ai_sql_fields["check"], "cross_origin")
        self.assert_no_secret(json.dumps(record.ai_sql_fields, ensure_ascii=False))

    def test_only_json_bodies_are_read(self):
        # 表单能发的类型不触发浏览器的预检；这里一律不收
        client = self.app_client()
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
            response = client.put("/admin/model", content=json.dumps(page_body()),
                                  headers={"Content-Type": content_type})
            self.assertEqual(response.status_code, 415, content_type)
            self.assert_no_secret(response.text)
        self.assertFalse(self.store_path.exists())

    def test_with_the_feature_off_the_status_is_readable_but_nothing_can_be_changed(self):
        self.setenv(AI_SQL_MODEL_SETTINGS_ENABLED="false", AI_SQL_ANTHROPIC_API_KEY=ENV_KEY)
        client = self.app_client()
        status = client.get("/admin/model").json()
        self.assertEqual((status["editable"], status["reason"], status["key_configured"]),
                         (False, "disabled", True))
        for response in (client.put("/admin/model", json=page_body()),
                         client.post("/admin/model/test", json=page_body()),
                         client.delete("/admin/model")):
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["detail"]["code"], "disabled")
        self.assertEqual(self.service.calls, 0)


class AdminKeyWithAuthTest(_Environment):
    """开了鉴权的实例：管理员 Key，和其他运维接口一样；不看请求从哪儿来。"""

    EXTRA_ENV = {"AI_SQL_AUTH_MODE": "api_key"}

    def setUp(self):
        super().setUp()
        keys = self.directory / "keys.json"
        keys.write_text(json.dumps({
            "keys": [
                {"key": "k-admin", "caller": "ops", "user_id": "ops", "is_admin": True},
                {"key": "k-user", "caller": "buyer", "user_id": "buyer", "roles": ["everything"]},
            ],
            "policies": {"everything": {"allow_tables": ["*.*"]}},
        }), encoding="utf-8")
        self.setenv(AI_SQL_API_KEYS_FILE=str(keys))

    def test_only_an_admin_key_can_see_or_change_the_connection(self):
        client = self.app_client(**REMOTE)
        calls = [
            lambda headers: client.get("/admin/model", headers=headers),
            lambda headers: client.post("/admin/model/test", json=page_body(), headers=headers),
            lambda headers: client.put("/admin/model", json=page_body(), headers=headers),
            lambda headers: client.delete("/admin/model", headers=headers),
        ]
        for call in calls:
            self.assertEqual(call({}).status_code, 401)
            self.assertEqual(call({"X-API-Key": "k-user"}).status_code, 403)
        self.assertFalse(self.store_path.exists())
        self.assertEqual(self.service.calls, 0)

        admin = {"X-API-Key": "k-admin"}
        self.assertTrue(client.get("/admin/model", headers=admin).json()["editable"])
        saved = client.put("/admin/model", json=page_body(), headers=admin)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["status"]["saved_by"], "ops")
        self.assertEqual(get_settings().resolved_chat_api_key(), PAGE_KEY)

    def test_a_rejected_body_from_a_stranger_is_never_parsed(self):
        # 先过身份这一关，再读请求体：没带 Key 的人得到的是 401，不是「你的 JSON 哪里不对」
        client = self.app_client(**REMOTE)
        response = client.put("/admin/model", content="{not json", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 401)


# ----------------------------------------------------------------------
# 接口：保存、生效、恢复
# ----------------------------------------------------------------------


class SaveAndApplyTest(_Environment):
    def test_saving_takes_effect_for_the_next_question_without_a_restart(self):
        client = self.app_client()
        app = client.app
        service_before = app.state.service
        self.assertIsNone(service_before.router.model_classifier, "启动时没有 Key：路由用的是规则")
        self.assertEqual(type(app.state.memory.summarizer).__name__, "ExtractiveMemorySummarizer")

        saved = client.put("/admin/model", json=page_body())
        self.assertEqual(saved.status_code, 200, saved.text)
        status = saved.json()["status"]
        self.assertEqual(
            (status["source"], status["provider"], status["endpoint"], status["key_hint"], status["chat_model"],
             status["coder_model"]),
            ("page", "anthropic", f"{RELAY}/v1/messages", "bbbb", "claude-sonnet-5", "claude-opus-5-5"),
        )

        # 启动时按「没有凭证」选了规则版的那几处，现在都换成了模型版
        self.assertIsNot(app.state.service, service_before)
        self.assertIsNotNone(app.state.service.router.model_classifier)
        self.assertEqual(type(app.state.memory.summarizer).__name__, "ModelMemorySummarizer")
        self.assertIs(app.state.memory.short_term.summarizer, app.state.memory.summarizer)
        self.assertIs(app.state.memory.long_term.summarizer, app.state.memory.summarizer)

        answered = client.post("/query", json={"user_id": "u", "session_id": "s",
                                               "query": "Which suppliers have an on-time delivery rate below 0.9?"})
        self.assertEqual(answered.status_code, 200, answered.text)
        self.assertGreater(self.service.calls, 0, "问题真的去调了模型")
        self.assertEqual({(host, key) for host, key, _ in self.service.sent()}, {("relay.invalid", PAGE_KEY)})
        self.assertIn("saved from the console", client.get("/readyz").json()["checks"]["model_settings"])

    def test_restoring_goes_back_to_the_server_configuration_at_once(self):
        self.setenv(AI_SQL_ANTHROPIC_API_KEY=ENV_KEY, AI_SQL_ANTHROPIC_BASE_URL=OTHER)
        client = self.app_client()
        client.put("/admin/model", json=page_body())
        self.assertEqual(get_settings().chat_url, f"{RELAY}/v1/messages")

        restored = client.delete("/admin/model")
        self.assertEqual(restored.status_code, 200)
        self.assertEqual((restored.json()["cleared"], restored.json()["status"]["source"]), (True, "server"))
        self.assertFalse(self.store_path.exists())
        settings = get_settings()
        self.assertEqual((settings.chat_url, settings.resolved_chat_api_key()), (f"{OTHER}/v1/messages", ENV_KEY))
        self.assertNotIn("model_settings", client.get("/readyz").json()["checks"])
        # 本来就没有可丢的：照样成功，只是如实说
        self.assertFalse(client.delete("/admin/model").json()["cleared"])

    def test_the_saved_connection_survives_a_restart(self):
        self.app_client().put("/admin/model", json=page_body())
        settings = reload_settings()        # 进程重启后读到的就是这一份
        self.assertEqual((settings.chat_url, settings.resolved_chat_api_key(), settings.coder_model),
                         (f"{RELAY}/v1/messages", PAGE_KEY, "claude-opus-5-5"))
        self.assertEqual(stat.S_IMODE(self.store_path.stat().st_mode), 0o600)

    def test_the_current_key_is_kept_only_while_the_address_stays_on_the_same_host(self):
        client = self.app_client()
        client.put("/admin/model", json=page_body())

        kept = client.put("/admin/model", json=page_body(api_key="", base_url=f"{RELAY}/v1",
                                                         chat_model="claude-opus-5-5"))
        self.assertEqual(kept.status_code, 200, kept.text)
        settings = get_settings()
        self.assertEqual((settings.resolved_chat_api_key(), settings.chat_model), (PAGE_KEY, "claude-opus-5-5"))

        for moved in (OTHER, "http://relay.invalid", f"{RELAY}:8443", "https://evil.relay.invalid"):
            refused = client.put("/admin/model", json=page_body(api_key="", base_url=moved))
            self.assertEqual(refused.status_code, 400, moved)
            self.assertEqual(refused.json()["detail"]["code"], "key_required")
        self.assertEqual(get_settings().chat_url, f"{RELAY}/v1/messages")

        other_protocol = client.put("/admin/model", json=page_body(provider="openai", api_key="", base_url=RELAY))
        self.assertEqual(other_protocol.json()["detail"]["code"], "key_required", "这家协议还没有 Key")

    def test_a_server_key_is_tied_to_the_endpoint_it_is_really_sent_to(self):
        # 服务端把请求指到了一台内网主机上（AI_SQL_CHAT_URL），BASE_URL 留的是官方地址：
        # 那把 Key 从没发给过官方地址，页面也不能不填 Key 就把它改发过去
        self.setenv(AI_SQL_MODEL_PROVIDER="openai", AI_SQL_OPENAI_API_KEY=ENV_KEY,
                    AI_SQL_CHAT_URL="http://vllm.invalid:9000/custom/chat")
        client = self.app_client()
        status = client.get("/admin/model").json()
        self.assertTrue(status["custom_endpoint"])
        self.assertEqual(status["providers"]["openai"]["key_address"], "http://vllm.invalid:9000")

        refused = client.put("/admin/model", json={"provider": "openai", "base_url": "https://api.openai.com/v1"})
        self.assertEqual(refused.json()["detail"]["code"], "key_required")
        same_host = client.put("/admin/model", json={"provider": "openai", "base_url": "http://vllm.invalid:9000/v1"})
        self.assertEqual(same_host.status_code, 200, same_host.text)
        self.assertEqual(get_settings().resolved_chat_api_key(), ENV_KEY)

    def test_blank_fields_fall_back_to_the_official_address_and_default_models(self):
        client = self.app_client()
        saved = client.put("/admin/model", json={"provider": "openai", "api_key": PAGE_KEY})
        self.assertEqual(saved.status_code, 200, saved.text)
        settings = get_settings()
        self.assertEqual((settings.chat_url, settings.chat_model, settings.coder_model),
                         ("https://api.openai.com/v1/chat/completions", "gpt-5.6-luna", "gpt-5.6-luna"))

    def test_what_cannot_be_saved_is_refused_without_changing_anything(self):
        client = self.app_client()
        refused = {
            "invalid_provider": page_body(provider="llama"),
            "invalid_base_url": page_body(base_url="relay.invalid"),
            "forbidden_address": page_body(base_url="http://169.254.169.254/latest"),
            "invalid_api_key": page_body(api_key=PAGE_KEY + " trailing words"),
            "invalid_model": page_body(chat_model="claude sonnet"),
            "key_required": page_body(api_key=""),
            "invalid_request": page_body(sql="DROP TABLE x"),
        }
        for code, body in refused.items():
            with self.subTest(code=code):
                response = client.put("/admin/model", json=body)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["detail"]["code"], code)
                self.assert_no_secret(response.text)
        self.assertFalse(self.store_path.exists())
        self.assertEqual(get_settings().resolved_chat_api_key(), "")

    def test_a_malformed_body_is_refused_without_echoing_it(self):
        # FastAPI 的 422 会把提交的值原样带回来；这组接口不走那条路
        client = self.app_client()
        json_header = {"Content-Type": "application/json"}
        for body in (
            json.dumps({"provider": "anthropic", "api_key": [PAGE_KEY]}),
            json.dumps({"provider": "anthropic", "api_key": {"value": PAGE_KEY}}),
            json.dumps({"provider": 7, "api_key": PAGE_KEY}),
            json.dumps([PAGE_KEY]),
            json.dumps(PAGE_KEY),
            '{"api_key": "' + PAGE_KEY,
        ):
            response = client.put("/admin/model", content=body, headers=json_header)
            self.assertEqual(response.status_code, 400, body)
            self.assert_no_secret(response.text)
        oversized = client.put("/admin/model", content=json.dumps(page_body(chat_model="m" * 9000)),
                               headers=json_header)
        self.assertEqual(oversized.status_code, 413)
        self.assert_no_secret(oversized.text)
        self.assertFalse(self.store_path.exists())

    def test_the_key_is_never_returned_or_logged(self):
        client = self.app_client()
        with self.assertLogs("ai_sql", level=logging.DEBUG) as captured:
            responses = [
                client.put("/admin/model", json=page_body()),
                client.get("/admin/model"),
                client.post("/admin/model/test", json=page_body(api_key="")),
                client.get("/readyz"),
                client.put("/admin/model", json=page_body(api_key=OTHER_KEY, base_url=OTHER)),
                client.delete("/admin/model"),
            ]
        for response in responses:
            self.assertEqual(response.status_code, 200, response.text)
        logged = [record.getMessage() + json.dumps(getattr(record, "ai_sql_fields", {}), ensure_ascii=False,
                                                    default=str) for record in captured.records]
        self.assert_no_secret(*(response.text for response in responses), *logged)

        saved = [record.ai_sql_fields for record in captured.records
                 if record.getMessage() == "model_settings.saved"]
        self.assertEqual([(item["endpoint"], item["key_changed"]) for item in saved],
                         [(RELAY, True), (OTHER, True)])
        self.assertEqual(saved[1]["previous_endpoint"], RELAY)

    def test_saving_clears_what_was_recorded_against_the_old_credential(self):
        self.setenv(AI_SQL_ANTHROPIC_API_KEY=ENV_KEY)
        client = self.app_client()
        key = AccessKey.build("https://api.anthropic.com/v1/messages", "anthropic", ENV_KEY)
        MODEL_ACCESS.block(key, scope="credential", reason="credential_rejected", status_code=401)
        status = client.get("/admin/model").json()
        self.assertEqual([fault["reason"] for fault in status["access_faults"]], ["credential_rejected"])

        client.put("/admin/model", json=page_body())
        self.assertEqual(MODEL_ACCESS.snapshot(), [])
        self.assertEqual(client.get("/admin/model").json()["access_faults"], [])

    def test_a_file_that_cannot_be_written_changes_nothing(self):
        blocker = self.directory / "not-a-directory"
        blocker.write_text("", encoding="utf-8")
        self.setenv(AI_SQL_MODEL_SETTINGS_PATH=str(blocker / "model_settings.json"))
        client = self.app_client()
        response = client.put("/admin/model", json=page_body())
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"]["code"], "store_unwritable")
        self.assert_no_secret(response.text)
        self.assertEqual(get_settings().resolved_chat_api_key(), "")

    def test_a_damaged_file_is_reported_and_restore_clears_it(self):
        self.store_path.parent.mkdir(parents=True)
        self.store_path.write_text("{broken", encoding="utf-8")
        reload_settings()
        client = self.app_client()
        status = client.get("/admin/model").json()
        self.assertEqual(status["source"], "server")
        self.assertTrue(status["store_problem"])
        self.assertIn("unusable", client.get("/readyz").json()["checks"]["model_settings"])

        self.assertTrue(client.delete("/admin/model").json()["cleared"])
        self.assertEqual(client.get("/admin/model").json()["store_problem"], "")


# ----------------------------------------------------------------------
# 接口：试一下
# ----------------------------------------------------------------------


class ConnectionTestTest(_Environment):
    def post(self, client, **overrides):
        response = client.post("/admin/model/test", json=page_body(**overrides))
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_no_secret(response.text)
        return response.json()

    def test_each_model_name_gets_one_short_request_and_nothing_is_saved(self):
        client = self.app_client()
        report = self.post(client)
        self.assertTrue(report["ok"])
        self.assertEqual(report["endpoint"], f"{RELAY}/v1/messages")
        self.assertEqual([(item["roles"], item["model"], item["outcome"]) for item in report["results"]],
                         [(["chat"], "claude-sonnet-5", "ok"), (["coder"], "claude-opus-5-5", "ok")])
        self.assertEqual(self.service.sent(), [("relay.invalid", PAGE_KEY, "claude-sonnet-5"),
                                               ("relay.invalid", PAGE_KEY, "claude-opus-5-5")])
        body = json.loads(self.service.requests[0].content)
        self.assertLessEqual(body["max_tokens"], 64)
        self.assertNotIn("temperature", body, "Claude 5 起带这个字段会被 400：和正式调用用的是同一个请求体")

        self.assertFalse(self.store_path.exists(), "试一下不等于保存")
        self.assertEqual(get_settings().resolved_chat_api_key(), "")

    def test_the_same_name_for_both_roles_is_tested_once(self):
        report = self.post(self.app_client(), coder_model="claude-sonnet-5")
        self.assertEqual([item["roles"] for item in report["results"]], [["chat", "coder"]])
        self.assertEqual(self.service.calls, 1)

    def test_openai_compatible_connections_are_tested_over_their_own_protocol(self):
        self.service.respond = lambda request, index: httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]})
        report = self.post(self.app_client(), provider="openai", base_url=f"{RELAY}/v1",
                           chat_model="gpt-5.6-luna", coder_model="gpt-5.6-luna")
        self.assertTrue(report["ok"])
        request = self.service.requests[0]
        self.assertEqual((request.url.path, request.headers["authorization"]),
                         ("/v1/chat/completions", f"Bearer {PAGE_KEY}"))

    def test_a_rejected_key_is_reported_in_the_providers_own_words_and_stops_there(self):
        self.service.respond = lambda request, index: httpx.Response(
            401, json={"error": {"type": "authentication_error", "message": f"API key {PAGE_KEY} is disabled"}})
        client = self.app_client()
        report = self.post(client)
        self.assertFalse(report["ok"])
        (result,) = report["results"]
        self.assertEqual((result["outcome"], result["status_code"]), ("auth", 401))
        self.assertEqual(result["message"], "API key <redacted> is disabled")
        self.assertEqual(self.service.calls, 1, "Key 不行，第二个模型不用再试")

    def test_a_failed_test_does_not_mark_the_connection_in_use_as_rejected(self):
        self.setenv(AI_SQL_ANTHROPIC_API_KEY=ENV_KEY, AI_SQL_ANTHROPIC_BASE_URL=RELAY)
        self.service.respond = lambda request, index: httpx.Response(401, json={"error": {"message": "nope"}})
        client = self.app_client()
        # 试的就是正在用的这一份（Key 留空 = 现有的那把）
        self.assertFalse(self.post(client, api_key="")["ok"])
        self.assertEqual(MODEL_ACCESS.snapshot(), [], "记不记成「已被拒绝」由正式调用决定")
        self.assertEqual(self.service.sent()[0][:2], ("relay.invalid", ENV_KEY))

    def test_a_key_already_marked_as_rejected_can_still_be_tested(self):
        # 中转站那边把 Key 重新启用了：试一下应该真的发出去，而不是被本地的旧记录挡住
        self.setenv(AI_SQL_ANTHROPIC_API_KEY=ENV_KEY, AI_SQL_ANTHROPIC_BASE_URL=RELAY)
        MODEL_ACCESS.block(AccessKey.build(f"{RELAY}/v1/messages", "anthropic", ENV_KEY),
                           scope="credential", reason="credential_rejected", status_code=401)
        report = self.post(self.app_client(), api_key="", coder_model="claude-sonnet-5")
        self.assertTrue(report["ok"])
        self.assertEqual(self.service.calls, 1)
        self.assertEqual(len(MODEL_ACCESS.snapshot()), 1, "也不替正式调用把那条记录清掉")

    def test_a_wrong_model_name_is_told_apart_from_a_wrong_key(self):
        def respond(request, index):
            if json.loads(request.content)["model"] == "claude-opus-5-5":
                return httpx.Response(404, json={"error": {"type": "not_found_error", "message": "model: claude-opus-5-5"}})
            return chat_reply()

        self.service.respond = respond
        report = self.post(self.app_client())
        self.assertFalse(report["ok"])
        self.assertEqual([(item["model"], item["outcome"]) for item in report["results"]],
                         [("claude-sonnet-5", "ok"), ("claude-opus-5-5", "not_found")])
        self.assertEqual(report["results"][1]["message"], "model: claude-opus-5-5")

    def test_each_kind_of_failure_gets_its_own_outcome(self):
        def refuse(request):
            raise httpx.ConnectError("no route to host", request=request)

        def stall(request):
            raise httpx.ReadTimeout("timed out", request=request)

        cases = {
            "quota": lambda request, index: httpx.Response(402, json={"error": {"message": "balance is 0"}}),
            "model_denied": lambda request, index: httpx.Response(
                403, json={"error": {"type": "permission_error", "message": "model not allowed for this key"}}),
            "forbidden": lambda request, index: httpx.Response(403, text="<html>blocked</html>"),
            "rate_limited": lambda request, index: httpx.Response(429, json={"error": {"message": "slow down"}}),
            "rejected": lambda request, index: httpx.Response(400, json={"error": {"message": "bad field"}}),
            "server_error": lambda request, index: httpx.Response(503, text="upstream down"),
            # 200，但不是模型接口：地址多半填到了别的服务上
            "unexpected_response": lambda request, index: httpx.Response(200, text="<html>Welcome to nginx</html>"),
            "unreachable": lambda request, index: refuse(request),
            "timeout": lambda request, index: stall(request),
        }
        from ai_sql_agent.api.model_admin import PROBE_OUTCOMES

        client = self.app_client()
        for outcome, respond in cases.items():
            with self.subTest(outcome=outcome):
                self.assertIn(outcome, PROBE_OUTCOMES, "页面按这个闭集配文案")
                self.service.respond = respond
                report = self.post(client, coder_model="claude-sonnet-5")
                self.assertFalse(report["ok"])
                self.assertEqual(report["results"][0]["outcome"], outcome)

        self.service.respond = lambda request, index: httpx.Response(200, json={"status": "healthy"})
        report = self.post(client, coder_model="claude-sonnet-5")
        self.assertEqual((report["ok"], report["results"][0]["outcome"]), (False, "unexpected_response"))
        # 一连串失败之后熔断器不该把下一次测试挡在本地
        self.service.respond = chat_reply
        self.assertTrue(self.post(client, coder_model="claude-sonnet-5")["ok"])

    def test_whole_upstream_pages_are_not_passed_through(self):
        self.service.respond = lambda request, index: httpx.Response(
            500, text="<html><body>internal dashboard: db password = hunter2</body></html>")
        report = self.post(self.app_client(), coder_model="claude-sonnet-5")
        self.assertEqual(report["results"][0]["message"], "")
        self.assertNotIn("hunter2", json.dumps(report))

    def test_the_stored_key_is_not_sent_to_a_different_host_for_a_test_either(self):
        client = self.app_client()
        client.put("/admin/model", json=page_body())
        refused = client.post("/admin/model/test", json=page_body(api_key="", base_url=OTHER))
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json()["detail"]["code"], "key_required")
        self.assertEqual(self.service.calls, 0)

        kept = self.post(client, api_key="", coder_model="claude-sonnet-5")
        self.assertTrue(kept["ok"])
        self.assertEqual(self.service.sent(), [("relay.invalid", PAGE_KEY, "claude-sonnet-5")])

    def test_a_plain_http_address_comes_back_with_a_warning(self):
        client = self.app_client()
        report = self.post(client, base_url="http://relay.invalid", coder_model="claude-sonnet-5")
        self.assertEqual(report["warnings"], ["plaintext_http"])
        saved = client.put("/admin/model", json=page_body(base_url="http://relay.invalid"))
        self.assertEqual(saved.json()["warnings"], ["plaintext_http"])


LISTED = ["gpt-6-luna", "gpt-5.6-luna", "text-embedding-3-small", "Qwen/Qwen3-Embedding-0.6B",
          "BAAI/bge-reranker-v2-m3", "gpt-image-1", "gpt-4o-mini-tts", "whisper-1", "bad name!"]


def models_reply(_request=None, _index=0):
    return httpx.Response(200, json={"object": "list", "data": [{"id": name, "object": "model"} for name in LISTED]})


class ModelListTest(_Environment):
    """下拉框的候选：问接口有哪些模型。只读、不调模型、不保存；Key 的规则和连接测试一样。"""

    def respond(self, request, index):
        return models_reply()

    def listed(self, client, **overrides):
        body = {"provider": "openai", "base_url": f"{RELAY}/v1", "api_key": PAGE_KEY}
        body.update(overrides)
        response = client.post("/admin/model/models", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_no_secret(response.text)
        return response.json()

    def test_the_endpoints_models_are_listed_and_only_chat_models_offered(self):
        result = self.listed(self.app_client())
        self.assertEqual((result["ok"], result["outcome"]), (True, "ok"))
        self.assertEqual(result["models"], ["gpt-5.6-luna", "gpt-6-luna"])   # 向量、重排、图像、语音都不列
        self.assertEqual(result["total"], 8)                                  # 写法不合规的那条不算
        (request,) = self.service.requests
        self.assertEqual((request.method, request.url.path), ("GET", "/v1/models"))
        self.assertEqual(request.headers["authorization"], f"Bearer {PAGE_KEY}")
        self.assertFalse(self.store_path.exists(), "读列表不等于保存")

    def test_anthropic_lists_go_out_with_its_own_headers(self):
        self.service.respond = lambda request, index: httpx.Response(200, json={
            "data": [{"type": "model", "id": "claude-sonnet-5"}, {"type": "model", "id": "claude-opus-5-5"}],
            "has_more": False})
        result = self.listed(self.app_client(), provider="anthropic", base_url=RELAY)
        self.assertEqual(result["models"], ["claude-opus-5-5", "claude-sonnet-5"])
        (request,) = self.service.requests
        self.assertEqual(str(request.url), f"{RELAY}/v1/models?limit=1000")
        self.assertEqual((request.headers["x-api-key"], request.headers["anthropic-version"]),
                         (PAGE_KEY, get_settings().anthropic_version))
        self.assertNotIn("authorization", request.headers)

    def test_the_stored_key_is_not_sent_to_a_different_host_to_list_models(self):
        client = self.app_client()
        client.put("/admin/model", json=page_body(provider="openai", base_url=f"{RELAY}/v1",
                                                  chat_model="gpt-5.6-luna", coder_model="gpt-5.6-luna"))
        refused = client.post("/admin/model/models",
                              json={"provider": "openai", "base_url": f"{OTHER}/v1", "api_key": ""})
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json()["detail"]["code"], "key_required")
        self.assertEqual(self.service.calls, 0)

        kept = self.listed(client, api_key="")
        self.assertTrue(kept["ok"])
        self.assertEqual(self.service.requests[0].headers["authorization"], f"Bearer {PAGE_KEY}")

    def test_a_list_that_cannot_be_read_is_reported_not_raised(self):
        cases = {
            "auth": lambda request, index: httpx.Response(
                401, json={"error": {"message": f"Incorrect API key provided: {PAGE_KEY}"}}),
            "unsupported": lambda request, index: httpx.Response(404, json={"error": {"message": "not found"}}),
            "unexpected_response": lambda request, index: httpx.Response(200, text="<html>Welcome</html>"),
        }
        from ai_sql_agent.api.model_admin import PROBE_OUTCOMES

        client = self.app_client()
        for outcome, respond in cases.items():
            with self.subTest(outcome=outcome):
                self.assertIn(outcome, PROBE_OUTCOMES, "页面按这个闭集配文案")
                self.service.respond = respond
                result = self.listed(client)
                self.assertEqual((result["ok"], result["models"], result["outcome"]), (False, [], outcome))
        self.assertEqual(MODEL_ACCESS.snapshot(), [], "读不到列表不把正在用的 Key 记成「已被拒绝」")

    def test_embedding_models_are_listed_from_the_embedding_endpoint(self):
        response = self.app_client().post("/admin/model/embedding/models",
                                          json={"base_url": f"{RELAY}/v1", "api_key": PAGE_KEY})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["models"], ["Qwen/Qwen3-Embedding-0.6B", "text-embedding-3-small"])
        self.assertEqual(str(self.service.requests[0].url), f"{RELAY}/v1/models")
        self.assert_no_secret(response.text)

    def test_someone_else_on_the_network_cannot_list_with_the_stored_key(self):
        self.write_store(model_provider="openai", openai_api_key=PAGE_KEY, openai_base_url=f"{RELAY}/v1")
        client = self.app_client(**REMOTE)
        response = client.post("/admin/model/models",
                               json={"provider": "openai", "base_url": f"{RELAY}/v1", "api_key": ""})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "local_only")
        self.assertEqual(self.service.calls, 0)


if __name__ == "__main__":
    unittest.main()
