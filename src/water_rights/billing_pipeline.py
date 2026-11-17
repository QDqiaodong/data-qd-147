"""Pipeline wiring the strict input contract to the exact bill solver."""
from .billing_model import stamp, validate_input
from .billing_audit import audit_customer


def audit(data):
    """Validate the document and produce the full billing audit report."""
    normalized = validate_input(data)
    meters = normalized['meters']
    readings = normalized['readings']
    raw_counts = normalized['raw_reading_counts']
    cycles_by_customer = normalized['cycles']
    rates = normalized['rates']

    report = {'schema_version': 1, 'kind': 'billing-audit', 'customers': []}
    for customer in sorted(cycles_by_customer):
        meter_id = normalized['customers'][customer]
        meter = meters[meter_id]
        meter_readings = readings[meter_id]
        cycles = cycles_by_customer.get(customer, [])
        result_cycles, intervals = audit_customer(
            meter, meter_readings, cycles, rates)
        duplicates = raw_counts[meter_id] - len(meter_readings)
        report['customers'].append({
            'customer': customer,
            'meter': {
                'id': meter.id,
                'modulus': meter.modulus,
                'max_kwh_per_hour': meter.max_kwh_per_hour,
            },
            'readings': [
                {'at': stamp(reading.at), 'register': reading.register}
                for reading in meter_readings
            ],
            'folded_duplicate_readings': duplicates,
            'billed_intervals': len(intervals),
            'cycles': result_cycles,
        })
    return report
