-- Licence classes whose search priority doesn't fit whether they file an FCR: every filer needs one, so
-- its holders are searched in order, and a class that doesn't file shouldn't have one.
select licence_type, files_fcr, fcr_search_priority
from {{ ref('bma_licence_classes') }}
where files_fcr = (fcr_search_priority is null)
