"""Conservative employee maintenance: only expired sessions and unlinked uploads."""
import asyncio
import json
import logging
import os
import time
from pathlib import Path

from prometheus_client import Counter, Gauge

log = logging.getLogger(__name__)
AUTH_EVENTS = Counter('helpdesk_staff_auth_events_total', 'Employee authentication/lifecycle audit events', ['action'])
CLEANUP_LAST = Gauge('helpdesk_attachment_cleanup_last_success_timestamp_seconds', 'Last successful unlinked-upload cleanup')
OPERATIONS_CONFIGURED = Gauge('helpdesk_operations_monitoring_configured', 'Whether backup reports are mounted')
BACKUP_LAST = Gauge('aihd_backup_last_success_timestamp_seconds', 'Last completed encrypted backup')
VERIFY_LAST = Gauge('aihd_restore_verification_last_success_timestamp_seconds', 'Last isolated recovery verification')


def operation_metrics():
    directory = os.getenv('AI_HELPDESK_OPERATIONS_REPORT_DIR', '')
    OPERATIONS_CONFIGURED.set(bool(directory))
    if not directory:
        return
    for name, field, gauge in [('last-backup.json','completed_at',BACKUP_LAST), ('last-verification.json','verified_at',VERIFY_LAST)]:
        try:
            gauge.set(float(json.loads((Path(directory)/name).read_text())[field]))
        except (OSError, ValueError, KeyError, TypeError):
            gauge.set(0)


async def run(auth, attachments):
    await asyncio.sleep(60)
    while True:
        try:
            await auth.db.execute('DELETE FROM staff_sessions WHERE expires_at<now() OR last_seen<now()-$1*interval \'1 second\'', auth.idle_s)
            await auth.db.execute("DELETE FROM staff_login_limits WHERE window_start<floor(extract(epoch FROM now())/60)-60")
            if attachments:
                await attachments.cleanup(older_than_s=86400)
                CLEANUP_LAST.set(time.time())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.warning('Employee maintenance failed: %s', type(error).__name__)
        await asyncio.sleep(3600)
