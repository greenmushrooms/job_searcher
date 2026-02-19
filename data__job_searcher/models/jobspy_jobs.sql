{{
    config(
        materialized='incremental'
        ,unique_key = 'id'
        ,incremental_strategy = 'merge'
    )
}}

SELECT
    sys_profile,
    id,
    site,
    job_url,
    job_url_direct,
    title,
    company,
    "location",
    date_posted,
    job_type,
    salary_source,
    "interval",
    min_amount,
    max_amount,
    currency,
    is_remote,
    job_level,
    job_function,
    listing_type,
    emails,
    description,
    company_industry,
    company_url,
    company_logo,
    company_url_direct,
    company_addresses,
    company_num_employees,
    company_revenue,
    company_description,
    skills,
    experience_range,
    company_rating,
    company_reviews_count,
    vacancy_count,
    work_from_home_type
FROM {{ source('jobspy','import_jobs') }}
{% if is_incremental() %}
    WHERE sys_run_name = '{{ var("run_name") }}'
{% else %}
    WHERE 1=0
{% endif %}
