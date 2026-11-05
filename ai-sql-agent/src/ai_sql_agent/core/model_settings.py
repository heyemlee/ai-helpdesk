"""
从页面保存的模型连接：存在哪、怎么校验、怎么让已经建好的模型 Client 跟上。

模型连接（用哪家协议、接口地址、Key、两个模型名）本来只能写在 `.env` 里，改了要重启。
这里加一条运行期的路：管理员在页面上保存，服务把它写进一个小文件，并立刻换用新的连接。

三件事决定了这个模块的样子：

1. **页面保存的配置压过环境变量和 `.env`**，但只压「主链路的模型连接」这几项
   （`OVERRIDABLE`），而且存下来的是完整的一组：协议、接口地址、Key、两个模型名。
   Embedding / Rerank 的凭证不在其中——它们继续用服务端的配置，
   也不会因为页面换了 OpenAI 兼容通道的 Key 而跟着换（见 `settings.apply_model_overrides`）。
2. **Key 只进不出。** 文件里是明文（和 `.env` 一样，所以权限收到 0600），但没有任何接口
   把它读回去；对外只给「配没配」和末四位。日志里只记「换没换」。
3. **一把 Key 只发给它被填进来时对应的那个地址。** 换接口地址而不重填 Key，等于把现有的
   Key 交给一个新的主机——页面和接口都不允许（`same_address`），已经建好的 Client 换连接时
   也是整组一起换，不会出现「新地址 + 旧 Key」的中间状态（`SettingsBinding`）。

Embedding 是另一节（`embedding`），和主链路的连接分开存、分开恢复：Schema 检索用它把
表、字段说明变成向量。它不在 `OVERRIDABLE` 里——换 Embedding 要把检索索引整个重建一遍，
由 `api.model_admin` 先建好新索引、再写这个文件、最后一次换上。

这个文件只依赖标准库：`settings.py` 要在构造配置时读它，不能反过来再依赖配置或日志。
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

PROVIDERS = ("anthropic", "openai")

OFFICIAL_BASE_URL = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
}

OVERRIDABLE = (
    "model_provider",
    "anthropic_api_key",
    "anthropic_base_url",
    "openai_api_key",
    "openai_base_url",
    "chat_model",
    "coder_model",
)
"""页面能改的配置项，名字就是 `SqlAgentSettings` 的字段名。文件里出现别的键一律不认。"""



def key_field(provider: str) -> str:
    """这家协议的 Key 存在哪个配置项里。"""
    return f"{provider}_api_key"


def url_field(provider: str) -> str:
    """这家协议的接口地址存在哪个配置项里。"""
    return f"{provider}_base_url"


EMBEDDING_FIELDS = ("base_url", "api_key", "model", "dimensions")
"""页面保存的 Embedding 那一节存哪几项。"""

MAX_DIMENSIONS = 8192

WARNINGS = ("plaintext_http",)
"""`validate_base_url` 会给出的提醒。页面按这些代号配文案。"""

STORE_VERSION = 1
MAX_URL_LENGTH = 300
MAX_KEY_LENGTH = 512
MAX_MODEL_LENGTH = 120

_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@\-]*$")
# 全是数字 / 十六进制段的主机名：系统解析器会把 `2852039166`、`0xa9fea9fe` 这类写法当成
# IP 地址，而 `ipaddress` 不认。不规范的一律不收，免得绕过下面的地址检查。
_NUMERIC_HOST = re.compile(r"^(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+))*$")
_METADATA_HOSTS = {"metadata.google.internal", "metadata.goog", "metadata"}
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


class ModelSettingsError(ValueError):
    """页面提交的模型连接不成立。`code` 给页面做文案映射，`message` 给人和日志看。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ----------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------


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


def clean_model_name(value: Any, what: str) -> str:
    name = str(value or "").strip()
    if not name:
        return ""
    if len(name) > MAX_MODEL_LENGTH or not _MODEL_NAME.match(name):
        raise ModelSettingsError("invalid_model", f"{what}的写法不对：只能用字母、数字和 . _ - : / @。")
    return name


def clean_dimensions(value: Any) -> int:
    """向量维度：正整数，不超过 `MAX_DIMENSIONS`。"""
    if isinstance(value, bool):
        raise ModelSettingsError("invalid_dimensions", "向量维度要是一个正整数。")
    try:
        number = int(str(value).strip())
    except ValueError:
        raise ModelSettingsError("invalid_dimensions", "向量维度要是一个正整数。") from None
    if not 0 < number <= MAX_DIMENSIONS:
        raise ModelSettingsError("invalid_dimensions", f"向量维度要在 1 到 {MAX_DIMENSIONS} 之间。")
    return number


def _address(host: str) -> Optional[ipaddress._BaseAddress]:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped or address


def _is_local_host(host: str, address: Optional[ipaddress._BaseAddress]) -> bool:
    """本机或内网：明文 http 发到这里不出这台机器 / 这张内网，不用提醒。"""
    if address is not None:
        return bool(address.is_loopback or address.is_private)
    return host == "localhost" or "." not in host or host.endswith(_LOCAL_SUFFIXES)


def validate_base_url(value: Any) -> Tuple[str, List[str]]:
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
    warnings = (
        ["plaintext_http"] if scheme == "http" and not _is_local_host(host, address) else []
    )
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


def same_address(left: str, right: str) -> bool:
    """两个接口地址是不是同一台主机。只有是，现有的 Key 才可以不重填、接着用。"""
    a, b = origin_of(left), origin_of(right)
    return bool(a) and a == b


def key_hint(api_key: str) -> str:
    """给人认「配的是哪一把」用：末四位。太短的 Key 不给——四位就快是全部了。"""
    key = api_key or ""
    return key[-4:] if len(key) >= 12 else ""


# ----------------------------------------------------------------------
# 存取
# ----------------------------------------------------------------------


def _read_document(path: str) -> Dict[str, Any]:
    """
    读整份文件（写回时要保留另一节）。没有文件是空的；读不了抛 OSError，
    不是 JSON、或者不是一个 JSON 对象抛 ValueError。
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("不是一个 JSON 对象")
    return document


def _chat_section(document: Dict[str, Any]) -> Dict[str, Any]:
    """主链路那一节 → `{"values", "saved_at", "saved_by"}`，不成立时 `{"problem": ...}`。"""
    values = document.get("values")
    if not isinstance(values, dict):
        return {"problem": "缺少 values"}

    cleaned = {
        name: value.strip()
        for name, value in values.items()
        if name in OVERRIDABLE and isinstance(value, str) and value.strip()
    }
    provider = cleaned.get("model_provider")
    if provider not in PROVIDERS:
        return {"problem": "没有写明是哪家协议"}
    # 只认「这家协议的地址 + 这家协议的 Key」成对出现的配置：缺一样就会拿文件里的地址
    # 去配环境变量里的 Key（或者反过来），那正是这个模块要挡的事。另一家协议的字段不读——
    # Embedding / Rerank 回落用的是服务端配的 OpenAI 兼容凭证，不该被这个文件改掉。
    selected = ("model_provider", key_field(provider), url_field(provider), "chat_model", "coder_model")
    if not cleaned.get(key_field(provider)) or not cleaned.get(url_field(provider)):
        return {"problem": "不完整：接口地址和 Key 要成对出现"}
    return {
        "values": {name: cleaned[name] for name in selected if name in cleaned},
        "saved_at": str(document.get("saved_at") or ""),
        "saved_by": str(document.get("saved_by") or ""),
    }


def _embedding_section(entry: Any) -> Dict[str, Any]:
    """Embedding 那一节 → `{"values", "saved_at", "saved_by"}`，不成立时 `{"problem": ...}`。"""
    values = entry.get("values") if isinstance(entry, dict) else None
    if not isinstance(values, dict):
        return {"problem": "缺少 values"}
    try:
        cleaned = {
            "base_url": validate_base_url(values.get("base_url"))[0],
            "api_key": clean_api_key(values.get("api_key")),
            "model": clean_model_name(values.get("model"), "Embedding 模型"),
            "dimensions": clean_dimensions(values.get("dimensions")),
        }
    except ModelSettingsError as error:
        return {"problem": error.message}
    if not cleaned["api_key"] or not cleaned["model"]:
        return {"problem": "不完整：地址、Key、模型名都要有"}
    return {
        "values": cleaned,
        "saved_at": str(entry.get("saved_at") or ""),
        "saved_by": str(entry.get("saved_by") or ""),
    }


def load_store(path: str) -> Dict[str, Any]:
    """
    读页面保存的那份配置 → `{"values": {...}, "saved_at": ..., "saved_by": ...,
    "embedding": {"values": {...}, "saved_at": ..., "saved_by": ...}}`。两节都可能没有。

    没配路径、文件不存在、文件坏了，都返回空——服务必须照常起得来，
    用的就是环境变量和 `.env` 里的那一份。某一节不成立时只丢那一节，
    原因放在 `problem`（主链路）/ `embedding_problem` 里，由调用方去记日志。
    """
    if not path:
        return {}
    try:
        document = _read_document(path)
    except OSError as exc:
        return {"problem": f"读不了：{type(exc).__name__}"}
    except ValueError:
        return {"problem": "不是合法的 JSON 对象"}
    if not document:
        return {}

    store: Dict[str, Any] = {}
    if "values" in document or "embedding" not in document:
        chat = _chat_section(document)
        if "problem" in chat:
            store["problem"] = chat["problem"]
        else:
            store.update(chat)
    if "embedding" in document:
        embedding = _embedding_section(document["embedding"])
        if "problem" in embedding:
            store["embedding_problem"] = embedding["problem"]
        else:
            store["embedding"] = embedding
    return store


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _write_document(path: str, document: Dict[str, Any]) -> None:
    """
    先写临时文件再改名，读的一方要么看到旧的、要么看到新的。

    文件里有明文 Key，权限收到只有服务自己的账号能读（0600）。
    """
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


def _existing(path: str) -> Dict[str, Any]:
    """写之前读一下现有的文件，好把另一节原样留下。文件坏了就当没有。"""
    try:
        return _read_document(path)
    except (OSError, ValueError):
        return {}


def _keep_embedding(document: Dict[str, Any]) -> Dict[str, Any]:
    return {"embedding": document["embedding"]} if "embedding" in document else {}


def save_store(path: str, values: Dict[str, str], *, saved_by: str = "") -> Dict[str, Any]:
    """把页面保存的主链路连接写进文件（Embedding 那一节原样保留）。"""
    if not path:
        raise ModelSettingsError("disabled", "这台服务没有开启页面配置模型连接。")
    document = {
        "version": STORE_VERSION,
        "saved_at": _now(),
        "saved_by": saved_by,
        "values": {name: values[name] for name in OVERRIDABLE if values.get(name)},
        **_keep_embedding(_existing(path)),
    }
    _write_document(path, document)
    return document


def clear_store(path: str) -> bool:
    """丢掉页面保存的主链路连接（回到环境变量和 `.env`）。本来就没有时返回 False。"""
    if not path:
        return False
    document = _existing(path)
    if not Path(path).exists():
        return False
    had_chat = "values" in document or not document
    remaining = _keep_embedding(document)
    if remaining:
        _write_document(path, {"version": STORE_VERSION, **remaining})
    else:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            return False
    return had_chat


def save_embedding(path: str, values: Dict[str, Any], *, saved_by: str = "") -> Dict[str, Any]:
    """把页面保存的 Embedding 连接写进文件（主链路那一节原样保留）。"""
    if not path:
        raise ModelSettingsError("disabled", "这台服务没有开启页面配置模型连接。")
    document = _existing(path)
    document["version"] = STORE_VERSION
    document["embedding"] = {
        "saved_at": _now(),
        "saved_by": saved_by,
        "values": {name: values[name] for name in EMBEDDING_FIELDS if name in values},
    }
    _write_document(path, document)
    return document["embedding"]


def clear_embedding(path: str) -> bool:
    """丢掉页面保存的 Embedding 连接。本来就没有时返回 False。"""
    if not path:
        return False
    document = _existing(path)
    if "embedding" not in document:
        return False
    document.pop("embedding")
    if "values" in document:
        _write_document(path, document)
    else:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass
    return True


# ----------------------------------------------------------------------
# 让已经建好的模型 Client 跟上
# ----------------------------------------------------------------------

_version = 0
_version_lock = threading.Lock()


def connection_version() -> int:
    """模型连接换过几次。Client 拿它判断自己手里那一份是不是旧的。"""
    return _version


def bump_connection_version() -> int:
    global _version
    with _version_lock:
        _version += 1
        return _version


class SettingsBinding:
    """
    一个模型 Client 的连接参数里，哪些是构造时没给、要跟着全局配置走的。

    Client 在构造时把 Key、地址、模型名各抄了一份。页面换了模型连接之后，
    这些抄下来的值就是旧的；重启当然能解决，但「保存后立刻生效」要求不重启也跟上。
    所以每个 Client 留一个这样的记录：`refresh()` 发现连接换过，就把当初**没写、取自配置**
    的那几项重新取一遍。构造时显式传进来的值（测试、脚本里常这么用）永远不动。

    `refresh()` 自己不加锁——Client 在一把锁里调用它并顺手重建 transport，
    再把「地址 + 模型 + transport」作为一组取走，这样一次调用用到的永远是同一份连接。
    """

    def __init__(self, config: Any, resolvers: Dict[str, Callable[[Any], Any]]):
        self._config = config
        self._resolvers = {
            name: resolve for name, resolve in resolvers.items() if not getattr(config, name)
        }
        self._seen = -1

    def refresh(self, settings_provider: Callable[[], Any]) -> bool:
        """有参数被重新取过时返回 True。"""
        version = connection_version()
        if version == self._seen:
            return False
        first = self._seen == -1
        self._seen = version
        if not self._resolvers:
            return first
        settings = settings_provider()
        for name, resolve in self._resolvers.items():
            setattr(self._config, name, resolve(settings))
        return True
