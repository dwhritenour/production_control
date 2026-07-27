"""
Anvil Server Module - Routing Notifications Sender
===================================================
Drains the dbo.pc_notifications queue via Twilio (Routing process flow,
migration 007, 2026-07-24).

Design:
    - sp_pc_complete_step QUEUES notifications inside the completion
      transaction; THIS module (and only this module) SENDS them. A text
      failure can never roll back a step completion.
    - Recipients come from dbo.sms_notification_contacts (shared Transfer
      App infrastructure) using EXPLICIT routing tokens in
      notification_types:
          ROUTING_READY_<dept_code>  - texted when a step goes Ready at
                                       that department (B=SE Department,
                                       E=Inspection, ...)
          ROUTING_PC                 - Production Control oversight
                                       (step-complete + routing-complete)
      The Transfer App's 'NULL = all types' convention is deliberately
      NOT honored here, so Hub contacts never receive routing texts
      unless subscribed.
    - DIGEST (option 1, 2026-07-27): everything queued for the SAME
      token within one drain cycle goes out as ONE text. A lone row
      keeps its full original message; multiples collapse:
          dept:  "Ready for SE Department (4): WO-2026-0201 (YS12I0250), ..."
          PC:    "Routing updates (2):" + each original message on its
                 own line.
      The 1-minute task cadence IS the batching window. Queue rows stay
      one-per-event (audit unchanged); only the SMS is merged.
    - Retry: a failed row stays Queued until 3 attempts, then Failed.
      The queue table is the audit trail.

Scheduled task (Dave, in the IDE): task_name 'send_routing_notifications',
every 1 minute (5 if the plan disallows 1).

Requires PC app secrets: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
TWILIO_PHONE_NUMBER (copy from Transfer App).

Author: Dave Whritenour / Claude AI Assistant
Project: Titan Flow Control - Year of Automation
Created: July 2026
"""

import anvil.server
from datetime import datetime, date
from decimal import Decimal

from . import db_config
from .sms_notifications import TwilioSMSClient

MAX_ATTEMPTS = 3
BATCH_SIZE = 50


def get_connection():
  return db_config.get_nexus_connection()


def _recipients_for_token(cursor, token, cache):
  """Active contacts explicitly subscribed to a routing token."""
  if token in cache:
    return cache[token]
  cursor.execute("""
      SELECT contact_name, phone_number
      FROM dbo.sms_notification_contacts
      WHERE is_active = 1
        AND notification_types LIKE %s
      ORDER BY contact_name
  """, (f'%{token}%',))
  cache[token] = [{'name': r['contact_name'],
                   'phone': str(r['phone_number']).strip()}
                  for r in cursor.fetchall()]
  return cache[token]


def _digest_message(token, dept_name, rows):
  """One SMS per token per drain cycle. A single row keeps its original
  message; multiple rows collapse into a digest (option 1, 2026-07-27)."""
  if len(rows) == 1:
    return rows[0]['message']
  if token == 'ROUTING_PC':
    return (f"Routing updates ({len(rows)}):\n"
            + '\n'.join(r['message'] for r in rows))
  # Department digest: compact WO list - the kiosk board has the detail.
  items = []
  for r in sorted(rows, key=lambda x: x.get('work_order_number') or ''):
    wn = r.get('work_order_number') or f"wo#{r['work_order_id']}"
    pn = r.get('part_number') or ''
    items.append(f"{wn} ({pn})" if pn else wn)
  shown = items[:15]
  if len(items) > 15:
    shown.append(f"+{len(items) - 15} more")
  return (f"Ready for {dept_name or 'your department'} ({len(rows)}): "
          + ', '.join(shown))


def _mark_rows(cursor, ids, sent, err=None):
  """Bulk status update for one digest group (all rows share the SMS fate)."""
  ph = ','.join(['%s'] * len(ids))
  if sent:
    cursor.execute(f"""
        UPDATE dbo.pc_notifications
        SET status = 'Sent', sent_at = GETDATE(), last_error = %s
        WHERE notification_id IN ({ph})
    """, tuple([err] + ids))
  else:
    cursor.execute(f"""
        UPDATE dbo.pc_notifications
        SET attempts = attempts + 1,
            last_error = %s,
            status = CASE WHEN attempts + 1 >= {MAX_ATTEMPTS}
                          THEN 'Failed' ELSE 'Queued' END
        WHERE notification_id IN ({ph})
    """, tuple([err or 'send failed'] + ids))


def _drain_queue():
  """Send queued routing notifications, one digest text per recipient
  token per cycle. Returns a summary dict."""
  conn = get_connection()
  summary = {'picked_up': 0, 'sent': 0, 'failed': 0,
             'no_recipients': 0, 'texts_sent': 0}
  try:
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT TOP {BATCH_SIZE}
               n.notification_id, n.work_order_id, n.notify_type,
               n.message, n.attempts,
               d.dept_code, d.department_name,
               wo.work_order_number, wo.part_number
        FROM dbo.pc_notifications n
        LEFT JOIN dbo.pc_departments d
          ON d.department_id = n.department_id
        LEFT JOIN dbo.work_orders wo
          ON wo.work_order_id = n.work_order_id
        WHERE n.status = 'Queued' AND n.attempts < {MAX_ATTEMPTS}
        ORDER BY n.created_at
    """)
    rows = cursor.fetchall()
    summary['picked_up'] = len(rows)
    if not rows:
      return summary

    try:
      client = TwilioSMSClient()
    except Exception as e:
      # Twilio misconfigured (missing secrets?): count an attempt so a
      # permanent config problem eventually parks rows as Failed instead
      # of retrying forever.
      cursor.execute(f"""
          UPDATE dbo.pc_notifications
          SET attempts = attempts + 1,
              last_error = %s,
              status = CASE WHEN attempts + 1 >= {MAX_ATTEMPTS}
                            THEN 'Failed' ELSE 'Queued' END
          WHERE status = 'Queued' AND attempts < {MAX_ATTEMPTS}
      """, (f'Twilio init failed: {e}'[:300],))
      conn.commit()
      summary['failed'] = len(rows)
      return summary

    # Group this cycle's rows by recipient token (insertion-ordered).
    groups = {}
    for row in rows:
      if row['notify_type'].startswith('PC_'):
        token = 'ROUTING_PC'
      else:
        token = f"ROUTING_READY_{(row['dept_code'] or '?')}"
      groups.setdefault(token, []).append(row)

    token_cache = {}
    for token, group in groups.items():
      ids = [r['notification_id'] for r in group]

      recips = _recipients_for_token(cursor, token, token_cache)
      if not recips:
        _mark_rows(cursor, ids, sent=False,
                   err=f'No active recipients for {token}')
        summary['no_recipients'] += len(group)
        continue

      message = _digest_message(token, group[0].get('department_name'), group)

      sent_any, last_err = False, None
      for r in recips:
        try:
          res = client.send_sms(r['phone'], message)
          if res.get('success'):
            sent_any = True
          else:
            last_err = str(res.get('error') or res)[:300]
        except Exception as e:
          last_err = str(e)[:300]

      _mark_rows(cursor, ids, sent=sent_any, err=last_err)
      if sent_any:
        summary['sent'] += len(group)
        summary['texts_sent'] += 1
      else:
        summary['failed'] += len(group)

    conn.commit()
    return summary
  finally:
    conn.close()


@anvil.server.background_task
def send_routing_notifications():
  """Scheduled task entry point (every minute)."""
  summary = _drain_queue()
  if summary['picked_up']:
    print(f"routing notifications: {summary}")
  return summary


@anvil.server.callable
def send_routing_notifications_now():
  """Manual trigger for testing from the IDE console / a debug button."""
  return _drain_queue()


@anvil.server.callable
def get_routing_notification_log(limit=50):
  """Recent queue rows for a future PC oversight panel."""
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT TOP {int(limit)}
               n.notification_id, n.work_order_id, n.notify_type,
               n.department_id, n.message, n.status, n.attempts,
               n.created_at, n.sent_at, n.last_error
        FROM dbo.pc_notifications n
        ORDER BY n.created_at DESC
    """)
    rows = cursor.fetchall()
    out = []
    for r in rows:
      out.append({k: (v.isoformat() if isinstance(v, (datetime, date))
                      else float(v) if isinstance(v, Decimal) else v)
                  for k, v in r.items()})
    return out
  finally:
    conn.close()
