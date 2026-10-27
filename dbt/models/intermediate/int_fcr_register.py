"""Curate the search results into the FCR register with the pipeline's own rules (fcr_curator.curation).

Python, because curation is those rules: PDFs only and none from bma.bm, one FCR per institution, year
and kind (de-duplication compares every row at once), each named after its reference-list entity,
matched by domain, then by name. Rebuilt from all the search results every run, so an FCR that no
longer survives curation leaves the register; its file stays in the volume.
"""

from pyspark.sql import DataFrame, SparkSession

INCLUDED_DOC_KINDS = {"fcr"}  # "other" (annual reports, press releases...) stays out
MIN_REPORTING_YEAR = 2020  # None keeps every year


def model(dbt: object, session: SparkSession) -> DataFrame:
    """Return one row per curated FCR, with the volume path its PDF is downloaded to."""
    from fcr_curator.curation import curate, keep_doc_kinds, keep_reported_since  # From the pipeline's wheel
    from fcr_curator.download import document_path
    from fcr_curator.reference import entities_from_rows, entity_resolver
    from fcr_curator.schemas import REGISTER_SCHEMA

    dbt.config(materialized="table")
    # Each ref on its own line: dbt finds a model's refs by reading its code, and misses one in a generator
    reference_list = dbt.ref("bermuda_entities")
    search_results = dbt.ref("int_fcr_search_results")
    entities = entities_from_rows(row.asDict() for row in reference_list.collect())
    results = [row.asDict(recursive=True) for row in search_results.collect()]

    curated = curate(results, resolve_entity=entity_resolver(entities))
    documents = keep_reported_since(keep_doc_kinds(curated, INCLUDED_DOC_KINDS), MIN_REPORTING_YEAR)
    volume_root = dbt.config.get("volume_root")
    rows = [{**document, "volume_path": document_path(volume_root, document)} for document in documents]
    return session.createDataFrame(rows, schema=REGISTER_SCHEMA)
