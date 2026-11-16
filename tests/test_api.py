import pytest

from src import MovieStorage
import main

ORIGINAL_TITLES = [
    "Movie A", "Movie B", "Movie B", "Movie C", "Movie D", "Movie D",
    "Movie E", "Movie F", "Movie G", "Movie H", "Movie I", "Movie J",
    "Movie K", "Movie L", "Movie M", "Movie N", "Movie O", "Movie P",
    "Movie Q", "Movie R",
]
PROCESSED_TITLES = [f"Movie {letter}" for letter in "ABCDEFGHIJKLMNOP"]


def test_movies_persist(client, test_storage, uploaded_movies):
    other_storage = MovieStorage(test_storage.db_path)

    df = other_storage.get()
    assert len(df) == 20
    assert other_storage.count_movies() == 20
    assert list(df.columns) == ["title", "release_year", "rating", "genre"]
    assert df["title"].tolist() == ORIGINAL_TITLES

    stored_ids = [row["id"] for row in other_storage.select_columns(["title"], limit=100)]

    response = client.get("/movies", params={"limit": 100})
    assert response.status_code == 200
    api_ids = [record["id"] for record in response.json()["data"]]

    assert stored_ids == list(range(1, 21))
    assert api_ids == list(range(1, 21))


def test_cache_and_processing(client, uploaded_movies, processing_body):
    # Populate the cache with two separate limits.
    response_100 = client.get("/movies", params={"limit": 100})
    assert response_100.status_code == 200
    assert main.cache.get(100) == response_100.json()

    response_1 = client.get("/movies", params={"limit": 1})
    assert response_1.status_code == 200
    assert main.cache.get(1) == response_1.json()

    # Repeat limit=100: served from the cache, same body.
    repeat = client.get("/movies", params={"limit": 100})
    assert repeat.status_code == 200
    assert repeat.json() == response_100.json()

    # Processing writes to SQLite and must clear every cached limit.
    response = client.post("/movies/process", json=processing_body)
    assert response.status_code == 200
    body = response.json()
    assert body["validation_rows_removed"] == 0
    assert body["cleaning_report"] == {
        "rows_before": 20,
        "rows_after": 16,
        "rows_removed": 4,
        "columns_before": 4,
        "columns_after": 4,
        "columns_removed": 0,
    }
    assert main.cache.get(100) is None
    assert main.cache.get(1) is None

    # Retrieve again: fresh data from SQLite, then cached.
    response = client.get("/movies", params={"limit": 100})
    assert response.status_code == 200
    body = response.json()
    assert body["total_rows"] == 16
    assert [record["title"] for record in body["data"]] == PROCESSED_TITLES
    assert [record["id"] for record in body["data"]] == list(range(1, 17))
    assert main.cache.get(100) == body


def test_cache_hit_skips_database(client, uploaded_movies, monkeypatch):
    first = client.get("/movies", params={"limit": 100})
    assert first.status_code == 200
    first_body = first.json()

    def fail_if_called(*args, **kwargs):
        pytest.fail("The database was queried even though the response was cached")

    monkeypatch.setattr(main.storage, "select_columns", fail_if_called)
    monkeypatch.setattr(main.storage, "count_movies", fail_if_called)

    second = client.get("/movies", params={"limit": 100})
    assert second.status_code == 200
    assert second.json() == first_body


# Additional tests (ungraded)

def test_upload_csv(client, sample_csv_bytes):
    response = client.post(
        "/upload",
        files={"file": ("sample_movies.csv", sample_csv_bytes, "text/csv")}
    )

    assert response.status_code == 200
    assert response.json() == {
        "filename": "sample_movies.csv",
        "rows": 20,
        "columns": 4,
        "column_names": ["title", "release_year", "rating", "genre"],
    }


def test_upload_replaces_collection(client, uploaded_movies, sample_csv_bytes):
    response = client.post(
        "/upload",
        files={"file": ("sample_movies.csv", sample_csv_bytes, "text/csv")}
    )
    assert response.status_code == 200

    body = client.get("/movies", params={"limit": 100}).json()
    assert body["total_rows"] == 20
    assert [record["id"] for record in body["data"]] == list(range(1, 21))


def test_search_movies(client, uploaded_movies):
    response = client.get("/movies/search", params={"min_rating": 8.5, "limit": 2})
    assert response.status_code == 200
    assert response.json() == [
        {"id": 4, "title": "Movie C"},
        {"id": 2, "title": "Movie B"},
    ]

    response = client.get(
        "/movies/search",
        params={"min_rating": 8.5, "limit": 2, "columns": ["title", "rating"]},
    )
    assert response.status_code == 200
    assert response.json() == [
        {"id": 4, "title": "Movie C", "rating": 9.0},
        {"id": 2, "title": "Movie B", "rating": 8.5},
    ]


def test_search_unsupported_column(client, uploaded_movies):
    response = client.get("/movies/search", params={"columns": "unknown"})

    assert response.status_code == 400


def test_process_and_retrieve(client, uploaded_movies, processing_body):
    response = client.post("/movies/process", json=processing_body)

    assert response.status_code == 200
    body = response.json()
    assert body["validation_rows_removed"] == 0
    assert body["cleaning_report"]["rows_before"] == 20
    assert body["cleaning_report"]["rows_after"] == 16

    response = client.get("/movies", params={"limit": 100})

    assert response.status_code == 200
    body = response.json()
    assert body["total_rows"] == 16
    assert [record["id"] for record in body["data"]] == list(range(1, 17))


def test_unsupported_extension(client):
    response = client.post(
        "/upload",
        files={"file": ("movies.txt", b"hello", "text/plain")}
    )

    assert response.status_code == 400
    assert client.get("/health").status_code == 200


def test_bad_upload_keeps_stored_data(client, uploaded_movies):
    response = client.post(
        "/upload",
        files={"file": ("bad.csv", b"name,score\nA,1\n", "text/csv")}
    )
    assert response.status_code == 400

    response = client.post(
        "/upload",
        files={"file": ("empty.csv", b"title,release_year,rating,genre\n", "text/csv")}
    )
    assert response.status_code == 400

    assert client.get("/movies").json()["total_rows"] == 20


def test_missing_values_round_trip(client, processing_body):
    csv_bytes = b"title,release_year,rating,genre\nMovie A,2020,8.0,Drama\nMovie B,,7.5,\n"
    response = client.post("/upload", files={"file": ("m.csv", csv_bytes, "text/csv")})
    assert response.status_code == 200

    data = client.get("/movies").json()["data"]
    assert data[1] == {"id": 2, "title": "Movie B", "release_year": None,
                       "rating": 7.5, "genre": None}

    processing_body["processing"]["outliers"]["enabled"] = False
    response = client.post("/movies/process", json=processing_body)
    assert response.status_code == 200
    assert client.get("/movies").json()["total_rows"] == 1


def test_reupload_clears_cache(client, uploaded_movies, processing_body, sample_csv_bytes):
    client.post("/movies/process", json=processing_body)
    assert client.get("/movies").json()["total_rows"] == 16

    client.post("/upload", files={"file": ("sample_movies.csv", sample_csv_bytes, "text/csv")})
    assert main.cache.get(100) is None
    assert client.get("/movies").json()["total_rows"] == 20


def test_search_no_matches(client, uploaded_movies):
    response = client.get("/movies/search", params={"min_rating": 10})
    assert response.status_code == 200
    assert response.json() == []


def test_storage_rejects_bad_columns(test_storage):
    for method in (test_storage.select_columns,
                   lambda cols: test_storage.query_movies(cols, 0, 5)):
        with pytest.raises(ValueError):
            method([])
        with pytest.raises(ValueError):
            method(["unknown"])


def test_failed_processing_keeps_database_and_cache(client, uploaded_movies, processing_body):
    cached = client.get("/movies").json()
    processing_body["processing"]["outliers"]["method"] = "bad"

    response = client.post("/movies/process", json=processing_body)
    assert response.status_code == 400
    assert main.cache.get(100) == cached
    assert main.storage.count_movies() == 20


def test_empty_collection(client):
    assert client.get("/movies").json() == {"data": [], "total_rows": 0}
