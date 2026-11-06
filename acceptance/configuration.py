"""Frozen acceptance parameters shared by evaluation, fault and recovery work."""
CONFIGURATION = {"model": "gpt-6.1-sol", "api_mode": "responses", "max_output_tokens": 2048,
                 "http_retries": 0, "reasoning_effort": "low", "helpdesk_embedding": "local-hash-256",
                 "sql_embedding": "local-hash", "sql_rerank": "local-lexical", "replicas": 1,
                 "worker_concurrency": 4, "redispatch_seconds": 30, "cross_session_memory": False,
                 "fixture_quantity_unit": "pcs", "degrade_backlog": [20, 40, 80, 160]}
