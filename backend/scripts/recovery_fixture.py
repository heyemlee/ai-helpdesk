"""Synthetic data for an explicitly marked disposable recovery drill only."""
import asyncio
import io
import json
import os
import sys
import uuid

from PIL import Image

from app.attachments.objects import LocalObjects
from app.attachments.service import AttachmentService
from app.core.actor import ActorDirectory
from app.core.embedder import Embedder
from app.core.staff_auth import StaffAuth
from app.runs.store import PgRunStore
from app.storage.pg import Database
from app.storage.vector_store import PgStore
from app.tickets.cases import CaseInput, CaseService
from app.tools.knowledge_base import KnowledgeBase


async def main():
    if os.getenv('RECOVERY_DRILL') != '1':
        raise RuntimeError('Disposable recovery fixture only')
    options = json.load(sys.stdin)
    db = Database(os.environ['POSTGRES_DSN'])
    await db.connect(256)
    auth = StaffAuth(db, 'staff')
    employee = await auth.create_account('recovery-employee', options['password'], 'employee', bootstrap=True)
    admin = await auth.create_account('recovery-admin', options['password'], 'admin', bootstrap=True)
    actor = await auth.provider(employee['actor_id'], 'staff')
    directory = ActorDirectory(auth.provider)
    attachments = AttachmentService(db, LocalObjects(os.environ['AI_HELPDESK_ATTACHMENT_DIR']), directory, 'staff')
    cases = CaseService(db, 'staff', actors=directory, identity_store=auth, attachments=attachments, attachment_resolver=attachments.resolve)
    first = None
    for i in range(12):
        conv = await auth.new_conversation(actor)
        ids = []
        if i % 2:
            image = io.BytesIO()
            Image.new('RGB', (800,600), color=(i*10,100,200)).save(image,format='PNG')
            a = await attachments.upload(actor, image.getvalue(), 'synthetic.png','image/png',uuid.uuid4().hex)
            ids = [a['id']]
        body = CaseInput(new_case=True, conv_id=conv, message_id=uuid.uuid4().hex, message='Synthetic recovery case',
                         facts={'order_ref':'RECOVERY-001'}, issue_type='order', attachment_ids=ids)
        receipt = await cases.accept_input(actor, body)
        run = await PgRunStore(db, cases.run_instance).claim(receipt['run_id'],'recovery-fixture',60)
        await cases.complete(run, {'response':'Synthetic draft for recovery verification', 'needs_clarification':True, 'handoff':False})
        if first is None and ids:
            first = receipt['case_id']
    kb = KnowledgeBase(store=PgStore(db), embedder=Embedder(dim=256))
    await kb.add_documents_async([{'title':'Recovery policy', 'content':'Synthetic policy. Verify current source before confirming.',
                                  'policy':{'status':'published','version':'fixture-v1','audience':'employee','effective_at':'2020-01-01T00:00:00Z'}}])
    previews = {r['id']:r['preview_sha256'] for r in await db.fetch('SELECT id,preview_sha256 FROM attachments')}
    print(json.dumps({'case_id':first,'attachment_previews':previews,'accounts':[{'actor_id':employee['actor_id'],'role':'employee','active':True},
                                                 {'actor_id':admin['actor_id'],'role':'admin','active':True}]}))
    await db.close()


if __name__ == '__main__':
    asyncio.run(main())
