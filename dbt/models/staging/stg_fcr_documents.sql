-- The curated FCR register, keeping FCRs whose PDF is in the volume (only those can be parsed)
select
    document_id,
    registered_name,
    institution,
    domain,
    title,
    reporting_year,
    file_url,
    volume_path
from {{ source('landed', 'silver_fcr_documents') }}
where volume_path is not null
