"""Use the scraper straight from Python — no server needed.

    python main.py

Every function returns the same JSON the API does; results are written to
output/*.json.
"""
import json
import os

from workday import refs
from workday.companies import details as company_details
from workday.jobs import details as job_details
from workday.jobs import search as search_jobs

os.makedirs("output", exist_ok=True)


def save(name, data):
    path = os.path.join("output", name)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"saved {path}")


if __name__ == "__main__":
    # any company's career site: a bare tenant, tenant/site or a myworkdayjobs.com link
    nvidia = refs.resolve_site("nvidia")

    # 20 jobs a page, with the site's own filters (country, location, category, ...)
    save("search_nvidia_deep_learning.json", search_jobs(nvidia, query="deep learning", country=["US"]))

    # one posting in full: description, dates, every location, similar jobs
    posting = refs.resolve_job("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/"
                               "US-CA-Santa-Clara/Senior-Deep-Learning-Compiler-Engineer_JR2016814")
    save("job_nvidia_JR2016814.json", job_details(posting))

    # a company's career sites, job counts, top countries and categories
    save("company_nvidia.json", company_details(refs.resolve_company("NVIDIA")))
