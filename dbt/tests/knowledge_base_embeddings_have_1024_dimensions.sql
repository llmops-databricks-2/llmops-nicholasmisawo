-- Every embedding must have the model's 1,024 dimensions, or Lakebase's vector(1024) column rejects it
select source_key, size(embedding) as dimensions
from {{ ref('fcr_knowledge_base') }}
where size(embedding) != 1024
