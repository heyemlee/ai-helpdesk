"""Read-only employee/ownership/security smoke. Credentials enter through stdin."""
import json
import sys

import httpx


def main():
    options = json.load(sys.stdin)
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
        print(json.dumps({'passed':True,'attachment_downloads_verified':downloaded,'checks':['employee_login','admin_denied','sql_admin_denied','legacy_bypass_denied','history_owner','case_facts','private_attachment_hashes']}))


if __name__ == '__main__':
    main()
