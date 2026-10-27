-- Every chunk with its FCR's current metadata, read from the register so a renamed company or a
-- corrected year reaches every chunk on the next run
select
    chunk.id,
    chunk.document_id,
    chunk.chunk_seq,
    chunk.page_id,
    chunk.chunk_text,
    chunk.content_hash,
    fcr.company,
    fcr.fcr_class,
    fcr.business,
    fcr.registered_name,
    fcr.institution,
    fcr.domain,
    fcr.title,
    fcr.reporting_year,
    fcr.file_url
from {{ ref('chunk_fcr_sections') }} as chunk
inner join {{ ref('int_fcr_documents') }} as fcr
    on fcr.document_id = chunk.document_id
