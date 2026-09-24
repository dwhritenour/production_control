"""
perf_probe - server-side instrumentation for the Production Control app
==========================================================================
Answers "where does the time go?" for every decorated server callable:

    wall_ms        whole callable, as the client experiences the server side
    connect_ms     opening the Nexus connection (pymssql, cloud -> Nexus box)
    statements     how many cursor.execute() round trips the call made
    sql_ms         time inside execute() + fetch (network + SQL Server)
    exec_ms        the execute() part: query planning + SQL Server work +
                   the first packet back  (1.1.0)
    fetch_ms       the fetch part: the result set travelling Nexus -> Anvil.
                   Big fetch_ms with modest exec_ms = payload, not the query.
    rows_returned  size of the largest list in the result
    payload_bytes  JSON size of the result (what Anvil has to ship to the
                   client, roughly)
    process_age_s  seconds since this server process started - a call with
                   age < 5 s was served by a COLD process (Anvil spun a new
                   Python up for it); process_id changing = new process

Wiring (two edits per module, nothing else changes):
    from . import perf_probe                 # top of the module
    @anvil.server.callable
    @perf_probe.timed                        # directly under the callable line
    def get_work_orders(...):

How it counts statements without touching the callables: it wraps
db_config.get_nexus_connection at import so every connection handed out
while a timed call is running reports into that call's context (thread-
local, so concurrent calls on a persistent server don't mix). Outside a
timed call the connection is passed through untouched.

Where the numbers go:
    1. dbo.pc_perf_log if it exists (migration 087) - one row per call,
       written on a separate connection AFTER the timed call, never inside
       its transaction. This is the day-of-use dataset.
    2. A ring buffer of the last 500 calls in this process (get_perf_log).
    3. One JSON line in the Anvil server log (print) as a fallback.
    Failures to record are swallowed - the probe must never break a call.

Remove: delete the two edits per module and this module. No other trace.

Author: Claude (Dave Whritenour review) | 2026-09-24
"""

__version__       = "1.1.0"
__version_date__  = "2026-09-24"
__version_notes__ = "sql_ms split into exec_ms (SQL Server working) + fetch_ms (rows coming down the wire)"

import anvil.server
import collections
import functools
import json
import threading
import time
import uuid
from datetime import datetime

from . import db_config

_PROCESS_ID = uuid.uuid4().hex[:8]
_PROCESS_STARTED = time.time()
_local = threading.local()
_RING = collections.deque(maxlen=500)
_TABLE = {'checked': False, 'exists': False, 'split_cols': False}
_ARGS_MAX = 400


# =============================================================================
# Connection / cursor proxies (only active inside a timed call)
# =============================================================================

class _CursorProxy(object):
  def __init__(self, cursor, ctx):
    self._c = cursor
    self._ctx = ctx

  def execute(self, *a, **k):
    t0 = time.perf_counter()
    try:
      return self._c.execute(*a, **k)
    finally:
      dt = (time.perf_counter() - t0) * 1000.0
      self._ctx['statements'] += 1
      self._ctx['sql_ms'] += dt
      self._ctx['exec_ms'] += dt

  def executemany(self, *a, **k):
    t0 = time.perf_counter()
    try:
      return self._c.executemany(*a, **k)
    finally:
      dt = (time.perf_counter() - t0) * 1000.0
      self._ctx['statements'] += 1
      self._ctx['sql_ms'] += dt
      self._ctx['exec_ms'] += dt

  def callproc(self, *a, **k):
    t0 = time.perf_counter()
    try:
      return self._c.callproc(*a, **k)
    finally:
      dt = (time.perf_counter() - t0) * 1000.0
      self._ctx['statements'] += 1
      self._ctx['sql_ms'] += dt
      self._ctx['exec_ms'] += dt

  def fetchall(self):
    t0 = time.perf_counter()
    try:
      rows = self._c.fetchall()
      self._ctx['rows_fetched'] += len(rows) if rows else 0
      return rows
    finally:
      dt = (time.perf_counter() - t0) * 1000.0
      self._ctx['sql_ms'] += dt
      self._ctx['fetch_ms'] += dt

  def fetchone(self):
    t0 = time.perf_counter()
    try:
      row = self._c.fetchone()
      if row is not None:
        self._ctx['rows_fetched'] += 1
      return row
    finally:
      dt = (time.perf_counter() - t0) * 1000.0
      self._ctx['sql_ms'] += dt
      self._ctx['fetch_ms'] += dt

  def __getattr__(self, name):
    return getattr(self._c, name)


class _ConnProxy(object):
  def __init__(self, conn, ctx):
    self._conn = conn
    self._ctx = ctx

  def cursor(self, *a, **k):
    return _CursorProxy(self._conn.cursor(*a, **k), self._ctx)

  def __getattr__(self, name):
    return getattr(self._conn, name)


_orig_get_nexus_connection = db_config.get_nexus_connection


def _counting_get_nexus_connection(*a, **k):
  ctx = getattr(_local, 'ctx', None)
  if ctx is None:
    return _orig_get_nexus_connection(*a, **k)
  t0 = time.perf_counter()
  conn = _orig_get_nexus_connection(*a, **k)
  ctx['connect_ms'] += (time.perf_counter() - t0) * 1000.0
  ctx['connections'] += 1
  return _ConnProxy(conn, ctx)


db_config.get_nexus_connection = _counting_get_nexus_connection


# =============================================================================
# Recording
# =============================================================================

def _payload_size(result):
  """(rows_returned, payload_bytes) - rows = largest list in the result."""
  rows = 0
  try:
    if isinstance(result, dict):
      for v in result.values():
        if isinstance(v, (list, tuple)):
          rows = max(rows, len(v))
    elif isinstance(result, (list, tuple)):
      rows = len(result)
  except Exception:
    pass
  try:
    size = len(json.dumps(result, default=str))
  except Exception:
    size = -1
  return rows, size


def _args_summary(args, kwargs):
  try:
    parts = [repr(a) for a in args] + [f"{k}={v!r}" for k, v in kwargs.items()]
    s = ", ".join(parts)
    return s[:_ARGS_MAX]
  except Exception:
    return ''


def _user_email():
  try:
    import anvil.users
    u = anvil.users.get_user()
    return (u['email'] if u else None)
  except Exception:
    return None


def _table_exists(cursor):
  if not _TABLE['checked']:
    try:
      cursor.execute("SELECT OBJECT_ID('dbo.pc_perf_log', 'U') AS o, "
                     "COL_LENGTH('dbo.pc_perf_log', 'exec_ms') AS e")
      r = cursor.fetchone()
      _TABLE['exists'] = bool(r and r.get('o') is not None)
      _TABLE['split_cols'] = bool(r and r.get('e') is not None)   # migration 088
    except Exception:
      _TABLE['exists'] = False
    _TABLE['checked'] = True
  return _TABLE['exists']


def _record(rec):
  """Ring buffer + server log always; pc_perf_log when the table exists.
  Uses the UNWRAPPED connection so the write is not counted or timed."""
  _RING.append(rec)
  try:
    print("PERF " + json.dumps(rec, default=str))
  except Exception:
    pass
  conn = None
  try:
    conn = _orig_get_nexus_connection()
    cursor = conn.cursor()
    if _table_exists(cursor):
      if _TABLE['split_cols']:
        cursor.execute("""
            INSERT INTO dbo.pc_perf_log
                (process_id, process_age_s, callable, wall_ms, connect_ms,
                 sql_ms, exec_ms, fetch_ms, statements, connections,
                 rows_fetched, rows_returned, payload_bytes, args,
                 user_email, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (rec['process_id'], rec['process_age_s'], rec['callable'],
              rec['wall_ms'], rec['connect_ms'], rec['sql_ms'],
              rec['exec_ms'], rec['fetch_ms'],
              rec['statements'], rec['connections'], rec['rows_fetched'],
              rec['rows_returned'], rec['payload_bytes'], rec['args'],
              rec['user_email'], rec['error']))
      else:
        cursor.execute("""
            INSERT INTO dbo.pc_perf_log
                (process_id, process_age_s, callable, wall_ms, connect_ms,
                 sql_ms, statements, connections, rows_fetched, rows_returned,
                 payload_bytes, args, user_email, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (rec['process_id'], rec['process_age_s'], rec['callable'],
              rec['wall_ms'], rec['connect_ms'], rec['sql_ms'],
              rec['statements'], rec['connections'], rec['rows_fetched'],
              rec['rows_returned'], rec['payload_bytes'], rec['args'],
              rec['user_email'], rec['error']))
      conn.commit()
  except Exception:
    pass
  finally:
    try:
      if conn is not None:
        conn.close()
    except Exception:
      pass


# =============================================================================
# The decorator
# =============================================================================

def timed(fn):
  """Place DIRECTLY under @anvil.server.callable so the registered function
  is the wrapped one. Re-entrant: a timed callable calling another timed
  function keeps ONE context (the outer call owns the numbers)."""
  @functools.wraps(fn)
  def wrapper(*args, **kwargs):
    outer = getattr(_local, 'ctx', None)
    if outer is not None:
      return fn(*args, **kwargs)          # nested: outer call is measuring
    ctx = {'statements': 0, 'sql_ms': 0.0, 'exec_ms': 0.0, 'fetch_ms': 0.0,
           'connect_ms': 0.0, 'connections': 0, 'rows_fetched': 0}
    _local.ctx = ctx
    t0 = time.perf_counter()
    result = None
    error = None
    try:
      result = fn(*args, **kwargs)
      return result
    except Exception as e:                # record, then re-raise unchanged
      error = f"{type(e).__name__}: {e}"[:400]
      raise
    finally:
      wall_ms = (time.perf_counter() - t0) * 1000.0
      _local.ctx = None
      try:
        rows, size = _payload_size(result) if error is None else (0, 0)
        rec = {
          'logged_at': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
          'process_id': _PROCESS_ID,
          'process_age_s': int(time.time() - _PROCESS_STARTED),
          'callable': fn.__name__,
          'wall_ms': int(wall_ms),
          'connect_ms': int(ctx['connect_ms']),
          'sql_ms': int(ctx['sql_ms']),
          'exec_ms': int(ctx['exec_ms']),
          'fetch_ms': int(ctx['fetch_ms']),
          'statements': ctx['statements'],
          'connections': ctx['connections'],
          'rows_fetched': ctx['rows_fetched'],
          'rows_returned': rows,
          'payload_bytes': size,
          'args': _args_summary(args, kwargs),
          'user_email': _user_email(),
          'error': error,
        }
        _record(rec)
      except Exception:
        pass
  return wrapper


# =============================================================================
# Read-back
# =============================================================================

@anvil.server.callable
def get_perf_log(limit=200):
  """Last `limit` records from THIS process's ring buffer (newest first).
  A fresh process = empty buffer = you just observed a cold start."""
  items = list(_RING)[-int(limit):]
  items.reverse()
  return {'process_id': _PROCESS_ID,
          'process_age_s': int(time.time() - _PROCESS_STARTED),
          'records': items}


@anvil.server.callable
def get_perf_summary(days=1):
  """Per-callable aggregates from dbo.pc_perf_log (falls back to the ring
  buffer when the table is absent)."""
  conn = None
  try:
    conn = _orig_get_nexus_connection()
    cursor = conn.cursor()
    if _table_exists(cursor) and _TABLE['split_cols']:
      cursor.execute("""
          SELECT callable,
                 COUNT(*)                                   AS calls,
                 AVG(wall_ms)                               AS avg_wall_ms,
                 MAX(wall_ms)                               AS max_wall_ms,
                 AVG(connect_ms)                            AS avg_connect_ms,
                 AVG(sql_ms)                                AS avg_sql_ms,
                 AVG(exec_ms)                               AS avg_exec_ms,
                 AVG(fetch_ms)                              AS avg_fetch_ms,
                 AVG(CAST(statements AS float))             AS avg_statements,
                 AVG(CAST(rows_returned AS float))          AS avg_rows,
                 AVG(CAST(payload_bytes AS float)) / 1024.0 AS avg_payload_kb,
                 MAX(payload_bytes) / 1024                  AS max_payload_kb,
                 SUM(CASE WHEN process_age_s < 5 THEN 1 ELSE 0 END) AS cold_calls,
                 COUNT(DISTINCT process_id)                 AS processes_seen,
                 SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors
          FROM dbo.pc_perf_log
          WHERE logged_at >= DATEADD(DAY, -%s, SYSDATETIME())
          GROUP BY callable
          ORDER BY SUM(wall_ms) DESC
      """, (int(days),))
      rows = [dict(r) for r in cursor.fetchall()]
      return {'status': 'success', 'source': 'pc_perf_log', 'rows': rows}
  except Exception as e:
    return {'status': 'error', 'message': str(e)}
  finally:
    try:
      if conn is not None:
        conn.close()
    except Exception:
      pass
  # Fallback: aggregate the ring buffer
  agg = {}
  for r in _RING:
    a = agg.setdefault(r['callable'], {'callable': r['callable'], 'calls': 0,
                                       'wall_ms': 0, 'sql_ms': 0,
                                       'connect_ms': 0, 'statements': 0,
                                       'payload_bytes': 0, 'cold_calls': 0})
    a['calls'] += 1
    a['wall_ms'] += r['wall_ms']
    a['sql_ms'] += r['sql_ms']
    a['connect_ms'] += r['connect_ms']
    a['statements'] += r['statements']
    a['payload_bytes'] += max(r['payload_bytes'], 0)
    a['cold_calls'] += 1 if r['process_age_s'] < 5 else 0
  rows = []
  for a in agg.values():
    n = a['calls'] or 1
    rows.append({'callable': a['callable'], 'calls': a['calls'],
                 'avg_wall_ms': a['wall_ms'] // n, 'avg_sql_ms': a['sql_ms'] // n,
                 'avg_connect_ms': a['connect_ms'] // n,
                 'avg_statements': a['statements'] / n,
                 'avg_payload_kb': a['payload_bytes'] / n / 1024.0,
                 'cold_calls': a['cold_calls']})
  rows.sort(key=lambda r: -(r['avg_wall_ms'] * r['calls']))
  return {'status': 'success', 'source': 'ring', 'rows': rows}