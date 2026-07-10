"""Unit tests for the OpenSubtitles filename cleaner.

Runs with pytest (`pytest tests/test_opensubtitles.py`) or as a plain script
(`python3 tests/test_opensubtitles.py`) — no other dependencies needed.
"""
from app.services.recap.opensubtitles import clean_movie_filename


CASES = [
    (
        "The.Pout-Pout.Fish.(THENKIRI.COM).2026.WEBRip.DOWNLOADED.FROM.THENKIRI.COM.mkv",
        "The Pout-Pout Fish",
    ),
    ("Inception.2010.1080p.BluRay.x264-SPARKS.mkv", "Inception"),
    ("The.Quiet.Earth.1985.720p.WEB-DL.AAC.H264.mkv", "The Quiet Earth"),
    ("[YTS.MX] Parasite (2019) [1080p].mp4", "Parasite"),
    ("Avengers.Endgame.2019.2160p.4K.BluRay.x265.HEVC.mkv", "Avengers Endgame"),
]


def test_clean_movie_filename_examples():
    for raw, expected in CASES:
        assert clean_movie_filename(raw) == expected, raw


def test_clean_movie_filename_empty():
    assert clean_movie_filename("") == ""
    assert clean_movie_filename("   ") == ""


def test_clean_movie_filename_no_year_still_strips_junk():
    # No year → junk-word sweep kicks in.
    assert clean_movie_filename("Some.Movie.WEBRip.mkv") == "Some Movie"


if __name__ == "__main__":
    test_clean_movie_filename_examples()
    test_clean_movie_filename_empty()
    test_clean_movie_filename_no_year_still_strips_junk()
    print("ok")
