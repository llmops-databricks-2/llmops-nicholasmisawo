-- The PDFs the download task put in the volume. A file's path says which FCR it is
-- (<source>/<document_id>.<file_type>, see fcr_curator.download), and its modified time is when it arrived
select
    regexp_replace(path, '^dbfs:', '') as volume_path,
    modificationTime as downloaded_at
from {{ fcr_volume_files() }}
