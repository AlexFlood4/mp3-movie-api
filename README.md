# MP3 Movie API

This FastAPI application stores one movie collection in a SQLite database and serves it through a small REST API. `POST /upload` accepts a CSV with exactly the columns `title`, `release_year`, `rating`, and `genre`, checks that it has at least one data row, and replaces the current collection. `src/database.py` opens SQLite connections and creates the `movies` table, while `src/storage.py` (`MovieStorage`) saves, reads, counts, and filters movies with SQL, so the data survives server restarts in `data/movies.db`. `GET /movies` returns stored movies with a total count, and `GET /movies/search` filters by minimum rating with SQL, sorting by rating and returning only the requested columns. `POST /movies/process` reads the collection, validates and cleans it with the MP1 pipeline functions in `data_pipeline/` (duplicate removal, missing-value handling, and outlier removal), and saves the cleaned result back to SQLite. `src/cache.py` (`MovieCache`) uses FakeRedis to cache `GET /movies` responses per `limit` for 90 seconds, so repeated requests skip the database. After every successful upload or processing run, the cache is cleared so later reads never return outdated rows. `src/models.py` defines the Pydantic request and response models, and `src/middleware.py` logs each request with a request ID.

## Run the API

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
python -m pip install -r requirements.txt
python -m uvicorn main:app --reload
```

Then open http://127.0.0.1:8000/docs.

## Run the tests

```bash
python -m pytest tests -v
```

## Sample processing request

After uploading `fixtures/sample_movies.csv`, send this body to `POST /movies/process`:

```json
{
  "validation": {
    "required_columns": ["title", "release_year", "rating", "genre"],
    "numeric_columns": ["release_year", "rating"]
  },
  "processing": {
    "remove_duplicates": true,
    "missing": {"enabled": true, "axis": "rows"},
    "outliers": {
      "enabled": true,
      "columns": ["rating"],
      "method": "iqr",
      "threshold": 1.5
    }
  }
}
```

Expected result:

```json
{
  "validation_rows_removed": 0,
  "cleaning_report": {
    "rows_before": 20,
    "rows_after": 16,
    "rows_removed": 4,
    "columns_before": 4,
    "columns_after": 4,
    "columns_removed": 0
  }
}
```

Two duplicate rows (Movie B and Movie D) and two rating outliers (Movie Q and Movie R) are removed, so `GET /movies?limit=100` then returns 16 movies.
