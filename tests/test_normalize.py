from jobsearch.matching.normalize import (
    detect_remote_type,
    detect_seniority,
    location_matches,
    normalize_employment_type,
    slugify,
    strip_html,
    title_matches,
)


class TestStripHtml:
    def test_keeps_paragraph_and_list_structure(self):
        text = strip_html("<p>We need:</p><ul><li>SQL</li><li>Python</li></ul>")
        assert "We need:" in text
        assert "- SQL" in text and "- Python" in text

    def test_decodes_entities_and_drops_tags(self):
        assert strip_html("<p>R&amp;D at Foo&nbsp;Ltd</p>") == "R&D at Foo Ltd"

    def test_handles_none_and_empty(self):
        assert strip_html(None) == ""
        assert strip_html("") == ""

    def test_collapses_runs_of_blank_lines(self):
        assert "\n\n\n" not in strip_html("<p>a</p><p></p><p></p><p>b</p>")


class TestEmploymentType:
    def test_recognises_common_spellings(self):
        assert normalize_employment_type("Full-Time") == "full_time"
        assert normalize_employment_type("Vollzeit") == "full_time"
        assert normalize_employment_type("Freelance / B2B") == "contract"
        assert normalize_employment_type("Werkstudent") == "internship"

    def test_internship_wins_over_full_time(self):
        # "Full-time internship" is an internship, not a full-time role.
        assert normalize_employment_type("Full-time internship") == "internship"

    def test_unknown_returns_empty(self):
        assert normalize_employment_type("", None) == ""
        assert normalize_employment_type("Something else entirely") == ""


class TestRemoteType:
    def test_hybrid_beats_remote(self):
        # Postings routinely say "hybrid - 2 days remote"; that is hybrid.
        assert detect_remote_type("Hybrid — partially remote") == "hybrid"

    def test_plain_remote(self):
        assert detect_remote_type("Remote (EU)") == "remote"

    def test_onsite(self):
        assert detect_remote_type("On-site in Berlin") == "onsite"

    def test_silent_posting(self):
        assert detect_remote_type("Berlin") == ""


class TestSeniority:
    def test_detects_levels(self):
        assert detect_seniority("Senior Data Analyst") == "senior"
        assert detect_seniority("Junior Developer") == "junior"
        assert detect_seniority("Head of Engineering") == "manager"
        assert detect_seniority("Data Analyst Intern") == "intern"

    def test_unsignalled_title(self):
        assert detect_seniority("Data Analyst") == ""


class TestTitleMatching:
    def test_matches_within_a_longer_title(self):
        assert title_matches("Senior Data Analyst, Growth", ["data analyst"])
        assert title_matches("Analyst, Data Platform", ["data analyst"])

    def test_rejects_a_different_role(self):
        assert not title_matches("Data Engineer", ["data analyst"])

    def test_empty_preference_matches_everything(self):
        assert title_matches("Anything At All", [])

    def test_is_accent_and_case_insensitive(self):
        assert title_matches("DÄTA ANALYST", ["data analyst"])


class TestLocationMatching:
    def test_named_location(self):
        assert location_matches("Tallinn, Estonia", "", ["Tallinn"], True)

    def test_rejects_elsewhere(self):
        assert not location_matches("Tokyo, Japan", "", ["Tallinn"], True)

    def test_worldwide_remote_accepted_when_opted_in(self):
        assert location_matches("Anywhere", "remote", ["Tallinn"], True)

    def test_worldwide_remote_rejected_when_opted_out(self):
        assert not location_matches("Anywhere", "remote", ["Tallinn"], False)

    def test_unstated_location_kept_only_for_remote(self):
        assert location_matches("", "remote", ["Tallinn"], True)
        assert not location_matches("", "", ["Tallinn"], True)


def test_slugify_strips_accents_and_punctuation():
    assert slugify("Zürich — Söder & Co.") == "zurich-soder-co"
