{# The Unity Catalog volume the download task writes FCR PDFs to, in the target's catalog and schema #}
{% macro fcr_volume_name() %}{{ target.catalog }}.{{ target.schema }}.{{ var('fcr_volume') }}{% endmacro %}

{% macro fcr_volume_root() %}/Volumes/{{ target.catalog }}/{{ target.schema }}/{{ var('fcr_volume') }}{% endmacro %}

{# Every file in the volume, read by path; content is only read when a query selects it #}
{% macro fcr_volume_files() %}
read_files('{{ fcr_volume_root() }}', format => 'binaryFile', recursiveFileLookup => true)
{% endmacro %}
