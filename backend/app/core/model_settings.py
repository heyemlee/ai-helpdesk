"""
从控制台页面保存的模型连接：存在哪、怎么校验。

两组连接，分开存、分开恢复：

  chat       对话模型——意图识别、查询改写、重排、回答、摘要、评测判分都用它；
  embedding  向量模型——知识库检索、情景记忆、意图识别的向量那一路用它。

本来两组都只能写在 `backend/.env` 里，改了要重启。这里加一条运行期的路：在控制台保存，
服务写进一个小文件，并立刻换用新的连接（怎么换见 core/model_runtime.py）。

和 ai-sql-agent 的 `core/model_settings.py` 是同一套规则，两个项目的页面因此是一个样子：

1. **页面保存的那一组压过环境变量和 `.env`**，直到有人点「恢复为服务端配置」。
   存的是完整的一组（地址、Key、模型……），不会出现「文件里的地址 + 环境变量里的 Key」。
2. **Key 只进不出。** 文件里是明文（和 `.env` 一样，所以权限收到 0600），但没有任何接口
   把它读回去；对外只给「配没配」和末四位。日志里只记「换没换」。
3. **一把 Key 只发给它被填进来时对应的那台主机。** 换了接口地址而不重填 Key，等于把现有的
   Key 交给一台新主机——接口不允许（`same_address`）。

和 ai-sql-agent 不一样的两处：

- **Embedding 换了要重建库里的向量。** 新旧模型的向量不在一个空间里，所以保存 Embedding
  不是改个配置就完——要先用新模型把知识库和情景记忆的向量全部重算、整批换上，最后才写进
  这个文件（core/model_runtime.py 的切换任务）。
- **员工实例和客户实例读同一个文件。** compose 把同一个目录挂进两个容器：员工控制台保存，
  客户实例过几秒自己跟上。只有员工实例能改（`AI_HELPDESK_MODEL_SETTINGS_EDITABLE`）。

没配 `AI_HELPDESK_MODEL_SETTINGS_PATH` 时整个功能关闭：不读也不写，只认 `.env`。
本地开发和练习环境从工作区跑，不设这个变量就不会和生产共用一份配置。

这个文件只依赖标准库。
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SECTIONS = ("chat", "embedding")
PROVIDERS = ("anthropic", "openai")

OFFICIAL_BASE_URL = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
}

# 每组存哪几项。文件里出现别的键一律不认。
FIELDS = {
    "chat": ("provider", "base_url", "api_key", "model"),
    "embedding": ("base_url", "api_key", "model", "dim", "query_prefix"),
}

STORE_VERSION = 1
MAX_URL_LENGTH = 300
MAX_KEY_LENGTH = 512
MAX_MODEL_LENGTH = 120
MAX_PREFIX_LENGTH = 400
MAX_DIM = 16_000

_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@\-]*$")
# 全是数字 / 十六进制段的主机名：系统解析器会把 `2852039166`、`0xa9fea9fe` 这类写法当成
# IP 地址，而 `ipaddress` 不认。不规范的一律不收，免得绕过下面的地址检查。
_NUMERIC_HOST = re.compile(r"^(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+))*$")
_METADATA_HOSTS = {"metadata.google.internal", "metadata.goog", "metadata"}
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


class ModelSettingsError(ValueError):
    """提交的模型连接不成立。`code` 给页面配文案，`message` 给人和日志看。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ── 开关 ──────────────────────────────────────────────────────────────────────

def settings_path() -> str:
    """页面保存的模型连接放在哪。空 = 功能关闭。"""
    return os.getenv("AI_HELPDESK_MODEL_SETTINGS_PATH", "").strip()


def editable_here() -> bool:
    """这个实例能不能改。面向客户的实例设成 false：它只跟着员工实例保存的那份走。"""
    return os.getenv("AI_HELPDESK_MODEL_SETTINGS_EDITABLE", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }


# ── 校验 ──────────────────────────────────────────────────────────────────────

def _has_control_or_space(text: str) -> bool:
    return any(ch.isspace() or ord(ch) < 33 or ord(ch) == 127 for ch in text)


def clean_api_key(value: Any) -> str:
    """去掉首尾空白；空串表示「没填」。Key 要放进请求头，所以只收可见的 ASCII 字符。"""
    key = str(value or "").strip()
    if not key:
        return ""
    if len(key) > MAX_KEY_LENGTH or _has_control_or_space(key) or not key.isascii():
        raise ModelSettingsError(
            "invalid_api_key", "API Key 里有空白、不可见字符或非 ASCII 字符，或者太长。"
        )
    return key


def clean_model_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise ModelSettingsError("invalid_model", "请填写模型名。")
    if len(name) > MAX_MODEL_LENGTH or not _MODEL_NAME.match(name):
        raise ModelSettingsError("invalid_model", "模型名的写法不对：只能用字母、数字和 . _ - : / @。")
    return name


def clean_provider(value: Any) -> str:
    provider = str(value or "").strip().lower()
    if provider not in PROVIDERS:
        raise ModelSettingsError("invalid_provider", "协议只能是 openai 或 anthropic。")
    return provider


def clean_dim(value: Any) -> int:
    """Embedding 维度。0 表示「没填，按测试时模型实际返回的来」。"""
    if value in (None, ""):
        return 0
    try:
        dim = int(value)
    except (TypeError, ValueError):
        raise ModelSettingsError("invalid_dim", "向量维度要是一个正整数。") from None
    if dim < 0 or dim > MAX_DIM:
        raise ModelSettingsError("invalid_dim", f"向量维度要在 1 到 {MAX_DIM} 之间。")
    return dim


def clean_query_prefix(value: Any) -> str:
    """查询前缀可以带换行（Qwen3 的写法就有），但不收别的控制字符。"""
    text = str(value or "")
    if len(text) > MAX_PREFIX_LENGTH or any(
        ord(ch) < 32 and ch not in "\n\t" or ord(ch) == 127 for ch in text
    ):
        raise ModelSettingsError("invalid_query_prefix", "查询前缀太长，或者有不可见字符。")
    return text


def _address(host: str) -> ipaddress._BaseAddress | None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped or address


def _is_local_host(host: str, address: ipaddress._BaseAddress | None) -> bool:
    """本机或内网：明文 http 发到这里不出这台机器 / 这张内网，不用提醒。"""
    if address is not None:
        return bool(address.is_loopback or address.is_private)
    return host == "localhost" or "." not in host or host.endswith(_LOCAL_SUFFIXES)


def validate_base_url(value: Any) -> tuple[str, list[str]]:
    """
    接口地址 → `(规范写法, 提醒)`。不成立时抛 `ModelSettingsError`。

    服务会带着 Key 往这个地址发请求，所以它不是随便一个字符串：

    - 只收 http / https，不收带用户名密码、带 `?` `#` 的写法；
    - 不收链路本地地址（云主机的元数据服务就在 169.254.169.254）、组播、未指定地址，
      也不收 `2852039166` 这类系统会当成 IP、却躲得过检查的写法；
    - 公网地址用明文 http 时照收（有的中转站只有 http），但会提醒：Key 是明文在路上走的。

    域名最后解析到哪里由 DNS 决定，这里拦的是「直接填一个不该填的地址」。
    """
    text = str(value or "").strip()
    if not text:
        raise ModelSettingsError("invalid_base_url", "请填写接口地址。")
    if len(text) > MAX_URL_LENGTH or _has_control_or_space(text):
        raise ModelSettingsError("invalid_base_url", "接口地址里有空白或不可见字符，或者太长。")

    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ModelSettingsError("invalid_base_url", "接口地址要以 http:// 或 https:// 开头。")
    try:
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise ModelSettingsError("invalid_base_url", "接口地址的端口不对。") from None
    if not host:
        raise ModelSettingsError("invalid_base_url", "接口地址里没有主机名。")
    if parts.username is not None or parts.password is not None:
        raise ModelSettingsError("invalid_base_url", "接口地址里不要带用户名和密码。")
    if parts.query or parts.fragment:
        raise ModelSettingsError("invalid_base_url", "接口地址里不要带 ? 或 # 之后的部分。")

    address = _address(host)
    if address is None and _NUMERIC_HOST.match(host):
        raise ModelSettingsError("forbidden_address", "接口地址的主机名写法不规范。")
    if host in _METADATA_HOSTS or (
        address is not None
        and (
            address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or (address.is_reserved and not address.is_loopback)
        )
    ):
        raise ModelSettingsError("forbidden_address", "这个地址不能用作模型接口地址。")

    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    normalized = f"{scheme}://{netloc}{parts.path.rstrip('/')}"
    warnings = ["plaintext_http"] if scheme == "http" and not _is_local_host(host, address) else []
    return normalized, warnings


def origin_of(url: str) -> str:
    """地址 → 「协议 + 主机 + 端口」。Key 是交给一台主机的，路径怎么写不改变它交给了谁。"""
    parts = urlsplit(str(url or "").strip())
    try:
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return ""
    if not host:
        return ""
    scheme = (parts.scheme or "https").lower()
    default = {"http": 80, "https": 443}.get(scheme)
    suffix = f":{port}" if port is not None and port != default else ""
    return f"{scheme}://{host}{suffix}"


def api_root(url: str) -> str:
    """
    OpenAI 兼容服务的接口根地址：只写到主机（`https://relay.example.com`）时补上 `/v1`。

    官方和绝大多数中转站的接口都在 `/v1` 下面。OpenAI SDK 把地址原样当根，只写到主机的话
    请求会落到 `https://relay.example.com/chat/completions`——中转站在那里回的往往是
    它的网页（200 + HTML），报错只剩一句 `'str' object has no attribute 'choices'`。
    ai-sql-agent 一直会补 `/v1`，同一个地址在两个项目里应该是同一个结果。

    带了路径的（`/v1`、`/api/v1`、`/compatible-mode/v1`、`/v1beta/openai`）原样用，不去猜。
    """
    text = str(url or "").strip().rstrip("/")
    parts = urlsplit(text)
    if parts.scheme in ("http", "https") and parts.netloc and not parts.path:
        return f"{text}/v1"
    return text


def same_address(left: str, right: str) -> bool:
    """两个接口地址是不是同一台主机。只有是，现有的 Key 才可以不重填、接着用。"""
    a, b = origin_of(left), origin_of(right)
    return bool(a) and a == b


def key_hint(api_key: str) -> str:
    """给人认「配的是哪一把」用：末四位。太短的 Key 不给——四位就快是全部了。"""
    key = api_key or ""
    return key[-4:] if len(key) >= 12 else ""


# ── 存取 ──────────────────────────────────────────────────────────────────────

def _clean_section(section: str, raw: Any) -> tuple[dict[str, Any] | None, str]:
    """文件里的一组 → (能用的值, 问题)。缺地址或 Key 的一组整个不认。"""
    if not isinstance(raw, dict):
        return None, "格式不对"
    values = raw.get("values")
    if not isinstance(values, dict):
        return None, "缺少 values"
    try:
        cleaned: dict[str, Any] = {
            "base_url": validate_base_url(values.get("base_url"))[0],
            "api_key": clean_api_key(values.get("api_key")),
            "model": clean_model_name(values.get("model")),
        }
        if section == "chat":
            cleaned["provider"] = clean_provider(values.get("provider"))
        else:
            cleaned["dim"] = clean_dim(values.get("dim"))
            cleaned["query_prefix"] = clean_query_prefix(values.get("query_prefix"))
            if not cleaned["dim"]:
                return None, "缺少向量维度"
    except ModelSettingsError as ex:
        return None, ex.message
    if not cleaned["api_key"]:
        return None, "不完整：接口地址和 Key 要成对出现"
    return {
        "values": cleaned,
        "saved_at": str(raw.get("saved_at") or ""),
        "saved_by": str(raw.get("saved_by") or ""),
    }, ""


def load_store(path: str) -> dict[str, Any]:
    """
    读页面保存的那份配置 → `{"chat": {...}, "embedding": {...}, "problems": {...}}`。

    没配路径、文件不存在都返回空；某一组写坏了只丢那一组，问题记在 `problems` 里，
    由调用方记日志——服务必须照常起得来，用的就是 `.env` 里的那一份。
    """
    if not path:
        return {}
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        return {"problems": {"file": f"读不了：{type(exc).__name__}"}}
    try:
        document = json.loads(raw)
    except ValueError:
        return {"problems": {"file": "不是合法的 JSON"}}
    if not isinstance(document, dict):
        return {"problems": {"file": "格式不对"}}

    store: dict[str, Any] = {}
    problems: dict[str, str] = {}
    for section in SECTIONS:
        if section not in document:
            continue
        cleaned, problem = _clean_section(section, document[section])
        if cleaned is not None:
            store[section] = cleaned
        else:
            problems[section] = problem
    if problems:
        store["problems"] = problems
    return store


def _write_document(path: str, document: dict[str, Any]) -> None:
    """先写临时文件再改名：读的一方（包括另一个容器里的客户实例）要么看到旧的、要么看到新的。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_document(path: str) -> dict[str, Any]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def save_section(path: str, section: str, values: dict[str, Any], *, saved_by: str = "") -> dict[str, Any]:
    """把一组连接写进文件，另一组原样保留。返回写进去的那一组（含 Key，调用方别往外发）。"""
    if not path:
        raise ModelSettingsError("disabled", "这台服务没有开启页面配置模型连接。")
    if section not in SECTIONS:
        raise ModelSettingsError("invalid_section", f"不认识的连接：{section}")
    document = _read_document(path)
    entry = {
        "saved_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "saved_by": saved_by,
        "values": {name: values[name] for name in FIELDS[section] if name in values},
    }
    document["version"] = STORE_VERSION
    document[section] = entry
    _write_document(path, document)
    return entry


def clear_section(path: str, section: str) -> bool:
    """丢掉页面保存的一组（回到 `.env`）。本来就没有时返回 False。"""
    if not path:
        return False
    document = _read_document(path)
    if section not in document:
        return False
    document.pop(section)
    if any(name in document for name in SECTIONS):
        _write_document(path, document)
    else:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass
    return True


def file_stamp(path: str) -> tuple[int, int] | None:
    """文件的 (修改时间, 大小)。客户实例靠它发现员工实例保存过；文件不在时是 None。"""
    if not path:
        return None
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size
