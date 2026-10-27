-- Licence types on the BMA's list that bma_licence_classes doesn't describe: a new or renamed class
-- would otherwise be treated as not filing an FCR. Any row returned fails the test; add the class.
select distinct licence_type
from {{ ref('stg_bma_insurers') }}
lateral view explode(licence_types) as licence_type
where licence_type not in (select licence_type from {{ ref('bma_licence_classes') }})
