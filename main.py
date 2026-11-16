import io
import logging
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, HTTPException, Query, UploadFile

from data_pipeline import create_cleaning_report, process_data, validate_dataframe
from src import (
    HealthResponse,
    MovieCache,
    MovieDataResponse,
    MovieProcessingReport,
    MovieStorage,
    MovieUploadInfo,
    ProcessConfig,
    log_requests,
    setup_logging,
)
from src.storage import MOVIE_COLUMNS

setup_logging(verbose=True)
logger = logging.getLogger(__name__)

app = FastAPI(title="Movie API", version="1.0.0")
app.middleware("http")(log_requests)
storage = MovieStorage()
cache = MovieCache()


@app.get("/health", response_model=HealthResponse)
def health_check():
    """Return the application status and version."""
    return HealthResponse(status="ok", version=app.version)


@app.post("/upload", response_model=MovieUploadInfo)
async def upload_movies(file: UploadFile = File(...)):
    """Read a CSV upload and replace the stored movie collection."""
    filename = file.filename or ""
    extension = Path(filename).suffix.lower()
    if extension != ".csv":
        logger.warning("Unsupported file format: %s", extension or "(none)")
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file format: '{extension or '(none)'}'. Only .csv files are accepted.",
        )

    contents = await file.read()
    try:
        df = pd.read_csv(io.BytesIO(contents))
    except pd.errors.EmptyDataError:
        logger.warning("Empty CSV upload: %s", filename)
        raise HTTPException(status_code=400, detail="The uploaded CSV file is empty.")
    except pd.errors.ParserError as exc:
        logger.warning("Could not parse CSV %s: %s", filename, exc)
        raise HTTPException(
            status_code=400,
            detail="The uploaded file could not be parsed as CSV. Check that it is well-formed.",
        )
    except UnicodeDecodeError:
        logger.warning("Could not decode CSV %s", filename)
        raise HTTPException(
            status_code=400,
            detail="The uploaded file could not be decoded. Save it as UTF-8 and try again.",
        )

    column_names = [str(col) for col in df.columns]
    if len(column_names) != len(MOVIE_COLUMNS) or set(column_names) != set(MOVIE_COLUMNS):
        logger.warning("Unexpected columns in %s: %s", filename, column_names)
        raise HTTPException(
            status_code=400,
            detail=f"The CSV must contain exactly these columns: {MOVIE_COLUMNS}.",
        )

    if df.empty:
        logger.warning("CSV upload has no data rows: %s", filename)
        raise HTTPException(status_code=400, detail="The uploaded CSV has no data rows.")

    df = df[MOVIE_COLUMNS]

    storage.save(df)
    cache.invalidate()
    logger.info("Uploaded movies: filename=%s, rows=%d", filename, len(df))
    return MovieUploadInfo(
        filename=filename,
        rows=len(df),
        columns=len(df.columns),
        column_names=list(df.columns),
    )


@app.get("/movies", response_model=MovieDataResponse)
def get_movies(limit: int = Query(default=100, ge=1)):
    """Return up to limit stored movies and the total movie count."""
    cached = cache.get(limit)
    if cached is not None:
        logger.info("Cache hit for movies: limit=%d", limit)
        return cached

    records = storage.select_columns(MOVIE_COLUMNS, limit)
    total_rows = storage.count_movies()
    response = {"data": records, "total_rows": total_rows}

    cache.set(limit, response)

    logger.info("Retrieved movies: returned_rows=%d", len(records))
    return response


@app.get("/movies/search", response_model=list[dict])
def query_movies(
    columns: list[str] = Query(default=["title"]),
    min_rating: float = Query(default=0, ge=0, le=10),
    limit: int = Query(default=100, ge=1)
):
    """Return id plus the selected fields for movies with rating >= min_rating."""
    try:
        records = storage.query_movies(columns, min_rating, limit)
    except ValueError as error:
        logger.warning("Movie query failed: %s", error)
        raise HTTPException(status_code=400, detail=str(error))

    logger.info(
        "Queried movies: columns=%s, min_rating=%s, returned_rows=%d",
        columns,
        min_rating,
        len(records),
    )
    return records


@app.post("/movies/process", response_model=MovieProcessingReport)
def process_movies(config: ProcessConfig):
    """Validate and process the stored movies using MP1 functions."""
    df = storage.get()
    if df.empty:
        logger.warning("No movies to process")
        raise HTTPException(status_code=400, detail="No movies are stored. Upload a CSV first.")

    config_dict = config.model_dump()
    required_columns = config_dict["validation"]["required_columns"]
    numeric_columns = config_dict["validation"]["numeric_columns"]

    for col in numeric_columns:
        if col not in df.columns:
            logger.warning("Numeric column not found: %s", col)
            raise HTTPException(
                status_code=400,
                detail=f"Numeric column not found: {col}",
            )

    try:
        rows_before_validation = len(df)
        validated = validate_dataframe(df, required_columns, numeric_columns)
        validation_rows_removed = rows_before_validation - len(validated)
        logger.info(
            "Validated movies: rows=%d → %d, removed=%d",
            rows_before_validation,
            len(validated),
            validation_rows_removed,
        )

        validated_copy = validated.copy()
        processed = process_data(validated, config_dict)
    except ValueError as error:
        logger.warning("Processing failed: %s", error)
        raise HTTPException(status_code=400, detail=str(error))

    report = create_cleaning_report(validated_copy, processed)
    storage.save(processed)
    cache.invalidate()

    logger.info(
        "Processed movies: validation_removed=%d, rows_before=%d, rows_after=%d",
        validation_rows_removed,
        report["rows_before"],
        report["rows_after"],
    )
    return {
        "validation_rows_removed": validation_rows_removed,
        "cleaning_report": report,
    }
