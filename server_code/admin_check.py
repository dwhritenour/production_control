"""
admin_check.py - Anvil Server Module
=====================================
Simple admin gating helper. Used by forms that expose write operations
(e.g. "Generate Forecast") to hide or guard actions for non-admins.

Phase 1 approach: email allowlist stored in an Anvil Secret named
FORECAST_ADMIN_EMAILS (comma-separated). Anyone not in the list is a
viewer (read-only).

Future (Phase 2+): replace with Anvil Users service + roles table when
you're ready to formalize auth.

Required Secret:
    FORECAST_ADMIN_EMAILS   e.g. 'dwhritenour@outlook.com,sarah@example.com'
"""

import anvil.server
import anvil.secrets
import anvil.users


def _get_admin_emails():
  """Parse the FORECAST_ADMIN_EMAILS secret into a lowercase set."""
  try:
    raw = anvil.secrets.get_secret('FORECAST_ADMIN_EMAILS') or ''
  except Exception:
    raw = ''
  return {e.strip().lower() for e in raw.split(',') if e.strip()}


def _current_user_email():
  """Best-effort: return the current Anvil user's email, or None."""
  try:
    user = anvil.users.get_user()
    if user and 'email' in user:
      return (user['email'] or '').strip().lower()
  except Exception:
    pass
  return None


@anvil.server.callable
def is_forecast_admin():
  """
    Returns True if the current user is on the admin allowlist.
    If no user is logged in (Anvil Users service not enabled), returns True
    as a permissive fallback — tighten this once you add auth.
    """
  admins = _get_admin_emails()
  if not admins:
    # No admin list configured → allow everyone (dev / single-user mode)
    return True

  email = _current_user_email()
  if email is None:
    # No auth yet → fall back to permissive (single-user use case).
    # Change this to `return False` once you enable Anvil Users service.
    return True

  return email in admins


def require_admin():
  """
    Server-side guard. Raises anvil.server.PermissionDenied if not admin.
    Call this at the top of any @anvil.server.callable that modifies data.
    """
  if not is_forecast_admin():
    raise anvil.server.PermissionDenied(
      'You do not have permission to perform this action. '
      'Contact an administrator if you need access.'
    )
