"""Run mrfkit's CSV reader behind the production parser's old staging API.

The CSV tests ported from the production parser assert on the calls it made
to a staging object (``stage_charge_item``, ``stage_payer_rate`` ...).
This shim reads the CSV with :func:`mrfkit.csv_reader.iter_csv` and replays
each record as the matching call, so those tests check mrfkit unchanged.
"""

import logging

from mrfkit.csv_reader import iter_csv
from mrfkit.json_reader import records_from_items
from mrfkit.records import (
    ChargeItem, FileMetadata, HeaderMapping, PayerRate, StandardCharge, UnmappedCell,
)
from mrfkit.reference import ParseStats


class _Forward(logging.Handler):
    def __init__(self, log):
        super().__init__(logging.DEBUG)
        self._log = log

    def emit(self, record):
        prefix = "WARNING: " if record.levelno >= logging.WARNING else ""
        self._log(prefix + record.getMessage())


def parse_csv_to_staging(file_handle, ingestor, hospital_id=1, run_id=1, log=print,
                         prescan_info=None, ingest_config=None, db_synonyms=None):
    config = ingest_config or {}
    return _run(lambda stats: iter_csv(
        file_handle, header_overrides=config.get("header_overrides"),
        code_extraction=config.get("code_extraction"), extra_synonyms=db_synonyms,
        stats=stats), ingestor, hospital_id, run_id, log, ingest_config)


def _process_json_items_to_staging(items_iter, ingestor, hospital_id=1, run_id=1, log=print,
                                   ingest_config=None):
    config = ingest_config or {}
    return _run(lambda stats: records_from_items(
        items_iter, header_overrides=config.get("header_overrides"),
        code_extraction=config.get("code_extraction"), stats=stats),
        ingestor, hospital_id, run_id, log, ingest_config)


def _run(read, ingestor, hospital_id, run_id, log, ingest_config):
    stats = ParseStats()
    logger = logging.getLogger("mrfkit")
    handler, old_level = _Forward(log), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    mappings = []
    try:
        for record in read(stats):
            if isinstance(record, HeaderMapping):
                mappings.append((record.source_header, record.normalized, record.mapped_to))
            else:
                _replay(record, ingestor, hospital_id)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    ingestor.insert_header_mappings(run_id, mappings)
    if ingest_config is not None:
        if stats.rejected_codes:
            ingest_config["_rejected_code_stats"] = {
                "count": stats.rejected_codes, "samples": stats.rejected_code_samples}
        if stats.baked_modifier_splits:
            ingest_config["_baked_modifier_split_stats"] = {
                "count": stats.baked_modifier_splits, "by_modifier": stats.baked_modifiers,
                "samples": stats.baked_modifier_samples}
    ingestor.flush_all_staging()
    return stats.warnings


def header_mappings(ingestor):
    """{source_header: mapped_to} from the shim's insert_header_mappings call."""
    (_run_id, rows), _ = ingestor.insert_header_mappings.call_args
    return {header: mapped for header, _normalized, mapped in rows}


def _replay(r, ingestor, hospital_id):
    if isinstance(r, ChargeItem):
        ingestor.stage_charge_item(
            r.code, r.code_type, r.description, r.billing_class, setting=r.setting,
            modifiers=r.modifiers, drug_unit_of_measurement=r.drug_unit_of_measurement,
            drug_type_of_measurement=r.drug_type_of_measurement,
            additional_generic_notes=r.additional_generic_notes)
    elif isinstance(r, StandardCharge):
        ingestor.stage_charge_standard(
            r.code, r.code_type, r.description, r.billing_class, r.setting,
            r.gross_charge, r.discounted_cash_price, r.min_negotiated_rate,
            r.max_negotiated_rate, modifiers=r.modifiers)
    elif isinstance(r, PayerRate):
        ingestor.stage_payer_rate(
            r.code, r.code_type, r.description, r.billing_class,
            payer_name=r.payer_name, raw_payer_name=r.raw_payer_name,
            plan_category=r.plan_category, plan_network=r.plan_network, plan_name=r.plan_name,
            item_setting=r.setting, negotiated_rate=r.negotiated_rate,
            negotiated_percentage=r.negotiated_percentage,
            negotiated_algorithm=r.negotiated_algorithm, methodology=r.methodology,
            estimated_amount=r.estimated_amount, rate_billing_class=r.rate_billing_class,
            setting=r.rate_setting, additional_notes=r.additional_notes, footnote=r.footnote,
            median_amount=r.median_amount, pct_10=r.pct_10, pct_90=r.pct_90,
            claim_count=r.claim_count, item_modifiers=r.modifiers)
    elif isinstance(r, UnmappedCell):
        ingestor.stage_unmapped(
            r.code, r.code_type, r.description, r.billing_class, r.setting,
            r.source_column, r.value, item_modifiers=r.modifiers)
    elif isinstance(r, FileMetadata):
        location = r.hospital_location or r.hospital_name
        if location:
            ingestor.upsert_hospital_locations(
                hospital_id, [location], [r.hospital_address] if r.hospital_address else None,
                license_number=r.license_number, license_state=r.license_state,
                npi=r.type_2_npi)
