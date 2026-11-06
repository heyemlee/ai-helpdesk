"""Read-only employee/ownership/security smoke. Credentials enter through stdin."""
import json
import os
import sys
from pathlib import Path

import httpx

SESSION = Path('/tmp/helpdesk-release-probe-session.json')


def sql_gateway(options):
    """Use the employee session already checked before SQL/worker startup."""
    saved = json.loads(SESSION.read_text())
    try:
        with httpx.Client(base_url='http://frontend', headers={
            'Host': httpx.URL(options['origin']).host, 'Origin': options['origin'],
            'Cookie': saved['cookie'], 'X-Employee-Session': saved['identity'], 'X-Console-Request': '1',
        }, timeout=15) as client:
            response = client.get('/sql-api/whoami')
            identity = response.json().get('identity', {}) if response.status_code == 200 else {}
            passed = response.status_code == 200 and identity.get('user_id') == saved['actor_id'] and identity.get('is_admin') is False and identity.get('can_impersonate') is False
            print(json.dumps({'passed': passed, 'status_code': response.status_code, 'check': 'employee_session_through_sql_gateway'}))
    finally:
        SESSION.unlink(missing_ok=True)


def main():
    options = json.load(sys.stdin)
    if '--sql' in sys.argv:
        return sql_gateway(options)
    origin = options['origin']
    with httpx.Client(base_url='http://frontend', headers={'Host': httpx.URL(origin).host, 'Origin': origin}, timeout=30) as client:
        assert client.get('/api/auth/session').status_code == 401
        assert client.get('/sql-api/admin/model', headers={'X-Console-Request':'1','X-API-Key':'forged'}).status_code == 401
        response = client.post('/api/auth/login', json={'login':options['login'], 'password':options['password']})
        assert response.status_code == 200
        session = response.json()
        assert session['actor']['roles'] == ['employee']
        # Probe inside the closed private network. HTTPS termination is separately
        # verified at the actual public ingress; never weaken the Secure cookie.
        client.headers['Cookie'] = response.headers['set-cookie'].split(';',1)[0]
        client.headers['X-CSRF-Token'] = session['csrf_token']
        assert client.get('/api/admin/employees').status_code == 403
        assert client.put('/api/admin/model/chat', json={}).status_code == 403
        assert client.put('/sql-api/admin/model', json={}, headers={'X-Console-Request':'1'}).status_code == 403
        assert client.get('/api/conversations/unowned-smoke-id').status_code == 404
        cases = client.get('/api/tickets/cases')
        assert cases.status_code == 200
        expected = options.get('case_id')
        if not expected:
            raise ValueError('A known restored Case is required for business validation')
        response = client.get(f'/api/tickets/{expected}/case')
        assert response.status_code == 200
        case = response.json()
        assert case['messages'] and case['id'] == expected
        for key, value in options.get('facts', {}).items():
            assert case['facts'][key]['value'] == value
        assert case.get('materials'), 'Business smoke requires a real private attachment'
        downloaded = 0
        for material in case['materials']:
            import hashlib
            obj = client.get('/api/attachments/'+material['attachment_id']+'/content')
            assert obj.status_code == 200 and hashlib.sha256(obj.content).hexdigest() == material['sha256']
            preview = client.get('/api/attachments/'+material['attachment_id']+'/content?preview=true')
            assert preview.status_code == 200 and hashlib.sha256(preview.content).hexdigest() == options['attachment_previews'][material['attachment_id']]
            downloaded += 2
        # Transient credential stays inside the closed API container and is
        # consumed/unlinked by the next gateway check; never written to stdout.
        saved = {'cookie': client.headers['Cookie'], 'actor_id': session['actor']['actor_id'],
                 'identity': session['actor']['actor_id'] + ':' + str(session['actor']['auth_version'])}
        with os.fdopen(os.open(SESSION, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), 'w') as file:
            os.fchmod(file.fileno(), 0o600)
            json.dump(saved, file)
        print(json.dumps({'passed':True,'attachment_downloads_verified':downloaded,'checks':['employee_login','admin_denied','sql_admin_denied','legacy_bypass_denied','history_owner','case_facts','private_attachment_hashes']}))


if __name__ == '__main__':
    main()
