"""
forecast_display.py - Anvil Server Module
===========================================
Read-only server module powering the Forecasts section of the Anvil app.

Supports these forms (being added incrementally):
  - forecast_sku_detail       (SKU-level drill-down: history + forecast + actuals)
  - forecast_dashboard        (list of forecast_batches + per-row Excel export)
  - forecast_batch_results    (SKU-level forecast grid for a given batch)
  - forecast_accuracy         (WMAPE + variance analysis)

All functions are @anvil.server.callable (client-callable).
The only "write" operation is `get_purchasing_export` which returns an
xlsx BlobMedia for download — generation is still done by the console app.

Uses the existing db_config module for Nexus connectivity.

Indentation note: this file uses 2-space indent to match the Anvil IDE's
convention. Keep new edits in 2-space so the workspace-canonical and
deployed versions stay aligned.
"""

import anvil.server
import anvil.media
import io
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from . import db_config


# Material-change thresholds for the Purchasing export.
# A SKU is flagged "Material Change" only when BOTH conditions are met:
#   |delta units|        > MATERIAL_DELTA_UNITS
#   |delta / baseline|   > MATERIAL_DELTA_PCT
MATERIAL_DELTA_UNITS = 50
MATERIAL_DELTA_PCT = 0.10  # 10%


# =============================================================================
# HELPERS
# =============================================================================

def _month_key(year, month):
  """Return 'YYYY-MM' string for a year/month pair."""
  return f'{int(year):04d}-{int(month):02d}'


def _serialize(row):
  """Convert pymssql row dict types (Decimal, datetime) for JSON transport."""
  out = {}
  for k, v in row.items():
    if isinstance(v, datetime):
      out[k] = v.isoformat()
    elif hasattr(v, '__float__') and not isinstance(v, bool):
      try:
        out[k] = float(v)
      except (TypeError, ValueError):
        out[k] = str(v)
    else:
      out[k] = v
  return out


def _fmt(v):
  """Format a numeric value to 2 decimal places as a string.
  Returns None (renders blank in grid) when v is None."""
  if v is None:
    return None
  try:
    return f'{float(v):.2f}'
  except (TypeError, ValueError):
    return None


# =============================================================================
# SKU DETAIL — used by forecast_sku_detail form
# =============================================================================

@anvil.server.callable
def search_skus_for_forecast(search_term, limit=25):
  """
  Search SKUs by partial match. Returns list of (sku, description) tuples
  suitable for a DropDown's items property.
  """
  if not search_term or not search_term.strip():
    return []

  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
      SELECT TOP (%s) p.sku, ISNULL(p.part_desc, '') AS description
      FROM dbo.part_specs p
      WHERE p.sku LIKE %s
      ORDER BY p.sku
      """,
      (limit, f'%{search_term.strip()}%'),
    )
    rows = cursor.fetchall()
    # Anvil DropDown items: list of (label, value)
    return [(f"{r['sku']} — {r['description']}"[:80], r['sku']) for r in rows]
  finally:
    conn.close()


@anvil.server.callable
def get_forecast_batches(year=None, limit=100):
  """
  Return forecast batches for the Forecast Dashboard grid, most recent first.
  Optional year filter (int or None).

  Returns:
    {
      'status': 'success',
      'batches': [ {batch_id, target_year, status, sku_count, series_count,
                    training_start, training_end, created_date, created_by,
                    notes}, ... ],
      'count': int,
      'years': [2026, 2025, ...]   # distinct target_years for the dropdown
    }
  """
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()

    # Distinct years for the filter dropdown
    cursor.execute(
      """
      SELECT DISTINCT target_year
      FROM dbo.forecast_batches
      ORDER BY target_year DESC
      """
    )
    years = [int(r['target_year']) for r in cursor.fetchall()]

    # Batches with optional year filter
    if year is not None:
      cursor.execute(
        """
        SELECT TOP (%s)
            batch_id, target_year, status, sku_count, series_count,
            training_start, training_end, created_date, created_by,
            ISNULL(notes, '') AS notes
        FROM dbo.forecast_batches
        WHERE target_year = %s
        ORDER BY created_date DESC
        """,
        (limit, int(year)),
      )
    else:
      cursor.execute(
        """
        SELECT TOP (%s)
            batch_id, target_year, status, sku_count, series_count,
            training_start, training_end, created_date, created_by,
            ISNULL(notes, '') AS notes
        FROM dbo.forecast_batches
        ORDER BY created_date DESC
        """,
        (limit,),
      )

    batches = []
    for r in cursor.fetchall():
      batches.append({
        'batch_id': r['batch_id'],
        'target_year': int(r['target_year']) if r['target_year'] is not None else None,
        'status': r['status'],
        'sku_count': int(r['sku_count']) if r['sku_count'] is not None else None,
        'series_count': int(r['series_count']) if r['series_count'] is not None else None,
        'training_start': r['training_start'].strftime('%Y-%m-%d') if r['training_start'] else None,
        'training_end': r['training_end'].strftime('%Y-%m-%d') if r['training_end'] else None,
        'created_date': r['created_date'].strftime('%Y-%m-%d %H:%M') if r['created_date'] else None,
        'created_by': r['created_by'],
        'notes': r['notes'] or '',
      })

    return {
      'status': 'success',
      'batches': batches,
      'count': len(batches),
      'years': years,
    }
  finally:
    conn.close()


@anvil.server.callable
def get_batch_series_list(batch_id):
  """
  Return the distinct series present in a specific batch, for the series
  filter dropdown on the Batch Results Viewer form. Scoping to the batch
  means users only see series that actually have data to show.

  Returns:
    { 'status': 'success', 'series': ['BF76E-DI', 'CV51-SS', ...] }
  """
  if not batch_id:
    return {'status': 'success', 'series': []}

  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
      SELECT DISTINCT series
      FROM dbo.forecast_sku_results
      WHERE forecast_batch = %s AND series IS NOT NULL
        AND LTRIM(RTRIM(series)) <> ''
      ORDER BY series
      """,
      (batch_id,),
    )
    series = [r['series'].strip() for r in cursor.fetchall() if r.get('series')]
    return {'status': 'success', 'series': series}
  finally:
    conn.close()


@anvil.server.callable
def get_forecast_batch_results(batch_id, sku=None, series=None, month=None, limit=500):
  """
  Return SKU-level forecast results for a given batch, with optional filters.

  Filters:
    - sku: partial-match filter
    - series: exact match
    - month: int 1-12 (forecast_month), or None for all months

  Returns:
    {
      'status': 'success',
      'rows': [ {sku, series, forecast_month, forecast_qty (2dp string),
                 forecast_lower (2dp), forecast_upper (2dp), model_used,
                 confidence_level (2dp)}, ... ],
      'count': int,
      'truncated': bool,    # True if result set hit the limit
      'target_year': int,
    }
  """
  if not batch_id:
    return {'status': 'error', 'error': 'batch_id is required'}

  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()

    # Get the target_year for the header display
    cursor.execute(
      'SELECT target_year FROM dbo.forecast_batches WHERE batch_id = %s',
      (batch_id,),
    )
    batch_row = cursor.fetchone()
    if not batch_row:
      return {'status': 'error', 'error': f"Batch '{batch_id}' not found"}
    target_year = int(batch_row['target_year'])

    # Build filtered query for results
    conditions = ['forecast_batch = %s']
    params = [batch_id]

    if sku and sku.strip():
      conditions.append('sku LIKE %s')
      params.append(f'%{sku.strip()}%')
    if series and series.strip() and series != 'All':
      conditions.append('series = %s')
      params.append(series.strip())
    if month is not None:
      try:
        m = int(month)
        if 1 <= m <= 12:
          conditions.append('forecast_month = %s')
          params.append(m)
      except (ValueError, TypeError):
        pass

    where = ' AND '.join(conditions)

    # Fetch one more than limit so we can flag truncation
    cursor.execute(
      f"""
      SELECT TOP ({limit + 1})
          sku, series, forecast_year, forecast_month,
          forecast_qty, forecast_lower, forecast_upper,
          model_used, confidence_level
      FROM dbo.forecast_sku_results
      WHERE {where}
      ORDER BY series, sku, forecast_month
      """,
      tuple(params),
    )
    raw_rows = cursor.fetchall()
    truncated = len(raw_rows) > limit
    if truncated:
      raw_rows = raw_rows[:limit]

    rows = []
    for r in raw_rows:
      rows.append({
        'sku': r['sku'],
        'series': r['series'] or '',
        'forecast_month': int(r['forecast_month']) if r['forecast_month'] is not None else None,
        'forecast_qty': _fmt(r['forecast_qty']),
        'forecast_lower': _fmt(r['forecast_lower']),
        'forecast_upper': _fmt(r['forecast_upper']),
        'model_used': r['model_used'] or '',
        'confidence_level': _fmt(r['confidence_level']),
      })

    return {
      'status': 'success',
      'rows': rows,
      'count': len(rows),
      'truncated': truncated,
      'target_year': target_year,
    }
  finally:
    conn.close()


@anvil.server.callable
def get_accuracy_comparison(batch_id, from_month=None, to_month=None,
                            series=None, outliers_only=False):
  """
  Compare forecasts vs actuals on-the-fly for the given batch and month range.

  Joins dbo.forecast_sku_results with dbo.usage_data. Computes per-row
  variance + outlier flag, then aggregate metrics: bias, MAPE, WMAPE,
  WMAPE excluding outliers. Also rolls up by series and prepares a
  scatter-plot trace for visualization.

  Outlier rule (from the console script):
    (actual > forecast*3 AND |variance| > 50) OR
    (forecast > actual*3 AND |variance| > 50 AND forecast > 50)

  Returns:
    {
      'status': 'success',
      'summary': {...},
      'detail_rows': [...],
      'series_rollup': [...],
      'scatter_data': {...},
    }
  """
  if not batch_id:
    return {'status': 'error', 'error': 'batch_id is required'}

  # Default month range 1-12
  try:
    m_from = int(from_month) if from_month is not None else 1
    m_to = int(to_month) if to_month is not None else 12
  except (ValueError, TypeError):
    return {'status': 'error', 'error': 'Invalid month range'}
  if not (1 <= m_from <= 12 and 1 <= m_to <= 12 and m_from <= m_to):
    return {'status': 'error', 'error': 'Month range must be 1-12 with from <= to'}

  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()

    # Batch metadata (target_year used to filter usage_data)
    cursor.execute(
      'SELECT target_year FROM dbo.forecast_batches WHERE batch_id = %s',
      (batch_id,),
    )
    batch_row = cursor.fetchone()
    if not batch_row:
      return {'status': 'error', 'error': f"Batch '{batch_id}' not found"}
    target_year = int(batch_row['target_year'])

    # Build the joined query. Aggregate usage_data in a CTE first, then
    # left-join to forecast_sku_results. ISNULL(actual_qty, 0) handles
    # months where a SKU had forecast but no sales.
    params = [target_year, m_from, m_to, batch_id, m_from, m_to]
    series_clause = ''
    if series and series.strip() and series != 'All':
      series_clause = ' AND f.series = %s'
      params.append(series.strip())

    sql = f"""
      WITH actuals AS (
          SELECT sku,
                 MONTH(usage_date) AS m,
                 SUM(qty_sold) AS actual_qty
          FROM dbo.usage_data
          WHERE YEAR(usage_date) = %s
            AND MONTH(usage_date) BETWEEN %s AND %s
          GROUP BY sku, MONTH(usage_date)
      )
      SELECT
          f.sku, f.series, f.forecast_month,
          f.forecast_qty,
          ISNULL(a.actual_qty, 0) AS actual_qty
      FROM dbo.forecast_sku_results f
      LEFT JOIN actuals a
             ON f.sku = a.sku AND f.forecast_month = a.m
      WHERE f.forecast_batch = %s
        AND f.forecast_month BETWEEN %s AND %s
        {series_clause}
      ORDER BY f.series, f.sku, f.forecast_month
    """
    cursor.execute(sql, tuple(params))
    raw = cursor.fetchall()
  finally:
    conn.close()

  if not raw:
    return {
      'status': 'success',
      'summary': {
        'target_year': target_year,
        'evaluated_months': f'{m_from}-{m_to}',
        'sku_month_count': 0,
        'outlier_count': 0,
        'total_forecast': '0.00',
        'total_actual': '0.00',
        'bias': '0.00',
        'mape': None,
        'wmape': None,
        'wmape_no_outliers': None,
      },
      'detail_rows': [],
      'series_rollup': [],
      'scatter_data': {'plot_data': [], 'plot_layout': {
        'title': 'No data available for this selection',
      }},
    }

  # ------- Per-row computation & aggregation -------
  detail_rows = []
  total_forecast = 0.0
  total_actual = 0.0
  total_abs_error = 0.0
  total_abs_error_no_outliers = 0.0
  total_actual_no_outliers = 0.0
  mape_sum = 0.0
  mape_count = 0
  outlier_count = 0

  series_agg = {}  # series -> {'forecast': 0, 'actual': 0, 'abs_error': 0, 'skus': set()}
  scatter_normal_x, scatter_normal_y = [], []
  scatter_outlier_x, scatter_outlier_y = [], []
  max_value = 0.0

  def _outlier_rule(forecast, actual):
    absv = abs(actual - forecast)
    if actual > forecast * 3 and absv > 50:
      return True
    if forecast > actual * 3 and absv > 50 and forecast > 50:
      return True
    return False

  for r in raw:
    f_qty = float(r['forecast_qty'] or 0)
    a_qty = float(r['actual_qty'] or 0)
    variance = a_qty - f_qty
    abs_variance = abs(variance)
    is_outlier = _outlier_rule(f_qty, a_qty)

    # Variance % relative to forecast (None if forecast is 0)
    if f_qty != 0:
      variance_pct = (variance / f_qty) * 100.0
      accuracy_pct = max(0.0, 100.0 - abs(variance_pct))
    else:
      variance_pct = None
      accuracy_pct = None

    # Aggregate
    total_forecast += f_qty
    total_actual += a_qty
    total_abs_error += abs_variance
    if not is_outlier:
      total_abs_error_no_outliers += abs_variance
      total_actual_no_outliers += a_qty
    if f_qty > 0:
      mape_sum += abs_variance / f_qty * 100.0
      mape_count += 1
    if is_outlier:
      outlier_count += 1

    # Series rollup
    s_key = r['series'] or '(no series)'
    if s_key not in series_agg:
      series_agg[s_key] = {
        'series': s_key,
        'forecast': 0.0, 'actual': 0.0, 'abs_error': 0.0,
        'skus': set(),
      }
    agg = series_agg[s_key]
    agg['forecast'] += f_qty
    agg['actual'] += a_qty
    agg['abs_error'] += abs_variance
    agg['skus'].add(r['sku'])

    # Scatter data
    if is_outlier:
      scatter_outlier_x.append(f_qty)
      scatter_outlier_y.append(a_qty)
    else:
      scatter_normal_x.append(f_qty)
      scatter_normal_y.append(a_qty)
    if f_qty > max_value:
      max_value = f_qty
    if a_qty > max_value:
      max_value = a_qty

    # Apply outliers_only filter on detail grid only
    if outliers_only and not is_outlier:
      continue

    detail_rows.append({
      'sku': r['sku'],
      'series': r['series'] or '',
      'forecast_month': int(r['forecast_month']) if r['forecast_month'] is not None else None,
      'forecast_qty': _fmt(f_qty),
      'actual_qty': _fmt(a_qty),
      'variance': _fmt(variance),
      'variance_pct': _fmt(variance_pct) if variance_pct is not None else None,
      'accuracy_pct': _fmt(accuracy_pct) if accuracy_pct is not None else None,
      'is_outlier': 'Yes' if is_outlier else '',
    })

  # ------- Summary metrics -------
  bias = total_actual - total_forecast
  mape = (mape_sum / mape_count) if mape_count > 0 else None
  wmape = (total_abs_error / total_actual * 100.0) if total_actual > 0 else None
  wmape_no_out = (
    (total_abs_error_no_outliers / total_actual_no_outliers * 100.0)
    if total_actual_no_outliers > 0 else None
  )

  summary = {
    'target_year': target_year,
    'evaluated_months': f'{m_from}-{m_to}',
    'sku_month_count': len(raw),
    'outlier_count': outlier_count,
    'total_forecast': _fmt(total_forecast),
    'total_actual': _fmt(total_actual),
    'bias': _fmt(bias),
    'mape': _fmt(mape),
    'wmape': _fmt(wmape),
    'wmape_no_outliers': _fmt(wmape_no_out),
  }

  # ------- Series rollup -------
  series_rollup = []
  for s_key, agg in series_agg.items():
    s_forecast = agg['forecast']
    s_actual = agg['actual']
    s_wmape = (agg['abs_error'] / s_actual * 100.0) if s_actual > 0 else None
    series_rollup.append({
      'series': s_key,
      'sku_count': len(agg['skus']),
      'total_forecast': _fmt(s_forecast),
      'total_actual': _fmt(s_actual),
      'bias': _fmt(s_actual - s_forecast),
      'wmape': _fmt(s_wmape) if s_wmape is not None else None,
    })
  # Sort series by descending WMAPE (worst first) so problem series float up
  series_rollup.sort(
    key=lambda x: -float(x['wmape']) if x['wmape'] is not None else float('inf')
  )

  # ------- Scatter plot (Plotly figure) -------
  axis_max = max_value * 1.1 if max_value > 0 else 100.0
  plot_data = []
  # Diagonal reference line (perfect forecast)
  plot_data.append({
    'x': [0, axis_max], 'y': [0, axis_max],
    'type': 'scatter', 'mode': 'lines',
    'name': 'Perfect forecast',
    'line': {'color': '#999999', 'width': 1, 'dash': 'dash'},
    'hoverinfo': 'skip',
  })
  if scatter_normal_x:
    plot_data.append({
      'x': scatter_normal_x, 'y': scatter_normal_y,
      'type': 'scatter', 'mode': 'markers',
      'name': f'On-target ({len(scatter_normal_x)})',
      'marker': {'color': '#6750A4', 'size': 7, 'opacity': 0.65},
    })
  if scatter_outlier_x:
    plot_data.append({
      'x': scatter_outlier_x, 'y': scatter_outlier_y,
      'type': 'scatter', 'mode': 'markers',
      'name': f'Outliers ({len(scatter_outlier_x)})',
      'marker': {'color': '#d84315', 'size': 10, 'symbol': 'diamond', 'opacity': 0.85},
    })

  plot_layout = {
    'title': f'Forecast vs Actual ({target_year} months {m_from}-{m_to})',
    'xaxis': {'title': 'Forecast', 'rangemode': 'tozero', 'range': [0, axis_max]},
    'yaxis': {'title': 'Actual',   'rangemode': 'tozero', 'range': [0, axis_max]},
    'hovermode': 'closest',
    'legend': {'orientation': 'h', 'y': -0.2},
    'margin': {'l': 70, 'r': 40, 't': 60, 'b': 80},
    'plot_bgcolor': '#FAFAFA',
  }

  return {
    'status': 'success',
    'summary': summary,
    'detail_rows': detail_rows,
    'series_rollup': series_rollup,
    'scatter_data': {'plot_data': plot_data, 'plot_layout': plot_layout},
  }


@anvil.server.callable
def get_recent_batches(limit=25):
  """
  Return recent forecast batches for the batch dropdown.
  Returns list of (label, batch_id) tuples.
  """
  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(
      """
      SELECT TOP (%s) batch_id, target_year, sku_count, created_date
      FROM dbo.forecast_batches
      ORDER BY created_date DESC
      """,
      (limit,),
    )
    rows = cursor.fetchall()
    result = []
    for r in rows:
      created = r['created_date']
      date_str = created.strftime('%Y-%m-%d') if hasattr(created, 'strftime') else str(created)
      label = f"{r['batch_id']} ({r['target_year']}, {r['sku_count']} SKUs, {date_str})"
      result.append((label, r['batch_id']))
    return result
  finally:
    conn.close()


@anvil.server.callable
def get_forecast_sku_detail(sku, batch_id, history_months=24):
  """
  Return everything needed for the SKU Forecast Detail form:
    - SKU metadata (description, series, classification)
    - Forecast batch info (target year, model used for this SKU)
    - Historical sales (monthly aggregates, last N months)
    - Forecast (monthly values with lower/upper bounds)
    - Actuals (usage_data for months in forecast_year that have already passed)
    - Monthly detail rows suitable for a grid
    - A plotly figure dict for the chart

  Returns:
    {
      'status': 'success',
      'sku': {...},
      'forecast_meta': {...},
      'monthly': [ {month, historical, forecast, lower, upper, actual, variance}, ... ],
      'plot_data': [ trace1, trace2, ... ],   # plotly traces
      'plot_layout': {...},                    # plotly layout
    }
    or {'status': 'error', 'error': '...'}
  """
  if not sku or not batch_id:
    return {'status': 'error', 'error': 'SKU and batch_id are required'}

  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()

    # --- SKU metadata -------------------------------------------------
    cursor.execute(
      """
      SELECT p.sku, p.part_desc AS description, p.series,
             a.abc_classification, a.is_stocked_at_starlite,
             m.min_stock, m.max_stock, m.avg_daily_usage, m.demand_pattern
      FROM dbo.part_specs p
      LEFT JOIN dbo.a_items_master a ON p.sku = a.sku
      LEFT JOIN dbo.min_max_calculated m ON p.sku = m.sku
      WHERE p.sku = %s
      """,
      (sku,),
    )
    sku_row = cursor.fetchone()
    if not sku_row:
      return {'status': 'error', 'error': f"SKU '{sku}' not found in part_specs"}
    sku_info = _serialize(sku_row)

    # --- Batch metadata -----------------------------------------------
    cursor.execute(
      """
      SELECT batch_id, target_year, training_start, training_end,
             created_date, notes
      FROM dbo.forecast_batches
      WHERE batch_id = %s
      """,
      (batch_id,),
    )
    batch_row = cursor.fetchone()
    if not batch_row:
      return {'status': 'error', 'error': f"Batch '{batch_id}' not found"}
    batch_info = _serialize(batch_row)
    target_year = int(batch_row['target_year'])

    # --- Forecast rows for this SKU+batch -----------------------------
    cursor.execute(
      """
      SELECT forecast_year, forecast_month,
             forecast_qty, forecast_lower, forecast_upper,
             model_used, confidence_level
      FROM dbo.forecast_sku_results
      WHERE sku = %s AND forecast_batch = %s
      ORDER BY forecast_year, forecast_month
      """,
      (sku, batch_id),
    )
    forecast_rows = [_serialize(r) for r in cursor.fetchall()]
    model_used = forecast_rows[0]['model_used'] if forecast_rows else None

    # --- Historical sales (monthly) -----------------------------------
    cursor.execute(
      """
      SELECT YEAR(usage_date) AS y, MONTH(usage_date) AS m,
             SUM(qty_sold) AS qty
      FROM dbo.usage_data
      WHERE sku = %s
        AND usage_date >= DATEADD(month, -%s, GETDATE())
        AND YEAR(usage_date) < %s
      GROUP BY YEAR(usage_date), MONTH(usage_date)
      ORDER BY y, m
      """,
      (sku, history_months, target_year),
    )
    hist_rows = cursor.fetchall()
    historical = {
      _month_key(r['y'], r['m']): float(r['qty'] or 0) for r in hist_rows
    }

    # --- Actuals for months in target_year that have already occurred -
    cursor.execute(
      """
      SELECT MONTH(usage_date) AS m, SUM(qty_sold) AS qty
      FROM dbo.usage_data
      WHERE sku = %s AND YEAR(usage_date) = %s
      GROUP BY MONTH(usage_date)
      """,
      (sku, target_year),
    )
    actuals = {int(r['m']): float(r['qty'] or 0) for r in cursor.fetchall()}

  finally:
    conn.close()

  # --- Build the monthly detail rows (forecast year only) --------------
  # Historical months show up in the chart only; the grid focuses on the
  # forecast year so forecast/lower/upper/actual columns stay populated.
  monthly = []
  for fr in forecast_rows:
    month = int(fr['forecast_month'])
    month_key = _month_key(fr['forecast_year'], month)
    fc_qty = float(fr['forecast_qty'] or 0)
    act = actuals.get(month)
    variance = None
    if act is not None:
      variance = act - fc_qty
    monthly.append({
      'month': month_key,
      'forecast': _fmt(fc_qty),
      'lower': _fmt(float(fr['forecast_lower'] or 0)),
      'upper': _fmt(float(fr['forecast_upper'] or 0)),
      'actual': _fmt(act),
      'variance': _fmt(variance),
    })

  # --- Build plotly figure ----------------------------------------------
  hist_x = [k for k in sorted(historical.keys())]
  hist_y = [historical[k] for k in hist_x]

  fcst_x = [_month_key(fr['forecast_year'], fr['forecast_month']) for fr in forecast_rows]
  fcst_y = [float(fr['forecast_qty'] or 0) for fr in forecast_rows]
  lower_y = [float(fr['forecast_lower'] or 0) for fr in forecast_rows]
  upper_y = [float(fr['forecast_upper'] or 0) for fr in forecast_rows]

  act_x = [_month_key(target_year, m) for m in sorted(actuals.keys())]
  act_y = [actuals[m] for m in sorted(actuals.keys())]

  plot_data = []

  # Upper bound (invisible line, for fill)
  if fcst_x:
    plot_data.append({
      'x': fcst_x,
      'y': upper_y,
      'type': 'scatter',
      'mode': 'lines',
      'line': {'width': 0},
      'showlegend': False,
      'hoverinfo': 'skip',
      'name': 'Upper',
    })
    # Lower bound (fills to upper = confidence band)
    plot_data.append({
      'x': fcst_x,
      'y': lower_y,
      'type': 'scatter',
      'mode': 'lines',
      'line': {'width': 0},
      'fill': 'tonexty',
      'fillcolor': 'rgba(103,80,164,0.18)',
      'showlegend': True,
      'hoverinfo': 'skip',
      'name': 'Confidence Band',
    })

  # Historical
  if hist_x:
    plot_data.append({
      'x': hist_x,
      'y': hist_y,
      'type': 'scatter',
      'mode': 'lines+markers',
      'name': 'Historical Sales',
      'line': {'color': '#555555', 'width': 2},
      'marker': {'size': 6},
    })

  # Forecast
  if fcst_x:
    plot_data.append({
      'x': fcst_x,
      'y': fcst_y,
      'type': 'scatter',
      'mode': 'lines+markers',
      'name': 'Forecast',
      'line': {'color': '#6750A4', 'width': 3, 'dash': 'dash'},
      'marker': {'size': 7},
    })

  # Actuals (overlay on forecast months if data exists)
  if act_x:
    plot_data.append({
      'x': act_x,
      'y': act_y,
      'type': 'scatter',
      'mode': 'lines+markers',
      'name': f'Actual {target_year}',
      'line': {'color': '#2e7d32', 'width': 2},
      'marker': {'size': 8, 'symbol': 'diamond'},
    })

  plot_layout = {
    'title': f'{sku} — Historical + Forecast ({target_year})',
    'xaxis': {'title': 'Month', 'tickangle': -45},
    'yaxis': {'title': 'Quantity', 'rangemode': 'tozero'},
    'hovermode': 'x unified',
    'legend': {'orientation': 'h', 'y': -0.25},
    'margin': {'l': 60, 'r': 40, 't': 60, 'b': 100},
    'plot_bgcolor': '#FAFAFA',
  }

  return {
    'status': 'success',
    'sku_info': sku_info,
    'batch_info': batch_info,
    'model_used': model_used,
    'history_months_shown': len(historical),
    'monthly': monthly,
    'plot_data': plot_data,
    'plot_layout': plot_layout,
  }


# =============================================================================
# PURCHASING EXPORT — used by forecast_dashboard form (per-row Export button)
# =============================================================================

def _find_baseline_batch(cursor, batch_id, target_year):
  """
  Find the immediately-prior batch for the same target_year.
  Returns the prior batch_id or None.
  """
  cursor.execute(
    """
    SELECT TOP 1 batch_id
    FROM dbo.forecast_batches
    WHERE target_year = %s
      AND created_date < (SELECT created_date FROM dbo.forecast_batches WHERE batch_id = %s)
    ORDER BY created_date DESC
    """,
    (target_year, batch_id),
  )
  row = cursor.fetchone()
  return row['batch_id'] if row else None


def _fetch_export_rows(cursor, batch_id, target_year):
  """
  Pull SKU/month rows for the export, joined with part_specs (description),
  A-item flag, last_cost, and last-year sales total. Mirrors the original
  console script's get_forecast_data shape.

  target_year is passed in (already known from the caller) so we don't
  need DECLARE / multi-statement T-SQL inside a pymssql cursor.
  """
  prior_year = int(target_year) - 1
  cursor.execute(
    """
    SELECT
        RTRIM(fr.series) AS series,
        RTRIM(fr.sku) AS sku,
        ps.part_desc,
        fr.forecast_year,
        fr.forecast_month,
        ROUND(fr.forecast_qty, 0) AS forecast_qty,
        lc.last_cost,
        CASE WHEN aim.sku IS NOT NULL THEN 1 ELSE 0 END AS is_a_item,
        ISNULL(lys.sold_last_year, 0) AS sold_last_year,
        fr.model_used
    FROM dbo.forecast_sku_results fr
    LEFT JOIN dbo.part_specs ps ON RTRIM(fr.sku) = RTRIM(ps.sku)
    LEFT JOIN (
        SELECT sku, MAX(last_cost) AS last_cost
        FROM dbo.forecast_sales_history
        WHERE last_cost > 0
        GROUP BY sku
    ) lc ON RTRIM(fr.sku) = lc.sku
    LEFT JOIN dbo.a_items_master aim ON RTRIM(fr.sku) = RTRIM(aim.sku)
        AND aim.abc_classification = 'A'
    LEFT JOIN (
        SELECT sku, SUM(units_sold) AS sold_last_year
        FROM dbo.forecast_sales_history
        WHERE year = %s
        GROUP BY sku
    ) lys ON RTRIM(fr.sku) = lys.sku
    WHERE fr.forecast_batch = %s
      AND fr.series IS NOT NULL
      AND RTRIM(fr.series) <> ''
    ORDER BY fr.series, fr.sku, fr.forecast_month
    """,
    (prior_year, batch_id),
  )
  return cursor.fetchall()


def _fetch_baseline_totals(cursor, batch_id):
  """
  Pull annual forecast total per SKU for a baseline batch.
  Returns dict: {sku: total_units}
  """
  if not batch_id:
    return {}
  cursor.execute(
    """
    SELECT RTRIM(sku) AS sku, SUM(forecast_qty) AS total_qty
    FROM dbo.forecast_sku_results
    WHERE forecast_batch = %s
    GROUP BY RTRIM(sku)
    """,
    (batch_id,),
  )
  return {r['sku']: float(r['total_qty'] or 0) for r in cursor.fetchall()}


def _fetch_booked_sales(cursor, target_year):
  """
  Pull already-booked (committed) sales for the forecast year.
  Mirrors the console script's get_booked_sales: open balances on
  sales_orders_due that ship in target_year, excluding closed/cancelled.
  Returns dict: {sku: {'booked_qty': float, 'order_count': int}}
  """
  cursor.execute(
    """
    SELECT
        RTRIM(so.nx_sku) AS sku,
        SUM(so.nx_qty_balance_due) AS booked_qty,
        COUNT(DISTINCT so.nx_order_numb) AS order_count
    FROM dbo.sales_orders_due so
    WHERE YEAR(so.nx_dship_by_date) = %s
      AND so.nx_qty_balance_due > 0
      AND so.nx_item_status NOT IN ('C', 'X')
    GROUP BY RTRIM(so.nx_sku)
    """,
    (int(target_year),),
  )
  out = {}
  for r in cursor.fetchall():
    out[r['sku']] = {
      'booked_qty': float(r['booked_qty'] or 0),
      'order_count': int(r['order_count'] or 0),
    }
  return out


def _build_sku_pivot(rows, baseline_totals, booked_sales):
  """
  Pivot SKU/month rows into per-SKU records with:
    - monthly[1..12] forecast quantities
    - total annual forecast
    - sold_last_year, booked_qty, order_count
    - baseline_total, delta, pct_change, material_change flag
    - is_a_item, last_cost, model_used, part_desc, series

  Returns a list of dicts sorted by (series, sku).
  """
  by_sku = {}
  for r in rows:
    sku = r['sku']
    if sku not in by_sku:
      by_sku[sku] = {
        'series': r['series'] or '',
        'sku': sku,
        'part_desc': r['part_desc'] or '',
        'monthly': {m: 0.0 for m in range(1, 13)},
        'sold_last_year': float(r['sold_last_year'] or 0),
        'is_a_item': int(r['is_a_item'] or 0),
        'last_cost': float(r['last_cost']) if r['last_cost'] is not None else None,
        'model_used': r['model_used'] or '',
      }
    m = int(r['forecast_month'])
    if 1 <= m <= 12:
      by_sku[sku]['monthly'][m] = float(r['forecast_qty'] or 0)

  # Compute totals + booked + comparison fields
  out = []
  for sku, d in by_sku.items():
    d['total'] = sum(d['monthly'].values())
    bs = booked_sales.get(sku, {'booked_qty': 0.0, 'order_count': 0})
    d['booked_qty'] = bs['booked_qty']
    d['order_count'] = bs['order_count']

    baseline = baseline_totals.get(sku)
    d['baseline_total'] = baseline  # may be None if no baseline batch / new SKU
    if baseline is not None and baseline > 0:
      delta = d['total'] - baseline
      pct = delta / baseline
      d['delta'] = delta
      d['pct_change'] = pct
      d['material_change'] = (abs(delta) > MATERIAL_DELTA_UNITS
                              and abs(pct) > MATERIAL_DELTA_PCT)
    elif baseline is None:
      # No baseline row at all — SKU is new in this batch
      d['delta'] = d['total']
      d['pct_change'] = None
      d['material_change'] = d['total'] > MATERIAL_DELTA_UNITS  # only abs criterion applies
    else:
      # Baseline existed but was zero
      d['delta'] = d['total']
      d['pct_change'] = None
      d['material_change'] = d['total'] > MATERIAL_DELTA_UNITS

    out.append(d)

  out.sort(key=lambda d: (d['series'], d['sku']))
  return out


# Excel styling helpers -------------------------------------------------------

_HEADER_FILL = PatternFill(start_color='1F4E79', end_color='1F4E79', fill_type='solid')
_HEADER_FONT = Font(bold=True, color='FFFFFF', size=11)
_A_ITEM_FILL = PatternFill(start_color='E2EFDA', end_color='E2EFDA', fill_type='solid')
_ALERT_FILL  = PatternFill(start_color='FCE4D6', end_color='FCE4D6', fill_type='solid')
_MATERIAL_FILL = PatternFill(start_color='FFF2CC', end_color='FFF2CC', fill_type='solid')


def _style_header_row(ws, row, num_cols):
  for col in range(1, num_cols + 1):
    cell = ws.cell(row=row, column=col)
    cell.fill = _HEADER_FILL
    cell.font = _HEADER_FONT
    cell.alignment = Alignment(horizontal='center', vertical='center')


def _auto_width(ws, max_width=50):
  for column in ws.columns:
    max_len = 0
    col_letter = get_column_letter(column[0].column)
    for cell in column:
      try:
        if cell.value is not None:
          max_len = max(max_len, len(str(cell.value)))
      except Exception:
        pass
    ws.column_dimensions[col_letter].width = min(max_len + 2, max_width)


def _build_workbook(records, current_batch, baseline_batch, target_year):
  """
  Build the four-sheet workbook:
    1. Summary
    2. Monthly Forecast (with Material_Change column)
    3. Alerts (booked > forecast)
    4. Material Changes (only flagged SKUs)
  Returns the workbook object.
  """
  wb = Workbook()

  total_skus = len(records)
  total_qty = sum(r['total'] for r in records)
  total_booked = sum(r['booked_qty'] for r in records)
  a_item_count = sum(1 for r in records if r['is_a_item'] == 1)
  alert_records = [r for r in records if r['booked_qty'] > r['total']]
  material_records = [r for r in records if r['material_change']]

  # --- Sheet 1: Summary --------------------------------------------------
  ws = wb.active
  ws.title = "Summary"
  ws['A1'] = f"TITAN FLOW CONTROL - {target_year} FORECAST"
  ws['A1'].font = Font(bold=True, size=16, color='1F4E79')
  ws.merge_cells('A1:D1')
  ws['A2'] = "Purchasing Export — for vendor blanket orders"
  ws['A2'].font = Font(italic=True, size=12, color='666666')
  ws['A3'] = f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
  ws['A3'].font = Font(size=10, color='999999')

  pct_str = f"{(total_booked / total_qty * 100):.1f}%" if total_qty > 0 else 'n/a'
  baseline_label = baseline_batch if baseline_batch else '(none — first batch for this year)'

  stats = [
    ('Forecast Year:',            target_year),
    ('Current Batch:',            current_batch),
    ('Baseline Batch (compare):', baseline_label),
    ('', ''),
    ('Total SKUs:',               f'{total_skus:,}'),
    ('A-Item SKUs:',              f'{a_item_count:,}'),
    ('Total Forecast Qty:',       f'{total_qty:,.0f}'),
    ('Total Booked Qty:',         f'{total_booked:,.0f}'),
    ('Booked % of Forecast:',     pct_str),
    ('SKUs with Booked > Forecast:', f'{len(alert_records)}'),
    ('SKUs with Material Change:',   f'{len(material_records)}'),
  ]
  row = 5
  for label, value in stats:
    ws[f'A{row}'] = label
    ws[f'A{row}'].font = Font(bold=True)
    ws[f'B{row}'] = value
    row += 1

  row += 2
  ws[f'A{row}'] = "Material Change definition:"
  ws[f'A{row}'].font = Font(bold=True)
  row += 1
  ws[f'A{row}'] = (f'TRUE when |current - baseline| > {MATERIAL_DELTA_UNITS} units '
                   f'AND |% change| > {MATERIAL_DELTA_PCT * 100:.0f}%')
  row += 2
  ws[f'A{row}'] = "Legend (row colors in Monthly Forecast sheet):"
  ws[f'A{row}'].font = Font(bold=True)
  row += 1
  ws[f'A{row}'] = "  Green = A-Item (high-volume SKU)"
  ws[f'A{row}'].fill = _A_ITEM_FILL
  row += 1
  ws[f'A{row}'] = "  Orange = Booked exceeds Forecast (vendor attention needed)"
  ws[f'A{row}'].fill = _ALERT_FILL
  row += 1
  ws[f'A{row}'] = "  Yellow = Material Change vs baseline (consider alerting vendor)"
  ws[f'A{row}'].fill = _MATERIAL_FILL
  _auto_width(ws)

  # --- Sheet 2: Monthly Forecast ----------------------------------------
  ws = wb.create_sheet("Monthly Forecast")
  ws['A1'] = f"{target_year} Monthly Forecast by SKU"
  ws['A1'].font = Font(bold=True, size=14, color='1F4E79')

  months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
            'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
  headers = (['Series', 'SKU'] + months
             + ['Total', 'Sold_Last_Yr', 'Booked_Qty',
                'Baseline_Total', 'Delta', 'Pct_Change', 'Material_Change'])
  header_row = 3
  for c_idx, name in enumerate(headers, start=1):
    ws.cell(row=header_row, column=c_idx, value=name)
  _style_header_row(ws, header_row, len(headers))

  data_start = header_row + 1
  for r_idx, rec in enumerate(records, start=data_start):
    ws.cell(row=r_idx, column=1, value=rec['series'])
    ws.cell(row=r_idx, column=2, value=rec['sku'])
    for m in range(1, 13):
      ws.cell(row=r_idx, column=2 + m, value=rec['monthly'][m])
    ws.cell(row=r_idx, column=15, value=rec['total'])
    ws.cell(row=r_idx, column=16, value=rec['sold_last_year'])
    ws.cell(row=r_idx, column=17, value=rec['booked_qty'])
    ws.cell(row=r_idx, column=18,
            value=rec['baseline_total'] if rec['baseline_total'] is not None else '')
    ws.cell(row=r_idx, column=19,
            value=rec['delta'] if rec['delta'] is not None else '')
    if rec['pct_change'] is not None:
      ws.cell(row=r_idx, column=20, value=rec['pct_change'])
      ws.cell(row=r_idx, column=20).number_format = '0.0%'
    else:
      ws.cell(row=r_idx, column=20, value='n/a')
    ws.cell(row=r_idx, column=21,
            value='YES' if rec['material_change'] else '')

    # Number formatting on numeric columns (Series and SKU + Pct/Material are special)
    for col in list(range(3, 20)):  # Jan..Delta
      ws.cell(row=r_idx, column=col).number_format = '#,##0'
    # Pct_Change handled above (col 20 with 0.0%)

    # Row coloring (priority: alert > material > a-item)
    if rec['booked_qty'] > rec['total']:
      for col in range(1, len(headers) + 1):
        ws.cell(row=r_idx, column=col).fill = _ALERT_FILL
    elif rec['material_change']:
      for col in range(1, len(headers) + 1):
        ws.cell(row=r_idx, column=col).fill = _MATERIAL_FILL
    elif rec['is_a_item'] == 1:
      for col in range(1, len(headers) + 1):
        ws.cell(row=r_idx, column=col).fill = _A_ITEM_FILL

  _auto_width(ws, max_width=15)

  # --- Sheet 3: Alerts (booked > forecast) ------------------------------
  if alert_records:
    ws = wb.create_sheet("Alerts (Booked over Forecast)")
    ws['A1'] = f"SKUs Where Booked Orders Exceed Forecast — {target_year}"
    ws['A1'].font = Font(bold=True, size=14, color='C65911')
    ws['A2'] = ("These SKUs have committed orders exceeding the statistical forecast. "
                "Consider ordering to booked quantity.")
    ws['A2'].font = Font(italic=True, color='666666')
    cols = ['Series', 'SKU', 'Forecast', 'Sold_Last_Yr', 'Booked_Qty',
            'Variance', 'Booked_%']
    hr = 4
    for c_idx, name in enumerate(cols, start=1):
      ws.cell(row=hr, column=c_idx, value=name)
    _style_header_row(ws, hr, len(cols))

    # Sort by variance descending
    sorted_alerts = sorted(alert_records,
                           key=lambda r: (r['booked_qty'] - r['total']),
                           reverse=True)
    for r_idx, rec in enumerate(sorted_alerts, start=hr + 1):
      ws.cell(row=r_idx, column=1, value=rec['series'])
      ws.cell(row=r_idx, column=2, value=rec['sku'])
      ws.cell(row=r_idx, column=3, value=rec['total'])
      ws.cell(row=r_idx, column=4, value=rec['sold_last_year'])
      ws.cell(row=r_idx, column=5, value=rec['booked_qty'])
      ws.cell(row=r_idx, column=6, value=rec['booked_qty'] - rec['total'])
      booked_pct = (rec['booked_qty'] / rec['total']) if rec['total'] > 0 else None
      if booked_pct is not None:
        ws.cell(row=r_idx, column=7, value=booked_pct)
        ws.cell(row=r_idx, column=7).number_format = '0.0%'
      else:
        ws.cell(row=r_idx, column=7, value='n/a')
      for col in range(3, 7):
        ws.cell(row=r_idx, column=col).number_format = '#,##0'
    _auto_width(ws)

  # --- Sheet 4: Material Changes ----------------------------------------
  if material_records:
    ws = wb.create_sheet("Material Changes")
    title_baseline = baseline_batch or '(no prior batch)'
    ws['A1'] = (f"SKUs With Material Forecast Change vs {title_baseline}")
    ws['A1'].font = Font(bold=True, size=14, color='806000')
    ws['A2'] = (f"Flagged when |delta| > {MATERIAL_DELTA_UNITS} units "
                f"AND |% change| > {MATERIAL_DELTA_PCT * 100:.0f}%. "
                f"These changes are large enough to warrant alerting vendors.")
    ws['A2'].font = Font(italic=True, color='666666')
    cols = ['Series', 'SKU', 'Description', 'Baseline_Total', 'Current_Total',
            'Delta', 'Pct_Change', 'Direction']
    hr = 4
    for c_idx, name in enumerate(cols, start=1):
      ws.cell(row=hr, column=c_idx, value=name)
    _style_header_row(ws, hr, len(cols))

    # Sort by |delta| descending — biggest movers first
    sorted_mat = sorted(material_records,
                        key=lambda r: abs(r['delta'] or 0),
                        reverse=True)
    for r_idx, rec in enumerate(sorted_mat, start=hr + 1):
      ws.cell(row=r_idx, column=1, value=rec['series'])
      ws.cell(row=r_idx, column=2, value=rec['sku'])
      ws.cell(row=r_idx, column=3, value=rec['part_desc'])
      ws.cell(row=r_idx, column=4,
              value=rec['baseline_total'] if rec['baseline_total'] is not None else '(new SKU)')
      ws.cell(row=r_idx, column=5, value=rec['total'])
      ws.cell(row=r_idx, column=6, value=rec['delta'])
      if rec['pct_change'] is not None:
        ws.cell(row=r_idx, column=7, value=rec['pct_change'])
        ws.cell(row=r_idx, column=7).number_format = '0.0%'
      else:
        ws.cell(row=r_idx, column=7, value='n/a')
      ws.cell(row=r_idx, column=8,
              value='Increase' if (rec['delta'] or 0) > 0 else 'Decrease')
      for col in [4, 5, 6]:
        ws.cell(row=r_idx, column=col).number_format = '#,##0'
    _auto_width(ws)

  return wb


@anvil.server.callable
def get_purchasing_export(batch_id):
  """
  Build the Purchasing Excel export for a given forecast batch and return it
  as anvil.BlobMedia for client-side download.

  The Excel has up to four sheets:
    1. Summary             — totals, metadata, legend, baseline batch named
    2. Monthly Forecast    — Series x SKU x Month pivot, Booked, Material_Change
    3. Alerts              — only SKUs where Booked_Qty > forecast Total
    4. Material Changes    — only SKUs flagged Material_Change (sorted by |delta|)

  "Material Change" compares this batch's SKU annual total against the
  immediately-prior batch (same target_year) by created_date. The flag is
  TRUE when |delta units| > MATERIAL_DELTA_UNITS AND |% change| > MATERIAL_DELTA_PCT.

  Args:
      batch_id: forecast batch identifier (e.g., 'AI2026_v6')

  Returns:
      anvil.BlobMedia object containing the .xlsx file. The client can
      invoke anvil.media.download(media) to trigger a download.

      Or {'status': 'error', 'error': '...'} dict if the batch isn't
      found or has no forecast rows.
  """
  if not batch_id:
    return {'status': 'error', 'error': 'batch_id is required'}

  conn = db_config.get_nexus_connection()
  try:
    cursor = conn.cursor()

    # Look up batch metadata
    cursor.execute(
      'SELECT batch_id, target_year FROM dbo.forecast_batches WHERE batch_id = %s',
      (batch_id,),
    )
    batch_row = cursor.fetchone()
    if not batch_row:
      return {'status': 'error', 'error': f"Batch '{batch_id}' not found"}
    target_year = int(batch_row['target_year'])

    # Find baseline (immediately-prior batch for same target_year)
    baseline_batch_id = _find_baseline_batch(cursor, batch_id, target_year)

    # Pull the data
    rows = _fetch_export_rows(cursor, batch_id, target_year)
    if not rows:
      return {'status': 'error',
              'error': f"Batch '{batch_id}' has no forecast rows to export"}
    baseline_totals = _fetch_baseline_totals(cursor, baseline_batch_id)
    booked_sales = _fetch_booked_sales(cursor, target_year)
  finally:
    conn.close()

  # Build the records and the workbook
  records = _build_sku_pivot(rows, baseline_totals, booked_sales)
  wb = _build_workbook(records, batch_id, baseline_batch_id, target_year)

  # Save to a BytesIO and wrap in BlobMedia
  buf = io.BytesIO()
  wb.save(buf)
  buf.seek(0)

  timestamp = datetime.now().strftime('%Y%m%d_%H%M')
  filename = f"Purchasing_Forecast_{target_year}_{batch_id}_{timestamp}.xlsx"
  return anvil.BlobMedia(
    content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    content=buf.read(),
    name=filename,
  )
