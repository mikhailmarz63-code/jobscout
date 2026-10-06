# jobscout

A personal job finder that reads thousands of remote job adverts every morning and keeps only the ones worth an evening: jobs the candidate can legally take, from where they live, at a salary that clears their floor, at a level they could actually get.

On its first full pass it narrowed **2,078 jobs down to 79**.

It is written in Python with SQLite, has **316 automated tests**, and runs on a laptop with no server and no paid APIs.

## What it does

Each morning `run.py` walks a pipeline. Every stage is idempotent, so a run that dies halfway leaves the database consistent and the next run finishes the job.

| Stage | File | What it decides |
|---|---|---|
| Ingest | `sources/` | Pulls adverts from 7 public job feeds and from company job boards read straight from their ATS (Greenhouse, Ashby, Lever and others) |
| Normalise | `normalise.py` | One posting seen on three boards becomes one job |
| Salary | `salary.py` | Parses free text pay into a monthly USD range. A missing salary is never treated as zero |
| Eligibility | `eligibility.py` | Can this person legally take it from their country? Open worldwide, open to their region, contractor, sponsored, or blocked |
| Geography | `geo.py`, `cities.py` | Where the job really is, and how many working hours overlap with the candidate's day |
| Fit | `fit.py` | Is it really remote, and is the seniority one they could be shortlisted for? |
| Score | `score.py` | Ranks what survives, with the reasons shown rather than hidden in a number |
| Judge (optional) | `judge.py` | Asks Claude to score the job against the CV. Every score must quote both the advert and the CV, and the code checks the quotes are real before it trusts the score |

## Tools around the pipeline

* **`ats.py`** checks a CV against six applicant tracking systems (Workday, Taleo, iCIMS, SuccessFactors, Greenhouse, Lever), lints it, and compares it with a specific advert. It never invents a claim: anything the CV cannot back up comes back marked `[NEEDS YOU]`.
* **`kit.py`** builds an application pack for one job from the candidate's own files: matched stories, answers to common form questions, and a salary answer anchored on the advert's own range.
* **`extension/`** is a Chrome extension that reads an application form and offers the prepared answers. It copies and fills. It never submits.
* **`board.py`** is a small local web board for reviewing the shortlist, with token auth, CSRF protection and a strict content security policy.

## Design choices worth reading

* **Unknown is not a rejection.** Many of the best paying roles publish no salary, so a salary filter only applies to known numbers.
* **Wrong in the permissive direction is the expensive bug.** An eligibility mistake means an application to a job the candidate cannot take. Most tests sit on the eligibility gate and on salary parsing, and nearly every one is a real defect kept so it cannot come back.
* **Private by default.** The database, the settings and the generated packs never leave the machine. City lookups run against a local GeoNames copy so no third party learns which cities are being searched. The HTTP user agent carries no identity.
* **The model is checked, not trusted.** The Claude judge is off by default, and its output is rejected unless its quotes appear in the source text.

## Quick start

```bash
pip install -r requirements.txt
cd jobscout
cp settings.example.yml settings.yml      # then fill in your own details
python3 -m pytest -q                      # 316 passed, 17 skipped on a fresh clone
python3 run.py                            # the daily pass
python3 run.py --status                   # what state everything is in
python3 ats.py --self                     # score the sample CV against six ATS platforms
python3 board.py --standalone             # local review board
```

Put your own CV at `resume/resume-ai-solutions-engineer.md`. The one in the repo is a made up sample so everything runs on a fresh clone. The 17 skipped tests need your own `resume/STAR-STORIES.md` and `resume/INTERVIEW-LINES.md`.

## Layout

```
jobscout/            the pipeline, tools, board, extension and tests
resume/              the CV the tools read (sample included)
job-applications/    matcher.py, shared CV variant matching
```

## Data sources and credit

* Job feeds: Himalayas, Remotive, RemoteOK, Jobicy, Arbeitnow, We Work Remotely, Hacker News "Who is hiring", and public ATS boards
* [open-jobs](https://github.com/elliottdehn/open-jobs) index (CC0), off by default
* City data: [GeoNames](https://www.geonames.org/) (CC BY 4.0)
* Country outlines: [Natural Earth](https://www.naturalearthdata.com/) (public domain)

## How it was built

Built by Mikhail Marzook using Claude Code. The comments in each module explain why each rule exists, usually by naming the bug that caused it.
