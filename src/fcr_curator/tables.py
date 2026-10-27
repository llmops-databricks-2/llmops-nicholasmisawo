"""The names of the tables the Python tasks land, and of the dbt tables they read.

Landed tables are append-only facts, stamped with the run that landed them (see landing.py); dbt reads
them as sources (dbt/models/staging/_sources.yml) and works out the current state.
"""

# Appended by search_fcrs
SEARCH_HITS_TABLE = "raw_fcr_search_hits"  # Every FCR PDF a run found, by search or on a disclosures page
SEARCHES_TABLE = "raw_fcr_searches"  # Every billed SerpAPI search, one row per results page
PAGE_READS_TABLE = "raw_fcr_page_reads"  # Every disclosures page that loaded
# Appended by download_fcrs
DOWNLOAD_ATTEMPTS_TABLE = "raw_fcr_download_attempts"  # Every attempt to fetch an FCR's PDF, and its outcome

# Built by dbt and read by the Python tasks
SEARCH_PLAN_TABLE = "fcr_search_plan"  # The searches due this run, in order (dbt/models/marts/fcr_search_plan.py)
REGISTER_TABLE = "fcr_register"  # Curated FCRs, one per company and year (dbt/models/marts/fcr_register.sql)
KNOWLEDGE_BASE_TABLE = "fcr_knowledge_base"  # Synced to Lakebase for search (dbt/models/context/ai)
