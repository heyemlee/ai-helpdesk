"""Real employee/session boundaries against disposable PostgreSQL."""
import asyncio
import os
import threading
import uuid

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api.staff_api import router
from app.core.actor import ActorContext, ActorDirectory
from app.core.staff_auth import StaffAuth, StaffSessionMiddleware, hash_password, verify_password
from app.storage.pg import Database

PASSWORD = "fixture-password-for-tests-only"


@pytest.fixture(scope="module")
def encoded():
    return hash_password(PASSWORD)


@pytest.fixture
async def staff(encoded):
    import asyncpg

    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN required")
    control = await asyncpg.connect(dsn)
    name = "staff_" + uuid.uuid4().hex[:12]
    await control.execute(f'CREATE DATABASE "{name}"')
    db = Database(dsn.rsplit("/", 1)[0] + "/" + name, min_size=1, max_size=3)
    try:
        await db.connect(128)
        auth = StaffAuth(db, "staff", origin="https://employees.test")
        actors = {}
        for name_, role in (("alice", "employee"), ("bob", "operations"), ("root", "admin"), ("root2", "admin")):
            await db.execute("INSERT INTO staff_accounts(actor_id,instance,login,password_hash,role) VALUES($1,'staff',$1,$2,$3)", name_, encoded, role)
            actors[name_] = await auth.provider(name_, "staff")
        app = FastAPI()
        app.state.staff_auth = auth
        app.state.actor_directory = ActorDirectory(auth.provider)
        app.add_middleware(StaffSessionMiddleware)
        app.include_router(router)

        @app.get("/private")
        async def private():
            return {"protected": True}

        @app.get("/admin/probe")
        async def admin():
            return {"admin": True}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=auth.origin) as client:
            yield auth, actors, client, app
    finally:
        await db.close()
        await control.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await control.close()


async def login(client, name="alice"):
    response = await client.post("/auth/login", json={"login": name, "password": PASSWORD}, headers={"Origin": "https://employees.test"})
    assert response.status_code == 200, response.text
    return {"Origin": "https://employees.test", "X-CSRF-Token": response.json()["csrf_token"]}


def test_passwords_are_salted_and_no_truncation(encoded):
    assert PASSWORD not in encoded
    assert verify_password(PASSWORD, encoded)
    assert not verify_password(PASSWORD + "x", encoded)
    assert not verify_password("x"*1000, encoded)
    assert hash_password(PASSWORD) != encoded


async def test_cookie_csrf_role_and_service_key_cannot_login(staff):
    auth, actors, client, _ = staff
    for headers in ({}, {"X-API-Key": "service", "X-Actor-Id": "root", "X-Actor-Roles": "admin"}):
        assert (await client.get("/private", headers=headers)).status_code == 401
    assert (await client.post("/auth/login", json={"login": "alice", "password": PASSWORD})).status_code == 403
    response = await client.post("/auth/login", json={"login": "alice", "password": PASSWORD}, headers={"Origin": auth.origin})
    cookie = response.headers["set-cookie"]
    assert all(s in cookie for s in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"))
    assert "Domain=" not in cookie
    assert (await client.get("/admin/probe")).status_code == 403
    assert (await client.post("/auth/conversations", headers={"Origin": auth.origin})).status_code == 403
    headers = {"Origin": auth.origin, "X-CSRF-Token": response.json()["csrf_token"]}
    assert (await client.post("/auth/conversations", headers=headers)).status_code == 201
    assert (await client.post("/auth/token", headers={**headers, "X-API-Key": "service"}, json={"user_id": "root"})).status_code == 403
    token = client.cookies.get(auth.cookie)
    assert token not in str(await auth.db.fetch("SELECT * FROM staff_sessions"))


async def test_expiry_revoke_logout_and_ownership(staff):
    auth, actors, client, app = staff
    headers = await login(client)
    conv = (await client.post("/auth/conversations", headers=headers)).json()["conv_id"]
    await auth.conversation(actors["alice"], conv)
    with pytest.raises(HTTPException):
        await auth.conversation(actors["bob"], conv)
    with pytest.raises(HTTPException):
        await auth.conversation(actors["alice"], "unowned-legacy")
    assert (await client.post("/auth/logout", headers=headers)).status_code == 200
    assert (await client.get("/private")).status_code == 401
    with pytest.raises(HTTPException):
        await app.state.actor_directory.from_snapshot(actors["alice"].snapshot())
    await login(client)
    await auth.db.execute("UPDATE staff_sessions SET last_seen=now()-interval '1 hour'")
    assert (await client.get("/private")).status_code == 401


async def test_shared_login_rate_limit_unknown_and_wrong_password(staff):
    auth, _, _, _ = staff
    for _ in range(6):
        with pytest.raises(HTTPException) as err:
            await auth.login("unknown", "wrong", "peer")
        assert err.value.status_code == 401
    with pytest.raises(HTTPException) as err:
        await StaffAuth(auth.db, "staff").login("unknown", "wrong", "peer")
    assert err.value.status_code == 429


async def test_cancelled_hashes_keep_real_capacity_bounded():
    auth = StaffAuth(None, "staff")
    started, release = threading.Event(), threading.Event()
    running, maximum = 0, 0
    lock = threading.Lock()

    def blocked():
        nonlocal running, maximum
        with lock:
            running += 1
            maximum = max(maximum, running)
            if running == 2:
                started.set()
        release.wait(5)
        with lock:
            running -= 1

    jobs = [asyncio.create_task(auth.password_work(blocked)) for _ in range(2)]
    try:
        assert await asyncio.to_thread(started.wait, 2)
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        for _ in range(3):
            with pytest.raises(HTTPException) as err:
                await auth.password_work(blocked)
            assert err.value.status_code == 429
        assert maximum == 2
    finally:
        release.set()
        await asyncio.gather(*auth._hash_tasks)
    assert await auth.password_work(lambda: "ready") == "ready"


async def test_revoked_admin_cannot_commit_after_password_work(staff, monkeypatch):
    auth, actors, _, _ = staff
    began, release = asyncio.Event(), asyncio.Event()
    original = auth.password_work

    async def delayed(*args):
        began.set()
        await release.wait()
        return await original(*args)
    monkeypatch.setattr(auth, "password_work", delayed)
    task = asyncio.create_task(auth.create_account("newbie", PASSWORD, "admin", by=actors["root"]))
    await began.wait()
    await auth.change_account("root", by=actors["root2"], active=False)
    release.set()
    with pytest.raises(HTTPException) as err:
        await task
    assert err.value.status_code == 401
    assert not await auth.db.fetchval("SELECT 1 FROM staff_accounts WHERE login='newbie'")


async def test_live_account_provider_reuses_readonly_single_connection(staff):
    auth, actors, _, app = staff
    async with auth.db.transaction() as conn:
        await conn.execute("SET TRANSACTION READ ONLY")
        assert await app.state.actor_directory.refresh(actors["alice"], conn=conn) == actors["alice"]
    await auth.change_account("alice", by=actors["root"], role="operations")
    with pytest.raises(HTTPException):
        await app.state.actor_directory.from_snapshot(actors["alice"].snapshot())


@pytest.mark.parametrize("role,audience,allowed", [("employee","admin",False), ("employee","operations",False),
    ("operations","admin",False), ("employee","employee",True), ("operations","operations",True), ("admin","admin",True)])
def test_history_time_never_expands_audience(role, audience, allowed):
    actor = ActorContext("actor", frozenset({role}), "staff")
    scope = {"audience": audience, "as_of": "2020-01-01T00:00:00Z"}
    if allowed:
        actor.require_policy(scope)
    else:
        with pytest.raises(HTTPException):
            actor.require_policy(scope)


async def test_simultaneous_logout_serializes_and_never_deadlocks(staff):
    auth, actors, _, _ = staff
    results = await asyncio.wait_for(asyncio.gather(
        auth.logout(actors['alice']), auth.logout(actors['alice']), return_exceptions=True), 3)
    assert results.count(None) == 1
    rejected = next(x for x in results if x is not None)
    assert isinstance(rejected, HTTPException) and rejected.status_code == 401


@pytest.mark.parametrize('env', [
    {'APP_ENV': ' production ', 'AI_HELPDESK_INSTANCE': ' STAFF ', 'AI_HELPDESK_STAFF_AUTH': 'disabled'},
    {'APP_ENV': 'prod', 'AI_HELPDESK_INSTANCE': 'staff', 'AI_HELPDESK_STAFF_AUTH': 'unknown'},
    {'APP_ENV': 'production'},
    {'AI_HELPDESK_STAFF_AUTH': 'sso'},
])
async def test_startup_and_middleware_fail_closed_with_invalid_provider(env, monkeypatch):
    from app.core.preflight import InsecureDeploymentError, run_startup_checks
    from app.core.staff_auth import check_config
    with pytest.raises(ValueError):
        check_config(env)
    with pytest.raises(InsecureDeploymentError):
        run_startup_checks(env)
    for key in ('APP_ENV', 'AI_HELPDESK_INSTANCE', 'AI_HELPDESK_STAFF_AUTH'):
        monkeypatch.setenv(key, env.get(key, '')) if key in env else monkeypatch.delenv(key, raising=False)
    app = FastAPI()
    app.add_middleware(StaffSessionMiddleware)
    @app.get('/admin/probe')
    async def probe():
        return {'unsafe': True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        assert (await client.get('/admin/probe')).status_code == 503


async def test_admin_policy_commit_checks_epoch_after_slow_work(staff):
    from app.core.staff_auth import current_actor, current_auth
    from app.storage.vector_store import PgStore
    auth, actors, _, _ = staff
    store = PgStore(auth.db)
    actor_token, auth_token = current_actor.set(actors['root']), current_auth.set(auth)
    try:
        await auth.change_account('root', by=actors['root2'], active=False)
        with pytest.raises(HTTPException) as error:
            await store.kb_doc_add({'id': 'blocked', 'title': 'policy', 'content': 'text', 'content_hash': 'x', 'created_at': 1})
        assert error.value.status_code == 401
        assert not await auth.db.fetchval("SELECT 1 FROM policy_documents WHERE id='blocked'")
        with pytest.raises(HTTPException):
            await store.replace_embeddings(128, {}, {}, 'new-model')
    finally:
        current_actor.reset(actor_token)
        current_auth.reset(auth_token)


async def test_sql_gateway_owns_history_and_never_forwards_browser_identity(staff, monkeypatch, tmp_path):
    from app.api.staff_sql import router as sql_router
    auth, actors, client, app = staff
    app.include_router(sql_router)
    monkeypatch.setenv('SQL_AGENT_API_KEY', 'read-service-key')
    key = tmp_path/'sql-admin-key'
    key.write_text('admin-service-key')
    monkeypatch.setenv('SQL_AGENT_ADMIN_API_KEY_FILE', str(key))
    seen = []
    def remote(request):
        import json
        seen.append(request)
        if request.url.path == '/query':
            assert request.headers['X-API-Key'] == 'read-service-key'
            body = json.loads(request.content)
            assert body['user_id'] == 'ai-helpdesk' and body['enable_long_term'] is False
            assert body['session_id'] != 'other-employee-session'
            return httpx.Response(200, json={'answer': 'read only', 'trace_id': 'trace-alice'}, headers={'X-Run-Id': 'alice-run'})
        if request.url.path == '/history':
            return httpx.Response(200, json={'items': [{'run_id': 'alice-run'}, {'run_id': 'other-private-run'}]})
        return httpx.Response(200, json={'ok': True})
    app.state.sql_transport = httpx.MockTransport(remote)
    headers = await login(client)
    result = await client.post('/sql-api/query', headers={**headers, 'X-API-Key': 'forged', 'X-Actor-Id': 'root'},
                               json={'query': 'order', 'user_id': 'root', 'session_id': 'other-employee-session', 'enable_long_term': True})
    assert result.status_code == 200
    assert [x['run_id'] for x in (await client.get('/sql-api/history?user_id=root')).json()['items']] == ['alice-run']
    for i in range(25):
        await auth.db.execute("INSERT INTO staff_sql_runs(run_id,instance,actor_id,history) VALUES($1,'staff','bob','{}')", f'bob-{i}')
    assert [x['run_id'] for x in (await client.get('/sql-api/history?limit=1')).json()['items']] == ['alice-run']
    assert (await client.get('/sql-api/runs/other-private-run')).status_code == 404
    assert (await client.put('/sql-api/admin/model', headers=headers, json={})).status_code == 403
    assert (await client.post('/sql-api/a2a', headers=headers, json={'method': 'tasks/get'})).status_code == 403
    headers = await login(client, 'bob')
    assert (await client.get('/sql-api/runs/alice-run')).status_code == 404
    assert all(x['run_id'].startswith('bob-') for x in (await client.get('/sql-api/history')).json()['items'])
    for params in [None, [], 'bad']:
        assert (await client.post('/sql-api/a2a', headers=headers, json={'method': 'message/stream', 'params': params})).status_code == 422
    assert len(seen) == 1


async def test_employee_case_listing_filters_audience_without_legacy_admin_access(staff):
    from types import SimpleNamespace

    from app.api.case_api import router as case_router
    from app.tickets.cases import CaseInput, CaseService
    auth, actors, client, app = staff
    service = CaseService(auth.db, 'staff', actors=app.state.actor_directory)
    ordinary = await service.accept_input(actors['alice'], CaseInput(new_case=True, conv_id='ordinary', message_id='m1', message='allowed'))
    private = await service.accept_input(actors['root'], CaseInput(new_case=True, conv_id='admin', message_id='m2', message='private', policy_scope={'audience':'admin'}))
    service.runs = SimpleNamespace()
    app.state.case_service = service
    app.include_router(case_router)
    await login(client)
    response = await client.get('/tickets/cases')
    assert response.status_code == 200
    assert [c['id'] for c in response.json()['tickets']] == [ordinary['case_id']]
    assert (await client.get('/tickets/'+private['case_id']+'/case')).status_code == 403
    assert (await client.get('/tickets')).status_code == 403


async def test_sql_service_credential_failure_does_not_revoke_employee_session(staff, monkeypatch):
    from app.api.staff_sql import router as sql_router
    auth, actors, client, app = staff
    app.include_router(sql_router)
    monkeypatch.setenv('SQL_AGENT_API_KEY', 'fixture-service-key')
    app.state.sql_transport = httpx.MockTransport(lambda request: httpx.Response(401, json={'detail':'downstream credentials refused'}))
    await login(client)
    response = await client.get('/sql-api/whoami')
    assert response.status_code == 502
    assert 'downstream credentials refused' not in response.text
    assert (await client.get('/auth/session')).status_code == 200
    assert (await client.get('/private')).status_code == 200
    assert await auth.db.fetchval('SELECT count(*) FROM staff_sessions') == 1
    await auth.logout(actors['alice'])
    assert (await client.get('/sql-api/whoami')).status_code == 401
    assert (await client.get('/auth/session')).status_code == 401


@pytest.mark.parametrize('backend', ['postgres', 'sqlite'])
async def test_policy_catalog_filters_before_pagination_and_hides_content_and_counts(staff, tmp_path, backend):
    from app.core.embedder import Embedder
    from app.storage.vector_store import LocalStore, PgStore
    from app.tools.knowledge_base import KnowledgeBase

    auth, actors, _, _ = staff
    store = PgStore(auth.db) if backend == 'postgres' else LocalStore(str(tmp_path/'catalog.db'))
    kb = KnowledgeBase(store=store, embedder=Embedder(dim=128))
    try:
        for audience in ['employee', 'operations', 'admin']:
            await kb.add_documents_async([{'title': audience+' policy', 'content': audience+' private policy content',
                'policy': {'audience': audience, 'status': 'published', 'version': 'v1', 'effective_at': '2020-01-01T00:00:00Z'}}])
        full = await kb.list_documents(actor=actors['root'])
        ids = {d['title'].split()[0]: d['id'] for d in full['documents']}
        # Restricted docs are newer: post-pagination filtering would lose the employee row.
        page = await kb.list_documents(query='policy', limit=1, actor=actors['alice'])
        assert [d['id'] for d in page['documents']] == [ids['employee']]
        assert page['matched'] == page['total_documents'] == page['total_chunks'] == 1
        assert page['uncategorized'] + sum(c['documents'] for c in page['categories']) == 1
        assert (await kb.list_documents(query='private', limit=1, offset=1, actor=actors['alice']))['documents'] == []
        assert await kb.get_document(ids['admin'], actor=actors['alice']) is None
        assert await kb.get_document(ids['operations'], actor=actors['alice']) is None
        assert await kb.get_document(ids['admin'], actor=actors['bob']) is None
        own = await kb.get_document(ids['employee'], actor=actors['alice'])
        assert own['content'] == 'employee private policy content' and own['passages']
        assert (await kb.get_document(ids['operations'], actor=actors['bob']))['passages']
        assert (await kb.get_document(ids['admin'], actor=actors['root']))['passages']
        for actor, count in [('alice', 1), ('bob', 2), ('root', 3)]:
            assert (await kb.document_stats(actor=actors[actor]))['total_documents'] == count
            overview = await kb.category_overview(actor=actors[actor])
            assert overview['uncategorized'] + sum(c['documents'] for c in overview['categories']) == count
    finally:
        if backend == 'sqlite':
            await store.close()


async def test_legacy_status_write_rechecks_revoked_operations_at_commit(staff):
    from app.core.staff_auth import current_actor, current_auth
    from app.tickets.ticket_store import PgTicketStore

    auth, actors, _, _ = staff
    store = PgTicketStore(auth.db)
    ticket = await store.create('legacy', 'bob', 'synthetic handoff')
    actor_token, auth_token = current_actor.set(actors['bob']), current_auth.set(auth)
    try:
        await auth.change_account('bob', by=actors['root'], role='employee')
        with pytest.raises(HTTPException) as error:
            await store.update_status(ticket.id, 'resolved')
        assert error.value.status_code == 401
        assert (await store.get(ticket.id)).status == 'open'
    finally:
        current_actor.reset(actor_token)
        current_auth.reset(auth_token)


# ── 本机演示管理员（.env 里配账号密码）─────────────────────────────────────────

DEMO = {"AI_HELPDESK_DEMO_ADMIN_LOGIN": "admin", "AI_HELPDESK_DEMO_ADMIN_PASSWORD": "demo88"}


def test_demo_admin_is_off_unless_both_settings_are_present():
    from app.core.staff_auth import STRONG_MIN, demo_admin, min_password_length
    assert demo_admin({}) is None and min_password_length({}) == STRONG_MIN == 14
    for partial in ({"AI_HELPDESK_DEMO_ADMIN_LOGIN": "admin"}, {"AI_HELPDESK_DEMO_ADMIN_PASSWORD": "demo88"},
                    {"AI_HELPDESK_DEMO_ADMIN_LOGIN": " ", "AI_HELPDESK_DEMO_ADMIN_PASSWORD": "demo88"}):
        assert demo_admin(partial) is None and min_password_length(partial) == 14
    assert demo_admin(DEMO) == ("admin", "demo88") and min_password_length(DEMO) == 6


@pytest.mark.parametrize("env,problem", [
    ({"AI_HELPDESK_DEMO_ADMIN_LOGIN": "admin"}, "both"),
    ({"AI_HELPDESK_DEMO_ADMIN_PASSWORD": "demo88"}, "both"),
    ({**DEMO, "AI_HELPDESK_DEMO_ADMIN_LOGIN": "Admin!"}, "lowercase"),
    ({**DEMO, "AI_HELPDESK_DEMO_ADMIN_LOGIN": "ab"}, "lowercase"),
    ({**DEMO, "AI_HELPDESK_DEMO_ADMIN_PASSWORD": "12345"}, "6-128"),
    ({**DEMO, "AI_HELPDESK_DEMO_ADMIN_PASSWORD": "x" * 129}, "6-128"),
    ({**DEMO, "APP_ENV": "production"}, "production"),
    ({"AI_HELPDESK_DEMO_ADMIN_LOGIN": "admin", "APP_ENV": "production"}, "production"),
])
def test_demo_admin_settings_that_are_incomplete_invalid_or_in_production_stop_the_start(env, problem):
    from app.core.staff_auth import check_demo_admin
    with pytest.raises(ValueError, match=problem):
        check_demo_admin(env)
    with pytest.raises(ValueError):
        from app.core.staff_auth import check_config
        check_config({"AI_HELPDESK_INSTANCE": "staff", "POSTGRES_DSN": "postgresql://x/y", **env})


def test_a_valid_demo_admin_config_and_no_config_both_pass():
    from app.core.staff_auth import check_demo_admin
    check_demo_admin({})
    check_demo_admin(DEMO)


def test_the_password_minimum_is_14_by_default_and_6_only_in_demo_mode(monkeypatch):
    for name in DEMO:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(HTTPException) as strict:
        hash_password("demo88")
    assert strict.value.status_code == 422 and "14" in strict.value.detail
    for name, value in DEMO.items():
        monkeypatch.setenv(name, value)
    assert verify_password("demo88", hash_password("demo88"))
    with pytest.raises(HTTPException) as short:
        hash_password("demo8")
    assert short.value.status_code == 422 and "6" in short.value.detail


async def test_demo_admin_is_created_once_can_log_in_and_never_overwritten(staff, monkeypatch):
    auth, _, client, _ = staff
    for name, value in DEMO.items():
        monkeypatch.setenv(name, value)
    created = await auth.seed_demo_admin()
    assert created["login"] == "admin" and created["role"] == "admin"
    assert await auth.seed_demo_admin() is None  # a second start leaves it alone
    row = await auth.db.fetchrow("SELECT password_hash, role, instance FROM staff_accounts WHERE login='admin'")
    assert row["role"] == "admin" and row["instance"] == "staff" and "demo88" not in row["password_hash"]
    ok = await client.post("/auth/login", json={"login": "admin", "password": "demo88"}, headers={"Origin": auth.origin})
    assert ok.status_code == 200
    bad = await client.post("/auth/login", json={"login": "admin", "password": "demo89"}, headers={"Origin": auth.origin})
    assert bad.status_code == 401
    # the same login configured with another password does not replace the stored one
    monkeypatch.setenv("AI_HELPDESK_DEMO_ADMIN_PASSWORD", "other-password")
    assert await auth.seed_demo_admin() is None
    again = await client.post("/auth/login", json={"login": "admin", "password": "demo88"}, headers={"Origin": auth.origin})
    assert again.status_code == 200


async def test_the_seeded_demo_admin_can_create_a_short_password_employee_only_in_demo_mode(staff, monkeypatch):
    auth, _, client, _ = staff
    for name, value in DEMO.items():
        monkeypatch.setenv(name, value)
    await auth.seed_demo_admin()
    login_response = await client.post("/auth/login", json={"login": "admin", "password": "demo88"}, headers={"Origin": auth.origin})
    headers = {"Origin": auth.origin, "X-CSRF-Token": login_response.json()["csrf_token"]}
    created = await client.post("/admin/employees", headers=headers, json={"login": "zhang", "password": "zhang1", "role": "employee"})
    assert created.status_code == 201, created.text
    too_short = await client.post("/admin/employees", headers=headers, json={"login": "li", "password": "li123", "role": "employee"})
    assert too_short.status_code == 422
    for name in DEMO:
        monkeypatch.delenv(name)
    strict = await client.post("/admin/employees", headers=headers, json={"login": "wang", "password": "wang123", "role": "employee"})
    assert strict.status_code == 422 and "14" in strict.text
