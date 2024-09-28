import os
import requests

bearer_token = os.getenv('BEARER_TOKEN')


url = "https://api.theirstack.com/v1/jobs/search"
def job_list (job_titles, country, location=None):
    payload = {
        "order_by": [
            {
                "desc": True,
                "field": "date_posted"
            },
            {
                "desc": True,
                "field": "discovered_at"
            }
        ],
        "page": 0,
        "limit": 1,
        "only_yc_companies": False,
        "company_location_pattern_or": location,
        "company_country_code_or": [country],
        "job_title_or": [job_titles],
        "posted_at_max_age_days": 15,
        "job_technology_slug_or": ["dbt"]
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": bearer_token
    }

    response = requests.post(url, json=payload, headers=headers)

    return response.json()