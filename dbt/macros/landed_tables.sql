{#
  Create every table of the landed source (models/staging/_sources.yml) that doesn't exist yet, from
  its declared columns, so the planning and staging models can read them on a target's very first run,
  before any Python task has appended a row. An existing table is left alone.
#}
{% macro create_landed_tables() %}
    {% if execute %}
        {% for source in graph.sources.values() if source.source_name == 'landed' %}
            {% set definitions = [] %}
            {% for column in source.columns.values() %}
                {% do definitions.append(column.name ~ ' ' ~ column.data_type) %}
            {% endfor %}
            {% do run_query(
                'create table if not exists ' ~ source.database ~ '.' ~ source.schema ~ '.' ~ source.identifier
                ~ ' (' ~ definitions | join(', ') ~ ')'
            ) %}
        {% endfor %}
    {% endif %}
{% endmacro %}
