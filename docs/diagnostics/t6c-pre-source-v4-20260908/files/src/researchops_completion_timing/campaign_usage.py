"""Numeric SDK/native reconciliation. Never inspect output, IDs or error text."""
from __future__ import annotations

_MAX = 9_007_199_254_740_991


def _counter(value):
    return value if type(value) is int and 0 <= value <= _MAX else None


def _usage(value):
    details = getattr(value, 'input_tokens_details', None)
    return dict(input_tokens=_counter(getattr(value, 'input_tokens', None)),
        output_tokens=_counter(getattr(value, 'output_tokens', None)),
        total_tokens=_counter(getattr(value, 'total_tokens', None)),
        cached_tokens=_counter(getattr(details, 'cached_tokens', None)))


def _consistent(value):
    return (all(item is not None for item in value.values())
        and value['total_tokens'] == value['input_tokens'] + value['output_tokens']
        and value['cached_tokens'] <= value['input_tokens'])


def _project_case_usage(result, records, *, observed_response_count, terminal_attempt_count):
    """Result may be an actual RunResult or exception.run_data; absence stays null.

    Aggregate entry grouping is permitted only for one-request responses and
    equal-length ordered SDK lists, with numerical cross-checks. No synthetic
    request row is made when the SDK supplied no per-request entries.
    """
    errors = []
    def reject(reason):
        if reason not in errors: errors.append(reason)
    responses = getattr(result, 'raw_responses', None)
    if type(responses) is not list or len(responses) > 8:
        responses = None; reject('sdk_responses_unavailable')
    aggregate = getattr(getattr(result, 'context_wrapper', None), 'usage', None)
    request_count = _counter(getattr(aggregate, 'requests', None))
    totals = _usage(aggregate)
    aggregate_entries = getattr(aggregate, 'request_usage_entries', None)
    if type(aggregate_entries) is not list or len(aggregate_entries) > 8: aggregate_entries = None
    rows = []; indices = {}; grouping = []
    if responses is not None:
        if len(responses) != observed_response_count or len(responses) != len(records): reject('sdk_native_response_count_mismatch')
        for index, response in enumerate(responses):
            usage = getattr(response, 'usage', None)
            response_totals = _usage(usage)
            entries = getattr(usage, 'request_usage_entries', None)
            if type(entries) is list and len(entries) == 1:
                entry = entries[0]; source = 'response_request_entry'
            elif type(entries) is list and not entries and aggregate_entries is not None and len(aggregate_entries) == len(responses):
                entry = aggregate_entries[index]; source = 'aggregate_single_request_alignment'
            else:
                indices[index] = (); reject('sdk_request_entries_unavailable'); continue
            row = _usage(entry)
            matched = _counter(getattr(usage, 'requests', None)) == 1 and row == response_totals and _consistent(row)
            if index < len(records):
                native = records[index]['usage']['normalized']
                expected = dict(input_tokens=native['input_tokens'], output_tokens=native['output_tokens'],
                    total_tokens=native['total_tokens'], cached_tokens=native['cached_input_tokens'])
                matched = matched and native['requests'] == 1 and row == expected
                # An SDK default zero is not evidence for a missing native field.
                for name in row:
                    if expected[name] is None: row[name] = None
            else:
                matched = False
            if not matched: reject('sdk_native_usage_mismatch')
            row.update(response_index=index, request_index=0, matched=matched)
            rows.append(row); indices[index] = (0,); grouping.append(source)
    if request_count != terminal_attempt_count: reject('sdk_attempt_count_mismatch')
    row_totals = {name: sum(row[name] for row in rows) if rows and all(row[name] is not None for row in rows) else None
                  for name in ('input_tokens', 'output_tokens', 'total_tokens', 'cached_tokens')}
    if rows:
        if request_count != len(rows) or totals != row_totals or not _consistent(totals): reject('sdk_aggregate_usage_mismatch')
        if aggregate_entries is None or len(aggregate_entries) != len(rows):
            reject('sdk_aggregate_entries_unavailable')
        elif any(_usage(entry) != {name: row[name] for name in row_totals} for entry, row in zip(aggregate_entries, rows)):
            reject('sdk_aggregate_entries_mismatch')
    else:
        reject('sdk_usage_rows_unavailable')
    return dict(sdk_raw_response_count=None if responses is None else len(responses), sdk_usage_request_count=request_count,
        rows=tuple(rows), indices_by_response=indices, grouping_sources=tuple(grouping), aggregate=totals,
        matched=not errors, errors=tuple(errors))


def _usage_event(projection, *, response_detail_count, provider, transport):
    rows = projection['rows']; aggregate = projection['aggregate']
    # Keep actual aggregate disagreement visible to the frozen verifier. Do not
    # "repair" it by substituting native totals. No SDK rows means unknown totals.
    values = {name: aggregate[name] if rows else None for name in aggregate}
    for name in values:
        if any(row[name] is None for row in rows): values[name] = None
    return dict(usage_complete=bool(rows) and _consistent(values),
        request_count=projection['sdk_usage_request_count'] if rows else None,
        input_unit_count=values['input_tokens'], output_unit_count=values['output_tokens'], total_unit_count=values['total_tokens'],
        cached_input_unit_count=values['cached_tokens'], response_detail_count=response_detail_count,
        estimated_cost_usd=None, model_call_rows_recorded=len(rows), latency_allocation='equal_share_of_agent_segment',
        model_call_cost_method='cny_recomputed_only_in_closure', provider=provider, transport=transport)
