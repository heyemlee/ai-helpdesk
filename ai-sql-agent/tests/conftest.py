"""测试期与本机配置隔离。

两件事会让测试读到开发者本机的配置，两件都得挡：

1. 项目根目录的 `.env`——pydantic-settings 会自动读它。里面钉死的
   provider / base_url / 模型名会盖过用例自己设的环境变量（`AI_SQL_`
   前缀的优先级高于不带前缀的别名），断言就变成了「你的 .env 配了什么」。
2. shell 里导出的模型凭证——自从关键词抽取改成「有凭证走模型」，构造
   Text2SQLPipeline 的测试会真的去调模型 API：变慢、要联网、还花钱，
   而且模型输出会变，结果不再确定。

需要真实 HTTP 行为的测试（如 OpenAI 兼容端点测试）会在用例内部自行设置
指向本地 mock 服务的凭证，优先级高于这里。

第三件是同一类事：管理员在页面上保存过的模型连接（`runtime_data/model_settings.json`）
比环境变量的优先级还高。从仓库根目录跑测试时会读到本机服务用的那一份，所以整个关掉；
测它的用例（`test_model_settings.py`）自己指到临时文件上。
"""

import os

import pytest

_CREDENTIAL_VARS = (
    "AI_SQL_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "AI_SQL_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
)
_PAGE_STORE_SWITCH = "AI_SQL_MODEL_SETTINGS_ENABLED"


@pytest.fixture(scope="session", autouse=True)
def isolate_from_local_config():
    from ai_sql_agent.core import reload_settings
    from ai_sql_agent.core.settings import SqlAgentSettings

    saved_env_file = SqlAgentSettings.model_config.get("env_file")
    SqlAgentSettings.model_config["env_file"] = None

    saved = {key: os.environ.get(key) for key in (*_CREDENTIAL_VARS, _PAGE_STORE_SWITCH)}
    for key in _CREDENTIAL_VARS:
        os.environ[key] = ""
    os.environ[_PAGE_STORE_SWITCH] = "false"

    reload_settings()
    yield

    SqlAgentSettings.model_config["env_file"] = saved_env_file
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    reload_settings()


@pytest.fixture(autouse=True)
def forget_rejected_model_credentials():
    """
    模型访问状态是进程级的：一把 Key 被拒之后，同一个地址 + 同一把 Key 的调用不再发出。

    测试里的假地址、假 Key 来来回回就那几个，一个用例故意制造的 401 不该挡住下一个用例。
    需要这张表的用例自己在 setUp 里也会清一次——CI 走的是 `unittest discover`，不经过这里。
    """
    from ai_sql_agent.core import MODEL_ACCESS

    MODEL_ACCESS.reset_all()
    yield
    MODEL_ACCESS.reset_all()
