"""AI SQL Agent：面向业务数据库的 Text2SQL 服务。

子包按请求链路分层：
    api / a2a / mcp_server    对外入口
    pipeline                  编排：路由、记忆、Text2SQL 主链路
    normalization → retrieval → planning → generation → execution → repair → validation
    memory / auth / audit / semantic / feedback / ops
    core                      配置、模型传输、日志、指标、熔断
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
