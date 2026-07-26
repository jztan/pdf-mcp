"""Tests for the financial-corpus fetch helper (pure logic only)."""

import hashlib

import pytest

from scripts.fetch_financial_corpus import SEC_USER_AGENT, sha256_file, ua_for_url


class TestUaForUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.sec.gov/Archives/edgar/data/789019/x/d61995dars.pdf",
            "https://data.sec.gov/submissions/CIK0000789019.json",
        ],
    )
    def test_sec_hosts_get_the_declaring_user_agent(self, url):
        assert ua_for_url(url) == SEC_USER_AGENT

    @pytest.mark.parametrize(
        "url",
        [
            "https://s2.q4cdn.com/470004039/files/x.pdf",
            "https://www.jpmorganchase.com/content/dam/x.pdf",
            "https://notsec.gov.example.com/x.pdf",
        ],
    )
    def test_other_hosts_get_a_browser_user_agent(self, url):
        assert ua_for_url(url) != SEC_USER_AGENT
        assert "Mozilla" in ua_for_url(url)


class TestSha256File:
    def test_matches_hashlib(self, tmp_path):
        p = tmp_path / "x.pdf"
        p.write_bytes(b"%PDF-1.7 hello")
        assert sha256_file(p) == hashlib.sha256(b"%PDF-1.7 hello").hexdigest()
