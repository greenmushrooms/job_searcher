FROM prefecthq/prefect:3-python3.12

RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/prefect

RUN pip install --no-cache-dir \
    "prefect>=3.4.24" \
    pandas \
    requests \
    python-jobspy \
    "prefect-dbt>=0.7.0" \
    sqlalchemy \
    beautifulsoup4 \
    lxml \
    "psycopg2-binary==2.9.9" \
    "dbt-postgres>=1.8.1" \
    psycopg2-binary

COPY . /opt/prefect/
