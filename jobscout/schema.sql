-- jobscout schema.
--
-- Two constraints in this file are the whole system, and both work the same way
-- xonvet's PECR gate does: they make a dishonest row *impossible to write*,
-- rather than trusting every future caller to remember the rule.
--
--   1. eligibility.state -- a state that permits an automated application must
--      carry the sentence that decided it and the URL that sentence came from.
--      Nobody types one of these by hand.
--   2. salary.known -- "we know the salary" and "we have a number" are the same
--      claim. A missing salary can never be stored as zero, because RemoteOK
--      returns salary_min: 0 for "not stated" and reading that literally would
--      bury every job that doesn't publish a range.
--
-- Everything else is ordinary bookkeeping.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;


-- ---------------------------------------------------------------- postings --
-- One row per posting per source, kept raw and never rewritten. The same job
-- on five boards is five rows here and one row in `job`.

CREATE TABLE IF NOT EXISTS posting (
    id              INTEGER PRIMARY KEY,
    source          TEXT    NOT NULL,
    source_id       TEXT    NOT NULL,
    url             TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    company         TEXT    NOT NULL,
    description     TEXT,
    posted_at       INTEGER,            -- epoch seconds, source's own timestamp
    location_raw    TEXT,               -- whatever the source said, verbatim
    employment_type TEXT,
    seniority       TEXT,
    apply_url       TEXT,
    apply_email     TEXT,               -- rare, and the only auto-send lane
    tags            TEXT,               -- json array
    timezones       TEXT,               -- json array of utc offsets, if given
    raw             TEXT    NOT NULL,   -- the original record, entire
    first_seen      INTEGER NOT NULL,
    last_seen       INTEGER NOT NULL,
    job_id          INTEGER REFERENCES job(id) ON DELETE SET NULL,
    UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_posting_job     ON posting(job_id);
CREATE INDEX IF NOT EXISTS idx_posting_source_id ON posting(source_id);
CREATE INDEX IF NOT EXISTS idx_posting_seen    ON posting(last_seen);
CREATE INDEX IF NOT EXISTS idx_posting_company ON posting(company);


-- --------------------------------------------------------------------- job --
-- The canonical, deduplicated job. `source_count` is a signal in its own
-- right: a role syndicated to six boards is being pushed hard.

CREATE TABLE IF NOT EXISTS job (
    id            INTEGER PRIMARY KEY,
    dedupe_key    TEXT    NOT NULL UNIQUE,
    title         TEXT    NOT NULL,
    company       TEXT    NOT NULL,
    company_slug  TEXT    NOT NULL,
    canonical_url TEXT,
    description   TEXT,
    posted_at     INTEGER,
    first_seen    INTEGER NOT NULL,
    last_seen     INTEGER NOT NULL,
    source_count  INTEGER NOT NULL DEFAULT 1,
    status        TEXT    NOT NULL DEFAULT 'open'
                          CHECK (status IN ('open', 'stale', 'closed'))
);

CREATE INDEX IF NOT EXISTS idx_job_company ON job(company_slug);
CREATE INDEX IF NOT EXISTS idx_job_status  ON job(status);
CREATE INDEX IF NOT EXISTS idx_job_posted  ON job(posted_at);


-- ------------------------------------------------------------- eligibility --
-- Can the candidate actually take this job? Most "remote" postings are silently
-- geo-locked, so a system that skips this question produces a beautiful ranked
-- list of jobs he cannot have.
--
-- The five permissive states below are the ones an automated application may
-- act on. The CHECK makes each of them unwritable without recorded evidence --
-- the quote, its URL, and which rule fired. UNKNOWN is not a dead end; it is
-- the review queue, and review is a human writing `decided_by = 'human'`.

CREATE TABLE IF NOT EXISTS eligibility (
    job_id         INTEGER PRIMARY KEY REFERENCES job(id) ON DELETE CASCADE,
    state          TEXT    NOT NULL CHECK (state IN (
                       'OPEN_WORLDWIDE',    -- explicitly anywhere
                       'OPEN_REGION',       -- region named, and it includes LK
                       'OPEN_CONTRACTOR',   -- EOR / contractor, any country
                       'ONSITE_LK',         -- on-site in Sri Lanka; he's here
                       'ONSITE_SPONSORED',  -- on-site abroad, sponsorship stated
                       'ONSITE_NO_SPONSOR', -- on-site abroad, no sponsorship
                       'BLOCKED',           -- geo-locked out
                       'UNKNOWN')),         -- no signal; goes to review
    evidence_quote TEXT,
    evidence_url   TEXT,
    rule           TEXT,      -- which rule fired, by name
    decided_by     TEXT    NOT NULL CHECK (decided_by IN ('rule', 'agent', 'human')),
    confidence     REAL,
    decided_at     INTEGER NOT NULL,

    -- The gate. Never relax this to "warn instead" -- the warning is what gets
    -- ignored at 2am by a sender running unattended.
    CHECK (state NOT IN ('OPEN_WORLDWIDE', 'OPEN_REGION', 'OPEN_CONTRACTOR',
                         'ONSITE_LK', 'ONSITE_SPONSORED')
           OR (evidence_quote IS NOT NULL AND length(trim(evidence_quote)) > 0
               AND evidence_url IS NOT NULL AND length(trim(evidence_url)) > 0
               AND rule IS NOT NULL AND length(trim(rule)) > 0))
);

CREATE INDEX IF NOT EXISTS idx_elig_state ON eligibility(state);


-- ------------------------------------------------------------------ salary --
-- Normalised to USD per month, because that is the unit the decision is made
-- in. `known` and "has a number" are the same fact, enforced below.

CREATE TABLE IF NOT EXISTS salary (
    job_id        INTEGER PRIMARY KEY REFERENCES job(id) ON DELETE CASCADE,
    known         INTEGER NOT NULL CHECK (known IN (0, 1)),
    min_usd_month REAL,
    max_usd_month REAL,
    src_currency  TEXT,
    src_period    TEXT CHECK (src_period IN
                      ('hour', 'day', 'week', 'month', 'year') OR src_period IS NULL),
    src_min       REAL,
    src_max       REAL,
    raw           TEXT,      -- exactly what the source said
    derived_by    TEXT,      -- 'structured' | 'text:<pattern name>'
    confidence    REAL,
    decided_at    INTEGER NOT NULL,

    CHECK ((known = 1 AND (min_usd_month IS NOT NULL OR max_usd_month IS NOT NULL))
        OR (known = 0 AND min_usd_month IS NULL AND max_usd_month IS NULL)),
    -- A negative or absurd figure is a parse bug, not a salary.
    CHECK (min_usd_month IS NULL OR (min_usd_month >= 0 AND min_usd_month < 1000000)),
    CHECK (max_usd_month IS NULL OR (max_usd_month >= 0 AND max_usd_month < 1000000))
);

CREATE INDEX IF NOT EXISTS idx_salary_known ON salary(known, max_usd_month);


-- Every salary string the ladder could not parse, so the ladder can be
-- improved from evidence instead of imagination.
CREATE TABLE IF NOT EXISTS salary_unparsed (
    id         INTEGER PRIMARY KEY,
    job_id     INTEGER REFERENCES job(id) ON DELETE CASCADE,
    raw        TEXT NOT NULL,
    source     TEXT,
    seen_at    INTEGER NOT NULL
);


-- Currency -> USD. Refreshed weekly; a stale rate is still far better than
-- refusing to rank a job because the internet was down.
CREATE TABLE IF NOT EXISTS fx (
    currency   TEXT PRIMARY KEY,
    usd_per    REAL NOT NULL CHECK (usd_per > 0),
    fetched_at INTEGER NOT NULL
);


-- ------------------------------------------------------------ source state --
-- Where each source got to, so a daily run continues rather than re-walking
-- the pages it already has.
--
-- Himalayas serves 20 jobs a page against a feed of ~103,000. Fetching the
-- newest 30 pages every morning collected the same 600 jobs forever; the other
-- 102,400 were unreachable not because the API refused but because nothing ever
-- asked for page 31. The cursor lives here.

CREATE TABLE IF NOT EXISTS source_state (
    source     TEXT PRIMARY KEY,
    cursor     TEXT,       -- opaque, whatever the source's paging token is
    page       INTEGER NOT NULL DEFAULT 0,
    exhausted  INTEGER NOT NULL DEFAULT 0 CHECK (exhausted IN (0, 1)),
    updated_at INTEGER NOT NULL
);


-- ------------------------------------------------------------------ boards --
-- Company job boards found by probing, as opposed to companies.yml, which is
-- hand-curated and stays that way. A miss is recorded too: re-probing 1,100
-- companies that answered 404 last month is the wasteful half of discovery.

CREATE TABLE IF NOT EXISTS board (
    company    TEXT NOT NULL,
    slug       TEXT NOT NULL,
    ats        TEXT NOT NULL,
    jobs_seen  INTEGER NOT NULL DEFAULT 0,
    live       INTEGER NOT NULL DEFAULT 1 CHECK (live IN (0, 1)),
    found_at   INTEGER NOT NULL,
    checked_at INTEGER NOT NULL,
    PRIMARY KEY (ats, slug)
);

CREATE INDEX IF NOT EXISTS idx_board_live ON board(live, checked_at);


-- A company that has been probed and produced nothing, so it is not probed
-- again next week. Cheaper than a `live = 0` row per provider.
CREATE TABLE IF NOT EXISTS board_miss (
    company    TEXT PRIMARY KEY,
    probed_at  INTEGER NOT NULL
);


-- ---------------------------------------------------------------- company --
-- What the employer says about itself, fetched once and cached. Feeds the one
-- sentence in a cover letter that could not have been sent to anyone else.

CREATE TABLE IF NOT EXISTS company (
    slug        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    site        TEXT,
    source_url  TEXT,       -- the page the summary came from; quoted as evidence
    summary     TEXT,
    fetched_at  INTEGER,
    failed      INTEGER NOT NULL DEFAULT 0 CHECK (failed IN (0, 1))
);


-- ------------------------------------------------------------------ cities --
-- GeoNames, loaded once by cities.py and matched locally thereafter. Lives in
-- the shared schema so every connection has it -- including the temporary
-- databases the tests build, where geo.apply() would otherwise fail on a
-- missing table rather than simply finding no city.

CREATE TABLE IF NOT EXISTS city (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    norm       TEXT NOT NULL,
    country    TEXT NOT NULL,
    admin1     TEXT,
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    timezone   TEXT NOT NULL,
    population INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_city_norm ON city(norm, population DESC);
CREATE INDEX IF NOT EXISTS idx_city_cc   ON city(country);


-- --------------------------------------------------------------------- geo --
-- Phase 2. Where the company is, and how many working hours it shares with
-- Colombo (UTC+5:30).

CREATE TABLE IF NOT EXISTS geo (
    job_id        INTEGER PRIMARY KEY REFERENCES job(id) ON DELETE CASCADE,
    country       TEXT,      -- ISO 3166-1 alpha-2
    country_name  TEXT,
    region        TEXT,
    lat           REAL,
    lon           REAL,
    precision     TEXT CHECK (precision IN ('city', 'country', 'company',
                                            'region', 'unknown')
                              OR precision IS NULL),
    utc_offset    REAL,      -- hours, may be fractional (+5.5)
    overlap_hours REAL,      -- with a workable Colombo day
    band          TEXT CHECK (band IN ('green', 'amber', 'red') OR band IS NULL),
    decided_at    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_geo_country ON geo(country);
CREATE INDEX IF NOT EXISTS idx_geo_band    ON geo(band);


-- --------------------------------------------------------------------- fit --
-- Two questions the eligibility gate does not ask, added 2026-08-24 after an
-- independent audit of the top 40 found that 49 of 69 verdicts were "well above
-- him" and only 6 jobs would plausibly get him an interview.
--
--   1. **work_mode** -- "open to APAC" does not mean "remote". A job can be
--      geographically open to him and still require a desk in Singapore. Unless
--      it is in Sri Lanka, or comes with a sponsored relocation, it has to be
--      remote or it is not a job he can take.
--   2. **reach** -- would they actually take *him*? Eighteen months across four
--      employers. A Senior title he will never be shortlisted for is worth less
--      to him than a junior one he might get, whatever it pays.
--
-- `viable` is the two answers combined, and it is what the daily list filters
-- on. Stored rather than computed so a job's status can be explained later.

CREATE TABLE IF NOT EXISTS fit (
    job_id     INTEGER PRIMARY KEY REFERENCES job(id) ON DELETE CASCADE,

    work_mode  TEXT NOT NULL CHECK (work_mode IN
                   ('remote', 'hybrid', 'onsite', 'unknown')),
    mode_quote TEXT,
    mode_rule  TEXT,

    reach      TEXT NOT NULL CHECK (reach IN
                   ('likely',      -- his level, his field
                    'plausible',   -- mid, but reachable
                    'stretch',     -- senior; worth a shot, not a plan
                    'no_chance')), -- staff/principal/director, or not his job
    reach_why  TEXT,

    viable     INTEGER NOT NULL CHECK (viable IN (0, 1)),
    viable_why TEXT NOT NULL,
    decided_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fit_viable ON fit(viable, reach);
CREATE INDEX IF NOT EXISTS idx_fit_mode   ON fit(work_mode);


-- -------------------------------------------------------------- freshness --
-- Added 2026-09 after the open-jobs steal: a source's own `posted_at` can be
-- pushed forward by a repost without the job ever having closed, and
-- `score.py` used to trust it outright (`posted or first_seen`), so a bumped
-- listing counted as brand new. `freshness.py` is the one place that decides
-- this now; `score.py` reads `basis_days` off it instead of recomputing.
--
-- Four verdicts, in the order that matters most to the candidate first:
--   ghost      -- still open long past what jobs like it usually take
--   restamped  -- the source's posted_at is newer than our first_seen by more
--                 than two days: evidence of a bump, not a new posting
--   stale      -- older than the par for its kind, not yet a ghost
--   fresh      -- everything else
CREATE TABLE IF NOT EXISTS freshness (
    job_id        INTEGER PRIMARY KEY REFERENCES job(id) ON DELETE CASCADE,
    verdict       TEXT    NOT NULL CHECK (verdict IN
                      ('fresh', 'stale', 'restamped', 'ghost')),
    basis_days    REAL    NOT NULL,   -- age from min(posted_at, first_seen)
    par_days      REAL    NOT NULL,   -- the par this job was measured against
    par_source    TEXT    NOT NULL CHECK (par_source IN ('family', 'global')),
    restamp_days  REAL,               -- posted_at - first_seen, when positive
    decided_at    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_freshness_verdict ON freshness(verdict);


-- ------------------------------------------------------------------- score --

CREATE TABLE IF NOT EXISTS score (
    job_id     INTEGER PRIMARY KEY REFERENCES job(id) ON DELETE CASCADE,
    total      REAL NOT NULL,
    breakdown  TEXT NOT NULL,   -- json: every component, so a rank is explainable
    scored_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_score_total ON score(total DESC);


-- ------------------------------------------------------------- application --
-- Phase 3/4. One row per application actually made.

CREATE TABLE IF NOT EXISTS application (
    id            INTEGER PRIMARY KEY,
    job_id        INTEGER NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    lane          TEXT    NOT NULL CHECK (lane IN ('email', 'portal', 'manual')),
    mode          TEXT    NOT NULL CHECK (mode IN ('auto', 'reviewed', 'manual')),
    resume_variant TEXT,
    cover_letter  TEXT,
    subject       TEXT,
    body          TEXT,
    to_address    TEXT,
    status        TEXT    NOT NULL DEFAULT 'held' CHECK (status IN (
                      'held',      -- inside the hold window, cancellable
                      'cancelled',
                      'sent',
                      'failed',
                      'prepared')),  -- portal lane: ready for one click
    held_until    INTEGER,
    sent_at       INTEGER,
    created_at    INTEGER NOT NULL,
    UNIQUE (job_id)
);

CREATE INDEX IF NOT EXISTS idx_app_status ON application(status);


CREATE TABLE IF NOT EXISTS touch (
    id         INTEGER PRIMARY KEY,
    job_id     INTEGER NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL CHECK (kind IN ('applied', 'followup', 'reply', 'note')),
    channel    TEXT    CHECK (channel IN ('email', 'portal', 'phone', 'other')),
    body       TEXT,
    occurred_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_touch_job ON touch(job_id, occurred_at);


CREATE TABLE IF NOT EXISTS reply (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER REFERENCES job(id) ON DELETE CASCADE,
    from_addr   TEXT,
    subject     TEXT,
    body        TEXT,
    sentiment   TEXT CHECK (sentiment IN
                    ('interview', 'positive', 'neutral', 'rejection') OR sentiment IS NULL),
    received_at INTEGER NOT NULL,
    handled     INTEGER NOT NULL DEFAULT 0 CHECK (handled IN (0, 1))
);


-- ------------------------------------------------------------- agent calls --
-- Every judgment-layer call, with its prompt and reasoning, so a decision that
-- looks wrong in six weeks can still be read.

CREATE TABLE IF NOT EXISTS agent_call (
    id         INTEGER PRIMARY KEY,
    job_id     INTEGER REFERENCES job(id) ON DELETE CASCADE,
    purpose    TEXT NOT NULL,
    backend    TEXT NOT NULL,
    prompt     TEXT NOT NULL,
    reply      TEXT,
    choice     TEXT,
    confidence REAL,
    reasoning  TEXT,
    called_at  INTEGER NOT NULL
);


-- ------------------------------------------------------------- run journal --
-- What each stage did, so `run.py --status` never has to guess.

CREATE TABLE IF NOT EXISTS run_log (
    id         INTEGER PRIMARY KEY,
    stage      TEXT NOT NULL,
    ok         INTEGER NOT NULL CHECK (ok IN (0, 1)),
    detail     TEXT,
    started_at INTEGER NOT NULL,
    ended_at   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_runlog_stage ON run_log(stage, started_at);


-- ------------------------------------------------------- application forms --
-- What the advert's own application page actually asks, so a pack is aimed at
-- the real questions rather than the seven `kit.py` used to guess. Only
-- Greenhouse publishes this; everything else falls back to the generic pack.
--
-- Cached because the form barely changes and refetching 130 of them every run
-- would spend two minutes to learn nothing.

CREATE TABLE IF NOT EXISTS form_question (
    job_id      INTEGER NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    label       TEXT    NOT NULL,
    required    INTEGER NOT NULL CHECK (required IN (0, 1)),
    field_type  TEXT    NOT NULL,
    values_json TEXT,
    vendor      TEXT    NOT NULL,
    fetched_at  INTEGER NOT NULL,
    PRIMARY KEY (job_id, position)
);

CREATE INDEX IF NOT EXISTS idx_formq_job ON form_question(job_id);

-- Jobs whose form was fetched and returned nothing, so a second run does not
-- ask again every morning. A row here means "asked, got no form", which is
-- different from "never asked".
CREATE TABLE IF NOT EXISTS form_miss (
    job_id     INTEGER PRIMARY KEY REFERENCES job(id) ON DELETE CASCADE,
    reason     TEXT    NOT NULL,
    tried_at   INTEGER NOT NULL
);


-- --------------------------------------------------------------------- meta --
-- Key/value flags the code leaves for itself: `needs_vacuum` after a
-- migration that freed a lot of pages. Nothing here is user data.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);


-- ----------------------------------------------------------------- job_text --
-- The advert body for a job: the longest description among its postings.
-- Until 2026-09 normalise.py copied this into job.description as well, which
-- doubled the file. Every stage that reads the body joins this view instead;
-- the column on `job` is kept (NULL) so nothing that still names it breaks.

CREATE VIEW IF NOT EXISTS job_text AS
SELECT j.id AS job_id,
       COALESCE((SELECT p.description FROM posting p WHERE p.job_id = j.id
                 ORDER BY length(p.description) DESC, p.id LIMIT 1),
                j.description) AS description
FROM job j;
