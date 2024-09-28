import json
from job_search import job_list


job_titles = ['data engineer', 'software developer']
country = ['CA']
location = 'toronto'
result = job_list(job_titles=job_titles, country=country, location=location)

print(result)
with open('job_list_sample_2.json', 'w') as json_file:
    json.dump(result, json_file, indent=4)