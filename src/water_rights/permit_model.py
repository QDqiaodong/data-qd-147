"""Deterministic candidate folding, report assembly and offline re-verification.

The dataset identity is the SHA256 of the canonical JSON encoding of the
report core (the full report minus its own ``dataset_id`` field).  The
encoding rules are fixed: UTF-8, sorted keys, ``,``/``:`` separators, no
surrogate escaping, no non-finite numbers, so identical verified content
always yields the identical report and identity.
"""
import hashlib
import json

SCHEMA_VERSION = 1
KIND = 'permit-dataset'


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def dataset_identity(core_report):
    return hashlib.sha256(canonical_json(core_report).encode('utf-8')).hexdigest()


def fold_outlets(entries):
    """Fold repeated permit_id+outlet_id keys only on identical content.

    Holder identity and all twelve monthly quotas must match exactly; a
    conflict rejects the whole candidate.  Every source reference is kept.
    """
    merged = {}
    for entry in entries:
        key = (entry['permit_id'], entry['outlet_id'])
        if key not in merged:
            merged[key] = {
                'permit_id': entry['permit_id'],
                'outlet_id': entry['outlet_id'],
                'holder': entry['holder'],
                'annual_litres': entry['annual_litres'],
                'months': list(entry['months']),
                'refs': [],
            }
        existing = merged[key]
        if existing['holder'] != entry['holder']:
            raise ValueError('conflicting holder repeated for %s/%s' % key)
        if existing['annual_litres'] != entry['annual_litres'] \
                or list(existing['months']) != list(entry['months']):
            raise ValueError('conflicting monthly quota repeated for %s/%s' % key)
        existing['refs'].extend(entry['refs'])
    return merged


def _sorted_refs(refs):
    return sorted(refs, key=lambda ref: (
        ref['announcement_url'], ref['announcement_logical_row'],
        ref['detail_url'], ref['csv_url']))


def build_report(snapshot, entries, manifest):
    """Assemble the deterministic report from verified outlets and evidence."""
    merged = fold_outlets(entries)
    outlets = []
    for key in sorted(merged):
        outlet = merged[key]
        months = [{'month': month, 'licensed_litres': litres}
                  for month, litres, _record in outlet['months']]
        annual = sum(litres for _, litres, _ in outlet['months'])
        if annual != outlet['annual_litres']:
            raise ValueError('annual total does not conserve monthly quotas')
        outlets.append({
            'permit_id': outlet['permit_id'],
            'outlet_id': outlet['outlet_id'],
            'holder': outlet['holder'],
            'annual_litres': annual,
            'months': months,
            'sources': _sorted_refs(outlet['refs']),
        })
    resources = sorted(manifest, key=lambda item: (
        item['request_url'], item['final_url'], item['kind']))
    core = {
        'schema_version': SCHEMA_VERSION,
        'kind': KIND,
        'snapshot': snapshot,
        'outlets': outlets,
        'resources': resources,
    }
    identity = dataset_identity(core)
    report = dict(core)
    report['dataset_id'] = identity
    return report, identity


def verify_report_evidence(conn, report):
    """Re-check a published report against the raw evidence stored in the DB.

    Every reference resolves to stored bytes whose SHA256 matches both the
    manifest and the reference; every outlet's twelve months recompute from
    the stored CSV bytes and conserve the detail's annual amount.  No
    network access occurs.  Raises ValueError on any unrecoverable mismatch.
    """
    from .permit_html import parse_announcement, parse_detail
    from .permit_csv import parse_quota_csv
    from .permit_store import evidence_for

    snapshot = report['snapshot']
    manifest_index = {}
    for item in report['resources']:
        manifest_index[(item['request_url'], item['final_url'])] = item
        evidence = evidence_for(conn, item['request_url'], snapshot)
        if evidence is None or evidence.get('corrupt'):
            raise ValueError('stored evidence missing or corrupt: ' + item['request_url'])
        if evidence['sha256'] != item['raw_sha256']:
            raise ValueError('evidence digest mismatch: ' + item['request_url'])
        if evidence['final_url'] != item['final_url'] or evidence['etag'] != item['etag']:
            raise ValueError('evidence metadata mismatch: ' + item['request_url'])

    seen_keys = set()
    for outlet in report['outlets']:
        key = (outlet['permit_id'], outlet['outlet_id'])
        if key in seen_keys:
            raise ValueError('duplicate outlet key in report')
        seen_keys.add(key)
        recomputed_months = None
        for ref in outlet['sources']:
            ann = evidence_for(conn, ref['announcement_request_url'], snapshot)
            det = evidence_for(conn, ref['detail_request_url'], snapshot)
            csv_e = evidence_for(conn, ref['csv_request_url'], snapshot)
            for evidence in (ann, det, csv_e):
                if evidence is None or evidence.get('corrupt'):
                    raise ValueError('referenced evidence is unavailable')
            if ann['sha256'] != ref['announcement_raw_sha256'] \
                    or det['sha256'] != ref['detail_raw_sha256'] \
                    or csv_e['sha256'] != ref['csv_raw_sha256']:
                raise ValueError('reference digest does not match stored bytes')
            if (ref['announcement_request_url'], ref['announcement_url']) not in manifest_index \
                    or (ref['detail_request_url'], ref['detail_url']) not in manifest_index \
                    or (ref['csv_request_url'], ref['csv_url']) not in manifest_index:
                raise ValueError('reference points outside the resource manifest')
            _, rows = parse_announcement(ann['raw'])
            logical = rows[ref['announcement_logical_row'] - 1]
            if logical['permit_id'] != outlet['permit_id'] \
                    or logical['holder'] != outlet['holder']:
                raise ValueError('announcement row no longer supports the outlet')
            detail = parse_detail(det['raw'])
            if detail['permit_id'] != outlet['permit_id'] \
                    or detail['holder'] != outlet['holder'] \
                    or detail['outlet_id'] != outlet['outlet_id']:
                raise ValueError('detail identity no longer supports the outlet')
            parsed = parse_quota_csv(csv_e['raw'], outlet['outlet_id'])
            months = parsed['months']
            if recomputed_months is not None and months != recomputed_months:
                raise ValueError('folded sources disagree on monthly quotas')
            recomputed_months = months
            record_numbers = {str(month): record for month, _l, record in months}
            if ref['csv_records'] != record_numbers:
                raise ValueError('CSV record numbers do not match stored bytes')
        if recomputed_months is None:
            raise ValueError('outlet has no source reference')
        if [m['month'] for m in outlet['months']] != [m for m, _l, _r in recomputed_months]:
            raise ValueError('report months do not match stored CSV')
        if [m['licensed_litres'] for m in outlet['months']] != \
                [litres for _m, litres, _r in recomputed_months]:
            raise ValueError('report litres do not match stored CSV')
        if outlet['annual_litres'] != sum(litres for _m, litres, _r in recomputed_months):
            raise ValueError('annual conservation broken against stored CSV')
    if len(seen_keys) != len(report['outlets']):
        raise ValueError('report outlet count mismatch')
    if dataset_identity({k: v for k, v in report.items() if k != 'dataset_id'}) \
            != report['dataset_id']:
        raise ValueError('dataset identity does not match report content')
    return True
