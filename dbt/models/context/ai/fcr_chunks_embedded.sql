-- Chunks joined to the vector made for their current text; a chunk whose text changed waits for its
-- new vector rather than being served with a stale one
{{ config(materialized='view') }}
select chunk.*, vector.embedding
from {{ ref('fcr_chunks_meta') }} as chunk
inner join {{ ref('fcr_chunk_embeddings') }} as vector
    on vector.id = chunk.id and vector.content_hash = chunk.content_hash
