"""
Transfer AI Review - Anvil Background Task
==========================================
Ported from transfer_ai_review.py for Anvil scheduled execution.
Uses centralized db_config for database and API connections.

RECOMMENDATION STATUS LIFECYCLE
===============================
Status Flow (v5.7, 2026-04-22):
  Pending → Approved/Rejected/Paused → Applied → Trip_Assigned → Finalized → Delivered
                      ↓
                 Paused → Approved/Rejected (when resumed)
                      ↓
  Pending/Paused → Obsolete (when SKU no longer needs transfer)

Status Definitions:
  - Pending:       AI created recommendation, awaiting human review
  - Auto-Approved: CRITICAL/HIGH urgency, auto-approved by system
  - Approved:      Human approved the recommendation
  - Rejected:      Human rejected the recommendation (terminal)
  - Paused:        Human needs time to deliberate - held for future decision
  - Obsolete:      Conditions changed - SKU no longer in view (terminal)
  - Applied:       Approved recommendation ready for truck optimizer
  - Trip_Assigned: Assigned to a trip (batch still Proposed)
  - Finalized:     Batch finalized by Hub Manager; cascade from
                   transfer_trip_assignments.status='Finalized'. Once here,
                   the Hub Manager manually enters the transfer order into
                   e21 (workflow agreed 2026-04-21), and the view's
                   PendingTransferQty CTE stops counting this rec as virtual
                   inventory (doc Section 9) because e21 now carries the
                   Hub depletion in hub.available.
  - Delivered:     (future) Driver confirmed physical delivery at Starlite
                   via mobile form. Cascade from transfer_trips or a
                   trip_deliveries record. The "Received" name is
                   intentionally reserved for a later e21-integrated
                   Starlite receiving process.

  Note: Shipped and Received states from earlier drafts were dropped once
  the team decided e21 integration is not happening in the near term.

NON-STOCKED ITEM HANDLING (v5.2)
================================
Non-stocked items (min_stock=0, max_stock=0) receive different AI treatment:
  - Transfer ONLY the exact shortfall amount (no buffer stock)
  - These items are not regularly stocked at Starlite
  - They only appear due to specific sales order demand
  - Goal: Fulfill orders, don't accumulate excess inventory

EFFECTIVE_ON_HAND LOGIC (v5.0)
==============================
The view now provides effective_on_hand which accounts for:
  - starlite_on_hand: Physical inventory at Starlite
  - e21_in_transit_qty: Open transfer orders in e21 (hub_transfer_orders)
  - fresh_approved_qty: AI recommendations approved < 1 hour ago (not yet in e21)

This prevents duplicate transfer recommendations for SKUs that already have
inventory in transit or recently approved.

v5.3 CHANGE: Now storing e21_in_transit_qty, fresh_approved_qty, and effective_on_hand
in the ai_transfer_recommendations table for audit trail and debugging.

v5.4 CHANGE (exclusion filter removal):
  get_transfer_candidates() no longer excludes SKUs by status. The view
  (vw_unified_transfer_detection) is trusted as the single source of truth:
  its FreshApprovedRecommendations CTE covers the 1-hour fresh window for
  Auto-Approved/Approved/Applied/Trip_Assigned recs, and e21_in_transit_qty
  covers transfers already visible in the e21 export. This lets incremental
  demand (new demand after an in-pipeline rec) surface to the Hub Manager.

v5.5 CHANGE (duplicate-detection warning):
  Between hour 1 (fresh window expires) and the moment e21 is refreshed,
  Applied/Trip_Assigned transfers are briefly invisible to the view. If new
  demand arrives in that gap, the view will recommend a transfer that could
  be a real incremental need OR a duplicate of one already en route.

  This version introduces check_duplicate_warnings_batch(conn, candidates):
    - Looks back 48 hours for Applied/Trip_Assigned recs per candidate SKU
      whose reviewed_date is older than 1 hour (past the fresh window).
    - Sums their transfer qty.
    - If that sum exceeds the candidate's current e21_in_transit_qty,
      flags the candidate as has_duplicate_warning = True and attaches a
      duplicate_warning_text describing the latest suspected rec (ID, qty,
      reviewed_date, status).

  The warning is then:
    (a) prepended to reasoning (so it shows at the TOP of the TextArea)
    (b) stored in the has_duplicate_warning BIT column for UI highlighting
        and filtering (see alter_ai_transfer_recommendations_add_has_duplicate_warning.sql)

  The LEARNING POINT append previously tacked onto reasoning has been
  removed to avoid overcrowding the rp_transfer panel now that the
  duplicate warning lives there.

UPDATE vs INSERT LOGIC
======================
For SKUs with existing Pending or Paused recommendations:
  - UPDATE the existing record with fresh inventory data
  - Re-run AI analysis with current state
  - Preserve the recommendation_id for audit trail continuity
This ensures the Hub Manager always sees current data when reviewing.
"""

# =============================================================================
# Version:       5.13.1
# Last updated:  2026-05-12
# Changelog (this module only):
#   5.13.1 (2026-05-12) * Extended _check_view_data_freshness to also
#                        catch the STALE-BUT-NON-EMPTY failure mode that
#                        v5.13.0 missed. Today's incident: EasyMorph
#                        crashed mid-day, sales_orders_due was still
#                        non-empty (2,666 rows from 7:19 AM EDT), the
#                        empty check passed, and the cleanup sweep ran
#                        against ~8-hour-old data — obsoleting 9 active
#                        recommendations with a misleading
#                        "Demand_Resolved" reason code. Caller had to
#                        manually rerun EasyMorph + AI Review to
#                        recreate them.
#                      * Staleness check: read MAX(sales_orders_due
#                        .nx_last_updated) and skip the run if older than
#                        a threshold. Threshold sourced from
#                        global_parameters.sales_orders_due_freshness_minutes
#                        with a 60-minute fallback (4x the 15-min
#                        EasyMorph cycle — generous enough to absorb a
#                        single transient deadlock, tight enough that
#                        repeated failures are caught quickly).
#                      * Comparison done SQL-side via DATEDIFF + GETDATE()
#                        so Anvil-process tz and SQL Server tz cannot
#                        drift.
#                      * Same skip-and-log pattern as v5.13.0 (same
#                        action_type 'Transfer_Review_Skipped_Stale_Data');
#                        reason text now distinguishes "Empty: ..." vs
#                        "Stale (>N min): table=Mmin" so the SMS / audit
#                        log says exactly which mode tripped.
#                      * No deadlock fix here — companion change is
#                        WITH (NOLOCK) on the EasyMorph nonstock import
#                        (sql/em_sales_orders_due_nonstock.sql) plus an
#                        OUTER APPLY rewrite in v2 of that file. The
#                        weekend of 2026-05-16 will bring proper
#                        retry-on-deadlock in EasyMorph itself.
#   5.13.0 (2026-05-05) * Data-freshness gate at the top of
#                        run_transfer_review (immediately after the
#                        v5.11 business-hours gate). Checks that the
#                        two view-input tables are populated before
#                        running any destructive work:
#                          - dbo.sales_orders_due
#                          - dbo.inventory_current
#                        If either is empty (strict-zero threshold),
#                        the run short-circuits with one
#                        Transfer_Review_Skipped_Stale_Data row in
#                        ai_agent_log and the task_state status
#                        'Skipped - upstream data stale'. Auto-resumes
#                        on the next firing once the data is back.
#                      * Motivated by 2026-05-05 EasyMorph deadlock
#                        incident: the loader's "clear sales_orders_due
#                        then repopulate" pattern left the table empty
#                        when its Import-from-database step deadlocked.
#                        AI Review fired moments later, saw zero demand
#                        in the view, and obsoleted 15 Pending/Paused
#                        recs before the next batch recreated 14 of
#                        them. The gate prevents that cycle by refusing
#                        to run when the upstream data clearly hasn't
#                        finished loading.
#                      * Min/max-driven (Type 1) tables intentionally
#                        NOT gated: they refresh independently of the
#                        e21 loader and an empty min_max_calculated
#                        is a benign "no min/max recs needed today,"
#                        not a pipeline failure.
#                      * Companion EasyMorph fix (out of scope here):
#                        export-then-clear instead of clear-then-export
#                        so the live table is never empty during a
#                        successful run; the gate is the belt to that
#                        suspenders.
#   5.12.0 (2026-05-04) * mark_stale_recommendations_obsolete now runs a
#                        SECOND sweep after the existing not-in-view
#                        pass: catches Applied/Auto-Approved recs where
#                        the effective qty is 0 AND reviewed_date is
#                        older than 14 days. Marks them Obsolete with
#                        reason_code 'Stale_Zero_Qty_Approval'.
#                      * Motivation (2026-05-04 YS58D0800 incident): an
#                        "approved no-op" rec from 2026-04-02 (rec 404,
#                        ai_recommended_qty=0) sat at status='Applied'
#                        for 32 days, silently obsoleting three later
#                        fresh recs (666, 694, 731) for the same SKU
#                        via v5.9 dedupe — 60 units of nullified
#                        transfers. This sweep retires those landmines
#                        on a normal review-batch cycle. 14-day
#                        threshold is Dave-selected: long enough to
#                        give the Hub Manager room to change their
#                        mind, short enough that landmines don't
#                        accumulate.
#                      * Companion fix in transfer_truck_optimizer.py
#                        (v5.12 dedupe filter on qty>0). The two fixes
#                        defend against the same class of bug from
#                        different angles: dedupe ignores qty=0 recs,
#                        cleanup retires them so they can't accumulate.
#   5.11.1 (2026-04-28) * Removed stale 21-day default from
#                        build_ai_prompt(planning_horizon_days=21). The
#                        live value of
#                        global_parameters.transfer_planning_horizon_days
#                        has been 28 since 2026-04-22; the function-
#                        signature default was a dead literal that could
#                        only fire if a future caller forgot the
#                        argument. Argument is now required (no default).
#                        No behavior change today — the only caller
#                        always passes the live value via
#                        get_planning_horizon_days(conn).
#   5.11.0 (2026-04-24) * Business-hours gate on run_transfer_review. The
#                        Anvil scheduler has no start/stop window — it
#                        fires every 15 min 24x7 — which burns AI tokens
#                        on the 11 hours/day nobody is reviewing anyway.
#                        v5.11 adds a gate that short-circuits off-hours
#                        firings so they cost effectively nothing.
#                      * Window: 06:00 <= local_hour < 19:00 in
#                        America/New_York, seven days a week (no weekday
#                        gating per 2026-04-24 decision). DST-aware via
#                        zoneinfo with pytz fallback.
#                      * Logging is option-C (Dave-selected): log once on
#                        the transition INTO off-hours
#                        (Transfer_Review_Skipped_Off_Hours) and once on
#                        the transition BACK to on-hours
#                        (Transfer_Review_Resumed). Routine intra-window
#                        skips and intra-window runs are silent to keep
#                        ai_agent_log tidy. Prior-run state is inferred
#                        from the most recent log row of either
#                        action_type — no new table or column needed.
#                      * Added __version__ bump only — no other behavioral
#                        changes. v5.10's hub-side short-circuit, v5.8+
#                        version stamping, etc. all remain as-is.
#   5.10.0 (2026-04-24) * Hub-side short-circuit: in run_transfer_review's
#                        per-candidate loop, skip any SKU where the view
#                        reports hub_effective_on_hand <= 0. Prevents the
#                        generator from producing unfulfillable duplicate
#                        recs when in-pipeline transfers have already
#                        consumed the Hub's available supply. Writes one
#                        AI_REVIEW_SKIPPED_NO_HUB_SUPPLY row to
#                        ai_agent_log per skipped SKU (option-2 design,
#                        agreed with Dave 2026-04-23).
#                      * Closes the v5.6 asymmetry: v5.6 wired the
#                        effective-on-hand math into the view but never
#                        fed `hub_effective_on_hand` back into the
#                        generator's supply decisions. v5.6 landed on
#                        the Starlite side (demand coverage) but not the
#                        Hub side (supply cap).
#                      * First version-stamped release of
#                        transfer_ai_review.py — joins the v5.8+
#                        three-layer versioning scheme so the module
#                        now shows a real version in the System Admin
#                        grid instead of "unversioned".
#                      * Scope discipline: does NOT rewrite the existing
#                        `hub_available` cap sites (lines ~930, ~1088,
#                        ~1564) or the AI prompt text. Those stay on
#                        `hub_available` until a separate cleanup — the
#                        short-circuit makes them unreachable for the
#                        cases that matter.
# =============================================================================
__version__       = "5.13.1"
__version_date__  = "2026-05-12"
__version_notes__ = (
  "Data-freshness gate extended to catch stale-but-non-empty case: now "
  "skips when sales_orders_due is older than 60 min (configurable via "
  "global_parameters.sales_orders_due_freshness_minutes). Closes the "
  "loophole that obsoleted 9 active recs on 2026-05-12 when EasyMorph "
  "crashed but left the prior good snapshot in place. Empty-table and "
  "stale-data both share the existing Transfer_Review_Skipped_Stale_Data "
  "action_type; reason text distinguishes which mode tripped."
)

# v5.12: Threshold for the stale-zero-qty cleanup sweep.
# A qty=0 Applied rec older than this many days is considered an
# abandoned "approved no-op" decision and gets marked Obsolete on the
# next review batch. 14 days is Dave-selected (2026-05-04): long enough
# to allow second-guessing, short enough that landmines don't pile up.
# If you change this, also update the v5.12 changelog entry above.
_STALE_ZERO_QTY_DAYS = 14

# v5.13: Tables the data-freshness gate checks before each run.
# Strict-zero threshold (Dave-selected 2026-05-05): if any of these
# is empty, EasyMorph (or whatever loader feeds the view) is mid-load
# or has failed, and the view's "no SKU needs a transfer" answer is
# meaningless. Refusing to run prevents mark_stale from obsoleting
# every Pending/Paused rec under that bad signal.
#
# Scope deliberately narrow:
#   - sales_orders_due: Type 2 demand source. EasyMorph reload target.
#   - inventory_current: on-hand for both sites; loader target.
# NOT gated:
#   - min_max_calculated (Type 1): refreshed by a different pipeline;
#     empty here is a benign "no min/max recs today," not a failure.
#   - usage_data: historical trend; staleness doesn't immediately
#     corrupt mark_stale's not-in-view sweep.
_FRESHNESS_TABLES = (
  # (table_name, purpose, timestamp_column_for_staleness_check)
  # When timestamp_column is None, only the empty-row-count check applies.
  ('dbo.sales_orders_due',   'Type 2 demand source (e21 loader)',     'nx_last_updated'),
  ('dbo.inventory_current',  'On-hand inventory at Starlite + Hub',   None),
)


import anvil.server
import anvil.media
import anvil.email
from datetime import datetime, date
from decimal import Decimal
import json
import uuid
import csv
import io
import time

# Import centralized configuration
from . import db_config


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_connection():
  """Get database connection from centralized config."""
  return db_config.get_nexus_connection()


def make_serializable(value):
  if value is None:
    return None
  elif isinstance(value, (datetime, date)):
    return value.isoformat()
  elif isinstance(value, Decimal):
    return float(value)
  elif isinstance(value, bytes):
    return value.decode('utf-8', errors='replace')
  else:
    return value


# =============================================================================
# DATABASE FUNCTIONS - Data Retrieval
# =============================================================================

def get_transfer_candidates(conn):
  """
  Get SKUs that need transfer recommendations.

  v5.0 Changes:
  - Now retrieves effective_on_hand, e21_in_transit_qty, fresh_approved_qty
  - Includes SKUs with existing Pending/Paused recommendations (will UPDATE them)

  v5.3 Exclusion fix:
  - Removed terminal statuses (Applied, Trip_Assigned, Shipped, Received)
    from the exclusion list. Inventory_current already reflects those
    completed transfers, so the view's suggested_transfer_qty is a
    genuine new need.

  v5.4 Exclusion fix (this version):
  - NO status-based exclusion at all. The view (vw_unified_transfer_detection)
    is now the single source of truth for detecting whether a new
    recommendation is needed. Its FreshApprovedRecommendations CTE
    accounts for in-pipeline qty (statuses: Auto-Approved, Approved,
    Applied, Trip_Assigned) within a 1-hour window, and e21_in_transit_qty
    accounts for stock already on its way via the hub_transfer_orders table.
  - Rationale: when a SKU has an Auto-Approved or Approved rec and
    NEW demand arrives, the Hub Manager needs to see an incremental
    recommendation. Previously, those statuses were excluded from
    candidate generation, so incremental demand was silently dropped
    and the Hub Manager never saw it. The view correctly computes the
    incremental shortfall (new demand minus already-approved qty within
    the fresh window), so trusting the view yields the right answer.
  - Known trade-off: if the 1-hour fresh window expires before the
    transfer lands in e21 (hub_transfer_orders), the view may briefly
    re-detect the full shortfall and create a duplicate-sized rec.
    v5.5 ships the companion warning system (see
    check_duplicate_warnings_batch) that surfaces these situations
    to the Hub Manager so they can reject duplicates with full
    context rather than the system hiding or guessing.
  """
  query = """
    SELECT
        event_type, base_urgency, sku, is_a_item,
        total_demand, order_count, earliest_ship_date, latest_ship_date,
        demand_overdue, demand_1_2_days, demand_3_5_days, demand_6_plus_days,
        demand_in_horizon,
        starlite_available, starlite_on_hand,
        effective_on_hand, starlite_effective_on_hand, hub_effective_on_hand,
        e21_in_transit_qty, fresh_approved_qty,
        hub_available, hub_on_hand,
        min_stock, max_stock, avg_daily_usage, remaining_after_demand,
        time_urgency, inventory_urgency, hub_status,
        transfer_needed, suggested_transfer_qty
    FROM dbo.vw_unified_transfer_detection
    WHERE transfer_needed = 'Yes'
      AND suggested_transfer_qty > 0
    ORDER BY
        CASE time_urgency
            WHEN 'CRITICAL' THEN 1
            WHEN 'HIGH' THEN 2
            WHEN 'MEDIUM' THEN 3
            WHEN 'LOW' THEN 4
            WHEN 'NO DATE' THEN 5
            ELSE 6
        END,
        event_type DESC, sku
    """
  cursor = conn.cursor()
  cursor.execute(query)
  results = list(cursor.fetchall())
  return results


def check_duplicate_warnings_batch(conn, candidates):
  """
  v5.17 — Detect probable duplicate recommendations using TWO signals:

    (1) State-1 gate (from v5.16): qty applied in last 24h is not covered
        by current e21 in-transit + receipts in the last 24h. Catches the
        original "transfer is genuinely stuck" pattern.

    (2) Fast-cycle guardrail (NEW in v5.17): a rec for the same SKU was
        Delivered in the last 2 hours. The view's hub_available snapshot
        is likely stale, AI may have regenerated essentially the same ask.
        Catches the YS55EB0200 2026-05-27 pattern.

  Returns a dict keyed by SKU:
    {
      sku: {
        'has_warning': True,
        'warning_text': 'POSSIBLE DUPLICATE: ...',
      }
    }
  SKUs without any applicable signal are absent from the dict.

  Performance: one round-trip; the new CTE adds one read on the same
  ai_transfer_recommendations table that AppliedRecent already uses.
  """
  if not candidates:
    return {}

  e21_by_sku = {}
  qty_by_sku = {}        # v5.17: track candidate qty for warning-text clarity
  for c in candidates:
    key = (c['sku'] or '').strip()
    e21_by_sku[key] = float(c.get('e21_in_transit_qty') or 0)
    qty_by_sku[key] = float(c.get('suggested_transfer_qty')
                            or c.get('ai_recommended_qty')
                            or c.get('calculated_transfer_qty')
                            or 0)

  skus = list(e21_by_sku.keys())
  if not skus:
    return {}
  placeholders = ','.join(['%s'] * len(skus))

  query = """
    WITH AppliedRecent AS (
        SELECT
            LTRIM(RTRIM(r.sku)) AS sku,
            r.recommendation_id,
            r.ai_recommended_qty,
            r.calculated_transfer_qty,
            r.manager_override_qty,
            r.reviewed_date,
            r.status,
            ROW_NUMBER() OVER (
                PARTITION BY LTRIM(RTRIM(r.sku))
                ORDER BY r.reviewed_date DESC
            ) AS rn
        FROM dbo.ai_transfer_recommendations r
        WHERE LTRIM(RTRIM(r.sku)) IN (""" + placeholders + """)
          AND r.status IN ('Applied', 'Trip_Assigned', 'Finalized', 'Delivered')
          AND r.reviewed_date > DATEADD(HOUR, -24, GETDATE())
    ),
    SkuAgg AS (
        SELECT
            sku,
            SUM(
                CAST(
                    COALESCE(manager_override_qty,
                             ai_recommended_qty,
                             calculated_transfer_qty) AS DECIMAL(18, 2)
                )
            ) AS applied_qty_past_fresh,
            COUNT(*) AS rec_count
        FROM AppliedRecent
        GROUP BY sku
    ),
    ReceivedRecent AS (
        SELECT
            sku,
            SUM(CAST(transfer_qty AS DECIMAL(18, 2))) AS received_qty_24h
        FROM dbo.hub_transfer_orders_history
        WHERE sku IN (""" + placeholders + """)
          AND received_date >= CAST(DATEADD(HOUR, -24, GETDATE()) AS DATE)
        GROUP BY sku
    ),
    -- v5.17: fast-cycle guardrail. Any rec for this SKU Delivered in the
    -- last 2 hours flags the SKU as "snapshot likely stale, treat new
    -- candidates with suspicion." The 2h window covers one EasyMorph
    -- nexus_hourly cycle plus a buffer.
    DeliveredVeryRecent AS (
        SELECT
            LTRIM(RTRIM(sku)) AS sku,
            MAX(reviewed_date) AS last_delivered_date,
            MAX(recommendation_id) AS last_delivered_rec_id,
            MAX(CAST(COALESCE(manager_override_qty,
                              ai_recommended_qty,
                              calculated_transfer_qty) AS DECIMAL(18, 2)))
                                  AS last_delivered_qty
        FROM dbo.ai_transfer_recommendations
        WHERE LTRIM(RTRIM(sku)) IN (""" + placeholders + """)
          AND status = 'Delivered'
          AND reviewed_date > DATEADD(HOUR, -2, GETDATE())
        GROUP BY LTRIM(RTRIM(sku))
    )
    SELECT
        COALESCE(a.sku, dvr.sku) AS sku,
        ISNULL(a.applied_qty_past_fresh, 0) AS applied_qty_past_fresh,
        ISNULL(a.rec_count, 0) AS rec_count,
        r.recommendation_id AS latest_rec_id,
        CAST(
            COALESCE(r.manager_override_qty,
                     r.ai_recommended_qty,
                     r.calculated_transfer_qty) AS DECIMAL(18, 2)
        ) AS latest_rec_qty,
        r.reviewed_date AS latest_reviewed_date,
        r.status        AS latest_status,
        ISNULL(rr.received_qty_24h, 0) AS received_qty_24h,
        dvr.last_delivered_date,
        dvr.last_delivered_rec_id,
        dvr.last_delivered_qty
    FROM SkuAgg a
    FULL OUTER JOIN DeliveredVeryRecent dvr
        ON dvr.sku = a.sku
    LEFT JOIN AppliedRecent r
        ON r.sku = COALESCE(a.sku, dvr.sku)
       AND r.rn = 1
    LEFT JOIN ReceivedRecent rr
        ON rr.sku = COALESCE(a.sku, dvr.sku)
  """

  cursor = conn.cursor()
  # skus tuple passed THREE times — AppliedRecent, ReceivedRecent, DeliveredVeryRecent
  cursor.execute(query, tuple(skus) + tuple(skus) + tuple(skus))
  rows = list(cursor.fetchall())

  warnings = {}
  for row in rows:
    sku = (row['sku'] or '').strip()
    applied_sum = float(row['applied_qty_past_fresh'] or 0)
    received_qty_24h = float(row['received_qty_24h'] or 0)
    e21_qty = e21_by_sku.get(sku, 0)
    last_delivered_date = row.get('last_delivered_date')
    last_delivered_rec_id = row.get('last_delivered_rec_id')
    last_delivered_qty = float(row.get('last_delivered_qty') or 0)

    # v5.16 State-1 gate
    state1_triggered = applied_sum > (e21_qty + received_qty_24h)

    # v5.17 fast-cycle guardrail. A recent Delivered means inventory
    # snapshots are likely stale; treat any candidate as suspect.
    fast_cycle_triggered = last_delivered_date is not None

    if not (state1_triggered or fast_cycle_triggered):
      continue

    # Build the warning text. Both signals share the same prefix but the
    # body differs so the Hub Manager knows which condition fired.
    latest_id = row['latest_rec_id']
    latest_qty = float(row['latest_rec_qty'] or 0)
    latest_date = row['latest_reviewed_date']
    latest_status = row['latest_status']
    rec_count = int(row['rec_count'] or 0)

    if state1_triggered:
      if isinstance(latest_date, datetime):
        when = latest_date.strftime('%Y-%m-%d %H:%M')
      else:
        when = str(latest_date or '')
      count_phrase = '1 transfer' if rec_count == 1 else (str(rec_count) + ' transfers')
      warning_text = (
        'POSSIBLE DUPLICATE: ' + count_phrase +
        ' for this SKU totaling ' + str(int(applied_sum)) +
        ' units were ' + (latest_status or '').replace('_', ' ').lower() +
        ' in the last 24h but are not covered by e21 in-transit (' +
        str(int(e21_qty)) + ') plus recent receipts (' +
        str(int(received_qty_24h)) + '). ' +
        'Most recent: rec #' + str(latest_id) +
        ', ' + str(int(latest_qty)) + ' units, ' + when + '. ' +
        'Verify with the Hub before approving to avoid double-shipment.'
      )
    else:
      # fast_cycle_triggered (and state1 not triggered)
      if isinstance(last_delivered_date, datetime):
        delivered_when = last_delivered_date.strftime('%Y-%m-%d %H:%M')
      else:
        delivered_when = str(last_delivered_date or '')
      warning_text = (
        'POSSIBLE DUPLICATE (fast-cycle): a transfer of ' +
        str(int(last_delivered_qty)) + ' units (rec #' +
        str(last_delivered_rec_id) + ', ' + delivered_when + ') was just ' +
        'Delivered for this SKU. Hub inventory and Starlite effective-on-hand ' +
        'snapshots may not yet reflect that transfer, and this rec may be a ' +
        'duplicate of the just-completed ask. Verify with the Hub before approving.'
      )

    warnings[sku] = {
      'has_warning': True,
      'warning_text': warning_text,
    }

  return warnings


def get_usage_trends(conn, sku):
  recent_days = 30
  prior_days = 30
  query = """
    WITH RecentUsage AS (
        SELECT 
            AVG(CAST(qty_sold AS DECIMAL(18,4))) AS avg_daily,
            COUNT(DISTINCT usage_date) AS days_with_sales
        FROM dbo.usage_data
        WHERE sku = %s
          AND usage_date >= DATEADD(DAY, %s, CAST(GETDATE() AS DATE))
          AND usage_date < CAST(GETDATE() AS DATE)
    ),
    PriorUsage AS (
        SELECT 
            AVG(CAST(qty_sold AS DECIMAL(18,4))) AS avg_daily
        FROM dbo.usage_data
        WHERE sku = %s
          AND usage_date >= DATEADD(DAY, %s, CAST(GETDATE() AS DATE))
          AND usage_date < DATEADD(DAY, %s, CAST(GETDATE() AS DATE))
    )
    SELECT 
        r.avg_daily as recent_avg, 
        r.days_with_sales,
        p.avg_daily as prior_avg
    FROM RecentUsage r, PriorUsage p
    """
  cursor = conn.cursor()
  cursor.execute(query, (sku, -recent_days, sku, -(recent_days + prior_days), -recent_days))
  row = cursor.fetchone()

  if row:
    recent_avg = float(row['recent_avg']) if row['recent_avg'] else 0
    prior_avg = float(row['prior_avg']) if row['prior_avg'] else 0

    if prior_avg > 0:
      trend_pct = ((recent_avg - prior_avg) / prior_avg) * 100
      if trend_pct > 5:
        trend_direction = 'UP'
      elif trend_pct < -5:
        trend_direction = 'DOWN'
      else:
        trend_direction = 'STABLE'
    else:
      trend_pct = 0
      if recent_avg > 0:
        trend_direction = 'NEW'
      else:
        trend_direction = 'NO DATA'

    return {
      'recent_avg': recent_avg,
      'prior_avg': prior_avg,
      'trend_pct': trend_pct,
      'trend_direction': trend_direction
    }

  return {'recent_avg': 0, 'prior_avg': 0, 'trend_pct': 0, 'trend_direction': 'NO DATA'}


def get_seasonal_context(conn):
  current_month = datetime.now().month

  query = """
    SELECT month_number, sales_factor
    FROM dbo.seasonal_patterns
    WHERE month_number = %s
      AND pattern_type = 'company_wide'
    """
  cursor = conn.cursor()
  cursor.execute(query, (current_month,))
  row = cursor.fetchone()

  if row:
    return {
      'current_month': current_month,
      'current_factor': float(row['sales_factor']) if row['sales_factor'] else 1.0
    }

  return {'current_month': current_month, 'current_factor': 1.0}


def get_hub_trajectory(conn, sku):
  query = """
    SELECT TOP 7 
        snapshot_date,
        on_hand,
        available
    FROM dbo.inventory_snapshots
    WHERE sku = %s AND cost_center = 'Hub'
    ORDER BY snapshot_date DESC
    """
  cursor = conn.cursor()
  cursor.execute(query, (sku,))
  rows = list(cursor.fetchall())

  if len(rows) >= 2:
    latest = float(rows[0]['available']) if rows[0]['available'] else 0
    oldest = float(rows[-1]['available']) if rows[-1]['available'] else 0
    trend = latest - oldest
    avg_available = sum(float(r['available'] or 0) for r in rows) / len(rows)

    return {
      'trend': trend,
      'trend_direction': 'INCREASING' if trend > 0 else ('DECREASING' if trend < 0 else 'STABLE'),
      'avg_available': avg_available,
      'days_of_data': len(rows)
    }

  return {'trend': 0, 'trend_direction': 'UNKNOWN', 'avg_available': 0, 'days_of_data': 0}


def get_part_specs(conn, sku):
  query = """
    SELECT 
        part_desc,
        act_volume_ft3,
        volume_factor,
        act_weight_lb,
        weight_factor,
        line, series, model
    FROM dbo.part_specs
    WHERE sku = %s
    """
  cursor = conn.cursor()
  cursor.execute(query, (sku,))
  row = cursor.fetchone()

  if row:
    vol = float(row['act_volume_ft3'] or 0) * float(row['volume_factor'] or 1)
    wgt = float(row['act_weight_lb'] or 0) * float(row['weight_factor'] or 1)

    return {
      'part_desc': row['part_desc'] or sku,
      'unit_volume_ft3': vol,
      'unit_weight_lb': wgt,
      'line': row['line'],
      'series': row['series'],
      'model': row['model']
    }

  return {
    'part_desc': sku,
    'unit_volume_ft3': 0,
    'unit_weight_lb': 0,
    'line': None,
    'series': None,
    'model': None
  }


def get_truck_capacity(conn):
  query = """
    SELECT parameter_name, parameter_value
    FROM dbo.global_parameters
    WHERE parameter_name IN ('truck_cap_ft3', 'truck_cap_lb')
    """
  cursor = conn.cursor()
  cursor.execute(query)
  rows = list(cursor.fetchall())

  result = {'max_volume_ft3': 1456, 'max_weight_lb': 13000}
  for row in rows:
    if row['parameter_name'] == 'truck_cap_ft3':
      result['max_volume_ft3'] = float(row['parameter_value'])
    elif row['parameter_name'] == 'truck_cap_lb':
      result['max_weight_lb'] = float(row['parameter_value'])

  return result


def get_planning_horizon_days(conn):
  """
  Pull the transfer planning horizon (days) from dbo.global_parameters.
  Falls back to 21 if the parameter is missing — same fallback the view uses.
  Read once per batch and threaded into the AI prompt so the label stays
  in sync with whatever the view actually computed.
  """
  cursor = conn.cursor()
  cursor.execute("""
    SELECT parameter_value
    FROM dbo.global_parameters
    WHERE parameter_name = 'transfer_planning_horizon_days'
  """)
  row = cursor.fetchone()
  if row and row['parameter_value'] is not None:
    try:
      return int(row['parameter_value'])
    except (TypeError, ValueError):
      pass
  return 21


def get_existing_recommendation(conn, sku):
  """
  Check if a Pending or Paused recommendation exists for this SKU.
  v5.0: Instead of skipping, we'll UPDATE the existing record.
  """
  query = """
    SELECT recommendation_id, status, ai_recommended_qty
    FROM dbo.ai_transfer_recommendations
    WHERE sku = %s
      AND status IN ('Pending', 'Paused')
    """
  cursor = conn.cursor()
  cursor.execute(query, (sku,))
  row = cursor.fetchone()

  if row:
    return {
      'exists': True,
      'recommendation_id': row['recommendation_id'],
      'status': row['status'],
      'previous_ai_qty': row['ai_recommended_qty']
    }

  return {'exists': False, 'recommendation_id': None, 'status': None, 'previous_ai_qty': None}


def mark_stale_recommendations_obsolete(conn, batch_id):
  """
  Mark Pending/Paused recommendations as 'Obsolete' if their SKU is no longer
  in vw_unified_transfer_detection (conditions changed, no transfer needed).

  This prevents stale recommendations from sitting in Pending status forever
  when the underlying conditions have changed (e.g., orders fulfilled,
  inventory received, etc.).

  v5.6: Distinguishes WHY the rec became obsolete so we can measure how often
        the new effective-on-hand logic (which treats committed recs as virtual
        inventory) is correctly superseding open Pending recs vs. how often
        demand simply resolved on its own.

        For each stale rec, look at the SUM of committed qty on the same SKU
        across Auto-Approved / Approved / Applied / Trip_Assigned / Finalized
        / Delivered rows:
          - committed_qty >= this rec's planned qty  -> Superseded_By_Later_Rec
          - otherwise                                 -> Demand_Resolved

        v5.7 (2026-04-22): added 'Finalized' and 'Delivered' so the cascade
        that advances recs past Trip_Assigned on Finalize Batch does not
        cause this classifier to mis-label superseded recs as Demand_Resolved.

        Reason is written to dbo.ai_agent_log (via log_action) and to the
        rec's notes field. No schema change.

  Returns: dict with count of obsoleted recommendations and list of SKUs
  """
  find_stale_query = """
    SELECT r.recommendation_id, r.sku, r.status,
           r.ai_recommended_qty, r.calculated_transfer_qty,
           r.manager_override_qty,
           r.recommendation_date, r.notes
    FROM dbo.ai_transfer_recommendations r
    WHERE r.status IN ('Pending', 'Paused')
      AND r.sku NOT IN (
          SELECT DISTINCT sku
          FROM dbo.vw_unified_transfer_detection
          WHERE transfer_needed = 'Yes'
      )
  """

  cursor = conn.cursor()
  cursor.execute(find_stale_query)
  stale_records = list(cursor.fetchall())

  if not stale_records:
    return {'obsoleted': 0, 'skus': []}

  # v5.6: Pre-compute committed qty per SKU so we can classify the obsolete
  # reason in a single round-trip instead of N+1 queries.
  stale_skus = list({r['sku'] for r in stale_records})
  placeholders = ','.join(['%s'] * len(stale_skus))
  committed_query = (
    "SELECT sku, "
    "       SUM(COALESCE(manager_override_qty, ai_recommended_qty, "
    "                    calculated_transfer_qty)) AS committed_qty "
    "FROM dbo.ai_transfer_recommendations "
    "WHERE status IN ('Auto-Approved', 'Approved', 'Applied', 'Trip_Assigned', 'Finalized', 'Delivered') "
    "  AND sku IN (" + placeholders + ") "
    "GROUP BY sku"
  )
  cursor.execute(committed_query, tuple(stale_skus))
  committed_by_sku = {row['sku']: (row['committed_qty'] or 0)
                      for row in cursor.fetchall()}

  obsoleted_skus = []
  timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

  for record in stale_records:
    rec_id = record['recommendation_id']
    sku = record['sku']
    prev_status = record['status']
    prev_qty = (record['manager_override_qty']
                or record['ai_recommended_qty']
                or record['calculated_transfer_qty']
                or 0)
    existing_notes = record['notes'] or ''

    # v5.6: classify the reason.
    committed_qty = committed_by_sku.get(sku, 0)
    if committed_qty > 0 and committed_qty >= prev_qty:
      reason_code = 'Superseded_By_Later_Rec'
      reason_human = (
        "superseded: " + str(committed_qty) + " units already committed on "
        "another rec for this SKU, fully covering the " + str(prev_qty) +
        "-unit shortfall this rec was raised for"
      )
    else:
      reason_code = 'Demand_Resolved'
      reason_human = (
        "demand resolved: SKU no longer in view and no committed rec covers "
        "the " + str(prev_qty) + "-unit shortfall (committed elsewhere: " +
        str(committed_qty) + ")"
      )

    obsolete_note = (
      "[" + timestamp + "] OBSOLETE (" + reason_code + "): " + reason_human +
      ". Prev status: " + str(prev_status) + ", Prev qty: " + str(prev_qty)
    )

    if existing_notes:
      new_notes = existing_notes + '\n' + obsolete_note
    else:
      new_notes = obsolete_note

    update_query = """
      UPDATE dbo.ai_transfer_recommendations
      SET status = 'Obsolete',
          reviewed_by = 'SYSTEM',
          reviewed_date = GETDATE(),
          batch_id = %s,
          notes = %s
      WHERE recommendation_id = %s
    """
    cursor.execute(update_query, (batch_id, new_notes, rec_id))

    # v5.6: audit trail so we can report Superseded-vs-Resolved mix.
    # A logging failure must never break the obsolete sweep.
    try:
      log_action(
        conn,
        'Obsolete_' + reason_code,
        sku,
        str(prev_qty),
        '0',
        reason_human
      )
    except Exception:
      pass

    obsoleted_skus.append(sku)

  conn.commit()

  # ─────────────────────────────────────────────────────────────────────
  # v5.12: SECOND SWEEP — stale qty=0 Applied/Auto-Approved cleanup
  # ─────────────────────────────────────────────────────────────────────
  # The first sweep above only catches Pending/Paused recs whose SKU is
  # no longer in the view. It misses a different class of landmine:
  # Applied/Auto-Approved recs where the Hub Manager set the AI qty to
  # zero (an "approved no-op" decision) and the rec was never moved to
  # a terminal state. Those recs sit at status='Applied' indefinitely,
  # invisible to the optimizer (qty=0 filter in get_eligible_transfers)
  # but VERY visible to v5.9's dedupe — which can pick them as the
  # "older keeper" and silently obsolete every fresh meaningful rec
  # for the same SKU. See 2026-05-04 YS58D0800 incident (rec 404).
  #
  # Filter conditions:
  #   - status IN ('Applied', 'Auto-Approved')
  #   - effective qty (override > AI > calc) = 0
  #   - reviewed_date is older than _STALE_ZERO_QTY_DAYS (14 by default)
  # Tied to reviewed_date rather than created_date because the Hub
  # Manager's review action is what made the rec a "no-op" — the
  # creation date is irrelevant for that decision.
  cursor.execute(
    """
        SELECT recommendation_id, sku, status, reviewed_date, notes
        FROM   dbo.ai_transfer_recommendations
        WHERE  status IN ('Applied', 'Auto-Approved')
          AND  COALESCE(manager_override_qty,
                        ai_recommended_qty,
                        calculated_transfer_qty) = 0
          AND  reviewed_date IS NOT NULL
          AND  reviewed_date < DATEADD(day, -%s, GETDATE())
    """,
    (_STALE_ZERO_QTY_DAYS,)
  )
  zero_qty_records = list(cursor.fetchall())

  zero_qty_obsoleted = []
  if zero_qty_records:
    timestamp_zq = datetime.now().strftime('%Y-%m-%d %H:%M')

    for record in zero_qty_records:
      rec_id = record['recommendation_id']
      sku = record['sku']
      prev_status = record['status']
      reviewed_date = record['reviewed_date']
      existing_notes = record['notes'] or ''

      reason_code = 'Stale_Zero_Qty_Approval'
      reason_human = (
        "stale qty=0 approval from " + str(reviewed_date)[:10] +
        " (>" + str(_STALE_ZERO_QTY_DAYS) + " days old). "
        "Retired so it can't interfere with v5.9 dedupe on fresh recs."
      )
      obsolete_note = (
        "[" + timestamp_zq + "] OBSOLETE (" + reason_code + "): " +
        reason_human + " Prev status: " + str(prev_status) + ", Prev qty: 0"
      )
      new_notes = (existing_notes + '\n' + obsolete_note) if existing_notes \
      else obsolete_note

      cursor.execute(
        """
          UPDATE dbo.ai_transfer_recommendations
          SET status = 'Obsolete',
              reviewed_by = 'SYSTEM',
              reviewed_date = GETDATE(),
              batch_id = %s,
              notes = %s
          WHERE recommendation_id = %s
            AND status IN ('Applied', 'Auto-Approved')
            AND COALESCE(manager_override_qty,
                         ai_recommended_qty,
                         calculated_transfer_qty) = 0
        """,
        (batch_id, new_notes, rec_id)
      )

      try:
        log_action(
          conn,
          'Obsolete_' + reason_code,
          sku,
          '0',
          '0',
          reason_human + " (rec " + str(rec_id) + ", v" + __version__ + ")"
        )
      except Exception:
        pass

      zero_qty_obsoleted.append(sku)

    conn.commit()

  # Combine both sweep results so callers see one unified count.
  all_obsoleted_skus = obsoleted_skus + zero_qty_obsoleted

  return {
    'obsoleted': len(all_obsoleted_skus),
    'skus': all_obsoleted_skus,
    'not_in_view_obsoleted': len(obsoleted_skus),
    'stale_zero_qty_obsoleted': len(zero_qty_obsoleted),
  }


# =============================================================================
# EMAIL AND CSV FUNCTIONS
# =============================================================================

def get_part_descriptions(conn, skus):
  """Get part descriptions for a list of SKUs."""
  if not skus:
    return {}

  placeholders = ','.join(['%s'] * len(skus))
  query = "SELECT sku, part_desc FROM dbo.part_specs WHERE sku IN (" + placeholders + ")"

  cursor = conn.cursor()
  cursor.execute(query, tuple(skus))

  return {row['sku']: row['part_desc'] for row in cursor.fetchall()}


def generate_auto_approved_csv(conn, auto_approved_items, batch_id):
  """
  Generate CSV content for auto-approved transfers.
  Returns tuple of (csv_media, total_qty, item_count)
  """
  if not auto_approved_items:
    return None, 0, 0

  skus = [item['sku'] for item in auto_approved_items]
  part_descs = get_part_descriptions(conn, skus)

  timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
  csv_buffer = io.StringIO()
  writer = csv.writer(csv_buffer)

  writer.writerow(['# Auto-Approved Transfer Orders - Generated ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'), '', '', '', '', '', '', '', ''])
  writer.writerow(['# Batch ID: ' + batch_id, '', '', '', '', '', '', '', ''])
  writer.writerow(['# Total Items: ' + str(len(auto_approved_items)), '', '', '', '', '', '', '', ''])
  writer.writerow(['# CRITICAL/HIGH urgency - requires immediate action', '', '', '', '', '', '', '', ''])
  writer.writerow(['#', '', '', '', '', '', '', '', ''])

  writer.writerow([
    'item_type', 'part_code', 'part_desc', 'uom', 'qty',
    'item_status', 'event_type', 'time_urgency', 'batch_id'
  ])

  total_qty = 0
  for item in auto_approved_items:
    sku = item['sku']
    qty = int(item['suggested_transfer_qty'] or 0)

    if qty > 0:
      writer.writerow([
        db_config.TRANSFER_CONSTANTS['item_type'],
        sku,
        part_descs.get(sku, sku),
        db_config.TRANSFER_CONSTANTS['uom'],
        qty,
        db_config.TRANSFER_CONSTANTS['item_status'],
        'Type ' + str(item['event_type']),
        item['time_urgency'] or '',
        batch_id
      ])
      total_qty += qty

  csv_content = csv_buffer.getvalue()
  csv_buffer.close()

  csv_media = anvil.BlobMedia(
    'text/csv',
    csv_content.encode('utf-8'),
    name='Transfer_AutoApproved_' + timestamp + '.csv'
  )

  return csv_media, total_qty, len(auto_approved_items)


def send_auto_approved_email(csv_media, item_count, total_qty, batch_id):
  """
  Send email to Hub Manager with auto-approved transfers CSV.
  Returns True if successful, False otherwise.
  """
  try:
    subject = 'URGENT: ' + str(item_count) + ' Auto-Approved Transfers Ready (' + str(total_qty) + ' units)'

    body_text = """
Auto-Approved Transfer Orders
=============================

""" + str(item_count) + """ transfer(s) have been auto-approved due to CRITICAL or HIGH urgency.

Total Units: """ + str(total_qty) + """
Batch ID: """ + batch_id + """
Generated: """ + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + """

These items have customer orders requiring immediate fulfillment.

ACTION REQUIRED:
1. Open the attached CSV file
2. Import into e21 to create transfer orders
3. Process transfers promptly to avoid delays

The CSV is attached to this email.

--
Titan Flow Control Automated Transfer System
"""

    body_html = """
<html>
<body style="font-family: Arial, sans-serif;">
<h2 style="color: #d32f2f;">Auto-Approved Transfer Orders</h2>

<p><strong>""" + str(item_count) + """ transfer(s)</strong> have been auto-approved due to <span style="color: #d32f2f;">CRITICAL</span> or <span style="color: #ff9800;">HIGH</span> urgency.</p>

<table style="border-collapse: collapse; margin: 20px 0;">
<tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>Total Units:</strong></td><td style="padding: 8px; border: 1px solid #ddd;">""" + str(total_qty) + """</td></tr>
<tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>Batch ID:</strong></td><td style="padding: 8px; border: 1px solid #ddd;">""" + batch_id + """</td></tr>
<tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>Generated:</strong></td><td style="padding: 8px; border: 1px solid #ddd;">""" + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + """</td></tr>
</table>

<p>These items have customer orders requiring immediate fulfillment.</p>

<h3>ACTION REQUIRED:</h3>
<ol>
<li>Open the attached CSV file</li>
<li>Import into e21 to create transfer orders</li>
<li>Process transfers promptly to avoid delays</li>
</ol>

<p style="color: #666; font-size: 12px; margin-top: 30px;">
--<br>
Titan Flow Control Automated Transfer System
</p>
</body>
</html>
"""

    anvil.email.send(
      to=db_config.get_hub_manager_email(),
      subject=subject,
      text=body_text,
      html=body_html,
      attachments=[csv_media]
    )

    return True

  except Exception as e:
    print('Email error: ' + str(e))
    return False


# =============================================================================
# DATABASE FUNCTIONS - Save Results
# =============================================================================

def save_recommendation(conn, candidate, ai_result, batch_id, status='Pending'):
  """
  Insert a NEW recommendation record.

  v5.3 Change: Now includes e21_in_transit_qty, fresh_approved_qty, effective_on_hand
  for audit trail and debugging purposes.

  v5.5 Changes:
  - Persists has_duplicate_warning flag (set by check_duplicate_warnings_batch).
  - Prepends duplicate_warning_text to the reasoning field so it appears at
    the TOP of the Hub Manager's TextArea.
  - Removed the LEARNING POINT append (de-cluttering rp_transfer panel
    to make room for the warning).
  """
  query = """
    INSERT INTO dbo.ai_transfer_recommendations (
        sku, event_type, base_urgency,
        starlite_available, starlite_on_hand, hub_available, hub_on_hand,
        e21_in_transit_qty, fresh_approved_qty, effective_on_hand,
        starlite_effective_on_hand, hub_effective_on_hand,
        min_stock, max_stock, avg_daily_usage,
        total_demand, order_count, earliest_ship_date, latest_ship_date,
        demand_overdue, demand_1_2_days,
        remaining_after_demand, time_urgency, inventory_urgency, hub_status,
        calculated_transfer_qty, ai_recommended_qty,
        confidence_level, reasoning, seasonal_consideration, risk_factors,
        recommendation_date, status, batch_id, created_by,
        has_duplicate_warning
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, GETDATE(), %s, %s, %s,
            %s)
    """

  # v5.5: prepend duplicate warning (if any) so it leads the reasoning text.
  base_reasoning = ai_result.get('reasoning', '')
  warning_text = candidate.get('duplicate_warning_text') or ''
  has_warning = 1 if candidate.get('has_duplicate_warning') else 0

  if warning_text:
    full_reasoning = warning_text + '\n\n' + base_reasoning
  else:
    full_reasoning = base_reasoning

  # HARD CAP: AI cannot recommend more than hub_available
  hub_available = float(candidate['hub_available'] or 0)
  rec_qty = ai_result.get('recommended_qty', candidate['suggested_transfer_qty'])
  rec_qty = min(float(rec_qty), hub_available)  # Enforce hard cap

  conf_level = ai_result.get('confidence_level', 'High' if status == 'Auto-Approved' else 'Medium')

  cursor = conn.cursor()
  cursor.execute(query, (
    candidate['sku'],
    candidate['event_type'],
    candidate['base_urgency'],
    candidate['starlite_available'],
    candidate['starlite_on_hand'],
    candidate['hub_available'],
    candidate['hub_on_hand'],
    candidate.get('e21_in_transit_qty') or 0,
    candidate.get('fresh_approved_qty') or 0,
    candidate.get('effective_on_hand') or candidate['starlite_on_hand'],
    candidate.get('starlite_effective_on_hand'),   # v5.6: snapshot
    candidate.get('hub_effective_on_hand'),        # v5.6: snapshot
    candidate['min_stock'],
    candidate['max_stock'],
    candidate['avg_daily_usage'],
    candidate['total_demand'],
    candidate['order_count'],
    candidate['earliest_ship_date'],
    candidate['latest_ship_date'],
    candidate['demand_overdue'],
    candidate['demand_1_2_days'],
    candidate['remaining_after_demand'],
    candidate['time_urgency'],
    candidate['inventory_urgency'],
    candidate['hub_status'],
    candidate['suggested_transfer_qty'],
    rec_qty,
    conf_level,
    full_reasoning,
    ai_result.get('seasonal_consideration', ''),
    ai_result.get('risk_factors', ''),
    status,
    batch_id,
    'anvil_transfer_review',
    has_warning
  ))
  conn.commit()


def get_existing_recommendation_details(conn, recommendation_id):
  """
  Get full details of existing recommendation for comparison.
  Used to determine if meaningful changes occurred.
  """
  query = """
    SELECT 
        ai_recommended_qty,
        calculated_transfer_qty,
        time_urgency,
        inventory_urgency,
        event_type,
        total_demand,
        hub_available
    FROM dbo.ai_transfer_recommendations
    WHERE recommendation_id = %s
  """
  cursor = conn.cursor()
  cursor.execute(query, (recommendation_id,))
  row = cursor.fetchone()

  if row:
    return {
      'ai_recommended_qty': float(row['ai_recommended_qty'] or 0),
      'calculated_transfer_qty': float(row['calculated_transfer_qty'] or 0),
      'time_urgency': row['time_urgency'],
      'inventory_urgency': row['inventory_urgency'],
      'event_type': row['event_type'],
      'total_demand': float(row['total_demand'] or 0),
      'hub_available': float(row['hub_available'] or 0)
    }
  return None


def detect_meaningful_changes(old_values, new_candidate, new_ai_qty):
  """
  Compare old vs new values and return list of meaningful changes.
  Returns empty list if nothing significant changed.
  """
  changes = []

  if old_values is None:
    return ['Initial review']

  # Check AI recommended quantity change (any change is meaningful)
  old_ai_qty = old_values['ai_recommended_qty']
  if abs(new_ai_qty - old_ai_qty) >= 1:  # At least 1 unit difference
    changes.append(f"AI qty: {int(old_ai_qty)}→{int(new_ai_qty)}")

  # Check calculated transfer quantity change
  old_calc = old_values['calculated_transfer_qty']
  new_calc = float(new_candidate['suggested_transfer_qty'] or 0)
  if abs(new_calc - old_calc) >= 1:
    changes.append(f"Calc qty: {int(old_calc)}→{int(new_calc)}")

  # Check time urgency change
  old_time_urg = old_values['time_urgency']
  new_time_urg = new_candidate['time_urgency']
  if old_time_urg != new_time_urg:
    changes.append(f"Time urg: {old_time_urg}→{new_time_urg}")

  # Check inventory urgency change
  old_inv_urg = old_values['inventory_urgency']
  new_inv_urg = new_candidate['inventory_urgency']
  if old_inv_urg != new_inv_urg:
    changes.append(f"Inv urg: {old_inv_urg}→{new_inv_urg}")

  # Check event type change
  old_event = old_values['event_type']
  new_event = new_candidate['event_type']
  if old_event != new_event:
    changes.append(f"Event: Type {old_event}→Type {new_event}")

  # Check total demand change (>10% change is meaningful)
  old_demand = old_values['total_demand']
  new_demand = float(new_candidate['total_demand'] or 0)
  if old_demand > 0 and abs(new_demand - old_demand) / old_demand > 0.10:
    changes.append(f"Demand: {int(old_demand)}→{int(new_demand)}")
  elif old_demand == 0 and new_demand > 0:
    changes.append(f"Demand: 0→{int(new_demand)}")

  # Check hub available change (significant change)
  old_hub = old_values['hub_available']
  new_hub = float(new_candidate['hub_available'] or 0)
  if abs(new_hub - old_hub) >= 5:  # At least 5 unit difference
    changes.append(f"Hub avail: {int(old_hub)}→{int(new_hub)}")

  return changes


def update_recommendation(conn, recommendation_id, candidate, ai_result, batch_id, previous_status):
  """
  UPDATE an existing Pending or Paused recommendation with fresh data.

  v5.0 Addition: Instead of skipping existing recommendations, we update them
  with current inventory state so the Hub Manager always sees accurate data.

  v5.1 Change: Only append to notes when meaningful changes occur to prevent
  notes column overflow from hourly updates with no real changes.

  v5.3 Change: Now includes e21_in_transit_qty, fresh_approved_qty, effective_on_hand
  for audit trail and debugging purposes.

  v5.5 Changes:
  - Persists has_duplicate_warning flag (refreshed every run so the warning
    accurately reflects the current e21 state).
  - Prepends duplicate_warning_text to the reasoning field.
  - Removed the LEARNING POINT append (de-cluttering rp_transfer panel).
  """
  # HARD CAP: AI cannot recommend more than hub_available
  hub_available = float(candidate['hub_available'] or 0)
  rec_qty = ai_result.get('recommended_qty', candidate['suggested_transfer_qty'])
  rec_qty = min(float(rec_qty), hub_available)  # Enforce hard cap

  # Get old values for comparison
  old_values = get_existing_recommendation_details(conn, recommendation_id)

  # Detect what changed
  changes = detect_meaningful_changes(old_values, candidate, rec_qty)

  # Build update note only if meaningful changes occurred
  if changes:
    change_summary = ', '.join(changes)
    update_note = '\n[' + datetime.now().strftime('%Y-%m-%d %H:%M') + '] ' + change_summary
  else:
    update_note = ''  # No note if nothing meaningful changed

  # v5.5: prepend duplicate warning (if any) so it leads the reasoning text.
  base_reasoning = ai_result.get('reasoning', '')
  warning_text = candidate.get('duplicate_warning_text') or ''
  has_warning = 1 if candidate.get('has_duplicate_warning') else 0

  if warning_text:
    full_reasoning = warning_text + '\n\n' + base_reasoning
  else:
    full_reasoning = base_reasoning

  conf_level = ai_result.get('confidence_level', 'Medium')

  # Preserve Paused status if it was Paused, otherwise set to Pending
  new_status = 'Paused' if previous_status == 'Paused' else 'Pending'

  query = """
    UPDATE dbo.ai_transfer_recommendations
    SET event_type = %s,
        base_urgency = %s,
        starlite_available = %s,
        starlite_on_hand = %s,
        hub_available = %s,
        hub_on_hand = %s,
        e21_in_transit_qty = %s,
        fresh_approved_qty = %s,
        effective_on_hand = %s,
        starlite_effective_on_hand = %s,
        hub_effective_on_hand = %s,
        min_stock = %s,
        max_stock = %s,
        avg_daily_usage = %s,
        total_demand = %s,
        order_count = %s,
        earliest_ship_date = %s,
        latest_ship_date = %s,
        demand_overdue = %s,
        demand_1_2_days = %s,
        remaining_after_demand = %s,
        time_urgency = %s,
        inventory_urgency = %s,
        hub_status = %s,
        calculated_transfer_qty = %s,
        ai_recommended_qty = %s,
        confidence_level = %s,
        reasoning = %s,
        seasonal_consideration = %s,
        risk_factors = %s,
        recommendation_date = GETDATE(),
        status = %s,
        batch_id = %s,
        notes = COALESCE(notes, '') + %s,
        has_duplicate_warning = %s
    WHERE recommendation_id = %s
    """

  cursor = conn.cursor()
  cursor.execute(query, (
    candidate['event_type'],
    candidate['base_urgency'],
    candidate['starlite_available'],
    candidate['starlite_on_hand'],
    candidate['hub_available'],
    candidate['hub_on_hand'],
    candidate.get('e21_in_transit_qty') or 0,
    candidate.get('fresh_approved_qty') or 0,
    candidate.get('effective_on_hand') or candidate['starlite_on_hand'],
    candidate.get('starlite_effective_on_hand'),   # v5.6: snapshot
    candidate.get('hub_effective_on_hand'),        # v5.6: snapshot
    candidate['min_stock'],
    candidate['max_stock'],
    candidate['avg_daily_usage'],
    candidate['total_demand'],
    candidate['order_count'],
    candidate['earliest_ship_date'],
    candidate['latest_ship_date'],
    candidate['demand_overdue'],
    candidate['demand_1_2_days'],
    candidate['remaining_after_demand'],
    candidate['time_urgency'],
    candidate['inventory_urgency'],
    candidate['hub_status'],
    candidate['suggested_transfer_qty'],
    rec_qty,
    conf_level,
    full_reasoning,
    ai_result.get('seasonal_consideration', ''),
    ai_result.get('risk_factors', ''),
    new_status,
    batch_id,
    update_note,
    has_warning,
    recommendation_id
  ))
  conn.commit()

  # Return whether changes were detected (useful for logging)
  return len(changes) > 0


def log_action(conn, action_type, sku, old_value, new_value, reason):
  query = """
    INSERT INTO dbo.ai_agent_log (
        action_type, sku, old_value, new_value, reason, action_timestamp, executed_by
    )
    VALUES (%s, %s, %s, %s, %s, GETDATE(), %s)
    """
  cursor = conn.cursor()
  cursor.execute(query, (action_type, sku, old_value, new_value, reason, 'anvil_transfer_review'))
  conn.commit()


# =============================================================================
# AI FUNCTIONS
# =============================================================================

def build_ai_prompt(candidate, usage_trends, seasonal, hub_trajectory, part_specs, truck_capacity, planning_horizon_days):
  """
  Build AI prompt for transfer recommendation analysis.

  v5.2 Change: Added explicit handling for non-stocked items (min_stock=0, max_stock=0).
  Non-stocked items should ONLY receive the exact shortfall amount, no buffer stock.

  v5.6-fix Change: planning_horizon_days is now passed in (read from
  global_parameters.transfer_planning_horizon_days per batch) so the label
  in the prompt stays in sync with whatever the view actually computed.

  v5.11.1 Change: planning_horizon_days no longer has a default value. The
  prior signature defaulted it to 21, which was a stale literal (the live
  parameter has been 28 since 2026-04-22). The default was unreachable in
  practice — the only caller (run_transfer_review at ~line 1802) always
  passes the live value via get_planning_horizon_days(conn) — but a
  silent default that drifts from configuration is exactly the kind of
  thing that bites you eighteen months from now. Making the argument
  required forces any future caller to read the live value explicitly.
  """
  if candidate['event_type'] == 1:
    event_type_desc = "Min/Max Level Replenishment"
  else:
    event_type_desc = "Sales Order Demand"

  # Get effective_on_hand values (new in v5.0)
  effective_on_hand = candidate.get('effective_on_hand') or candidate['starlite_on_hand']
  e21_in_transit = candidate.get('e21_in_transit_qty') or 0
  fresh_approved = candidate.get('fresh_approved_qty') or 0

  # Determine if this is a stocked (A-item) or non-stocked item
  min_stock = candidate.get('min_stock') or 0
  max_stock = candidate.get('max_stock') or 0
  is_stocked_item = min_stock > 0 or max_stock > 0

  prompt = """You are an inventory analyst AI for Titan Flow Control. Your role is to evaluate 
transfer recommendations AND teach inventory principles to personnel who are learning.

CONTEXT:
- Starlite is a FORWARD STOCKING LOCATION (lean inventory, target 5-10 days supply for STOCKED items)
- Hub is 6 minutes away (2-day replenishment lead time)
- Frequent small transfers from Hub are preferred over large inventory holdings
- Truck capacity: """ + str(truck_capacity['max_volume_ft3']) + """ cubic feet / """ + str(truck_capacity['max_weight_lb']) + """ lb

SKU: """ + str(candidate['sku']) + """
Event Type: """ + str(candidate['event_type']) + """ - """ + event_type_desc + """
Base Urgency: """ + str(candidate['base_urgency']) + """
"""

  # Add CRITICAL distinction for stocked vs non-stocked items
  if is_stocked_item:
    prompt = prompt + """
*** THIS IS A STOCKED ITEM (A-ITEM) ***
- This SKU is regularly stocked at Starlite with defined Min/Max levels
- Goal: Maintain inventory between Min and Max to support ongoing demand
- You MAY recommend buffer stock to maintain forward stocking position
"""
  else:
    prompt = prompt + """
*** THIS IS A NON-STOCKED ITEM ***
- This SKU is NOT regularly stocked at Starlite (no Min/Max levels)
- It only appears because of specific sales order demand
- CRITICAL RULE: Transfer ONLY the exact shortfall amount (calculated_transfer_qty)
- Do NOT add buffer stock or forward stocking inventory
- After fulfilling the sales orders, Starlite should have ~0 excess of this item
- Reason: Non-stocked items tie up capital and space for items not in regular demand
"""

  prompt = prompt + """
CURRENT INVENTORY STATE:
- Starlite On Hand (physical): """ + str(candidate['starlite_on_hand']) + """
- In-Transit from e21 Orders: """ + str(e21_in_transit) + """
- Recently Approved (< 1 hr): """ + str(fresh_approved) + """
- EFFECTIVE ON HAND (total): """ + str(effective_on_hand) + """ ← Use this for decisions
- Starlite Available: """ + str(candidate['starlite_available']) + """
- Hub Available: """ + str(candidate['hub_available']) + """
- Hub On Hand: """ + str(candidate['hub_on_hand']) + """
- Min Stock: """ + str(candidate['min_stock']) + """ """ + ("(stocked item)" if is_stocked_item else "(non-stocked - NO buffer!)") + """
- Max Stock: """ + str(candidate['max_stock']) + """
- Avg Daily Usage: """ + str(candidate['avg_daily_usage']) + """

CRITICAL CONSTRAINTS:
- Your recommended_qty CANNOT exceed Hub Available (""" + str(candidate['hub_available']) + """ units)
- The Hub cannot transfer inventory it does not have
- If demand exceeds Hub supply, recommend the maximum transferable (Hub Available) and note the shortfall in risk_factors
"""

  # Add additional constraint for non-stocked items
  if not is_stocked_item:
    prompt = prompt + """- FOR THIS NON-STOCKED ITEM: recommended_qty should equal calculated transfer qty (""" + str(int(candidate['suggested_transfer_qty'])) + """) - NO BUFFER
"""

  if candidate['event_type'] == 2 and candidate['total_demand']:
    # Get demand_in_horizon, defaulting to total_demand if not available
    demand_in_horizon = candidate.get('demand_in_horizon') or candidate['total_demand']
    prompt = prompt + """
SALES ORDER PRESSURE (Event Type 2):
- Demand in Planning Horizon (""" + str(planning_horizon_days) + """ days): """ + str(int(demand_in_horizon)) + """ units
- Total Demand (all dates): """ + str(candidate['total_demand']) + """ units across """ + str(candidate['order_count']) + """ orders
  (Note: Total includes orders with ship dates far in the future - focus on horizon demand)
- Earliest Ship Date: """ + str(candidate['earliest_ship_date']) + """
- Demand Overdue: """ + str(candidate['demand_overdue']) + """
- Demand 1-2 Days: """ + str(candidate['demand_1_2_days']) + """
- Time Urgency: """ + str(candidate['time_urgency']) + """
- Inventory Urgency: """ + str(candidate['inventory_urgency']) + """
- After Fulfilling Horizon Demand: """ + str(candidate['remaining_after_demand']) + """ units remaining
"""

  prompt = prompt + """
USAGE TRENDS:
- Recent Avg Daily: """ + str(round(usage_trends['recent_avg'], 2)) + """
- Prior Avg Daily: """ + str(round(usage_trends['prior_avg'], 2)) + """
- Trend: """ + usage_trends['trend_direction'] + """ (""" + str(round(usage_trends['trend_pct'], 1)) + """%)

SEASONAL CONTEXT:
- Current Month Factor: """ + str(seasonal['current_factor']) + """x

CALCULATED TRANSFER QUANTITY: """ + str(int(candidate['suggested_transfer_qty'])) + """ units
(This already accounts for in-transit inventory via effective_on_hand)
"""

  # Different task instructions for stocked vs non-stocked
  if is_stocked_item:
    prompt = prompt + """
YOUR TASK (STOCKED ITEM):
1. Evaluate whether the calculated transfer quantity is appropriate
2. Consider: usage trends, seasonal factors, hub sustainability, demand patterns
3. Recommend a transfer quantity (may match calculated or differ based on forward stocking needs)
4. Never exceed Hub Available of """ + str(candidate['hub_available']) + """ units
5. Explain your reasoning in terms a new inventory analyst would understand
6. Include one key learning point about inventory management
"""
  else:
    prompt = prompt + """
YOUR TASK (NON-STOCKED ITEM):
1. Verify the calculated shortfall quantity (""" + str(int(candidate['suggested_transfer_qty'])) + """ units)
2. Your recommended_qty should MATCH the calculated transfer qty - this is a shortfall-only transfer
3. Do NOT add buffer stock - this item is not regularly stocked at Starlite
4. Explain why non-stocked items should only receive shortfall quantities
5. Include a learning point about the difference between stocked and non-stocked item handling
"""

  prompt = prompt + """
Respond in JSON format only:
{
    "recommended_qty": <number - MUST be <= """ + str(candidate['hub_available']) + """>,
    "confidence_level": "High/Medium/Low",
    "reasoning": "<detailed explanation suitable for training, 2-3 paragraphs>",
    "seasonal_consideration": "<how seasonality affects this recommendation>",
    "risk_factors": "<concerns or risks to watch - MUST mention if demand exceeds Hub supply>",
    "learning_point": "<one key inventory principle this situation illustrates>"
}
"""
  return prompt


def call_claude_ai(prompt, max_retries=3):
  """Call Claude API using centralized config with exponential backoff retry.
  
  Retries on transient errors (529 Overloaded, 500 Server Error, etc.)
  with exponential backoff: 2s, 4s, 8s between attempts.
  """
  client = db_config.get_anthropic_client()
  last_exception = None

  for attempt in range(max_retries):
    try:
      response = client.messages.create(
        model=db_config.AI_MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
      )

      response_text = response.content[0].text.strip()

      # Clean markdown code blocks if present
      if response_text.startswith('```'):
        lines = response_text.split('\n')
        lines = [l for l in lines if not l.startswith('```')]
        response_text = '\n'.join(lines)

      ai_result = json.loads(response_text)

      if 'recommended_qty' in ai_result:
        ai_result['recommended_qty'] = float(ai_result['recommended_qty'])

      return ai_result

    except json.JSONDecodeError as e:
      # Don't retry parse errors - the API responded, just not valid JSON
      return {
        'recommended_qty': 0,
        'confidence_level': 'Low',
        'reasoning': 'AI response parsing error: ' + str(e),
        'seasonal_consideration': '',
        'risk_factors': 'Response was not valid JSON',
        'learning_point': ''
      }
    except Exception as e:
      last_exception = e
      error_str = str(e)

      # Check if this is a retryable error (529 Overloaded, 500, 502, 503)
      is_retryable = any(code in error_str for code in ['529', '500', '502', '503', 'overloaded', 'Overloaded'])

      if is_retryable and attempt < max_retries - 1:
        wait_time = 2 ** (attempt + 1)  # 2s, 4s, 8s
        print(f"AI API attempt {attempt + 1}/{max_retries} failed (retryable): {error_str}. Retrying in {wait_time}s...")
        time.sleep(wait_time)
        continue
      else:
        # Non-retryable error or final attempt exhausted
        retry_note = f' (after {attempt + 1} attempts)' if attempt > 0 else ''
        return {
          'recommended_qty': 0,
          'confidence_level': 'Low',
          'reasoning': 'Titan AI API error' + retry_note + ': ' + error_str,
          'seasonal_consideration': '',
          'risk_factors': 'Could not reach AI service',
          'learning_point': ''
        }

  # Should not reach here, but safety net
  return {
    'recommended_qty': 0,
    'confidence_level': 'Low',
    'reasoning': 'AI API error (exhausted ' + str(max_retries) + ' retries): ' + str(last_exception),
    'seasonal_consideration': '',
    'risk_factors': 'Could not reach AI service after retries',
    'learning_point': ''
  }


# =============================================================================
# v5.11 BUSINESS-HOURS GATE HELPERS
# =============================================================================
# Anvil's scheduled-tasks UI supports a fixed cadence ("every 15 min") but
# has no start/stop window, so the task fires 24x7. We only want it to run
# between 06:00 and 19:00 America/New_York. These two helpers let
# run_transfer_review short-circuit off-hours firings with near-zero cost,
# and log state transitions (option C — one row when entering off-hours,
# one row when resuming) so ai_agent_log doesn't fill up with ~68 skip rows
# per day.


def _is_in_business_hours_et(now_utc=None):
  """v5.11: Return True if `now_utc` falls inside the business-hours
  window used by run_transfer_review (06:00 <= local ET hour < 19:00),
  seven days a week. DST-aware via zoneinfo (Python 3.9+) with a pytz
  fallback for older runtimes. Both libraries handle the spring-forward
  and fall-back transitions without us having to track them manually.

  Parameter:
    now_utc: optional datetime in UTC. If None, uses datetime.now(UTC).
             Accepts naive datetimes and treats them as UTC (defensive
             for callers that forget tzinfo).
  """
  from datetime import timezone
  try:
    from zoneinfo import ZoneInfo
    et = ZoneInfo('America/New_York')
  except ImportError:
    # Anvil runtimes older than Python 3.9 won't have zoneinfo; pytz is
    # widely available. If neither is present we intentionally let the
    # ImportError propagate — silently falling back to UTC-4/UTC-5 would
    # break on DST transitions without warning.
    import pytz
    et = pytz.timezone('America/New_York')

  if now_utc is None:
    now_utc = datetime.now(timezone.utc)
  elif now_utc.tzinfo is None:
    now_utc = now_utc.replace(tzinfo=timezone.utc)

  local = now_utc.astimezone(et)
  return 6 <= local.hour < 19


def _prior_run_was_off_hours(conn):
  """v5.11: Inspect ai_agent_log for the most recent row written by
  run_transfer_review's gate path (Transfer_Review_Batch for a completed
  on-hours run, Transfer_Review_Skipped_Off_Hours for an off-hours
  transition). Lets the gate detect transitions without persisting
  state in a new table or global parameter.

  Returns:
    True  — last relevant row is Transfer_Review_Skipped_Off_Hours,
            so the prior firing was during an off-hours window.
    False — last relevant row is Transfer_Review_Batch, so the prior
            firing ran normally (was in on-hours).
    None  — no prior row at all (fresh install, log cleared, etc.).
            Callers treat None like False: the first firing after
            install should not spam a transition row.
  """
  cursor = conn.cursor()
  cursor.execute("""
    SELECT TOP 1 action_type
    FROM   dbo.ai_agent_log
    WHERE  action_type IN ('Transfer_Review_Batch',
                           'Transfer_Review_Skipped_Off_Hours')
    ORDER BY action_timestamp DESC
  """)
  row = cursor.fetchone()
  if row is None:
    return None
  return row['action_type'] == 'Transfer_Review_Skipped_Off_Hours'


# =============================================================================
# v5.13 DATA-FRESHNESS GATE
# =============================================================================
# EasyMorph loads e21 data into Nexus by clearing then repopulating each
# target table. If a step in the middle deadlocks (as happened
# 2026-05-05 on the import-from-database action), the live table is
# left empty. The view vw_unified_transfer_detection then reports "no
# SKUs need transfer," and mark_stale_recommendations_obsolete dutifully
# obsoletes every Pending/Paused rec.
#
# This gate runs immediately AFTER the v5.11 business-hours gate so
# off-hours skips still log correctly, but BEFORE any destructive work.
# Strict-zero threshold per Dave 2026-05-05: any of the gated tables
# at row count 0 = skip.

def _get_freshness_threshold_minutes(conn):
  """v5.13.1: Read the loader-freshness threshold from global_parameters.

  Default 60 minutes: EasyMorph runs sales_orders_due every 15 minutes,
  so 60 min = four cycles of buffer. Anything older than that means the
  loader has missed at least three consecutive cycles, which is well past
  any single transient deadlock. Returns int minutes.

  Override the default by inserting/updating a row in dbo.global_parameters
  with parameter_name = 'sales_orders_due_freshness_minutes'.
  """
  cursor = conn.cursor()
  cursor.execute("""
    SELECT parameter_value
    FROM   dbo.global_parameters
    WHERE  parameter_name = 'sales_orders_due_freshness_minutes'
  """)
  row = cursor.fetchone()
  if row and row['parameter_value'] is not None:
    try:
      return int(row['parameter_value'])
    except (TypeError, ValueError):
      pass
  return 60


def _check_view_data_freshness(conn):
  """v5.13: Verify the view's input tables are populated.
  v5.13.1: Also verify they're not stale (last refresh inside threshold).

  Two failure modes share one gate:
    - Empty: COUNT(*) = 0 on any gated table. EasyMorph crashed mid-load
      and left the table truncated.
    - Stale: MAX(timestamp_column) is older than the configured threshold.
      EasyMorph crashed BEFORE its next refresh fired, so the table is
      non-empty but holding a snapshot from before the failure. This is
      the failure mode that bit us on 2026-05-12: 2,666 rows from 7:19 AM
      EDT still in place at 3:10 PM EDT when the cleanup sweep ran.

  Staleness comparison is done SQL-side via DATEDIFF + GETDATE() so the
  Anvil server's local time and the SQL Server's local time can't drift.

  Returns (is_fresh: bool, details: dict) where details has:
    - 'counts':            {table_name: row_count, ...}
    - 'empty':             list of table_names with count == 0
    - 'age_minutes':       {table_name: int_or_None, ...}
                           None when no timestamp column or no rows.
    - 'stale':             list of table_names whose age exceeds threshold
    - 'threshold_minutes': int — the threshold in effect this firing

  is_fresh is False if any table is empty OR any table is stale.
  """
  cursor = conn.cursor()
  counts = {}
  age_minutes = {}
  for entry in _FRESHNESS_TABLES:
    table_name = entry[0]
    ts_col = entry[2] if len(entry) > 2 else None
    # Hardcoded table list (module constant), so f-string SQL interpolation
    # is safe here — no user input on this path.
    cursor.execute(f"SELECT COUNT(*) AS cnt FROM {table_name}")
    counts[table_name] = int(cursor.fetchone()['cnt'])

    age = None
    if ts_col is not None and counts[table_name] > 0:
      cursor.execute(
        f"SELECT DATEDIFF(MINUTE, MAX({ts_col}), GETDATE()) AS age_min "
        f"FROM {table_name}"
      )
      row = cursor.fetchone()
      if row and row['age_min'] is not None:
        age = int(row['age_min'])
    age_minutes[table_name] = age

  threshold = _get_freshness_threshold_minutes(conn)
  empty = [t for t, c in counts.items() if c == 0]
  stale = [t for t, a in age_minutes.items() if a is not None and a > threshold]

  return (
    len(empty) == 0 and len(stale) == 0,
    {
      'counts':            counts,
      'empty':             empty,
      'age_minutes':       age_minutes,
      'stale':             stale,
      'threshold_minutes': threshold,
    },
  )


# =============================================================================
# MAIN BACKGROUND TASK
# =============================================================================

@anvil.server.background_task
def run_transfer_review():
  """
  Main background task for transfer review.
  Runs on schedule or on-demand to analyze transfer candidates.

  v5.11: The very first thing this function does is check the business-
  hours gate. Off-hours firings short-circuit before any expensive work
  (no get_transfer_candidates query, no AI calls, no rec writes). The
  only cost of an off-hours firing is a single SQL SELECT to check the
  prior-run state.
  """
  # ─────────────────────────────────────────────────────────────────────
  # v5.11: Business-hours gate
  # ─────────────────────────────────────────────────────────────────────
  # Open a dedicated connection for the gate so we can short-circuit
  # cleanly before the main try-block opens its own. On an on-hours run
  # this connection is closed immediately and the existing code opens
  # its own conn normally — the two-open pattern is cheap and keeps the
  # gate self-contained.
  _gate_conn = None
  try:
    _gate_conn = get_connection()
    _in_hours = _is_in_business_hours_et()
    _prior_was_off = _prior_run_was_off_hours(_gate_conn)

    if not _in_hours:
      # Off-hours firing. Only log if this is the first skip after
      # being on-hours (i.e. we're crossing the 19:00 ET boundary).
      # If the prior firing was already off-hours, stay silent.
      # If there's no prior row at all (None), also stay silent —
      # we don't want a transition log on fresh install.
      if _prior_was_off is False:
        log_action(
          _gate_conn,
          'Transfer_Review_Skipped_Off_Hours',
          None,
          '',
          'Off-hours window began',
          datetime.now().strftime('%Y-%m-%d %H:%M UTC') + f' (v{__version__})'
        )
      anvil.server.task_state['status'] = 'Skipped - off hours'
      return {'status': 'skipped_off_hours'}

    # On-hours firing. If the prior firing was off-hours, we just
    # crossed the 06:00 ET boundary — log the resume. Then fall
    # through to the normal review flow below.
    if _prior_was_off is True:
      log_action(
        _gate_conn,
        'Transfer_Review_Resumed',
        None,
        '',
        'Back in business-hours window',
        datetime.now().strftime('%Y-%m-%d %H:%M UTC') + f' (v{__version__})'
      )
  finally:
    if _gate_conn is not None:
      _gate_conn.close()
  # ─────────────────────────────────────────────────────────────────────
  # End v5.11 gate
  # ─────────────────────────────────────────────────────────────────────

  # ─────────────────────────────────────────────────────────────────────
  # v5.13: Data-freshness gate  (v5.13.1: now covers stale-but-non-empty)
  # ─────────────────────────────────────────────────────────────────────
  # Refuse to run when the view's input tables are mid-load, empty, or
  # holding stale data. Two failure modes share this gate:
  #   - empty: COUNT(*) = 0 on any gated table (EasyMorph mid-truncate).
  #   - stale: most recent row in sales_orders_due is older than the
  #            configured threshold (default 60 min). Covers the 2026-05-12
  #            scenario where EasyMorph crashed but left the prior good
  #            snapshot in place, fooling the empty check.
  # Auto-resumes on the next firing once data is back / fresh. One skip
  # log row per failed firing — these should be loud because each one
  # represents a real upstream failure.
  _fresh_conn = None
  try:
    _fresh_conn = get_connection()
    _is_fresh, _fresh_info = _check_view_data_freshness(_fresh_conn)
    if not _is_fresh:
      _empty_csv = ', '.join(_fresh_info['empty']) if _fresh_info['empty'] else ''
      _stale_csv = ', '.join(
        f"{t}={_fresh_info['age_minutes'][t]}min"
        for t in _fresh_info['stale']
      )
      _counts_str = ', '.join(
        f"{t}={c}" for t, c in _fresh_info['counts'].items()
      )

      _reason_parts = []
      if _empty_csv:
        _reason_parts.append(f"Empty: {_empty_csv}")
      if _stale_csv:
        _reason_parts.append(
          f"Stale (>{_fresh_info['threshold_minutes']} min): {_stale_csv}"
        )
      _reason = '; '.join(_reason_parts)

      log_action(
        _fresh_conn,
        'Transfer_Review_Skipped_Stale_Data',
        None,
        '',
        _reason,
        f"counts: {_counts_str} (v{__version__})"
      )
      anvil.server.task_state['status'] = 'Skipped - upstream data stale'
      anvil.server.task_state['freshness'] = _fresh_info
      return {
        'status':       'skipped_stale_data',
        'empty_tables': _fresh_info['empty'],
        'stale_tables': _fresh_info['stale'],
        'age_minutes':  _fresh_info['age_minutes'],
        'counts':       _fresh_info['counts'],
      }
  finally:
    if _fresh_conn is not None:
      _fresh_conn.close()
  # ─────────────────────────────────────────────────────────────────────
  # End v5.13 gate — from here on is the original run_transfer_review body.
  # ─────────────────────────────────────────────────────────────────────

  batch_id = datetime.now().strftime('%Y%m%d') + '-' + str(uuid.uuid4())[:8]

  anvil.server.task_state['status'] = 'Starting'
  anvil.server.task_state['batch_id'] = batch_id

  results = {
    'auto_approved': 0,
    'ai_reviewed': 0,
    'updated': 0,      # Track updated vs new
    'updated_with_changes': 0,  # Track updates that had meaningful changes
    'inserted': 0,     # Track inserted vs updated
    'obsoleted': 0,    # Track stale recommendations marked obsolete
    'skipped': 0,
    'errors': 0,
    'email_sent': False
  }

  try:
    conn = get_connection()

    candidates = get_transfer_candidates(conn)
    anvil.server.task_state['total_candidates'] = len(candidates)

    if not candidates:
      anvil.server.task_state['status'] = 'Complete - No candidates'
      log_action(conn, 'Transfer_Review_Batch', None, '0', 'No candidates found', batch_id)
      conn.close()
      return results

    # Get shared context
    truck_capacity = get_truck_capacity(conn)
    seasonal = get_seasonal_context(conn)
    # v5.6-fix: pull planning horizon from global_parameters so the AI prompt
    # label matches whatever vw_unified_transfer_detection actually used.
    planning_horizon_days = get_planning_horizon_days(conn)

    # =========================================================================
    # Mark stale Pending/Paused recommendations as Obsolete
    # These are recommendations where conditions have changed and the SKU
    # is no longer in the view (orders fulfilled, inventory received, etc.)
    # =========================================================================
    anvil.server.task_state['status'] = 'Checking stale recommendations'
    stale_result = mark_stale_recommendations_obsolete(conn, batch_id)

    if stale_result['obsoleted'] > 0:
      log_action(conn, 'Stale_Recommendations_Obsoleted', None,
                 f"Count: {stale_result['obsoleted']}", 
                 f"SKUs: {', '.join(stale_result['skus'][:10])}{'...' if len(stale_result['skus']) > 10 else ''}",
                 f'Batch:{batch_id}')

    results['obsoleted'] = stale_result['obsoleted']

    # ==========================================================================
    # TRAINING MODE: All transfers go to AI review for Hub Manager approval
    # When ready for full automation, uncomment AUTO_APPROVE section in db_config
    # and restore the auto-approve logic here.
    # ==========================================================================

    # All candidates go to AI review (no auto-approve during training)
    ai_review_candidates = candidates

    # v5.5: Run one batch query to detect duplicate-risk candidates and
    # annotate each candidate dict with has_duplicate_warning / warning_text.
    # Done BEFORE the per-candidate loop so the warning text flows into
    # both save_recommendation and update_recommendation paths uniformly.
    anvil.server.task_state['status'] = 'Checking duplicate warnings'
    try:
      duplicate_warnings = check_duplicate_warnings_batch(conn, ai_review_candidates)
    except Exception as dup_err:
      # Fail-open: a warning-check failure must not block recommendations.
      log_action(conn, 'Duplicate_Warning_Check_Error', None,
                 str(dup_err), '', 'Batch:' + batch_id)
      duplicate_warnings = {}

    duplicate_warnings_count = 0
    for candidate in ai_review_candidates:
      w = duplicate_warnings.get(candidate['sku'])
      if w:
        candidate['has_duplicate_warning'] = True
        candidate['duplicate_warning_text'] = w['warning_text']
        duplicate_warnings_count += 1
      else:
        candidate['has_duplicate_warning'] = False
        candidate['duplicate_warning_text'] = ''
    results['duplicate_warnings'] = duplicate_warnings_count

    # AI Review ALL candidates
    anvil.server.task_state['status'] = 'AI reviewing'

    for i, candidate in enumerate(ai_review_candidates):
      sku = candidate['sku']

      anvil.server.task_state['current_sku'] = sku
      anvil.server.task_state['progress'] = str(i + 1) + '/' + str(len(ai_review_candidates))

      # ─────────────────────────────────────────────────────────────────────
      # v5.10: Hub-side short-circuit
      # ─────────────────────────────────────────────────────────────────────
      # v5.6 wired virtual Hub depletion into the view so
      # hub_effective_on_hand correctly reports zero (or negative) when
      # in-pipeline transfers have already claimed all of the Hub's
      # physical supply for a SKU. But the generator never consumed that
      # signal — it just capped rec_qty at raw hub_available, which
      # produced "duplicate" recs for SKUs the Hub literally cannot
      # fulfill more of.
      #
      # Option 2 (Dave-selected 2026-04-23): don't cap, don't invoke the
      # AI at all. Skip the SKU for this batch. One log row per skip
      # makes it auditable without creating a rec that will just have
      # to be rejected.
      #
      # Defensive: treat NULL as "unknown — proceed normally." Only skip
      # when we have a concrete non-positive value from the view. Stale
      # Pending/Paused recs for this SKU are left alone — they'll age
      # out via mark_stale_recommendations_obsolete on a later batch
      # once the view stops surfacing the SKU, OR the Hub Manager can
      # reject them manually.
      hub_eff = candidate.get('hub_effective_on_hand')
      if hub_eff is not None and float(hub_eff) <= 0:
        total_demand = candidate.get('total_demand') or 0
        starlite_on_hand = candidate.get('starlite_on_hand') or 0
        hub_on_hand = candidate.get('hub_on_hand') or 0
        e21_in_transit = candidate.get('e21_in_transit_qty') or 0
        skip_reason = (
          f"hub_effective_on_hand={hub_eff} "
          f"(hub_on_hand={hub_on_hand}, e21_in_transit={e21_in_transit}, "
          f"demand={total_demand}, starlite_on_hand={starlite_on_hand}); "
          f"in-flight pipeline already exhausts Hub supply "
          f"(v{__version__})"
        )
        log_action(
          conn,
          'AI_REVIEW_SKIPPED_NO_HUB_SUPPLY',
          sku,
          f"Type:{candidate.get('event_type')}",
          'Skipped',
          skip_reason
        )
        results['skipped'] += 1
        continue
      # ─────────────────────────────────────────────────────────────────────
      # End v5.10 short-circuit
      # ─────────────────────────────────────────────────────────────────────

      # Check for existing Pending or Paused recommendation
      existing = get_existing_recommendation(conn, sku)

      try:
        usage_trends = get_usage_trends(conn, sku)
        hub_trajectory = get_hub_trajectory(conn, sku)
        part_specs = get_part_specs(conn, sku)

        prompt = build_ai_prompt(candidate, usage_trends, seasonal,
                                 hub_trajectory, part_specs, truck_capacity,
                                 planning_horizon_days)

        ai_result = call_claude_ai(prompt)

        # HARD CAP ENFORCEMENT (defense in depth - also enforced in save/update)
        hub_available = float(candidate['hub_available'] or 0)
        if ai_result.get('recommended_qty', 0) > hub_available:
          ai_result['recommended_qty'] = hub_available
          ai_result['risk_factors'] = (ai_result.get('risk_factors', '') + 
                                       ' [CAPPED: AI recommendation was reduced to Hub Available limit]')

        if existing['exists']:
          # UPDATE existing record with fresh data
          had_changes = update_recommendation(conn, existing['recommendation_id'], 
                                              candidate, ai_result, batch_id, existing['status'])

          if had_changes:
            log_action(conn, 'Transfer_Recommendation_Updated', sku,
                       'Prev:' + str(existing['previous_ai_qty']) + ', Status:' + existing['status'],
                       'New:' + str(ai_result.get('recommended_qty')) + ', Calc:' + str(candidate['suggested_transfer_qty']),
                       'Batch:' + batch_id)
            results['updated_with_changes'] += 1
          # If no changes, we still updated the record but don't log (reduces noise)

          results['updated'] += 1
        else:
          # INSERT new record
          save_recommendation(conn, candidate, ai_result, batch_id, status='Pending')

          log_action(conn, 'Transfer_Recommendation', sku,
                     'Type:' + str(candidate['event_type']) + ', Calc:' + str(candidate['suggested_transfer_qty']),
                     'AI:' + str(ai_result.get('recommended_qty')) + ', Conf:' + str(ai_result.get('confidence_level')),
                     'Batch:' + batch_id)

          results['inserted'] += 1

        results['ai_reviewed'] += 1

      except Exception as e:
        results['errors'] += 1
        log_action(conn, 'Transfer_Review_Error', sku, 'AI review', str(e), batch_id)

    anvil.server.task_state['status'] = 'Complete'
    anvil.server.task_state['results'] = results

    # Summary for training mode (no auto-approve).
    # v5.10: include Skip: count so the operator can see the short-circuit
    # fired. Non-zero Skip with zero DupWarn means v5.10 caught cases that
    # would previously have become has_duplicate_warning=true noise.
    summary = ('AI:' + str(results['ai_reviewed']) +
               ' (New:' + str(results['inserted']) +
               ', Upd:' + str(results['updated']) +
               ', Chg:' + str(results['updated_with_changes']) +
               ', Obs:' + str(results.get('obsoleted', 0)) +
               ', Skip:' + str(results.get('skipped', 0)) +
               ', DupWarn:' + str(results.get('duplicate_warnings', 0)) + ')' +
               ', Err:' + str(results['errors']))
    log_action(conn, 'Transfer_Review_Batch', None, 'Candidates:' + str(len(candidates)), summary, 'Batch:' + batch_id)

    # =========================================================================
    # SMS NOTIFICATION
    # Send SMS to Hub Managers if there are new recommendations to review
    # =========================================================================
    sms_gate = results['ai_reviewed'] > 0
    print(f"[SMS DEBUG] Gate check: ai_reviewed={results['ai_reviewed']}, "
          f"inserted={results['inserted']}, updated_with_changes={results['updated_with_changes']}, "
          f"gate={'PASS' if sms_gate else 'FAIL'}")

    if sms_gate:
      try:
        from . import sms_notifications
        print("[SMS DEBUG] sms_notifications module imported successfully")

        # Dashboard URL for SMS notifications.
        # Pulled from dbo.global_parameters_text so it can be edited
        # at runtime via the system_admin form (no redeploy needed).
        # Fallback is the bit.ly short link in case the DB row is
        # missing or the lookup fails — we never want to block SMS
        # sending on a config read.
        from . import system_settings
        dashboard_url = system_settings.get_text_parameter(
          'dashboard_url', 'https://bit.ly/4txYx5B'
        )

        sms_result = sms_notifications.integrate_sms_notification(
          batch_id, results, dashboard_url
        )
        print(f"[SMS DEBUG] integrate_sms_notification returned: {sms_result}")

        if sms_result:
          results['sms_notification'] = sms_result
          if sms_result.get('sent_count', 0) > 0:
            log_action(conn, 'SMS_Notification_Sent', None,
                       f"Sent: {sms_result.get('sent_count', 0)}/{sms_result.get('sent_count', 0) + sms_result.get('failed_count', 0)}",
                       f"New: {results['inserted']}, Changed: {results['updated_with_changes']}",
                       f'Batch:{batch_id}')
          else:
            # Log WHY SMS didn't send (no recipients, disabled, error, etc.)
            reason = sms_result.get('reason', sms_result.get('error', 'Unknown'))
            log_action(conn, 'SMS_Notification_Skipped', None,
                       f"sent_count=0, success={sms_result.get('success')}",
                       f"Reason: {reason}",
                       f'Batch:{batch_id}')
        else:
          # integrate_sms_notification returned None
          log_action(conn, 'SMS_Notification_Skipped', None,
                     'integrate_sms_notification returned None',
                     f"inserted={results['inserted']}, ai_reviewed={results['ai_reviewed']}",
                     f'Batch:{batch_id}')

      except ImportError as ie:
        # DO NOT silently pass - log it so we know this is the problem
        print(f"[SMS DEBUG] ImportError: {ie}")
        log_action(conn, 'SMS_Notification_Error', None, 
                   f'ImportError: {str(ie)}', 'Module import failed', f'Batch:{batch_id}')
      except Exception as e:
        # Don't fail the whole process if SMS fails
        print(f"[SMS DEBUG] Exception: {e}")
        log_action(conn, 'SMS_Notification_Error', None, str(e), '', f'Batch:{batch_id}')
    else:
      # Gate didn't pass - log WHY so we can diagnose
      log_action(conn, 'SMS_Notification_Skipped', None,
                 f"Gate failed: ai_reviewed={results['ai_reviewed']}",
                 f"inserted={results['inserted']}, updated={results['updated']}, chg={results['updated_with_changes']}",
                 f'Batch:{batch_id}')
    # =========================================================================
    # END SMS NOTIFICATION
    # =========================================================================

    conn.close()

  except Exception as e:
    anvil.server.task_state['status'] = 'Error: ' + str(e)
    results['errors'] += 1

  return results


# =============================================================================
# CALLABLE FUNCTIONS (for manual trigger from UI)
# =============================================================================

@anvil.server.callable
def trigger_transfer_review():
  """Manually trigger a transfer review run."""
  task = anvil.server.launch_background_task('run_transfer_review')
  return {
    'status': 'launched',
    'task_id': task.get_id(),
    'message': 'Transfer review started in background'
  }


@anvil.server.callable
def get_review_task_status(task_id):
  """Get the status of a running review task."""
  try:
    task = anvil.server.get_background_task(task_id)
    return {
      'is_running': task.is_running(),
      'is_completed': task.is_completed(),
      'state': task.get_state()
    }
  except Exception as e:
    return {'error': str(e)}


@anvil.server.callable
def get_last_review_stats():
  """Get statistics from the most recent review run."""
  try:
    conn = get_connection()
    cursor = conn.cursor()

    # Query ai_agent_log for the last batch run (this logs every run, even with no candidates)
    cursor.execute("""
        SELECT TOP 1 reason as batch_id, action_timestamp, new_value as summary
        FROM dbo.ai_agent_log
        WHERE action_type = 'Transfer_Review_Batch'
        ORDER BY action_timestamp DESC
    """)
    row = cursor.fetchone()

    if not row:
      conn.close()
      return {'status': 'no_data', 'message': 'No review runs found'}

    # Extract batch_id from the reason field (format: "Batch:xxxxxxxx")
    batch_id_raw = row['batch_id'] or ''
    if batch_id_raw.startswith('Batch:'):
      batch_id = batch_id_raw.replace('Batch:', '')
    else:
      batch_id = batch_id_raw

    last_run = row['action_timestamp']
    summary = row['summary'] or ''

    # Get current counts by status (not just from the last batch)
    cursor.execute("""
        SELECT status, COUNT(*) as count
        FROM dbo.ai_transfer_recommendations
        WHERE status IN ('Pending', 'Auto-Approved', 'Approved', 'Applied', 'Paused')
        GROUP BY status
    """)

    counts = {}
    for r in cursor.fetchall():
      counts[r['status']] = r['count']

    conn.close()

    return {
      'status': 'success',
      'batch_id': batch_id,
      'last_run': make_serializable(last_run),
      'last_run_summary': summary,
      'auto_approved': counts.get('Auto-Approved', 0),
      'pending': counts.get('Pending', 0),
      'approved': counts.get('Approved', 0),
      'applied': counts.get('Applied', 0),
      'paused': counts.get('Paused', 0)
    }

  except Exception as e:
    return {'status': 'error', 'message': str(e)}