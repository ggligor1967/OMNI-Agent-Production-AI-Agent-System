import os
import sys


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import job_search_tank_adr_improved as search


def test_canonicalize_url_normalizes_locale_variants():
    jobup_en = "https://jobup.ch/en/jobs/detail/f5da67b1-b5fb-4222-95c8-658bb53fe02a"
    jobup_fr = "https://jobup.ch/fr/emplois/detail/f5da67b1-b5fb-4222-95c8-658bb53fe02a"
    scout_de = "https://jobscout24.ch/de/job/1aad668c-907c-426d-a432-85b987258425"
    scout_fr = "https://jobscout24.ch/fr/job/1aad668c-907c-426d-a432-85b987258425"

    assert search.canonicalize_url(jobup_en) == search.canonicalize_url(jobup_fr)
    assert search.canonicalize_url(scout_de) == search.canonicalize_url(scout_fr)


def test_is_result_allowed_rejects_non_swiss_external_listing():
    allowed, reason = search.is_result_allowed(
        "CH",
        "https://stellen-anzeiger.de/jobs/weitere-trucker-jobs-aus-d/job_tankwagenfahrer-mwd-ce-adr-gefahrgut/3925118",
        "Job als Tankwagenfahrer (m/w/d) | CE | ADR Gefahrgut | Stellen ...",
        'Parameters : {"method"=>:get, "title"=>"job-tankwagenfahrer-mwd-ce-adr-gefahrgut"}',
    )

    assert allowed is False
    assert reason == "outside-target-domain"


def test_is_result_allowed_rejects_training_page():
    allowed, reason = search.is_result_allowed(
        "CH",
        "http://sapsi.ch/?catid=0&id=53",
        "ADR 02: Corso di base ADR e cisterne (3 giorni) - SAPSI",
        "SAPSI Scuola Autisti Professionisti Svizzera Italiana ADR course for tank certificates.",
    )

    assert allowed is False
    assert reason == "negative-content"


def test_is_result_allowed_rejects_non_job_career_path():
    allowed, reason = search.is_result_allowed(
        "CH",
        "https://astag.ch/aus-weiterbildung/weiterbildung-lehrgaenge/tankwagenfahrerin",
        "Tankwagenfahrer:in - ASTAG",
        "Suchen Sie eine neue Herausforderung als verantwortungsbewusster Spezialist? Werden Sie Tankwagenfahrer:in.",
    )

    assert allowed is False
    assert reason == "non-job-path"


def test_compute_score_prefers_direct_board_over_aggregator():
    title = "ADR Tankwagenfahrer Schweiz"
    snippet = "Apply now for this ADR tanker role in Aargau, Switzerland."

    direct_score, _ = search.compute_score(
        title,
        snippet,
        "Reliable employer in Aargau looking for ADR tanker drivers.",
        "https://jobs.ch/de/stellenangebote/detail/65d128b6-3661-4975-a0cd-894462a4fa2f",
        "job-board",
    )
    aggregator_score, _ = search.compute_score(
        title,
        "Jobs: Tankerfahrer Adr in Schweiz. Extensive selection and job-mail service.",
        "",
        "https://de.jooble.org/stellenangebote-tankerfahrer-adr/Schweiz",
        "aggregator",
    )

    assert direct_score > aggregator_score


def test_compute_score_penalizes_interstitial_content():
    clean_score, _ = search.compute_score(
        "AUTISTA patente C + ADR Liquidi pericolosi",
        "Role in Lugano, Switzerland for hazardous liquid transport.",
        "ADR liquidi pericolosi, C+CE, Swiss territory knowledge required.",
        "https://example.ch/jobs/adr-driver-lugano",
        "company-page",
    )
    blocked_score, _ = search.compute_score(
        "AUTISTA patente C + ADR Liquidi pericolosi",
        "Role in Lugano, Switzerland for hazardous liquid transport.",
        "We use a security service to protect our website. Your access has been denied.",
        "https://example.ch/jobs/adr-driver-lugano",
        "company-page",
    )

    assert blocked_score < clean_score


def test_compute_score_caps_listing_pages_below_high_relevance():
    score, _ = search.compute_score(
        "18 Tanker driver jobs in Zurich - jobs.ch",
        "Apply now for Tanker driver jobs in Zurich. We have everything for your next job.",
        "",
        "https://jobs.ch/en/vacancies?location=zurich&term=tanker+driver",
        "job-board",
    )

    assert score <= search.NON_DETAIL_SCORE_CAP


def test_compute_score_caps_informational_pages_without_job_signal():
    score, _ = search.compute_score(
        "Planzer hazardous goods logistics: ADR-trained and tested",
        "Including receipt of goods, storage, ADR transport and proper delivery.",
        "Dangerous goods logistics solutions for customers in Switzerland.",
        "https://planzer.ch/en/total-solutions/hazardous-goods",
        "company-page",
    )

    assert score <= search.NON_DETAIL_SCORE_CAP


def test_compute_score_caps_broken_detail_page():
    score, _ = search.compute_score(
        "Chauffeur PL ADR CITERNE - Stellenangebot auf jobs.ch",
        "Real-looking detail result from a board.",
        "jobs.ch - Die angeforderte Seite wurde nicht gefunden. La page demandée n’a pas pu être trouvée.",
        "https://jobs.ch/jobs/detail/65d128b6-3661-4975-a0cd-894462a4fa2f",
        "job-board",
    )

    assert score <= search.NON_DETAIL_SCORE_CAP